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
from epubconvert.export.naming import disambiguator
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
    assets: dict[str, str | None] | None = None,
) -> int:
    named = [Assignment(Path(name), name, name) for name in library]
    return notes.write_vault(
        found, str(vault), named, copyable=(), suffix=suffix, assets=assets
    )


def _notes(vault: Path) -> dict[str, str]:
    return {note.name: note.read_text(encoding="utf-8") for note in vault.iterdir()}


def _legacy(found: list[dict[str, Any]]) -> str:
    """Render a note as a version naming neither its book nor file wrote it."""
    note = notes.compose(found)
    return noteformat.SOURCE_TAG.sub("", noteformat.BOOK_TAG.sub("", note, count=1))


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


class TestABookWithoutHighlightsToday:
    """
    Its tagged note is its own whether or not it has highlights today, so a
    namesake's outcome does not turn on them. Only the books with highlights
    looked for their tagged notes, so without suffix the other book took the
    name and was refused the note, exiting 1, on exactly the runs the note's
    own book had nothing to write.
    """

    ASSETS: dict[str, str | None] = {"EPUBASSET": EPUB, "PDFASSET": PDF}

    @pytest.mark.parametrize("pdf_has_highlights", [True, False])
    def test_without_suffix_the_namesake_loses_the_collision_either_way(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        pdf_has_highlights: bool,
    ):
        app_logger.configure(verbosity=0)
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Dune.md").write_text(notes.compose(PDF_ONLY) + MINE)
        found = BOTH if pdf_has_highlights else BOTH[:1]

        code = _write(vault, found, suffix=False, assets=self.ASSETS)

        assert code == exits.SUCCESS
        reported = capsys.readouterr().err
        assert "lost a name collision" in reported
        assert "another book" not in reported
        held = _notes(vault)
        assert list(held) == ["Dune.md"]
        assert "epub highlight" not in held["Dune.md"]
        assert MINE in held["Dune.md"]

    def test_under_suffix_it_keeps_a_numbered_note(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Dune (2).md").write_text(notes.compose(PDF_ONLY) + MINE)
        before = (vault / "Dune (2).md").read_bytes()
        library = (PDF, EPUB)

        code = _write(vault, BOTH[:1], suffix=True, library=library, assets=self.ASSETS)

        assert code == exits.SUCCESS
        assert (vault / "Dune (2).md").read_bytes() == before
        assert "epub highlight" in _notes(vault)["Dune.md"]


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

    @staticmethod
    def _legacy(vault: Path, found: list[dict[str, Any]]) -> bytes:
        vault.mkdir()
        (vault / "Dune.md").write_text(_legacy(found) + MINE, encoding="utf-8")
        return (vault / "Dune.md").read_bytes()

    @pytest.mark.parametrize("suffix", [False, True])
    def test_it_goes_to_the_book_holding_every_one_of_its_highlights(
        self, tmp_path: Path, suffix: bool
    ):
        # Both editions highlighted one passage. The note was the first
        # book's that held any of its highlights, and was re-tagged for it.
        vault = tmp_path / "vault"
        pdf = [_highlight(PDF, "shared"), _highlight(PDF, "only in the pdf")]
        self._legacy(vault, pdf)
        found = [_highlight(EPUB, "shared"), *pdf, _highlight(PDF, "new")]

        for _ in range(2):
            assert _write(vault, found, suffix=suffix) == exits.SUCCESS

        held = _notes(vault)
        pdfs = noteformat.split(held["Dune.md"])
        assert pdfs is not None
        assert pdfs.book == disambiguator("PDFASSET")
        assert "> new" in held["Dune.md"]
        assert MINE in held["Dune.md"]
        assert sorted(held) == (["Dune (2).md", "Dune.md"] if suffix else ["Dune.md"])

    def test_without_suffix_one_both_hold_alike_is_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # Nothing tells whose it is, so neither book is handed it.
        app_logger.configure(verbosity=0)
        vault = tmp_path / "vault"
        before = self._legacy(vault, [_highlight(PDF, "shared")])
        found = [_highlight(EPUB, "shared"), _highlight(PDF, "shared")]

        assert _write(vault, found, suffix=False) == exits.FAILED

        assert (vault / "Dune.md").read_bytes() == before
        assert "another book" in capsys.readouterr().err

    def test_under_suffix_one_both_hold_alike_is_numbered_past(self, tmp_path: Path):
        vault = tmp_path / "vault"
        before = self._legacy(vault, [_highlight(PDF, "shared")])
        found = [_highlight(EPUB, "shared"), _highlight(PDF, "shared")]

        for _ in range(2):
            assert _write(vault, found, suffix=True) == exits.SUCCESS

        assert (vault / "Dune.md").read_bytes() == before
        held = _notes(vault)
        assert sorted(held) == ["Dune (2).md", "Dune (3).md", "Dune.md"]
        assert "shared" in held["Dune (2).md"]
        assert "shared" in held["Dune (3).md"]

    @pytest.mark.parametrize("tagged_for_a_book_gone", [False, True])
    def test_a_books_tagged_note_comes_before_one_its_highlights_give_it(
        self, tmp_path: Path, tagged_for_a_book_gone: bool
    ):
        # Both books highlighted the same passage, and a note that names no
        # book of the run holds it. The PDF, first in the run, already has a
        # note tagged for it, and keeps that rather than taking the other.
        vault = tmp_path / "vault"
        vault.mkdir()
        shared = [_highlight(EPUB, "shared"), _highlight(PDF, "shared")]
        if tagged_for_a_book_gone:
            gone = {"title": "Dune", "assetId": "GONE"}
            other = notes.compose([{**shared[0], "book": gone}])
        else:
            other = noteformat.BOOK_TAG.sub("", notes.compose(shared[:1]), count=1)
        (vault / "Dune.md").write_text(other + MINE, encoding="utf-8")
        (vault / "Dune (2).md").write_text(notes.compose(shared[1:]))

        code = _write(vault, shared, suffix=True, library=(PDF, EPUB))

        assert code == exits.SUCCESS
        assert sorted(_notes(vault)) == ["Dune (2).md", "Dune.md"]
        held = noteformat.split(_notes(vault)["Dune (2).md"])
        assert held is not None
        assert held.book == disambiguator("PDFASSET")
        assert MINE in _notes(vault)["Dune.md"]

    def test_holding_no_ones_highlights_it_goes_with_its_name(self, tmp_path: Path):
        # Nothing says whose it is, and only one book wants its name: it is
        # that book's note, as it always was.
        vault = tmp_path / "vault"
        vault.mkdir()
        gone = [_highlight(EPUB, "deleted in Books")]
        (vault / "Dune.md").write_text(_legacy(gone) + MINE, encoding="utf-8")

        code = _write(vault, BOTH[:1], suffix=False, library=(EPUB,))

        assert code == exits.SUCCESS
        held = _notes(vault)
        assert "epub highlight" in held["Dune.md"]
        assert MINE in held["Dune.md"]


class TestANoteNothingClaimsThatTwoBooksWant:
    """
    A note tagged for no book the run knows, or for none at all, and holding
    none of a book's highlights -- or only some of them -- says nothing of
    whose it is. It went to whichever book of its name came first: the
    PDF's note, every highlight deleted in Books, was re-tagged for the EPUB
    once the EPUB gained one. Two books wanting it, neither is handed it.
    """

    CASES = {
        "holding none of their highlights": [_highlight(PDF, "deleted in Books")],
        "holding one of theirs": [
            _highlight(EPUB, "epub highlight"),
            _highlight(PDF, "deleted in Books"),
        ],
    }

    @staticmethod
    def _vault(tmp_path: Path, found: list[dict[str, Any]]) -> tuple[Path, bytes]:
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Dune.md").write_text(_legacy(found) + MINE, encoding="utf-8")
        return vault, (vault / "Dune.md").read_bytes()

    @pytest.mark.parametrize("found", CASES.values(), ids=CASES)
    def test_without_suffix_it_is_refused(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        found: list[dict[str, Any]],
    ):
        app_logger.configure(verbosity=0)
        vault, before = self._vault(tmp_path, found)

        assert _write(vault, BOTH[:1], suffix=False) == exits.FAILED

        assert (vault / "Dune.md").read_bytes() == before
        assert "another book" in capsys.readouterr().err

    @pytest.mark.parametrize("found", CASES.values(), ids=CASES)
    def test_under_suffix_it_is_numbered_past(
        self, tmp_path: Path, found: list[dict[str, Any]]
    ):
        vault, before = self._vault(tmp_path, found)

        for _ in range(2):
            assert _write(vault, BOTH, suffix=True) == exits.SUCCESS

        assert (vault / "Dune.md").read_bytes() == before
        held = _notes(vault)
        assert sorted(held) == ["Dune (2).md", "Dune (3).md", "Dune.md"]
        assert "epub highlight" in held["Dune (2).md"]
        assert "pdf highlight" in held["Dune (3).md"]

    @pytest.mark.parametrize("suffix", [False, True])
    def test_one_tagged_for_a_book_gone_names_the_file_it_was_written_for(
        self, tmp_path: Path, suffix: bool
    ):
        # The PDF removed from Books and added again, under a new asset id
        # with no highlights yet: its note is still the PDF's, as a tagged
        # note is, and the EPUB loses the collision, or is numbered.
        vault = tmp_path / "vault"
        _write(vault, PDF_ONLY, suffix=suffix, library=(PDF,))
        _add_mine(vault / "Dune.md")
        before = (vault / "Dune.md").read_bytes()
        assets: dict[str, str | None] = {"EPUBASSET": EPUB, "NEWPDF": PDF}

        assert _write(vault, BOTH[:1], suffix=suffix, assets=assets) == 0

        assert (vault / "Dune.md").read_bytes() == before
        reimported = _highlight(PDF, "new")
        reimported["book"] = {**reimported["book"], "assetId": "NEWPDF"}
        found = [*BOTH[:1], reimported]

        for _ in range(2):
            assert _write(vault, found, suffix=suffix, assets=assets) == 0

        pdfs = _notes(vault)["Dune.md"]
        assert "epub highlight" not in pdfs
        assert "> new" in pdfs
        assert MINE in pdfs
        if suffix:
            assert "epub highlight" in _notes(vault)["Dune (2).md"]
        else:
            assert list(_notes(vault)) == ["Dune.md"]


class TestANumberedNoteOfABookAddedAgain:
    """
    The PDF, numbered past the EPUB's note, removed from Books and added
    again: its numbered note was passed over as a file already there, and a
    fresh ``Dune (3).md`` stranded the reader's writing in ``Dune (2).md``.
    """

    ASSETS: dict[str, str | None] = {"EPUBASSET": EPUB, "NEWPDF": PDF}

    @staticmethod
    def _readded(text: str) -> dict[str, Any]:
        found = _highlight(PDF, text)
        found["book"] = {**found["book"], "assetId": "NEWPDF"}
        return found

    def test_it_is_taken_back(self, tmp_path: Path):
        vault = tmp_path / "vault"
        _write(vault, BOTH, suffix=True)
        _add_mine(vault / "Dune (2).md")
        found = [*BOTH[:1], self._readded("pdf again")]

        for _ in range(2):
            assert _write(vault, found, suffix=True, assets=self.ASSETS) == 0

        assert sorted(_notes(vault)) == ["Dune (2).md", "Dune.md"]
        pdfs = _notes(vault)["Dune (2).md"]
        assert "> pdf again" in pdfs
        assert MINE in pdfs

    def test_one_naming_no_file_is_taken_back_by_the_one_book_wanting_it(
        self, tmp_path: Path
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Dune.md").write_text("# my own page about Dune\n")
        (vault / "Dune (2).md").write_text(_legacy(PDF_ONLY) + MINE)
        found = [self._readded("pdf again")]

        for _ in range(2):
            code = _write(vault, found, suffix=True, library=(PDF,))
            assert code == exits.SUCCESS

        assert sorted(_notes(vault)) == ["Dune (2).md", "Dune.md"]
        pdfs = _notes(vault)["Dune (2).md"]
        assert "> pdf again" in pdfs
        assert MINE in pdfs

    @pytest.mark.parametrize("epub_has_a_note", [False, True])
    def test_not_while_another_book_may_want_it(
        self, tmp_path: Path, epub_has_a_note: bool
    ):
        # A note that names neither book could be either's, whichever of
        # them has a note of its own already: both are numbered past it.
        vault = tmp_path / "vault"
        vault.mkdir()
        if epub_has_a_note:
            (vault / "Dune.md").write_text(notes.compose(BOTH[:1]))
        else:
            (vault / "Dune.md").write_text("# my own page about Dune\n")
        (vault / "Dune (2).md").write_text(_legacy(PDF_ONLY) + MINE)
        before = (vault / "Dune (2).md").read_bytes()
        found = [*BOTH[:1], self._readded("pdf again")]

        for _ in range(2):
            assert _write(vault, found, suffix=True, assets=self.ASSETS) == 0

        assert (vault / "Dune (2).md").read_bytes() == before
        expected = ["Dune (2).md", "Dune (3).md", "Dune.md"]
        if not epub_has_a_note:
            expected.insert(2, "Dune (4).md")
        assert sorted(_notes(vault)) == expected


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
        # X is a book of the library, so the note is another book's. Tagged
        # for no book the run knows, it would be a note nothing claims, and
        # the one book left wanting its name takes it back (see
        # TestANumberedNoteOfABookAddedAgain).
        assets: dict[str, str | None] = {"X": "X.epub"}

        assert _write(vault, BOTH, suffix=True, assets=assets) == exits.SUCCESS

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

        book = notenames.claimant(found)
        assert notenames.holding(held, book, None) is notenames.Holding.MINE
        assert held.quoted == {"firstlinesecondline"}
