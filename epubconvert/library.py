"""
Take a reader's library out of Apple Books.

Apple gives no way to get a catalogue out: what you own, who wrote it, when
you got it, and the collections you sorted it onto. All of it sits in the
``BKLibrary`` database inside the Books container. This module reads that
database and writes it two ways -- a Goodreads-format CSV, which The
StoryGraph and other trackers import directly, and a JSON document in the
style of the annotation export, which is the canonical record the CSV is a
view of. The annotation export reads the same database to name the book an
annotation belongs to, and does so through this module.

**It is a catalogue, not a reading history.** Measured against a real
3,620-book library, ``ZRATING`` is zero on every row, ``ZPAGECOUNT`` on all but
one, ``ZYEAR`` and ``ZISFINISHED`` are null throughout, and eleven books carry
a finish date. Title, author, collections and acquisition date are what the
database reliably holds, and the export promises those and nothing more. Every
other column is carried when it is there and omitted when it is not, because
another library may differ.

**Reading state is evidence or nothing.** The obvious rule -- a book with no
finish date and no progress is "to-read" -- would tell a tracker that this
reader intends to read 3,389 books they merely bought. So the shelf is derived
only from what the database says and left out otherwise; a reader who wants a
default chooses it with ``--unknown-shelf``.

**The membership table is stale and will invent books if trusted.** 749 of
14,500 collection memberships in that library point at an asset in no library
row: deleted books whose shelf rows survived. So the join runs from the asset
side, on ``ZASSETID``, and a membership without a book is nothing.
"""

from __future__ import annotations

import csv
import io
import json
import math
from collections.abc import Mapping
from pathlib import Path
from sqlite3 import Row
from typing import Any, Literal

from . import __version__, schema
from .app_logger import logger
from .contained import escapes
from .coredata import (
    ContainerUnavailableError,
    container_directory,
    database_in,
    moment,
    newest,
    now,
    rows,
)
from .display import collapse, printable
from .policy import NamingPolicy
from .spec import PACKAGE_SUFFIX
from .validate import (
    Package,
    ValidationError,
    canonical_identifier,
    isbn10_of,
    isbn13_of,
    read_package_dir,
    usable_identifier,
)

#: What a book with neither a title nor an asset id is called. Both schemas
#: require a non-empty title, and an entry with no identity at all is worse
#: than one that admits it has none.
UNKNOWN_BOOK = "Unknown book"

#: The schema shipped beside this module, which is the contract.
SCHEMA_PATH = Path(__file__).with_name("library.schema.json")

#: The shapes an export can take.
LibraryFormat = Literal["csv", "json"]
LIBRARY_FORMATS: tuple[LibraryFormat, ...] = ("csv", "json")

#: The three shelves Goodreads treats as exclusive, and so the only values a
#: tracker's importer understands in that column.
READ = "read"
READING = "currently-reading"
TO_READ = "to-read"
SHELVES = (TO_READ, READING, READ)

#: Collections Apple creates and fills by itself. They say where a book is
#: stored and what format it is in, not where the reader shelved it, and
#: emitting them gives nearly every book a ``Books`` tag and a ``Downloaded``
#: tag: 2,825 and 2,854 of 3,620 in a surveyed library. Matched by the names
#: Books uses in English, so on a system in another language they pass
#: through as shelves; the JSON export carries every collection regardless.
AUTOMATIC_COLLECTIONS = frozenset(
    {"Library", "Downloaded", "Books", "PDFs", "Audiobooks", "My Samples"}
)

#: The Goodreads export header, verbatim and in Goodreads' order, because an
#: importer built for that file keys on it. Columns this database cannot fill
#: are written empty rather than dropped: a missing column is a different
#: format, an empty one is a missing value.
GOODREADS_COLUMNS = (
    "Book Id",
    "Title",
    "Author",
    "Author l-f",
    "Additional Authors",
    "ISBN",
    "ISBN13",
    "My Rating",
    "Average Rating",
    "Publisher",
    "Binding",
    "Number of Pages",
    "Year Published",
    "Original Publication Year",
    "Date Read",
    "Date Added",
    "Bookshelves",
    "Bookshelves with positions",
    "Exclusive Shelf",
    "My Review",
    "Spoiler",
    "Private Notes",
    "Read Count",
    "Owned Copies",
)

