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
from typing import TYPE_CHECKING

from .app_logger import logger
from .naming import NamingPolicy, truncate_bytes
from .source import inspect_package
from .spec import PACKAGE_SUFFIX

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for typing only
    from .convert import Report


@dataclass(frozen=True)
class PlanOptions:
    """How the planner should treat the packages it is given."""

    force: bool = False
    refresh: bool = False
    check_incomplete: bool = False
    on_collision: str = "skip"


@dataclass
class Decision:
    """What the planner decided about one package."""

    package: Path
    status: str
    target: Path | None = None
    reason: str | None = None


#: Decision statuses.
PENDING = "pending"
ALREADY = "exported"
COLLISION = "collision"
DRM = "drm"
INCOMPLETE = "incomplete"

SKIP = "skip"
SUFFIX = "suffix"
COLLISION_MODES = (SKIP, SUFFIX)

MAX_SUFFIX = 99


def _suffixed(filename: str, position: int, max_bytes: int) -> str:
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

    base = Path(filename)
    marker = f" ({position})"
    candidate = f"{base.stem}{marker}{base.suffix}"
    if not max_bytes or len(candidate.encode("utf-8")) <= max_bytes:
        return candidate

    budget = max_bytes - len(marker.encode("utf-8"))
    budget -= len(base.suffix.encode("utf-8"))
    stem = truncate_bytes(base.stem, max(budget, 1)).rstrip(" .") or "_"
    return f"{stem}{marker}{base.suffix}"


def _assign_names(
    packages: Sequence[Path], policy: NamingPolicy, on_collision: str
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

    :param packages: Packages to name.
    :param policy: Naming policy supplying filenames and identities.
    :param on_collision: ``skip`` or ``suffix``.

    :return: Triples of package, filename and identity. A filename of ``""``
        marks a package that lost a collision.
    """
    assigned: list[tuple[Path, str, str]] = []
    claimed: set[str] = set()

    for package in sorted(packages):
        wanted = policy.filename(package.name)
        limit = MAX_SUFFIX if on_collision == SUFFIX else 1

        for position in range(1, limit + 1):
            candidate = _suffixed(wanted, position, getattr(policy, "max_bytes", 0))
            key = policy.identity(candidate)
            if key not in claimed:
                claimed.add(key)
                assigned.append((package, candidate, key))
                break
        else:
            assigned.append((package, "", policy.identity(wanted)))

    return assigned


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

    # Missing directories glob to nothing, which is what a dry run wants.
    existing = {
        policy.identity(found.name): found
        for found in output_dir.glob(f"*{PACKAGE_SUFFIX}")
    }
    named = {
        package: (filename, key)
        for package, filename, key in _assign_names(
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
    existing: dict[str, Path],
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

    found = existing.get(key)
    refreshing = False

    # Settled before the package is inspected: a book that is already
    # exported needs no source walk, and --skip-incomplete would otherwise
    # pay for one on every book of the library on every rerun.
    if found is not None and not settings.force:
        if not (settings.refresh and _source_is_newer(package, found)):
            return Decision(package, ALREADY, found)
        refreshing = True

    status = inspect_package(package, check_incomplete=settings.check_incomplete)
    if status.drm:
        return Decision(package, DRM, reason=status.reason)
    if status.incomplete:
        return Decision(package, INCOMPLETE, reason=status.reason)

    if refreshing and found is not None:
        # Write over the archive judged stale, not over a freshly computed
        # name. Under a policy whose identity is looser than its filename the
        # two differ, and targeting the new name would leave the stale file in
        # place, still satisfying the identity check forever.
        return Decision(package, PENDING, found, reason="source is newer")

    return Decision(package, PENDING, output_dir / filename)


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


def record_decisions(decisions: Sequence[Decision], report: Report) -> None:
    """
    Fold planning decisions into the report and log them.

    :param decisions: The planner's output.
    :param report: Report to accumulate counts into.
    """
    for decision in decisions:
        if decision.status == ALREADY:
            report.skipped += 1
            logger.info("Already exported, skipping: %s", decision.package.name)
        elif decision.status == COLLISION:
            report.collisions += 1
            logger.warning(
                "Name collision, skipping: %s (%s)",
                decision.package.name,
                decision.reason,
            )
        elif decision.status == DRM:
            report.drm += 1
            logger.warning("Skipped, %s: %s", decision.reason, decision.package.name)
        elif decision.status == INCOMPLETE:
            report.incomplete += 1
            logger.warning("Skipped, %s: %s", decision.reason, decision.package.name)

    if report.skipped:
        logger.info("Skipped %d already-exported file(s).", report.skipped)
    if report.collisions:
        logger.warning(
            "%d package(s) skipped because another book claims the same output name.",
            report.collisions,
        )
    if report.drm:
        logger.warning("%d package(s) skipped as DRM-protected.", report.drm)
    if report.incomplete:
        logger.warning("%d package(s) skipped as not downloaded.", report.incomplete)


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
                    "source": str(decision.package),
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
        f"{decision.status:<{width}}  {decision.package.name}"
        + (f"  ({decision.reason})" if decision.reason else "")
        for decision in decisions
    ]
    counts = Counter(decision.status for decision in decisions)
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    lines.append("")
    lines.append(summary)
    return "\n".join(lines)
