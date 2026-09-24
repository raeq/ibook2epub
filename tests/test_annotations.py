"""
Tests for taking annotations out of Apple Books.

Highlights live in a Core Data SQLite database inside Apple's container, keyed
by an asset id that joins to the library database, which is what knows a book's
title and where its package directory is. Neither is documented, so every
column this reads is pinned by a test built from a database this file creates.

The output is shaped after the direction of the W3C EPUB Annotations work
rather than its current draft. That work is converging on URL text fragments
as the locator and has explicitly ruled out EPUB CFI -- "which will rule out
epubcfi (which, b.t.w., is bound to XHTML...)" -- so the CFI Apple records is
kept in its own field rather than presented as the locator. It is the only
thing that still points at the right place when the highlighted text is
ambiguous or must not be reproduced.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import contextlib
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from epubconvert.collect import annotations, coredata

#: Core Data counts seconds from 2001-01-01, not from the Unix epoch.
APPLE_EPOCH_OFFSET = 978307200
MADE_AT = 567470668.170355  # 2018-12-25T22:44:28Z


@contextlib.contextmanager
def open_for_writing(path: Path) -> Iterator[sqlite3.Connection]:
    """
    Open a database to build it, commit, and close it.

    Closed explicitly: ``sqlite3.connect`` as a context manager commits but
    never closes, and the connections it leaked surfaced as ResourceWarnings
    against whichever test happened to be running when they were collected.
    """
    connection = sqlite3.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def make_databases(
    root: Path,
    rows: list[tuple[object, ...]] | None = None,
    books: list[tuple[object, ...]] | None = None,
    collections: dict[str, list[str]] | None = None,
) -> Path:
    """
    Build a pair of databases with the columns Apple actually uses.

    :param collections: Collection name to the asset ids in it. An id that
        names no book is allowed, because Apple's table holds 749 of those.
    """
    root.mkdir(parents=True, exist_ok=True)
    annotation_db = root / "AEAnnotation" / "AEAnnotation_v1_local.sqlite"
    library_db = root / "BKLibrary" / "BKLibrary-1-1.sqlite"
    annotation_db.parent.mkdir(parents=True, exist_ok=True)
    library_db.parent.mkdir(parents=True, exist_ok=True)

    annotation_db.unlink(missing_ok=True)
    library_db.unlink(missing_ok=True)

    with open_for_writing(annotation_db) as connection:
        connection.execute(
            "CREATE TABLE ZAEANNOTATION ("
            "ZANNOTATIONUUID VARCHAR, ZANNOTATIONASSETID VARCHAR,"
            "ZANNOTATIONTYPE INTEGER,"
            "ZANNOTATIONSTYLE INTEGER, ZANNOTATIONDELETED INTEGER,"
            "ZANNOTATIONSELECTEDTEXT VARCHAR, ZANNOTATIONNOTE VARCHAR,"
            "ZANNOTATIONLOCATION VARCHAR, ZFUTUREPROOFING5 VARCHAR,"
            "ZANNOTATIONCREATIONDATE TIMESTAMP,"
            "ZANNOTATIONMODIFICATIONDATE TIMESTAMP)"
        )
        for row in rows if rows is not None else [highlight()]:
            connection.execute(
                "INSERT INTO ZAEANNOTATION VALUES (?,?,?,?,?,?,?,?,?,?,?)", row
            )

    with open_for_writing(library_db) as connection:
        connection.execute(
            "CREATE TABLE ZBKLIBRARYASSET ("
            "ZASSETID VARCHAR, ZTITLE VARCHAR, ZAUTHOR VARCHAR,"
            "ZLANGUAGE VARCHAR, ZYEAR INTEGER, ZPATH VARCHAR,"
            "ZPURCHASEDATE TIMESTAMP, ZLASTOPENDATE TIMESTAMP,"
            "ZDATEFINISHED TIMESTAMP, ZISFINISHED INTEGER,"
            "ZREADINGPROGRESS FLOAT, ZRATING INTEGER, ZPAGECOUNT INTEGER)"
        )
        for book in books if books is not None else [library_row()]:
            connection.execute(
                "INSERT INTO ZBKLIBRARYASSET VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                book,
            )
        connection.execute(
            "CREATE TABLE ZBKCOLLECTION (Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR)"
        )
        connection.execute(
            "CREATE TABLE ZBKCOLLECTIONMEMBER (Z_PK INTEGER PRIMARY KEY,"
            " ZASSET INTEGER, ZCOLLECTION INTEGER, ZASSETID VARCHAR)"
        )
        for key, (title, members) in enumerate((collections or {}).items(), 1):
            connection.execute("INSERT INTO ZBKCOLLECTION VALUES (?, ?)", (key, title))
            for asset in members:
                connection.execute(
                    "INSERT INTO ZBKCOLLECTIONMEMBER (ZASSET, ZCOLLECTION, ZASSETID)"
                    " VALUES (?, ?, ?)",
                    (None, key, asset),
                )
    return root


def highlight(**overrides: object) -> tuple[object, ...]:
    """One highlight row, with Apple's column order."""
    row = {
        "uuid": "U1",
        "asset": "ASSET1",
        "type": 2,
        "style": 3,
        "deleted": 0,
        "text": "Summary roadside justice",
        "note": "",
        "location": "epubcfi(/6/46[ch15.xhtml]!/4,/80/2/1:25,/82/2/1:25)",
        "chapter": "Chapter Fifteen: Holden",
        "created": MADE_AT,
        "modified": MADE_AT,
    }
    row.update(overrides)
    return tuple(row.values())


