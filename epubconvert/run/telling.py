"""
Whose a file on the shelf is, where two books want its name.

A name is not a book (:mod:`epubconvert.run.holders`), and where two books of
the library want one name, the name says nothing about which of them a file
of it holds. Each archive this tool writes names its source
(:mod:`epubconvert.export.provenance`), and that decides: the book it names
keeps the file, claiming before its namesakes, and a file that names no book
of them is another's, which none of them is given.

An archive written before markers names nothing, and then the identifiers
decide where they can: the one book that declares the file's identifier
keeps it, and a book that declares another, or none where the file declares
one, is not given it. Where they cannot -- two books declaring no usable
identifier, or one between them -- the planner took the file for the first
of them in sorted order: reported exported from a file that could be the
other's, never written, and under ``--force`` or ``--refresh`` written over
it (formal/README.md, ``Unidentifiable``). Now none of them is given it.
Under ``--on-collision suffix`` each moves on to a name of its own, where it
is written with its marker, so the next run tells them apart; in skip mode
each is a collision, and the file, which may be either one's only archive,
stays off the orphan list. A book alone in wanting such a file still takes
it by its name: refusing it would write every shelf made before markers
again, and that is the limit formal/README.md describes.

Read only for a name two books want that has files on the shelf: the last
bytes of each such file, for its marker, and for a file with none, its
identifier and, under a policy that names from the folder, each book's.
Under a policy that names from the package document the marker is read
with the file's identifier instead, which placing reads anyway, so a rerun
opens each such file once; named from the folder, the last bytes are one
short read per such file per run that nothing else would have made.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Container, Sequence
from pathlib import Path
from typing import NamedTuple

from ..export.naming import filesystem_key
from ..utils.policy import NamingPolicy
from .claims import Wanting, declared_by, numbered_names
from .holders import (
    UNREAD,
    identifier_on_shelf,
    marker_on_shelf,
    marker_with_identifier,
    moved,
)


class Told(NamedTuple):
    """What the files of the names two books want say of whose they are."""

    #: The file each book keeps, by index: in skip mode, its plain name's
    #: file, which it claims before its namesakes. In suffix mode
    #: :func:`~epubconvert.run.claims.kept_numbers` says.
    keeps: dict[int, str]
    #: Each book's usable identifier, where one was read here.
    identifiers: dict[int, str]
    #: The files no book of their name's crowd may have, and why.
    refused: dict[str, str]
    #: Why each book that cannot be told apart from another was refused a
    #: file, by index.
    untold: dict[int, str]


def tell_apart(
    books: Sequence[Wanting],
    shown: Sequence[str],
    shelf: Collection[str],
    policy: NamingPolicy,
    *,
    suffix: bool,
    unopened: Container[Path] = frozenset(),
) -> Told:
    """
    Find whose each file of a name two books want is.

    :param books: Each package, in sorted order.
    :param shown: Each package as a person knows it, for the reasons.
    :param shelf: The shelf's names, from
        :func:`~epubconvert.run.claims.shelf_names`.
    :param policy: The naming policy in force.
    :param suffix: Under ``--on-collision suffix``, where a crowd claims the
        numbers of its name too.
    :param unopened: The books not to open for their identifier.

    :return: The files kept and refused, and the identifiers read.
    """
    # Named from its package document, a book placed at the file compares
    # its identifier with the file's, which opens the archive: the marker is
    # read from that one open. Named from the folder, nothing else reads the
    # file, and its last bytes are the one short read it costs.
    judge = _Judge(
        books, shown, unopened, opened=bool(getattr(policy, "needs_metadata", False))
    )
    directory = getattr(shelf, "directory", None)
    if directory is None or not judge.sources:
        # Without a library to name sources from, no marker says anything,
        # and none would be written to break a tie: refused a file, a book
        # would be written again on every run.
        return judge.told
    crowds = _crowds(books, policy)
    index = numbered_names(shelf, policy)
    for key, crowd in crowds.items():
        for number, name in sorted(index.get(key, [])) if len(crowd) > 1 else []:
            # A number that is another book's own name (a title "Dune (2)")
            # is that book's to keep or lose, as kept_numbers leaves it.
            if number == 1 or (
                suffix and filesystem_key(policy.identity(name)) not in crowds
            ):
                found = _File(directory / name, crowd, number == 1 and not suffix)
                if not judge.marked(found):
                    judge.unmarked(found)
    return judge.told


def _crowds(books: Sequence[Wanting], policy: NamingPolicy) -> dict[str, list[int]]:
    """
    Group the books by the file their name is.

    :param books: Each package, in sorted order.
    :param policy: The naming policy in force.

    :return: The indices of the books that want each name, by its
        filesystem key.
    """
    crowds: dict[str, list[int]] = {}
    for position, book in enumerate(books):
        crowds.setdefault(filesystem_key(policy.identity(book.base)), []).append(
            position
        )
    return crowds


class _File(NamedTuple):
    """A file of a crowd's name."""

    path: Path
    #: The books that want its name, by index.
    crowd: Sequence[int]
    #: The book it is found to be kept by keeps it here: the plain file in
    #: skip mode. In suffix mode kept_numbers keeps the numbers, reading the
    #: same.
    kept: bool


