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

from ..collect.annotations import for_book
from ..collect.identifiers import isbn13_of
from ..utils import exits
from ..utils.app_logger import logger
from ..utils.display import collapse, printable
from ..utils.policy import Assignment
from .archive import index_by_package, write_atomically
from .naming import (
    MAX_FILENAME_BYTES,
    disambiguator,
    encode_name,
    truncate_bytes,
)
from .notenames import note_names

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

#: Suffix for the copy written when a reader has edited the note itself. Not
#: ``.new.md``: a book titled "Foo.new" is named ``Foo.new.md``, which was
#: exactly the sidecar name for a book titled "Foo", so one book's sidecar
#: overwrote another book's note. This suffix ends in ``.md.new`` instead, so
#: it is not a name any naming policy can produce and not a file a later run
#: will adopt as some book's note.
SIDECAR_SUFFIX = ".md.new"

#: How much of the digest to write. Enough that a collision is not a practical
#: concern and short enough to read.
DIGEST_LENGTH = 16

#: What opens a block element at the start of a line. A ``> `` prefix does not
#: neutralise it: inside a blockquote it still opens a heading, a list or a
#: nested quote. A note's continuation lines have no prefix at all, and only
#: ``# > + * -`` and list numbers were caught, so an unclosed fence or
#: ``<!--`` in a note swallowed every highlight after it, ``===`` turned the
#: line above into a heading, and a link reference definition vanished.
#:
#: Each opener is matched as CommonMark (4.1-4.9, 5.1-5.2) and GFM tables
#: define it, not by its first character, because every backslash shows in
#: Obsidian's source view and an escaped ``[[`` is no longer a link:
#: ``~~struck~~``, ``_emphasis_`` and ``<3`` open nothing and are left alone.
#: ``#``, ``*``, ``-``, ``+`` and list numbers were still matched by their
#: first character, so ``**bold** start`` became ``\**bold**``, which renders
#: a literal star and an emphasised ``bold*``; ``#idea`` lost its Obsidian
#: tag; ``2.5 million``, ``-5 degrees`` and ``+1 agreed`` showed a backslash.
#: A heading's hashes, a bullet and a list number open a block only when white
#: space or the end of the line follows them. ``*`` and ``-`` also open a
#: thematic break, and ``-`` a setext underline; each of those is spelled
#: out, as ``_`` and ``=`` already were. A table's delimiter row may lead with
#: ``-`` or with an alignment colon, and ``:-- | --:`` under a note's first
#: line once went unescaped and put its ``**Note:**`` label in a table header;
#: the row is matched by its whole shape, cells and pipes. A link label
#: may continue onto the next line, so an unfinished one counts. ``>`` opens a
#: quote whatever follows it.
#:
#: Indentation is up to three spaces and nothing else. A fourth column, or a
#: tab, opens an indented code block (``code``); a line led by any other white
#: space opens nothing, and escaping behind one showed the backslash. An
#: ordered list is numbered in one to nine ASCII digits (CommonMark 5.2);
#: ``\d`` also matches digits in other scripts, which open nothing, and a
#: tenth digit makes the line text.
BLOCK_OPENERS = re.compile(
    r"""
    ^(?:
        (?P<code>(?:\ {0,3}\t|\ {4})[ \t]*)(?=[^ \t])   # indented code
      | (?P<indent>\ {0,3})
        (?:
            (?P<mark>
                \#(?=\#{0,5}(?:[ \t]|$))      # ATX heading
              | >                            # block quote
              | [-+*](?=[ \t]|$)             # bullet list item
              | \*(?=(?:[ \t]*\*){2}[ \t*]*$)  # thematic break
              | -(?=(?:[ \t]*-){2}[ \t-]*$)    # thematic break
              | -(?=-*[ \t]*$)               # setext underline
              | (?=:?-+:?[ \t]*\|(?:[ \t]*:?-+:?[ \t]*\|)*(?:[ \t]*:?-+:?)?[ \t]*$)
                [-:]                         # table delimiter row, no pipe first
              | \|                           # table row
              | `(?=``) | ~(?=~~)            # code fence
              | <(?=[A-Za-z/!?])            # HTML block, either marker included
              | =(?==*[ \t]*$)              # setext underline
              | _(?=(?:[ \t]*_){2}[ \t_]*$)  # thematic break
              | \[(?=(?:[^\[\]\\]|\\.)*(?:\]:|\\?$))  # link reference definition
            )
          | (?P<number>[0-9]{1,9})(?P<delimiter>[.)])(?=[ \t]|$)  # ordered list
        )
    )
    """,
    re.VERBOSE,
)

