"""
Tests for what the claim pass of the files copied through knows of the shelf.

The rules the copies are named by (:func:`~epubconvert.run.copynames.claim_copies`)
were tested against an empty shelf, or one the same library had just written.
A shelf a run finds is not always that: a copy was put there before the package
that now wants its name was added, a book was left out of the run, or a file
is the same size as another book's. Each of these tests starts from such a
shelf.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring

from pathlib import Path

from epubconvert.run import run
from tests.conftest import make_metadata_package
from tests.test_copy_claims import SUFFIX, identifier_of, listing, shelf, zipped_book


def _copy_then_package(tmp_path: Path, output_dir: Path, *extra: str) -> list[str]:
    """Copy a zipped book to the shelf, then add a package of its name."""
    library = tmp_path / "lib"
    zipped_book(tmp_path, library / "zipped" / "Book.epub", "urn:uuid:ZIPPED")
    argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", *extra]
    run.main([*argv, "-q"])
    make_metadata_package(
        library / "pkg", "Book.epub", title="P", identifier="urn:uuid:PACKAGE"
    )
    return argv


class TestSuffixModeKeepsACopyAtItsFile:
    """
    Under ``--on-collision suffix`` a free ``" (n)"`` always exists, so the
    copy that lost its name to a package took ``Book (2).epub`` and was copied
    again, while its original was listed as an orphan and the package was a
    collision on every run. The README says the copy keeps its file.
    """

    def test_the_copy_keeps_its_file_and_the_package_moves_on(
        self, tmp_path, output_dir, capsys
    ):
        argv = _copy_then_package(tmp_path, output_dir, *SUFFIX)
        capsys.readouterr()

        run.main(argv)
        first = capsys.readouterr()

        assert shelf(output_dir) == ["Book (2).epub", "Book.epub"]
        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:ZIPPED"
        assert identifier_of(output_dir / "Book (2).epub") == "urn:uuid:PACKAGE"
        assert "collision" not in first.out + first.err
        assert "copied" not in first.out
        assert "orphan" not in first.out

    def test_it_is_stable_on_rerun(self, tmp_path, output_dir, capsys):
        argv = _copy_then_package(tmp_path, output_dir, *SUFFIX)
        library = Path(argv[1])
        run.main([*argv, "-q"])
        capsys.readouterr()

        run.main(argv)
        again = capsys.readouterr()
        listed = listing(library, output_dir, capsys, *SUFFIX)

        assert "Exported 0 epub file(s)" in again.out
        assert "collision" not in again.out + again.err
        assert "copied" not in again.out
        assert "orphan" not in again.out
        assert shelf(output_dir) == ["Book (2).epub", "Book.epub"]
        assert sorted(listed) == [("Book.epub", "exported")]

    def test_a_copy_added_before_it_in_sort_order_does_not_duplicate_it(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        zipped_book(tmp_path, library / "b" / "Book.epub", "urn:uuid:FIRST")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", *SUFFIX]
        run.main([*argv, "-q"])
        zipped_book(tmp_path, library / "a" / "Book.epub", "urn:uuid:ADDED", "Added")
        capsys.readouterr()

        run.main(argv)
        run.main(argv)
        again = capsys.readouterr()

        assert shelf(output_dir) == ["Book (2).epub", "Book.epub"]
        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:FIRST"
        assert identifier_of(output_dir / "Book (2).epub") == "urn:uuid:ADDED"
        assert "orphan" not in again.out
        assert "collision" not in again.out
