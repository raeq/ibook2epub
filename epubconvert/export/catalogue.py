"""
Write a library out, as a tracker reads it or as the schema describes it.

Two shapes over one catalogue. The Goodreads-format CSV is what The StoryGraph
and every other importer is built for, so its header is Goodreads' header and
its cells are spelled the way Goodreads spells them. The JSON is the canonical
record the CSV is a view of, and carries what the CSV has no column for.

Held apart from :mod:`epubconvert.collect.library`, which reads the database: this
module decides nothing about what a book *is*, only how to write one down.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Literal

from .. import __version__
from ..collect.coredata import now
from ..collect.library import READ
from ..collect.validate import isbn10_of, isbn13_of
from ..utils import schema
from ..utils.display import collapse, printable

#: The schema shipped beside this module, which is the contract.
SCHEMA_PATH = Path(__file__).with_name("library.schema.json")

#: The shapes an export can take.
LibraryFormat = Literal["csv", "json"]
LIBRARY_FORMATS: tuple[LibraryFormat, ...] = ("csv", "json")

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
