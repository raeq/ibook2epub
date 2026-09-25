"""
Tests for books whose names are all that tells them apart.

A book that declares no usable identifier -- none, or a placeholder such as
``none`` -- or one it shares with another book, cannot be told from its
namesake by anything the planner read, so the name decided: a book could be
reported exported from another book's file and never written, and
``--force`` or ``--refresh`` wrote it over the other's archive, maybe the
last copy of a book deleted from Apple Books (formal/README.md,
``Unidentifiable``). Every archive now names its source
(:mod:`epubconvert.export.provenance`), and where nothing does, the planner
refuses to guess between two books rather than hand the file to one.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from epubconvert.collect import annotations
from epubconvert.export import provenance
from epubconvert.export.archive import zip_package
from epubconvert.export.naming import disambiguator
from epubconvert.run import annotating, holders, run
from tests.conftest import make_metadata_package, make_package, remove_tree, unmark
from tests.test_annotations import highlight, library_row, make_databases

AUTHOR_TITLE = ["--name-by", "author-title"]
SUFFIX = ["--on-collision", "suffix"]
MODES = pytest.mark.parametrize("mode", [[], SUFFIX], ids=["skip", "suffix"])
#: The name author-title gives every edition here.
NAME = "Frank Herbert - Dune.epub"


def book(library: Path, folder: str, identifier: str = "none") -> Path:
    return make_metadata_package(
        library / folder,
        "Dune.epub",
        title="Dune",
        creator="Frank Herbert",
        identifier=identifier,
    )


def convert(library: Path, output_dir: Path, *extra: str) -> int:
    return run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", *extra])


def rows(library: Path, output_dir: Path, capsys, *extra: str) -> list[dict[str, Any]]:
    capsys.readouterr()
    run.main(["-s", str(library), "-o", str(output_dir), "--list", "--json", *extra])
    listed: list[dict[str, Any]] = json.loads(capsys.readouterr().out)
    return listed


def placed(
    listed: list[dict[str, Any]], library: Path
) -> dict[str, tuple[str, str | None]]:
    """Each book's status and file, by its folder; each orphan's under its file."""
    found: dict[str, tuple[str, str | None]] = {}
    for row in listed:
        target = Path(row["target"]).name if row["target"] else None
        if row["source"] is None:
            found[f"orphan:{target}"] = (row["status"], target)
        else:
            folder = Path(row["source"]).relative_to(library).parent.as_posix()
            found[folder] = (row["status"], target)
    return found


def listing(
    library: Path, output_dir: Path, capsys, *extra: str
) -> dict[str, tuple[str, str | None]]:
    return placed(rows(library, output_dir, capsys, *extra), library)


def source(output_dir: Path, name: str) -> str | None:
    return provenance.read_source(output_dir / name)


def mine(folder: str) -> str:
    return disambiguator(f"{folder}/dune.epub")


def shelf(output_dir: Path) -> dict[str, int]:
    """Every archive on the shelf, and when it was written."""
    return {path.name: path.stat().st_mtime_ns for path in output_dir.glob("*.epub")}


class TestTwoBooksThatDeclareNothing:
    @MODES
    def test_a_rerun_writes_nothing_and_each_keeps_its_file(
        self, tmp_path, output_dir, capsys, mode
    ):
        library = tmp_path / "lib"
        book(library, "a")
        book(library, "b")
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)
        before = shelf(output_dir)

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE, *mode)
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)

        assert shelf(output_dir) == before
        assert listed["a"] == ("exported", NAME)
        assert source(output_dir, NAME) == mine("a")
        if mode:
            numbered = "Frank Herbert - Dune (2).epub"
            assert listed["b"] == ("exported", numbered)
            assert source(output_dir, numbered) == mine("b")
        else:
            assert listed["b"][0] == "collision"

    @MODES
    def test_the_survivor_is_not_exported_from_a_deleted_books_file(
        self, tmp_path, output_dir, capsys, mode
    ):
        library = tmp_path / "lib"
        book(library, "a")
        book(library, "b")
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)
        remove_tree(library / "a")

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE, *mode)

        assert listed[f"orphan:{NAME}"] == ("orphan", NAME)
        assert listed["b"] != ("exported", NAME)
        if mode:
            assert listed["b"] == ("exported", "Frank Herbert - Dune (2).epub")
        else:
            assert listed["b"][0] == "collision"

    @MODES
    @pytest.mark.parametrize("flag", ["--force", "--refresh"])
    def test_the_survivor_never_writes_over_a_deleted_books_file(
        self, tmp_path, output_dir, mode, flag
    ):
        library = tmp_path / "lib"
        book(library, "a")
        survivor = book(library, "b")
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)
        remove_tree(library / "a")
        later = (output_dir / NAME).stat().st_mtime + 100
        os.utime(survivor, (later, later))
        held = (output_dir / NAME).read_bytes()

        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode, flag)

        assert (output_dir / NAME).read_bytes() == held

    @MODES
    def test_a_book_added_again_finds_its_own_file(
        self, tmp_path, output_dir, capsys, mode
    ):
        library = tmp_path / "lib"
        book(library, "a")
        book(library, "b")
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)
        remove_tree(library / "a")
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)
        book(library, "a")
        before = shelf(output_dir)

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE, *mode)
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)

        assert listed["a"] == ("exported", NAME)
        assert shelf(output_dir) == before

    @MODES
    def test_a_folder_renamed_by_case_keeps_its_file(
        self, tmp_path, output_dir, capsys, mode
    ):
        library = tmp_path / "lib"
        book(library, "a")
        book(library, "b")
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)
        before = shelf(output_dir)
        (library / "a").rename(library / "A")

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE, *mode)
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)

        assert listed["A"] == ("exported", NAME)
        assert shelf(output_dir) == before

    def test_named_from_the_folder_each_keeps_its_number(
        self, tmp_path, output_dir, capsys
    ):
        # The default policy reads no package document, and markers are read
        # only where two books want one name: here, with numbered files.
        library = tmp_path / "lib"
        for folder in ("a", "b"):
            make_package(library / folder, "Dune.epub")
        convert(library, output_dir, "-q", *SUFFIX)
        remove_tree(library / "a")
        make_package(library / "c", "Dune.epub")

        listed = listing(library, output_dir, capsys, *SUFFIX)
        convert(library, output_dir, "-q", *SUFFIX)

        assert listed["b"] == ("exported", "Dune (2).epub")
        assert listed["c"][0] == "pending"
        assert source(output_dir, "Dune (2).epub") == disambiguator("b/dune.epub")


class TestNamedFromTheFolderAWriteReadsTheMarker:
    """
    A policy that names from the folder trusts a book's name for a report,
    which reads nothing (formal/README.md, ``FolderNamedReports``); before a
    write over an archive the marker is read, as the identifiers are.
    """

    @staticmethod
    def _left(tmp_path: Path, output_dir: Path) -> tuple[Path, Path]:
        library = tmp_path / "lib"
        make_package(library / "a", "Dune.epub")
        survivor = make_package(library / "b", "Dune.epub")
        convert(library, output_dir, "-q")
        remove_tree(library / "a")
        return library, survivor

    @pytest.mark.parametrize("flag", ["--force", "--refresh"])
    def test_the_survivor_is_not_written_over_a_deleted_books_file(
        self, tmp_path, output_dir, capsys, flag
    ):
        library, survivor = self._left(tmp_path, output_dir)
        later = (output_dir / "Dune.epub").stat().st_mtime + 100
        os.utime(survivor, (later, later))
        held = (output_dir / "Dune.epub").read_bytes()

        listed = listing(library, output_dir, capsys, flag)
        convert(library, output_dir, "-q", flag)

        assert (output_dir / "Dune.epub").read_bytes() == held
        assert listed["b"][0] == "collision"

    def test_a_refresh_of_highlights_leaves_it_alone(
        self, tmp_path, output_dir, monkeypatch
    ):
        library, survivor = self._left(tmp_path, output_dir)
        make_databases(
            tmp_path / "container",
            rows=[highlight(uuid="U0", asset="B")],
            books=[library_row(asset="B", path=str(survivor))],
        )
        monkeypatch.setattr(
            annotating,
            "collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        held = (output_dir / "Dune.epub").read_bytes()

        run.main(["-s", str(library), "-o", str(output_dir), "-q", "-ae", "-ar"])

        assert (output_dir / "Dune.epub").read_bytes() == held


class TestAMarkerDoesNotExcuseTheIdentifiers:
    """
    A marker names a path, case folded: a book deleted and another added at
    its path, by another case, is the same source. Where both declare a usable
    identifier and they differ, the file is still the other book's; so is it
    where the newcomer declares none and the file one.
    """

    NEWCOMERS = pytest.mark.parametrize("identifier", ["urn:uuid:NEW", "none"])

    @staticmethod
    def _replaced(
        tmp_path: Path, output_dir: Path, identifier: str
    ) -> tuple[Path, Path]:
        library = tmp_path / "lib"
        book(library, "c", "urn:uuid:GONE")
        convert(library, output_dir, "-q")
        remove_tree(library / "c")
        newcomer = make_metadata_package(
            library / "c", "dune.epub", title="Dune", identifier=identifier
        )
        assert provenance.source_of(newcomer, library) == mine("c")
        later = (output_dir / "Dune.epub").stat().st_mtime + 100
        os.utime(newcomer, (later, later))
        return library, newcomer

    @NEWCOMERS
    @pytest.mark.parametrize("flag", ["--force", "--refresh"])
    def test_a_newcomer_at_its_path_is_not_written_over_it(
        self, tmp_path, output_dir, flag, identifier
    ):
        library, _ = self._replaced(tmp_path, output_dir, identifier)
        held = (output_dir / "Dune.epub").read_bytes()

        convert(library, output_dir, "-q", flag)

        assert (output_dir / "Dune.epub").read_bytes() == held

    @NEWCOMERS
    def test_nor_are_its_highlights_written_into_it(
        self, tmp_path, output_dir, monkeypatch, identifier
    ):
        library, newcomer = self._replaced(tmp_path, output_dir, identifier)
        make_databases(
            tmp_path / "container",
            rows=[highlight(uuid="U0", asset="N")],
            books=[library_row(asset="N", path=str(newcomer))],
        )
        monkeypatch.setattr(
            annotating,
            "collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        held = (output_dir / "Dune.epub").read_bytes()

        run.main(["-s", str(library), "-o", str(output_dir), "-q", "-ae", "-ar"])

        assert (output_dir / "Dune.epub").read_bytes() == held


class TestTwoBooksThatShareAnIdentifier:
    """
    In suffix mode each has a marked file of its own, which it keeps when the
    other leaves. In skip mode the survivor was a collision and has none: the
    other's file, naming a source no book has and declaring the identifier
    the survivor now alone declares, is what a book moved from that folder
    finds (holders.moved), and it is taken for one -- as it is in suffix mode
    by a survivor not yet written. Pinned as the cost of finding a moved
    book's archive (formal/README.md, ``SharedIdMoved``).
    """

    SHARED = "urn:uuid:template"

    def test_the_survivor_is_not_exported_from_the_others_file(
        self, tmp_path, output_dir, capsys
    ):
        mode = SUFFIX
        library = tmp_path / "lib"
        book(library, "a", self.SHARED)
        book(library, "b", self.SHARED)
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)
        written = sorted(shelf(output_dir))
        a_file = next(name for name in written if source(output_dir, name) == mine("a"))
        remove_tree(library / "a")

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE, *mode)

        assert listed["b"][1] != a_file
        assert listed[f"orphan:{a_file}"] == ("orphan", a_file)

    def test_in_skip_mode_a_survivor_with_no_file_is_taken_for_a_moved_book(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        book(library, "a", self.SHARED)
        book(library, "b", self.SHARED)
        convert(library, output_dir, "-q", *AUTHOR_TITLE)
        assert sorted(shelf(output_dir)) == [NAME]
        remove_tree(library / "a")

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE)

        assert listed == {"b": ("exported", NAME)}

    def test_force_does_not_write_the_survivor_over_it(self, tmp_path, output_dir):
        mode = SUFFIX
        library = tmp_path / "lib"
        book(library, "a", self.SHARED)
        book(library, "b", self.SHARED)
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)
        a_file = next(
            name for name in shelf(output_dir) if source(output_dir, name) == mine("a")
        )
        held = (output_dir / a_file).read_bytes()
        remove_tree(library / "a")

        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode, "--force")

        assert (output_dir / a_file).read_bytes() == held


class TestADeletedBooksMarkedArchive:
    @MODES
    def test_a_namesake_added_since_does_not_take_it(
        self, tmp_path, output_dir, capsys, mode
    ):
        library = tmp_path / "lib"
        book(library, "a")
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)
        remove_tree(library / "a")
        book(library, "c")

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE, *mode)

        assert listed[f"orphan:{NAME}"] == ("orphan", NAME)
        assert listed["c"] != ("exported", NAME)

    def test_named_from_the_folder_the_orphan_is_still_listed(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        for folder in ("a", "b"):
            make_package(library / folder, "Dune.epub")
        convert(library, output_dir, "-q")
        remove_tree(library / "a")
        make_package(library / "c", "Dune.epub")

        listed = placed(rows(library, output_dir, capsys), library)

        # b and c both want Dune.epub, a's: neither is given it.
        assert listed["orphan:Dune.epub"] == ("orphan", "Dune.epub")
        assert listed["b"][0] == "collision"
        assert listed["c"][0] == "collision"


class TestABookThatDeclaresNoIdentifier:
    @staticmethod
    def _legacy(tmp_path: Path, output_dir: Path) -> Path:
        """An archive written before markers, declaring a book's identifier."""
        gone = book(tmp_path / "gone", "x", "urn:uuid:GONE")
        zip_package(gone, output_dir / NAME)
        library = tmp_path / "lib"
        book(library, "b")
        return library

    @MODES
    def test_it_never_takes_a_file_that_declares_one(
        self, tmp_path, output_dir, capsys, mode
    ):
        library = self._legacy(tmp_path, output_dir)
        held = (output_dir / NAME).read_bytes()

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE, *mode)
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode, "--force")

        assert listed["b"] != ("exported", NAME)
        assert listed[f"orphan:{NAME}"] == ("orphan", NAME)
        assert (output_dir / NAME).read_bytes() == held
        if mode:
            assert listed["b"] == ("pending", "Frank Herbert - Dune (2).epub")
        else:
            assert listed["b"][0] == "collision"