#: A cell beginning with one of these is a formula to a spreadsheet, and a
#: title is input: a sideloaded book called ``=HYPERLINK(...)`` would run when
#: the CSV is opened in Excel. Such a cell is prefixed with an apostrophe,
#: which is how spreadsheets themselves spell "this is text".
FORMULA_TRIGGERS = ("=", "+", "-", "@")

#: What describes a book: the columns :func:`describe_book` reads, and the
#: only ones the annotation export needs. Asked for on their own, because a
#: Books update that drops or renames a column the *catalogue* reads should
#: not also cost every highlight its title.
#:
#: ``ZASSETID`` is what the annotation database joins on, and the only column
#: read anywhere here that is unique: 3,620 rows, 3,620 distinct values in a
#: surveyed library. The identifier a book's package document declares is not
#: -- 52 books there share one with another -- and the two must not be
#: confused.
BOOK_COLUMNS = "ZASSETID, ZTITLE, ZAUTHOR, ZLANGUAGE, ZYEAR, ZPATH"

#: What the reader has done with a book. Only the catalogue reads these.
STATE_COLUMNS = (
    "ZPURCHASEDATE, ZLASTOPENDATE, ZDATEFINISHED, ZISFINISHED,"
    " ZREADINGPROGRESS, ZRATING, ZPAGECOUNT"
)

#: Rows with no asset id are not books: the id is the key both exports join
#: on, and a row without one can be matched to nothing.
REAL_ROWS = " FROM ZBKLIBRARYASSET WHERE ZASSETID IS NOT NULL AND ZASSETID <> ''"

ASSET_QUERY = f"SELECT {BOOK_COLUMNS}, {STATE_COLUMNS}{REAL_ROWS}"

#: The catalogue without the reading state, for the annotation export.
BOOK_QUERY = f"SELECT {BOOK_COLUMNS}{REAL_ROWS}"

#: Collection memberships, joined to the collection's name. Keyed by the
#: string ``ZASSETID`` rather than the ``ZASSET`` row reference: 13,751 of
#: 14,500 rows in a surveyed library carry both, so they are not
#: interchangeable, and the string is what the asset table is keyed on.
COLLECTION_QUERY = (
    "SELECT ZBKCOLLECTIONMEMBER.ZASSETID AS asset, ZBKCOLLECTION.ZTITLE AS title"
    " FROM ZBKCOLLECTIONMEMBER"
    " JOIN ZBKCOLLECTION ON ZBKCOLLECTION.Z_PK = ZBKCOLLECTIONMEMBER.ZCOLLECTION"
    " WHERE ZBKCOLLECTIONMEMBER.ZASSETID IS NOT NULL"
    "   AND ZBKCOLLECTION.ZTITLE IS NOT NULL"
)


def source_name(path: object) -> str | None:
    """
    Take the package directory's name out of the path Apple recorded.

    The name only. ``ZPATH`` is absolute and runs through the reader's home
    directory, which has no business in a file they may share.

    :param path: The recorded path, of whatever type the column held.

    :return: The name, or None when it is not one. A ``ZPATH`` ending in
        ``..`` yielded ``source: ".."``, published into JSON another tool is
        expected to act on.
    """
    if not isinstance(path, str) or not path:
        return None
    name = Path(path).name
    if name in ("", ".", "..") or escapes(name):
        return None
    return name


def is_package_name(name: str) -> bool:
    """
    Whether a name is a package directory's.

    Case-folded, because a sideloaded book can arrive as ``Dune.EPUB``. Stated
    here once: judged one way when deciding what to open and another when
    deciding what to call it, a book was read as a package and then named as
    though it were not.

    :param name: A directory or file name, without its path.

    :return: True when it names an epub package.
    """
    return name.casefold().endswith(PACKAGE_SUFFIX)


