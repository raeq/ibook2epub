"""
Naming the files copied through in the claim pass the packages were named in.

Split from :mod:`epubconvert.run.planning` when that module reached the line
limit; the rules a name is claimed by are that module's, and applied here to
the files that are taken along rather than converted.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Container, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

from ..collect.identifiers import usable_identifier
from ..collect.package import ValidationError, read_archive_package
from ..export.naming import DISAMBIGUATOR_CHARS, disambiguator, filesystem_key
from ..utils.policy import Assignment, NamingPolicy
from ..utils.spec import PACKAGE_SUFFIX
from .claims import (
    NUMBERED,
    Claims,
    claim_order,
    lost_to,
    shelf_files,
    shelf_names,
)
from .holders import identifier_on_shelf
from .planning import SUFFIX, CollisionMode, _claim, _metadata_of, _Naming

#: A name marked by planning._stable_base, numbered or not, as a filesystem
#: key: its stem, the digest, and the extension.
_MARKED = re.compile(
    rf"(?P<stem>.*) \[(?P<digest>[0-9a-f]{{{DISAMBIGUATOR_CHARS}}})\]"
    r"(?: \(\d+\))?(?P<extension>\.[^.]*)?"
)


class Names(NamedTuple):
    """Every name a run gives: the packages', then the files copied through."""

    packages: list[Assignment]
    #: One per copy that was named, in sorted order; an empty filename means it
    #: lost its name, and the reason says to what.
    copies: list[Assignment]


def claim_copies(
    assigned: Sequence[Assignment],
    copies: Sequence[tuple[Path, str | None]],
    policy: NamingPolicy,
    on_collision: CollisionMode,
    *,
    output_dir: Path | None,
    unopened: Container[Path] = frozenset(),
) -> Names:
    """
    Name the files copied through in the claim pass the packages were named in.

    Packages were named by the planner and copies by
    :func:`~epubconvert.run.planning.copy_target_name`, and the
    two never met: ``a/Book.epub/`` and a zipped ``b/Book.epub`` both wanted
    ``Book.epub``. The copy ran first, so the package was reported exported
    from the zipped book's file on every run; the other way round the copy
    found the package's archive and was dropped without a word, as was the
    second of two zipped editions under one ``--name-by author-title`` name.

    The packages keep the names
    :func:`~epubconvert.run.planning.assign_names` gave them, so every other
    caller, which names packages alone, agrees with the run. Each copy then
    takes the first name that is free, in sorted order, so the first still
    wins: the loser is a collision, or under ``--on-collision suffix`` it takes
    the next
    ``" (n)"``. A copy that has no identifier to digest has no stabler marker.

    What is already on the shelf is weighed as for a package
    (:func:`epubconvert.run.placing.place`), and read only where two books meet
    at a name: a copy that finds its own bytes under the name another book
    holds -- copied before the package arrived -- keeps that file in either
    mode, rather than being copied again under a suffix, and the package that
    now wants it has its identifier read, as a folder-named book about to be
    written has, so it is not reported exported from the other book's file.

    :param assigned: The whole library's package names, from
        :func:`~epubconvert.run.planning.assign_names`.
    :param copies: Each file to copy with the name it wants, or None when it
        was left unnamed; see :class:`epubconvert.run.copying.CopyPlan`.
    :param policy: The naming policy in force.
    :param on_collision: How a collision is settled.
    :param output_dir: Directory holding exported files, or None to name the
        files without looking at the shelf, as a run that reads only Apple's
        container does.
    :param unopened: Books not to open, because opening them downloads them:
        the copies' files, and under ``--skip-incomplete`` an evicted
        package (:class:`~epubconvert.run.holders.Unopened`).

    :return: The packages' names, some with an identifier read, and the copies'.
    """
    wanting = sorted(
        ((source, name) for source, name in copies if name is not None),
        key=lambda entry: entry[0],
    )
    wants = Counter(filesystem_key(item.identity) for item in assigned if item.filename)
    wants.update(filesystem_key(policy.identity(name)) for _, name in wanting)
    claiming = _Claiming(
        _Naming(policy, on_collision, getattr(policy, "max_bytes", 0)),
        existing=_on_shelf(output_dir, policy),
        unopened=unopened,
        contested={key for key, count in wants.items() if count > 1},
        stamps=frozenset(_stamp(source)[1] for source, _ in wanting),
    )
    for item in assigned:
        if item.filename:
            claiming.claims.take(item.identity, 1, item.identity, item.filename)
            claiming.holders[filesystem_key(item.identity)] = item.filename
            claiming.packaged[filesystem_key(item.identity)] = item

    order = claim_order([name for _, name in wanting], shelf_names(output_dir))
    kept = claiming.keep_all(wanting, order)
    assigned = [claiming.reclaim(item) for item in assigned]
    named = claiming.settle(wanting, order, kept)
    wanted = {filesystem_key(policy.identity(name)) for _, name in copies if name}
    # And the file a copy keeps: a package with no identifier to go by keeps
    # the one numbered file of its name (claims.kept_numbers), which can be a
    # copy's. Read, its identifier moves it on (placing.place); unread, it
    # was reported exported from the copy's file. formal/RerunPlanner.tla
    # found it.
    wanted.update(filesystem_key(item.identity) for item in named if item.filename)
    return Names(_identified(assigned, wanted, policy, unopened), named)


