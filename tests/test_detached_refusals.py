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
from epubconvert.collect.annotations import SCHEMA_PATH
from epubconvert.export import detached
from epubconvert.utils import app_logger, exits, schema

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
    "a key of the reader's own on an entry": _document(
        [{"id": "X", "text": "x", "myTags": ["favourite"]}]
    ),
    "a key of the reader's own on an entry's book": _document(
        [{"id": "X", "text": "x", "book": {"title": "Dune", "shelfNote": "lent"}}]
    ),
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


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps(_document([], myNotes="mine")),
        '{"annotations": [{"id": "\\ud83d"}]}',
    ],
    ids=["unreadable", "not an export", "unmergeable", "a lone surrogate"],
)
def test_a_refused_files_name_is_escaped(
    tmp_path: Path, content: str, capsys: pytest.CaptureFixture[str]
):
    # The name is the reader's, and a control character in it reached the
    # terminal raw.
    app_logger.configure(verbosity=0)
    target = tmp_path / "h\x1b[2K.json"
    target.write_text(content, encoding="utf-8")

    assert detached._write_detached(FRESH, str(target)) == exits.NO_OUTPUT

    reported = capsys.readouterr().err
    assert "\x1b" not in reported
    assert "h\\x1b[2K.json is already there" in reported


class TestTheReadersOwnKeyIsNamed:
    @pytest.mark.parametrize(
        ("entry", "named"),
        [
            ({"id": "X", "text": "x", "myTags": []}, "'myTags'"),
            ({"id": "X", "text": "x", "book": {"shelfNote": "lent"}}, "'shelfNote'"),
        ],
    )
    def test_with_the_annotation_it_is_on(self, entry: dict[str, Any], named: str):
        refused = detached._unmergeable(_document([FRESH[0], entry]))

        assert refused is not None
        assert named in refused
        assert "annotation 2" in refused


class TestAFileItCanKeyIsStillMergedInto:
    def test_every_key_the_schema_allows_on_an_entry_and_its_book(self):
        # Read from the schema, so a key added there is accepted here too.
        definitions = schema.load(SCHEMA_PATH)["$defs"]
        book = dict.fromkeys(definitions["book"]["properties"], "v")
        entry = dict.fromkeys(definitions["annotation"]["properties"], "v")

        assert detached._unmergeable(_document([{**entry, "book": book}])) is None

    def test_a_book_that_is_not_an_object_is_left_to_the_merge(self):
        assert detached._unmergeable(_document([{"id": "X", "book": "Dune"}])) is None

    def test_every_key_the_schema_allows_is_accepted(self, tmp_path: Path):
        target = tmp_path / "highlights.json"
        kept = {"id": "OLD", "text": "gone from Books", "created": "2019"}
        document = _document([kept], generated="2020-01-01T00:00:00Z")
        target.write_text(json.dumps(document), encoding="utf-8")

        code = detached._write_detached(FRESH, str(target))

        written = json.loads(target.read_text(encoding="utf-8"))
        assert code == exits.SUCCESS
        assert sorted(item["id"] for item in written["annotations"]) == ["OLD", "U1"]