def package_of(row: Mapping[str, Any]) -> Path | None:
    """
    The package directory a library row points at, if it points at one.

    Only a package directory has a package document. A PDF or an audiobook --
    1,096 of 3,620 rows in a surveyed library -- has nothing to open, and
    opening it anyway cost a failed read per row. Decided here for both
    exports, so a row cannot be a package to one and not the other.

    :param row: The library row, or an empty mapping.

    :return: The path, or None when the column names no package.
    """
    held = row.get("ZPATH")
    if not isinstance(held, str) or not held:
        return None
    # On the name rather than the whole path: a trailing separator made an
    # otherwise ordinary package invisible, which cost the book its identifier.
    if not is_package_name(Path(held).name):
        return None
    return Path(held)


def read_package_once(
    package: Path, parsed: dict[Path, Package | None]
) -> Package | None:
    """
    Parse a book once, however many times it is asked for.

    :param package: The package directory.
    :param parsed: Books already read, added to in place.

    :return: The package document, or None if it could not be read.
    """
    if package not in parsed:
        try:
            parsed[package] = read_package_dir(package)
        except (ValidationError, OSError):
            parsed[package] = None
    return parsed[package]


def describe_book(
    asset_id: str,
    row: Mapping[str, Any],
    parsed: Package | None,
    policy: NamingPolicy | None,
) -> dict[str, Any]:
    """
    Describe a book from what the library database and its package say.

    :param asset_id: Apple's id for the book.
    :param row: The library row, or an empty mapping when the library has
        forgotten the book.
    :param parsed: The book's package document, if it could be read.
    :param policy: The naming policy, or None to make no claim about the shelf.

    :return: The book, with whatever fields are known. Falls back to the asset
        id as a title, so an annotation whose book the library has forgotten is
        still exported.
    """
    # Apple's columns are untyped. A BLOB where a title should be is treated
    # as no title: it used to reach json.dumps and take the whole export down,
    # and skipping the row instead would cost a highlight over a title.
    title = row.get("ZTITLE")
    book: dict[str, Any] = {
        "title": title if _is_text(title) else asset_id or UNKNOWN_BOOK
    }
    # Apple's row wins; the package fills what it lacks. One rule, here, so
    # the two exports cannot describe the same book two ways. The sort name
    # has no column in Apple's database at all.
    author = row.get("ZAUTHOR")
    if not _is_text(author) and parsed is not None:
        author = parsed.creator
    if _is_text(author):
        book["author"] = author
    # The sort name only for the author it sorts. Apple can name one person
    # and the package another -- 62 co-authored books in a surveyed library
    # -- and "Author l-f" for somebody the row does not name is wrong.
    if (
        "author" in book
        and parsed is not None
        and parsed.creator_sort
        and author == parsed.creator
    ):
        book["authorSort"] = parsed.creator_sort
    if _is_text(row.get("ZLANGUAGE")):
        book["language"] = row["ZLANGUAGE"]
    # Apple's column is untyped: a date-shaped string used to raise out of
    # the whole export, and a float infinity raised past the per-row guards
    # that string had taught it to have.
    year = _count(row.get("ZYEAR"))
    if year is not None:
        book["year"] = year
    source = source_name(row.get("ZPATH"))
    if source is not None:
        book["source"] = source
        # Asked of the policy, never worked out here. Under --name-by
        # author-title a book read from "Leviathan Wakes.epub" is written as
        # "Corey, James S.A. - Leviathan Wakes.epub", and an annotation naming
        # the wrong one cannot be traced back to its book. Claimed only when
        # the policy can really name it. One that needs the package document
        # falls back to the package name without it, and publishing that as
        # "the file on the shelf" was wrong for an epub whose package document
        # was not read. A PDF has no package document and is copied to the
        # shelf under exactly that fallback, so for it the name is right.
        if policy is not None and (
            parsed is not None
            or not policy.needs_metadata
            or not is_package_name(source)
        ):
            book["filename"] = policy.filename(source, parsed)
    # The publication's own key, and the only one here that is neither Apple's
    # nor this run's: a title, a package name and a shelf filename can all
    # change, and after any of them the book can still be matched by this.
    # Asked of validate rather than re-derived, because knowing which
    # dc:identifier counts -- and that "none" identifies 92 books in a real
    # library -- is that module's job.
    declared = usable_identifier(parsed)
    if declared is not None:
        # Canonical, because the field is only a matching key if the same book
        # always yields the same string, and 1,092 books in a surveyed library
        # write their ISBN six different ways. The declared form is kept
        # whenever it differed, so nothing about the book is lost.
        book["identifier"] = canonical_identifier(declared)
        if book["identifier"] != declared:
            book["declaredIdentifier"] = declared
    if asset_id:
        book["assetId"] = asset_id
    return book


