"""
Tests for what ``-ae`` says of the highlights of books ``-m`` held back.

A run capped by ``-m`` converts some books and holds the rest back for the
next run, and says so in its summary. The warning about highlights that
reached no file counted the books held back too, and told the reader they
had nowhere to go, pointing at DRM, when the next run would embed them.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import re
from pathlib import Path

import pytest

from epubconvert.collect import annotations
from epubconvert.run import run
from tests.conftest import make_metadata_package
from tests.test_annotations import highlight, library_row, make_databases


def _annotated_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    library, container = tmp_path / "lib", tmp_path / "container"
    books = [
        make_metadata_package(
            library, f"Book{index}.epub", title=f"B{index}", identifier=f"urn:{index}"
        )
        for index in range(8)
    ]
    make_databases(
        container,
        rows=[highlight(uuid=f"U{index}", asset=f"A{index}") for index in range(8)],
        books=[
            library_row(asset=f"A{index}", title=f"Book {index}", path=str(book))
            for index, book in enumerate(books)
        ],
    )
    monkeypatch.setattr(
        "epubconvert.run.annotating.collect_annotations",
        lambda policy=None: annotations.collect(container, policy),
    )
    return library


def _protect(package: Path) -> None:
    """Give *package* Apple's DRM, so it can never be converted."""
    (package / "META-INF" / "encryption.xml").write_text(
        '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">'
        '<EncryptionMethod Algorithm="http://www.apple.com/technology/fps"/>'
        '<CipherData><CipherReference URI="OEBPS/text/chapter1.xhtml"/>'
        "</CipherData></EncryptedData></encryption>",
        encoding="utf-8",
    )


class TestBooksHeldBackByTheCap:
    def test_are_not_said_to_have_reached_no_file(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = _annotated_library(tmp_path, monkeypatch)

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "5", "-ae"])
        captured = capsys.readouterr()

        assert code == 0
        assert "3 held back" in captured.out
        assert "reached no file" not in captured.err
        assert (
            "3 annotation(s) wait for books -m held back; they go in when those "
            "are converted."
        ) in captured.err

    def test_a_book_that_cannot_be_converted_still_is(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = _annotated_library(tmp_path, monkeypatch)
        _protect(library / "Book7.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "5", "-ae"])
        err = capsys.readouterr().err

        assert "1 annotation(s) from 1 book(s) reached no file: Book7.epub." in err
        assert "2 annotation(s) wait for books -m held back" in err


class TestBooksTheFloorStopped:
    """
    A book the --min-free floor kept from starting was not attempted, like a
    book -m held back, and the summary sends it back to a rerun. Its
    highlights were said to have reached no file, with the DRM advice.
    """

    def test_before_the_first_book_are_not_said_to_have_reached_no_file(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = _annotated_library(tmp_path, monkeypatch)
        monkeypatch.setattr("epubconvert.run.convert.free_megabytes", lambda _path: 1)

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-ae", "-m", "0"]
            + ["--min-free", "10"]
        )
        captured = capsys.readouterr()

        assert code == 1
        assert "8 not attempted" in captured.out
        assert "reached no file" not in captured.err
        assert (
            "8 annotation(s) wait for books the --min-free floor stopped; they go "
            "in when a rerun converts those."
        ) in captured.err

    def test_part_way_through_are_not_said_to_have_reached_no_file(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        # One worker, so every book measures the volume: room for the check
        # before the run and for the first book, and none after.
        library = _annotated_library(tmp_path, monkeypatch)
        measured: list[Path] = []

        def filling(path: Path) -> int:
            measured.append(path)
            return 100 if len(measured) <= 2 else 1

        monkeypatch.setattr("epubconvert.run.convert.free_megabytes", filling)

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-ae", "-m", "0", "-w", "1"]
            + ["--min-free", "10"]
        )
        captured = capsys.readouterr()

        assert code == 1
        assert "Exported 1 epub file(s)" in captured.out
        assert "reached no file" not in captured.err
        assert "7 annotation(s) wait for books the --min-free floor stopped" in (
            captured.err
        )


class TestTheReadmeQuotesTheWarning:
    def test_the_sample_reads_as_the_tool_prints_it(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        # The README's sample had an em dash where the tool prints "--".
        library = _annotated_library(tmp_path, monkeypatch)
        _protect(library / "Book7.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-ae"])
        [printed] = [
            " ".join(line.split(" reached no file: ", 1)[1].split(". ", 1)[1].split())
            for line in capsys.readouterr().err.splitlines()
            if " reached no file: " in line
        ]
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )
        [sample] = re.findall(r"```text\n([^`]* reached no file: [^`]*)```", readme)

        assert " ".join(sample.split(" more. ", 1)[1].split()) == printed
