"""
Take highlights and notes out of Apple Books.

Apple keeps annotations in a Core Data SQLite database inside its own
container, keyed by an asset id that joins to a second database holding the
library: titles, authors, and where each package directory lives. Neither
schema is documented or promised, so every column read here is pinned by a
test built from a database the tests create.

**The shape of the output.** The W3C EPUB Annotations work is converging on
URL text fragments as the locator, and has explicitly ruled out EPUB CFI --
"which will rule out epubcfi (which, b.t.w., is bound to XHTML...)"
(w3c/epub-specs#2763). Its requirements open with wanting an annotation to
stay useful *after* you lose access to the publication, so each entry here
carries enough of its book to be cited on its own.

This deliberately runs ahead of that draft rather than claiming conformance to
it. The draft still has T.B.D. sections, and its normative dependency on text
fragments has not yet landed in HTML. What is emitted is shaped so that it can
become conformant later without the data being re-gathered.

The CFI is kept in its own field rather than thrown away. It is the only
locator that still points at the right place when the highlighted text appears
more than once in a book, which a text fragment cannot disambiguate.

**An annotation is the reader's own work.** Their selection, and their note
beside it. That holds for a DRM-protected book exactly as for any other: the
annotation was never inside the protected file. Apple keeps it in a separate
database, and the licensing of the book says nothing about who owns the
sentence somebody chose to mark. A book whose file cannot be converted is
skipped by the converter, and its highlights still come out in full. That is
the case where taking them with you matters most, because the book is the one
thing that cannot come with them.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .. import __version__
from ..utils import schema
from ..utils.app_logger import logger
from ..utils.contained import escapes
from ..utils.opf import Package
from ..utils.policy import NamingPolicy
from ..utils.spec import PACKAGE_SUFFIX
from .coredata import container_directory, database_in, moment, now, rows
from .library import describe_book, index_assets, package_of, read_package_once

#: What names standard output where a filename is expected. The convention
#: every other command-line tool uses, so it needs no explaining.
STDOUT = "-"

#: Apple's annotation type for a highlight. Others exist -- a bare bookmark,
#: and a per-book reading position -- but only this one carries text. Rather
#: than trust the number, rows are filtered on having text, which is the thing
#: that actually makes an annotation exportable.
HIGHLIGHT_TYPE = 2

#: A CFI as Apple Books stores one, bare or after a book's address and "#"
#: (EPUB CFI 1.1, section 3.2). Its body is what the parentheses hold.
CFI_FORM = re.compile(r"(?:[^#]*#)?epubcfi\((.*)\)", re.S)

#: The body up to its first indirection: the first "!" that is neither escaped
#: nor inside an assertion, where "!" is an ordinary character.
CFI_BEFORE_INDIRECTION = re.compile(r"((?:\^.|\[(?:\^.|[^\]^])*\]|[^!\[^])*)!", re.S)

#: The step that ends there, when it asserts an ID: "/", its index, then "["
#: and the ID -- plain characters and "^"-escaped specials -- and after it,
#: optionally, the assertion's text-location or parameter part (section 3.1).
CFI_DOCUMENT_STEP = re.compile(
    r"(?<!\^)/[0-9]+\[((?:\^[\^\[\](),;=]|[^\^\[\](),;=])*)"
    r"(?:[,;](?:\^.|[^\^\[\]])*)?\]\Z",
    re.S,
)

#: A CFI escape, and the character it stands for.
CFI_ESCAPE = re.compile(r"\^(.)", re.S)

#: Characters a text fragment leaves alone. The rest are percent-encoded.
FRAGMENT_SAFE = ""

#: Beyond this many characters a highlight is quoted by its two ends rather
#: than whole. A real 249-character highlight made a 341-character locator that
#: had to match a rendered DOM exactly; the WICG format has textStart,textEnd
#: for precisely this.
FRAGMENT_WHOLE_LIMIT = 60

#: Roughly how much of each end to quote when a highlight is too long to quote
#: whole. Trimmed to a word boundary, so the real length varies.
FRAGMENT_END_CHARS = 30


def text_fragment(text: str) -> str:
    """
    Render highlighted text as a URL text fragment.

    This is the locator the W3C work is converging on, and the reason no CFI
    resolution is needed to produce one: the highlighted text *is* the
    fragment. A browser can act on it directly.

    :param text: The highlighted text.

    :return: A ``:~:text=`` fragment, percent-encoded. Empty when the text
        carries nothing to locate, so no locator is recorded at all: an empty
        fragment matched the schema's pattern while selecting nothing.
    """
    collapsed = " ".join(text.split())
    if not collapsed:
        return ""
    if len(collapsed) <= FRAGMENT_WHOLE_LIMIT:
        return f":~:text={quote(collapsed, safe=FRAGMENT_SAFE)}"

    start = _leading_words(collapsed, FRAGMENT_END_CHARS)
    end = _trailing_words(collapsed, FRAGMENT_END_CHARS)
    # Both halves take their first word unconditionally, so text with no word
    # boundary -- Japanese, Thai, a long URL -- made each of them the whole
    # string and emitted "text=X,X", a range whose ends are the same. Quoting
    # it whole is long but correct, which the pair was not.
    if (
        start in (end, collapsed)
        or end == collapsed
        or len(start) + len(end) >= len(collapsed)
    ):
        return f":~:text={quote(collapsed, safe=FRAGMENT_SAFE)}"
    return (
        f":~:text={quote(start, safe=FRAGMENT_SAFE)},{quote(end, safe=FRAGMENT_SAFE)}"
    )


def _leading_words(text: str, budget: int) -> str:
    """
    Take whole words from the front, up to roughly *budget* characters.

    Whole words because a fragment that starts mid-word matches nothing.

    :param text: The collapsed highlight.
    :param budget: Roughly how many characters to take.

    :return: The leading words.
    """
    taken: list[str] = []
    for word in text.split():
        if taken and len(" ".join([*taken, word])) > budget:
            break
        taken.append(word)
    return " ".join(taken)


def _trailing_words(text: str, budget: int) -> str:
    """
    Take whole words from the back, up to roughly *budget* characters.

    :param text: The collapsed highlight.
    :param budget: Roughly how many characters to take.

    :return: The trailing words.
    """
    taken: list[str] = []
    for word in reversed(text.split()):
        if taken and len(" ".join([word, *taken])) > budget:
            break
        taken.insert(0, word)
    return " ".join(taken)


def _assertion_of(cfi: str) -> str | None:
    """
    Pull the ID assertion out of a CFI.

    It is the assertion on the step just before the *first* indirection: the
    spine step, whose target the rest of the CFI points inside. A later "!"
    enters something embedded in that document, an SVG or an iframe, and an
    assertion before it names an element there, not a manifest item. An
    assertion further left, as in ``/6[spine]/46!``, names no document either,
    so a spine step without one asserts nothing.

    :param cfi: The CFI Apple recorded.

    :return: What it asserts, unescaped, or None if it asserts nothing or is
        not a CFI.
    """
    form = CFI_FORM.fullmatch(cfi)
    if form is None:
        return None
    before = CFI_BEFORE_INDIRECTION.match(form.group(1))
    if before is None:
        return None
    step = CFI_DOCUMENT_STEP.search(before.group(1))
    if step is None:
        return None
    return CFI_ESCAPE.sub(r"\1", step.group(1)) or None


def _href_of(cfi: str, book: Package | None) -> str | None:
    """
    Resolve the document a CFI points into.

    The bracketed part of a CFI is an **ID assertion** -- the spine item's
    ``id`` attribute -- not its href. Four of five annotations in a surveyed
    library carry an id there; one carries a filename, which is what makes
    passing the assertion off as a path look right until it is checked.

    The book is at hand, so the id is resolved against its manifest. When it
    cannot be -- the book is gone, or the id is in no manifest -- the field is
    left out rather than filled with something that is not a path. Nothing is
    lost: the raw assertion is still in the ``cfi``.

    A manifest href is attacker-controlled: the book arrived from Apple or a
    sideload, and this field is published as "the path of the document within
    the book" for a consumer to join onto a book root. One that climbs out is
    refused on the same terms as the rest, and by the same rule.

    :param cfi: The CFI Apple recorded.
    :param book: The parsed package document, if it could be read.

    :return: The document's path within the book, or None.
    """
    assertion = _assertion_of(cfi)
    if assertion is None or book is None:
        return None
    href = book.manifest.get(assertion)
    if href is None or escapes(href):
        return None
    return href


def collect(
    container: Path | None = None, policy: NamingPolicy | None = None
) -> list[dict[str, Any]]:
    """
    Gather every exportable annotation.

    :param container: The Books container directory. Defaults to Apple's.
    :param policy: The naming policy this run is using, so each book can say
        what its file on the shelf is called. Without one no claim is made:
        the name belongs to the policy, and guessing it here is what let the
        two disagree.

    :return: Annotations, grouped by book title and ordered by when each was
        made within a book.

    :raises ContainerUnavailableError: If the annotation database is missing
        or unreadable; :class:`~.coredata.ContainerPermissionError`, a
        subclass, when macOS refuses access and the terminal needs Full Disk
        Access.
    """
    directory = container_directory(container)
    database = database_in(directory, "AEAnnotation", "annotation")
    library = index_assets(directory)
    found_rows = rows(
        database,
        "SELECT ZANNOTATIONUUID, ZANNOTATIONASSETID, ZANNOTATIONSTYLE,"
        " ZANNOTATIONSELECTEDTEXT,"
        " ZANNOTATIONNOTE, ZANNOTATIONLOCATION, ZFUTUREPROOFING5,"
        " ZANNOTATIONCREATIONDATE, ZANNOTATIONMODIFICATIONDATE"
        " FROM ZAEANNOTATION"
        " WHERE COALESCE(ZANNOTATIONDELETED, 0) = 0"
        "   AND ZANNOTATIONSELECTEDTEXT IS NOT NULL"
        "   AND ZANNOTATIONSELECTEDTEXT <> ''",
    )

    # Manifests are read lazily and kept, so a book with twenty highlights is
    # opened once and a library with none is never opened at all.
    parsed: dict[Path, Package | None] = {}
    found = []
    for row in found_rows:
        # One row at a time, because this was a comprehension and one
        # unusable row therefore cost every good one. Apple's columns are
        # untyped: a date that is a string, a style that is a colour name and
        # text that is a BLOB have all been seen.
        try:
            found.append(_annotation_of(row, library, parsed, policy))
        except (TypeError, ValueError, OSError) as exc:
            logger.warning(
                "Skipped an unreadable annotation (%s): %s",
                row["ZANNOTATIONUUID"] or "no id",
                exc,
            )
    # By book, then by when it was made. Reading order would be better and is
    # what w3c/epub-specs#3030 is about, but it needs the CFI resolved against
    # the book, which this deliberately does not do.
    found.sort(key=_reading_order)
    return found


def _reading_order(item: dict[str, Any]) -> tuple[str, bool, str]:
    """
    The order an export is written in: by book, then by when it was made.

    Written once because :func:`collect` and :func:`merge` both sort, and a
    tie-break added to one of two identical lambdas would have quietly given
    a merged file a different order from a fresh one.

    An annotation with no creation date sorts after the dated ones in its
    book. The empty string sorts before every date, which put them first;
    the order they had when the export stamped them with the clock was last.

    :param item: An annotation, of whatever shape a hand-edited file holds.

    :return: The sort key.
    """
    book = item.get("book")
    title = book.get("title") if isinstance(book, dict) else None
    created = str(item.get("created") or "")
    return (str(title or "").casefold(), not created, created)


def _annotation_of(
    row: sqlite3.Row,
    library: dict[str, dict[str, Any]],
    parsed: dict[Path, Package | None],
    policy: NamingPolicy | None,
) -> dict[str, Any]:
    """
    Turn one database row into one exportable annotation.

    :param row: The row, with Apple's column names.
    :param library: What the library database knows.
    :param parsed: Books already read, added to in place.
    :param policy: The naming policy, or None to make no claim about the shelf.

    :return: The annotation, omitting fields the row does not carry rather
        than sending them as empty.

    :raises TypeError: If the row holds something no annotation can be made
        from. Caught per row by :func:`collect`, so one bad row costs one
        annotation.
    """
    text = row["ZANNOTATIONSELECTEDTEXT"]
    if not isinstance(text, str):
        raise TypeError(f"selected text is {type(text).__name__}, not text")
    asset_id = row["ZANNOTATIONASSETID"] or ""
    known = library.get(asset_id, {})
    package = package_of(known)
    book = read_package_once(package, parsed) if package else None

    annotation: dict[str, Any] = {
        "id": row["ZANNOTATIONUUID"],
        "book": describe_book(asset_id, known, book, policy),
        "text": text,
    }
    created = moment(row["ZANNOTATIONCREATIONDATE"])
    if created:
        # Left out when Apple recorded none, as "modified" is. It used to be
        # stamped with the moment of export, which made an embedded set move
        # with the clock and every -ae -ar run rewrite that archive.
        annotation["created"] = created
    locator = text_fragment(text)
    if locator:
        annotation["locator"] = locator
    if row["ZANNOTATIONNOTE"]:
        annotation["note"] = row["ZANNOTATIONNOTE"]
    if row["ZFUTUREPROOFING5"]:
        annotation["chapter"] = row["ZFUTUREPROOFING5"]

    cfi = row["ZANNOTATIONLOCATION"]
    if cfi and isinstance(cfi, str):
        annotation["cfi"] = cfi
        href = _href_of(cfi, book)
        if href:
            annotation["href"] = href

    if row["ZANNOTATIONSTYLE"] is not None:
        # Carried opaquely or not at all. The number means a colour whose
        # mapping Apple has changed between releases, so it is worth nothing
        # next to losing the highlight it belongs to.
        with contextlib.suppress(TypeError, ValueError):
            annotation["style"] = int(row["ZANNOTATIONSTYLE"])
    modified = moment(row["ZANNOTATIONMODIFICATIONDATE"])
    if modified:
        annotation["modified"] = modified
    return annotation


def build_document(
    found: list[dict[str, Any]], *, stamped: bool = True
) -> dict[str, Any]:
    """
    Wrap annotations in the envelope the schema describes.

    :param found: What :func:`collect` returned.
    :param stamped: Whether to record the moment of generation. False for a
        set going inside an archive: that stamp moves on every run, and an
        archive whose bytes move with the clock stops deduplicating in a
        backup and can no longer be compared by hash. The schema makes the
        field optional for exactly this.

    :return: The document to serialise.
    """
    document: dict[str, Any] = {
        "$schema": SCHEMA_PATH.name,
        "generator": {"name": "ibook2epub", "version": __version__},
    }
    if stamped:
        document["generated"] = now()
    document["annotations"] = found
    return document


#: The schema shipped beside this module, which is the contract.
SCHEMA_PATH = Path(__file__).with_name("annotations.schema.json")


def schema_problems(document: dict[str, Any]) -> list[str]:
    """
    Check a document against the shipped schema.

    :param document: The document to check.

    :return: What is wrong with it, empty if nothing is.
    """
    return schema.document_problems(
        document, schema.load(SCHEMA_PATH), "annotations", "annotation"
    )


def merge(
    existing: dict[str, Any] | None, found: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    Fold a fresh reading into a file the reader already has.

    An export is something kept and added to, so a rerun must not throw away
    what is in it. Apple gives every annotation a UUID that survives a sync,
    which is what an entry is matched on.

    Three rules, and the third is the one worth knowing:

    - An annotation Apple no longer has is **kept**. Taking them with you is
      the point; losing one because Books lost it would defeat that.
    - One whose modification date has moved is regenerated.
    - Every annotation in a file written by a **different version** is
      regenerated, whether or not it changed. The locator here is ahead of a
      W3C draft that is still moving, so an entry written by an older version
      is not merely old, it may say something this version would not say. An
      orphan is exempt: it cannot be regenerated, so it is kept as it stands.

    :param existing: The document read back from the target file, or None on a
        first export.
    :param found: What :func:`collect` just read from Apple.

    :return: The annotations to write, and a tally of what happened to them.
    """
    # The file is the reader's and may have been hand-edited, so nothing below
    # the top level is assumed to be the shape this wrote. An entry that is
    # not an object with an id is not something a merge can reason about, and
    # dropping it here beats raising out of the write it was protecting.
    held = (existing or {}).get("annotations")
    previous = {
        item["id"]: item
        for item in (held if isinstance(held, list) else [])
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
    }
    generator = (existing or {}).get("generator")
    version = generator.get("version") if isinstance(generator, dict) else None
    same_version = version == __version__

    merged: list[dict[str, Any]] = []
    tally = {"added": 0, "updated": 0, "unchanged": 0, "kept": 0}

    for item in found:
        was = previous.pop(item["id"], None)
        if was is None:
            tally["added"] += 1
        elif same_version and _says_the_same(was, item):
            tally["unchanged"] += 1
            item = was
        else:
            tally["updated"] += 1
        merged.append(item)

    # Whatever is left was in the file and is not in Books any more.
    tally["kept"] = len(previous)
    merged.extend(previous.values())

    merged.sort(key=_reading_order)
    return merged, tally


