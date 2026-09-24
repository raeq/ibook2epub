"""
Tests for a note left under the name its book had before.

A book's note is named after the book, and the name can change under it:
``--name-by author-title`` adopted, or the book's metadata corrected. Its
note, tagged for it, was looked for only under the names the book has now,
so a fresh note was started beside it and the reader's writing stayed
behind in the old one without a word.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring

from pathlib import Path
from typing import Any

import pytest

from epubconvert.export import noteformat, notes
from epubconvert.utils import app_logger, exits
from epubconvert.utils.policy import Assignment

MINE = "\nTHREE PARAGRAPHS OF MY OWN\n"
RENAMED = "Frank Herbert - Dune"


def _highlight(text: str) -> dict[str, Any]:
    book = {"title": "Dune", "assetId": "A", "source": "Dune.epub"}
    return {"id": text, "text": text, "book": book}


def _write(vault: Path, name: str, *texts: str, suffix: bool = False) -> int:
    named = [Assignment(Path("Dune.epub"), f"{name}.epub", f"{name}.epub".lower())]
    found = [_highlight(text) for text in texts]
    return notes.write_vault(
        found, str(vault), named, copyable=(), suffix=suffix, assets={"A": "Dune.epub"}
    )


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

        def refuse(*_: object) -> None:
            raise PermissionError("read-only")

        monkeypatch.setattr(Path, "rename", refuse)

        assert _write(vault, RENAMED, "x", "y") == exits.FAILED

        assert _names(vault) == ["Dune.md"]
        assert "read-only" in self._reported(capsys)


class TestWhatIsLookedFor:
    def test_only_a_note_tagged_for_the_book(self, vault: Path):
        (vault / "Dune.md").write_text(
            noteformat.BOOK_TAG.sub("", (vault / "Dune.md").read_text(), count=1)
        )

        assert _write(vault, RENAMED, "x", "y") == exits.SUCCESS

        assert _names(vault) == ["Dune.md", f"{RENAMED}.md"]

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
