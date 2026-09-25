"""
Tests for a legacy note two editions both hold, run after run.

Every release up to 2.3.1 wrote its notes naming neither book nor file, so
only the highlights in such a note say whose it is. Two editions sharing a
passage both hold a note of that one passage, and it goes to neither. It was
a tie only among the books still waiting for a name that wanted it, though:
once one of the two had a note of its own -- the fresh one the tie itself
had given it -- the other took the note on the next run, its region written
over with that book's highlights and the reader's writing left under them,
without a word. A rerun of an unchanged library rewrote the vault.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

from pathlib import Path
from typing import Any

import pytest

from epubconvert.export import noteformat, notes
from epubconvert.export.naming import disambiguator
from epubconvert.utils import exits
from epubconvert.utils.policy import Assignment

ASSETS: dict[str, str | None] = {"E": "Dune.epub", "P": "Dune.pdf"}
ON_THE_EPUB = "\nMY NOTES ON THE EPUB\n"
ON_THE_PDF = "\nMY NOTES ON THE PDF\n"


def _highlight(asset: str, text: str) -> dict[str, Any]:
    source = ASSETS[asset]
    book = {"title": "Dune", "assetId": asset, "source": source}
    return {"id": f"{asset}-{text}", "text": text, "book": book}


def _found(epub: list[str], pdf: list[str]) -> list[dict[str, Any]]:
    return [_highlight("E", t) for t in epub] + [_highlight("P", t) for t in pdf]


def _write(
    vault: Path, found: list[dict[str, Any]], names: tuple[str, str], *, suffix: bool
) -> int:
    """Write the EPUB named ``names[0]`` and the PDF named ``names[1]``."""
    named = [
        Assignment(Path("Dune.epub"), names[0], names[0]),
        Assignment(Path("Dune.pdf"), names[1], names[1]),
    ]
    return notes.write_vault(
        found, str(vault), named, copyable=(), suffix=suffix, assets=ASSETS
    )


def _legacy(text: str, mine: str) -> str:
    """Render a note as every release up to 2.3.1 wrote it, with *mine* added."""
    untagged = noteformat.BOOK_TAG.sub("", text, count=1)
    return noteformat.SOURCE_TAG.sub("", untagged, count=1) + mine


def _snapshot(vault: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in vault.iterdir()}


def _tagged(vault: Path, name: str) -> str | None:
    held = noteformat.split((vault / name).read_text(encoding="utf-8"))
    assert held is not None
    return {disambiguator("E"): "EPUB", disambiguator("P"): "PDF"}.get(held.book or "")


def _rerun_is_quiet(vault: Path, rerun: Any, code: int) -> None:
    before = _snapshot(vault)

    assert rerun() == code

    assert _snapshot(vault) == before


class TestAnUpgradedVault:
    """
    As ibook2epub up to 2.3.1 left it under ``--on-collision suffix``: the
    PDF's note ``Dune.md``, one passage both editions highlighted, and the
    EPUB's ``Dune (2).md``, which holds a passage only the EPUB has.
    """

    EPUB = ["The spice must flow.", "Fear is the mind-killer."]
    PDF = ["The spice must flow."]

    def test_each_note_stays_with_its_edition_and_a_rerun_writes_nothing(
        self, tmp_path: Path
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        pdfs = notes.compose(_found([], self.PDF))
        epubs = notes.compose(_found(self.EPUB, []))
        (vault / "Dune.md").write_text(_legacy(pdfs, ON_THE_PDF))
        (vault / "Dune (2).md").write_text(_legacy(epubs, ON_THE_EPUB))
        found = _found(self.EPUB, self.PDF)

        def run() -> int:
            return _write(vault, found, ("Dune.epub", "Dune.pdf"), suffix=True)

        assert run() == exits.SUCCESS

        # The EPUB holds the PDF's note too, but has one of its own.
        assert sorted(_snapshot(vault)) == ["Dune (2).md", "Dune.md"]
        # Its region comes out the same, so it is not rewritten, nor tagged.
        assert _tagged(vault, "Dune.md") in (None, "PDF")
        assert ON_THE_PDF in (vault / "Dune.md").read_text()
        assert "Fear" not in (vault / "Dune.md").read_text()
        assert ON_THE_EPUB in (vault / "Dune (2).md").read_text()
        _rerun_is_quiet(vault, run, exits.SUCCESS)


class TestARenamedEditionsNote:
    """
    The EPUB's note, as 2.3.1 wrote it, holds one passage; the EPUB is
    renamed, and ``Dune.pdf``, which highlighted that passage too, wants
    the note's name.
    """

    @pytest.mark.parametrize("suffix", [False, True])
    def test_it_goes_to_neither_run_after_run(self, tmp_path: Path, suffix: bool):
        vault = tmp_path / "vault"
        vault.mkdir()
        epubs = notes.compose(_found(["The spice must flow"], []))
        (vault / "Dune.md").write_text(_legacy(epubs, ON_THE_EPUB))
        before = (vault / "Dune.md").read_bytes()
        found = _found(
            ["The spice must flow"], ["The spice must flow", "Fear is the mind-killer"]
        )
        names = ("Frank Herbert - Dune.epub", "Dune.pdf")
        code = exits.SUCCESS if suffix else exits.FAILED

        def run() -> int:
            return _write(vault, found, names, suffix=suffix)

        assert run() == code

        assert (vault / "Dune.md").read_bytes() == before
        assert _tagged(vault, "Frank Herbert - Dune.md") == "EPUB"
        _rerun_is_quiet(vault, run, code)
        assert (vault / "Dune.md").read_bytes() == before


class TestARenameOntoANameOnlyCaseTellsApart:
    """
    Both editions written as this version writes them -- the PDF as
    ``dune.pdf``, the EPUB numbered ``Dune (3).pdf`` -- and then left as
    2.3.1 would have: untagged, with the reader's writing in each. The EPUB
    is renamed ``Dune.epub``, whose note is ``dune.md`` to a
    case-insensitive volume.
    """

    SHARED = "shared passage"
    EPUB = [SHARED, "only in the epub"]

    @pytest.mark.parametrize("suffix", [False, True])
    def test_the_pdf_keeps_its_note_and_a_rerun_writes_nothing(
        self, tmp_path: Path, suffix: bool
    ):
        vault = tmp_path / "vault"
        found = _found(self.EPUB, [self.SHARED])
        first = _write(vault, found, ("Dune (3).pdf", "dune.pdf"), suffix=suffix)
        assert first == exits.SUCCESS
        for note in vault.iterdir():
            mine = f"\nMY WRITING IN {note.name}\n"
            note.write_text(_legacy(note.read_text(encoding="utf-8"), mine))
        pdfs = "\nMY WRITING IN dune.md\n"
        epubs = "\nMY WRITING IN Dune (3).md\n"

        def run() -> int:
            return _write(vault, found, ("Dune.epub", "dune.pdf"), suffix=suffix)

        # Without suffix the EPUB loses the name collision to the PDF.
        assert run() == exits.SUCCESS

        assert sorted(_snapshot(vault)) == ["Dune (3).md", "dune.md"]
        assert _tagged(vault, "dune.md") in (None, "PDF")
        assert pdfs in (vault / "dune.md").read_text()
        assert "only in the epub" not in (vault / "dune.md").read_text()
        assert epubs in (vault / "Dune (3).md").read_text()
        _rerun_is_quiet(vault, run, exits.SUCCESS)


class TestATieWithABookThatDoesNotWantTheName:
    """
    ``Dune (2).pdf``'s note, as 2.3.1 wrote it, holds a passage that
    ``Dune (3).pdf`` highlighted too; the reader has edited the region of
    ``Dune (3).md``, so nothing says whose that one is but its name.
    """

    @staticmethod
    def _write(vault: Path) -> int:
        named = [
            Assignment(Path("Dune.epub"), "Dune (2).pdf", "dune (2).pdf"),
            Assignment(Path("Dune.pdf"), "Dune (3).pdf", "dune (3).pdf"),
        ]
        found = _found(["T7"], ["T7"])
        return notes.write_vault(
            found, str(vault), named, copyable=(), suffix=True, assets=ASSETS
        )

    def test_the_fresh_note_the_tie_gave_does_not_hand_it_over(self, tmp_path: Path):
        # The first run numbers Dune (2).pdf past the note it cannot tell
        # is its own. On the next, that fresh note took it out of the tie,
        # and the note went to Dune (3).pdf, which does not want its name:
        # as a note left under an old name, to be moved onto Dune (3).md.
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Dune (2).md").write_text(
            _legacy(notes.compose(_found(["T7"], [])), ON_THE_EPUB)
        )
        edited = notes.compose(_found([], ["T7"])).replace("> T7", "> T7, as I read")
        (vault / "Dune (3).md").write_text(_legacy(edited, ON_THE_PDF))
        before = (vault / "Dune (2).md").read_bytes()

        assert self._write(vault) == exits.SUCCESS

        assert (vault / "Dune (2).md").read_bytes() == before
        assert _tagged(vault, "Dune (2) (2).md") == "EPUB"
        _rerun_is_quiet(vault, lambda: self._write(vault), exits.SUCCESS)
