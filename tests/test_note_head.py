"""
Tests for what a reader may write above a note's start marker.

Everything above the start marker is the reader's (``Split.head``), but the
marker was looked for only straight after the closing ``---``, or on line 1
when there was no frontmatter. A blank line or a ``Related: [[X]]`` there made
the tool call its own note "not written by ibook2epub" and exit 1 on every
run, and the note was never updated again.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from epubconvert.export import noteformat, notes
from epubconvert.utils import exits
from epubconvert.utils.policy import Assignment

MARKER = "<!-- ibook2epub sha256="


def _highlight(text: str) -> dict[str, Any]:
    return {"id": text, "text": text, "book": {"title": "Dune", "assetId": "A"}}


def _under_frontmatter(line: str) -> Callable[[str], str]:
    return lambda note: note.replace(f"---\n{MARKER}", f"---\n{line}\n{MARKER}", 1)


def _instead_of_frontmatter(line: str) -> Callable[[str], str]:
    return lambda note: f"{line}\n" + note[note.index(MARKER) :]


EDITS = {
    "a line under the frontmatter": _under_frontmatter("Related: [[Arrakis]]"),
    "a blank line under the frontmatter": _under_frontmatter(""),
    "a heading where the frontmatter was": _instead_of_frontmatter("Up: [[Books]]"),
    "several lines under the frontmatter": _under_frontmatter("a\n\n# b\n---"),
}


@pytest.mark.parametrize("edit", EDITS.values(), ids=EDITS.keys())
class TestALineAboveTheMarker:
    def test_the_note_is_still_ours(self, edit: Callable[[str], str]):
        note = edit(notes.compose([_highlight("FIRST")]))

        assert noteformat.wrote_it(note) is True
        assert noteformat.is_ours(note) is True

    def test_it_is_part_of_the_readers_head(self, edit: Callable[[str], str]):
        original = notes.compose([_highlight("FIRST")])
        note = edit(original)

        held, written = noteformat.split(note), noteformat.split(original)

        assert held is not None
        assert written is not None
        assert note.startswith(held.head)
        assert held.generated == written.generated

    def test_a_rerun_adds_the_new_highlight_and_keeps_the_line(
        self, tmp_path: Path, edit: Callable[[str], str]
    ):
        vault = tmp_path / "vault"
        named = [Assignment(Path("Dune.epub"), "Dune.epub", "dune.epub")]
        found = [_highlight("FIRST")]
        notes.write_vault(found, str(vault), named, copyable=())
        note = vault / "Dune.md"
        note.write_text(edit(note.read_text(encoding="utf-8")), encoding="utf-8")
        head = note.read_text(encoding="utf-8").split(MARKER)[0]

        code = notes.write_vault(
            [*found, _highlight("SECOND")], str(vault), named, copyable=()
        )

        updated = note.read_text(encoding="utf-8")
        assert code == exits.SUCCESS
        assert updated.startswith(head + MARKER)
        assert "> SECOND" in updated
        assert sorted(path.name for path in vault.iterdir()) == ["Dune.md"]


class TestTheMarkerIsStillHardToForge:
    def test_a_highlight_shaped_like_a_marker_does_not_move_the_region(self):
        forged = notes.compose([_highlight("hl")]).split("\n")
        start = next(line for line in forged if line.startswith(MARKER))
        found = [_highlight(start), _highlight(noteformat.END_MARKER)]

        note = notes.compose(found)

        held = noteformat.split(note)
        assert held is not None
        assert held.generated == notes.body(found)
        assert notes.rewrite(note, found) == note

    def test_a_copy_of_a_note_below_the_end_marker_stays_the_readers(self):
        pasted = notes.compose([_highlight("OTHER")])
        note = notes.compose(
            [_highlight("hl")], tail=f"{noteformat.END_MARKER}\n{pasted}"
        )

        held = noteformat.split(note)

        assert held is not None
        assert held.tail == f"{noteformat.END_MARKER}\n{pasted}"
        assert noteformat.is_ours(note) is True

    def test_a_file_with_no_marker_is_still_not_ours(self):
        assert noteformat.wrote_it("---\ntitle: x\n---\n\nmy note\n") is False
        assert noteformat.split("Related: [[X]]\n\nmy note\n") is None

    def test_a_marker_with_no_end_marker_below_it_is_an_edit(self):
        note = "Related: [[X]]\n" + notes.compose([_highlight("hl")])
        edited = note.replace(noteformat.END_MARKER, "")

        assert noteformat.wrote_it(edited) is True
        assert noteformat.is_ours(edited) is False
