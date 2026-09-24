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

import re
from collections.abc import Collection, Mapping, Sequence
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
from .noteformat import (
    END_MARKER,
    END_PATTERN,
    START_PATTERN,
    book_tags,
    is_ours,
    normalise,
    quoted,
    readable,
    split,
    start_marker,
    trimmed,
    untouched,
    wrote_it,
)
from .notenames import Claimant, Holding, Vault, claimant, holding, note_names, parse

#: Suffix for the copy written when a reader has edited the note itself. Not
#: ``.new.md``: a book titled "Foo.new" is named ``Foo.new.md``, which was
#: exactly the sidecar name for a book titled "Foo", so one book's sidecar
#: overwrote another book's note. This suffix ends in ``.md.new`` instead, so
#: it is not a name any naming policy can produce and not a file a later run
#: will adopt as some book's note.
SIDECAR_SUFFIX = ".md.new"

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
#: the row is matched by its whole shape, cells and pipes. A one-column
#: row needs no pipe at all when it carries an alignment colon -- ``:---``,
#: ``---:``, ``:-:`` -- and under ``see also |`` it made a table as well. A
#: link label may continue onto the next line, so an unfinished one counts.
#: ``>`` opens a quote whatever follows it.
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
              | (?=:?-+:?[ \t]*\|(?:[ \t]*:?-+:?[ \t]*\|)*(?:[ \t]*:?-+:?)?[ \t]*$
                  | :-+:?[ \t]*$ | -+:[ \t]*$)
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
            lines.append(f"{key}: {quoted(book[key])}")
    isbn = isbn13_of(book.get("identifier"))
    if isbn:
        lines.append(f"isbn: {quoted(isbn)}")
    if book.get("language"):
        lines.append(f"language: {quoted(book['language'])}")
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
    # No line ends in white space, so an editor that trims it on save leaves
    # the region as written: "> " for a blank line in a highlight and
    # "**Note:** " before a note's blank first line each did.
    return trimmed("\n".join(lines)) + "\n"


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


def compose(found: list[dict[str, Any]], tail: str | None = None) -> str:
    """
    Render a whole note.

    :param found: This book's annotations, in reading order.
    :param tail: The reader's region to carry across, or None for a new note.

    :return: The file's contents.
    """
    generated = body(found)
    marker = start_marker(generated, min(book_tags(found), default=None))
    book = found[0].get("book", {}) if found else {}
    below = tail if tail is not None else f"{END_MARKER}\n"
    return f"{frontmatter(book)}{marker}\n{generated}{below}"


def rewrite(
    existing: str, found: list[dict[str, Any]], tags: Collection[str] = ()
) -> str:
    """
    Re-render a note this tool wrote, keeping both of the reader's regions.

    A note whose region would come out the same is returned as it stands, so
    a rerun with nothing new writes nothing and a vault under version control
    stays quiet. A note written before notes were tagged is therefore tagged
    only when its region is rewritten anyway, and so is one tagged for an
    asset id the book no longer answers to -- the old one of a book removed
    from Books and added again.

    :param existing: The note as it stands.
    :param found: This book's annotations, in reading order.
    :param tags: Every tag the book answers to besides its highlights':
        the library's other asset ids for the same package. A note tagged
        for any of them keeps its tag.

    :return: The note with only the generated region replaced.
    """
    held = split(existing)
    if held is None:
        raise ValueError("not a note this tool wrote")
    generated = body(found)
    # Compared as an editor may have left it: a note an older version wrote
    # with trailing spaces is unchanged when only those spaces differ.
    if trimmed(generated) == trimmed(held.generated) and untouched(
        held.generated, held.digest
    ):
        return normalise(existing)
    own = book_tags(found)
    book = held.book
    if book is None or (own and book not in own | set(tags)):
        book = min(own, default=book)
    return f"{held.head}{start_marker(generated, book)}\n{generated}{held.tail}"


