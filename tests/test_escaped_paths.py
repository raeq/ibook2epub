"""
Tests that the paths a user typed reach the terminal escaped.

A book's name was escaped everywhere, but the output directory was logged as
given in half a dozen messages: "Writing output to", the file in its way,
the unwritable shelf, the --min-free floor and the lock refusals. A path is
typed rather than sideloaded, yet a script can build one from a book's title,
and the rest of the tool already treats every name it prints as input.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring

import os
from pathlib import Path

import pytest

from epubconvert.run import run
from epubconvert.run.convert import LOCK_NAME
from tests.conftest import make_package

ERASE = "\x1b[2K\r"


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    code = run.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out + captured.err


@pytest.fixture(name="library")
def _library(tmp_path: Path) -> Path:
    library = tmp_path / "lib"
    make_package(library, "Book.epub")
    return library


class TestTheOutputDirectoryIsLoggedEscaped:
    def test_where_the_books_are_written(self, tmp_path, library, capsys):
        output = tmp_path / f"shelf{ERASE}"

        code, said = _run(capsys, "-s", str(library), "-o", str(output), "-m", "0")

        assert code == 0
        assert "\x1b" not in said and "\r" not in said

    def test_a_file_in_its_way(self, tmp_path, library, capsys):
        output = tmp_path / f"shelf{ERASE}"
        output.write_text("not a directory", encoding="utf-8")

        code, said = _run(capsys, "-s", str(library), "-o", str(output))

        assert code == 5
        assert "\x1b" not in said and "\r" not in said

    def test_the_min_free_floor(self, tmp_path, library, capsys):
        output = tmp_path / f"shelf{ERASE}"

        code, said = _run(
            capsys, "-s", str(library), "-o", str(output), "--min-free", "999999999"
        )

        assert code == 1
        assert "\x1b" not in said and "\r" not in said

    def test_a_lock_file_that_is_not_a_plain_file(self, tmp_path, library, capsys):
        output = tmp_path / f"shelf{ERASE}"
        output.mkdir()
        (output / LOCK_NAME).symlink_to(tmp_path / "elsewhere")

        code, said = _run(capsys, "-s", str(library), "-o", str(output))

        assert code == 5
        assert "\x1b" not in said and "\r" not in said
        # Not "is not writable": the lock file is refused for what it is.
        assert "not a plain file" in said


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_the_lock_is_refused_the_same_way_by_a_dry_run(tmp_path, library, capsys):
    output = tmp_path / "shelf"
    output.mkdir()
    (output / LOCK_NAME).symlink_to(tmp_path / "elsewhere")

    code, said = _run(capsys, "-s", str(library), "-o", str(output), "-d")

    assert code == 5
    assert "not a plain file" in said