def library_row(**overrides: object) -> tuple[object, ...]:
    """One library row."""
    row = {
        "asset": "ASSET1",
        "title": "Leviathan Wakes",
        "author": "James S. A. Corey",
        "language": "en",
        "year": 2011,
        "path": "/Users/someone/Books/Leviathan Wakes.epub",
        "added": None,
        "opened": None,
        "finished": None,
        "is_finished": None,
        "progress": None,
        "rating": None,
        "pages": None,
    }
    row.update(overrides)
    return tuple(row.values())


class TestWhatIsRead:
    """Every column here is undocumented, so every column is pinned."""

    def test_a_highlight_carries_its_text_and_note(self, tmp_path):
        make_databases(tmp_path, rows=[highlight(note="The point of the whole scene")])

        found = annotations.collect(tmp_path)

        assert len(found) == 1
        assert found[0]["text"] == "Summary roadside justice"
        assert found[0]["note"] == "The point of the whole scene"

    def test_an_empty_note_is_left_out_rather_than_sent_as_empty(self, tmp_path):
        make_databases(tmp_path, rows=[highlight(note="")])

        assert "note" not in annotations.collect(tmp_path)[0]

    def test_the_book_travels_with_the_annotation(self, tmp_path):
        # The requirement the W3C work opens with: useful even once you have
        # lost access to the publication.
        make_databases(tmp_path)

        book = annotations.collect(tmp_path)[0]["book"]

        assert book["title"] == "Leviathan Wakes"
        assert book["author"] == "James S. A. Corey"
        assert book["year"] == 2011
        assert book["assetId"] == "ASSET1"

    def test_the_source_is_a_name_not_a_home_directory(self, tmp_path):
        # ZPATH holds an absolute path through the reader's home directory.
        make_databases(tmp_path)

        book = annotations.collect(tmp_path)[0]["book"]

        assert book["source"] == "Leviathan Wakes.epub"
        assert "/Users/" not in json.dumps(book)

    def test_the_chapter_heading_is_kept(self, tmp_path):
        make_databases(tmp_path)

        assert annotations.collect(tmp_path)[0]["chapter"] == (
            "Chapter Fifteen: Holden"
        )

    def test_dates_are_utc_with_a_trailing_z(self, tmp_path):
        make_databases(tmp_path)

        found = annotations.collect(tmp_path)[0]

        assert found["created"] == "2018-12-25T22:44:28Z"
        assert found["modified"] == "2018-12-25T22:44:28Z"


