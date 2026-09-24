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
# pylint: disable=too-few-public-methods

from pathlib import Path
from typing import Any

import pytest

from epubconvert.collect import annotations, library, validate
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
        moment = annotations.moment

        def overflowing(value: object) -> str | None:
            if value == 1.0:
                raise OverflowError("date value out of range")
            return moment(value)

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
        with pytest.raises(validate.ValidationError, match="cannot be resolved"):
            validate.read_package_dir(Path(NUL_PATH))

    def test_a_value_error_from_a_package_is_unreadable_metadata(self, monkeypatch):
        # The backstop: whatever else a path can raise ValueError for is still
        # the one book's metadata, never its row.
        def refusing(_package: Path) -> Package:
            raise ValueError("embedded null byte")

        monkeypatch.setattr(library, "read_package_dir", refusing)
        parsed: dict[Path, Package | None] = {}

        assert library.read_package_once(Path("Dune.epub"), parsed) is None
        assert parsed == {Path("Dune.epub"): None}
