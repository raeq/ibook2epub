"""
Tests for what one hostile database row costs, and what a surviving row says.

Both exports read Apple's databases a row at a time so that one row Apple
filled with something odd costs one annotation or one book, never the run.
Each defect here is a way a row got past that guard -- an exception the guard
did not name -- or got through it and then broke the shipped schema, which is
the other half of the contract: what comes out must be what the schema says.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import logging
from pathlib import Path
from typing import Any

import pytest

from epubconvert.collect import annotations, coredata, library
from epubconvert.collect import package as package_reader
from epubconvert.utils.opf import Package
from tests.test_annotations import highlight, library_row, make_databases

#: A ZPATH holding a NUL. Nothing on disk can be called that, and Path.resolve
#: raises ValueError for it rather than OSError.
NUL_PATH = "/Users/someone/Books/a\x00b/Leviathan Wakes.epub"


def _collected(tmp_path: Path, rows: list[tuple[object, ...]]) -> list[dict[str, Any]]:
    make_databases(tmp_path, rows=rows)
    return annotations.collect(tmp_path)


class TestAnInfiniteNumberCostsOneRowAtMost:
    def test_an_infinite_style_keeps_every_highlight(self, tmp_path):
        # SQLite stores 9e999 as a real infinity, and int() of one raises
        # OverflowError: neither TypeError nor ValueError, so it escaped the
        # per-row guard and one row took the whole annotation export down.
        found = _collected(
            tmp_path,
            [highlight(uuid="GOOD"), highlight(uuid="BAD", style=float("inf"))],
        )

        assert sorted(item["id"] for item in found) == ["BAD", "GOOD"]
        assert "style" not in next(item for item in found if item["id"] == "BAD")

    def test_any_arithmetic_error_costs_only_its_own_row(self, tmp_path, monkeypatch):
        # The backstop: the next numeric column to overflow is caught by the
        # guard even if the conversion that meets it forgets to be careful.
        def overflowing(value: object) -> str | None:
            if value == 1.0:
                raise OverflowError("date value out of range")
            return coredata.moment(value)

        monkeypatch.setattr(annotations, "moment", overflowing)

        found = _collected(
            tmp_path, [highlight(uuid="GOOD"), highlight(uuid="BAD", created=1.0)]
        )

        assert [item["id"] for item in found] == ["GOOD"]


class TestANulInThePathCostsOnlyTheBook:
    def test_the_highlight_survives_without_its_package(self, tmp_path):
        # Path.resolve raises ValueError("embedded null byte"). Only
        # RuntimeError was translated into the ValidationError that
        # read_package_once takes for unreadable metadata, so the row was
        # dropped: a bad ZPATH cost every highlight in the book.
        make_databases(tmp_path, books=[library_row(path=NUL_PATH)])

        found = annotations.collect(tmp_path)

        assert len(found) == 1
        assert found[0]["book"]["title"] == "Leviathan Wakes"

    def test_the_catalogue_keeps_the_book(self, tmp_path):
        make_databases(tmp_path, books=[library_row(path=NUL_PATH)])

        found = library.collect(tmp_path)

        assert [entry["title"] for entry in found] == ["Leviathan Wakes"]

    def test_reading_such_a_package_is_a_validation_error(self):
        with pytest.raises(package_reader.ValidationError, match="cannot be resolved"):
            package_reader.read_package_dir(Path(NUL_PATH))

    def test_a_value_error_from_a_package_is_unreadable_metadata(self, monkeypatch):
        # The backstop: whatever else a path can raise ValueError for is still
        # the one book's metadata, never its row.
        def refusing(_package: Path) -> Package:
            raise ValueError("embedded null byte")

        monkeypatch.setattr(library, "read_package_dir", refusing)
        parsed: dict[Path, Package | None] = {}

        assert library.read_package_once(Path("Dune.epub"), parsed) is None
        assert parsed == {Path("Dune.epub"): None}


class TestWhatIsCollectedObeysTheSchema:
    def test_an_empty_id_is_refused(self, tmp_path, caplog):
        # The schema's id has minLength 1, and an empty one is also what a
        # rerun cannot match an entry on, so it was merged as new every time.
        with caplog.at_level(logging.WARNING):
            found = _collected(tmp_path, [highlight(uuid=""), highlight(uuid="U2")])

        assert [item["id"] for item in found] == ["U2"]
        assert "annotation id is empty" in caplog.text

    def test_a_negative_style_is_left_out(self, tmp_path):
        # The schema holds style to a minimum of 0.
        found = _collected(tmp_path, [highlight(style=-1)])

        assert "style" not in found[0]

    def test_a_style_of_zero_is_kept(self, tmp_path):
        found = _collected(tmp_path, [highlight(style=0)])

        assert found[0]["style"] == 0

    def test_a_cfi_after_a_book_address_is_stored_bare(self, tmp_path):
        # Apple can store "<address>#epubcfi(...)". CFI_FORM reads it, but the
        # schema's cfi is ^epubcfi\( and the address is Apple's name for its
        # own copy of the book, which the "book" object already describes.
        found = _collected(
            tmp_path, [highlight(location="1234.ibooks#epubcfi(/6/4[ch1]!/4/2:0)")]
        )

        assert found[0]["cfi"] == "epubcfi(/6/4[ch1]!/4/2:0)"

    @pytest.mark.parametrize("location", ["chapter-15", "x epubcfi(/6[a]!)"])
    def test_what_is_not_a_cfi_is_left_out(self, tmp_path, location):
        found = _collected(tmp_path, [highlight(location=location)])

        assert "cfi" not in found[0]
        assert found[0]["text"] == "Summary roadside justice"

    def test_the_whole_collection_passes_the_shipped_schema(self, tmp_path):
        found = _collected(
            tmp_path,
            [
                highlight(uuid="", text="empty id"),
                highlight(uuid="U2", text="negative style", style=-1),
                highlight(
                    uuid="U3",
                    text="address form",
                    location="book.epub#epubcfi(/6/4[ch1]!/4/2:0)",
                ),
                highlight(uuid="U4", text="not a cfi", location="chapter-15"),
                highlight(uuid="U5", text="infinite", style=float("inf")),
            ],
        )

        assert annotations.schema_problems(annotations.build_document(found)) == []
        assert sorted(item["id"] for item in found) == ["U2", "U3", "U4", "U5"]
