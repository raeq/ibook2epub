"""
Tests for where the bytes of an archive sit, which the OCF specification fixes.

A reader identifies an epub by its first bytes: the ``mimetype`` member's local
header at offset 0, its name, and ``application/epub+zip`` at offset 38. The
central directory is an index written at the end, and its order is whatever
the writer chose, so it says nothing about what comes first in the file.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from epubconvert.collect import validate
from epubconvert.export.archive import zip_package
from tests.conftest import make_metadata_package
from tests.test_validate import MEMBERS, write_epub


class TestMimetypeIsPhysicallyFirst:
    def test_an_index_listing_mimetype_first_does_not_make_it_first(
        self, tmp_path: Path
    ):
        # _check_mimetype asked namelist(), which is central-directory order.
        # An archive whose index lists mimetype first while its bytes come
        # after another member's passed, and a reader sniffing offset 0 found
        # something else there.
        path = tmp_path / "Reindexed.epub"
        with ZipFile(path, "w") as archive:
            for name, body in MEMBERS.items():
                archive.writestr(name, body)
            archive.writestr(
                ZipInfo("mimetype"), "application/epub+zip", compress_type=ZIP_STORED
            )
            archive.filelist.sort(key=lambda info: info.filename != "mimetype")
        with ZipFile(path) as archive:
            assert archive.namelist()[0] == "mimetype"

        problems = validate.validate_archive(path)

        assert any("mimetype" in problem and "first" in problem for problem in problems)

    def test_bytes_ahead_of_the_archive_move_mimetype_off_the_start(
        self, tmp_path: Path
    ):
        # zipfile reads an archive with a stub prepended, and reports offsets
        # from the start of the file, so the stub is what a reader sees first.
        good = write_epub(tmp_path / "Good.epub")
        path = tmp_path / "Stubbed.epub"
        path.write_bytes(b"#!/bin/sh\n" + good.read_bytes())

        problems = validate.validate_archive(path)

        assert any("mimetype" in problem and "first" in problem for problem in problems)

    def test_mimetype_stored_first_but_indexed_later_is_first(self, tmp_path: Path):
        # The other half of the same mistake: the check asked the index which
        # member came first, so a sound archive whose writer happened to list
        # mimetype later in its central directory was reported damaged.
        path = tmp_path / "IndexedLater.epub"
        with ZipFile(path, "w") as archive:
            archive.writestr(
                ZipInfo("mimetype"), "application/epub+zip", compress_type=ZIP_STORED
            )
            for name, body in MEMBERS.items():
                if name != "mimetype":
                    archive.writestr(name, body)
            archive.filelist.sort(key=lambda info: info.filename == "mimetype")
        with ZipFile(path) as archive:
            assert archive.namelist()[-1] == "mimetype"
            assert archive.getinfo("mimetype").header_offset == 0

        assert validate.validate_archive(path) == []

    def test_an_archive_written_properly_still_passes(self, tmp_path: Path):
        assert validate.validate_archive(write_epub(tmp_path / "Good.epub")) == []

    def test_what_this_tool_writes_still_passes(self, tmp_path: Path):
        package = make_metadata_package(tmp_path / "lib", "Book.epub", title="Book")
        target = tmp_path / "out" / "Book.epub"
        target.parent.mkdir()

        zip_package(package, target)

        with ZipFile(target) as archive:
            assert archive.getinfo("mimetype").header_offset == 0
        assert validate.validate_archive(target) == []
