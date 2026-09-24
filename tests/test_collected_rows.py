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

from epubconvert.collect import annotations
from tests.test_annotations import highlight, make_databases


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