class _Judge:
    """Says whose each file of a crowd's name is, into a :class:`Told`."""

    def __init__(
        self,
        books: Sequence[Wanting],
        shown: Sequence[str],
        unopened: Container[Path],
        *,
        opened: bool,
    ) -> None:
        self.books = books
        self.shown = shown
        self.unopened = unopened
        self.told = Told({}, {}, {}, {})
        #: How many books have each source: a source two have names neither.
        self.sources = Counter(book.source for book in books if book.source)
        #: The book of each source only one book has.
        self.by_source = {
            book.source: book
            for book in books
            if book.source and self.sources[book.source] == 1
        }
        #: The file is opened for its identifier anyway: the marker is read
        #: from that open (holders.marker_with_identifier).
        self.opened = opened

    def marked(self, found: _File) -> bool:
        """
        Say whose a file is by its marker, if it names a source.

        :param found: The file.

        :return: True when its marker settled it, or names a source two books
            share, which leaves it to the claims as before markers.
        """
        name = found.path.name
        read = marker_with_identifier if self.opened else marker_on_shelf
        source = read(found.path)
        if source is None:
            return False
        if self.sources[source] > 1:
            # Two books of the library have that source -- two paths that
            # differ by case alone, on a volume that tells them apart -- and
            # the file is one of theirs: no evidence either way, not the
            # silence of a file written before markers. Refused to both, as
            # that is, each was written under a new number with the same
            # source on every run, or in skip mode both collided for ever.
            # What the names and identifiers say stands, as before markers:
            # the identifier the file declares still names its owner, and a
            # file it cannot place is left to the claims, refused to no one.
            self.unmarked(found, refuse=False)
            return True
        owners = [i for i in found.crowd if self.books[i].source == source]
        if source not in self.sources:
            owners = self._moved(found, found.crowd)
        elif not owners or (self.opened and found.kept):
            # It names another book of the library, which may have been
            # added since at the old path of a book of the crowd that moved:
            # the moved book's where the identifiers say so. Asked where the
            # file would be refused, and where it is claimed in skip mode and
            # the identifiers are known, as naming read them; in suffix mode
            # kept_numbers asks for the book the marker names.
            others = [i for i in found.crowd if self.books[i].source != source]
            owners = self._moved(found, others) or owners
        if not owners:
            self.told.refused[name] = f"{name} was written for another book"
        elif found.kept:
            self.told.keeps.setdefault(owners[0], name)
        return True

    def _moved(self, found: _File, crowd: Sequence[int]) -> list[int]:
        """
        Find the book of a crowd that moved from the source a file names.

        The file is the book's, from before it moved to another folder, where
        it declares the book's usable identifier, no other book is known to
        declare that, and the book of the library at that source, if any, is
        known not to (:func:`~epubconvert.run.holders.moved`): each book of
        the crowd has its identifier read, as for a file that names no
        source, and so has that book.

        :param found: The file.
        :param crowd: The books of its crowd to ask, by index.

        :return: That book, by index, or nothing.
        """
        identifiers = {i: self._identifier(self.books[i]) for i in crowd}
        declared = Counter(
            book.identifier for book in self.books if book.identifier is not None
        )
        for i, identifier in identifiers.items():
            if isinstance(identifier, str):
                self.told.identifiers[i] = identifier
                if self.books[i].identifier is None:
                    declared[identifier] += 1
        return [
            i
            for i, identifier in identifiers.items()
            if isinstance(identifier, str)
            and moved(found.path, identifier, self.sources, declared, self._declares)
        ]

    def _declares(self, source: str) -> object:
        """Say what the one book of *source* declares, as naming or a read says."""
        book = self.by_source.get(source)
        return UNREAD if book is None else self._identifier(book)

    def unmarked(self, found: _File, *, refuse: bool = True) -> None:
        """
        Say whose a file that names no source is, by the identifiers.

        :param found: The file.
        :param refuse: Refuse the file to books nothing tells apart. Not for
            a file whose marker names a source two books share, which is
            theirs and must not cost both their files on every run.
        """
        declared = identifier_on_shelf(found.path)
        identifiers = {i: self._identifier(self.books[i]) for i in found.crowd}
        for i, identifier in identifiers.items():
            if isinstance(identifier, str):
                self.told.identifiers[i] = identifier
        owners = [i for i, identifier in identifiers.items() if identifier == declared]
        if declared is not None and len(owners) == 1:
            if found.kept:
                self.told.keeps.setdefault(owners[0], found.path.name)
            return
        if not refuse:
            return
        counts = Counter(value for value in identifiers.values() if value is not None)
        # Told apart from each other by nothing -- no usable identifier, or
        # one between them -- and not refused by an identifier the file
        # declares that they do not.
        untold = [
            i
            for i, identifier in identifiers.items()
            if (identifier is None or counts[identifier] > 1 or identifier is UNREAD)
            and (declared is None or identifier in (UNREAD, declared))
        ]
        if len(untold) < 2:
            return
        reason = (
            "cannot tell these books apart: "
            f"{', '.join(self.shown[i] for i in untold)}: {found.path.name} names "
            "no source, and nothing they declare tells them apart"
        )
        self.told.refused[found.path.name] = reason
        for i in untold:
            self.told.untold.setdefault(i, reason)

    def _identifier(self, book: Wanting) -> object:
        """
        Read a book's usable identifier, unless opening it would download it.

        :param book: The book.

        :return: The identifier, None when it declares none, or
            :data:`UNREAD`.
        """
        return declared_by(book, self.unopened)
