"""
Render annotations as Markdown notes, one file per book.

The reader's vault is not like the other places this tool writes. Every other
output is a file only it writes; a note is a file the reader writes in too --
they link it, tag it, and write underneath a highlight to fold it into their own
thinking. Clobbering that is the one unrecoverable failure here, because nothing
upstream can regenerate what they wrote.

**The file is four regions, each with an owner.** The frontmatter belongs to the
reader and to Obsidian, which rewrites it whenever anyone adds a tag. The
generated body belongs to this tool. Everything below the end marker belongs to
the reader again. The hash covers only the region this tool owns, so tagging a
note or writing beneath it never makes the note un-updatable.

The start marker sits *below* the closing ``---`` and never above it: frontmatter
is recognised only at byte 0, so a comment on line 1 turns the fences into a
thematic break and every property disappears without an error.

The rest of this module is escaping. A blockquote does not neutralise a line
that opens a heading, and any string arriving here from a book or a reader could
otherwise forge the marker that ends the generated region.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

from . import exits
from .annotations import for_book, index_by_book
from .app_logger import logger
from .archive import write_atomically
from .display import printable
from .naming import encode_name
from .planning import Assignment

#: Ends the region this tool owns. Everything after it is the reader's and is
#: copied through untouched. Written from the first run even when there is
#: nothing beneath it, so the reader has a signposted place to write rather
#: than putting their first paragraph inside the generated region.
END_MARKER = "<!-- ibook2epub end — your notes below this line are never modified -->"

#: Matched on a stable prefix, so the human-readable tail above can be reworded
#: without orphaning every note already in a vault.
END_PATTERN = re.compile(r"^<!-- ibook2epub end")

#: Carries the digest of the generated region.
START_TEMPLATE = "<!-- ibook2epub sha256={digest} -->"
START_PATTERN = re.compile(r"^<!-- ibook2epub sha256=([0-9a-f]{16,64}) -->$")

#: How much of the digest to write. Enough that a collision is not a practical
#: concern and short enough to read.
DIGEST_LENGTH = 16

#: Characters that open a block element at the start of a line. A ``> `` prefix
#: does not neutralise them: inside a blockquote they still open a heading, a
#: list or a nested quote.
BLOCK_OPENERS = re.compile(r"^(\s*)([#>+*-]|\d+[.)])")

#: Frontmatter keys this tool owns, which are safe to emit bare because no book
#: supplies them.
LITERALS = {"category": "book", "tags": "[books]", "source": "ibook2epub"}


def _collapse(value: object) -> str:
    """
    Render a value as one line.

    ``usable_title`` trims but leaves internal newlines alone, so a title
    carrying one used to split the ``#`` heading and drop its second line into
    the body unguarded.

    :param value: Whatever the book or the reader supplied.

    :return: The value with its whitespace collapsed to single spaces.
    """
    return " ".join(str(value).split())


def _escape(line: str) -> str:
    """
    Make one line safe to emit into the generated region.

    Two hazards, and they are the same hazard: a line that means something
    structural where only text was intended.

    :param line: One line of book- or reader-derived text.

    :return: The line, escaped.
    """
    # A forged end marker would hand the rest of the generated body to the
    # reader's region on the next run. Highlights are already safe because
    # every line carries "> ", but nothing else was.
    if END_PATTERN.match(line) or START_PATTERN.match(line):
        return "\\" + line
    return BLOCK_OPENERS.sub(r"\1\\\2", line, count=1)


def _lines(value: object) -> list[str]:
    """
    Split a value into escaped lines, newlines normalised.

    :param value: Whatever the book or the reader supplied.

    :return: The lines, each safe to emit.
    """
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return [_escape(line) for line in text.split("\n")]


def _quoted(value: object) -> str:
    """
    Render a value as a double-quoted YAML scalar.

    Quoted always, never conditionally. 433 of 5,531 title and author values in
    a surveyed library break or change unquoted, 298 of them because they carry
    ``": "``, which starts a mapping and makes Obsidian show the note as having
    no properties at all -- silently, which is the worst way for it to fail.

    :param value: The value, which came from the book.

    :return: The quoted scalar.
    """
    escaped = _collapse(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def frontmatter(book: dict[str, Any]) -> str:
    """
    Render the YAML block that heads a note.

    Written once and never rewritten, because the reader and Obsidian own it
    from the moment it lands. A corrected title upstream will not propagate into
    an existing note; deleting the note is the refresh. That is the price of the
    reader keeping their tags and aliases.

    :param book: The ``book`` object an annotation carries.

    :return: The block, fences included, ending in a newline.

    :raises TypeError: If ``year`` is not an integer. It cannot be --
        ``_book_of`` suppresses the conversion error and omits the key -- and
        the guarantee is enforced here because here is where it is relied on.
    """
    lines = ["---"]
    for key in ("title", "author", "identifier"):
        if book.get(key):
            lines.append(f"{key}: {_quoted(book[key])}")
    isbn = _isbn_of(book.get("identifier"))
    if isbn:
        lines.append(f"isbn: {_quoted(isbn)}")
    if book.get("language"):
        lines.append(f"language: {_quoted(book['language'])}")
    if "year" in book:
        year = book["year"]
        if not isinstance(year, int) or isinstance(year, bool):
            raise TypeError(f"year must be an integer, got {type(year).__name__}")
        lines.append(f"year: {year}")
    lines.extend(f"{key}: {value}" for key, value in LITERALS.items())
    lines.append("---")
    return "\n".join(lines) + "\n"


def _isbn_of(identifier: object) -> str | None:
    """
    Take the bare ISBN out of a canonical identifier, when it is one.

    Derived by stripping the prefix rather than looked up again, so it cannot
    disagree with the field it came from. About 41% of books have one: 1,108
    ``urn:isbn`` against 1,448 ``urn:uuid`` in a surveyed library.

    :param identifier: The canonical identifier, or None.

    :return: The digits, or None when the book is identified some other way.
    """
    if isinstance(identifier, str) and identifier.startswith("urn:isbn:"):
        return identifier[len("urn:isbn:") :]
    return None


def body(found: list[dict[str, Any]]) -> str:
    """
    Render the generated region: the title, the author, and the highlights.

    Chapters become ``##`` headings, emitted whenever the chapter changes rather
    than grouping every chapter's highlights at its first appearance. A reader
    who returns to chapter three after chapter nine then gets a repeated heading
    instead of reordered highlights: the order is theirs, and a repeated heading
    is honest about what happened.

    :param found: This book's annotations, in reading order.

    :return: The region, ending in a newline.
    """
    book = found[0].get("book", {}) if found else {}
    lines = [f"# {_collapse(book.get('title', 'Unknown book'))}"]
    if book.get("author"):
        lines.append(f"*{_collapse(book['author'])}*")

    chapter: object = object()  # Never equal to a real chapter, so the first
    for item in found:  # one always prints.
        current = item.get("chapter") or "Highlights"
        if current != chapter:
            chapter = current
            lines.extend(["", f"## {_collapse(current)}"])
        lines.append("")
        lines.extend(f"> {line}" for line in _lines(item.get("text", "")))
        if item.get("note"):
            lines.append("")
            note = _lines(item["note"])
            lines.append(f"**Note:** {note[0]}")
            lines.extend(note[1:])
    return "\n".join(lines) + "\n"


class Split(NamedTuple):
    """One note, divided into the regions that have different owners."""

    #: Everything up to the start marker, frontmatter included. The reader's.
    head: str
    #: The digest the start marker carries.
    digest: str
    #: Between the markers. This tool's, and the only hashed part.
    generated: str
    #: The end marker and everything after it. The reader's.
    tail: str


def split(text: str) -> Split | None:
    """
    Divide an existing note into its four regions.

    Structural rather than semantic: line 1 is ``---``, scan to the next
    ``---``, the next line is the start marker. Deliberately not YAML parsing --
    answering this from the frontmatter would mean parsing YAML a reader has
    edited, with nested maps, block scalars and plugin keys, and this project
    has no YAML reader nor should it acquire one it must then keep correct.

    :param text: The file's contents, already decoded.

    :return: The regions, or None when this file is not one of ours.
    """
    lines = normalise(text).split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].rstrip() != "---":
            continue
        if index + 1 >= len(lines):
            return None
        found = START_PATTERN.match(lines[index + 1].rstrip())
        if not found:
            return None
        head = "\n".join(lines[: index + 1]) + "\n"
        rest = lines[index + 2 :]
        for offset, line in enumerate(rest):
            if END_PATTERN.match(line.rstrip()):
                return Split(
                    head,
                    found.group(1),
                    "\n".join(rest[:offset]) + "\n" if rest[:offset] else "",
                    "\n".join(rest[offset:]),
                )
        # A missing end marker is treated as an edit. Skipping a note that may
        # be fine is recoverable; overwriting one that is not is not.
        return None
    return None


def normalise(text: str) -> str:
    """
    Put a file's line endings back the way this tool writes them.

    A note that has round-tripped through iCloud, a Windows editor or a
    non-Obsidian tool comes back with CRLF endings, and without this it is
    reported as *not written by ibook2epub* -- about a file ibook2epub wrote.
    The byte-order mark is handled by reading with ``utf-8-sig``.

    :param text: The file's contents.

    :return: The contents with newlines normalised.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def digest_of(generated: str) -> str:
    """
    Digest the generated region, and only that region.

    Not to end of file: the reader's writing lives below the end marker and must
    not change whether the note is recognised. Not from byte 0 either: Obsidian
    rewrites frontmatter whenever anyone adds a tag, and tagging a new note is
    the first thing a reader does.

    Through ``encode_name`` rather than a bare ``.encode()``: it is the one
    sanctioned way this package turns a string into bytes, and it tolerates the
    lone surrogates an undecodable name carries. A second encoder here would
    grow the rule's exception list, which is what makes such a rule rot.

    :param generated: The region between the markers.

    :return: The digest, truncated.
    """
    return hashlib.sha256(encode_name(generated)).hexdigest()[:DIGEST_LENGTH]