class TestNamedFromTheFolderAnArchiveWrittenBeforeMarkers:
    @staticmethod
    def _legacy(tmp_path: Path, output_dir: Path, a_identifier: str) -> Path:
        """b/Dune.epub exported alone before markers, and a/Dune.epub added."""
        library = tmp_path / "lib"
        book(library, "b", "urn:uuid:B")
        convert(library, output_dir, "-q")
        unmark(output_dir / "Dune.epub")
        book(library, "a", a_identifier)
        return library

    def test_the_book_whose_identifier_it_declares_keeps_it(
        self, tmp_path, output_dir, capsys
    ):
        # a sorts first and claimed the name: reported exported from b's
        # archive, and b a collision.
        library = self._legacy(tmp_path, output_dir, "urn:uuid:A")

        listed = listing(library, output_dir, capsys)

        assert listed["b"] == ("exported", "Dune.epub")
        assert listed["a"][0] == "collision"

    def test_a_book_that_declares_none_is_not_written_over_it(
        self, tmp_path, output_dir, capsys
    ):
        library = self._legacy(tmp_path, output_dir, "none")
        remove_tree(library / "b")
        held = (output_dir / "Dune.epub").read_bytes()

        listed = listing(library, output_dir, capsys, "--force")
        convert(library, output_dir, "-q", "--force")

        assert listed["a"][0] == "collision"
        assert (output_dir / "Dune.epub").read_bytes() == held


