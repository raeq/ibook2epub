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
        (library / "Book7.epub" / "META-INF" / "encryption.xml").write_text(
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">'
            '<EncryptionMethod Algorithm="http://www.apple.com/technology/fps"/>'
            '<CipherData><CipherReference URI="OEBPS/text/chapter1.xhtml"/>'
            "</CipherData></EncryptedData></encryption>",
            encoding="utf-8",
        )

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "5", "-ae"])
        err = capsys.readouterr().err

        assert "1 annotation(s) from 1 book(s) reached no file: Book7.epub." in err
        assert "2 annotation(s) wait for books -m held back" in err
