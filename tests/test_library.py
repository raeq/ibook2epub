"""
Tests for taking a library out of Apple Books.

The catalogue lives in ``BKLibrary``, the same database the annotation export
reads a title from. Neither its columns nor its collection tables are
documented, so every one this reads is pinned by a test built from a database
this file creates through the fixture in ``test_annotations``.

Two decisions here were made against a survey of a real 3,620-book library
and are tested as decisions: the exclusive shelf is derived from evidence or
left out, never defaulted to "to-read"; and the collection join runs from the
asset side, so a membership row whose book is gone invents nothing.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import csv
import io
import json
from pathlib import Path

import pytest

from epubconvert.collect import annotations, coredata, library, validate
from epubconvert.export import catalogue
from epubconvert.export.naming import MetadataNaming, PassthroughNaming, StripNaming
from epubconvert.utils.opf import Package
from tests.conftest import make_metadata_package, needs_permissions
from tests.test_annotations import (
    MADE_AT,
    highlight,
    library_row,
    make_databases,
    open_for_writing,
)

#: Three days before the highlight, so the two are told apart in output.
BOUGHT = MADE_AT - 3 * 86400  # 2018-12-22T22:44:28Z


def _csv_rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


class TestWhatIsRead:
    """Every column here is undocumented, so every column is pinned."""

    def test_a_book_carries_its_title_author_and_when_it_arrived(self, tmp_path):
        make_databases(tmp_path, books=[library_row(added=BOUGHT, opened=MADE_AT)])

        book = library.collect(tmp_path)[0]

        assert book["title"] == "Leviathan Wakes"
        assert book["author"] == "James S. A. Corey"
        assert book["assetId"] == "ASSET1"
        assert book["added"] == "2018-12-22T22:44:28Z"
        assert book["lastOpened"] == "2018-12-25T22:44:28Z"

    def test_the_source_is_a_name_not_a_home_directory(self, tmp_path):
        make_databases(tmp_path)

        book = library.collect(tmp_path)[0]

        assert book["source"] == "Leviathan Wakes.epub"
        assert "/Users/" not in json.dumps(book)

    def test_collections_are_the_readers_shelves(self, tmp_path):
        make_databases(
            tmp_path,
            collections={"Space opera": ["ASSET1"], "Favourites": ["ASSET1"]},
        )

        assert library.collect(tmp_path)[0]["collections"] == [
            "Favourites",
            "Space opera",
        ]

    def test_a_membership_for_a_deleted_book_invents_nothing(self, tmp_path):
        # 749 of 14,500 membership rows in a real library point at a book that
        # is in no library row. Joined from the membership side they would
        # each become a title.
        make_databases(tmp_path, collections={"Favourites": ["ASSET1", "GONE"]})

        found = library.collect(tmp_path)

        assert [book["assetId"] for book in found] == ["ASSET1"]

    def test_a_book_in_no_collection_says_so_by_omission(self, tmp_path):
        make_databases(tmp_path, collections={"Favourites": ["OTHER"]})

        assert "collections" not in library.collect(tmp_path)[0]

    def test_a_zero_date_is_absent_not_new_years_day_2001(self, tmp_path):
        make_databases(tmp_path, books=[library_row(added=0, opened=0.0)])

        book = library.collect(tmp_path)[0]

        assert "added" not in book
        assert "lastOpened" not in book

    def test_a_rating_and_a_page_count_are_kept_only_when_set(self, tmp_path):
        # ZRATING is 0 on every row of a surveyed library and ZPAGECOUNT on
        # all but one, so a zero is "unknown" rather than "none" or "zero".
        make_databases(
            tmp_path,
            books=[
                library_row(asset="A", title="Rated", rating=4, pages=320),
                library_row(asset="B", title="Unrated", rating=0, pages=0),
            ],
        )

        found = library.collect(tmp_path)

        assert (found[0]["rating"], found[0]["pages"]) == (4, 320)
        assert "rating" not in found[1]
        assert "pages" not in found[1]

    def test_the_catalogue_is_ordered_by_title(self, tmp_path):
        make_databases(
            tmp_path,
            books=[
                library_row(asset="B", title="beta"),
                library_row(asset="A", title="Alpha"),
                library_row(asset="C", title="Gamma"),
            ],
        )

        assert [book["title"] for book in library.collect(tmp_path)] == [
            "Alpha",
            "beta",
            "Gamma",
        ]

    def test_the_package_supplies_a_title_apple_lacks(self, tmp_path):
        # Apple's row wins and the package fills what it lacks, which every
        # other field already did. The title alone fell straight through to
        # the asset id, so a book was catalogued under a UUID while the same
        # run named its file on the shelf from the very title this ignored.
        package = make_metadata_package(
            tmp_path / "lib", "Leviathan Wakes.epub", title="Leviathan Wakes"
        )
        make_databases(tmp_path, books=[library_row(title=None, path=str(package))])

        assert library.collect(tmp_path)[0]["title"] == "Leviathan Wakes"

    def test_a_junk_package_title_is_no_better_than_none(self, tmp_path):
        # "none" is a title 92 books in a surveyed library declare.
        package = make_metadata_package(
            tmp_path / "lib", "Leviathan Wakes.epub", title="none"
        )
        make_databases(tmp_path, books=[library_row(title=None, path=str(package))])

        assert library.collect(tmp_path)[0]["title"] == "ASSET1"

    def test_a_blob_where_a_title_should_be_costs_the_title_not_the_export(
        self, tmp_path
    ):
        # Apple's columns are untyped. A BLOB title used to reach json.dumps
        # and take the whole JSON export down with a traceback; the CSV wrote
        # its repr. Neither is a title, so neither is published as one.
        make_databases(
            tmp_path,
            books=[library_row(title=b"\x00", author=b"\x01", language=b"\x02")],
        )

        found = library.collect(tmp_path)
        text = catalogue.render(found, "json")

        assert found[0]["title"] == "ASSET1"
        assert "author" not in found[0]
        assert "language" not in found[0]
        assert json.loads(text)["books"][0]["title"] == "ASSET1"

    def test_a_row_with_no_asset_id_is_not_a_book(self, tmp_path):
        make_databases(
            tmp_path,
            books=[library_row(asset=None), library_row(asset=""), library_row()],
        )

        assert len(library.collect(tmp_path)) == 1


class TestTheShelfIsEvidenceOrNothing:
    """
    Running the obvious rule on a real library said its reader had finished
    11 of 3,620 books and intended to read 3,389. "to-read" is an assertion
    about intent, and the database supports it for none of them.
    """

    def test_a_finish_date_means_read(self, tmp_path):
        make_databases(tmp_path, books=[library_row(finished=MADE_AT)])

        book = library.collect(tmp_path)[0]

        assert book["shelf"] == "read"
        assert book["finished"] == "2018-12-25T22:44:28Z"

    def test_the_finished_flag_means_read(self, tmp_path):
        # Null on every row of the surveyed library, kept because another
        # library may set it.
        make_databases(tmp_path, books=[library_row(is_finished=1)])

        assert library.collect(tmp_path)[0]["shelf"] == "read"

    def test_progress_short_of_the_end_means_currently_reading(self, tmp_path):
        make_databases(tmp_path, books=[library_row(progress=0.42)])

        book = library.collect(tmp_path)[0]

        assert book["shelf"] == "currently-reading"
        assert book["progress"] == 0.42

    def test_reaching_the_end_means_read(self, tmp_path):
        make_databases(tmp_path, books=[library_row(progress=1.0)])

        assert library.collect(tmp_path)[0]["shelf"] == "read"

    def test_no_evidence_means_no_shelf(self, tmp_path):
        make_databases(tmp_path, books=[library_row(progress=0.0)])

        book = library.collect(tmp_path)[0]

        assert "shelf" not in book
        assert "progress" not in book

    def test_a_collection_is_never_the_shelf(self, tmp_path):
        # "Want to Read" and "Finished" are Apple's collections, and on the
        # surveyed library the eleven books with a finish date are in neither.
        # They reach the CSV as bookshelves, which is what they are.
        make_databases(tmp_path, collections={"Want to Read": ["ASSET1"]})

        book = library.collect(tmp_path)[0]

        assert "shelf" not in book
        assert book["collections"] == ["Want to Read"]

    @pytest.mark.parametrize(
        ("finished", "flag", "progress", "shelf"),
        [
            (None, None, None, None),
            ("2018-12-25T22:44:28Z", None, None, "read"),
            (None, 1, None, "read"),
            (None, 0, 0.5, "currently-reading"),
            (None, "1", None, "read"),  # untyped: Apple's flag can be a string
            (None, "yes", None, None),  # but not anything that is not a number
            (None, True, None, None),  # and a bool is not a count
            (None, None, 1.0, "read"),
            (None, None, 1.5, "read"),
        ],
    )
    def test_the_rule_itself(self, finished, flag, progress, shelf):
        assert library.shelf_of(finished, flag, progress) == shelf


class TestTheIdentifierComesFromTheBook:
    def _package(self, tmp_path: Path, identifier: str, **book: object) -> Path:
        package = make_metadata_package(
            tmp_path / "lib",
            "Leviathan Wakes.epub",
            title="Leviathan Wakes",
            creator="James S. A. Corey",
            file_as="Corey, James S. A.",
            identifier=identifier,
        )
        make_databases(tmp_path, books=[library_row(path=str(package), **book)])
        return package

    def test_the_isbn_is_read_from_the_package_document(self, tmp_path):
        self._package(tmp_path, "978-1-4493-4036-0")

        book = library.collect(tmp_path)[0]

        assert book["identifier"] == "urn:isbn:9781449340360"
        assert book["declaredIdentifier"] == "978-1-4493-4036-0"

    def test_without_isbns_no_package_document_is_opened(self, tmp_path, monkeypatch):
        # The cost is not shared evenly: the annotation export opens a package
        # per annotated book, five on a real library; this opens one per book,
        # 2,805 there. The escape hatch has to really escape.
        self._package(tmp_path, "urn:isbn:9781449340360")
        monkeypatch.setattr(
            library, "read_package_dir", lambda _: pytest.fail("opened a book")
        )

        book = library.collect(tmp_path, identifiers=False)[0]

        assert "identifier" not in book
        assert book["title"] == "Leviathan Wakes"

    def test_a_pdf_is_never_opened(self, tmp_path, monkeypatch):
        make_databases(tmp_path, books=[library_row(path="/x/Paper.pdf")])
        monkeypatch.setattr(
            library, "read_package_dir", lambda _: pytest.fail("opened a PDF")
        )

        assert library.collect(tmp_path)[0]["source"] == "Paper.pdf"

    def test_a_book_that_cannot_be_read_keeps_its_row(self, tmp_path):
        make_databases(tmp_path, books=[library_row(path="/nowhere/Gone.epub")])

        book = library.collect(tmp_path)[0]

        assert book["title"] == "Leviathan Wakes"
        assert "identifier" not in book

    def test_the_package_document_fills_in_an_author_apple_lacks(self, tmp_path):
        self._package(tmp_path, "urn:uuid:1", author=None)

        assert library.collect(tmp_path)[0]["author"] == "James S. A. Corey"

    def test_apples_author_wins_when_both_say_something(self, tmp_path):
        self._package(tmp_path, "urn:uuid:1", author="Corey")

        assert library.collect(tmp_path)[0]["author"] == "Corey"

    def test_the_sort_name_comes_from_the_package_document(self, tmp_path):
        self._package(tmp_path, "urn:uuid:1")

        assert library.collect(tmp_path)[0]["authorSort"] == "Corey, James S. A."

    def test_the_matchable_count_is_the_isbn_count(self):
        found = [
            {"title": "A", "identifier": "urn:isbn:9781449340360"},
            {"title": "B", "identifier": "urn:uuid:1"},
            {"title": "C"},
        ]

        assert catalogue.matchable_count(found) == 1

    @pytest.mark.parametrize(
        "declared", ["urn:isbn:1234567890123", "urn:isbn:", "urn:isbn:0306406153"]
    )
    def test_an_isbn_that_does_not_verify_is_not_an_isbn(self, tmp_path, declared):
        # canonical_identifier leaves what it cannot verify as declared, so the
        # prefix alone is no evidence: 68 identifiers in a surveyed library are
        # ten or thirteen digits that fail their check. Handed to a tracker,
        # one of those matches the wrong book.
        self._package(tmp_path, declared)

        found = library.collect(tmp_path)
        row = _csv_rows(catalogue.goodreads_csv(found, unknown_shelf=None))[0]

        assert catalogue.matchable_count(found) == 0
        assert row["ISBN13"] == ""
        assert row["ISBN"] == ""

    def test_the_shelf_name_is_claimed_only_when_the_book_was_read(self, tmp_path):
        # Under --name-by author-title the policy needs the package document
        # to name a book; without it, it falls back to the package name, and
        # publishing that as "the file on the shelf" was wrong. A PDF has no
        # package document at all.
        self._package(tmp_path, "urn:uuid:1")
        policy = MetadataNaming(StripNaming())

        named = library.collect(tmp_path, policy)[0]
        unnamed = library.collect(tmp_path, policy, identifiers=False)[0]

        assert named["filename"].startswith("Corey, James S. A.")
        assert "filename" not in unnamed

    @pytest.mark.parametrize("path", ["/x/Dune.EPUB", "/x/Dune.EpUb"])
    def test_a_package_is_recognised_whatever_case_it_is_written_in(self, path):
        # A sideloaded book can arrive as "Dune.EPUB". Judged case-sensitively
        # it was never opened, so it lost its identifier while still claiming
        # a shelf name.
        assert library.package_of({"ZPATH": path}) == Path(path)

    def test_a_package_is_named_by_the_same_rule_it_is_opened_by(self):
        # Recognised as a package for reading and not for naming, an
        # unreadable "Dune.EPUB" was given a shelf name the policy could not
        # really have produced -- and it travels into a merged export.
        policy = MetadataNaming(StripNaming())
        book = library.describe_book(
            "A", {"ZTITLE": "T", "ZPATH": "/x/Dune.EPUB"}, None, policy
        )

        assert book["source"] == "Dune.EPUB"
        assert "filename" not in book

    def test_a_trailing_separator_does_not_hide_a_package(self):
        assert library.package_of({"ZPATH": "/x/Dune.epub/"}) == Path("/x/Dune.epub")

    def test_a_pdf_keeps_its_shelf_name_under_every_policy(self, tmp_path):
        # A PDF has no package document and is copied to the shelf under the
        # very fallback name a metadata policy produces without one, so for
        # it the claim is right where for an unread epub it was wrong.
        make_databases(tmp_path, books=[library_row(path="/x/Paper.pdf")])

        plain = library.collect(tmp_path, PassthroughNaming())[0]
        metadata = library.collect(tmp_path, MetadataNaming(StripNaming()))[0]

        assert plain["filename"] == "Paper.pdf"
        assert metadata["filename"] == "Paper.pdf"

    def test_both_exports_agree_about_the_shelf_name(self, tmp_path):
        make_databases(
            tmp_path, rows=[highlight()], books=[library_row(path="/x/Gone.epub")]
        )
        policy = MetadataNaming(StripNaming())

        catalogued = library.collect(tmp_path, policy)[0]
        annotated = annotations.collect(tmp_path, policy)[0]["book"]

        assert "filename" not in catalogued
        assert "filename" not in annotated

    def test_no_sort_name_is_claimed_without_an_author_to_sort(self):
        # An empty dc:creator carrying a file-as gave a CSV row with
        # "Author l-f" filled and "Author" blank.
        book = library.describe_book(
            "A",
            {"ZTITLE": "T"},
            Package(opf_path="c.opf", creator="", creator_sort="Corey, James S. A."),
            None,
        )

        assert "author" not in book
        assert "authorSort" not in book

    def test_the_sort_name_belongs_to_the_author_it_sorts(self, tmp_path):
        # Apple can name one person and the package another: 62 co-authored
        # books in a surveyed library. "Author l-f" for the other one is wrong.
        self._package(tmp_path, "urn:uuid:1", author="Daniel Abraham")

        book = library.collect(tmp_path)[0]

        assert book["author"] == "Daniel Abraham"
        assert "authorSort" not in book


class TestTheCsvIsWhatGoodreadsWrites:
    """
    A tracker's importer is built for Goodreads' own export, so the header is
    Goodreads' header, verbatim and in its order, and the cells are spelled
    the way Goodreads spells them.
    """

    def _one(self, **entry: object) -> dict[str, str]:
        book = {"title": "Leviathan Wakes", **entry}
        return _csv_rows(catalogue.goodreads_csv([book], unknown_shelf=None))[0]

    def test_the_header_is_goodreads_own(self):
        text = catalogue.goodreads_csv([], unknown_shelf=None)

        assert text.split("\n")[0] == ",".join(catalogue.GOODREADS_COLUMNS)
        assert text.split("\n")[0].startswith("Book Id,Title,Author,")

    def test_a_row_carries_what_apple_holds(self):
        row = self._one(
            author="James S. A. Corey",
            authorSort="Corey, James S. A.",
            identifier="urn:isbn:9781449340360",
            added="2018-12-22T22:44:28Z",
            finished="2018-12-25T22:44:28Z",
            shelf="read",
            rating=4,
            pages=320,
            year=2011,
            collections=["Space opera"],
        )

        assert row["Title"] == "Leviathan Wakes"
        assert row["Author"] == "James S. A. Corey"
        assert row["Author l-f"] == "Corey, James S. A."
        assert row["ISBN13"] == '="9781449340360"'
        assert row["ISBN"] == '="1449340369"'
        assert row["Date Added"] == "2018/12/22"
        assert row["Date Read"] == "2018/12/25"
        assert row["Exclusive Shelf"] == "read"
        assert row["Read Count"] == "1"
        assert row["My Rating"] == "4"
        assert row["Number of Pages"] == "320"
        assert row["Year Published"] == "2011"
        assert row["Bookshelves"] == "Space opera"
        assert row["Owned Copies"] == "1"

    def test_what_is_unknown_is_blank_not_zero(self):
        row = self._one()

        for column in ("ISBN", "ISBN13", "My Rating", "Number of Pages"):
            assert row[column] == ""
        for column in ("Date Read", "Date Added", "Exclusive Shelf", "Read Count"):
            assert row[column] == ""

    def test_the_unknown_shelf_fills_only_the_blanks(self):
        text = catalogue.goodreads_csv(
            [{"title": "Finished", "shelf": "read"}, {"title": "Bought"}],
            unknown_shelf="to-read",
        )

        assert [row["Exclusive Shelf"] for row in _csv_rows(text)] == [
            "read",
            "to-read",
        ]

    def test_the_unknown_shelf_does_not_also_invent_a_read_count(self):
        # The flag fills one column. A read count from it told a tracker the
        # reader had finished 3,389 books they merely bought.
        text = catalogue.goodreads_csv(
            [{"title": "Finished", "shelf": "read"}, {"title": "Bought"}],
            unknown_shelf="read",
        )

        rows = _csv_rows(text)
        assert [row["Exclusive Shelf"] for row in rows] == ["read", "read"]
        assert [row["Read Count"] for row in rows] == ["1", ""]

    def test_apples_own_collections_are_not_shelves(self):
        # Every downloaded epub is in Books, Downloaded and Library. Emitted,
        # that is three tags on 2,800 books, which is noise not a catalogue.
        row = self._one(collections=["Books", "Downloaded", "Library", "Sci-fi"])

        assert row["Bookshelves"] == "Sci-fi"

    def test_a_comma_in_a_collection_name_does_not_split_it(self):
        row = self._one(collections=["Read, then reread"])

        assert row["Bookshelves"] == "Read then reread"

    def test_a_title_with_a_comma_and_a_quote_survives(self):
        row = self._one(title='Say "hello", world')

        assert row["Title"] == 'Say "hello", world'

    @pytest.mark.parametrize("title", ["=HYPERLINK(1)", "+1", "-1", "@SUM"])
    def test_a_formula_shaped_title_is_defused(self, title):
        # A title is input. Opened in Excel, a cell starting with "=" runs.
        row = self._one(title=title)

        assert row["Title"] == "'" + title

    def test_only_the_whole_cell_is_defused_not_each_shelf_in_it(self):
        # Only the first character of a cell can begin a formula, so an
        # apostrophe on the second shelf became part of its name.
        row = self._one(collections=["Alpha", "-Noir", "=Sci"])

        assert row["Bookshelves"] == "Alpha, -Noir, =Sci"

    def test_a_collection_that_collapses_to_nothing_is_not_a_shelf(self):
        # A tracker splitting the cell would import an empty shelf.
        row = self._one(collections=[",", "   ", "Sci-fi"])

        assert row["Bookshelves"] == "Sci-fi"

    def test_a_leading_shelf_that_looks_like_a_formula_is_defused(self):
        row = self._one(collections=["=Sci", "Alpha"])

        assert row["Bookshelves"] == "'=Sci, Alpha"

    def test_an_ordinary_title_is_untouched(self):
        assert self._one(title="1984")["Title"] == "1984"

    def test_a_multi_line_title_becomes_one_line(self):
        assert self._one(title="Line\none")["Title"] == "Line one"

    def test_a_control_character_in_a_title_is_escaped(self):
        # With --library-export - the cell goes straight to the terminal, and
        # a NUL makes several importers truncate or drop the row.
        row = self._one(title="Foo\x1b]0;pwned\x07 bar\x00")

        assert row["Title"] == "Foo\\x1b]0;pwned\\x07 bar\\x00"

    @pytest.mark.parametrize(
        ("isbn13", "isbn10"),
        [
            ("9781449340360", "1449340369"),
            ("9780804429573", "080442957X"),
            ("9791234567896", None),  # 979 books never had an ISBN-10
            (None, None),
        ],
    )
    def test_the_isbn_10_is_the_same_book(self, isbn13, isbn10):
        assert validate.isbn10_of(isbn13) == isbn10

    def test_lines_end_in_newline_only(self):
        # write_text translates newlines, so "\\r\\n" would become "\\r\\r\\n"
        # on Windows and the file would open with a blank row after each book.
        assert "\r" not in catalogue.goodreads_csv([{"title": "T"}], unknown_shelf=None)


class TestTheJsonIsTheCanonicalRecord:
    def test_it_validates_against_the_shipped_schema(self, tmp_path):
        make_databases(
            tmp_path,
            books=[
                library_row(added=BOUGHT, finished=MADE_AT, rating=5, pages=300),
                library_row(asset="B", title="Second", progress=0.5),
            ],
            collections={"Books": ["ASSET1", "B"], "Favourites": ["B"]},
        )

        document = catalogue.build_document(library.collect(tmp_path))

        assert catalogue.schema_problems(document) == []
        assert document["generator"]["name"] == "ibook2epub"
        assert document["generated"].endswith("Z")

    def test_the_schema_file_is_shipped_beside_the_module(self):
        assert catalogue.SCHEMA_PATH.is_file()

    def test_an_unknown_field_is_a_problem(self):
        document = catalogue.build_document([{"title": "T", "isbn": "x"}])

        assert catalogue.schema_problems(document) == ["books[0] has unknown ['isbn']"]

    def test_a_book_without_a_title_is_a_problem(self):
        document = catalogue.build_document([{"author": "A"}])

        assert catalogue.schema_problems(document) == ["books[0] missing title"]

    def test_an_entry_that_is_not_an_object_is_a_problem(self):
        document = catalogue.build_document([])
        document["books"] = ["Leviathan Wakes"]

        assert catalogue.schema_problems(document) == ["books[0] is str, not an object"]

    def test_every_collection_is_kept_including_apples(self, tmp_path):
        # The CSV filters; the record does not.
        make_databases(tmp_path, collections={"Books": ["ASSET1"]})

        assert library.collect(tmp_path)[0]["collections"] == ["Books"]

    def test_the_render_is_the_document_plus_a_newline(self):
        text = catalogue.render([{"title": "T"}], "json")

        assert text.endswith("}\n")
        assert json.loads(text)["books"] == [{"title": "T"}]


class TestItFailsSafely:
    def test_a_missing_library_database_says_so(self, tmp_path):
        # #9: the folder was read, so the permission is not the reason, and
        # the Full Disk Access advice this used to give was wrong.
        (tmp_path / "BKLibrary").mkdir(parents=True)

        with pytest.raises(
            coredata.ContainerUnavailableError, match="has not created"
        ) as caught:
            library.collect(tmp_path)

        assert "Full Disk Access" not in str(caught.value)

    def test_a_missing_container_is_reported_not_raised(self, tmp_path):
        with pytest.raises(coredata.ContainerUnavailableError, match="not there"):
            library.collect(tmp_path / "absent")

    def test_a_reshaped_asset_table_is_reported(self, tmp_path):
        make_databases(tmp_path)
        database = next(tmp_path.rglob("BKLibrary*.sqlite"))
        with open_for_writing(database) as connection:
            connection.execute("DROP TABLE ZBKLIBRARYASSET")
            connection.execute("CREATE TABLE ZBKLIBRARYASSET (SOMETHINGELSE TEXT)")

        with pytest.raises(coredata.ContainerUnavailableError, match="not shaped"):
            library.collect(tmp_path)

    def test_a_missing_state_column_costs_the_state_not_the_catalogue(
        self, tmp_path, caplog
    ):
        # Those columns are empty on nearly every row of a real library, so
        # losing every title to one of them is the worst trade available.
        make_databases(tmp_path, books=[library_row(added=BOUGHT, progress=0.5)])
        database = next(tmp_path.rglob("BKLibrary*.sqlite"))
        with open_for_writing(database) as connection:
            connection.execute("ALTER TABLE ZBKLIBRARYASSET DROP COLUMN ZPAGECOUNT")

        found = library.collect(tmp_path)

        assert [book["title"] for book in found] == ["Leviathan Wakes"]
        assert "shelf" not in found[0]
        assert "added" not in found[0]
        assert "no book has a shelf" in caplog.text

    def test_missing_collection_tables_cost_the_shelves_not_the_books(
        self, tmp_path, caplog
    ):
        make_databases(tmp_path, collections={"Favourites": ["ASSET1"]})
        database = next(tmp_path.rglob("BKLibrary*.sqlite"))
        with open_for_writing(database) as connection:
            connection.execute("DROP TABLE ZBKCOLLECTIONMEMBER")

        found = library.collect(tmp_path)

        assert len(found) == 1
        assert "collections" not in found[0]
        assert "collections failed" in caplog.text

    def test_an_unusable_row_costs_one_book_not_the_catalogue(self, tmp_path, caplog):
        # A BLOB, because that is what survives the column's text affinity:
        # an integer written to a VARCHAR column comes back as text.
        make_databases(
            tmp_path,
            books=[library_row(asset=b"\x00", title="Blobbed"), library_row()],
        )

        found = library.collect(tmp_path)

        assert [book["title"] for book in found] == ["Leviathan Wakes"]
        assert "Skipped an unreadable library row" in caplog.text
        assert "not text" in caplog.text

    def test_a_date_that_is_not_a_date_is_left_out(self, tmp_path):
        make_databases(tmp_path, books=[library_row(added="yesterday", pages="many")])

        book = library.collect(tmp_path)[0]

        assert "added" not in book
        assert "pages" not in book

    @pytest.mark.parametrize("year", [float("inf"), float("nan"), "MMXI", 0])
    def test_a_year_that_is_not_a_year_costs_the_year_not_the_export(
        self, tmp_path, year
    ):
        # An infinity raised OverflowError past the per-row guards, which
        # catch TypeError and ValueError, and took both exports down; a bool
        # became year 1.
        make_databases(tmp_path, books=[library_row(year=year)])

        found = library.collect(tmp_path)

        assert len(found) == 1
        assert "year" not in found[0]

    def test_text_that_is_not_utf8_costs_that_value_not_the_catalogue(self, tmp_path):
        # SQLite stores whatever bytes it is handed as TEXT, and sqlite3
        # decoded them strictly: one such value raised mid-fetch, and the
        # whole query -- every book -- was reported as an unreadable database.
        make_databases(tmp_path, books=[library_row(), library_row(asset="ASSET2")])
        database = next(tmp_path.rglob("BKLibrary*.sqlite"))
        with open_for_writing(database) as connection:
            connection.execute(
                "UPDATE ZBKLIBRARYASSET SET ZAUTHOR = CAST(x'4cff' AS TEXT)"
                " WHERE ZASSETID = 'ASSET2'"
            )

        found = library.collect(tmp_path)

        assert sorted(book["title"] for book in found) == ["Leviathan Wakes"] * 2
        assert {book.get("author") for book in found} == {
            "James S. A. Corey",
            "L\ufffd",
        }

    def test_text_that_is_not_utf8_costs_that_value_not_the_highlights(self, tmp_path):
        make_databases(tmp_path, rows=[highlight(), highlight(uuid="U2")])
        database = next(tmp_path.rglob("AEAnnotation*.sqlite"))
        with open_for_writing(database) as connection:
            connection.execute(
                "UPDATE ZAEANNOTATION SET ZANNOTATIONNOTE = CAST(x'ff' AS TEXT)"
                " WHERE ZANNOTATIONUUID = 'U2'"
            )

        found = annotations.collect(tmp_path)

        assert sorted(item["id"] for item in found) == ["U1", "U2"]

    def test_reading_does_not_write_to_the_database(self, tmp_path):
        make_databases(tmp_path)
        database = next(tmp_path.rglob("BKLibrary*.sqlite"))
        before = database.stat().st_mtime_ns

        library.collect(tmp_path)

        assert database.stat().st_mtime_ns == before


class TestBothExportsDescribeABookTheSameWay:
    """
    ``describe_book`` is the one place a library row and a package document
    are merged. It was two places: the catalogue patched in the package's
    author and sort name after the fact, so an annotation and a catalogue
    entry for the same asset id disagreed about who wrote the book.
    """

    def test_the_annotation_export_gets_the_same_author_and_sort_name(self, tmp_path):
        package = make_metadata_package(
            tmp_path / "lib",
            "Wizard.epub",
            title="A Wizard of Earthsea",
            creator="Ursula K. Le Guin",
            file_as="Le Guin, Ursula K.",
        )
        make_databases(
            tmp_path,
            rows=[highlight()],
            books=[library_row(author=None, path=str(package))],
        )

        catalogued = library.collect(tmp_path)[0]
        annotated = annotations.collect(tmp_path)[0]["book"]

        assert annotated["author"] == catalogued["author"] == "Ursula K. Le Guin"
        assert annotated["authorSort"] == catalogued["authorSort"]
        assert (
            annotations.schema_problems(
                annotations.build_document(annotations.collect(tmp_path))
            )
            == []
        )


class TestUndatedAnnotationsSortLast:
    def test_an_annotation_with_no_date_follows_the_dated_ones(self, tmp_path):
        # The empty string sorts before every date, which put them first; the
        # clock stamp they used to get put them last, and a rerun into an
        # existing export must not reorder the file the reader already has.
        make_databases(
            tmp_path,
            rows=[
                highlight(uuid="NONE", text="undated", created=0),
                highlight(uuid="LATE", text="later", created=MADE_AT + 5),
                highlight(uuid="EARLY", text="earlier", created=MADE_AT),
            ],
        )

        found = annotations.collect(tmp_path)

        assert [item["id"] for item in found] == ["EARLY", "LATE", "NONE"]

    def test_a_gained_author_counts_as_a_change(self):
        was = {"id": "U", "book": {"title": "T"}, "text": "x"}
        now = {"id": "U", "book": {"title": "T", "author": "A"}, "text": "x"}

        _merged, tally = annotations.merge(annotations.build_document([was]), [now])

        assert tally["updated"] == 1


class TestAMissingPermissionIsToldApartFromAbsence:
    """
    #9: a missing container and a missing Full Disk Access grant each had one
    fixed message, and each fired for the other. ``is_dir()`` and ``glob()``
    both swallow the ``OSError`` that names the cause, so by the time a message
    was chosen the errno was gone. Driven with ``chmod``, which fails the way a
    denied grant does on macOS: both arrive as ``PermissionError``.
    """

    @needs_permissions
    def test_a_refusing_parent_names_the_permission(self, tmp_path):
        parent = tmp_path / "Containers"
        container = parent / "Documents"
        container.mkdir(parents=True)
        parent.chmod(0)
        try:
            with pytest.raises(
                coredata.ContainerPermissionError, match="Full Disk Access"
            ):
                coredata.container_directory(container)
        finally:
            parent.chmod(0o700)

    @needs_permissions
    def test_a_refusing_folder_names_the_permission(self, tmp_path):
        folder = tmp_path / "BKLibrary"
        folder.mkdir()
        folder.chmod(0)
        try:
            with pytest.raises(
                coredata.ContainerPermissionError, match="Full Disk Access"
            ):
                coredata.database_in(tmp_path, "BKLibrary", "library")
        finally:
            folder.chmod(0o700)

    def test_an_absent_container_says_books_never_ran(self, tmp_path):
        with pytest.raises(
            coredata.ContainerUnavailableError, match="may never have run"
        ) as caught:
            coredata.container_directory(tmp_path / "absent")

        assert "Full Disk Access" not in str(caught.value)

    def test_an_absent_folder_is_not_blamed_on_the_permission(self, tmp_path):
        with pytest.raises(
            coredata.ContainerUnavailableError, match="BKLibrary"
        ) as caught:
            coredata.database_in(tmp_path, "BKLibrary", "library")

        assert "Full Disk Access" not in str(caught.value)

    def test_an_empty_folder_says_books_has_not_made_one(self, tmp_path):
        (tmp_path / "BKLibrary").mkdir()

        with pytest.raises(
            coredata.ContainerUnavailableError, match="has not created"
        ) as caught:
            coredata.database_in(tmp_path, "BKLibrary", "library")

        assert "Full Disk Access" not in str(caught.value)

    def test_a_refusal_still_reaches_every_caller(self):
        # Every caller catches ContainerUnavailableError. A refusal that got
        # past them would be a traceback where there used to be a message.
        assert issubclass(
            coredata.ContainerPermissionError, coredata.ContainerUnavailableError
        )

    @needs_permissions
    def test_the_library_export_names_the_permission(self, tmp_path):
        folder = tmp_path / "BKLibrary"
        folder.mkdir()
        folder.chmod(0)
        try:
            with pytest.raises(
                coredata.ContainerPermissionError, match="Full Disk Access"
            ):
                library.collect(tmp_path)
        finally:
            folder.chmod(0o700)

    def test_a_container_that_is_a_file_says_so(self, tmp_path):
        # Copilot on #18: NotADirectoryError was worded as "not there", which
        # sent the reader looking for something that is there, as a file.
        container = tmp_path / "Documents"
        container.write_text("not a folder", encoding="utf-8")

        with pytest.raises(
            coredata.ContainerUnavailableError, match="is a file"
        ) as caught:
            coredata.container_directory(container)

        assert "not there" not in str(caught.value)

    def test_a_folder_that_is_a_file_says_so(self, tmp_path):
        (tmp_path / "BKLibrary").write_text("not a folder", encoding="utf-8")

        with pytest.raises(
            coredata.ContainerUnavailableError, match="is a file"
        ) as caught:
            coredata.database_in(tmp_path, "BKLibrary", "library")

        assert "has not created" not in str(caught.value)