def write_vault(
    found: list[dict[str, Any]],
    destination: str,
    named: Sequence[Assignment],
    *,
    copyable: Sequence[Path],
    suffix: bool = False,
    assets: Mapping[str, str | None] | None = None,
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
    :param assets: Every book in Apple's library, highlighted or not: its
        asset id, and the package name it is read from. A note tagged for
        one of them is that book's; one tagged for an id neither the library
        nor any highlight knows is taken as the book's whose name it has,
        which is what a book removed from Books and added again looks like.

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
        [(item, for_book(item.package.name, index)) for item in named],
        suffix=suffix,
        known=_known(assets or {}, found),
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


class Known(NamedTuple):
    """The books a run knows of, by the tags their notes carry."""

    #: Every book's: in Apple's library, or with a highlight this run read.
    tags: frozenset[str]
    #: Each package name's, from the library, highlighted or not.
    of_package: dict[str, set[str]]


def _known(assets: Mapping[str, str | None], found: list[dict[str, Any]]) -> Known:
    """
    Name the books a run knows of, which a note's tag may be another's.

    Both halves: the library lists every book, highlighted or not, and a
    highlight can name a book the library has forgotten, or one this run
    does not name.

    :param assets: Every book in Apple's library, by asset id.
    :param found: Every annotation this run read.

    :return: The books.
    """
    of_package: dict[str, set[str]] = {}
    for asset, source in assets.items():
        if source is not None:
            of_package.setdefault(source, set()).add(disambiguator(asset))
    tags = frozenset(disambiguator(asset) for asset in assets) | book_tags(found)
    return Known(tags, of_package)


def _write_notes(
    directory: Path,
    wanted: list[tuple[Assignment, list[dict[str, Any]]]],
    *,
    suffix: bool,
    known: Known,
) -> tuple[dict[str, list[str]], list[str]]:
    """
    Write each book's note, and name the books that have none to write.

    Every book is named, and only the books with highlights written. Only
    the books with highlights were named, so a note's name moved between
    books as one gained its first highlight or lost its last, and the
    reader's writing in it went with the name or was stranded.

    :param directory: The vault.
    :param wanted: Every book of the run, and its highlights, if any.
    :param suffix: Whether a book that loses its note's name is numbered.
    :param known: The books this run knows of.

    :return: The notes by outcome, and the books that lost a name collision.
    """
    tally: dict[str, list[str]] = {name: [] for name in OUTCOMES}
    books = {
        item.package: claimant(mine, known.of_package.get(item.package.name, ()))
        for item, mine in wanted
        if mine
    }
    names = note_names(
        [item for item, _ in wanted if item.filename],
        suffix=suffix,
        claimants=books,
        vault=Vault(directory),
        known=known.tags,
    )
    collided: list[str] = []
    for item, mine in wanted:
        if not mine:
            continue
        name = names[item.package] if item.filename else None
        if name is None:
            # Lost a name collision: the book's own, which leaves it no stem to
            # share -- under -ao no planner runs to report that, and these
            # highlights were dropped without a word -- or its note's, when
            # another book's file has the same stem.
            collided.append(item.package.name)
            continue
        written = _write_one(
            directory / name, mine, book=books[item.package], known=known.tags
        )
        tally[written].append(name)
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
    "highlights were not written beside them either; see the errors above: %s",
    "unreadable": "%d file(s) could not be read and were left alone, so their "
    "books' highlights were not written; see the errors above: %s",
    "foreign": "%d file(s) were not written by ibook2epub and were left alone, "
    "so their books' highlights were not written; move them aside and rerun: %s",
    "another": "%d note(s) are another book's and were left alone, so these "
    "books' highlights were not written; rerun with --on-collision suffix to "
    "give each book a note of its own, or move the note aside: %s",
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
    target: Path,
    mine: list[dict[str, Any]],
    *,
    book: Claimant | None = None,
    known: Collection[str] | None = None,
    beside: Path | None = None,
) -> str:
    """
    Put one book's note in place, without touching what the reader wrote.

    Every outcome here becomes a sentence the reader is told, and they act on
    it, so none of them may be a guess. "Your new highlights are in a file
    beside it" is a promise that a file exists.

    :param target: The note's path.
    :param mine: This book's annotations, in reading order.
    :param book: The book, as a note is judged against it; by default as
        its highlights describe it.
    :param known: The tags of every book the run knows of, or None to take
        any tag as a known book's (:func:`~.notenames.holding`).
    :param beside: The note *target* is the sidecar of, or None when it is
        a note itself.

    Each branch returns rather than threading one variable through, because
    every one of them is a different thing to tell the reader and collapsing
    them into a single exit obscured which case produced which sentence.

    :return: One of :data:`OUTCOMES`, or ``edited`` for a sidecar the reader
        has edited, which only :func:`_write_beside` asks about.
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
    book = book if book is not None else claimant(mine)
    if holding(parse(existing), book, known) is Holding.ANOTHER:
        # A name is worked out afresh each run, and a note tagged for another
        # book is that book's whatever this run named it. Checked before the
        # sidecar, which would carry this book's highlights beside it.
        return "another"
    if not is_ours(existing):
        # Edited inside the generated region, or missing the end marker, which
        # is treated as an edit. Either way the note is left exactly as it is
        # and the new highlights go beside it.
        if beside is None:
            return _write_beside(target, mine, book=book, known=known)
        # A sidecar the reader has edited -- part way through merging it --
        # is left alone like the note. It was treated as a note one level
        # down, and the highlights went into a third file, .md.new.new, that
        # the run then said could not be written.
        logger.error(
            "Left %s alone: it holds your edits. Merge it into %s, or remove "
            "it, and rerun to have the new highlights written beside the note.",
            printable(target.name),
            printable(beside.name),
        )
        return "edited"

    rewritten = rewrite(existing, mine, book.tags)
    if rewritten == normalise(existing):
        return "unchanged"
    return _put(target, rewritten)


def _write_beside(
    target: Path,
    mine: list[dict[str, Any]],
    *,
    book: Claimant,
    known: Collection[str] | None,
) -> str:
    """
    Write an edited note's new highlights into its sidecar.

    :param target: The note, which the reader has edited.
    :param mine: This book's annotations, in reading order.
    :param book: The book, as a note is judged against it.
    :param known: The tags of every book the run knows of.

    :return: ``kept`` when they are in the sidecar, ``blocked`` when they are
        in no file at all.
    """
    sidecar = sidecar_for(target)
    outcome = _write_one(sidecar, mine, book=book, known=known, beside=target)
    if outcome in ("written", "unchanged"):
        return "kept"
    if outcome in ("foreign", "another"):
        logger.error(
            "Left %s alone: %s. Move it aside and rerun to have the new "
            "highlights written beside %s.",
            printable(sidecar.name),
            "it is another book's note"
            if outcome == "another"
            else "ibook2epub did not write it",
            printable(target.name),
        )
    # Nothing was saved, and the reader must not be told otherwise. Every
    # other outcome has already said why.
    return "blocked"


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