def compose(found: list[dict[str, Any]], tail: str | None = None) -> str:
    """
    Render a whole note.

    :param found: This book's annotations, in reading order.
    :param tail: The reader's region to carry across, or None for a new note.

    :return: The file's contents.
    """
    generated = body(found)
    marker = START_TEMPLATE.format(digest=digest_of(generated))
    book = found[0].get("book", {}) if found else {}
    below = tail if tail is not None else f"{END_MARKER}\n"
    return f"{frontmatter(book)}{marker}\n{generated}{below}"


def rewrite(existing: str, found: list[dict[str, Any]]) -> str:
    """
    Re-render a note this tool wrote, keeping both of the reader's regions.

    :param existing: The note as it stands.
    :param found: This book's annotations, in reading order.

    :return: The note with only the generated region replaced.
    """
    held = split(existing)
    if held is None:
        raise ValueError("not a note this tool wrote")
    generated = body(found)
    marker = START_TEMPLATE.format(digest=digest_of(generated))
    return f"{held.head}{marker}\n{generated}{held.tail}"


def is_ours(existing: str) -> bool:
    """
    Whether this tool wrote a note and the reader has not touched its region.

    :param existing: The note as it stands.

    :return: True when the generated region is exactly as it was written.
    """
    held = split(existing)
    return held is not None and digest_of(held.generated) == held.digest


