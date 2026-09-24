"""
What a note is, as a file: its markers, its regions and its digest.

Split from :mod:`epubconvert.export.notes`, which renders and rewrites a note,
so that :mod:`epubconvert.export.notenames` can read a note already in the
vault -- whose it is -- without importing the module that writes one.

A note is four regions: the reader's frontmatter, a start marker carrying a
digest of the generated region and the book the note is of, the generated
region, and the end marker with the reader's writing beneath it. Everything
here reads or renders that shape and nothing else.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

from ..utils.display import collapse
from .naming import disambiguator, encode_name

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
#: Carries the digest and the book the note is of: a digest of its asset id
#: (:func:`book_tags`). Notes written before the tag existed carry none and
#: are still read; each gains one the next time its region is rewritten.
TAGGED_TEMPLATE = "<!-- ibook2epub sha256={digest} book={book} -->"
#: Trailing white space is part of the pattern, not stripped by each caller:
#: an editor may leave some after the marker, and the one caller that did not
#: strip it -- the escaper -- let a forged marker with a trailing space through.
START_PATTERN = re.compile(
    r"^<!-- ibook2epub sha256=([0-9a-f]{16,64})(?: book=([0-9a-f]{8,64}))? -->\s*$"
)
#: The book tag within a start marker.
BOOK_TAG = re.compile(r" book=[0-9a-f]{8,64}(?= -->)")

#: Largest note this will read back. A note of a few hundred highlights is
#: tens of kilobytes; anything past this is a runaway or a planted file, and
#: reading it whole every run costs twice its size in memory. Mirrors
#: ``detached.MAX_EXPORT_BYTES``, which bounds the JSON export for the same
#: reason.
MAX_NOTE_BYTES = 8 * 1024 * 1024

#: How much of the digest to write. Enough that a collision is not a practical
#: concern and short enough to read.
DIGEST_LENGTH = 16


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
    #: The book the start marker is tagged for, or None for a note written
    #: before notes were tagged.
    book: str | None = None


def split(text: str) -> Split | None:
    """
    Divide an existing note into its four regions.

    Structural rather than semantic: the first start marker, and everything
    above it is the head. Deliberately not YAML parsing -- answering this from
    the frontmatter would mean parsing YAML a reader has edited, with nested
    maps, block scalars and plugin keys, and this project has no YAML reader
    nor should it acquire one it must then keep correct.

    The head is the reader's whatever it holds. The marker was looked for only
    straight after the closing ``---``, or on line 1 once the reader deleted
    the frontmatter, so a blank line or a ``Related: [[X]]`` above it called
    this tool's own note foreign, failed every run and never updated it
    again. The first marker is the real one: every line of the generated
    region that could pass for one is escaped
    (:func:`~epubconvert.export.notes._unforged`), and the frontmatter above
    it quotes every value.

    :param text: The file's contents, already decoded.

    :return: The regions, or None when this file is not one of ours.
    """
    lines = normalise(text).split("\n")
    start = first_start(lines)
    if start is None:
        return None
    index, found = start
    head = "\n".join(lines[:index]) + "\n" if index else ""
    return _regions(head, found, lines[index + 1 :])


def first_start(lines: list[str]) -> tuple[int, re.Match[str]] | None:
    """Find the first start marker, and the line it is on, if any line is one."""
    for index, line in enumerate(lines):
        found = START_PATTERN.match(line)
        if found:
            return index, found
    return None


def _regions(head: str, found: re.Match[str], rest: list[str]) -> Split | None:
    """
    Divide what follows the start marker at the end marker.

    :param head: Everything above the start marker, or "" when nothing is.
    :param found: The start marker.
    :param rest: Every line after it.

    :return: The regions, or None when this file is not one of ours.
    """
    for offset, line in enumerate(rest):
        if END_PATTERN.match(line.rstrip()):
            return Split(
                head,
                found.group(1),
                "\n".join(rest[:offset]) + "\n" if rest[:offset] else "",
                "\n".join(rest[offset:]),
                found.group(2),
            )
    # A missing end marker is treated as an edit. Skipping a note that may be
    # fine is recoverable; overwriting one that is not is not.
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

    Each line's trailing spaces and tabs are left out. Most editors trim
    them on save, so a digest over them made a note the reader had only
    opened and written beneath look edited, and every later highlight went
    into a sidecar.

    :param generated: The region between the markers.

    :return: The digest, truncated.
    """
    return _sha(trimmed(generated))