@dataclass
class _Claiming:
    """The names spoken for so far, and what the shelf already holds."""

    setup: _Naming
    #: The shelf's archives, by filesystem key.
    existing: dict[str, Path]
    #: Books not to open, because opening them downloads them.
    unopened: Container[Path]
    #: Filesystem keys more than one book of the pass wants.
    contested: set[str] = field(default_factory=set)
    claims: Claims = field(default_factory=Claims)
    #: The name that took each filesystem key.
    holders: dict[str, str] = field(default_factory=dict)
    #: Each copy's identifier, once read.
    read: dict[Path, str | None] = field(default_factory=dict)
    #: The shelf's files by the key of their name less any " (n)", with n.
    numbered: dict[str, list[tuple[int, Path]]] = field(default_factory=dict)
    #: The shelf's files by the key of their name less any digest marker and
    #: " (n)", with the digest.
    marked: dict[str, list[tuple[str, Path]]] = field(default_factory=dict)
    #: The package given each name in the pass, by filesystem key.
    packaged: dict[str, Assignment] = field(default_factory=dict)
    #: The shelf's files a copy has kept as its own, each by one copy only.
    taken: set[Path] = field(default_factory=set)
    #: The modification time of every file the pass copies, in nanoseconds.
    stamps: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        for key, found in self.existing.items():
            numbered = NUMBERED.fullmatch(key)
            if numbered is None:
                self.numbered.setdefault(key, []).append((1, found))
            else:
                plain = numbered["stem"] + (numbered["extension"] or "")
                position = int(numbered["position"])
                self.numbered.setdefault(plain, []).append((position, found))
            if marked := _MARKED.fullmatch(key):
                plain = marked["stem"] + (marked["extension"] or "")
                self.marked.setdefault(plain, []).append((marked["digest"], found))

    def keep_all(
        self, wanting: Sequence[tuple[Path, str]], order: Sequence[int]
    ) -> dict[int, Assignment]:
        """
        Keep every copy whose own file is on the shelf at that file.

        A copy whose own bytes are on the shelf under its name or one of its
        numbers claims first: two PDFs of one name have no identifier to tell
        them apart, and the first in sorted order took the other's file for
        its own copy and was never copied. Of those, one whose file stat says
        is its copy before one whose file only declares its identifier,
        which two copies of one book share. Each file is kept by one copy at
        most, so one that finds its file already kept claims a name with the
        rest (:meth:`settle`).

        :param wanting: Each copy and the name it wants, in sorted order.
        :param order: Indices into *wanting*, from
            :func:`~epubconvert.run.claims.claim_order`.

        :return: The copies kept, by index into *wanting*.
        """
        claimed: dict[int, Assignment] = {}
        for exact in (True, False):
            for index in order:
                if index not in claimed and (kept := self.keep(*wanting[index], exact)):
                    claimed[index] = kept
        return claimed

    def reclaim(self, item: Assignment) -> Assignment:
        """
        Name a package again that kept a numbered file a copy keeps.

        A package with no identifier to go by keeps the one numbered file of
        its name when no other package wants the name and nothing holds the
        plain one (claims.kept_numbers). The copies were not asked, and the
        file can be a copy's: the package kept it and moved on past it, a
        copy claimed the plain name the package had left, and a later run
        found two numbered files where the package had one, kept neither,
        and listed the package's only archive as an orphan.
        formal/RerunPlanner.tla found it. The file stays the copy's; the
        package claims the first free name of its own, as before it kept any.

        :param item: A package's assignment.

        :return: It, or when a copy keeps the numbered file it kept, its
            first free name, or no name and the reason.
        """
        if not item.kept_number or item.marked is None:
            return item
        key = filesystem_key(item.identity)
        policy = self.setup.policy
        if not any(
            filesystem_key(policy.identity(found.name)) == key for found in self.taken
        ):
            return item
        self.packaged.pop(key, None)
        wanted = item.marked
        taken = _claim(self.claims, wanted, policy.identity(wanted), setup=self.setup)
        if taken is None:  # pragma: no cover - every " (n)" spoken for
            return replace(
                item,
                filename="",
                identity=policy.identity(wanted),
                reason=lost_to(
                    self.claims.holder(policy.identity(wanted), wanted), None
                ),
            )
        filename, identity = taken
        self.holders[filesystem_key(identity)] = filename
        self.packaged[filesystem_key(identity)] = item
        return replace(item, filename=filename, identity=identity, kept_number=False)

    def settle(
        self,
        wanting: Sequence[tuple[Path, str]],
        order: Sequence[int],
        kept: dict[int, Assignment],
    ) -> list[Assignment]:
        """
        Name every copy :meth:`keep_all` did not keep, in the order to claim.

        One whose name is on the shelf claims first, as a package does.

        :param wanting: Each copy and the name it wants, in sorted order.
        :param order: Indices into *wanting*, from
            :func:`~epubconvert.run.claims.claim_order`.
        :param kept: The copies kept at their own files.

        :return: Each copy's assignment, in the order of *wanting*.
        """
        claimed = dict(kept)
        for index in order:
            if index not in claimed:
                source, name = wanting[index]
                claimed[index] = self.name(
                    source, name, self.setup.policy.identity(name)
                )
        return [claimed[index] for index in range(len(wanting))]

    def kept(
        self, source: Path, name: str, *, exact: bool = False
    ) -> tuple[Path, str, str | None] | None:
        """
        Find *source*'s own copy on the shelf, under its name or one of its numbers.

        A copy already on the shelf keeps its file, and not only under the
        plain name: in suffix mode the copy numbered " (2)" was pushed to
        " (3)" by a package or an earlier copy arriving, found another
        book's file under the next free number, or none, and was copied
        again. formal/RerunPlanner.tla found it. Only the files under the
        name's numbers are looked at, from an index of the shelf, and in
        suffix mode those under its digest-marked forms (:meth:`_marked`).

        :param source: The file to copy.
        :param name: The name it wants.
        :param exact: Only a file :func:`_same_file` finds is *source*'s will
            do.

        :return: The file, its identity and *source*'s identifier when it was
            read; None when no file there is *source*'s.
        """
        policy = self.setup.policy
        wanted = filesystem_key(policy.identity(name))
        numbers = self.numbered.get(wanted, [])
        if self.setup.on_collision != SUFFIX:
            numbers = [entry for entry in numbers if entry[0] == 1]
        for _position, found in sorted(numbers):
            # One file is one copy's: two copies of one book, or a copy and a
            # package of one, share an identifier, and the second took the
            # first's file for its own. The collision the first run reported
            # was gone from the next, and in suffix mode the second copy's own
            # file was listed as an orphan.
            if found in self.taken or self._packages(source, found):
                continue
            # Whether another book wants it is a question about the name,
            # not the number.
            own, identifier = self._own(source, found, wanted, exact=exact)
            if own:
                return found, policy.identity(found.name), identifier
        if exact or self.setup.on_collision != SUFFIX:
            return None
        return self._marked(source, wanted)

    def _marked(self, source: Path, wanted: str) -> tuple[Path, str, str] | None:
        """
        Find *source*'s archive under its name marked with its digest.

        A package in a crowd takes a digest-marked name. Zipped in place, it
        is a file copied through, which has no digest and wants the plain
        name, so the archive it had was nobody's: under
        ``--no-copy-through``, its only one, listed as an orphan. Its
        identifier is read, and the archive's, only when a file of its name
        marked with its digest is on the shelf. A name the marker had to be
        cut short to fit is not found here.

        :param source: The file to copy.
        :param wanted: The filesystem key of the name it wants.

        :return: The file, its identity and *source*'s identifier; None when
            no such file holds *source*'s book.
        """
        candidates = self.marked.get(wanted)
        if not candidates:
            return None
        identifier = self._identifier(source)
        if identifier is None:
            return None
        digest = disambiguator(identifier)
        for mark, found in sorted(candidates):
            if (
                mark == digest
                and found not in self.taken
                and not self._packages(source, found)
                and identifier_on_shelf(found) == identifier
            ):
                return found, self.setup.policy.identity(found.name), identifier
        return None

    def keep(self, source: Path, name: str, exact: bool) -> Assignment | None:
        """
        Keep a copy at its own file on the shelf, if it has one no copy kept.

        Copied before the book now holding the name arrived: the copy keeps
        its file, whatever the mode, and that book moves on (placing.place) or
        is a collision. Settled only in _lost, this never ran under
        ``--on-collision suffix``, where a free ``" (n)"`` always exists: the
        copy was written again under one and its file listed as an orphan.

        :param source: The file to copy.
        :param name: The name it wants.
        :param exact: Only a file :func:`_same_file` finds is *source*'s will
            do.

        :return: Its assignment, or None when it has to claim a name.
        """
        kept = self.kept(source, name, exact=exact)
        if kept is None:
            return None
        mine, identity, identifier = kept
        self.taken.add(mine)
        # Refused only where a package was given the name, since no other
        # copy's file gets here: the copy's own bytes, which it keeps while
        # that package moves on.
        self.claims.take(identity, 1, identity, mine.name)
        self.holders.setdefault(filesystem_key(identity), mine.name)
        return Assignment(source, mine.name, identity, identifier=identifier)

    def name(self, source: Path, name: str, group: str) -> Assignment:
        """
        Claim the first free name for one file, or settle why it has none.

        :param source: The file to copy.
        :param name: The name it wants.
        :param group: That name's identity.

        :return: Its assignment.
        """
        taken = _claim(self.claims, name, group, setup=self.setup)
        if taken is None:
            return self._lost(source, group)
        filename, key = taken
        self.holders[filesystem_key(key)] = filename
        found = self.existing.get(filesystem_key(key))
        # Read where the file there may be another book's, so the plan can
        # tell (placing.place). Whether another book wants it is a question
        # about the name it wanted, as in kept.
        own, identifier = (
            self._own(source, found, filesystem_key(group))
            if found is not None
            else (True, None)
        )
        # A zipped book left unopened may declare the identifier that says
        # the file is its own; only a size that differs cannot tell.
        doubt = source in self.unopened and source.suffix.lower() == PACKAGE_SUFFIX
        return Assignment(
            source,
            filename,
            key,
            identifier=identifier,
            # Positional, from the name it wanted: a copy has no digest of its
            # own identifier to move on to. See placing.place.
            marked=name if self.setup.on_collision == SUFFIX else None,
            # Its size says so where no identifier can: a PDF of another size
            # under its name, once the book copied there left the library,
            # was placed at that file and never copied.
            not_own=not (own or doubt),
        )

    def _lost(self, source: Path, group: str) -> Assignment:
        """
        Settle a copy that lost its name, which is a collision.

        :param source: The file to copy.
        :param group: The identity of the name it wanted.

        :return: The copy, nameless, with the reason.
        """
        key = filesystem_key(group)
        found = self.existing.get(key)
        # The file under the name is not its own (kept): read what this book
        # is, for the reason.
        identifier = None if found is None else self._own(source, found, key)[1]
        return Assignment(
            source,
            "",
            group,
            lost_to(self.holders.get(key), None)
            + (f"; this book is {identifier}" if identifier else ""),
            identifier=identifier,
        )

    def _packages(self, source: Path, found: Path) -> bool:
        """
        Decide whether *found* may be the archive of the package named after it.

        A package given the name in this pass claimed it first. The file is
        another book's when the identifiers say so: a copy on the shelf
        before a package of its name arrived. When they cannot, because one
        of them declares none, a file of the copy's size is the copy's. A
        copy and a package of one book declare one identifier, and the copy
        took the package's archive for its own on the next run: the size
        does not tell them apart there, since a converted archive can be the
        size of the zipped book it was made from. A file with the copy's size
        and modification time is the copy's, whatever the identifiers say.

        :param source: The file to copy.
        :param found: A file on the shelf under a name it wants.

        :return: True when a package was given its name and it may be that
            package's archive.
        """
        key = filesystem_key(self.setup.policy.identity(found.name))
        item = self.packaged.get(key)
        if item is None or _stamp(found) == _stamp(source):
            return False
        identifier = item.identifier
        if identifier is None and not getattr(
            self.setup.policy, "needs_metadata", False
        ):
            # Named from the folder, so the package document was not read.
            identifier = _package_identifier(item.package, self.unopened)
        holder = identifier_on_shelf(found) if identifier is not None else None
        if holder is not None:
            return holder == identifier
        return not _same_file(source, found, self.stamps)

    def _own(
        self, source: Path, found: Path, key: str, *, exact: bool = False
    ) -> tuple[bool, str | None]:
        """
        Decide whether *found* is *source*'s copy, which is written byte for byte.

        Its size and modification time say so while no other book of the
        pass wants the name (:func:`_same_file`): a stat each, which
        downloads nothing, so a rerun over a shelf of copies opens none of
        them. Where another book wants it too, two different
        zipped books of one size told apart by nothing else, and the one
        added later was never copied; there the identifiers decide. They
        decide too for a file of another size, which may be the copy's own
        from before Apple rewrote the book. A PDF has none, and its size is
        all there is.

        :param source: The file to copy.
        :param found: The file on the shelf under the name it wants.
        :param key: That name's filesystem key.
        :param exact: Only a file :func:`_same_file` finds is *source*'s will
            do, whatever the identifiers say.

        :return: Whether it is, and *source*'s identifier when it was read.
        """
        same = _same_file(source, found, self.stamps)
        if same and key not in self.contested:
            return True, None
        identifier = self._identifier(source)
        if identifier is not None:
            holder = identifier_on_shelf(found)
            if holder is not None:
                return holder == identifier and (same or not exact), identifier
        return same, identifier

    def _identifier(self, source: Path) -> str | None:
        """Read a file's identifier, unless opening it would download it."""
        if source not in self.read:
            self.read[source] = (
                None if source in self.unopened else _copy_identifier(source)
            )
        return self.read[source]


