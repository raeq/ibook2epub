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
from zipfile import BadZipFile, ZipFile

from ..collect.validate import ValidationError, read_package, usable_identifier
from ..export.naming import filesystem_key
from ..utils.policy import Assignment, NamingPolicy
from ..utils.spec import PACKAGE_SUFFIX
from .claims import Claims, lost_to
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
    at a name: a copy that lost its name but finds its own bytes under it --
    copied before the package arrived -- keeps that file, and the package that
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
        existing={
            filesystem_key(policy.identity(found.name)): found
            for found in (output_dir.glob(f"*{PACKAGE_SUFFIX}") if output_dir else ())
            if found.is_file()
        },
        unopened=unopened,
    )
    for item in assigned:
        if item.filename:
            claiming.claims.take(item.identity, 1, item.identity, item.filename)
            claiming.holders[filesystem_key(item.identity)] = item.filename

    named = [
        claiming.name(source, name, policy.identity(name))
        for source, name in sorted(copies, key=lambda entry: entry[0])
        if name is not None
    ]
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
        with ZipFile(source) as archive:
            return usable_identifier(read_package(archive))
    except (ValidationError, BadZipFile, OSError):
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
