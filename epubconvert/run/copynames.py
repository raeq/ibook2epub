"""
Naming the files copied through in the claim pass the packages were named in.

Split from :mod:`epubconvert.run.planning` when that module reached the line
limit; the rules a name is claimed by are that module's, and applied here to
the files that are taken along rather than converted.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

from ..collect.identifiers import usable_identifier
from ..collect.package import ValidationError, read_archive_package
from ..export.archive import COPYABLE_SUFFIXES
from ..export.naming import filesystem_key
from ..utils.policy import Assignment, NamingPolicy
from .claims import Claims, claim_order, lost_to, shelf_names
from .holders import identifier_on_shelf
from .planning import SUFFIX, CollisionMode, _claim, _metadata_of, _Naming


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
    unopened: Collection[Path] = frozenset(),
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
    :param unopened: Files not to open, because opening them downloads them.

    :return: The packages' names, some with an identifier read, and the copies'.
    """
    claiming = _Claiming(
        _Naming(policy, on_collision, getattr(policy, "max_bytes", 0)),
        existing=_on_shelf(output_dir, policy),
        unopened=unopened,
    )
    for item in assigned:
        if item.filename:
            claiming.claims.take(item.identity, 1, item.identity, item.filename)
            claiming.holders[filesystem_key(item.identity)] = item.filename

    wanting = sorted(
        ((source, name) for source, name in copies if name is not None),
        key=lambda entry: entry[0],
    )
    # A copy whose name is on the shelf claims first, as a package does, and
    # before it one whose own bytes are that file: two PDFs of one name have
    # no identifier to tell them apart, and the first in sorted order took the
    # other's file for its own copy and was never copied.
    order = sorted(
        claim_order([name for _, name in wanting], shelf_names(output_dir)),
        key=lambda index: not claiming.owns(*wanting[index], policy),
    )
    claimed = {
        index: claiming.name(*wanting[index], policy.identity(wanting[index][1]))
        for index in order
    }
    named = [claimed[index] for index in range(len(wanting))]
    wanted = {filesystem_key(policy.identity(name)) for _, name in copies if name}
    return Names(_identified(assigned, wanted, policy), named)


@dataclass
class _Claiming:
    """The names spoken for so far, and what the shelf already holds."""

    setup: _Naming
    #: The shelf's archives, by filesystem key.
    existing: dict[str, Path]
    #: Files not to open, because opening them downloads them.
    unopened: Collection[Path]
    claims: Claims = field(default_factory=Claims)
    #: The name that took each filesystem key.
    holders: dict[str, str] = field(default_factory=dict)

    def owns(self, source: Path, name: str, policy: NamingPolicy) -> bool:
        """
        Report whether the shelf's file under *name* can be *source*'s copy.

        :param source: The file to copy.
        :param name: The name it wants.
        :param policy: The naming policy in force.

        :return: True if a file is there and is *source*'s size.
        """
        found = self.existing.get(filesystem_key(policy.identity(name)))
        return found is not None and _same_size(source, found)

    def name(self, source: Path, name: str, group: str) -> Assignment:
        """
        Claim the first free name for one file, or settle why it has none.

        :param source: The file to copy.
        :param name: The name it wants.
        :param group: That name's identity.

        :return: Its assignment.
        """
        key = filesystem_key(group)
        found = self.existing.get(key)
        if found is not None and key in self.holders and _same_size(source, found):
            # Copied before the book now holding the name arrived: the copy
            # keeps its file, whatever the mode, and that book moves on
            # (placing.place) or is a collision. Settled only in _lost, this
            # never ran under --on-collision suffix, where a free " (n)"
            # always exists: the copy was written again under one and its
            # file listed as an orphan.
            return Assignment(source, found.name, group)
        taken = _claim(self.claims, name, group, setup=self.setup)
        if taken is None:
            return self._lost(source, group)
        filename, key = taken
        self.holders[filesystem_key(key)] = filename
        found = self.existing.get(filesystem_key(key))
        return Assignment(
            source,
            filename,
            key,
            identifier=(
                self._identifier(source)
                if found is not None and not _same_size(source, found)
                else None
            ),
            # Positional, from the name it wanted: a copy has no digest of its
            # own identifier to move on to. See placing.place.
            marked=name if self.setup.on_collision == SUFFIX else None,
        )

    def _lost(self, source: Path, group: str) -> Assignment:
        """
        Settle a copy that lost its name: its own file under it, or a collision.

        :param source: The file to copy.
        :param group: The identity of the name it wanted.

        :return: The copy at its own file, or nameless with the reason.
        """
        key = filesystem_key(group)
        found = self.existing.get(key)
        identifier = None
        if found is not None:
            if _same_size(source, found):
                return Assignment(source, found.name, group)
            # Read only where there is a file to compare, so a rerun that loses
            # the same name opens nothing it did not open before.
            identifier = self._identifier(source)
            if identifier is not None and identifier_on_shelf(found) == identifier:
                return Assignment(source, found.name, group, identifier=identifier)
        return Assignment(
            source,
            "",
            group,
            lost_to(self.holders.get(key), None)
            + (f"; this book is {identifier}" if identifier else ""),
            identifier=identifier,
        )

    def _identifier(self, source: Path) -> str | None:
        """Read a file's identifier, unless opening it would download it."""
        return None if source in self.unopened else _copy_identifier(source)


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
    if output_dir is None or not output_dir.is_dir():
        return {}
    return {
        filesystem_key(policy.identity(found.name)): found
        for found in output_dir.iterdir()
        if found.suffix.lower() in COPYABLE_SUFFIXES and found.is_file()
    }


def _identified(
    assigned: Sequence[Assignment], wanted: set[str], policy: NamingPolicy
) -> list[Assignment]:
    """
    Read the identifier of each folder-named package a copy wanted the name of.

    :param assigned: The packages' names.
    :param wanted: Filesystem keys of the names the copies wanted.
    :param policy: The naming policy in force.

    :return: The packages' names, with those identifiers.
    """
    if getattr(policy, "needs_metadata", False):
        return list(assigned)
    return [
        (
            replace(
                item,
                identifier=usable_identifier(_metadata_of(item.package, True)),
            )
            if item.filename and filesystem_key(item.identity) in wanted
            else item
        )
        for item in assigned
    ]


def _copy_identifier(source: Path) -> str | None:
    """Read an already-zipped book's usable identifier; None for anything else."""
    try:
        return usable_identifier(read_archive_package(source))
    except ValidationError:
        return None


def _same_size(source: Path, found: Path) -> bool:
    """
    Whether *found* can be *source*'s copy, which is written byte for byte.

    A stat each, which downloads nothing, so a rerun over a shelf of copies
    opens none of them to know they are there.
    """
    try:
        return source.stat().st_size == found.stat().st_size
    except OSError:  # pragma: no cover - racing removal
        return False
