"""
Tests for the provenance marker every archive this tool writes carries.

With no state file, the shelf is the record, and a name on it is not a book:
two books that declare no usable identifier, or one between them, were told
apart by nothing but the name, so one could be reported exported from the
other's file and ``--force`` could write over it (formal/README.md,
``Unidentifiable``). The archive comment -- outside OCF, ignored by readers,
behind the central directory where it moves nothing -- now names the book's
source: a digest of its path in the library.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import os
import unicodedata
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.collect import annotations
from epubconvert.collect.validate import validate_archive
from epubconvert.export import provenance
from epubconvert.export.archive import replace_annotations, zip_package
from epubconvert.export.naming import disambiguator
from epubconvert.run import annotating, run
from epubconvert.utils import exits
from tests.conftest import make_metadata_package, make_package
from tests.test_annotations import highlight, library_row, make_databases


def comment_of(archive: Path) -> bytes:
    with ZipFile(archive) as opened:
        return opened.comment


def exported(tmp_path: Path, relative: str) -> tuple[Path, Path, Path]:
    """Export one book at *relative* in a library; its package, library, archive."""
    library = tmp_path / "lib"
    folder, _, name = relative.rpartition("/")
    package = make_metadata_package(library / folder, name, title="Dune")
    target = tmp_path / "out.epub"
    zip_package(package, target, provenance=provenance.source_of(package, library))
    return package, library, target


class TestTheMarker:
    def test_an_export_names_its_source_in_the_archive_comment(self, tmp_path):
        package, library, target = exported(tmp_path, "a/Dune.epub")

        expected = disambiguator("a/dune.epub")
        assert provenance.source_of(package, library) == expected
        assert comment_of(target) == f"ibook2epub/1 src={expected}".encode()

    def test_the_marked_archive_is_still_a_valid_epub(self, tmp_path):
        _, _, target = exported(tmp_path, "a/Dune.epub")

        assert not validate_archive(target)
        with ZipFile(target) as opened:
            first = opened.infolist()[0]
        assert (first.filename, first.header_offset) == ("mimetype", 0)
        assert target.read_bytes()[30:38] == b"mimetype"

    def test_the_marker_is_read_back_from_the_end_of_the_file(self, tmp_path):
        package, library, target = exported(tmp_path, "a/Dune.epub")

        assert provenance.read_source(target) == provenance.source_of(package, library)

    def test_an_export_without_a_source_carries_no_marker(self, tmp_path):
        package = make_package(tmp_path / "lib", "Dune.epub")
        target = tmp_path / "out.epub"

        zip_package(package, target)

        assert comment_of(target) == b""
        assert provenance.read_source(target) is None


class TestTheKey:
    def test_two_folders_holding_one_name_are_two_sources(self, tmp_path):
        library = tmp_path / "lib"

        first = provenance.source_of(library / "a" / "Dune.epub", library)
        second = provenance.source_of(library / "b" / "Dune.epub", library)

        assert first != second

    def test_a_rename_by_case_is_the_same_source(self, tmp_path):
        # A case-insensitive volume, the macOS default, keeps one folder
        # whatever its case: a rename by case must not cost the book its file.
        library = tmp_path / "lib"

        renamed = provenance.source_of(library / "A" / "dune.EPUB", library)

        assert renamed == provenance.source_of(library / "a" / "Dune.epub", library)

    def test_a_decomposed_name_is_the_same_source(self, tmp_path):
        library = tmp_path / "lib"
        composed = unicodedata.normalize("NFC", "Café.epub")
        decomposed = unicodedata.normalize("NFD", "Café.epub")

        assert provenance.source_of(
            library / decomposed, library
        ) == provenance.source_of(library / composed, library)

    def test_a_book_outside_the_library_has_no_source(self, tmp_path):
        assert provenance.source_of(tmp_path / "Dune.epub", tmp_path / "lib") is None

    def test_no_library_no_source(self, tmp_path):
        assert provenance.source_of(tmp_path / "Dune.epub", None) is None


class TestReadingAMarker:
    @staticmethod
    def _commented(tmp_path: Path, comment: bytes) -> Path:
        path = tmp_path / "book.epub"
        with ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.comment = comment
        return path

    @pytest.mark.parametrize(
        "comment",
        [
            b"",
            b"made by another tool",
            b"ibook2epub/1 src=NOTHEX00",
            b"ibook2epub/1 src=abc",
            b"ibook2epub/0 src=0123abcd",
            b"ibook2epub/1 src=0123abcd\x1b[2K",
            b" ibook2epub/1 src=0123abcd",
            b"ibook2epub/1 book=0123abcd",
            b"x" * 65535,
        ],
    )
    def test_anything_but_a_marker_names_no_source(self, tmp_path, comment):
        assert provenance.read_source(self._commented(tmp_path, comment)) is None

    def test_a_later_version_with_more_fields_still_names_its_source(self, tmp_path):
        path = self._commented(tmp_path, b"ibook2epub/2 asset=00ff src=0123abcd")

        assert provenance.read_source(path) == "0123abcd"

    @pytest.mark.parametrize("body", [b"", b"PK\x05\x06", b"not a zip at all"])
    def test_a_file_that_is_no_archive_names_no_source(self, tmp_path, body):
        path = tmp_path / "book.epub"
        path.write_bytes(body)

        assert provenance.read_source(path) is None

    def test_a_missing_file_names_no_source(self, tmp_path):
        assert provenance.read_source(tmp_path / "gone.epub") is None

    def test_a_fifo_is_not_waited_on(self, tmp_path):
        fifo = tmp_path / "book.epub"
        os.mkfifo(fifo)

        assert provenance.read_source(fifo) is None


class TestARefreshKeepsTheMarker:
    HIGHLIGHT: list[dict[str, object]] = [{"id": "U1", "text": "Fear."}]

    def test_the_rebuild_writes_the_books_source(self, tmp_path):
        package, library, target = exported(tmp_path, "a/Dune.epub")
        source = provenance.source_of(package, library)
        before = comment_of(target)

        assert replace_annotations(target, self.HIGHLIGHT, provenance=source)

        assert comment_of(target) == before
        assert not validate_archive(target)

    def test_an_unmarked_archive_gains_the_marker_when_rewritten(self, tmp_path):
        package = make_metadata_package(tmp_path / "lib" / "a", "Dune.epub", title="D")
        target = tmp_path / "out.epub"
        zip_package(package, target)
        source = provenance.source_of(package, tmp_path / "lib")

        replace_annotations(target, self.HIGHLIGHT, provenance=source)

        assert provenance.read_source(target) == source

    def test_a_rebuild_that_names_no_source_keeps_the_comment_it_found(self, tmp_path):
        _, _, target = exported(tmp_path, "a/Dune.epub")
        before = comment_of(target)

        replace_annotations(target, self.HIGHLIGHT)

        assert comment_of(target) == before


class TestThroughARun:
    def test_each_namesake_names_its_own_folder(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        for folder in ("a", "b"):
            make_package(library / folder, "Dune.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-q"])
        run.main(
            ["-s", str(library), "-o", str(output_dir), "-q"]
            + ["--on-collision", "suffix"]
        )

        sources = {
            path.name: provenance.read_source(path)
            for path in output_dir.glob("*.epub")
        }
        assert sources == {
            "Dune.epub": disambiguator("a/dune.epub"),
            "Dune (2).epub": disambiguator("b/dune.epub"),
        }

    def test_a_book_copied_through_is_left_byte_for_byte(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        library.mkdir()
        made = make_metadata_package(tmp_path / "made", "Plain.epub", title="P")
        zip_package(made, library / "Plain.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-q"])

        copied = output_dir / "Plain.epub"
        assert copied.read_bytes() == (library / "Plain.epub").read_bytes()
        assert provenance.read_source(copied) is None

    def test_a_refresh_through_the_cli_keeps_every_marker(self, tmp_path, monkeypatch):
        library = tmp_path / "lib"
        output_dir = tmp_path / "out"
        package = make_package(library / "a", "Old.epub")
        make_databases(
            tmp_path / "container",
            rows=[highlight(uuid="U0", asset="A0")],
            books=[library_row(asset="A0", path=str(package))],
        )
        monkeypatch.setattr(
            annotating,
            "collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        argv = ["-s", str(library), "-o", str(output_dir), "-q"]
        assert run.main(argv) == exits.SUCCESS
        book = output_dir / "Old.epub"
        before = provenance.read_source(book)

        assert run.main([*argv, "-ae", "-ar"]) == exits.SUCCESS

        with ZipFile(book) as opened:
            assert annotations.EMBEDDED_PATH in opened.namelist()
        assert before == disambiguator("a/old.epub")
        assert provenance.read_source(book) == before
