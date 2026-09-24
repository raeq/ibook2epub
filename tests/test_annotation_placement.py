"""
Tests for which archive on the shelf a book's highlights go into.

The annotation routes found a book's archive by its assigned name. But a file
under that name is not proof that it holds the book: the planner reads the
archive's identifier and, when it holds another book, reports a collision or
moves the book on to its marked name. The routes skipped that question, so
``-ae -ar`` wrote one edition's highlights into another edition's archive --
likely the last copy of a book deleted from the library -- and the edition
that had moved on never got its own.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import json
import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.collect import annotations
from epubconvert.run.run import main
from tests.conftest import make_metadata_package, remove_tree
from tests.test_annotations import highlight, library_row, make_databases
from tests.test_copy_claims import zipped_book

PLAIN = "Frank Herbert - Dune.epub"


def _embedded(archive: Path) -> list[str] | None:
    with ZipFile(archive) as opened:
        if annotations.EMBEDDED_PATH not in opened.namelist():
            return None
        document = json.loads(opened.read(annotations.EMBEDDED_PATH))
    return [entry["id"] for entry in document["annotations"]]


def _shelf_after_an_edition_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str]
) -> tuple[Path, Path]:
    """
    Export the 1965 edition, delete it, add the Ace edition, export again.

    The 1965 edition's archive keeps the plain name; the Ace edition, with a
    highlight in Apple's database, is either a collision (skip) or moved on to
    its marked name (suffix).
    """
    library, output = tmp_path / "lib", tmp_path / "out"
    make_metadata_package(
        library,
        "Dune (1965).epub",
        title="Dune",
        creator="Frank Herbert",
        identifier="urn:uuid:1",
    )
    main(["-s", str(library), "-o", str(output), "-m", "0", "-q", *flags])
    remove_tree(library / "Dune (1965).epub")
    make_metadata_package(
        library,
        "Dune (Ace).epub",
        title="Dune",
        creator="Frank Herbert",
        identifier="urn:uuid:2",
    )
    make_databases(
        tmp_path / "container",
        rows=[highlight()],
        books=[library_row(path="/x/Dune (Ace).epub", title="Dune")],
    )
    monkeypatch.setattr(
        "epubconvert.run.annotating.collect_annotations",
        lambda policy=None: annotations.collect(tmp_path / "container", policy),
    )
    return library, output


class TestARefreshWritesOnlyIntoTheBooksOwnArchive:
    @pytest.mark.parametrize("mode", ["skip", "suffix"])
    def test_another_editions_archive_is_left_alone(self, tmp_path, monkeypatch, mode):
        flags = ["--name-by", "author-title", "--on-collision", mode]
        library, output = _shelf_after_an_edition_is_replaced(
            tmp_path, monkeypatch, flags
        )
        main(["-s", str(library), "-o", str(output), "-m", "0", "-q", *flags])

        main(["-s", str(library), "-o", str(output), "-ae", "-ar", "-q", *flags])

        assert _embedded(output / PLAIN) is None

    def test_an_edition_moved_to_its_marked_name_gets_its_highlights(
        self, tmp_path, monkeypatch
    ):
        flags = ["--name-by", "author-title", "--on-collision", "suffix"]
        library, output = _shelf_after_an_edition_is_replaced(
            tmp_path, monkeypatch, flags
        )
        main(["-s", str(library), "-o", str(output), "-m", "0", "-q", *flags])
        [moved] = [path for path in output.glob("*.epub") if path.name != PLAIN]

        main(["-s", str(library), "-o", str(output), "-ae", "-ar", "-q", *flags])

        assert _embedded(moved) == ["U1"]


class TestAHighlightWithNoArchiveOfItsOwnIsReported:
    def test_a_name_held_by_another_book_does_not_count_as_a_home(
        self, tmp_path, monkeypatch, capsys
    ):
        # Skip mode: the Ace edition collides and is not written, and the file
        # under its name holds the 1965 edition. The warning looked only for
        # a file of that name, found one, and said nothing.
        flags = ["--name-by", "author-title"]
        library, output = _shelf_after_an_edition_is_replaced(
            tmp_path, monkeypatch, flags
        )

        main(["-s", str(library), "-o", str(output), "-m", "0", "-ae", *flags])

        warned = capsys.readouterr().err
        assert "reached no file: Dune (Ace).epub" in warned
        assert _embedded(output / PLAIN) is None

    @pytest.mark.parametrize("flag", ["--force", "--refresh"])
    def test_a_folder_named_collision_before_a_write_is_reported(
        self, tmp_path, monkeypatch, capsys, flag
    ):
        # Named from the folder, so the plan reads the identifier only before
        # it writes: --force and --refresh found the name held by another
        # book and called it a collision, while the warning trusted the name
        # and said nothing.
        library, output = tmp_path / "lib", tmp_path / "out"
        make_metadata_package(
            library / "a", "Dune.epub", title="Dune", identifier="urn:uuid:1"
        )
        main(["-s", str(library), "-o", str(output), "-m", "0", "-q"])
        remove_tree(library / "a")
        make_metadata_package(
            library / "b", "Dune.epub", title="Dune", identifier="urn:uuid:2"
        )
        make_databases(
            tmp_path / "container",
            rows=[highlight()],
            books=[library_row(path="/x/Dune.epub", title="Dune")],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        if flag == "--refresh":
            later = (output / "Dune.epub").stat().st_mtime + 60
            os.utime(library / "b" / "Dune.epub", (later, later))
        capsys.readouterr()

        main(["-s", str(library), "-o", str(output), "-m", "0", "-ae", flag])

        warned = capsys.readouterr().err
        assert "Name collision, skipping: Dune.epub" in warned
        assert "reached no file: Dune.epub" in warned
        assert _embedded(output / "Dune.epub") is None


class TestARefreshFindsABookMovedOnPastACopy:
    def test_the_package_moved_on_past_a_copys_file_gets_its_highlights(
        self, tmp_path, monkeypatch
    ):
        # A zipped book copied as "Dune A.epub"; a package that -p strip names
        # the same is placed past it at "Dune A (2).epub". -ar placed the
        # packages without the copies, read no identifier, took the copy's
        # file for the package's own, and then, comparing before the write,
        # found no archive at all: nothing was refreshed.
        library, output = tmp_path / "lib", tmp_path / "out"
        zipped_book(tmp_path, library / "z" / "Dune A.epub", "urn:uuid:Z", "Z")
        flags = ["-p", "strip", "--on-collision", "suffix"]
        main(["-s", str(library), "-o", str(output), "-m", "0", "-q", *flags])
        package = make_metadata_package(
            library / "p", "Dune: A.epub", title="Dune", identifier="urn:uuid:P"
        )
        main(["-s", str(library), "-o", str(output), "-m", "0", "-q", *flags])
        make_databases(
            tmp_path / "container",
            rows=[highlight()],
            books=[library_row(path=str(package), title="Dune")],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )

        main(["-s", str(library), "-o", str(output), "-ae", "-ar", "-q", *flags])

        assert _embedded(output / "Dune A (2).epub") == ["U1"]
        assert _embedded(output / "Dune A.epub") is None


class TestAHighlightRecordedAgainstNoBookIsReported:
    """
    Apple records some highlights against no asset. Those name no book, so
    ``-ae`` puts them in none, and only a detached file carries them; ``-ae``
    and ``-ae -ar`` said nothing of them at all.
    """

    WARNING = (
        "2 highlight(s) Apple recorded against no book were not embedded; "
        "use -ad FILE or -ao FILE"
    )

    @staticmethod
    def _library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        library = tmp_path / "lib"
        book = make_metadata_package(
            library, "Book.epub", title="Book", identifier="urn:uuid:1"
        )
        make_databases(
            tmp_path / "container",
            rows=[
                highlight(uuid="U1", asset="A1"),
                highlight(uuid="U2", asset=None),
                highlight(uuid="U3", asset=""),
            ],
            books=[library_row(asset="A1", title="Book", path=str(book))],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        return library

    @pytest.mark.parametrize("how", [["-m", "0"], ["-ar"]])
    def test_embedding_says_so(self, tmp_path, monkeypatch, capsys, how):
        library, output = self._library(tmp_path, monkeypatch), tmp_path / "out"
        main(["-s", str(library), "-o", str(output), "-m", "0", "-q"])
        capsys.readouterr()

        code = main(["-s", str(library), "-o", str(output), "-ae", *how])

        assert code == 0
        assert self.WARNING in capsys.readouterr().err

    def test_a_detached_file_is_where_they_went(self, tmp_path, monkeypatch, capsys):
        library, output = self._library(tmp_path, monkeypatch), tmp_path / "out"

        main(
            ["-s", str(library), "-o", str(output), "-m", "0", "-ae"]
            + ["-ad", str(tmp_path / "highlights.json")]
        )

        assert "recorded against no book" not in capsys.readouterr().err