def _is_text(value: object) -> bool:
    """Whether an untyped column holds something worth publishing as text."""
    return isinstance(value, str) and bool(value)


def index_assets(directory: Path) -> dict[str, dict[str, Any]]:
    """
    Read the library database, keyed by the id annotations join on.

    :param directory: The container directory.

    :return: Asset id to library row. Empty if the library cannot be read,
        because losing every highlight is worse than losing every title.
    """
    # The columns describe_book reads and no more, so both exports name a
    # book from one query while a change to a state column the catalogue
    # needs cannot cost every highlight its title.
    database = newest(directory / "BKLibrary", "BKLibrary")
    if database is None:
        # Not an error, and not Full Disk Access either: the annotation
        # database under the same protected tree has just been read.
        return {}
    try:
        found = rows(database, BOOK_QUERY)
    except ContainerUnavailableError as exc:
        logger.warning(
            "Reading the Books library failed, so titles are missing: %s", exc
        )
        return {}
    return {row["ZASSETID"]: dict(row) for row in found}


def collect(
    container: Path | None = None,
    policy: NamingPolicy | None = None,
    *,
    identifiers: bool = True,
) -> list[dict[str, Any]]:
    """
    Gather every book in the library.

    :param container: The Books container directory. Defaults to Apple's.
    :param policy: The naming policy this run is using, so each book can say
        what its file on the shelf is called. Without one no claim is made.
    :param identifiers: Whether to open each book's package document for its
        identifier. That is one read per book -- 2,805 on a real library --
        where the annotation export reads one per *annotated* book, five
        there. Without it no book carries an ISBN, and a tracker matches none.

    :return: The books, ordered by title.

    :raises ContainerUnavailableError: If the library database is missing or
        unreadable. On macOS this usually means the terminal has not been
        granted Full Disk Access.
    """
    database = database_in(container_directory(container), "BKLibrary", "library")
    assets = _assets(database)
    collections = _collections(database)
    parsed: dict[Path, Package | None] = {}
    found: list[dict[str, Any]] = []
    for row in assets:
        # One row at a time, so one unusable row costs one book rather than
        # the catalogue. Apple's columns are untyped: a date that is a string,
        # a title that is a BLOB and a path that is a number are all possible.
        try:
            found.append(_entry_of(dict(row), collections, parsed, policy, identifiers))
        except (TypeError, ValueError, OSError) as exc:
            logger.warning(
                "Skipped an unreadable library row (%s): %s",
                printable(str(row["ZASSETID"])),
                exc,
            )
    found.sort(key=_catalogue_order)
    return found