class TestWhatIsSkipped:
    def test_a_row_with_no_text_is_not_an_annotation(self, tmp_path):
        # 268 of 278 rows in a real library are these: one per book, holding a
        # reading position, with empty text and empty location.
        make_databases(
            tmp_path, rows=[highlight(type=3, text="", location="", chapter="")]
        )

        assert annotations.collect(tmp_path) == []

    def test_a_deleted_annotation_is_not_exported(self, tmp_path):
        make_databases(tmp_path, rows=[highlight(deleted=1)])

        assert annotations.collect(tmp_path) == []

    def test_an_annotation_for_an_unknown_book_is_still_exported(self, tmp_path):
        # The library database can lag; losing the highlight would be worse
        # than losing the title.
        make_databases(tmp_path, rows=[highlight(asset="GONE")])

        found = annotations.collect(tmp_path)

        assert len(found) == 1
        assert found[0]["book"]["title"] == "GONE"

    @pytest.mark.parametrize("column", ["note", "chapter"])
    def test_a_blob_beside_the_highlight_costs_that_field_not_the_export(
        self, tmp_path, column
    ):
        # Only the selected text was type-checked. A BLOB in the note or the
        # chapter passed through to json.dumps, which raised outside every
        # per-row guard and took the whole export down.
        make_databases(
            tmp_path, rows=[highlight(**{column: b"\xff\xfe"}), highlight(uuid="U2")]
        )

        found = annotations.collect(tmp_path)

        json.dumps(annotations.build_document(found))
        assert sorted(item["id"] for item in found) == ["U1", "U2"]
        assert all(column not in item for item in found if item["id"] == "U1")

    @pytest.mark.parametrize("column", ["uuid", "asset"])
    def test_a_blob_where_an_id_belongs_costs_one_annotation(
        self, tmp_path, column, caplog
    ):
        # An annotation without a usable id, or a book to belong to, cannot be
        # matched on a rerun: the row is dropped, and only that row.
        bad: dict[str, object] = {"uuid": "BAD", column: b"\x00"}
        make_databases(tmp_path, rows=[highlight(**bad), highlight()])

        found = annotations.collect(tmp_path)

        json.dumps(annotations.build_document(found))
        assert [item["id"] for item in found] == ["U1"]
        assert "not text" in caplog.text


class TestTheLocator:
    """
    A URL text fragment, because that is what the W3C work is converging on.
    The CFI is kept beside it rather than instead of it.
    """

    def test_the_locator_is_a_text_fragment_of_the_highlight(self, tmp_path):
        make_databases(tmp_path, rows=[highlight(text="Summary roadside justice")])

        found = annotations.collect(tmp_path)[0]

        assert found["locator"] == ":~:text=Summary%20roadside%20justice"

    @pytest.mark.parametrize(
        ("text", "locator"),
        [
            ("-40 degrees", ":~:text=%2D40%20degrees"),
            ("well-known", ":~:text=well%2Dknown"),
            ("a, b & c", ":~:text=a%2C%20b%20%26%20c"),
        ],
    )
    def test_the_characters_that_are_fragment_syntax_are_encoded(self, text, locator):
        # A text directive reads a leading "prefix-," and a trailing ",-suffix"
        # by their dash, so "-40 degrees" left bare was a suffix, not the
        # highlight. The WICG syntax requires "-", "&" and "," percent-encoded.
        assert annotations.text_fragment(text) == locator

    def test_a_dash_is_encoded_in_both_ends_of_a_long_highlight(self):
        text = "-" + " ".join(["word"] * 30) + " end-"
        start, _, end = annotations.text_fragment(text).partition(",")

        assert "-" not in start.removeprefix(":~:text=")
        assert "-" not in end

    def test_the_original_cfi_is_kept_verbatim(self, tmp_path):
        make_databases(tmp_path)

        assert annotations.collect(tmp_path)[0]["cfi"] == (
            "epubcfi(/6/46[ch15.xhtml]!/4,/80/2/1:25,/82/2/1:25)"
        )

    def test_an_assertion_is_not_passed_off_as_a_path(self, tmp_path):
        # Without the book there is nothing to resolve the id against, and an
        # id is not a path -- so the field is left out rather than filled with
        # something untrue. The assertion is still in the cfi.
        make_databases(tmp_path)

        found = annotations.collect(tmp_path)[0]

        assert "href" not in found
        assert "[ch15.xhtml]" in found["cfi"]

    def test_a_cfi_naming_no_document_leaves_the_href_out(self, tmp_path):
        make_databases(tmp_path, rows=[highlight(location="epubcfi(/6/46!/4/2)")])

        found = annotations.collect(tmp_path)[0]

        assert "href" not in found
        assert found["cfi"] == "epubcfi(/6/46!/4/2)"

    def test_a_missing_cfi_leaves_both_the_cfi_and_href_out(self, tmp_path):
        make_databases(tmp_path, rows=[highlight(location="")])

        found = annotations.collect(tmp_path)[0]

        assert "cfi" not in found
        assert "href" not in found
        assert found["locator"].startswith(":~:text=")


