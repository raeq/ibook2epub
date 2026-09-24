"""
Tests for a detached export the merge cannot account for every entry of.

The merge keys an entry on its id. An entry with none, one whose id was not a
string, and the second of two entries sharing an id were dropped without a
word, and the reader's own top-level keys went with the envelope, which is
rebuilt on every write. The file is the one destination that keeps what Books
has forgotten, so it is refused rather than rewritten less what it held.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods
# pylint: disable=protected-access

import json
from pathlib import Path
from typing import Any

import pytest

from epubconvert import __version__
from epubconvert.export import detached
from epubconvert.utils import app_logger, exits

FRESH = [{"id": "U1", "text": "hi", "created": "2020", "modified": "2020"}]


def _document(entries: list[Any], **extra: Any) -> dict[str, Any]:
    return {
        "$schema": "annotations.schema.json",
        "generator": {"name": "ibook2epub", "version": __version__},
        "annotations": entries,
        **extra,
    }


REFUSED = {
    "an entry with no id": _document([{"text": "typed from a paper book"}]),
    "an entry with an empty id": _document([{"id": "", "text": "x"}]),
    "an entry whose id is a number": _document([{"id": 7, "text": "x"}]),
    "an entry that is not an object": _document(["just a string"]),
    "two entries with one id": _document(
        [{"id": "DUP", "text": "first"}, {"id": "DUP", "text": "second, edited"}]
    ),
    "a top-level key of the reader's own": _document([], myNotes="mine"),
}


@pytest.mark.parametrize("document", REFUSED.values(), ids=REFUSED.keys())
class TestAFileTheMergeCannotKeyIsRefused:
    def test_it_is_left_exactly_as_it_was(
        self, tmp_path: Path, document: dict[str, Any]
    ):
        target = tmp_path / "highlights.json"
        target.write_text(json.dumps(document), encoding="utf-8")
        before = target.read_bytes()

        code = detached._write_detached(FRESH, str(target))

        assert code == exits.NO_OUTPUT
        assert target.read_bytes() == before

    def test_it_is_named_with_the_advice_every_refusal_gives(
        self,
        tmp_path: Path,
        document: dict[str, Any],
        capsys: pytest.CaptureFixture[str],
    ):
        app_logger.configure(verbosity=0)
        target = tmp_path / "highlights.json"
        target.write_text(json.dumps(document), encoding="utf-8")

        detached._write_detached(FRESH, str(target))

        reported = capsys.readouterr().err
        assert "highlights.json" in reported
        assert "move it aside" in reported


class TestAFileItCanKeyIsStillMergedInto:
    def test_every_key_the_schema_allows_is_accepted(self, tmp_path: Path):
        target = tmp_path / "highlights.json"
        kept = {"id": "OLD", "text": "gone from Books", "created": "2019"}
        document = _document([kept], generated="2020-01-01T00:00:00Z")
        target.write_text(json.dumps(document), encoding="utf-8")

        code = detached._write_detached(FRESH, str(target))

        written = json.loads(target.read_text(encoding="utf-8"))
        assert code == exits.SUCCESS
        assert sorted(item["id"] for item in written["annotations"]) == ["OLD", "U1"]
