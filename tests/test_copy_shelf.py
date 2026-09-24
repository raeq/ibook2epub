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
        assert sorted(listed) == [("Book.epub", "copied"), ("Book.epub", "exported")]

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

    def test_a_copy_keeps_a_numbered_file(self, tmp_path, output_dir, capsys):
        # A run narrowed to the second of two editions copied it as "(2)";
        # a package of their name then took the plain name, and the first
        # edition, claiming the next free name, took the second's file.
        # formal/RerunPlanner.tla found it.
        library = tmp_path / "lib"
        for folder, identifier in (("a", "urn:uuid:1965"), ("b", "urn:uuid:ACE")):
            zipped_book(
                tmp_path, library / folder / f"Dune {folder}.epub", identifier, "Dune"
            )
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", *SUFFIX]
        argv += ["--name-by", "author-title"]
        run.main([*argv, "-q", "--match", "dune b"])
        make_metadata_package(
            library / "c",
            "Dune c.epub",
            title="Dune",
            creator="Frank Herbert",
            identifier="urn:uuid:PKG",
        )
        run.main([*argv, "-q"])
        capsys.readouterr()

        run.main(argv)
        again = capsys.readouterr()

        held = {path.name: identifier_of(path) for path in output_dir.glob("*.epub")}
        assert held["Frank Herbert - Dune (2).epub"] == "urn:uuid:ACE"
        assert sorted(held.values()) == [
            "urn:uuid:1965",
            "urn:uuid:ACE",
            "urn:uuid:PKG",
        ]
        assert "orphan" not in again.out
        assert "Exported 0" in again.out


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
        rows = json.loads(capsys.readouterr().out)

        assert {Path(row["source"]).parent.name: row["status"] for row in rows} == {
            "0": "collision",
            "a": "copied",
        }
        assert (output_dir / "Paper.pdf").read_bytes() == b"%PDF-1.4 paper A"


class TestTwoZippedBooksOfOneSize:
    """
    A copy was taken for its own file on the shelf by its size alone. Two
    different zipped books of one name and one size: the one added later,
    earlier in sort order, found the other's file and was never copied, with
    nothing said. Where two books want one name, the identifiers decide.
    """

    @staticmethod
    def _equal_sizes(tmp_path: Path, output_dir: Path, mode: str) -> list[str]:
        library = tmp_path / "lib"
        first = zipped_book(tmp_path, library / "a" / "Book.epub", "urn:uuid:A", "A")
        argv = ["-s", str(library), "-o", str(output_dir), "--on-collision", mode]
        run.main([*argv, "-m", "0", "-q"])
        added = zipped_book(tmp_path, library / "0" / "Book.epub", "urn:uuid:C", "C")
        assert added.stat().st_size == first.stat().st_size
        return argv

    def test_skip_mode_reports_the_newcomer(self, tmp_path, output_dir, capsys):
        argv = self._equal_sizes(tmp_path, output_dir, "skip")
        capsys.readouterr()

        run.main([*argv, "-m", "0"])
        captured = capsys.readouterr()

        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:A"
        assert "1 name collision(s)" in captured.out
        assert "this book is urn:uuid:C" in captured.err

    def test_suffix_mode_copies_the_newcomer_beside_it(
        self, tmp_path, output_dir, capsys
    ):
        argv = self._equal_sizes(tmp_path, output_dir, "suffix")

        run.main([*argv, "-m", "0", "-q"])
        capsys.readouterr()
        run.main([*argv, "-m", "0"])
        again = capsys.readouterr()

        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:A"
        assert identifier_of(output_dir / "Book (2).epub") == "urn:uuid:C"
        assert " copied" not in again.out
        assert "collision" not in again.out


