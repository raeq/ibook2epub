"""
Tests for a note left under the name its book had before.

A book's note is named after the book, and the name can change under it:
``--name-by author-title`` adopted, or the book's metadata corrected. Its
note, tagged for it, was looked for only under the names the book has now,
so a fresh note was started beside it and the reader's writing stayed
behind in the old one without a word.

Nor is a tagged note the only one a book has. Every release up to 2.3.1
wrote its notes untagged, and a note is tagged only when its region is
rewritten; a book removed from Books and added again answers to a new asset
id, and its note keeps the old one until then. Such a note was left behind
too, and a book with the name the renamed one had -- ``Dune.pdf`` beside it
-- was handed it by name: its region was written over with that book's
highlights, and the reader's writing left under them.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring

from pathlib import Path
from typing import Any

import pytest

from epubconvert.export import noteformat, notes
from epubconvert.export.naming import disambiguator
from epubconvert.utils import app_logger, exits
from epubconvert.utils.policy import Assignment

MINE = "\nTHREE PARAGRAPHS OF MY OWN\n"
RENAMED = "Frank Herbert - Dune"


def _highlight(
    text: str, asset: str = "A", source: str = "Dune.epub"
) -> dict[str, Any]:
    book = {"title": "Dune", "assetId": asset, "source": source}
    return {"id": f"{asset}-{text}", "text": text, "book": book}


def _write(
    vault: Path, name: str, *texts: str, suffix: bool = False, asset: str = "A"
) -> int:
    named = [Assignment(Path("Dune.epub"), f"{name}.epub", f"{name}.epub".lower())]
    found = [_highlight(text, asset) for text in texts]
    assets: dict[str, str | None] = {asset: "Dune.epub"}
    return notes.write_vault(
        found, str(vault), named, copyable=(), suffix=suffix, assets=assets
    )


def _legacy(note: Path) -> bytes:
    """Make *note* as every release up to 2.3.1 wrote it: naming no book or file."""
    text = note.read_text(encoding="utf-8")
    untagged = noteformat.BOOK_TAG.sub("", text, count=1)
    note.write_text(noteformat.SOURCE_TAG.sub("", untagged, count=1), encoding="utf-8")
    return note.read_bytes()


@pytest.fixture(name="vault")
def vault_fixture(tmp_path: Path) -> Path:
    """A vault holding Dune's note, under the name the book had then."""
    vault = tmp_path / "vault"
    assert _write(vault, "Dune", "x") == exits.SUCCESS
    note = vault / "Dune.md"
    note.write_text(note.read_text(encoding="utf-8") + MINE, encoding="utf-8")
    return vault


def _names(vault: Path) -> list[str]:
    return sorted(path.name for path in vault.iterdir())


class TestABookWhoseNameChanged:
    @pytest.mark.parametrize("suffix", [False, True])
    def test_its_note_moves_to_the_new_name(self, vault: Path, suffix: bool):
        assert _write(vault, RENAMED, "x", "y", suffix=suffix) == exits.SUCCESS

        assert _names(vault) == [f"{RENAMED}.md"]
        text = (vault / f"{RENAMED}.md").read_text(encoding="utf-8")
        assert "> y" in text
        assert text.endswith(MINE)

    def test_a_rerun_writes_nothing(self, vault: Path):
        _write(vault, RENAMED, "x", "y")
        before = (vault / f"{RENAMED}.md").read_bytes()

        assert _write(vault, RENAMED, "x", "y") == exits.SUCCESS

        assert _names(vault) == [f"{RENAMED}.md"]
        assert (vault / f"{RENAMED}.md").read_bytes() == before

    def test_an_edited_note_moves_and_takes_a_sidecar_there(self, vault: Path):
        note = vault / "Dune.md"
        note.write_text(note.read_text().replace("> x", "> x, as I read it"))

        assert _write(vault, RENAMED, "x", "y") == exits.SUCCESS

        assert _names(vault) == [f"{RENAMED}.md", f"{RENAMED}.md.new"]
        assert "as I read it" in (vault / f"{RENAMED}.md").read_text()

    @pytest.mark.parametrize("legacy", [False, True])
    def test_a_rename_only_in_case_writes_the_note_where_it_is(
        self, vault: Path, legacy: bool
    ):
        # The book is given the spelling on disk, so the note is its own
        # where it lies and nothing is moved: a move to a name differing only
        # in case is never asked for.
        if legacy:
            _legacy(vault / "Dune.md")

        assert _write(vault, "DUNE", "x", "y") == exits.SUCCESS

        assert _names(vault) == ["Dune.md"]
        text = (vault / "Dune.md").read_text(encoding="utf-8")
        assert "> y" in text
        assert text.endswith(MINE)


