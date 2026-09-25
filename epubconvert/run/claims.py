"""
Which output names a run has spoken for, and what to say to a book that lost.

The bookkeeping :func:`epubconvert.run.planning.assign_names` settles names
with, kept apart from the rules it applies, and the candidates a name is
tried as, which placing a book on the shelf tries too
(:func:`epubconvert.run.placing.place`).
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Container, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ..collect.identifiers import usable_identifier
from ..export.archive import COPYABLE_SUFFIXES, PARTIAL_PREFIX
from ..export.naming import (
    DISAMBIGUATOR_CHARS,
    encode_name,
    filesystem_key,
    split_extension,
    truncate_bytes,
)
from .holders import identifier_on_shelf, source_identifier

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..utils.opf import Package
    from ..utils.policy import NamingPolicy


#: Highest ``" (n)"`` suffix the planner will try before giving up on a name.
MAX_SUFFIX = 99

#: A name numbered by :func:`suffixed`, as a filesystem key: its stem, the
#: number, and the extension. From 2 and without a leading zero, as it
#: numbers: ``Dune (1).epub``, a book's folder, was taken for the first
#: number of ``Dune``, and so for its plain file.
NUMBERED = re.compile(
    r"(?P<stem>.*) \((?P<position>[2-9]|[1-9]\d+)\)(?P<extension>\.[^.]*)?"
)

#: The place in :func:`kept_numbers`'s forms of a file found under a book's
#: own name where that name looks numbered (``Dune (2)``), which the shelf's
#: index files as a number of another name.
ITSELF = 0

#: A name marked by planning._stable_base, numbered or not, as a filesystem
#: key: its stem, the digest, and the extension.
MARKED = re.compile(
    rf"(?P<stem>.*) \[(?P<digest>[0-9a-f]{{{DISAMBIGUATOR_CHARS}}})\]"
    r"(?: \(\d+\))?(?P<extension>\.[^.]*)?"
)


def suffixed(filename: str, position: int, max_bytes: int) -> str:
    """
    Render the *position*-th candidate name for a filename.

    The suffix is applied within the budget the naming policy declares.
    Appending to a name already at that limit would push it over and the
    export would fail at the closing rename with a filesystem error rather
    than a name collision. A policy declaring no budget is left alone: its
    names come from the source directory, and truncating one would break the
    identity round trip that rerun safety depends on.

    :param filename: The base filename.
    :param position: 1 for the base name itself, 2 upwards for suffixes.
    :param max_bytes: The policy's byte budget, or 0 for no clamping.

    :return: The candidate filename.
    """
    if position == 1:
        return filename
    return marked(filename, f" ({position})", max_bytes)


def marked(filename: str, marker: str, max_bytes: int) -> str:
    """
    Insert *marker* before the extension, within the policy's byte budget.

    :param filename: The base filename.
    :param marker: Text to insert, its own leading space included.
    :param max_bytes: The policy's byte budget, or 0 for no clamping.

    :return: The marked filename.
    """
    # The module's own splitter, not Path().suffix: pathlib treats ".epub" as
    # extension-less, so the marker landed after it -- ".epub (2)" -- and no
    # *.epub glob matches that.
    stem_text, extension = split_extension(filename)
    candidate = f"{stem_text}{marker}{extension}"
    if not max_bytes or len(encode_name(candidate)) <= max_bytes:
        return candidate

    budget = max_bytes - len(encode_name(marker))
    budget -= len(encode_name(extension))
    stem_text = truncate_bytes(stem_text, max(budget, 1)).rstrip(" .") or "_"
    return f"{stem_text}{marker}{extension}"


class Claims:
    """
    Which output names are spoken for, and where each search left off.

    Two sets rather than one. ``identity`` answers whether two books are the
    same book; ``filesystem_key`` answers whether two names are the same
    *file*, which on a case-insensitive volume is a looser question and the one
    that decides whether a write destroys another write.

    The resume positions exist because a colliding group that exhausted
    ``MAX_SUFFIX`` made every later member retry all 99 candidates, recomputing
    identity each time, before losing.
    """

    def __init__(self) -> None:
        self.identities: set[str] = set()
        self.paths: set[str] = set()
        self.positions: dict[str, int] = {}
        self.holders: dict[str, str] = {}
        #: The name that took each filesystem key.
        self.held: dict[str, str] = {}

    def resume(self, group: str) -> int:
        """Return the first position worth trying for this group."""
        return self.positions.get(group, 1)

    def take(self, group: str, position: int, key: str, candidate: str) -> bool:
        """Claim a candidate if both its identity and its path are free."""
        path_key = filesystem_key(candidate)
        if key in self.identities or path_key in self.paths:
            return False
        self.identities.add(key)
        self.paths.add(path_key)
        self.positions[group] = position + 1
        self.holders.setdefault(group, candidate)
        self.held[path_key] = candidate
        return True

    def keep(self, group: str, key: str, candidate: str) -> bool:
        """
        Claim a file already on the shelf for a book, if both are free.

        As :meth:`take`, but where the search for the group's first free
        position resumes is left alone: the file is kept at its number, and
        the positions before it are still free for the rest of the group.
        """
        path_key = filesystem_key(candidate)
        if key in self.identities or path_key in self.paths:
            return False
        self.identities.add(key)
        self.paths.add(path_key)
        self.holders.setdefault(group, candidate)
        self.held[path_key] = candidate
        return True

    def holder(self, group: str, name: str = "") -> str | None:
        """
        Return the name that took this group, or the file *name* is, if any.

        By the file too: two identities can be one file, as ``Dune.epub``
        and ``dune.epub`` are to the default policy and a case-insensitive
        volume, and a book that lost to its namesake was told only that
        "another book already claims this name".
        """
        return self.holders.get(group) or self.held.get(filesystem_key(name))

    def exhaust(self, group: str, limit: int) -> None:
        """Record that this group has no positions left to try."""
        self.positions[group] = limit + 1


def lost_to(holder: str | None, metadata: Package | None) -> str:
    """
    Explain which book holds the name, and say what this one is.

    Naming the winner turns "another book already claims this name" into
    something a person can act on, and the loser's identifier is what tells
    them whether the two are the same book stored twice or genuinely different
    editions. In a surveyed library both cases are common.

    :param holder: The filename that took the name, if one did.
    :param metadata: The losing package's document, if it was read.

    :return: The reason to record on the decision.
    """
    reason = (
        f"{holder} already holds this name"
        if holder
        else "another book already claims this name"
    )
    identifier = usable_identifier(metadata)
    return f"{reason}; this book is {identifier}" if identifier else reason


def shelf_files(output_dir: Path) -> list[Path]:
    """
    Find every file on the shelf a run could have put there.

    Every kind :func:`~epubconvert.export.archive.collect_copyable` takes
    along, whatever the case of the extension, and no partial. The shelf was
    read with ``glob("*.epub")``, which is case-sensitive, so a zipped book
    copied through as ``Foo.EPUB`` was invisible to the plan, the orphan
    check and the placing: a package ``Foo.epub`` was judged free and, on a
    case-insensitive volume, written over it. PDFs were invisible the same
    way.

    :param output_dir: Directory holding exported files.

    :return: The files, sorted; none when the directory is missing, which is
        what a dry run or a first run finds.
    """
    try:
        entries = sorted(output_dir.iterdir())
    except OSError:
        return []
    return [
        found
        for found in entries
        if found.suffix.lower() in COPYABLE_SUFFIXES
        and not found.name.startswith(PARTIAL_PREFIX)
        and found.is_file()
    ]


class ShelfNames(frozenset[str]):
    """The names of the files on a shelf, and the directory they are in."""

    #: The shelf, when there is one to read a file's identifier from.
    directory: Path | None

    def __new__(
        cls, names: Iterable[str] = (), directory: Path | None = None
    ) -> ShelfNames:
        """Hold *names*, read from *directory*."""
        made = super().__new__(cls, names)
        made.directory = directory
        return made


def shelf_names(output_dir: Path | None) -> ShelfNames:
    """
    Read the name of every file on the shelf, for the claim pass to weigh.

    :param output_dir: Directory holding exported files, or None for none.

    :return: The names, NFC-normalized, as :func:`claim_order` compares them;
        empty when there is no shelf to read.
    """
    if output_dir is None:
        return ShelfNames()
    try:
        return ShelfNames(
            (
                unicodedata.normalize("NFC", found.name)
                for found in output_dir.iterdir()
                if found.is_file()
            ),
            output_dir,
        )
    except OSError:
        # Missing, which is what a dry run or a first run finds.
        return ShelfNames()


def numbered_names(
    names: Iterable[str], policy: NamingPolicy
) -> dict[str, list[tuple[int, str]]]:
    """
    Index names by the filesystem key of the name they number.

    :param names: File names on the shelf.
    :param policy: The naming policy, whose identities the keys are of.

    :return: Each name under the key of its name less any ``" (n)"``, with
        n; 1 for a name with no number.
    """
    index: dict[str, list[tuple[int, str]]] = {}
    for name in names:
        key = filesystem_key(policy.identity(name))
        numbered = NUMBERED.fullmatch(key)
        if numbered is None:
            index.setdefault(key, []).append((1, name))
        else:
            plain = numbered["stem"] + (numbered["extension"] or "")
            index.setdefault(plain, []).append((int(numbered["position"]), name))
    return index


class Keeping(NamedTuple):
    """What :func:`kept_numbers` found of one package's files on the shelf."""

    #: The file it keeps, if any.
    file: str | None = None
    #: The file of its plain name is another book's, as its identifier
    #: says: the package is not given that file for its own.
    refused: bool = False
    #: Its usable identifier, where one was read to find its file: placing
    #: compares it with the file it is given (holders.foreign).
    identifier: str | None = None


