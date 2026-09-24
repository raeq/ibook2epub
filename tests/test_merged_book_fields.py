"""
Tests for a merged -ao file taking a book's fields as they are now.

A highlight's modification date says nothing about its book: Apple's
library row, or the package, supplies the title, language, year and
declared identifier, and any of them can change while the highlight does
not.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import pytest

from epubconvert import __version__
from epubconvert.collect import annotations
from tests.test_annotation_hardening import _annotation


class TestAMergedFileTakesTheBooksFields:
    @pytest.mark.parametrize(
        ("field", "was", "now"),
        [
            ("title", "Old Title", "New Title"),
            ("language", "en", "fr"),
            ("year", 2011, 2012),
            ("declaredIdentifier", "978-1-78883-568-8", "9781788835688"),
        ],
    )
    def test_a_changed_book_field_counts_as_changed(self, field, was, now):
        # The highlight's modification date says nothing about its book: a
        # book retitled in Books moves no annotation's date, and only a list
        # of fields was compared, so the merged file kept the old title.
        book = {"title": "T", "source": "T.epub", "assetId": "A1"}
        before = _annotation(book={**book, field: was}, modified="2020-01-01T00:00:00Z")
        after = _annotation(book={**book, field: now}, modified="2020-01-01T00:00:00Z")
        existing = {
            "generator": {"name": "ibook2epub", "version": __version__},
            "annotations": [before],
        }

        merged, tally = annotations.merge(existing, [after])

        assert tally["updated"] == 1
        assert merged[0]["book"][field] == now

    def test_a_dropped_book_field_counts_as_changed(self):
        book = {"title": "T", "source": "T.epub"}
        before = _annotation(book={**book, "year": 2011})
        existing = {
            "generator": {"name": "ibook2epub", "version": __version__},
            "annotations": [before],
        }

        merged, tally = annotations.merge(existing, [_annotation(book=book)])

        assert tally["updated"] == 1
        assert "year" not in merged[0]["book"]