class TestTheDocument:
    def test_it_names_what_wrote_it_and_when(self, tmp_path):
        make_databases(tmp_path)

        document = annotations.build_document(annotations.collect(tmp_path))

        assert document["generator"]["name"] == "ibook2epub"
        assert document["generator"]["version"] == __import__("epubconvert").__version__
        assert document["generated"].endswith("Z")
        assert len(document["generated"]) == 20

    def test_annotations_are_an_array(self, tmp_path):
        make_databases(tmp_path)

        document = annotations.build_document(annotations.collect(tmp_path))

        assert isinstance(document["annotations"], list)

    def test_an_empty_library_still_produces_a_valid_document(self, tmp_path):
        make_databases(tmp_path, rows=[])

        document = annotations.build_document(annotations.collect(tmp_path))

        assert document["annotations"] == []
        assert "generated" in document

    def test_it_validates_against_the_shipped_schema(self, tmp_path):
        make_databases(
            tmp_path,
            rows=[highlight(), highlight(note="a note", text="another")],
        )

        document = annotations.build_document(annotations.collect(tmp_path))

        problems = annotations.schema_problems(document)
        assert problems == [], problems


class TestOrdering:
    def test_annotations_are_grouped_by_book_and_ordered_within_it(self, tmp_path):
        make_databases(
            tmp_path,
            rows=[
                highlight(asset="B", text="second book", created=MADE_AT + 10),
                highlight(asset="A", text="later", created=MADE_AT + 5),
                highlight(asset="A", text="earlier", created=MADE_AT),
            ],
            books=[
                library_row(asset="A", title="Alpha"),
                library_row(asset="B", title="Beta"),
            ],
        )

        found = annotations.collect(tmp_path)

        assert [item["text"] for item in found] == [
            "earlier",
            "later",
            "second book",
        ]


class TestTheDatabasesAreNotDisturbed:
    def test_reading_does_not_write_to_them(self, tmp_path):
        make_databases(tmp_path)
        database = next(tmp_path.rglob("AEAnnotation*.sqlite"))
        before = database.stat().st_mtime_ns

        annotations.collect(tmp_path)

        assert database.stat().st_mtime_ns == before

    def test_a_missing_container_is_reported_not_raised(self, tmp_path):
        with pytest.raises(coredata.ContainerUnavailableError, match="not"):
            annotations.collect(tmp_path / "absent")