class Wanting(NamedTuple):
    """What :func:`kept_numbers` needs to know of one package."""

    #: The name it claims first.
    base: str
    #: Its digest-marked name, or *base* when it has none or is marked.
    stable: str
    #: Its usable identifier, when naming read one.
    identifier: str | None
    #: No other package wants its name.
    alone: bool
    #: The package to read its identifier from, when naming did not.
    unread: Path | None


def kept_numbers(
    books: Sequence[Wanting],
    shelf: Collection[str],
    policy: NamingPolicy,
    unopened: Container[Path] = frozenset(),
) -> dict[int, Keeping]:
    """
    Find the numbered file on the shelf each package in suffix mode keeps.

    A package with no digest marker is numbered by its place in its group,
    so once the book before it left the library with its archive, it took
    the name it had given up, was written again, and its own file was
    listed as an orphan. Before any name is claimed, each book whose name
    has numbered files on the shelf keeps one of them: where it has a
    usable identifier, the lowest-numbered one declaring that identifier;
    where it has none, the one numbered file, when no other book wants its
    name, no file has the plain name, and the file declares no usable
    identifier either. Nothing else can say whose it is. A file that does
    declare one is another book's: ``Dune (1965)``, deleted from the library,
    left its archive, and an unidentified ``Dune`` added since was reported
    exported from it and never written, and the deleted book's archive, maybe
    its last copy, was not listed as an orphan. A book left unopened may
    declare the file's identifier, since nobody read its own: it keeps the
    file, as before.
    A file under a name another book wants is never kept, as for a title
    that looks like a number (``Dune (2)``). The book of that title keeps
    it, as its own name, where its identifier is the file's: renamed by
    case to ``dune (2)``, it was never looked for there, and no longer
    claimed first by its exact name (:func:`claim_order`), so the second of
    two books ``Dune`` added since took the file as its number and was
    reported exported from it on every run. And two books of that title,
    ``b/Dune (2).epub`` exported alone and ``a/Dune (2).epub`` added since,
    were never found to share the file: the newcomer was reported exported
    from the other's archive, and the other written again under a number.
    Without an identifier to go by, the file is as much a number of the
    plain name, as before.

    Two books whose names are one file on the shelf ask the same, numbered
    files or not: ``b/Cafe.epub``, exported and renamed by case to
    ``b/cAFE.epub``, or exported alone, and ``a/Cafe.epub`` added since,
    which sorts first and claims first by its exact name
    (:func:`claim_order`). Nothing was read without a numbered file, so the
    newcomer was reported exported from the other's archive and never
    written, the other was written again under a number, and the next run
    wrote the newcomer too, leaving that copy an orphan. Now the book whose
    identifier the file declares keeps it.

    And a book the plain file is not is refused its name, where the file
    declares a usable identifier and the book, read, has another or none.
    With no digest of its identifier to move on to, as a book whose name
    holds another book does (:func:`epubconvert.run.placing.place`), it
    claims its name numbered instead. Under
    ``--skip-incomplete`` the owner above is left unopened and keeps
    nothing, and the newcomer, whose identifier and the file's had both been
    read, was reported exported from the owner's archive, or with
    ``--refresh`` written over it: the evicted book's only one, where the
    newcomer declared no identifier to compare. The identifier read goes
    with the book to placing, which compares it with the file the book is
    given: a number a deleted namesake left is another book's too.

    A book that has left its crowd wants its plain name again, and its
    archive is under its marked name, or that name numbered where it shares
    its identifier: it keeps that too, rather than being written again
    under the plain name and its archive listed as an orphan.

    Identifiers are read only for a name with numbered files on the shelf,
    a marked name with any, a name two books want with a file, or one with
    a file that looks like a number of a name a book wants: under a
    policy that names from the folder, the book's own too, unless the run
    leaves it unopened. A rerun over a shelf with none of these reads
    nothing. A copy's own bytes are its own whatever an
    identifier says, so a copy that keeps the file sends the package back to
    claim a name (copynames._Claiming.reclaim).

    :param books: Each package, in sorted order.
    :param shelf: The shelf's names, from :func:`shelf_names`.
    :param policy: The naming policy in force.
    :param unopened: The books not to open for their identifier: under
        ``--skip-incomplete``, a package iCloud has evicted
        (:class:`~epubconvert.run.holders.Unopened`). Left unread whatever
        the run was asked, an evicted book kept nothing, and without the
        flag its only archive was listed as an orphan.

    :return: What was found of each book whose files were looked at, by
        index into *books*: the file it keeps, whether its plain name's file
        is another book's, and the identifier read.
    """
    index = numbered_names(shelf, policy)
    sharing = Counter(filesystem_key(policy.identity(book.base)) for book in books)
    directory = getattr(shelf, "directory", None)
    kept: dict[int, Keeping] = {}

    def forms(name: str) -> list[tuple[int, str]]:
        key = filesystem_key(policy.identity(name))
        # A name that looks numbered is filed as a number of its plain name;
        # its own file is found there, as its name, but told apart.
        itself = [
            (ITSELF, found)
            for _, found in index.get(_numbered_plain(key) or "", [])
            if filesystem_key(policy.identity(found)) == key
        ]
        return sorted(
            (number, found)
            for number, found in [*index.get(key, []), *itself]
            if found not in {keeping.file for keeping in kept.values()}
            and (number <= 1 or filesystem_key(policy.identity(found)) not in sharing)
        )

    for position, book in enumerate(books):
        numbers = forms(book.base)
        marked_forms = forms(book.stable) if book.stable != book.base else []
        if directory is None or (
            all(n <= 1 for n, _ in numbers)
            and not marked_forms
            # Nor shared: no other book wants the file, as its name or as a
            # number of its own.
            and not (
                numbers and _shared(filesystem_key(policy.identity(book.base)), sharing)
            )
        ):
            continue
        kept[position] = _keeps(book, (numbers, marked_forms), directory, unopened)
    return kept


