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
more than once, or when the text must not be reproduced at all -- a live
question for DRM-protected books, still unsettled in that same thread.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import __version__
from .app_logger import logger
from .contained import escapes
from .naming import NamingPolicy
from .spec import PACKAGE_SUFFIX
from .validate import (
    Package,
    ValidationError,
    canonical_identifier,
    read_package_dir,
    usable_identifier,
)

#: What names standard output where a filename is expected. The convention
#: every other command-line tool uses, so it needs no explaining.
STDOUT = "-"


#: Where Apple keeps the two databases, relative to the user's home.
CONTAINER = Path(
    "Library/Containers/com.apple.iBooksX/Data/Documents",
)

#: Core Data counts seconds from 2001-01-01, not from the Unix epoch.
APPLE_EPOCH_OFFSET = 978307200

#: Apple's annotation type for a highlight. Others exist -- a bare bookmark,
#: and a per-book reading position -- but only this one carries text. Rather
#: than trust the number, rows are filtered on having text, which is the thing
#: that actually makes an annotation exportable.
HIGHLIGHT_TYPE = 2

#: The document a CFI names, when it names one: the bracketed assertion in the
#: step before the "!" that separates the spine path from the path within it.
#: Anchored on the *last* such assertion. Taking the first matched a spine-level
#: assertion in ``/6[spine]/46[ch15.xhtml]!``, which resolves to nothing.
CFI_DOCUMENT = re.compile(r"\[([^\]]+)\](?=[^!\[\]]*!)")

#: The one way an instant is written here, matching the schema's pattern.
INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

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


class AnnotationsUnavailableError(RuntimeError):
    """Raised when Apple's databases cannot be found or read."""


def _newest(directory: Path, prefix: str) -> Path | None:
    """
    Find the database Apple is currently using.

    The filenames carry a version and a build stamp, and old ones are left in
    place across upgrades, so the name cannot be hard-coded and the newest is
    the live one.

    :param directory: The container subdirectory to look in.
    :param prefix: The filename prefix to match.

    :return: The most recently modified match, or None if there is none.
    """
    # Stat inside the sort key raised on a dangling symlink among the
    # candidates, out of a function whose whole contract is "or None". Books
    # leaves old files here across upgrades, which is why the glob exists.
    dated: list[tuple[float, Path]] = []
    for path in directory.glob(f"{prefix}*.sqlite"):
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not dated:
        return None
    return max(dated, key=lambda pair: pair[0])[1]


def _rows(database: Path, query: str) -> list[sqlite3.Row]:
    """
    Run one query against a database, without writing to it.

    Opened read-only through a URI so that reading somebody's library cannot
    modify it, and so that a live Books process holding the write lock does not
    stop the export.

    :param database: The database file.
    :param query: The query to run.

    :return: The rows, as mappings.

    :raises AnnotationsUnavailableError: If the database cannot be read.
    """
    # as_uri() percent-encodes "?", "#" and "%" itself. Building the URI by
    # concatenation let a filename carrying "?" append its own parameters
    # ahead of mode=ro -- a read path that could open the database writable.
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise AnnotationsUnavailableError(f"could not open {database.name}") from exc
    try:
        connection.row_factory = sqlite3.Row
        return list(connection.execute(query))
    except sqlite3.Error as exc:
        # A schema change in a Books update lands here rather than as a
        # traceback: the columns below are Apple's, and Apple never promised
        # them.
        raise AnnotationsUnavailableError(
            f"{database.name} is not shaped as expected: {exc}"
        ) from exc
    finally:
        connection.close()


