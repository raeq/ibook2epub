"""
Tests for two books that want one note.

A note is named after its book's file with the extension swapped for ``.md``,
but the run claims each book's name with the extension on. ``Dune.epub`` and
``Dune.pdf`` are two names to the run and one note to the vault: each run
wrote one book's highlights over the other's, said "Wrote 2 note(s)" and
exited 0. The same held for two names the filesystem cannot tell apart,
``Dune.epub`` and ``dune.pdf`` on a case-insensitive volume.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

from pathlib import Path
from typing import Any

import pytest

from epubconvert.collect import annotations
from epubconvert.export import notes
from epubconvert.run.run import main
from epubconvert.utils import app_logger, exits
from epubconvert.utils.policy import Assignment
from tests.conftest import make_metadata_package
from tests.test_annotations import highlight, library_row, make_databases


def _highlight(source: str, text: str) -> dict[str, Any]:
    return {
        "id": text,
        "text": text,
        "created": "2020-01-01T00:00:00Z",
        "book": {"title": "Dune", "source": source},
    }


def _named(*filenames: str) -> list[Assignment]:
    return [Assignment(Path(name), name, name.casefold()) for name in filenames]


def _write(vault: Path, *filenames: str, suffix: bool = False) -> int:
    found = [_highlight(name, f"TEXT OF {name}") for name in filenames]
    return notes.write_vault(
        found, str(vault), _named(*filenames), copyable=(), suffix=suffix
    )


class TestTwoBooksWantingOneNote:
    @pytest.mark.parametrize("second", ["Dune.pdf", "dune.pdf", "DUNE.PDF"])
    def test_the_first_keeps_the_note_and_the_second_is_not_written_into_it(
        self, tmp_path: Path, second: str
    ):
        vault = tmp_path / "vault"

        _write(vault, "Dune.epub", second)

        [note] = list(vault.iterdir())
        text = note.read_text(encoding="utf-8")
        assert "TEXT OF Dune.epub" in text
        assert f"TEXT OF {second}" not in text

    def test_the_loser_is_named_and_the_count_is_of_notes_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        app_logger.configure(verbosity=1)

        code = _write(tmp_path / "vault", "Dune.epub", "Dune.pdf")

        reported = capsys.readouterr().err
        assert "Wrote 1 note(s)" in reported
        assert "Dune.pdf" in reported
        assert "--on-collision suffix" in reported
        # A collision, as everywhere else in this tool: the fix is a flag.
        assert code == exits.SUCCESS

    def test_a_rerun_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # The note was rewritten twice on every run, once per book.
        vault = tmp_path / "vault"
        _write(vault, "Dune.epub", "Dune.pdf")
        app_logger.configure(verbosity=1)
        capsys.readouterr()

        _write(vault, "Dune.epub", "Dune.pdf")

        assert "Wrote 0 note(s)" in capsys.readouterr().err

    def test_names_the_filesystem_tells_apart_both_get_notes(self, tmp_path: Path):
        vault = tmp_path / "vault"

        _write(vault, "Dune.epub", "Dune Messiah.pdf")

        assert sorted(note.name for note in vault.iterdir()) == [
            "Dune Messiah.md",
            "Dune.md",
        ]


class TestUnderSuffixEachBookGetsItsOwnNote:
    def test_the_second_is_numbered(self, tmp_path: Path):
        vault = tmp_path / "vault"

        code = _write(vault, "Dune.epub", "Dune.pdf", suffix=True)

        assert code == exits.SUCCESS
        assert "TEXT OF Dune.epub" in (vault / "Dune.md").read_text(encoding="utf-8")
        assert "TEXT OF Dune.pdf" in (vault / "Dune (2).md").read_text(encoding="utf-8")

    def test_a_numbered_note_never_takes_another_books_own_name(self, tmp_path: Path):
        # "Dune (2).epub" is a book of its own; the PDF moves on past it rather
        # than taking its note, whichever order the books come in.
        vault = tmp_path / "vault"

        _write(vault, "Dune.epub", "Dune.pdf", "Dune (2).epub", suffix=True)

        assert "TEXT OF Dune (2).epub" in (vault / "Dune (2).md").read_text(
            encoding="utf-8"
        )
        assert "TEXT OF Dune.pdf" in (vault / "Dune (3).md").read_text(encoding="utf-8")

    def test_a_numbered_name_fits_the_filesystem(self, tmp_path: Path):
        long = "D" * 250 + ".epub"
        vault = tmp_path / "vault"

        _write(vault, long, long[:-5] + ".pdf", suffix=True)

        written = [note.name for note in vault.iterdir()]
        assert len(written) == 2
        assert all(len(name.encode()) <= 255 for name in written)


class TestAPackageAndAPdfOfOneName:
    """The reviewer's case, end to end through ``-ao``."""

    def test_each_keeps_its_own_highlights(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library, container, vault = tmp_path / "lib", tmp_path / "c", tmp_path / "v"
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(container, policy),
        )
        package = make_metadata_package(
            library, "Dune.epub", title="Dune", identifier="urn:uuid:A"
        )
        pdf = library / "Dune.pdf"
        pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        make_databases(
            container,
            rows=[
                highlight(asset="A", uuid="UA", text="EPUB HIGHLIGHT"),
                highlight(asset="P", uuid="UP", text="PDF HIGHLIGHT"),
            ],
            books=[
                library_row(asset="A", path=str(package), title="Dune"),
                library_row(asset="P", path=str(pdf), title="Dune (PDF)"),
            ],
        )
        flags = ["-s", str(library), "-ao", str(vault), "--annotations-format"]

        for _ in range(2):
            main([*flags, "markdown", "-q"])
            text = (vault / "Dune.md").read_text(encoding="utf-8")
            assert "EPUB HIGHLIGHT" in text
            assert "PDF HIGHLIGHT" not in text

        main([*flags, "markdown", "-q", "--on-collision", "suffix"])

        assert "PDF HIGHLIGHT" in (vault / "Dune (2).md").read_text(encoding="utf-8")