def _assets(database: Path) -> list[Row]:
    """
    Read every book, with as much of its reading state as the database holds.

    A Books update that renames one state column costs that column rather
    than the catalogue. Those columns are empty on nearly every row of a real
    library -- the rating on all 3,620, the page count on all but one -- so
    losing every title to one of them is the worst trade available. The same
    rule the collections already follow.

    :param database: The library database.

    :return: The rows.

    :raises ContainerUnavailableError: If the columns that *name* a book are
        not there. Without them there is no catalogue to write.
    """
    try:
        return rows(database, ASSET_QUERY)
    except ContainerUnavailableError as exc:
        logger.warning(
            "Reading what you have done with each book failed, so no book has "
            "a shelf, a date or a rating: %s",
            exc,
        )
    return rows(database, BOOK_QUERY)


def _collections(database: Path) -> dict[str, list[str]]:
    """
    Read which collections each book is in.

    A failure here costs the shelves and not the catalogue: the collection
    tables are the part of Apple's schema this module knows least about, and
    a Books update that reshapes them should not take the titles down too.

    :param database: The library database.

    :return: Asset id to the names of its collections, sorted and distinct.
    """
    try:
        members = rows(database, COLLECTION_QUERY)
    except ContainerUnavailableError as exc:
        logger.warning("Reading the collections failed, so no book has any: %s", exc)
        return {}
    held: dict[str, set[str]] = {}
    for member in members:
        asset, title = member["asset"], member["title"]
        if isinstance(asset, str) and isinstance(title, str) and title:
            held.setdefault(asset, set()).add(title)
    return {asset: sorted(titles) for asset, titles in held.items()}


def _entry_of(
    row: dict[str, Any],
    collections: Mapping[str, list[str]],
    parsed: dict[Path, Package | None],
    policy: NamingPolicy | None,
    identifiers: bool,
) -> dict[str, Any]:
    """
    Turn one library row into one catalogue entry.

    :param row: The row, with Apple's column names.
    :param collections: Every book's collections, by asset id.
    :param parsed: Books already read, added to in place.
    :param policy: The naming policy, or None to make no claim about the shelf.
    :param identifiers: Whether to open the package document.

    :return: The entry, omitting fields the row does not carry rather than
        sending them as empty.

    :raises TypeError: If the row holds something no entry can be made from.
        Caught per row by :func:`collect`, so one bad row costs one book.
    """
    asset_id = row["ZASSETID"]
    if not isinstance(asset_id, str):
        raise TypeError(f"asset id is {type(asset_id).__name__}, not text")
    package = package_of(row)
    book = read_package_once(package, parsed) if identifiers and package else None

    entry = describe_book(asset_id, row, book, policy)
    entry.update(_reading_state(row))
    if collections.get(asset_id):
        entry["collections"] = list(collections[asset_id])
    return entry


def _reading_state(row: dict[str, Any]) -> dict[str, Any]:
    """
    Read what the row says about the reader and the book.

    :param row: The row, with Apple's column names.

    :return: The dates, the progress, the counts and the shelf they add up
        to, each present only when the row carries it.
    """
    state: dict[str, Any] = {}
    for key, column in (
        ("added", "ZPURCHASEDATE"),
        ("lastOpened", "ZLASTOPENDATE"),
        ("finished", "ZDATEFINISHED"),
    ):
        when = moment(row.get(column))
        if when is not None:
            state[key] = when
    progress = _fraction(row.get("ZREADINGPROGRESS"))
    if progress is not None:
        state["progress"] = progress
    for key, column in (("rating", "ZRATING"), ("pages", "ZPAGECOUNT")):
        count = _count(row.get(column))
        if count is not None:
            state[key] = count
    shelf = shelf_of(state.get("finished"), row.get("ZISFINISHED"), progress)
    if shelf is not None:
        state["shelf"] = shelf
    return state