class TestRerunningUpdatesOnlyWhatChanged:
    """
    An export is a file the reader keeps and adds to, so a rerun must not throw
    away what is already in it. Apple gives every annotation a UUID that is
    stable across syncs, which is what an entry is matched on.

    The generator version decides the rest. The locator this writes is ahead of
    a W3C draft that is still moving; when the algorithm changes, entries
    written by an older version are wrong, not merely old. So a file written by
    a different version is regenerated rather than trusted.
    """

    def test_an_unchanged_annotation_is_left_alone(self, tmp_path):
        make_databases(tmp_path)
        first = annotations.build_document(annotations.collect(tmp_path))

        merged, tally = annotations.merge(first, annotations.collect(tmp_path))

        assert tally == {"added": 0, "updated": 0, "unchanged": 1, "kept": 0}
        assert merged == first["annotations"]

    def test_an_entry_whose_created_was_invented_is_regenerated(self, tmp_path):
        # An unusable creation date used to be stamped with the moment of
        # export. Matched only on the modification date, such an entry read as
        # unchanged and carried the invented date forward for ever.
        make_databases(tmp_path, rows=[highlight(created="soon")])
        stale = annotations.build_document(annotations.collect(tmp_path))
        stale["annotations"][0]["created"] = "2019-01-01T00:00:00Z"

        merged, tally = annotations.merge(stale, annotations.collect(tmp_path))

        assert tally["updated"] == 1
        assert "created" not in merged[0]

    def test_a_new_annotation_is_added(self, tmp_path):
        make_databases(tmp_path)
        first = annotations.build_document(annotations.collect(tmp_path))
        make_databases(
            tmp_path, rows=[highlight(), highlight(uuid="U2", text="a second one")]
        )

        merged, tally = annotations.merge(first, annotations.collect(tmp_path))

        assert tally["added"] == 1
        assert tally["unchanged"] == 1
        assert len(merged) == 2

    def test_an_edited_annotation_is_regenerated(self, tmp_path):
        make_databases(tmp_path)
        first = annotations.build_document(annotations.collect(tmp_path))
        make_databases(
            tmp_path,
            rows=[highlight(note="added later", modified=MADE_AT + 60)],
        )

        merged, tally = annotations.merge(first, annotations.collect(tmp_path))

        assert tally["updated"] == 1
        assert merged[0]["note"] == "added later"

    def test_an_annotation_deleted_in_books_is_still_kept(self, tmp_path):
        # The whole point is taking them with you. Losing one because Apple
        # lost it would defeat that.
        make_databases(tmp_path)
        first = annotations.build_document(annotations.collect(tmp_path))
        make_databases(tmp_path, rows=[])

        merged, tally = annotations.merge(first, annotations.collect(tmp_path))

        assert tally == {"added": 0, "updated": 0, "unchanged": 0, "kept": 1}
        assert len(merged) == 1

    def test_a_file_from_another_version_is_regenerated(self, tmp_path):
        # Not merely old -- wrong. The locator algorithm is ahead of a moving
        # draft, so an entry written by a different version may not say what
        # this version would say.
        make_databases(tmp_path)
        stale = annotations.build_document(annotations.collect(tmp_path))
        stale["generator"]["version"] = "0.0.1"
        stale["annotations"][0]["locator"] = ":~:text=whatever-the-old-rule-said"

        merged, tally = annotations.merge(stale, annotations.collect(tmp_path))

        assert tally["updated"] == 1
        assert merged[0]["locator"] == ":~:text=Summary%20roadside%20justice"

    def test_an_orphan_survives_a_version_change(self, tmp_path):
        # It cannot be regenerated -- Apple no longer has it -- so it is kept
        # as it stands rather than dropped for being stale.
        make_databases(tmp_path)
        stale = annotations.build_document(annotations.collect(tmp_path))
        stale["generator"]["version"] = "0.0.1"
        make_databases(tmp_path, rows=[])

        merged, tally = annotations.merge(stale, annotations.collect(tmp_path))

        assert tally["kept"] == 1
        assert len(merged) == 1

    def test_merging_into_nothing_is_a_first_export(self, tmp_path):
        make_databases(tmp_path)

        merged, tally = annotations.merge(None, annotations.collect(tmp_path))

        assert tally == {"added": 1, "updated": 0, "unchanged": 0, "kept": 0}
        assert len(merged) == 1

    def test_every_annotation_carries_its_id(self, tmp_path):
        make_databases(tmp_path)

        assert annotations.collect(tmp_path)[0]["id"] == "U1"

    def test_the_merged_file_still_matches_the_schema(self, tmp_path):
        make_databases(tmp_path)
        first = annotations.build_document(annotations.collect(tmp_path))
        make_databases(tmp_path, rows=[highlight(uuid="U2", text="second")])

        merged, _tally = annotations.merge(first, annotations.collect(tmp_path))

        problems = annotations.schema_problems(annotations.build_document(merged))
        assert problems == [], problems