def _sha(text: str) -> str:
    """Digest a string, truncated to :data:`DIGEST_LENGTH`."""
    return hashlib.sha256(encode_name(text)).hexdigest()[:DIGEST_LENGTH]


def trimmed(text: str) -> str:
    """Strip the trailing spaces and tabs of every line, as an editor does."""
    return "\n".join(line.rstrip(TRAILING) for line in text.split("\n"))


#: What an editor trims from the end of a line, and so what the digest and
#: the generated region leave out.
TRAILING = " \t"

#: The lines an older version ended in a space of its own: a blank line in a
#: highlight, ``"> "``, a note's blank first line, ``"**Note:** "``, and the
#: heading of a title or a chapter of white space alone, ``"# "`` or
#: ``"## "``. Put back to check that version's digest once an editor has
#: trimmed them. A bare ``#`` or ``##`` line is never anything else in a
#: region: every line of a note that opens a heading is escaped, and the
#: first line of one follows its label.
WIDENED = (
    (re.compile(r"^>$", re.MULTILINE), "> "),
    (re.compile(r"^\*\*Note:\*\*$", re.MULTILINE), "**Note:** "),
    (re.compile(r"^(##?)$", re.MULTILINE), r"\1 "),
)


def untouched(generated: str, digest: str) -> bool:
    """
    Whether a region is as its digest says it was written.

    By this version, whose digest leaves out trailing white space, or by an
    older one, whose digest covered it: as the region stands, or with the
    spaces that version wrote itself put back, since an editor may since
    have trimmed them. Spaces that came with a highlight's own text cannot
    be put back, so an older note holding some, once trimmed, still reads
    as edited.

    :param generated: The region between the markers.
    :param digest: The digest the start marker carries.

    :return: True when no reader has changed the region.
    """
    if digest in (digest_of(generated), _sha(generated)):
        return True
    widened = trimmed(generated)
    for pattern, spaced in WIDENED:
        widened = pattern.sub(spaced, widened)
    return digest == _sha(widened)


def readable(target: Path) -> bool:
    """
    Whether this path is a note that can safely be read back.

    Two hazards a plain ``exists()`` misses. A FIFO left in the vault blocks
    ``read_text`` until a writer appears, which is never, and froze the whole
    run; ``is_file`` is False for one. And a file far larger than any real note
    costs twice its size in memory to read, so it is refused rather than read.

    :param target: The path about to be read.

    :return: True when it is an ordinary file of a plausible size.
    """
    try:
        if not target.is_file():
            return False
        return target.stat().st_size <= MAX_NOTE_BYTES
    except OSError:
        return False


def wrote_it(existing: str) -> bool:
    """
    Whether this tool wrote a note, however much the reader has since changed.

    Distinct from :func:`is_ours`, which asks the narrower question of whether
    the generated region is still untouched. Conflating the two told a reader
    that a note this tool had written "was not written by ibook2epub", and
    denied it the sidecar its edits had earned.

    :param existing: The note as it stands.

    :return: True when it carries this tool's start marker, wherever the
        reader's head leaves it.
    """
    return _start_marker_of(existing) is not None


def _start_marker_of(existing: str) -> re.Match[str] | None:
    """
    Find the start marker of a note this tool wrote.

    :param existing: The note as it stands.

    :return: The first start marker, or None.
    """
    start = first_start(normalise(existing).split("\n"))
    return None if start is None else start[1]


def book_tags(found: list[dict[str, Any]]) -> set[str]:
    """
    Name the book a note is of, as its start marker records it.

    A note's name is worked out afresh each run, and two routes once worked
    it out two ways: ``-ao`` wrote one edition's highlights over the note of
    the other edition, which held its name. The tag lets a note refuse a book
    that is not its own, whatever named it.

    Apple's asset id, digested as a marked name digests an identifier. It is
    on every annotation, read from the row itself, so it does not come and go
    with whether a package document could be read, as a ``dc:identifier``
    does; and a store book's id is a purchase number, which has no business
    in a reader's vault undigested.

    :param found: This book's annotations.

    :return: Every tag they carry: usually one, and none when no annotation
        names its asset.
    """
    tags = set()
    for item in found:
        book = item.get("book")
        asset = book.get("assetId") if isinstance(book, dict) else None
        if isinstance(asset, str) and asset:
            tags.add(disambiguator(asset))
    return tags


