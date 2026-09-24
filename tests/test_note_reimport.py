"""
Tests for a note tagged for an asset no book of the run answers to.

Removing a book from Books and adding it again gives it a new asset id. Its
note, tagged for the old one, was refused on every later run as "another
book's", with advice to rerun with ``--on-collision suffix`` that could not
help. A note is refused now only when its tag is another book's that this
run knows of -- from Apple's library, which lists every book whether or not
it has highlights, or from the highlights themselves -- or when its
frontmatter names another edition.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

from pathlib import Path
from typing import Any

import pytest

from epubconvert.collect import library
from epubconvert.export import noteformat, notes
from epubconvert.export.naming import disambiguator
from epubconvert.utils import app_logger, exits
from epubconvert.utils.policy import Assignment
from tests.test_annotations import library_row, make_databases

MINE = "\nMY OWN THOUGHTS\n"


def _highlight(asset: str, text: str, **book: str) -> dict[str, Any]:
    described = {"title": "Dune", "assetId": asset, "source": "Dune.epub", **book}
    return {"id": f"{asset}-{text}", "text": text, "book": described}


def _write(
    vault: Path,
    found: list[dict[str, Any]],
    *,
    suffix: bool = False,
    assets: dict[str, str | None] | None = None,
) -> int:
    named = [Assignment(Path("Dune.epub"), "Dune.epub", "dune.epub")]
    return notes.write_vault(
        found, str(vault), named, copyable=(), suffix=suffix, assets=assets or {}
    )


def _tag(note: Path) -> str | None:
    held = noteformat.split(note.read_text(encoding="utf-8"))
    assert held is not None
    return held.book


@pytest.fixture(name="vault")
def vault_fixture(tmp_path: Path) -> Path:
    """A vault holding Dune's note, written under its old asset id."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = notes.compose([_highlight("OLDASSET", "a")]) + MINE
    (vault / "Dune.md").write_text(note, encoding="utf-8")
    return vault


class TestABookReimportedUnderANewAssetId:
    @pytest.mark.parametrize("suffix", [False, True])
    def test_its_note_is_still_its_own_and_is_retagged(self, vault: Path, suffix: bool):
        found = [_highlight("NEWASSET", "a"), _highlight("NEWASSET", "b")]

        for _ in range(2):
            assert _write(vault, found, suffix=suffix) == exits.SUCCESS

        [note] = list(vault.iterdir())
        text = note.read_text(encoding="utf-8")
        assert "> b" in text
        assert MINE in text
        assert _tag(note) == disambiguator("NEWASSET")

    def test_so_is_one_holding_none_of_its_highlights(self, vault: Path):
        # Nothing names another book: the note goes with its name.
        assert _write(vault, [_highlight("NEWASSET", "z")]) == exits.SUCCESS

        assert "> z" in (vault / "Dune.md").read_text(encoding="utf-8")


class TestANoteOfAnotherBookThisRunKnows:
    def test_one_in_the_library_without_highlights_is_refused(
        self, vault: Path, capsys: pytest.CaptureFixture[str]
    ):
        # OLDASSET is another book in Apple's library, read from another
        # package: the note is that book's even though it has no highlights.
        app_logger.configure(verbosity=0)
        before = (vault / "Dune.md").read_bytes()

        code = _write(
            vault,
            [_highlight("NEWASSET", "b")],
            assets={"OLDASSET": "Dune Messiah.epub", "NEWASSET": "Dune.epub"},
        )

        assert code == exits.FAILED
        assert (vault / "Dune.md").read_bytes() == before
        reported = capsys.readouterr().err
        assert "another book" in reported
        assert "--on-collision suffix" in reported
        assert "two books want one note" not in reported

    def test_under_suffix_the_book_is_numbered_past_it(self, vault: Path):
        before = (vault / "Dune.md").read_bytes()

        code = _write(
            vault,
            [_highlight("NEWASSET", "b")],
            suffix=True,
            assets={"OLDASSET": "Dune Messiah.epub"},
        )

        assert code == exits.SUCCESS
        assert (vault / "Dune.md").read_bytes() == before
        assert "> b" in (vault / "Dune (2).md").read_text(encoding="utf-8")

    def test_one_of_the_same_packages_other_asset_ids_is_not(self, vault: Path):
        # A package the library lists twice: its note is tagged for either.
        found = [_highlight("NEWASSET", "a"), _highlight("NEWASSET", "b")]

        code = _write(
            vault, found, assets={"OLDASSET": "Dune.epub", "NEWASSET": "Dune.epub"}
        )

        assert code == exits.SUCCESS
        assert "> b" in (vault / "Dune.md").read_text(encoding="utf-8")
        assert _tag(vault / "Dune.md") == disambiguator("OLDASSET")


class TestANoteNamingAnotherEdition:
    def test_is_not_taken_though_its_asset_is_gone(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        old = [_highlight("OLDASSET", "a", identifier="urn:isbn:9780441013593")]
        (vault / "Dune.md").write_text(notes.compose(old), encoding="utf-8")
        before = (vault / "Dune.md").read_bytes()
        found = [_highlight("NEWASSET", "b", identifier="urn:isbn:9780593099322")]

        assert _write(vault, found) == exits.FAILED
        assert (vault / "Dune.md").read_bytes() == before

    def test_an_untagged_note_of_the_same_edition_is_taken(self, tmp_path: Path):
        vault = tmp_path / "vault"
        vault.mkdir()
        same = {"identifier": "urn:isbn:9780441013593"}
        old = notes.compose([_highlight("OLDASSET", "a", **same)])
        untagged = noteformat.BOOK_TAG.sub("", old, count=1)
        (vault / "Dune.md").write_text(untagged, encoding="utf-8")

        assert _write(vault, [_highlight("NEWASSET", "b", **same)]) == 0

        assert "> b" in (vault / "Dune.md").read_text(encoding="utf-8")


class TestTheLibraryIsReadForEveryBook:
    def test_each_asset_id_with_the_package_it_is_read_from(self, tmp_path: Path):
        make_databases(
            tmp_path,
            rows=[],
            books=[
                library_row(asset="A", path="/Users/x/Books/Dune.epub"),
                library_row(asset="P", path="/Users/x/Books/Dune.pdf"),
                library_row(asset="C", path=None),
            ],
        )

        assert library.asset_sources(tmp_path) == {
            "A": "Dune.epub",
            "P": "Dune.pdf",
            "C": None,
        }

    def test_no_library_is_no_books(self, tmp_path: Path):
        assert library.asset_sources(tmp_path / "not there") == {}