def _on_shelf(output_dir: Path | None, policy: NamingPolicy) -> dict[str, Path]:
    """
    Find every file on the shelf a copy could have been written to.

    Everything :func:`~epubconvert.export.archive.collect_copyable` takes
    along, whatever the case of its extension: the pass looked for ``*.epub``
    alone, so a PDF on the shelf was invisible to it.

    :param output_dir: Directory holding exported files, or None.
    :param policy: The naming policy in force.

    :return: The files, by filesystem key.
    """
    if output_dir is None:
        return {}
    return {
        filesystem_key(policy.identity(found.name)): found
        for found in shelf_files(output_dir)
    }


def _identified(
    assigned: Sequence[Assignment],
    wanted: set[str],
    policy: NamingPolicy,
    unopened: Container[Path],
) -> list[Assignment]:
    """
    Read the identifier of each folder-named package a copy wanted the name of.

    :param assigned: The packages' names.
    :param wanted: Filesystem keys of the names the copies wanted.
    :param policy: The naming policy in force.
    :param unopened: Books not to open, because opening them downloads them.

    :return: The packages' names, with those identifiers.
    """
    if getattr(policy, "needs_metadata", False):
        return list(assigned)
    return [
        (
            replace(item, identifier=_package_identifier(item.package, unopened))
            if item.filename and filesystem_key(item.identity) in wanted
            else item
        )
        for item in assigned
    ]