class TestANoteThatCannotBeMoved:
    """It is named, and the book's highlights wait for it rather than start
    a second note beside it."""

    @staticmethod
    def _reported(capsys: pytest.CaptureFixture[str]) -> str:
        return capsys.readouterr().err

    def test_not_onto_a_file_already_there(
        self, vault: Path, capsys: pytest.CaptureFixture[str]
    ):
        app_logger.configure(verbosity=0)
        (vault / f"{RENAMED}.md").write_text("# my own page\n")
        before = (vault / "Dune.md").read_bytes()

        for _ in range(2):
            assert _write(vault, RENAMED, "x", "y") == exits.FAILED
            reported = self._reported(capsys)
            assert "Dune.md" in reported
            assert "name it had before" in reported

        assert (vault / "Dune.md").read_bytes() == before
        assert (vault / f"{RENAMED}.md").read_text() == "# my own page\n"

    def test_not_while_a_sidecar_is_beside_it(
        self, vault: Path, capsys: pytest.CaptureFixture[str]
    ):
        app_logger.configure(verbosity=0)
        note = vault / "Dune.md"
        note.write_text(note.read_text().replace("> x", "> x, as I read it"))
        assert _write(vault, "Dune", "x", "y") == exits.SUCCESS
        assert _names(vault) == ["Dune.md", "Dune.md.new"]
        capsys.readouterr()

        assert _write(vault, RENAMED, "x", "y", "z") == exits.FAILED

        assert _names(vault) == ["Dune.md", "Dune.md.new"]
        reported = self._reported(capsys)
        assert "Dune.md.new" in reported

    def test_its_name_is_escaped(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        app_logger.configure(verbosity=0)
        vault = tmp_path / "vault"
        _write(vault, "Dune\x1b[31m", "x")
        (vault / f"{RENAMED}.md").write_text("# my own page\n")

        assert _write(vault, RENAMED, "x", "y") == exits.FAILED

        reported = self._reported(capsys)
        assert "\x1b" not in reported
        assert "Dune\\x1b[31m.md" in reported

    def test_nor_one_of_two(self, vault: Path, capsys: pytest.CaptureFixture[str]):
        app_logger.configure(verbosity=0)
        (vault / "Dune Again.md").write_bytes((vault / "Dune.md").read_bytes())

        assert _write(vault, RENAMED, "x", "y") == exits.FAILED

        assert _names(vault) == ["Dune Again.md", "Dune.md"]
        assert "Dune Again.md, Dune.md alone" in self._reported(capsys)

    def test_nor_when_the_move_fails(
        self,
        vault: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ):
        app_logger.configure(verbosity=0)

        def refuse(*_: object, **__: object) -> None:
            raise PermissionError("read-only")

        monkeypatch.setattr(Path, "rename", refuse)
        monkeypatch.setattr("os.link", refuse)

        assert _write(vault, RENAMED, "x", "y") == exits.FAILED

        assert _names(vault) == ["Dune.md"]
        assert "read-only" in self._reported(capsys)


class TestANoteNotTaggedForTheBook:
    """
    Untagged, or tagged for an asset id no book answers to now: the book's
    all the same when it names the file the book is read from, or, naming
    none, holds nothing but the book's highlights.
    """

    @pytest.mark.parametrize("suffix", [False, True])
    def test_a_note_an_older_release_wrote_moves(self, vault: Path, suffix: bool):
        _legacy(vault / "Dune.md")

        assert _write(vault, RENAMED, "x", "y", suffix=suffix) == exits.SUCCESS

        assert _names(vault) == [f"{RENAMED}.md"]
        text = (vault / f"{RENAMED}.md").read_text(encoding="utf-8")
        assert "> y" in text
        assert text.endswith(MINE)
        before = (vault / f"{RENAMED}.md").read_bytes()
        assert _write(vault, RENAMED, "x", "y", suffix=suffix) == exits.SUCCESS
        assert (vault / f"{RENAMED}.md").read_bytes() == before

    def test_one_moved_but_not_rewritten_stays_quiet(self, vault: Path):
        # Its region comes out the same, so it is moved and not tagged; the
        # rerun finds it the book's by its highlights, and writes nothing.
        before = _legacy(vault / "Dune.md")

        for _ in range(2):
            assert _write(vault, RENAMED, "x") == exits.SUCCESS
            assert _names(vault) == [f"{RENAMED}.md"]
            assert (vault / f"{RENAMED}.md").read_bytes() == before

    @pytest.mark.parametrize("suffix", [False, True])
    def test_a_re_imported_books_note_moves(self, vault: Path, suffix: bool):
        # Removed from Books and added again, the book answers to NEW; its
        # note, not rewritten since, is tagged for A, which nothing answers
        # to, and names the file the book is read from.
        assert _write(vault, "Dune", "x", asset="NEW") == exits.SUCCESS

        assert _write(vault, RENAMED, "x", "y", suffix=suffix, asset="NEW") == 0

        assert _names(vault) == [f"{RENAMED}.md"]
        text = (vault / f"{RENAMED}.md").read_text(encoding="utf-8")
        assert "> y" in text
        assert text.endswith(MINE)
        before = (vault / f"{RENAMED}.md").read_bytes()
        assert _write(vault, RENAMED, "x", "y", suffix=suffix, asset="NEW") == 0
        assert (vault / f"{RENAMED}.md").read_bytes() == before


class TestANamesakeOfTheOldName:
    """
    ``Dune.pdf`` is still named as ``Dune.epub`` was, and wants its note's
    name. The note holds only the epub's highlights, and none of the PDF's.
    """

    NAMED = [
        Assignment(Path("Dune.epub"), f"{RENAMED}.epub", f"{RENAMED}.epub".lower()),
        Assignment(Path("Dune.pdf"), "Dune.pdf", "dune.pdf"),
    ]
    FOUND = [_highlight("x"), _highlight("y"), _highlight("p", "P", "Dune.pdf")]

    def _write(self, vault: Path, *, suffix: bool) -> int:
        assets: dict[str, str | None] = {"A": "Dune.epub", "P": "Dune.pdf"}
        return notes.write_vault(
            self.FOUND,
            str(vault),
            self.NAMED,
            copyable=(),
            suffix=suffix,
            assets=assets,
        )

    def test_without_suffix_neither_book_writes_it(
        self, vault: Path, capsys: pytest.CaptureFixture[str]
    ):
        # The PDF keeps the name and is refused the note, and the renamed
        # book cannot take it from under the PDF: it is named, not written.
        app_logger.configure(verbosity=0)
        before = _legacy(vault / "Dune.md")

        for _ in range(2):
            assert self._write(vault, suffix=False) == exits.FAILED
            assert _names(vault) == ["Dune.md"]
            assert (vault / "Dune.md").read_bytes() == before
            reported = capsys.readouterr().err
            assert "name it had before" in reported
            assert "another book's" in reported

    def test_under_suffix_the_note_moves_and_the_pdf_is_numbered(self, vault: Path):
        _legacy(vault / "Dune.md")

        assert self._write(vault, suffix=True) == exits.SUCCESS

        assert _names(vault) == ["Dune (2).md", f"{RENAMED}.md"]
        text = (vault / f"{RENAMED}.md").read_text(encoding="utf-8")
        assert "> y" in text
        assert text.endswith(MINE)
        assert "> p" in (vault / "Dune (2).md").read_text(encoding="utf-8")
        before = {name: (vault / name).read_bytes() for name in _names(vault)}
        assert self._write(vault, suffix=True) == exits.SUCCESS
        assert {name: (vault / name).read_bytes() for name in _names(vault)} == before


class TestWhatIsLookedFor:
    def test_not_a_note_naming_another_file(self, vault: Path):
        note = vault / "Dune.md"
        text = noteformat.BOOK_TAG.sub("", note.read_text(), count=1)
        other = f" src={disambiguator('Dune.pdf')}"
        note.write_text(noteformat.SOURCE_TAG.sub(other, text, count=1))

        assert _write(vault, RENAMED, "x", "y") == exits.SUCCESS

        assert _names(vault) == ["Dune.md", f"{RENAMED}.md"]

    def test_not_one_holding_a_highlight_the_book_has_not(self, vault: Path):
        # Holding only some of them is evidence of nothing: two editions
        # share a passage.
        _legacy(vault / "Dune.md")

        assert _write(vault, RENAMED, "y") == exits.SUCCESS

        assert _names(vault) == ["Dune.md", f"{RENAMED}.md"]

    def test_not_one_another_book_holds_alike(self, vault: Path):
        before = _legacy(vault / "Dune.md")
        named = [
            Assignment(Path("Dune.epub"), f"{RENAMED}.epub", f"{RENAMED}.epub"),
            Assignment(Path("Other.epub"), "Other.epub", "other.epub"),
        ]
        found = [_highlight("x"), _highlight("y"), _highlight("x", "O", "Other.epub")]
        assets: dict[str, str | None] = {"A": "Dune.epub", "O": "Other.epub"}

        code = notes.write_vault(found, str(vault), named, copyable=(), assets=assets)

        assert code == exits.SUCCESS
        assert _names(vault) == ["Dune.md", f"{RENAMED}.md", "Other.md"]
        assert (vault / "Dune.md").read_bytes() == before

    def test_nor_from_under_another_book_of_the_run(self, vault: Path):
        # Without suffix the other book keeps the name, and is refused the
        # note; it stays where it is, and the renamed book waits for it.
        named = [
            Assignment(Path("Dune Messiah.epub"), "Dune.epub", "dune.epub"),
            Assignment(Path("Dune.epub"), f"{RENAMED}.epub", f"{RENAMED}.epub"),
        ]
        found = [_highlight("x"), _highlight("y")]
        book = {"title": "M", "assetId": "M", "source": "Dune Messiah.epub"}
        found.append({"id": "m", "text": "m", "book": book})
        assets: dict[str, str | None] = {"A": "Dune.epub", "M": "Dune Messiah.epub"}

        code = notes.write_vault(found, str(vault), named, copyable=(), assets=assets)

        assert code == exits.FAILED
        assert MINE in (vault / "Dune.md").read_text()
        assert _names(vault) == ["Dune.md"]


def test_a_note_appearing_at_the_new_name_mid_move_is_not_replaced(
    tmp_path, monkeypatch
):
    # The move checked the new name was free and then renamed, and rename
    # replaces whatever is there: a note the reader saved at that name in
    # between was lost. Linked, the new name is claimed only if still free.
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "Old.md").write_text("the book's note", encoding="utf-8")
    (vault / "New.md").write_text("the reader's own", encoding="utf-8")
    monkeypatch.setattr(notes, "_present", lambda _: False)  # as if not yet

    moved = notes._gather(  # pylint: disable=protected-access
        vault, ["Old.md"], "New.md", ["New.md"]
    )

    assert not moved
    assert (vault / "New.md").read_text(encoding="utf-8") == "the reader's own"
    assert (vault / "Old.md").read_text(encoding="utf-8") == "the book's note"
