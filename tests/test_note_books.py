"""
Tests for a note knowing which book it is the note of.

``-ad`` named a book's note after the file the plan placed the book at, and
``-ao`` after the name the book was assigned. Edition A exported and gone from
the library holds the plain name; edition B, moved on to its marked name, then
had ``-ao`` write its highlights over A's note. ``-ao`` now names notes from
the shelf as ``-ad`` does, and a note carries a tag for its book in its start
marker, so no route can rewrite one book's note with another's highlights.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=protected-access

from pathlib import Path
from typing import Any

import pytest

from epubconvert.collect import annotations
from epubconvert.export import notes
from epubconvert.run.run import main
from epubconvert.utils import app_logger, exits
from epubconvert.utils.policy import Assignment
from tests.test_vault_names import PLAIN_NOTE, _edition


def _highlight(asset: str | None, text: str) -> dict[str, Any]:
    book: dict[str, Any] = {"title": "Dune", "source": "Dune.epub"}
    if asset is not None:
        book["assetId"] = asset
    return {"id": text, "text": text, "book": book}


def _untagged(note: str) -> str:
    """Render *note* as a version without book tags wrote it."""
    return notes.BOOK_TAG.sub("", note, count=1)


def _book(note: str) -> str | None:
    held = notes.split(note)
    assert held is not None
    return held.book


class TestTheMarkerNamesTheBook:
    def test_a_note_is_tagged_for_its_book(self):
        book = _book(notes.compose([_highlight("A", "hl")]))

        assert book is not None
        assert book != _book(notes.compose([_highlight("B", "hl")]))

    def test_the_tag_keeps_every_round_trip(self):
        note = notes.compose([_highlight("A", "hl")])

        assert notes.is_ours(note) is True
        assert notes.wrote_it(note) is True
        assert notes.rewrite(note, [_highlight("A", "hl")]) == note

    def test_a_book_without_an_asset_id_gets_no_tag(self):
        note = notes.compose([_highlight(None, "hl")])

        assert " book=" not in note
        assert _book(note) is None

    def test_a_forged_tagged_marker_is_escaped(self):
        forged = notes.compose([_highlight("A", "hl")]).split("\n")
        [marker] = [line for line in forged if line.startswith("<!-- ibook2epub sha")]

        assert notes._escape(marker) == "\\" + marker


class TestAnOlderNoteWithoutATag:
    def test_it_is_still_ours(self):
        note = _untagged(notes.compose([_highlight("A", "hl")]))

        assert " book=" not in note
        assert notes.is_ours(note) is True

    def test_a_rerun_with_nothing_new_leaves_its_bytes_alone(self, tmp_path: Path):
        # A vault kept in git stays quiet: the tag alone is no reason to write.
        target = tmp_path / "Dune.md"
        target.write_text(_untagged(notes.compose([_highlight("A", "hl")])))
        before = target.read_bytes()

        assert notes._write_one(target, [_highlight("A", "hl")]) == "unchanged"
        assert target.read_bytes() == before

    def test_it_is_tagged_when_its_region_is_rewritten_anyway(self, tmp_path: Path):
        target = tmp_path / "Dune.md"
        target.write_text(_untagged(notes.compose([_highlight("A", "hl")])))

        found = [_highlight("A", "hl"), _highlight("A", "new")]
        assert notes._write_one(target, found) == "written"

        assert _book(target.read_text()) is not None


class TestANoteTaggedForAnotherBook:
    def test_it_is_not_rewritten(self, tmp_path: Path):
        target = tmp_path / "Dune.md"
        target.write_text(notes.compose([_highlight("A", "A TEXT")]))
        before = target.read_bytes()

        outcome = notes._write_one(target, [_highlight("B", "B TEXT")])

        assert outcome == "another"
        assert target.read_bytes() == before

    def test_nor_is_a_sidecar_written_beside_it(self, tmp_path: Path):
        # The reader's edit earns a sidecar for the note's own book only.
        target = tmp_path / "Dune.md"
        edited = notes.compose([_highlight("A", "A TEXT")]).replace("> A", "> mine")
        target.write_text(edited)

        notes._write_one(target, [_highlight("B", "B TEXT")])

        assert not notes.sidecar_for(target).exists()

    def test_the_run_names_it_and_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        app_logger.configure(verbosity=0)
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Dune.md").write_text(notes.compose([_highlight("A", "A TEXT")]))
        named = [Assignment(Path("Dune.epub"), "Dune.epub", "dune.epub")]

        code = notes.write_vault(
            [_highlight("B", "B TEXT")], str(vault), named, copyable=()
        )

        reported = capsys.readouterr().err
        assert code == exits.FAILED
        assert "Dune.md" in reported
        assert "another book" in reported
        assert "not written by ibook2epub" not in reported


class TestAnnotationsOnlyNamesNotesFromTheShelf:
    """
    Edition A is exported with ``-ad`` and leaves the library; edition B moves
    on to its marked name. ``-ao`` then wrote B's highlights over A's note.
    """

    FLAGS = ["-q", "--name-by", "author-title", "--on-collision", "suffix"]
    VAULT = ["--annotations-format", "markdown"]

    def _two_editions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Path, Path, Path]:
        container, output = tmp_path / "container", tmp_path / "out"
        vault = tmp_path / "vault"
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(container, policy),
        )
        for library, asset, identifier in (
            (tmp_path / "lib1", "A", "urn:isbn:9780441013593"),
            (tmp_path / "lib2", "B", "urn:isbn:9780593099322"),
        ):
            _edition(library, container, asset, identifier)
            flags = ["-s", str(library), "-o", str(output), *self.FLAGS]
            assert main([*flags, "-ad", str(vault), *self.VAULT]) == 0
        return tmp_path / "lib2", output, vault

    def test_with_the_shelf_it_writes_the_moved_editions_own_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library, output, vault = self._two_editions(tmp_path, monkeypatch)
        before = (vault / PLAIN_NOTE).read_text()

        code = main(
            ["-s", str(library), "-o", str(output), *self.FLAGS]
            + ["-ao", str(vault), *self.VAULT]
        )

        assert code == 0
        assert (vault / PLAIN_NOTE).read_text() == before
        assert "EDITION A TEXT" in before
        [moved] = [p for p in output.glob("*.epub") if p.stem + ".md" != PLAIN_NOTE]
        assert "EDITION B TEXT" in (vault / (moved.stem + ".md")).read_text()

    def test_without_the_shelf_the_other_editions_note_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library, _, vault = self._two_editions(tmp_path, monkeypatch)
        before = (vault / PLAIN_NOTE).read_text()

        code = main(
            ["-s", str(library), "-o", str(tmp_path / "nowhere"), *self.FLAGS]
            + ["-ao", str(vault), *self.VAULT]
        )

        assert code == exits.FAILED
        assert (vault / PLAIN_NOTE).read_text() == before