def _package_identifier(package: Path, unopened: Container[Path]) -> str | None:
    """
    Read a package's usable identifier, unless opening it would download it.

    Asked before the read: under ``--skip-incomplete`` the package document
    of a book iCloud had evicted was downloaded to be compared, before the
    inspection that calls the book not downloaded.
    """
    if package in unopened:
        return None
    return usable_identifier(_metadata_of(package, True))


def _copy_identifier(source: Path) -> str | None:
    """Read an already-zipped book's usable identifier; None for anything else."""
    try:
        return usable_identifier(read_archive_package(source))
    except ValidationError:
        return None


def _stamp(path: Path) -> tuple[int, int]:
    """A file's size and modification time in nanoseconds; a stat, no download."""
    try:
        status = path.stat()
    except OSError:  # pragma: no cover - racing removal
        return -1, -1
    return status.st_size, status.st_mtime_ns


def _same_file(source: Path, found: Path, stamps: Collection[int]) -> bool:
    """
    Decide from a stat of each whether *found* is *source*'s copy.

    A copy keeps its source's modification time
    (:func:`~epubconvert.export.archive.copy_through`), so the size and the
    time together say so. By size alone, a zipped book or a PDF of the same
    size replacing a deleted one was taken as already copied, and never was.

    Copies made before that took the time they were written, which is later
    than their source's. A file newer than the source is taken as one of
    those, by its size alone, as it always was: otherwise every copy on a
    shelf made before the upgrade was another book's on the first run after
    it, and copied again or reported. Unless its time is that of a file this
    pass copies, when it is that file's copy. A file older than the source is
    not its copy: the source changed since, or it is another file.

    :param source: The file to copy.
    :param found: A file on the shelf under a name it wants.
    :param stamps: The modification time of every file the pass copies.

    :return: True when stat says *found* is *source*'s copy.
    """
    size, made = _stamp(source)
    found_size, found_made = _stamp(found)
    if size != found_size:
        return False
    if found_made == made:
        return True
    return found_made > made and found_made not in stamps
