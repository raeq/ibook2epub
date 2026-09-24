"""
Tests for a note name that depends on the library, not on the highlights.

Only the books with highlights in a run claimed note names, so a name moved
from one book to another whenever a book gained its first highlight or lost
its last. ``Dune.pdf``, highlighted alone, was given ``Dune.md`` and the
reader wrote in it; once ``Dune.epub`` gained a highlight it claimed that
name first, and the PDF's note was either refused on every later run or
stranded while the PDF moved on to ``Dune (2).md``. Every named book claims
its name now, and a note already tagged for a book stays that book's.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

from pathlib import Path
from typing import Any

import pytest

from epubconvert.export import noteformat, notenames, notes
from epubconvert.utils import app_logger, exits
from epubconvert.utils.policy import Assignment

EPUB, PDF = "Dune.epub", "Dune.pdf"
MINE = "\nMY OWN THOUGHTS ON THE PDF\n"


def _highlight(source: str, text: str) -> dict[str, Any]:
    asset = "EPUBASSET" if source == EPUB else "PDFASSET"
    return {
        "id": f"{asset}-{text}",
        "text": text,
        "book": {"title": "Dune", "assetId": asset, "source": source},
    }


def _write(
    vault: Path,
    found: list[dict[str, Any]],
    *,
    suffix: bool,
    library: tuple[str, ...] = (EPUB, PDF),
) -> int:
    named = [Assignment(Path(name), name, name) for name in library]
    return notes.write_vault(found, str(vault), named, copyable=(), suffix=suffix)


def _notes(vault: Path) -> dict[str, str]:
    return {note.name: note.read_text(encoding="utf-8") for note in vault.iterdir()}


def _add_mine(note: Path) -> None:
    note.write_text(note.read_text(encoding="utf-8") + MINE, encoding="utf-8")


PDF_ONLY = [_highlight(PDF, "pdf highlight")]
BOTH = [_highlight(EPUB, "epub highlight"), *PDF_ONLY]


class TestABookGainingItsFirstHighlight:
    def test_under_suffix_the_pdf_keeps_its_note(self, tmp_path: Path):
        vault = tmp_path / "vault"
        assert _write(vault, PDF_ONLY, suffix=True) == exits.SUCCESS
        _add_mine(vault / "Dune (2).md")

        for _ in range(2):
            assert _write(vault, BOTH, suffix=True) == exits.SUCCESS

        held = _notes(vault)
        assert sorted(held) == ["Dune (2).md", "Dune.md"]
        assert "pdf highlight" in held["Dune (2).md"]
        assert MINE in held["Dune (2).md"]
        assert "epub highlight" in held["Dune.md"]
        assert "pdf highlight" not in held["Dune.md"]

    def test_without_suffix_the_name_was_never_the_pdfs(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # The epub claims Dune.md whether or not it has a highlight, so the
        # PDF loses the collision from the first run rather than the second.
        app_logger.configure(verbosity=0)
        vault = tmp_path / "vault"

        assert _write(vault, PDF_ONLY, suffix=False) == exits.SUCCESS
        assert "lost a name collision" in capsys.readouterr().err
        assert not list(vault.iterdir())

        for _ in range(2):
            assert _write(vault, BOTH, suffix=False) == exits.SUCCESS
            held = _notes(vault)
            assert list(held) == ["Dune.md"]
            assert "epub highlight" in held["Dune.md"]
            assert "pdf highlight" not in held["Dune.md"]

    @pytest.mark.parametrize("suffix", [False, True])
    def test_a_note_an_earlier_version_gave_the_pdf_stays_the_pdfs(
        self, tmp_path: Path, suffix: bool
    ):
        # Written when the PDF alone had highlights and so alone claimed a
        # name: tagged for the PDF, it is the PDF's however the run is named.
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Dune.md").write_text(notes.compose(PDF_ONLY) + MINE)

        for _ in range(2):
            assert _write(vault, BOTH, suffix=suffix) == exits.SUCCESS

        held = _notes(vault)
        assert "pdf highlight" in held["Dune.md"]
        assert MINE in held["Dune.md"]
        assert "epub highlight" not in held["Dune.md"]
        if suffix:
            assert "epub highlight" in held["Dune (2).md"]
        else:
            assert list(held) == ["Dune.md"]


class TestABookLosingItsLastHighlight:
    @pytest.mark.parametrize("suffix", [False, True])
    def test_the_other_book_does_not_take_its_note(self, tmp_path: Path, suffix: bool):
        vault = tmp_path / "vault"
        _write(vault, BOTH, suffix=suffix)
        before = (vault / "Dune.md").read_bytes()
        if suffix:
            _add_mine(vault / "Dune (2).md")
        only_pdf = [*PDF_ONLY, _highlight(PDF, "pdf highlight 2")]

        assert _write(vault, only_pdf, suffix=suffix) == exits.SUCCESS

        assert (vault / "Dune.md").read_bytes() == before
        if suffix:
            pdfs = (vault / "Dune (2).md").read_text(encoding="utf-8")
            assert "pdf highlight 2" in pdfs
            assert MINE in pdfs
        else:
            assert list(_notes(vault)) == ["Dune.md"]


class TestANamesakeLeavingTheLibrary:
    def test_under_suffix_the_pdf_keeps_its_numbered_note(self, tmp_path: Path):
        vault = tmp_path / "vault"
        _write(vault, BOTH, suffix=True)
        before = (vault / "Dune.md").read_bytes()
        _add_mine(vault / "Dune (2).md")
        found = [*BOTH, _highlight(PDF, "pdf highlight 2")]

        code = _write(vault, found, suffix=True, library=(PDF,))

        assert code == exits.SUCCESS
        assert (vault / "Dune.md").read_bytes() == before
        pdfs = (vault / "Dune (2).md").read_text(encoding="utf-8")
        assert "pdf highlight 2" in pdfs
        assert MINE in pdfs

    def test_without_suffix_its_leftover_note_is_not_taken_over(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # Its highlights are still in Books, so the note is still a known
        # book's: the PDF is refused it, and told how to get one of its own.
        app_logger.configure(verbosity=0)
        vault = tmp_path / "vault"
        _write(vault, BOTH, suffix=False)
        before = (vault / "Dune.md").read_bytes()

        code = _write(vault, BOTH, suffix=False, library=(PDF,))

        assert code == exits.FAILED
        assert (vault / "Dune.md").read_bytes() == before
        assert "--on-collision suffix" in capsys.readouterr().err


class TestANoteWrittenBeforeNotesWereTagged:
    """
    A legacy note carries no tag, so the only evidence of whose it is is the
    highlights in it. It is the book's whose highlights it holds.
    """

    @staticmethod
    def _legacy_pdf_note(vault: Path) -> Path:
        vault.mkdir()
        note = vault / "Dune.md"
        untagged = noteformat.BOOK_TAG.sub("", notes.compose(PDF_ONLY), count=1)
        note.write_text(untagged + MINE, encoding="utf-8")
        return note

    @pytest.mark.parametrize("suffix", [False, True])
    def test_it_stays_with_the_book_whose_highlights_it_holds(
        self, tmp_path: Path, suffix: bool
    ):
        vault = tmp_path / "vault"
        note = self._legacy_pdf_note(vault)

        assert _write(vault, BOTH, suffix=suffix) == exits.SUCCESS

        text = note.read_text(encoding="utf-8")
        assert "pdf highlight" in text
        assert "epub highlight" not in text
        assert MINE in text
        if suffix:
            assert "epub highlight" in (vault / "Dune (2).md").read_text()

    def test_holding_no_ones_highlights_it_goes_with_its_name(self, tmp_path: Path):
        # Nothing says whose it is, so it is the note of the book that
        # claims its name, as it always was.
        vault = tmp_path / "vault"
        vault.mkdir()
        gone = [_highlight(EPUB, "deleted in Books")]
        untagged = noteformat.BOOK_TAG.sub("", notes.compose(gone), count=1)
        (vault / "Dune.md").write_text(untagged + MINE, encoding="utf-8")

        assert _write(vault, BOTH, suffix=True) == exits.SUCCESS

        held = _notes(vault)
        assert "epub highlight" in held["Dune.md"]
        assert MINE in held["Dune.md"]
        assert "pdf highlight" in held["Dune (2).md"]


class TestUnderSuffixAFileThatIsNotANoteIsPassedOver:
    def test_a_file_of_the_readers_own_is_numbered_past(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Dune.md").write_text("# my own page about Dune\n")

        code = _write(vault, [_highlight(EPUB, "e")], suffix=True, library=(EPUB,))

        assert code == exits.SUCCESS
        assert (vault / "Dune.md").read_text() == "# my own page about Dune\n"
        assert "> e" in (vault / "Dune (2).md").read_text()

    def test_a_numbered_name_holding_another_books_note_is_passed_over(
        self, tmp_path: Path
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        other = [{"id": "x", "text": "x", "book": {"title": "X", "assetId": "X"}}]
        (vault / "Dune (2).md").write_text(notes.compose(other))
        before = (vault / "Dune (2).md").read_bytes()

        assert _write(vault, BOTH, suffix=True) == exits.SUCCESS

        assert (vault / "Dune (2).md").read_bytes() == before
        assert "pdf highlight" in (vault / "Dune (3).md").read_text()


class TestReadingTheVault:
    def test_a_vault_that_cannot_be_listed_holds_nothing_to_pass_over(
        self, tmp_path: Path
    ):
        vault = notenames.Vault(tmp_path / "not there")

        assert vault.held("Dune.md").kind is notenames.Holding.ABSENT

    def test_a_note_that_is_not_utf8_cannot_be_judged(self, tmp_path: Path):
        (tmp_path / "Dune.md").write_bytes(b"\xff\xfe not a note")

        held = notenames.Vault(tmp_path).held("Dune.md")

        assert held.kind is notenames.Holding.UNREADABLE

    def test_a_multi_line_highlight_is_one_block(self, tmp_path: Path):
        found = [_highlight(PDF, "first line\nsecond line")]
        (tmp_path / "Dune.md").write_text(notes.compose(found))

        held = notenames.Vault(tmp_path).held("Dune.md")

        assert notenames.holding(held, found) is notenames.Holding.MINE
        assert held.quoted == {"firstlinesecondline"}