def _fraction(value: object) -> float | None:
    """
    Read a progress column: a share of the book, ``1`` being its end.

    :param value: Whatever the column held.

    :return: The share when it is a positive finite number, else None.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _count(value: object) -> int | None:
    """
    Read a counting column: a rating, a page count.

    :param value: Whatever the column held.

    :return: The number when it is a positive integer, else None.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def shelf_of(finished: str | None, flag: object, progress: float | None) -> str | None:
    """
    Derive the exclusive shelf from the evidence, or decline to.

    Three columns can say a book was read, and on a surveyed library they
    disagree: eleven books carry a finish date, ``ZISFINISHED`` is null on
    every row, and none of the eleven is in Apple's ``Finished`` collection.
    A finish date, the flag, or having reached the end each count. Progress
    short of the end means the book is being read. Nothing else is claimed:
    "to-read" is not a neutral default but an assertion about intent, and
    the database supports it for none of the 3,389 books it would be made of.

    Collections are deliberately not consulted. They are the reader's
    bookshelves, and ``Want to Read`` reaches the CSV as one of those.

    :param finished: The finish date, if any.
    :param flag: ``ZISFINISHED``, whatever type it held.
    :param progress: The reading progress, if any.

    :return: ``read``, ``currently-reading``, or None for no evidence.
    """
    if finished or _count(flag) == 1 or (progress is not None and progress >= 1):
        return READ
    if progress:
        return READING
    return None


def _catalogue_order(entry: dict[str, Any]) -> tuple[str, str]:
    """
    The order a catalogue is written in: by title, then by asset id.

    :param entry: A book.

    :return: The sort key.
    """
    return (str(entry.get("title") or "").casefold(), str(entry.get("assetId") or ""))