def _moment(seconds: float | None) -> str | None:
    """
    Render a Core Data timestamp as UTC.

    :param seconds: Seconds since 2001-01-01, or None.

    :return: An RFC 3339 instant ending in ``Z``, or None.
    """
    # Apple's column is untyped and undocumented. A string, a NaN or a value
    # past the year 9999 all reach here, and every one of them used to take
    # the whole export down with it.
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    try:
        when = datetime.fromtimestamp(seconds + APPLE_EPOCH_OFFSET, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return when.strftime(INSTANT_FORMAT)


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

    :param cfi: The CFI Apple recorded.

    :return: What it asserts, or None if it asserts nothing.
    """
    found = CFI_DOCUMENT.findall(cfi)
    return found[-1] if found else None


def _read(package: Path, parsed: dict[Path, Package | None]) -> Package | None:
    """
    Parse a book once, however many annotations point into it.

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


def _library(directory: Path) -> dict[str, dict[str, Any]]:
    """
    Read the library database, keyed by the id annotations join on.

    :param directory: The container directory.

    :return: Asset id to book metadata. Empty if the library cannot be read,
        because losing every highlight is worse than losing every title.
    """
    database = _newest(directory / "BKLibrary", "BKLibrary")
    if database is None:
        return {}
    try:
        rows = _rows(
            database,
            "SELECT ZASSETID, ZTITLE, ZAUTHOR, ZLANGUAGE, ZYEAR, ZPATH "
            "FROM ZBKLIBRARYASSET",
        )
    except AnnotationsUnavailableError as exc:
        logger.warning(
            "Reading the Books library failed, so titles are missing: %s", exc
        )
        return {}
    return {row["ZASSETID"]: dict(row) for row in rows if row["ZASSETID"]}


def _book_of(
    asset_id: str,
    library: dict[str, dict[str, Any]],
    parsed: Package | None,
    policy: NamingPolicy | None,
) -> dict[str, Any]:
    """
    Describe the book an annotation belongs to.

    :param asset_id: Apple's id for the book.
    :param library: What the library database knows.
    :param parsed: The book's package document, if it could be read.
    :param policy: The naming policy, or None to make no claim about the shelf.

    :return: The book, with whatever fields are known. Falls back to the asset
        id as a title, so an annotation whose book the library has forgotten is
        still exported.
    """
    row = library.get(asset_id, {})
    book: dict[str, Any] = {"title": row.get("ZTITLE") or asset_id or UNKNOWN_BOOK}
    if row.get("ZAUTHOR"):
        book["author"] = row["ZAUTHOR"]
    if row.get("ZLANGUAGE"):
        book["language"] = row["ZLANGUAGE"]
    if row.get("ZYEAR"):
        # Apple's column is untyped, and a date-shaped value in it used to
        # raise out of the whole export.
        with contextlib.suppress(TypeError, ValueError):
            book["year"] = int(row["ZYEAR"])
    source = _source_name(row.get("ZPATH"))
    if source is not None:
        book["source"] = source
        if policy is not None:
            # Asked of the policy, never worked out here. Under --name-by
            # author-title a book read from "Leviathan Wakes.epub" is written
            # as "Corey, James S.A. - Leviathan Wakes.epub", and an annotation
            # naming the wrong one cannot be traced back to its book.
            book["filename"] = policy.filename(source, parsed)
    # The publication's own key, and the only one here that is neither Apple's
    # nor this run's: a title, a package name and a shelf filename can all
    # change, and after any of them the annotation can still be matched to its
    # book by this. Asked of validate rather than re-derived, because knowing
    # which dc:identifier counts -- and that "none" identifies 92 books in a
    # real library -- is that module's job.
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


#: What a book with neither a title nor an asset id is called. The schema
#: requires a non-empty title, and an entry with no identity at all is worse
#: than one that admits it has none.
UNKNOWN_BOOK = "Unknown book"


def _source_name(path: object) -> str | None:
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

    :raises AnnotationsUnavailableError: If the annotation database is missing
        or unreadable. On macOS this usually means the terminal has not been
        granted Full Disk Access.
    """
    directory = container if container is not None else Path.home() / CONTAINER
    if not directory.is_dir():
        raise AnnotationsUnavailableError(
            f"{directory} is not there; Apple Books may never have run here"
        )

    database = _newest(directory / "AEAnnotation", "AEAnnotation")
    if database is None:
        raise AnnotationsUnavailableError(
            f"no annotation database under {directory / 'AEAnnotation'}; on macOS "
            "this usually means the terminal needs Full Disk Access"
        )

    library = _library(directory)
    rows = _rows(
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
    for row in rows:
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


def _reading_order(item: dict[str, Any]) -> tuple[str, str]:
    """
    The order an export is written in: by book, then by when it was made.

    Written once because :func:`collect` and :func:`merge` both sort, and a
    tie-break added to one of two identical lambdas would have quietly given
    a merged file a different order from a fresh one.

    :param item: An annotation, of whatever shape a hand-edited file holds.

    :return: The sort key.
    """
    book = item.get("book")
    title = book.get("title") if isinstance(book, dict) else None
    return (str(title or "").casefold(), str(item.get("created") or ""))


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
    held = library.get(row["ZANNOTATIONASSETID"] or "", {}).get("ZPATH")
    package = Path(held) if isinstance(held, str) and held else None
    book = _read(package, parsed) if package else None

    annotation: dict[str, Any] = {
        "id": row["ZANNOTATIONUUID"],
        "book": _book_of(row["ZANNOTATIONASSETID"] or "", library, book, policy),
        "text": text,
        "created": _moment(row["ZANNOTATIONCREATIONDATE"]) or _now(),
    }
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
    modified = _moment(row["ZANNOTATIONMODIFICATIONDATE"])
    if modified:
        annotation["modified"] = modified
    return annotation


def _now() -> str:
    """Return this instant as UTC, to the second, ending in ``Z``."""
    return datetime.now(tz=timezone.utc).strftime(INSTANT_FORMAT)


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
        document["generated"] = _now()
    document["annotations"] = found
    return document


#: The schema shipped beside this module, which is the contract.
SCHEMA_PATH = Path(__file__).with_name("annotations.schema.json")


def schema_problems(document: dict[str, Any]) -> list[str]:
    """
    Check a document against the shipped schema.

    Deliberately not a JSON Schema library: this tool has no runtime
    dependencies, and the parts of the schema worth enforcing at runtime are
    the required fields and the two patterns a consumer will actually rely on.
    The full schema is for consumers, who may use whatever validator they like.

    The checks are *derived* from the schema rather than restated beside it.
    Restating them is how the nested ``book`` came to be unchecked: the schema
    required a title there and this function never looked, so a book with no
    identity at all passed the tool's own validator.

    :param document: The document to check.

    :return: What is wrong with it, empty if nothing is.
    """
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    problems: list[str] = []

    for name in schema["required"]:
        if name not in document:
            problems.append(f"missing {name}")

    instant = re.compile(schema["properties"]["generated"]["pattern"])
    if "generated" in document and not instant.match(document["generated"]):
        problems.append(f"generated is not an instant: {document['generated']!r}")

    item = schema["$defs"]["annotation"]
    for index, annotation in enumerate(document.get("annotations", [])):
        where = f"annotations[{index}]"
        problems.extend(_object_problems(annotation, item, where))
        if isinstance(annotation, dict) and isinstance(annotation.get("book"), dict):
            problems.extend(
                _object_problems(
                    annotation["book"], schema["$defs"]["book"], f"{where}.book"
                )
            )
    return problems


def _object_problems(value: Any, rules: dict[str, Any], where: str) -> list[str]:
    """
    Check one object against one schema definition.

    The three constraints worth enforcing at runtime, taken from the schema
    itself: what must be present, what may be present, and the patterns and
    minimum lengths a consumer will rely on.

    :param value: The object to check.
    :param rules: The schema definition it should satisfy.
    :param where: What to call it in a message.

    :return: What is wrong with it.
    """
    if not isinstance(value, dict):
        return [f"{where} is {type(value).__name__}, not an object"]

    problems: list[str] = []
    for name in rules.get("required", []):
        if name not in value:
            problems.append(f"{where} missing {name}")

    properties = rules.get("properties", {})
    for name, rule in properties.items():
        if name not in value:
            continue
        held = value[name]
        pattern = rule.get("pattern")
        if pattern and not re.match(pattern, str(held)):
            problems.append(f"{where}.{name} does not match {pattern}")
        minimum = rule.get("minLength")
        if minimum is not None and len(str(held)) < minimum:
            problems.append(f"{where}.{name} is shorter than {minimum}")

    if rules.get("additionalProperties") is False:
        extra = set(value) - set(properties)
        if extra:
            problems.append(f"{where} has unknown {sorted(extra)}")
    return problems


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
    held = was.get("book")
    fresh = item.get("book")
    old: dict[str, Any] = held if isinstance(held, dict) else {}
    new: dict[str, Any] = fresh if isinstance(fresh, dict) else {}
    return all(old.get(field) == new.get(field) for field in RUN_DEPENDENT_FIELDS)


#: Fields of ``book`` that this run works out rather than reads from Apple, so
#: a change to them is a change even when Apple says nothing moved.
RUN_DEPENDENT_FIELDS = ("filename", "source", "identifier")


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
    :func:`~epubconvert.run.ambiguous_names`.

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