def _says_the_same(was: dict[str, Any], item: dict[str, Any]) -> bool:
    """
    Whether a kept entry still says what a fresh reading would say.

    Apple's modification date settles the parts that come from Apple. It says
    nothing about the parts that come from *this run*: ``book.filename`` is
    computed from the naming policy, so after a ``--name-by`` change the old
    entry was stale and was still being reported as unchanged.

    :param was: The entry already in the file.
    :param item: The entry just read.

    :return: True if keeping the old one loses nothing.
    """
    if was.get("modified") != item.get("modified"):
        return False
    if was.get("created") != item.get("created"):
        # Not read from Apple in the way "modified" is: an unusable creation
        # date used to be stamped with the moment of export, so an entry
        # written that way differs from what this version would write and is
        # regenerated whether or not the version changed.
        return False
    held = was.get("book")
    fresh = item.get("book")
    old: dict[str, Any] = held if isinstance(held, dict) else {}
    new: dict[str, Any] = fresh if isinstance(fresh, dict) else {}
    return all(old.get(field) == new.get(field) for field in RUN_DEPENDENT_FIELDS)


#: Fields of ``book`` that this run works out rather than reads from Apple, so
#: a change to them is a change even when Apple says nothing moved. The author
#: and sort name are here because the package document fills them in when
#: Apple's row lacks them, and a package unreadable on one run is readable on
#: the next.
RUN_DEPENDENT_FIELDS = ("filename", "source", "identifier", "author", "authorSort")