def start_marker(generated: str, book: str | None) -> str:
    """Render the start marker for a region, tagged for its book when known."""
    digest = digest_of(generated)
    if book is None:
        return START_TEMPLATE.format(digest=digest)
    return TAGGED_TEMPLATE.format(digest=digest, book=book)


def is_ours(existing: str) -> bool:
    """
    Whether this tool wrote a note and the reader has not touched its region.

    :param existing: The note as it stands.

    :return: True when the generated region is exactly as it was written.
    """
    held = split(existing)
    return held is not None and untouched(held.generated, held.digest)


def quoted(value: object) -> str:
    """
    Render a value as a double-quoted YAML scalar.

    Quoted always, never conditionally. 433 of 5,531 title and author values in
    a surveyed library break or change unquoted, 298 of them because they carry
    ``": "``, which starts a mapping and makes Obsidian show the note as having
    no properties at all -- silently, which is the worst way for it to fail.

    Characters YAML will not carry unescaped are escaped too. U+0092, which is
    what CP1252 mojibake leaves of an apostrophe, was written raw, and a
    single one made the whole frontmatter invalid: Obsidian dropped every
    property of the note, silently again.

    :param value: The value, which came from the book.

    :return: The quoted scalar.
    """
    escaped = collapse(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{UNPRINTABLE.sub(_yaml_escape, escaped)}"'


#: What YAML 1.2 (5.1) will not carry unescaped in a stream: C0 but for the
#: white space ``collapse`` has already turned into spaces, DEL, C1 but for
#: NEL, the surrogates a filename can hand back, and the two non-characters.
UNPRINTABLE = re.compile(
    r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f\ud800-\udfff\ufffe\uffff]"
)


def _yaml_escape(match: re.Match[str]) -> str:
    """Render one character as a double-quoted YAML escape: ``\\x92``."""
    code = ord(match.group())
    return f"\\x{code:02X}" if code <= 0xFF else f"\\u{code:04X}"


#: A double-quoted YAML scalar, and a single-quoted one, each alone on the
#: rest of its line but for a comment.
DOUBLE_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"[ \t]*(?:#.*)?')
SINGLE_QUOTED = re.compile(r"'((?:[^']|'')*)'[ \t]*(?:#.*)?")

#: One escape of a double-quoted scalar: a code point in hex, or one letter.
ESCAPE = re.compile(
    r"\\(?:x([0-9A-Fa-f]{2})|u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|(.))", re.DOTALL
)

#: YAML 1.2's one-letter escapes (5.7) but for the controls no identifier
#: holds; one not listed is left as written.
ESCAPED = {
    '"': '"',
    "\\": "\\",
    "/": "/",
    " ": " ",
    "t": "\t",
    "n": "\n",
    "N": "\x85",
    "_": "\xa0",
}

#: Where a plain scalar's comment starts: a hash after white space.
COMMENT = re.compile(r"[ \t]#")


def scalar(written: str) -> str:
    """
    Read the value of one frontmatter line, as YAML would.

    The inverse of :func:`quoted`, and of whatever else writes the
    frontmatter back: Obsidian's property editor and YAML linters write a
    plain scalar unquoted, or single-quote it. Only the three forms a value
    on one line takes; anything else is returned as it stands, so it
    compares equal to nothing it does not spell.

    :param written: What follows ``key:`` on the line.

    :return: The value.
    """
    written = written.strip()
    double = DOUBLE_QUOTED.fullmatch(written)
    if double:
        return ESCAPE.sub(_unescape, double.group(1))
    single = SINGLE_QUOTED.fullmatch(written)
    if single:
        return single.group(1).replace("''", "'")
    if written[:1] in ("'", '"'):
        return written
    return COMMENT.split(written, maxsplit=1)[0].rstrip()


def _unescape(match: re.Match[str]) -> str:
    """Read one escape of a double-quoted scalar; an unknown one stays."""
    code = next((group for group in match.groups()[:3] if group), None)
    if code is None:
        return ESCAPED.get(match.group(4), match.group())
    point = int(code, 16)
    return chr(point) if point <= sys.maxunicode else match.group()
