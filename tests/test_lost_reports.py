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
import json
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TextIO

import pytest

from epubconvert.collect import annotations
from epubconvert.run import run
from epubconvert.utils import exits
from epubconvert.utils.display import emit
from tests.conftest import make_metadata_package, make_package
from tests.test_annotations import highlight, library_row, make_databases


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
    def shelf(tmp_path: Path) -> list[str]:
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
        base = self.shelf(tmp_path)
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
        base = self.shelf(tmp_path)
        (tmp_path / "out" / "Book.epub").write_bytes(b"CORRUPTED")
        monkeypatch.setattr(sys, "stdout", failing(FULL))

        code = run.main([*base, "--verify"])

        assert code == exits.DAMAGED
        assert "Could not write the report" in capsys.readouterr().err

    def test_the_reason_is_escaped(self, tmp_path, monkeypatch, capsys, failing):
        base = self.shelf(tmp_path)
        monkeypatch.setattr(
            sys, "stdout", failing(OSError(errno.EIO, "odd\x1b[2K\u202eerror"))
        )

        code = run.main([*base, "--list"])

        err = capsys.readouterr().err
        assert code == exits.NO_OUTPUT
        assert "odd\\x1b[2K\\u202eerror" in err
        assert "\x1b" not in err

    def test_the_next_run_starts_afresh(self, tmp_path, monkeypatch, failing):
        base = self.shelf(tmp_path)
        monkeypatch.setattr(sys, "stdout", failing(FULL))
        run.main([*base, "--list"])
        monkeypatch.setattr(sys, "stdout", io.StringIO())

        assert run.main([*base, "--list"]) == exits.SUCCESS

    @pytest.mark.skipif(not Path("/dev/full").exists(), reason="no /dev/full here")
    def test_for_real(self, tmp_path, monkeypatch, capsys):
        base = self.shelf(tmp_path)
        with Path("/dev/full").open("w", encoding="utf-8") as full:
            monkeypatch.setattr(sys, "stdout", full)

            code = run.main([*base, "--list"])

        assert code == exits.NO_OUTPUT
        assert "No space left on device" in capsys.readouterr().err


class _AsciiText:
    """A text stream with no bytes beneath it that holds only ASCII."""

    encoding = "ascii"

    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, text: str) -> int:
        str(bytes(text, "ascii"), "ascii")  # Raises as an ASCII stream does.
        self.written.append(text)
        return len(text)

    def flush(self) -> None:
        pass


class TestAReportUnderAnEncodingThatIsNotUTF8:
    """
    Under ``PYTHONIOENCODING=ascii`` or a Latin-1 locale, the first title in
    another script ended --list, --verify and a run's summary in a
    UnicodeEncodeError traceback and exit 1, after the books were written.
    The report goes out as UTF-8, as the documents on standard output do.
    """

    @pytest.mark.parametrize(
        "mode",
        [
            pytest.param(["--list"], id="list"),
            pytest.param(["--list", "--json"], id="json"),
            pytest.param(["--verify"], id="verify"),
            pytest.param(["-m", "0", "--force"], id="convert"),
        ],
    )
    def test_it_is_written_as_utf8(self, tmp_path, monkeypatch, mode):
        library = tmp_path / "lib"
        make_metadata_package(library, "Café 日本.epub", title="Café 日本")
        base = ["-s", str(library), "-o", str(tmp_path / "Café 日本")]
        assert run.main([*base, "-m", "0", "-q"]) == exits.SUCCESS
        written = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(written, "ascii"))

        code = run.main([*base, *mode])

        sys.stdout.flush()
        assert code == exits.SUCCESS
        assert "Café 日本" in str(written.getvalue(), "utf-8")

    def test_a_stream_with_no_bytes_beneath_it_is_escaped(self, monkeypatch):
        stream = _AsciiText()
        monkeypatch.setattr(sys, "stdout", stream)

        emit("Café 日本")

        assert "".join(stream.written) == "Caf\\xe9 \\u65e5\\u672c\n"


class TestAStreamThatIsClosed:
    """
    A standard stream closed before the run started (``>&-``, ``2>&-``) is
    None in Python. ``emit`` passed that on to ``print``, which took it for
    standard output: under ``-ad -`` the summary meant for standard error
    landed in the middle of the JSON there, and ``--list >&-`` lost its
    listing silently and exited 0.
    """

    def test_a_listing_to_a_closed_standard_output_is_lost(
        self, tmp_path, monkeypatch, capsys
    ):
        base = TestAReportThatCannotBeWritten.shelf(tmp_path)
        capsys.readouterr()
        monkeypatch.setattr(sys, "stdout", None)

        code = run.main([*base, "--list"])

        assert code == exits.NO_OUTPUT
        assert "Could not write the report to standard output" in (
            capsys.readouterr().err
        )

    def test_a_summary_to_a_closed_standard_error_stays_out_of_the_document(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations", lambda **_kwargs: []
        )
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        document = io.StringIO()
        monkeypatch.setattr(sys, "stdout", document)
        monkeypatch.setattr(sys, "stderr", None)

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "out"), "-m", "0", "-q"]
            + ["-ae", "-ad", "-"]
        )

        assert code == exits.SUCCESS
        assert json.loads(document.getvalue())["annotations"] == []
        assert (tmp_path / "out" / "Book.epub").exists()


class TestADocumentOnStandardOutput:
    """
    ``-ao -`` writes its document through its own path to standard output,
    which handled only a closed pipe: ``-ao - > /dev/full`` and ``-ao - >&-``
    each ended in a traceback and exit 1.
    """

    @pytest.fixture(name="highlights")
    def _highlights(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        make_databases(
            tmp_path / "container", rows=[highlight()], books=[library_row()]
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None, **_kwargs: annotations.collect(
                tmp_path / "container", policy
            ),
        )
        return ["-s", str(tmp_path / "lib"), "-o", str(tmp_path / "out"), "-ao", "-"]

    def test_that_cannot_be_written_is_said_once_and_exits_5(
        self, highlights, monkeypatch, capsys, failing
    ):
        monkeypatch.setattr(sys, "stdout", failing(FULL))

        code = run.main(highlights)

        assert code == exits.NO_OUTPUT
        assert capsys.readouterr().err.count("No space left on device") == 1

    def test_to_a_closed_standard_output_exits_5(self, highlights, monkeypatch, capsys):
        monkeypatch.setattr(sys, "stdout", None)

        code = run.main(highlights)

        assert code == exits.NO_OUTPUT
        assert "Could not write the report to standard output" in (
            capsys.readouterr().err
        )


class TestACtrlCAsTheRunEnds:
    def test_while_it_asks_whether_the_report_was_lost(
        self, tmp_path, monkeypatch, capsys
    ):
        # Asked after main's handler, so a Ctrl-C there was a traceback and
        # exit 1 once every book had been written.
        make_package(tmp_path / "lib", "Book.epub")

        def interrupted() -> bool:
            raise KeyboardInterrupt

        monkeypatch.setattr("epubconvert.utils.display.report_lost", interrupted)

        code = run.main(["-s", str(tmp_path / "lib"), "-o", str(tmp_path / "out")])

        assert code == exits.INTERRUPTED
        assert "Interrupted; rerun to continue." in capsys.readouterr().err
        assert (tmp_path / "out" / "Book.epub").is_file()
