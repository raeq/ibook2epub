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

import json
from pathlib import Path

import pytest

from epubconvert.collect import annotations
from epubconvert.run import run
from tests.conftest import make_metadata_package
from tests.test_annotations import highlight, library_row, make_databases
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


class TestNoCopyThroughStillSeesTheCopies:
    """
    ``--no-copy-through`` stopped the library's zipped books and PDFs being
    walked at all, so the claim pass and the orphan check never saw them: a
    package was reported exported from a zipped book's file, a copy whose book
    is still in the library was listed as an orphan, and the zipped books were
    counted as not books. The flag only stops the copying.
    """

    NO_COPY = "--no-copy-through"

    def test_a_package_is_not_exported_from_a_copied_books_file(
        self, tmp_path, output_dir, capsys
    ):
        argv = _copy_then_package(tmp_path, output_dir)
        library = Path(argv[1])
        capsys.readouterr()

        listed = listing(library, output_dir, capsys, self.NO_COPY)
        run.main([*argv, self.NO_COPY])
        captured = capsys.readouterr()

        assert listed == [("Book.epub", "collision")]
        assert "Already exported" not in captured.err
        assert "holds another book, urn:uuid:ZIPPED" in captured.err
        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:ZIPPED"

    def test_a_copy_whose_book_is_in_the_library_is_no_orphan(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        zipped_book(tmp_path, library / "zipped" / "Beta.epub", "urn:uuid:BETA")
        make_metadata_package(
            library / "pkg", "Alpha.epub", title="Alpha", identifier="urn:uuid:ALPHA"
        )
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0"]
        run.main([*argv, "-q"])
        capsys.readouterr()

        run.main(["-s", str(library), "-o", str(output_dir), "--list", self.NO_COPY])
        listed = capsys.readouterr().out
        run.main([*argv, self.NO_COPY])
        ran = capsys.readouterr().out

        assert "orphan" not in listed
        assert "ignored" not in listed
        assert "orphaned" not in ran
        assert "ignored" not in ran

    def test_nothing_is_copied(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        zipped_book(tmp_path, library / "zipped" / "Beta.epub", "urn:uuid:BETA")
        (library / "Paper.pdf").write_bytes(b"%PDF-1.4 fake")

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", self.NO_COPY]
        )

        assert code == 0
        assert not list(output_dir.glob("*.epub"))
        assert not list(output_dir.glob("*.pdf"))
        assert " copied" not in capsys.readouterr().out

    def test_a_vault_still_has_a_note_for_each_book_not_copied(
        self, tmp_path, output_dir, monkeypatch
    ):
        # The highlights are the point of a note, and the book not being
        # copied takes nothing from them.
        library, container = tmp_path / "lib", tmp_path / "container"
        zipped = zipped_book(tmp_path, library / "zipped" / "Beta.epub", "urn:uuid:B")
        paper = library / "papers" / "Gamma.pdf"
        paper.parent.mkdir(parents=True)
        paper.write_bytes(b"%PDF-1.4 fake")
        make_databases(
            container,
            rows=[
                highlight(asset="B", uuid="UB", text="BETA TEXT"),
                highlight(asset="C", uuid="UC", text="GAMMA TEXT"),
            ],
            books=[
                library_row(asset="B", path=str(zipped), title="Beta"),
                library_row(asset="C", path=str(paper), title="Gamma"),
            ],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(container, policy),
        )
        vault = tmp_path / "vault"

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q", self.NO_COPY]
            + ["-ad", str(vault), "--annotations-format", "markdown"]
        )

        assert code == 0
        assert "BETA TEXT" in (vault / "Beta.md").read_text()
        assert "GAMMA TEXT" in (vault / "Gamma.md").read_text()


class TestAPdfOnTheShelfIsWeighed:
    """
    The claim pass looked only for ``*.epub`` on the shelf, so a PDF of the
    same name added earlier in sort order took the name, found the first
    PDF's file there, took it for its own copy and was never copied; the one
    on the shelf was reported as the collision, or under suffix copied again.
    """

    @staticmethod
    def _papers(tmp_path: Path, output_dir: Path, name: str, mode: str) -> list[str]:
        library = tmp_path / "lib"
        (library / "a").mkdir(parents=True)
        (library / "a" / name).write_bytes(b"%PDF-1.4 paper A")
        argv = ["-s", str(library), "-o", str(output_dir), "--on-collision", mode]
        run.main([*argv, "-m", "0", "-q"])
        (library / "0").mkdir()
        (library / "0" / name).write_bytes(b"%PDF-1.4 paper C, a longer one")
        return argv

    @pytest.mark.parametrize("name", ["Paper.pdf", "Scan.PDF"])
    def test_suffix_mode_copies_the_newcomer_beside_it(
        self, tmp_path, output_dir, capsys, name
    ):
        argv = self._papers(tmp_path, output_dir, name, "suffix")
        stem, suffix = name.split(".")

        run.main([*argv, "-m", "0", "-q"])
        capsys.readouterr()
        run.main([*argv, "-m", "0"])
        again = capsys.readouterr()

        shelved = {
            path.name: path.read_bytes()
            for path in output_dir.iterdir()
            if path.suffix.lower() == ".pdf"
        }
        assert shelved == {
            name: b"%PDF-1.4 paper A",
            f"{stem} (2).{suffix}": b"%PDF-1.4 paper C, a longer one",
        }
        assert " copied" not in again.out

    def test_skip_mode_reports_the_newcomer(self, tmp_path, output_dir, capsys):
        argv = self._papers(tmp_path, output_dir, "Paper.pdf", "skip")
        capsys.readouterr()

        run.main([*argv, "--list", "--json"])
        [row] = json.loads(capsys.readouterr().out)

        assert row["status"] == "collision"
        assert Path(row["source"]).parent.name == "0"
        assert (output_dir / "Paper.pdf").read_bytes() == b"%PDF-1.4 paper A"
