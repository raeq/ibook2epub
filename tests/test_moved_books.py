"""
Tests for a book moved to another folder of the library.

Each archive names the book's path in the library
(:mod:`epubconvert.export.provenance`), and one naming a path no book of the
library has is another book's: a deleted one's, maybe its last copy. A book
moved to another folder is such a path too, and cost a rewrite and an orphan,
or in skip mode a collision until the old file was moved away. Where the book
declares a usable identifier no other book of the library declares, and the
file declares it too, the file is the book's own, moved: found, kept, and
written over by ``--refresh``. Without such an identifier the move still costs
a rewrite, as it did.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import os
from pathlib import Path

import pytest

from epubconvert.collect import annotations
from epubconvert.run import annotating, run
from tests.conftest import make_package
from tests.test_annotations import highlight, library_row, make_databases
from tests.test_told_apart import (
    AUTHOR_TITLE,
    MODES,
    NAME,
    SUFFIX,
    book,
    convert,
    listing,
    mine,
    shelf,
    source,
)

POLICIES = pytest.mark.parametrize(
    "policy", [[], AUTHOR_TITLE], ids=["folder", "author-title"]
)


def _name(policy: list[str]) -> str:
    return NAME if policy else "Dune.epub"


def _moved(tmp_path: Path, output_dir: Path, *extra: str) -> Path:
    """a/Dune.epub, declaring an identifier of its own, exported, then moved to b/."""
    library = tmp_path / "lib"
    book(library, "a", "urn:uuid:MOVED")
    convert(library, output_dir, "-q", *extra)
    (library / "a").rename(library / "b")
    return library


class TestABookWithAnIdentifierOfItsOwn:
    @POLICIES
    @MODES
    def test_it_is_exported_from_its_old_file(
        self, tmp_path, output_dir, capsys, policy, mode
    ):
        library = _moved(tmp_path, output_dir, *policy, *mode)
        before = shelf(output_dir)

        listed = listing(library, output_dir, capsys, *policy, *mode)
        convert(library, output_dir, *policy, *mode)
        ran = capsys.readouterr()

        assert listed == {"b": ("exported", _name(policy))}
        assert shelf(output_dir) == before
        assert "orphan" not in ran.out
        # Written for a/, and not stamped again: a quiet rerun writes nothing.
        assert source(output_dir, _name(policy)) == mine("a")

    @POLICIES
    @MODES
    def test_refresh_writes_over_it_and_names_the_new_folder(
        self, tmp_path, output_dir, capsys, policy, mode
    ):
        library = _moved(tmp_path, output_dir, *policy, *mode)
        name = _name(policy)
        later = (output_dir / name).stat().st_mtime + 100
        os.utime(library / "b" / "Dune.epub", (later, later))

        listed = listing(library, output_dir, capsys, *policy, *mode, "--refresh")
        convert(library, output_dir, "-q", *policy, *mode, "--refresh")

        assert listed == {"b": ("pending", name)}
        assert sorted(shelf(output_dir)) == [name]
        assert source(output_dir, name) == mine("b")

    @POLICIES
    def test_force_writes_over_it(self, tmp_path, output_dir, policy):
        library = _moved(tmp_path, output_dir, *policy)

        convert(library, output_dir, "-q", *policy, "--force")

        assert sorted(shelf(output_dir)) == [_name(policy)]
        assert source(output_dir, _name(policy)) == mine("b")

    @POLICIES
    def test_its_highlights_are_written_into_it(
        self, tmp_path, output_dir, monkeypatch, policy
    ):
        library = _moved(tmp_path, output_dir, *policy)
        make_databases(
            tmp_path / "container",
            rows=[highlight(uuid="U0", asset="M")],
            books=[library_row(asset="M", path=str(library / "b" / "Dune.epub"))],
        )
        monkeypatch.setattr(
            annotating,
            "collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        before = (output_dir / _name(policy)).read_bytes()

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-q", *policy, "-ae", "-ar"]
        )

        assert code == 0
        assert (output_dir / _name(policy)).read_bytes() != before
        assert source(output_dir, _name(policy)) == mine("b")

    @MODES
    def test_beside_a_namesake_it_keeps_its_file(
        self, tmp_path, output_dir, capsys, mode
    ):
        # Named from the folder, two books of one name: the marker is read
        # where the names are claimed, and names a folder no book has.
        library = tmp_path / "lib"
        book(library, "a", "urn:uuid:MOVED")
        book(library, "c", "urn:uuid:OTHER")
        convert(library, output_dir, "-q", *mode)
        (library / "a").rename(library / "b")
        before = shelf(output_dir)

        listed = listing(library, output_dir, capsys, *mode)
        convert(library, output_dir, "-q", *mode)

        assert listed["b"] == ("exported", "Dune.epub")
        assert shelf(output_dir) == before
        assert not [key for key in listed if key.startswith("orphan:")]


class TestTheSafeSideStands:
    @MODES
    def test_a_book_sharing_its_identifier_is_refused_it(
        self, tmp_path, output_dir, capsys, mode
    ):
        # Another book of the library declares the identifier: nothing says
        # which of the two the file was.
        library = tmp_path / "lib"
        book(library, "a", "urn:uuid:SHARED")
        book(library, "c", "urn:uuid:SHARED")
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode)
        a_file = next(
            n for n in shelf(output_dir) if source(output_dir, n) == mine("a")
        )
        held = (output_dir / a_file).read_bytes()
        (library / "a").rename(library / "b")

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE, *mode)
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *mode, "--force")

        assert listed["b"] != ("exported", a_file)
        assert (output_dir / a_file).read_bytes() == held

    def test_a_book_that_declares_none_is_written_again(
        self, tmp_path, output_dir, capsys
    ):
        # Pinned: nothing tells its old file from a deleted namesake's.
        library = tmp_path / "lib"
        book(library, "a")
        convert(library, output_dir, "-q", *AUTHOR_TITLE, *SUFFIX)
        (library / "a").rename(library / "b")

        listed = listing(library, output_dir, capsys, *AUTHOR_TITLE, *SUFFIX)

        assert listed["b"] == ("pending", "Frank Herbert - Dune (2).epub")
        assert listed[f"orphan:{NAME}"] == ("orphan", NAME)

    def test_named_from_the_folder_one_that_declares_none_is_trusted_alone(
        self, tmp_path, output_dir, capsys
    ):
        # Nothing is read for a book alone in wanting its name (the
        # FolderNamedReports limit): moved, it is found by its name, as before.
        library = tmp_path / "lib"
        make_package(library / "a", "Dune.epub")
        convert(library, output_dir, "-q")
        (library / "a").rename(library / "b")

        listed = listing(library, output_dir, capsys)

        assert listed == {"b": ("exported", "Dune.epub")}