class TestAnArchiveWrittenBeforeMarkers:
    @staticmethod
    def _crowd(tmp_path: Path, output_dir: Path, mode: list[str]) -> Path:
        library = tmp_path / "lib"
        book(library, "a")
        book(library, "b")
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)
        for archive in output_dir.glob("*.epub"):
            unmark(archive)
        return library

    def test_two_books_that_cannot_be_told_apart_are_both_refused_it(
        self, tmp_path, output_dir, capsys
    ):
        library = self._crowd(tmp_path, output_dir, [])
        held = shelf(output_dir)

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE)
        convert(library, output_dir, *AUTHOR_TITLE)
        said = capsys.readouterr().err

        assert listed["a"][0] == "collision"
        assert listed["b"][0] == "collision"
        assert f"orphan:{NAME}" not in listed
        assert shelf(output_dir) == held
        assert "cannot tell these books apart" in said
        assert "none was written" in said

    def test_in_suffix_mode_each_is_given_a_name_of_its_own(
        self, tmp_path, output_dir, capsys
    ):
        library = self._crowd(tmp_path, output_dir, SUFFIX)

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE, *SUFFIX)
        convert(library, output_dir, *AUTHOR_TITLE, *SUFFIX)
        said = capsys.readouterr().err
        after = shelf(output_dir)
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *SUFFIX)

        assert listed["a"] == ("pending", "Frank Herbert - Dune (3).epub")
        assert listed["b"] == ("pending", "Frank Herbert - Dune (4).epub")
        assert "cannot tell these books apart" in said
        assert "each was given a name of its own" in said
        assert source(output_dir, "Frank Herbert - Dune (3).epub") == mine("a")
        assert source(output_dir, "Frank Herbert - Dune (4).epub") == mine("b")
        assert shelf(output_dir) == after

    def test_a_single_book_keeps_it_and_it_is_not_restamped(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        book(library, "a")
        convert(library, output_dir, "-q", *AUTHOR_TITLE)
        unmark(output_dir / NAME)
        before = shelf(output_dir)

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE)
        convert(library, output_dir, *AUTHOR_TITLE)

        assert listed["a"] == ("exported", NAME)
        assert shelf(output_dir) == before
        assert source(output_dir, NAME) is None
        assert "cannot tell" not in capsys.readouterr().err

    @MODES
    def test_the_listing_the_dry_run_and_the_run_agree(
        self, tmp_path, output_dir, capsys, mode
    ):
        library = self._crowd(tmp_path, output_dir, mode)
        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE, *mode)
        pending = sorted(
            str(name) for status, name in listed.values() if status == "pending"
        )

        convert(library, output_dir, *AUTHOR_TITLE, *mode, "-d")
        dry = capsys.readouterr().out
        before = set(shelf(output_dir))
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)

        assert sorted(set(shelf(output_dir)) - before) == pending
        assert f"would export {len(pending)} epub file(s)" in dry


class TestMarkersAreReadOnlyWhereANameCannotBeTrusted:
    @staticmethod
    def _counted(monkeypatch) -> Counter[str]:
        reads: Counter[str] = Counter()
        real = provenance.read_source

        def counting(archive: Path) -> str | None:
            reads[archive.name] += 1
            return real(archive)

        monkeypatch.setattr(provenance, "read_source", counting)
        holders._identifier_of.cache_clear()  # pylint: disable=protected-access
        return reads

    @pytest.mark.parametrize("policy", [[], AUTHOR_TITLE])
    @MODES
    def test_a_no_op_rerun_over_identified_books_reads_no_marker(
        self, tmp_path, output_dir, monkeypatch, policy, mode
    ):
        library = tmp_path / "lib"
        for index in range(4):
            make_metadata_package(
                library,
                f"Book {index}.epub",
                title=f"Book {index}",
                identifier=f"urn:uuid:{index}",
            )
        convert(library, output_dir, "-q", *policy, *mode)
        reads = self._counted(monkeypatch)

        convert(library, output_dir, "-q", *policy, *mode)
        run.main(["-s", str(library), "-o", str(output_dir), "--list", *policy, *mode])

        assert reads == Counter()
