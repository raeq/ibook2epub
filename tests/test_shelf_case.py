"""
Tests for books on the shelf whose extension is not written in lower case.

A book copied through keeps the name it arrived with, and copy-through picks
its files by ``suffix.lower()``, so ``Foo.EPUB`` lands on the shelf as that.
``--verify`` looked for ``*.epub``, which is case-sensitive, and never read it.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring

from pathlib import Path

from epubconvert.export import inspect_output
from epubconvert.export.archive import PARTIAL_PREFIX
from epubconvert.run.run import main
from epubconvert.utils import exits


class TestVerifyReadsEveryBookOnTheShelf:
    def test_a_damaged_book_named_in_upper_case_is_found(self, tmp_path: Path):
        (tmp_path / "Foo.EPUB").write_bytes(b"this is not a zip")
        (tmp_path / "Bar.Epub").write_bytes(b"nor is this")

        checked, damaged, broken = inspect_output.verify_output(tmp_path)

        assert (checked, damaged) == (2, 2)
        assert sorted(broken) == ["Bar.Epub", "Foo.EPUB"]

    def test_what_is_not_a_book_is_still_left_out(self, tmp_path: Path):
        (tmp_path / "Folder.EPUB").mkdir()
        (tmp_path / f"{PARTIAL_PREFIX}abc.EPUB").write_bytes(b"half written")
        (tmp_path / "notes.txt").write_bytes(b"mine")

        assert inspect_output.verify_output(tmp_path)[:2] == (0, 0)

    def test_a_directory_that_is_not_there_holds_nothing(self, tmp_path: Path):
        # As the glob it replaced answered; the caller says it is missing.
        assert inspect_output.verify_output(tmp_path / "gone") == (0, 0, [])

    def test_a_book_copied_through_in_upper_case_is_verified(self, tmp_path: Path):
        library, shelf = tmp_path / "lib", tmp_path / "out"
        library.mkdir()
        (library / "Foo.EPUB").write_bytes(b"a damaged sideloaded book")
        main(["-s", str(library), "-o", str(shelf), "-q"])
        assert (shelf / "Foo.EPUB").is_file()

        code = main(["-s", str(library), "-o", str(shelf), "--verify", "-q"])

        assert code == exits.DAMAGED