def write_vault(
    found: list[dict[str, Any]],
    destination: str,
    named: Sequence[Assignment],
) -> int:
    """
    Write one Markdown note per annotated book, into a vault.

    A note is a file the reader writes in too, so this preserves both of the
    regions that are theirs -- the frontmatter, which Obsidian rewrites whenever
    anyone adds a tag, and everything below the end marker. Only the generated
    region between the markers is ever replaced, and only when it changed.

    :param found: Every annotation this run read.
    :param destination: The directory to write into.
    :param named: The names the run gave every book, so a note and its epub
        share a stem. Named once with ``.epub`` and the suffix swapped here:
        naming again with ``.md`` clamps a long title against a budget two
        bytes larger, so a book near the limit would get a stem its epub
        never had.

    :return: A process exit code.
    """
    directory = Path(destination)
    if directory.is_file():
        logger.critical(
            "%s is a file; --annotations-format markdown writes one note per "
            "book and needs a directory.",
            printable(str(directory)),
        )
        return exits.NO_OUTPUT
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.critical("Could not create %s: %s", printable(str(directory)), exc)
        return exits.NO_OUTPUT

    index = index_by_book(found)
    written = kept = foreign = 0
    for item in named:
        if not item.filename:
            continue
        mine = for_book(item.package.name, index)
        if not mine:
            continue
        target = directory / (Path(item.filename).stem + ".md")
        outcome = _write_one(target, mine)
        written += outcome == "written"
        kept += outcome == "kept"
        foreign += outcome == "foreign"

    logger.info("Wrote %d note(s) to %s.", written, printable(str(directory)))
    if kept:
        logger.warning(
            "%d note(s) you have edited were left alone; their new highlights "
            "are in *.new.md beside them.",
            kept,
        )
    if foreign:
        logger.warning(
            "%d file(s) were not written by ibook2epub and were left alone.",
            foreign,
        )
    return exits.SUCCESS


def _write_one(target: Path, mine: list[dict[str, Any]]) -> str:
    """
    Put one book's note in place, without touching what the reader wrote.

    :param target: The note's path.
    :param mine: This book's annotations, in reading order.

    :return: ``written``, ``kept`` (the reader edited it), ``foreign`` (not
        ours), or ``unchanged``.
    """
    try:
        existing = target.read_text(encoding="utf-8-sig") if target.exists() else None
    except OSError as exc:
        logger.error("Could not read %s: %s", printable(target.name), exc)
        return "foreign"

    if existing is None:
        write_atomically(target, compose(mine))
        return "written"

    if split(existing) is None:
        return "foreign"
    if not is_ours(existing):
        # Edited inside the generated region, so it is left exactly as it is.
        # The sidecar is a note like any other and gets the same treatment one
        # level down, so a sidecar the reader has partly merged into survives
        # too -- the rule that protects the note protects its sidecar.
        _write_one(target.with_name(target.stem + ".new.md"), mine)
        return "kept"

    rewritten = rewrite(existing, mine)
    if rewritten == normalise(existing):
        return "unchanged"
    write_atomically(target, rewritten)
    return "written"
