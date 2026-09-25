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
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Container, Sequence
from pathlib import Path
from typing import NamedTuple

from ..export.naming import filesystem_key
from ..utils.policy import NamingPolicy
from .claims import Wanting, numbered_names
from .holders import identifier_on_shelf, marker_on_shelf, moved, source_identifier

#: A book whose identifier was not read, because reading it downloads it.
_UNREAD = object()


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
    judge = _Judge(books, shown, unopened)
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
        self, books: Sequence[Wanting], shown: Sequence[str], unopened: Container[Path]
    ) -> None:
        self.books = books
        self.shown = shown
        self.unopened = unopened
        self.told = Told({}, {}, {}, {})
        #: How many books have each source: a source two have names neither.
        self.sources = Counter(book.source for book in books if book.source)

    def marked(self, found: _File) -> bool:
        """
        Say whose a file is by its marker, if it names a source.

        :param found: The file.

        :return: True when its marker settled it.
        """
        name = found.path.name
        source = marker_on_shelf(found.path)
        if source is None or self.sources[source] > 1:
            return False
        owners = [i for i in found.crowd if self.books[i].source == source]
        if not owners and source not in self.sources:
            owners = self._moved(found)
        if not owners:
            self.told.refused[name] = f"{name} was written for another book"
        elif found.kept:
            self.told.keeps.setdefault(owners[0], name)
        return True

    def _moved(self, found: _File) -> list[int]:
        """
        Find the book of a crowd that moved from the source a file names.

        No book of the library has that source any more. The file is the
        book's, from before it moved to another folder, where it declares the
        book's usable identifier and no other book is known to declare that
        (:func:`~epubconvert.run.holders.moved`): each book of the crowd has
        its identifier read, as for a file that names no source.

        :param found: The file.

        :return: That book, by index, or nothing.
        """
        identifiers = {i: self._identifier(self.books[i]) for i in found.crowd}
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
            and moved(found.path, identifier, self.sources, declared)
        ]

    def unmarked(self, found: _File) -> None:
        """
        Say whose a file that names no source is, by the identifiers.

        :param found: The file.
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
        counts = Counter(value for value in identifiers.values() if value is not None)
        # Told apart from each other by nothing -- no usable identifier, or
        # one between them -- and not refused by an identifier the file
        # declares that they do not.
        untold = [
            i
            for i, identifier in identifiers.items()
            if (identifier is None or counts[identifier] > 1 or identifier is _UNREAD)
            and (declared is None or identifier in (_UNREAD, declared))
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
            :data:`_UNREAD`.
        """
        if book.identifier is not None or book.unread is None:
            return book.identifier
        if book.unread in self.unopened:
            return _UNREAD
        return source_identifier(book.unread)