class TestHighlightsOfABookCopiedThrough:
    """
    ``-ae`` embeds as each book is converted, and a book copied through is
    copied byte for byte, so its highlights went in nowhere; the warning about
    highlights that reached no file left the copies out, and ``-ae -ar``
    walked the packages alone. Both said nothing.
    """

    WARNING = "copied through unchanged were not embedded"

    @staticmethod
    def _library(tmp_path: Path, monkeypatch) -> Path:
        library, container = tmp_path / "lib", tmp_path / "container"
        alpha = make_metadata_package(
            library / "pkg", "Alpha.epub", title="Alpha", identifier="urn:uuid:A"
        )
        beta = zipped_book(tmp_path, library / "zipped" / "Beta.epub", "urn:uuid:B")
        make_databases(
            container,
            rows=[
                highlight(asset="A", uuid="UA", text="ALPHA TEXT"),
                highlight(asset="B", uuid="UB1", text="BETA ONE"),
                highlight(asset="B", uuid="UB2", text="BETA TWO"),
            ],
            books=[
                library_row(asset="A", path=str(alpha), title="Alpha"),
                library_row(asset="B", path=str(beta), title="Beta"),
            ],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(container, policy),
        )
        return library

    def test_a_conversion_says_so(self, tmp_path, output_dir, monkeypatch, capsys):
        library = self._library(tmp_path, monkeypatch)

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-ae"])
        err = capsys.readouterr().err

        assert code == 0
        assert (
            f"2 annotation(s) from 1 book(s) {self.WARNING} "
            "(copies are byte-for-byte): Beta.epub. Use -ad FILE or -ao FILE."
        ) in err
        assert "reached no file" not in err

    def test_a_refresh_says_so(self, tmp_path, output_dir, monkeypatch, capsys):
        library = self._library(tmp_path, monkeypatch)
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        capsys.readouterr()

        code = run.main(["-s", str(library), "-o", str(output_dir), "-ae", "-ar"])

        assert code == 0
        assert f"2 annotation(s) from 1 book(s) {self.WARNING}" in (
            capsys.readouterr().err
        )

    def test_a_long_list_is_cut_short(self, tmp_path, output_dir, monkeypatch, capsys):
        library, container = tmp_path / "lib", tmp_path / "container"
        library.mkdir()
        papers = []
        for index in range(5):
            papers.append(library / f"Paper {index}.pdf")
            papers[-1].write_bytes(b"%PDF-1.4 " + bytes([48 + index]))
        make_databases(
            container,
            rows=[highlight(asset=f"P{i}", uuid=f"U{i}") for i in range(5)],
            books=[
                library_row(asset=f"P{i}", path=str(paper))
                for i, paper in enumerate(papers)
            ],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(container, policy),
        )

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-ae"])

        assert (
            "Paper 0.pdf, Paper 1.pdf, Paper 2.pdf, and 2 more. Use -ad FILE"
        ) in capsys.readouterr().err

    def test_a_book_not_copied_says_why(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = self._library(tmp_path, monkeypatch)

        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-ae"]
            + ["--no-copy-through"]
        )

        assert (
            "2 annotation(s) from 1 book(s) not copied (--no-copy-through) were "
            "not embedded: Beta.epub."
        ) in capsys.readouterr().err

    def test_a_detached_file_is_where_they_went(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = self._library(tmp_path, monkeypatch)

        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-ae"]
            + ["-ad", str(tmp_path / "highlights.json")]
        )

        assert self.WARNING not in capsys.readouterr().err

    def test_a_copy_that_lost_its_name_is_counted(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library, container = tmp_path / "lib", tmp_path / "container"
        for folder, identifier in (("a", "urn:uuid:1965"), ("b", "urn:uuid:ACE")):
            zipped_book(
                tmp_path, library / folder / f"Dune {folder}.epub", identifier, "Dune"
            )
        make_databases(
            container,
            rows=[highlight(asset="B", uuid="UB", text="ACE TEXT")],
            books=[library_row(asset="B", path=str(library / "b" / "Dune b.epub"))],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(container, policy),
        )

        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-ae"]
            + ["--name-by", "author-title"]
        )

        assert f"1 annotation(s) from 1 book(s) {self.WARNING}" in (
            capsys.readouterr().err
        )


class TestALibraryOfOnlyCopies:
    """
    "No matching *.epub packages found" and then "Copied Paper.pdf": a run
    over a library of PDFs, or a --match naming only one, read as having
    found nothing and then done something.
    """

    def test_it_does_not_say_it_found_nothing(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Paper.pdf").write_bytes(b"%PDF-1.4 fake")
        make_metadata_package(library, "Dune.epub", title="Dune")

        run.main(["-s", str(library), "-o", str(output_dir), "--match", "paper"])
        captured = capsys.readouterr()

        assert "No matching" not in captured.err
        assert "1 copied" in captured.out

    def test_an_empty_selection_still_says_so(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Paper.pdf").write_bytes(b"%PDF-1.4 fake")

        run.main(["-s", str(library), "-o", str(output_dir), "--match", "nothing"])

        assert "No matching" in capsys.readouterr().err
