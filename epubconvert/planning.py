"""
Deciding what to do with each package, before anything is written.

Planning is kept separate from exporting so that ``--list`` can render the
same decisions the exporter acts on, and so the rules that make a rerun safe
live in one place: identity is recomputed from the filenames already in the
output directory, which is why this tool needs no state file.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .app_logger import logger
from .display import printable
from .naming import (
    NamingPolicy,
    encode_name,
    filesystem_key,
    split_extension,
    truncate_bytes,
)
from .source import inspect_package
from .spec import PACKAGE_SUFFIX
from .validate import Package, ValidationError, read_package_dir

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for typing only
    from .convert import Report

#: What the planner can decide about a package. These strings are a public
#: contract, not an internal detail: ``--list`` names them all in its help text
#: and ``--list --json`` emits them verbatim. Typing them keeps a mistyped
#: comparison from passing the type checker.
Status = Literal["pending", "exported", "collision", "drm", "incomplete", "orphan"]

#: The :class:`~epubconvert.convert.Report` fields the outcome table may bump.
ReportField = Literal["skipped", "collisions", "drm", "incomplete"]

#: Decision statuses. Each constant's value is what a user sees.
PENDING: Status = "pending"
EXPORTED: Status = "exported"
COLLISION: Status = "collision"
DRM: Status = "drm"
INCOMPLETE: Status = "incomplete"

#: Not a decision about a book in the library at all: an archive on the shelf
#: that no book in the library claims. Reported, never acted on.
ORPHAN: Status = "orphan"

#: How ``--on-collision`` may be set.
CollisionMode = Literal["skip", "suffix"]
SKIP: CollisionMode = "skip"
SUFFIX: CollisionMode = "suffix"
COLLISION_MODES = (SKIP, SUFFIX)

#: Every status, in the order --list documents them. Exported so the CLI help
#: can name them without restating the set: Status's own docstring calls that
#: help part of the contract, and a sixth status would otherwise leave it wrong
#: with nothing to catch it.
STATUSES: tuple[Status, ...] = (
    PENDING,
    EXPORTED,
    COLLISION,
    DRM,
    INCOMPLETE,
    ORPHAN,
)

#: Highest ``" (n)"`` suffix the planner will try before giving up on a name.
MAX_SUFFIX = 99


@dataclass(frozen=True)
class PlanOptions:
    """How the planner should treat the packages it is given."""

    force: bool = False
    refresh: bool = False
    check_incomplete: bool = False
    on_collision: CollisionMode = SKIP


@dataclass(frozen=True)
class _Existing:
    """An archive already in the output directory, and whose book it is."""

    path: Path
    identity: str


@dataclass
class Decision:
    """What the planner decided about one package."""

    package: Path
    status: Status
    target: Path | None = None
    reason: str | None = None


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

    # The module's own splitter, not Path().suffix: pathlib treats ".epub" as
    # extension-less, so the marker landed after it -- ".epub (2)" -- and no
    # *.epub glob matches that.
    stem_text, extension = split_extension(filename)
    marker = f" ({position})"
    candidate = f"{stem_text}{marker}{extension}"
    if not max_bytes or len(encode_name(candidate)) <= max_bytes:
        return candidate

    budget = max_bytes - len(encode_name(marker))
    budget -= len(encode_name(extension))
    stem_text = truncate_bytes(stem_text, max(budget, 1)).rstrip(" .") or "_"
    return f"{stem_text}{marker}{extension}"


class _Claims:
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

    def resume(self, group: str) -> int:
        """Return the first position worth trying for this group."""
        return self.positions.get(group, 1)

    def take(self, group: str, position: int, key: str, path_key: str) -> bool:
        """Claim a candidate if both its identity and its path are free."""
        if key in self.identities or path_key in self.paths:
            return False
        self.identities.add(key)
        self.paths.add(path_key)
        self.positions[group] = position + 1
        return True

    def exhaust(self, group: str, limit: int) -> None:
        """Record that this group has no positions left to try."""
        self.positions[group] = limit + 1


def _metadata_of(package: Path, wanted: bool) -> Package | None:
    """
    Read a package document, but only for a policy that asked for it.

    :param package: The package directory.
    :param wanted: Whether the naming policy needs the metadata at all.

    :return: The parsed package document, or None if it was not wanted or
        could not be read.
    """
    if not wanted:
        return None
    try:
        return read_package_dir(package)
    except (ValidationError, OSError):
        # One package in a surveyed 2,805-book library has no container.xml.
        # A book that cannot describe itself still deserves a name.
        return None


def assign_names(
    packages: Sequence[Path], policy: NamingPolicy, on_collision: CollisionMode
) -> list[tuple[Path, str, str]]:
    """
    Give every package an output name, resolving collisions deterministically.

    Assignment walks the packages in sorted order rather than the order they
    were selected, so the same set of packages always produces the same names
    regardless of shuffling.

    It is stable for a **fixed** set of packages only. Suffixes are positions
    within the colliding group, so adding a book that sorts earlier shifts
    every later member of that group, and ``--max-export-files`` selecting a
    different subset changes the group. Nothing here can do better without
    recording which archive belongs to which package, and this tool
    deliberately keeps no state file; identity keyed on the package document's
    dc:identifier would be the real fix. Until then, ``--on-collision
    suffix`` is best used with ``-m 0 --no-shuffle`` on a library that is not
    changing underneath it.

    Policies that name a book after its own metadata need the package document
    read first. That read is skipped entirely for the policies that do not ask
    for it, which is what keeps a no-op rerun over thousands of books free of
    any source-side open. A package that cannot be parsed yields no metadata
    rather than an error, and the policy falls back to the directory name.

    :param packages: Packages to name.
    :param policy: Naming policy supplying filenames and identities.
    :param on_collision: :data:`SKIP` or :data:`SUFFIX`.

    :return: Triples of package, filename and identity. A filename of ``""``
        marks a package that lost a collision.
    """
    assigned: list[tuple[Path, str, str]] = []
    claims = _Claims()

    wants_metadata = getattr(policy, "needs_metadata", False)

    for package in sorted(packages):
        wanted = policy.filename(package.name, _metadata_of(package, wants_metadata))
        limit = MAX_SUFFIX if on_collision == SUFFIX else 1
        group = policy.identity(wanted)

        for position in range(claims.resume(group), limit + 1):
            candidate = suffixed(wanted, position, getattr(policy, "max_bytes", 0))
            key = policy.identity(candidate)
            if claims.take(group, position, key, filesystem_key(candidate)):
                assigned.append((package, candidate, key))
                break
        else:
            claims.exhaust(group, limit)
            assigned.append((package, "", group))

    return assigned


def find_orphans(
    output_dir: Path,
    policy: NamingPolicy,
    packages: Sequence[Path],
    on_collision: CollisionMode = SKIP,
    claimed_extra: Sequence[str] = (),
) -> list[Path]:
    """
    Find archives on the shelf that no book in the library claims.

    The library has always been seen richly -- five statuses, reasons, tallies
    -- and the output directory not at all. An archive left behind by a book
    deleted from the library, or by adopting a naming policy that renames
    everything, sits there for ever: ``--verify`` blesses it because it is a
    sound archive, and ``--list`` only ever looked at sources.

    Asks the planner for the names rather than deriving them, so a book that
    took a ``" (2)"`` suffix is not reported as abandoning the name it holds.

    Nothing is deleted, here or anywhere. The never-deletes stance is
    deliberate; the gap was that nothing would say either.

    :param output_dir: Directory holding exported files.
    :param policy: Naming policy supplying filenames and identities.
    :param packages: **Every** package in the library, not the subset this run
        is looking at -- ``--match`` narrows a run, not the shelf.
    :param on_collision: The collision mode, so suffixed names are recognised.
    :param claimed_extra: Names claimed by something other than a package,
        such as a file copied through verbatim.

    :return: Archives no book accounts for, sorted by path.
    """
    claimed = {
        filesystem_key(key)
        for _package, filename, key in assign_names(packages, policy, on_collision)
        if filename
    }
    claimed |= {filesystem_key(policy.identity(name)) for name in claimed_extra}

    return sorted(
        found
        for found in output_dir.glob(f"*{PACKAGE_SUFFIX}")
        if found.is_file()
        and filesystem_key(policy.identity(found.name)) not in claimed
    )


def orphan_decisions(orphans: Sequence[Path]) -> list[Decision]:
    """
    Render orphans as decisions so one listing can carry both.

    :param orphans: Archives no book accounts for.

    :return: One decision per orphan.
    """
    return [
        Decision(path, ORPHAN, path, reason="no book in the library claims this name")
        for path in orphans
    ]


def plan_exports(
    packages: Sequence[Path],
    output_dir: Path,
    policy: NamingPolicy,
    options: PlanOptions | None = None,
) -> list[Decision]:
    """
    Decide what to do with every package, without doing any of it.

    Books already present are recognised by recomputing their identity from
    the filenames on disk, so the output directory stays the sole record of
    completed work and no state file is needed.

    :param packages: Package directories to consider.
    :param output_dir: Directory the epub files are written into.
    :param policy: Naming policy supplying filenames and identities.
    :param options: Planning behaviour; defaults are conservative.

    :return: One decision per package, in the order given.
    """
    settings = options if options is not None else PlanOptions()
    # Deduplicated, order preserved: the name map is keyed by path, so a
    # repeated package collapsed there while the decision list still emitted
    # one per element -- two workers writing the same target.
    packages = list(dict.fromkeys(packages))

    # Missing directories glob to nothing, which is what a dry run wants.
    # Keyed through the same fold the name assignment uses. Folding one and
    # not the other meant a book already exported under a different case was
    # never recognised, and was re-exported on every run for ever.
    existing = {
        filesystem_key(policy.identity(found.name)): _Existing(
            path=found, identity=policy.identity(found.name)
        )
        for found in output_dir.glob(f"*{PACKAGE_SUFFIX}")
        if found.is_file()
    }
    named = {
        package: (filename, key)
        for package, filename, key in assign_names(
            packages, policy, settings.on_collision
        )
    }

    return [
        _decide(package, named[package], existing, output_dir, settings)
        for package in packages
    ]


def _decide(
    package: Path,
    assignment: tuple[str, str],
    existing: dict[str, _Existing],
    output_dir: Path,
    settings: PlanOptions,
) -> Decision:
    """
    Decide what to do with a single package.

    :param package: The package directory.
    :param assignment: Its assigned filename and identity.
    :param existing: Identities already present in the output directory.
    :param output_dir: Directory the epub files are written into.
    :param settings: Planning behaviour.

    :return: The decision for this package.
    """
    filename, key = assignment
    if not filename:
        return Decision(
            package, COLLISION, reason="another book already claims this name"
        )

    clash = existing.get(filesystem_key(key))
    taken = _decide_against_clash(package, clash, key)
    if taken is not None:
        return taken
    found = clash.path if clash is not None else None
    # --force still has to pass inspection. Settling it here, before the walk
    # below, let a DRM-protected or half-downloaded source overwrite a good
    # archive -- the one path a user reaches for when something already looks
    # wrong. Only the "already done" answer may be given without inspecting.
    forced = found is not None and settings.force
    if not forced:
        settled = _decide_against_existing(package, found, settings)
        if settled is not None:
            return settled
    refreshing = found is not None and not forced

    unusable = _decide_against_source(package, settings)
    if unusable is not None:
        return unusable

    if found is not None and (forced or refreshing):
        # Written over the archive that is really there, not over a freshly
        # computed name: under a policy whose identity is looser than its
        # filename the two differ, and the stale file would keep satisfying
        # the identity check for ever.
        reason = "forced" if forced else "source is newer"
        return Decision(package, PENDING, found, reason=reason)

    return Decision(package, PENDING, output_dir / filename)


def _decide_against_clash(
    package: Path, clash: _Existing | None, key: str
) -> Decision | None:
    """
    Decide what an archive holding this filename settles, if anything.

    The filesystem key answers a looser question than identity: two different
    books can share it. When they do, the archive on disk is not this book, and
    neither available answer is "write it" -- that would replace another book's
    archive, and calling it exported would silently drop this one.

    :param package: The package directory.
    :param clash: The archive occupying this filename, if any.
    :param key: This package's identity.

    :return: A collision decision, or None when the name is this book's own or
        free.
    """
    if clash is None or clash.identity == key:
        return None
    return Decision(
        package, COLLISION, reason=f"{clash.path.name} already holds this name"
    )


def _decide_against_existing(
    package: Path, found: Path | None, settings: PlanOptions
) -> Decision | None:
    """
    Decide what an archive already in the output directory settles, if anything.

    Answered before the package is inspected: a book that is already exported
    needs no source walk, and ``--skip-incomplete`` would otherwise pay for one
    on every book of the library on every rerun.

    Only ever answers "already exported" or "needs looking at". The paths that
    write are settled by the caller *after* inspection, because a book that is
    about to be overwritten still has to be a book worth writing.

    :param package: The package directory.
    :param found: The archive already present under this identity, if any.
    :param settings: Planning behaviour.

    :return: The decision, or None when the package still needs inspecting.
    """
    if found is None:
        return None
    if settings.refresh and _source_is_newer(package, found):
        return None
    return Decision(package, EXPORTED, found)


def _decide_against_source(package: Path, settings: PlanOptions) -> Decision | None:
    """
    Decide what inspecting the source settles, if anything.

    Runs for every book that is going to be written, ``--force`` included: a
    book about to overwrite a good archive still has to be a book worth
    writing.

    :param package: The package directory.
    :param settings: Planning behaviour.

    :return: The decision, or None when the package is usable.
    """
    status = inspect_package(package, check_incomplete=settings.check_incomplete)
    if status.drm:
        return Decision(package, DRM, reason=status.reason)
    if status.incomplete:
        return Decision(package, INCOMPLETE, reason=status.reason)
    return None


def _source_is_newer(package: Path, exported: Path) -> bool:
    """
    Report whether a package looks newer than its exported archive.

    Uses the package directory's own timestamp, which macOS updates when
    entries are added or removed. A book re-downloaded in place with identical
    entry names will not be noticed; use --force for that.

    :param package: The source package directory.
    :param exported: The archive already in the output directory.

    :return: True if the source appears to have changed since the export.
    """
    try:
        return package.stat().st_mtime > exported.stat().st_mtime
    except OSError:  # pragma: no cover - racing removal
        return False


@dataclass(frozen=True)
class _Outcome:
    """How one non-pending status is counted and reported."""

    #: Name of the :class:`~epubconvert.convert.Report` field to increment.
    #: Narrowed to a Literal because the table drives a setattr, which turned a
    #: type-checked ``report.drm += 1`` into a string the checker cannot see:
    #: renaming a Report field would have broken this at runtime with mypy,
    #: ruff and pylint all silent.
    counter: ReportField
    #: Whether the per-book line is a warning rather than information.
    warn: bool
    #: Format string for the per-book line. Every entry uses the same two
    #: named fields, ``name`` and ``reason``, so the caller never has to work
    #: out which arguments a particular message wants.
    line: str
    #: Format string for the closing tally; takes the count.
    tally: str


#: Everything the reporter needs to know about a status, in one place. The
#: alternative -- an if/elif chain over the statuses and a second block of ``if
#: report.x`` tallies below it -- stated the same mapping twice, so a new status
#: had to be added in three places and two of the branches drifted into being
#: byte-identical.
_OUTCOMES: dict[Status, _Outcome] = {
    EXPORTED: _Outcome(
        counter="skipped",
        warn=False,
        line="Already exported, skipping: %(name)s",
        tally="Skipped %d already-exported file(s).",
    ),
    COLLISION: _Outcome(
        counter="collisions",
        warn=True,
        line="Name collision, skipping: %(name)s (%(reason)s)",
        tally="%d package(s) skipped because another book claims the same output name.",
    ),
    DRM: _Outcome(
        counter="drm",
        warn=True,
        line="Skipped, %(reason)s: %(name)s",
        tally="%d package(s) skipped as DRM-protected.",
    ),
    INCOMPLETE: _Outcome(
        counter="incomplete",
        warn=True,
        line="Skipped, %(reason)s: %(name)s",
        tally="%d package(s) skipped as not downloaded.",
    ),
}


def record_decisions(decisions: Sequence[Decision], report: Report) -> None:
    """
    Fold planning decisions into the report and log them.

    Pending decisions are left alone; the exporter counts those as it writes
    them, so counting here too would double them.

    :param decisions: The planner's output.
    :param report: Report to accumulate counts into.
    """
    for decision in decisions:
        if decision.status == PENDING:
            # Counted by the exporter as it writes, so counting here doubles it.
            continue
        # Indexed, not .get(): a sixth status should raise here in the tests
        # rather than disappear silently from the report and the tallies.
        outcome = _OUTCOMES[decision.status]

        setattr(report, outcome.counter, getattr(report, outcome.counter) + 1)
        log = logger.warning if outcome.warn else logger.info
        log(
            outcome.line,
            {"name": printable(decision.package.name), "reason": decision.reason},
        )

    # Counted from the decisions in hand rather than from the report, which
    # export_planned documents as something a caller may accumulate across
    # calls -- so a second call logged the running total as this batch's tally.
    seen = Counter(decision.status for decision in decisions)
    for status, outcome in _OUTCOMES.items():
        if seen[status]:
            log = logger.warning if outcome.warn else logger.info
            log(outcome.tally, seen[status])


def render_listing(decisions: Sequence[Decision], as_json: bool) -> str:
    """
    Render the planner's decisions for human or machine consumption.

    :param decisions: The planner's output.
    :param as_json: Emit JSON rather than a table.

    :return: The text to print.
    """
    if as_json:
        return json.dumps(
            [
                {
                    "name": decision.package.name,
                    # An orphan has no source package; the path in "target" is
                    # where the file actually is.
                    "source": (
                        None if decision.status == ORPHAN else str(decision.package)
                    ),
                    "status": decision.status,
                    "target": str(decision.target) if decision.target else None,
                    "reason": decision.reason,
                }
                for decision in decisions
            ],
            indent=2,
            ensure_ascii=False,
        )

    if not decisions:
        return "No books found."

    width = max(len(decision.status) for decision in decisions)
    lines = [
        f"{decision.status:<{width}}  {printable(decision.package.name)}"
        + (f"  ({decision.reason})" if decision.reason else "")
        for decision in decisions
    ]
    counts = Counter(decision.status for decision in decisions)
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    lines.append("")
    lines.append(summary)
    return "\n".join(lines)
