"""
A report that cannot be written ends the run cleanly, never in a traceback.

``--list``, ``--verify`` and a conversion's summary are written to standard
output, and to standard error when ``-ad -`` has standard output for its
document. A reader closing the pipe early is saying they have seen enough;
anything else that stops the write is a report lost, and a script is told.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import errno
import io
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TextIO

import pytest

from epubconvert.run import run
from epubconvert.utils import exits
from tests.conftest import make_metadata_package, make_package


@pytest.fixture(name="closed_pipe")
def _closed_pipe() -> Iterator[TextIO]:
    """
    A pipe whose reader has already gone, as after ``head -c0``. Each test
    puts it in place itself: pytest restores its own capture between a
    fixture's setup and the test.
    """
    reader, writer = os.pipe()
    os.close(reader)
    with os.fdopen(writer, "w", encoding="utf-8") as stream:
        yield stream


class TestASummaryOnAClosedStandardError:
    """
    Under ``-ad -`` the summary goes to standard error, and
    ``... -ad - 2>&1 | head -c0`` ended in a BrokenPipeError traceback and
    exit 1 after every book was written.
    """

    def test_ends_quietly(self, tmp_path, monkeypatch, closed_pipe):
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations", lambda **_kwargs: []
        )
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", closed_pipe)

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "out"), "-m", "0", "-q"]
            + ["-ae", "-ad", "-"]
        )
        closed_pipe.flush()

        assert code == exits.SUCCESS
        assert (tmp_path / "out" / "Book.epub").exists()


class _Failing(io.StringIO):
    """
    Standard output on a device that refuses every write, with the error
    given. Its descriptor is a real one, for the run to point elsewhere.
    """

    def __init__(self, descriptor: int, error: OSError) -> None:
        super().__init__()
        self.descriptor = descriptor
        self.error = error

    def write(self, _text: str) -> int:
        raise self.error

    def fileno(self) -> int:
        return self.descriptor


@pytest.fixture(name="failing")
def _failing() -> Iterator[Callable[[OSError], _Failing]]:
    """Make a standard output that fails with the error given."""
    reader, writer = os.pipe()
    try:
        yield lambda error: _Failing(writer, error)
    finally:
        os.close(reader)
        os.close(writer)


#: A full disk, as ``> /dev/full`` gives one.
FULL = OSError(errno.ENOSPC, "No space left on device")


class TestAReportThatCannotBeWritten:
    """
    ``emit`` handled only a closed pipe. Any other error writing standard
    output -- ``--list > /dev/full`` -- was a traceback and exit 1, from
    ``--list``, ``--verify`` and a conversion's summary alike.
    """

    @staticmethod
    def _shelf(tmp_path: Path) -> list[str]:
        library = tmp_path / "lib"
        make_metadata_package(library, "Book.epub", title="Book")
        base = ["-s", str(library), "-o", str(tmp_path / "out")]
        assert run.main([*base, "-m", "0", "-q"]) == exits.SUCCESS
        return base

    @pytest.mark.parametrize(
        "mode",
        [
            pytest.param(["--list"], id="list"),
            pytest.param(["--list", "--json"], id="json"),
            pytest.param(["--verify"], id="verify"),
            pytest.param(["-m", "0", "--force"], id="convert"),
        ],
    )
    def test_is_said_once_and_exits_5(
        self, tmp_path, monkeypatch, capsys, failing, mode
    ):
        base = self._shelf(tmp_path)
        capsys.readouterr()
        monkeypatch.setattr(sys, "stdout", failing(FULL))

        code = run.main([*base, *mode])

        err = capsys.readouterr().err
        assert code == exits.NO_OUTPUT
        assert (
            err.count(
                "Could not write the report to standard output: No space left on device"
            )
            == 1
        )

    def test_a_damaged_shelf_still_exits_7(
        self, tmp_path, monkeypatch, capsys, failing
    ):
        base = self._shelf(tmp_path)
        (tmp_path / "out" / "Book.epub").write_bytes(b"CORRUPTED")
        monkeypatch.setattr(sys, "stdout", failing(FULL))

        code = run.main([*base, "--verify"])

        assert code == exits.DAMAGED
        assert "Could not write the report" in capsys.readouterr().err

    def test_the_reason_is_escaped(self, tmp_path, monkeypatch, capsys, failing):
        base = self._shelf(tmp_path)
        monkeypatch.setattr(
            sys, "stdout", failing(OSError(errno.EIO, "odd\x1b[2K\u202eerror"))
        )

        code = run.main([*base, "--list"])

        err = capsys.readouterr().err
        assert code == exits.NO_OUTPUT
        assert "odd\\x1b[2K\\u202eerror" in err
        assert "\x1b" not in err

    def test_the_next_run_starts_afresh(self, tmp_path, monkeypatch, failing):
        base = self._shelf(tmp_path)
        monkeypatch.setattr(sys, "stdout", failing(FULL))
        run.main([*base, "--list"])
        monkeypatch.setattr(sys, "stdout", io.StringIO())

        assert run.main([*base, "--list"]) == exits.SUCCESS

    @pytest.mark.skipif(not Path("/dev/full").exists(), reason="no /dev/full here")
    def test_for_real(self, tmp_path, monkeypatch, capsys):
        base = self._shelf(tmp_path)
        with Path("/dev/full").open("w", encoding="utf-8") as full:
            monkeypatch.setattr(sys, "stdout", full)

            code = run.main([*base, "--list"])

        assert code == exits.NO_OUTPUT
        assert "No space left on device" in capsys.readouterr().err