#: What stands in for a column of indentation. CommonMark counts only spaces
#: and tabs as indentation, so a no-break space keeps an indented line where
#: the reader put it without opening a code block.
INDENT = "\u00a0"

#: CommonMark's tab stop, for turning a tab into columns.
TAB_WIDTH = 4

#: Frontmatter keys this tool owns, which are safe to emit bare because no book
#: supplies them.
LITERALS = {"category": "book", "tags": "[books]", "source": "ibook2epub"}


def _escape(line: str) -> str:
    """
    Make one line safe to emit into the generated region.

    Two hazards, and they are the same hazard: a line that means something
    structural where only text was intended.

    :param line: One line of book- or reader-derived text.

    :return: The line, escaped.
    """
    unforged = _unforged(line)
    if unforged != line:
        return unforged
    # CommonMark escapes only ASCII punctuation, so the backslash goes on the
    # opener's punctuation: in front of a list number's digits it escapes
    # nothing and shows, as "\1. first". "1\. first" is the literal text.
    return BLOCK_OPENERS.sub(_escape_opener, line, count=1)


def _unforged(line: str) -> str:
    """
    Neutralise a line that would pass for one of this tool's markers.

    A forged end marker would hand the rest of the generated body to the
    reader's region on the next run. Highlights are already safe because every
    line carries "> ", but nothing else was.

    :param line: One line of book- or reader-derived text.

    :return: The line, behind a backslash if it forges a marker.
    """
    if END_PATTERN.match(line) or START_PATTERN.match(line):
        return "\\" + line
    return line


def _escape_opener(match: re.Match[str]) -> str:
    """Neutralise one block opener: ``\\#``, ``1\\.``, or indentation as text."""
    code, indent, mark, number, delimiter = match.groups()
    if code is not None:
        return INDENT * len(code.expandtabs(TAB_WIDTH))
    if mark is not None:
        return f"{indent}\\{mark}"
    return f"{indent}{number}\\{delimiter}"


def _raw_lines(value: object) -> list[str]:
    """
    Split a value into lines, newlines normalised and nothing escaped.

    :param value: Whatever the book or the reader supplied.

    :return: The lines as given.
    """
    return str(value).replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _lines(value: object) -> list[str]:
    """
    Split a value into escaped lines, newlines normalised.

    :param value: Whatever the book or the reader supplied.

    :return: The lines, each safe to emit at the start of a line.
    """
    return [_escape(line) for line in _raw_lines(value)]


def _quoted(value: object) -> str:
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
        ``library.describe_book`` suppresses the conversion error and omits the
        key -- and the guarantee is enforced here because here is where it is
        relied on.
    """
    lines = ["---"]
    for key in ("title", "author", "identifier"):
        if book.get(key):
            lines.append(f"{key}: {_quoted(book[key])}")
    isbn = isbn13_of(book.get("identifier"))
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
    lines = [f"# {collapse(book.get('title', 'Unknown book'))}"]
    if book.get("author"):
        lines.append(f"*{collapse(book['author'])}*")

    chapter: object = object()  # Never equal to a real chapter, so the first
    for item in found:  # one always prints.
        current = item.get("chapter") or "Highlights"
        if current != chapter:
            chapter = current
            lines.extend(["", f"## {collapse(current)}"])
        lines.append("")
        lines.extend(f"> {line}" for line in _lines(item.get("text", "")))
        if item.get("note"):
            lines.append("")
            first, *rest = _raw_lines(item["note"])
            # The first line follows "**Note:** ", so it never starts a line
            # and can open no block: escaping it as an opener only showed a
            # backslash. The marker rule is about the text, not where it
            # sits, so that one still applies.
            lines.append(f"**Note:** {_unforged(first)}")
            lines.extend(_escape(line) for line in rest)
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
    region that could pass for one is escaped (:func:`_unforged`), and the
    frontmatter above it quotes every value.

    :param text: The file's contents, already decoded.

    :return: The regions, or None when this file is not one of ours.
    """
    lines = normalise(text).split("\n")
    start = _first_start(lines)
    if start is None:
        return None
    index, found = start
    head = "\n".join(lines[:index]) + "\n" if index else ""
    return _regions(head, found, lines[index + 1 :])


