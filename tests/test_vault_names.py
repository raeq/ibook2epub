"""
Tests for which note in a vault a book's highlights are written into.

A note shares its stem with the book's file on the shelf. It was named from
the book's assigned name, not from the file the plan placed the book at: a
book moved on to its marked name, because the file under its plain name holds
another edition, had its highlights written into that edition's note. And a
book with no package -- already zipped, or a PDF -- got no note at all.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

from pathlib import Path

import pytest

from epubconvert.collect import annotations
from epubconvert.run.run import main
from tests.conftest import make_metadata_package
from tests.test_annotations import highlight, library_row, make_databases

PLAIN_NOTE = "Frank Herbert - Dune.md"


def _read_from(monkeypatch: pytest.MonkeyPatch, container: Path) -> None:
    monkeypatch.setattr(
        "epubconvert.run.annotating.collect_annotations",
        lambda policy=None: annotations.collect(container, policy),
    )


def _edition(library: Path, container: Path, asset: str, identifier: str) -> None:
    """Put one edition of Dune in *library*, with one highlight in Apple's."""
    book = make_metadata_package(
        library,
        "Dune.epub",
        title="Dune",
        creator="Frank Herbert",
        identifier=identifier,
    )
    make_databases(
        container,
        rows=[highlight(asset=asset, uuid=f"U{asset}", text=f"EDITION {asset} TEXT")],
        books=[library_row(asset=asset, path=str(book), title="Dune")],
    )


class TestANoteFollowsTheBooksArchive:
    """
    Edition A is exported, leaves the library, and edition B arrives: A's
    archive keeps the plain name and B moves on to its marked name.
    """

    FLAGS = ["-m", "0", "-q", "--name-by", "author-title", "--on-collision", "suffix"]

    def _replace_the_edition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vault: list[str]
    ) -> tuple[Path, Path, Path]:
        container, output = tmp_path / "container", tmp_path / "out"
        _read_from(monkeypatch, container)
        first = tmp_path / "lib1"
        _edition(first, container, "A", "urn:isbn:9780441013593")
        assert main(["-s", str(first), "-o", str(output), *self.FLAGS, *vault]) == 0
        second = tmp_path / "lib2"
        _edition(second, container, "B", "urn:isbn:9780593099322")
        return second, output, container

    @staticmethod
    def _notes(vault: Path) -> dict[str, str]:
        return {note.name: note.read_text() for note in vault.glob("*.md")}

    def test_after_a_conversion(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        flags = ["-ad", str(vault), "--annotations-format", "markdown"]
        library, output, _ = self._replace_the_edition(tmp_path, monkeypatch, flags)

        assert main(["-s", str(library), "-o", str(output), *self.FLAGS, *flags]) == 0

        [moved] = [
            path for path in output.glob("*.epub") if path.stem + ".md" != (PLAIN_NOTE)
        ]
        notes = self._notes(vault)
        assert "EDITION A TEXT" in notes[PLAIN_NOTE]
        assert "EDITION B TEXT" not in notes[PLAIN_NOTE]
        assert "EDITION B TEXT" in notes[moved.stem + ".md"]

    def test_after_a_refresh(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        flags = ["-ad", str(vault), "--annotations-format", "markdown"]
        library, output, _ = self._replace_the_edition(tmp_path, monkeypatch, flags)
        assert main(["-s", str(library), "-o", str(output), *self.FLAGS]) == 0

        code = main(
            ["-s", str(library), "-o", str(output), "-ae", "-ar", "-q"]
            + self.FLAGS[3:]
            + flags
        )

        assert code == 0
        [moved] = [
            path for path in output.glob("*.epub") if path.stem + ".md" != (PLAIN_NOTE)
        ]
        notes = self._notes(vault)
        assert "EDITION B TEXT" not in notes[PLAIN_NOTE]
        assert "EDITION B TEXT" in notes[moved.stem + ".md"]