#: Where the W3C work says an embedded annotation set lives. It needs no entry
#: in container.xml or the package manifest -- the location is the contract.
EMBEDDED_PATH = "META-INF/annotations.json"


def index_by_book(found: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """
    Group annotations by the key :func:`for_book` looks them up with.

    Built once per run rather than scanned per book. Scanning was O(books x
    annotations): measured over a 3,620-book library, 0.11s at 278 annotations
    but 7.41s at 20,000, against 0.004s for a lookup against this.

    :param found: Every annotation collected.

    :return: Lookup key to the annotations under it, each list in the order
        the annotations were given in.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for item in found:
        book = item.get("book")
        if not isinstance(book, dict):
            continue
        # A book the library database has forgotten has no source to match on
        # and falls back to its title. Weaker -- two books can share a title --
        # but the alternative was such a book being silently unembeddable
        # while looking like a book with no annotations.
        key = book.get("source") or _title_key(book.get("title"))
        if key:
            index.setdefault(str(key), []).append(item)
    return index


def _title_key(title: object) -> str:
    """Render a title as the key a package name would have had."""
    return f"{title}{PACKAGE_SUFFIX}" if title else ""


def for_book(
    source: str, index: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """
    Pick out the annotations belonging to one book.

    Matched on the package directory name, which is what ``book.source``
    records and what the converter knows about the book it is writing. The
    asset id would be a stronger key but the converter never sees one: it walks
    the library directory, not Apple's database.

    Two package directories with the same name in different subdirectories
    therefore look alike here. That ambiguity is settled by the caller, which
    is the only place that knows every package path; see
    :func:`~epubconvert.run.run._ambiguous_names`.

    :param source: The package directory name, e.g. ``Leviathan Wakes.epub``.
    :param index: What :func:`index_by_book` built.

    :return: Those belonging to this book, in the order collected.
    """
    return index.get(source, [])


def embedded_json(found: list[dict[str, Any]]) -> str:
    """
    Serialise an annotation set for storing inside a book.

    The same document shape as a detached export, so a consumer needs one
    reader rather than two, except that it carries no generation stamp: this
    text goes inside an archive, and an archive whose bytes move with the
    clock is no longer byte-reproducible.

    Returned as text rather than bytes: ``ZipFile.writestr`` encodes UTF-8
    itself, and encoding here would be a second way to turn a string into
    bytes, which the encoder rule exists to prevent.

    :param found: The annotations belonging to this book.

    :return: The document to store at :data:`EMBEDDED_PATH`.
    """
    return json.dumps(
        build_document(found, stamped=False), indent=2, ensure_ascii=False
    )