def build_document(found: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Wrap the catalogue in the envelope the schema describes.

    :param found: What :func:`collect` returned.

    :return: The document to serialise.
    """
    return {
        "$schema": SCHEMA_PATH.name,
        "generator": {"name": "ibook2epub", "version": __version__},
        "generated": now(),
        "books": found,
    }


def schema_problems(document: dict[str, Any]) -> list[str]:
    """
    Check a document against the shipped schema.

    :param document: The document to check.

    :return: What is wrong with it, empty if nothing is.
    """
    return schema.document_problems(document, schema.load(SCHEMA_PATH), "books", "book")


def matchable_count(found: list[dict[str, Any]]) -> int:
    """
    Count the books a tracker can match on an ISBN.

    The number worth telling the reader before they import, not after. On a
    surveyed library it is 1,108 of 3,620: most books identify themselves by
    a UUID, which is a perfectly good identifier and useless to a tracker.

    :param found: The catalogue.

    :return: How many entries carry an ISBN.
    """
    return sum(1 for entry in found if isbn13_of(entry.get("identifier")))


def render(
    found: list[dict[str, Any]],
    fmt: LibraryFormat,
    *,
    unknown_shelf: str | None = None,
) -> str:
    """
    Serialise the catalogue in whichever shape was asked for.

    :param found: The catalogue.
    :param fmt: ``csv`` or ``json``.
    :param unknown_shelf: What the CSV's Exclusive Shelf column says for a book
        the evidence says nothing about. None leaves it blank. Applies to the
        CSV only: the JSON records what the database says and no more.

    :return: The file's contents, ending in a newline.
    """
    if fmt == "json":
        return json.dumps(build_document(found), indent=2, ensure_ascii=False) + "\n"
    return goodreads_csv(found, unknown_shelf=unknown_shelf)


def goodreads_csv(found: list[dict[str, Any]], *, unknown_shelf: str | None) -> str:
    """
    Render the catalogue as the CSV Goodreads exports.

    That format rather than any other because it is the one every tracker's
    importer is built for and the one The StoryGraph maintains. The ISBN cells
    are written the way Goodreads writes them, ``="9781449340360"``, which is
    a spreadsheet's spelling of "keep this as text": a bare thirteen-digit
    number loses its leading zero and gains an exponent when opened in Excel,
    and the importers expect the quoted form.

    :param found: The catalogue.
    :param unknown_shelf: What Exclusive Shelf says when nothing is known.

    :return: The CSV, with ``\\n`` line endings so that it round-trips
        through ``write_text`` on every platform.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(GOODREADS_COLUMNS)
    for entry in found:
        writer.writerow(_goodreads_row(entry, unknown_shelf))
    return buffer.getvalue()


def _goodreads_row(entry: dict[str, Any], unknown_shelf: str | None) -> list[str]:
    """
    Render one book as one Goodreads row.

    :param entry: The book.
    :param unknown_shelf: What Exclusive Shelf says when nothing is known.

    :return: One cell per column of :data:`GOODREADS_COLUMNS`.
    """
    isbn13 = isbn13_of(entry.get("identifier"))
    shelf = entry.get("shelf") or unknown_shelf or ""
    # A name made only of commas or whitespace collapses to nothing, and an
    # empty shelf in the joined cell is one a tracker would import.
    shelves = [
        shelf
        for shelf in (
            _shelf_name(name)
            for name in entry.get("collections", [])
            if name not in AUTOMATIC_COLLECTIONS
        )
        if shelf
    ]
    cells: dict[str, object] = {
        "Title": _cell(entry["title"]),
        "Author": _cell(entry.get("author", "")),
        "Author l-f": _cell(entry.get("authorSort", "")),
        "ISBN": _quoted_number(isbn10_of(isbn13)),
        "ISBN13": _quoted_number(isbn13),
        "My Rating": entry.get("rating", ""),
        "Number of Pages": entry.get("pages", ""),
        "Year Published": entry.get("year", ""),
        "Date Read": _goodreads_date(entry.get("finished")),
        "Date Added": _goodreads_date(entry.get("added")),
        "Bookshelves": _defused(", ".join(shelves)),
        "Exclusive Shelf": shelf,
        # From the evidence, never from --unknown-shelf: that flag fills one
        # column, and a fabricated read count told a tracker the reader had
        # finished 3,389 books they merely bought.
        "Read Count": "1" if entry.get("shelf") == READ else "",
        # Everything here is in the reader's library, which is what owning
        # a copy means to a tracker.
        "Owned Copies": "1",
    }
    return [str(cells.get(column, "")) for column in GOODREADS_COLUMNS]


def _cell(value: object) -> str:
    """
    Render a value that came from a book as one CSV cell.

    :param value: The title, author or collection name.

    :return: The value on one line, with control characters escaped, and
        never a formula. A title is input, and with ``--library-export -`` a
        cell goes straight to the terminal.
    """
    return _defused(printable(collapse(value)))


def _defused(text: str) -> str:
    """
    Stop a cell being read as a formula.

    Applied to the whole cell, never to a part of it. Only the first
    character of a cell can begin a formula, so prefixing each shelf in a
    joined list put an apostrophe inside the second and third shelf names and
    a tracker imported them under it.

    :param text: The cell, already collapsed and escaped.

    :return: The cell, prefixed with the apostrophe a spreadsheet reads as
        "this is text" when it would otherwise be a formula.
    """
    return "'" + text if text.startswith(FORMULA_TRIGGERS) else text


def _shelf_name(name: str) -> str:
    """
    Render a collection name as one shelf within the Bookshelves cell.

    Goodreads separates shelves with commas, so a comma inside a name would
    split it into two shelves. The JSON export carries the name untouched.

    Not defused here: it is a part of a cell, not a cell.

    :param name: The collection's name, as the reader typed it.

    :return: The shelf name.
    """
    return printable(collapse(name.replace(",", " ")))


def _quoted_number(digits: str | None) -> str:
    """
    Render an ISBN the way Goodreads does, or an empty cell.

    :param digits: The ISBN, or None.

    :return: ``="digits"``, or an empty string.
    """
    return f'="{digits}"' if digits else ""


def _goodreads_date(instant: object) -> str:
    """
    Render an instant as the ``YYYY/MM/DD`` Goodreads writes.

    :param instant: An RFC 3339 instant, or None.

    :return: The date, or an empty cell.
    """
    if not isinstance(instant, str):
        return ""
    return instant[:10].replace("-", "/")
