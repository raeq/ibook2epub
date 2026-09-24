"""
Tests for a note that survives an editor trimming trailing white space.

A blank line inside a highlight was written as ``"> "``, and a note whose
first line is blank as ``"**Note:** "``. Most editors trim trailing white
space on save, and the digest covered those spaces, so opening a note and
typing beneath it -- in the reader's own region -- made the tool see an
edited note and put every later highlight in a sidecar.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest

from epubconvert.export import noteformat, notes
from epubconvert.utils import exits
from epubconvert.utils.policy import Assignment


def _highlight(text: str, note: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": text,
        "text": text,
        "chapter": "Ch 1",
        "book": {"title": "Dune", "assetId": "A", "source": "Dune.epub"},
    }
    if note is not None:
        item["note"] = note
    return item


FOUND = [
    _highlight("First paragraph.\n\nSecond paragraph."),
    _highlight("ends with a space ", note="\nnote after a blank first line"),
    _highlight("a note with a blank line", note="one\n\ntwo  "),
]
#: Only white space this tool added, none that came with the text.
OURS_ONLY = [FOUND[0], _highlight("plain", note="\nnote after a blank first line")]
NAMED = [Assignment(Path("Dune.epub"), "Dune.epub", "dune.epub")]


def _trimmed(text: str) -> str:
    """What an editor that trims trailing white space on save leaves."""
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _as_an_older_version_wrote_it(found: list[dict[str, Any]]) -> str:
    """Put back the trailing spaces, and the digest over them, of old notes."""
    note = notes.compose(found)
    held = noteformat.split(note)
    assert held is not None
    region = (
        held.generated.replace("\n>\n", "\n> \n")
        .replace("\n**Note:**\n", "\n**Note:** \n")
        .replace("> ends with a space\n", "> ends with a space \n")
        .replace("\ntwo\n", "\ntwo  \n")
    )
    digest = hashlib.sha256(region.encode()).hexdigest()[:16]
    marker = f"<!-- ibook2epub sha256={digest} book={held.book} -->\n"
    return f"{held.head}{marker}{region}{held.tail}"


class TestNoGeneratedLineEndsInWhiteSpace:
    def test_a_note_as_composed(self):
        note = notes.compose(FOUND)

        assert [line for line in note.split("\n") if line != line.rstrip()] == []

    def test_a_blank_line_in_a_highlight_is_a_bare_quote_mark(self):
        assert "\n>\n" in notes.body(FOUND)

    def test_a_blank_first_line_of_a_note_leaves_the_label_alone(self):
        assert "\n**Note:**\n" in notes.body(FOUND)

    def test_every_round_trip_still_holds(self):
        note = notes.compose(FOUND)

        assert notes.rewrite(note, FOUND) == note
        assert noteformat.is_ours(note) is True
        held = noteformat.split(note)
        assert held is not None
        assert held.generated == notes.body(FOUND)


class TestANoteAnEditorHasTrimmed:
    def test_is_still_ours(self):
        assert noteformat.is_ours(_trimmed(notes.compose(FOUND))) is True

    def test_takes_its_new_highlights_in_place(self, tmp_path: Path):
        vault = tmp_path / "vault"
        notes.write_vault(FOUND, str(vault), NAMED, copyable=())
        note = vault / "Dune.md"
        note.write_text(_trimmed(note.read_text()) + "my thoughts\n")

        code = notes.write_vault(
            [*FOUND, _highlight("new one")], str(vault), NAMED, copyable=()
        )

        assert code == exits.SUCCESS
        assert sorted(path.name for path in vault.iterdir()) == ["Dune.md"]
        text = note.read_text()
        assert "> new one" in text
        assert text.endswith("my thoughts\n")


class TestANoteAnOlderVersionWrote:
    """Its digest covers the trailing spaces it was written with."""

    @pytest.fixture(name="older")
    def older_fixture(self) -> str:
        return _as_an_older_version_wrote_it(FOUND)

    def test_is_still_ours(self, older: str):
        assert older != notes.compose(FOUND)
        assert noteformat.is_ours(older) is True

    def test_a_quiet_rerun_leaves_its_bytes_alone(self, older: str, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Dune.md").write_text(older)
        before = (vault / "Dune.md").read_bytes()

        assert notes.write_vault(FOUND, str(vault), NAMED, copyable=()) == 0

        assert (vault / "Dune.md").read_bytes() == before
        assert notes.rewrite(older, FOUND) == older

    def test_is_still_ours_once_an_editor_trims_it(self):
        # The spaces this tool wrote are known and put back to check the
        # digest; ones that came with a highlight's text are not.
        older = _as_an_older_version_wrote_it(OURS_ONLY)

        assert older != _trimmed(older)
        assert noteformat.is_ours(_trimmed(older)) is True

    @pytest.mark.parametrize(
        ("title", "chapter"), [(" ", "Chapter One"), ("Dune", " "), (" ", " ")]
    )
    def test_is_still_ours_once_an_editor_trims_an_empty_heading(
        self, title: str, chapter: str
    ):
        # A title or chapter of white space alone was written "# " or "## ",
        # and an editor trims that to a bare "#" or "##".
        found = [{"id": "1", "text": "a", "chapter": chapter, "book": {"title": title}}]
        held = noteformat.split(notes.compose(found))
        assert held is not None
        region = re.sub(r"^(##?)$", r"\1 ", held.generated, flags=re.MULTILINE)
        assert region != held.generated
        digest = hashlib.sha256(region.encode()).hexdigest()[:16]
        older = f"<!-- ibook2epub sha256={digest} -->\n{region}{held.tail}"

        assert noteformat.is_ours(older) is True
        assert noteformat.is_ours(_trimmed(older)) is True

    def test_an_edit_inside_it_is_still_an_edit(self, older: str):
        edited = older.replace("> First paragraph.", "> First, as I read it.")

        assert noteformat.is_ours(edited) is False