def _first_start(lines: list[str]) -> tuple[int, re.Match[str]] | None:
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

    :param generated: The region between the markers.

    :return: The digest, truncated.
    """
    return hashlib.sha256(encode_name(generated)).hexdigest()[:DIGEST_LENGTH]


def sidecar_for(target: Path) -> Path:
    """
    Name the copy written when the reader has edited the note itself.

    A note shares its stem with its epub, and an epub name can be the full
    255 bytes -- ``--name-by author-title`` clamps a long title to exactly
    that. Its note is then 253 bytes and the plain sidecar name 257, which no
    filesystem will create, so the reader's new highlights had nowhere to go.
    Such a name is cut back to fit and marked with a digest of the note's
    full name: two long titles sharing their first 240 bytes would otherwise
    share a sidecar, and one book's would be rewritten with the other's
    highlights.

    :param target: The note that is being left alone.

    :return: A path beside it that no book can be named.
    """
    name = target.name + SIDECAR_SUFFIX[len(".md") :]
    if len(encode_name(name)) <= MAX_FILENAME_BYTES:
        return target.with_name(name)
    marker = f" {disambiguator(target.name)}"
    budget = MAX_FILENAME_BYTES - len(encode_name(marker + SIDECAR_SUFFIX))
    stem = target.name.removesuffix(".md")
    return target.with_name(truncate_bytes(stem, budget) + marker + SIDECAR_SUFFIX)


def _present(target: Path) -> bool:
    """
    Report whether *target* exists, raising when that cannot be established.

    :param target: The path to test.

    :return: True if something is there, False if nothing is.

    :raises OSError: For any error other than the ones meaning "absent".
    """
    try:
        target.stat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


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
    start = _first_start(normalise(existing).split("\n"))
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


def of_another_book(existing: str, found: list[dict[str, Any]]) -> bool:
    """
    Whether a note is tagged for a book other than the one being written.

    A note written before notes were tagged, or annotations that name no
    asset, say nothing either way, and the note is taken as this book's, as
    it always was.

    :param existing: The note as it stands.
    :param found: The annotations about to be written into it.

    :return: True when the note's tag names another book.
    """
    marker = _start_marker_of(existing)
    held = marker.group(2) if marker is not None else None
    tags = book_tags(found)
    return held is not None and bool(tags) and held not in tags


def _start_marker(generated: str, book: str | None) -> str:
    """Render the start marker for a region, tagged for its book when known."""
    digest = digest_of(generated)
    if book is None:
        return START_TEMPLATE.format(digest=digest)
    return TAGGED_TEMPLATE.format(digest=digest, book=book)


def compose(found: list[dict[str, Any]], tail: str | None = None) -> str:
    """
    Render a whole note.

    :param found: This book's annotations, in reading order.
    :param tail: The reader's region to carry across, or None for a new note.

    :return: The file's contents.
    """
    generated = body(found)
    marker = _start_marker(generated, min(book_tags(found), default=None))
    book = found[0].get("book", {}) if found else {}
    below = tail if tail is not None else f"{END_MARKER}\n"
    return f"{frontmatter(book)}{marker}\n{generated}{below}"


def rewrite(existing: str, found: list[dict[str, Any]]) -> str:
    """
    Re-render a note this tool wrote, keeping both of the reader's regions.

    A note whose region would come out the same is returned as it stands, so
    a rerun with nothing new writes nothing and a vault under version control
    stays quiet. A note written before notes were tagged is therefore tagged
    only when its region is rewritten anyway.

    :param existing: The note as it stands.
    :param found: This book's annotations, in reading order.

    :return: The note with only the generated region replaced.
    """
    held = split(existing)
    if held is None:
        raise ValueError("not a note this tool wrote")
    generated = body(found)
    if generated == held.generated and digest_of(generated) == held.digest:
        return normalise(existing)
    book = held.book if held.book is not None else min(book_tags(found), default=None)
    return f"{held.head}{_start_marker(generated, book)}\n{generated}{held.tail}"


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
    *,
    copyable: Sequence[Path],
    suffix: bool = False,
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
    :param copyable: The library's already-zipped books and PDFs, which
        answer to a name a package may carry too. Left out, a zipped book's
        highlights were written into the note of the package that shares its
        name.
    :param suffix: Whether the run settles a collision with ``" (n)"``, as
        ``--on-collision suffix`` asks. Two books can hold distinct names and
        still want one note -- ``Dune.epub`` and ``Dune.pdf`` -- and under it
        the second is numbered rather than left without one.

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

    index = index_by_package(found, [item.package for item in named], copyable=copyable)
    tally, collided = _write_notes(
        directory,
        [
            (item, mine)
            for item in named
            if (mine := for_book(item.package.name, index))
        ],
        suffix=suffix,
    )

    logger.info(
        "Wrote %d note(s) to %s.", len(tally["written"]), printable(str(directory))
    )
    if collided:
        # Not a failure: a collision leaves the exit code alone everywhere
        # else in this tool, the README's exit-code section says so, and the
        # remedy is a flag rather than a retry. A scheduled run that treated
        # it as one would fail every night for as long as the library held
        # two copies of a book.
        logger.warning(
            "%d book(s) lost a name collision, so their highlights were not "
            "written: %s. Rerun with --on-collision suffix to give each its "
            "own note.",
            len(collided),
            _naming(collided),
        )
    if found and not collided and not any(tally[outcome] for outcome in OUTCOMES):
        # Highlights were read and not one reached a note. Every book they
        # belong to is absent from the library this run walked, so nothing
        # was matched -- which said "Wrote 0 note(s)" and exited 0. The same
        # shape as annotating._warn_about_stranded, and for the same reason:
        # silence here reads as "you had nothing to export".
        #
        # Two counts that are true whatever narrowed the run, and no third
        # invented from them: under --match the other books were excluded on
        # purpose, so reporting their highlights as stranded blamed the
        # source directory for a filter the reader asked for.
        logger.warning(
            "No note was written: this run considered %d book(s) and none of "
            "them has a highlight. Check -s, and --match if you passed one, or "
            "use --annotations-format json to write your %d annotation(s) "
            "without the books.",
            len(named),
            len(found),
        )
    for outcome, sentence in REPORTS.items():
        if tally[outcome]:
            logger.warning(sentence, len(tally[outcome]), _naming(tally[outcome]))
    unsaved = any(tally[outcome] for outcome in UNSAVED)
    return exits.FAILED if unsaved else exits.SUCCESS


def _write_notes(
    directory: Path,
    wanted: list[tuple[Assignment, list[dict[str, Any]]]],
    *,
    suffix: bool,
) -> tuple[dict[str, list[str]], list[str]]:
    """
    Write each book's note, and name the books that have none to write.

    :param directory: The vault.
    :param wanted: Each book with highlights, and its highlights.
    :param suffix: Whether a book that loses its note's name is numbered.

    :return: The notes by outcome, and the books that lost a name collision.
    """
    tally: dict[str, list[str]] = {name: [] for name in OUTCOMES}
    names = note_names([item for item, _ in wanted if item.filename], suffix=suffix)
    collided: list[str] = []
    for item, mine in wanted:
        name = names[item.package] if item.filename else None
        if name is None:
            # Lost a name collision: the book's own, which leaves it no stem to
            # share -- under -ao no planner runs to report that, and these
            # highlights were dropped without a word -- or its note's, when
            # another book's file has the same stem.
            collided.append(item.package.name)
            continue
        tally[_write_one(directory / name, mine)].append(name)
    return tally, collided


#: Everything :func:`_write_one` can report, so the tally cannot be typo'd into
#: silently dropping a case.
OUTCOMES = (
    "written",
    "unchanged",
    "kept",
    "foreign",
    "another",
    "unreadable",
    "blocked",
    "failed",
)

#: The outcomes that leave a book's highlights in no file at all, each of
#: which fails the run. Only ``failed`` and ``blocked`` did, so the same
#: obstacle exited 1 at a sidecar's path and 0 at the note's own: an
#: unreadable note, or a file somebody else wrote there, left a cron run that
#: wrote no notes reporting success. ``foreign`` is here by decision, not by
#: default: the file is the reader's and is never touched, but the book is
#: exactly as unsaved as when the same file sits at the sidecar's path, and
#: moving it aside is a fix only the reader can make. ``another`` is the same
#: case with a note this tool did write, for another book.
UNSAVED = ("foreign", "another", "unreadable", "blocked", "failed")

#: What the reader is told about each outcome worth mentioning. Every sentence
#: has to be true of every file it counts: "not written by ibook2epub" was
#: being said about notes this tool wrote but could not read.
REPORTS = {
    "kept": "%d note(s) you have edited were left alone; their new highlights "
    "are in a file beside each one: %s",
    "blocked": "%d note(s) you have edited were left alone, and their new "
    "highlights could not be written beside them either: %s",
    "unreadable": "%d file(s) could not be read and were left alone, so their "
    "books' highlights were not written; see the errors above: %s",
    "foreign": "%d file(s) were not written by ibook2epub and were left alone, "
    "so their books' highlights were not written; move them aside and rerun: %s",
    "another": "%d note(s) hold another book's highlights and were left alone, "
    "so these books' highlights were not written; two books want one note, so "
    "rerun with --on-collision suffix, or move the note aside: %s",
    "failed": "%d note(s) could not be written: %s",
}


def _naming(names: list[str]) -> str:
    """
    Render a handful of filenames for a summary line.

    Named rather than counted, because a reader with several edited notes in a
    large vault would otherwise have to glob for them. The same shape
    ``annotating._warn_about_stranded`` uses.

    :param names: The files this outcome applies to.

    :return: Up to three of them, and how many more there are.
    """
    shown = ", ".join(printable(name) for name in sorted(names)[:3])
    return shown + (f", and {len(names) - 3} more" if len(names) > 3 else "")


def _write_one(  # pylint: disable=too-many-return-statements
    target: Path, mine: list[dict[str, Any]]
) -> str:
    """
    Put one book's note in place, without touching what the reader wrote.

    Every outcome here becomes a sentence the reader is told, and they act on
    it, so none of them may be a guess. "Your new highlights are in a file
    beside it" is a promise that a file exists.

    :param target: The note's path.
    :param mine: This book's annotations, in reading order.

    Each branch returns rather than threading one variable through, because
    every one of them is a different thing to tell the reader and collapsing
    them into a single exit obscured which case produced which sentence.

    :return: One of :data:`OUTCOMES`.
    """
    try:
        # Inside the handler, and not exists(): a name past NAME_MAX raised
        # ENAMETOOLONG from exists() outside any handler and took the whole
        # vault with it, and from Python 3.14 exists() answers False to every
        # error, which reads "could not check" as "absent". Only the errors
        # that mean absent are taken as absent here.
        present = _present(target)
        if present and not readable(target):
            # A FIFO blocks read_text until a writer appears, which is never;
            # an oversized file costs twice its size to read. Neither is a
            # note.
            logger.error(
                "Skipped %s: not a readable note of a plausible size.",
                printable(target.name),
            )
            return "unreadable"
        existing = target.read_text(encoding="utf-8-sig") if present else None
    except (OSError, UnicodeDecodeError) as exc:
        # Not "foreign": this may well be a note this tool wrote. All that is
        # known is that it could not be checked, and saying otherwise put a
        # false sentence in the summary. A file that is not UTF-8 -- a note
        # saved as Windows-1252 -- raised out of here and ended the run.
        logger.error("Could not read %s: %s", printable(target.name), exc)
        return "unreadable"

    if existing is None:
        return _put(target, compose(mine))

    if not wrote_it(existing):
        return "foreign"
    if of_another_book(existing, mine):
        # A name is worked out afresh each run, and a note tagged for another
        # book is that book's whatever this run named it. Checked before the
        # sidecar, which would carry this book's highlights beside it.
        return "another"
    if not is_ours(existing):
        # Edited inside the generated region, or missing the end marker, which
        # is treated as an edit. Either way the note is left exactly as it is
        # and the new highlights go beside it. The sidecar is a note like any
        # other and gets the same treatment one level down, so one the reader
        # has partly merged into survives too.
        if _write_one(sidecar_for(target), mine) in ("written", "unchanged"):
            return "kept"
        # The sidecar could not be written either, so nothing was saved and
        # the reader must not be told otherwise.
        return "blocked"

    rewritten = rewrite(existing, mine)
    if rewritten == normalise(existing):
        return "unchanged"
    return _put(target, rewritten)


def _put(target: Path, text: str) -> str:
    """
    Write one note, letting one failure cost one note rather than the run.

    Every other per-file failure in this project is logged and stepped over --
    a damaged archive, an unreadable database row. An unguarded write here took
    the whole vault and its summary with it.

    :param target: The note's path.
    :param text: Its contents.

    :return: ``written`` or ``failed``.
    """
    try:
        write_atomically(target, text)
    except OSError as exc:
        logger.error("Could not write %s: %s", printable(target.name), exc)
        return "failed"
    return "written"