def _numbered_plain(key: str) -> str | None:
    """
    Return the plain name *key* is a number of, if it looks numbered.

    :param key: A book's name, as a filesystem key.

    :return: The plain name's key, or None when *key* does not look
        numbered.
    """
    numbered = NUMBERED.fullmatch(key)
    if numbered is None:
        return None
    return numbered["stem"] + (numbered["extension"] or "")


def _shared(key: str, sharing: Counter[str]) -> bool:
    """
    Say whether another book may claim the file of the name *key*.

    Two books want the name, or one wants the plain name *key* looks like a
    number of: ``Dune (2).epub``, the name of a book titled like a number,
    is the second ``Dune``'s number too.

    :param key: A book's name, as a filesystem key.
    :param sharing: How many books want each name, by filesystem key.

    :return: True when another book may claim it.
    """
    return sharing[key] > 1 or _numbered_plain(key) in sharing


def _keeps(
    book: Wanting,
    forms: tuple[list[tuple[int, str]], list[tuple[int, str]]],
    directory: Path,
    unopened: Container[Path],
) -> Keeping:
    """
    Name the file on the shelf that *book* keeps, of those its name has.

    And whether the file under its plain name is another book's: one that
    declares a usable identifier this book, read, does not have. Such a file
    is no number of the book's either, and the book is not given its name
    (:attr:`Keeping.refused`).

    :param book: The package in question.
    :param forms: The numbered files of its name and those of its marked
        name, each lowest first.
    :param directory: The shelf.
    :param unopened: The books not to open for their identifier.

    :return: The file it keeps, if any, whether the plain file is another
        book's, and the book's identifier.
    """
    numbers, marked_forms = forms
    identifier = book.identifier
    unknown = identifier is None and book.unread in unopened
    if identifier is None and book.unread and not unknown:
        identifier = source_identifier(book.unread)
    plain = next((name for number, name in numbers if number <= 1), None)
    declared = (
        None if unknown or plain is None else identifier_on_shelf(directory / plain)
    )
    foreign = declared is not None and declared != identifier
    if foreign:
        numbers = [entry for entry in numbers if entry[1] != plain]
    if identifier is not None:
        found = [
            name
            for _, name in [*numbers, *marked_forms]
            if identifier_on_shelf(directory / name) == identifier
        ]
        return Keeping(found[0] if found else None, foreign, identifier)
    # Without an identifier, a name that looks numbered has no file of its
    # own name to go by: that file is as much the plain name's number.
    numbers = [entry for entry in numbers if entry[0] != ITSELF]
    if (
        book.alone
        and len(numbers) == 1
        and numbers[0][0] > 1
        and (unknown or identifier_on_shelf(directory / numbers[0][1]) is None)
    ):
        return Keeping(numbers[0][1], foreign)
    return Keeping(None, foreign)


def claim_order(candidates: Sequence[str], shelf: Collection[str]) -> list[int]:
    """
    Order books for the claim pass: first those whose name is on the shelf.

    The pass walked the library in sorted order and never looked at the
    shelf. So ``a/Dune.epub``, added beside ``b/dune.epub`` already exported
    alone, took the name a case-insensitive volume gives both, as it sorts
    first: in suffix mode ``b`` was written again under a suffix and its
    archive listed as an orphan, and in skip mode both were collisions, on
    every run. A book whose exact name is a file on the shelf claims it
    first; otherwise the sorted order stands, so a library with nothing on
    the shelf is named as before.

    :param candidates: The first name each book tries, in sorted order.
    :param shelf: The shelf's names, from :func:`shelf_names`.

    :return: Indices into *candidates*, in the order to claim.
    """
    if not shelf:
        return list(range(len(candidates)))
    return sorted(
        range(len(candidates)),
        key=lambda index: unicodedata.normalize("NFC", candidates[index]) not in shelf,
    )
