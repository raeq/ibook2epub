"""
Telling a person what the planner decided.

Split from :mod:`epubconvert.run.planning` when that module reached the line
limit. The planner decides; this module only counts, logs and renders what it
decided, so it depends on the decisions and nothing depends on it but the
callers that report them.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from ..utils.app_logger import logger
from ..utils.display import printable, printable_json
from .planning import (
    COLLISION,
    DRM,
    EXPORTED,
    INCOMPLETE,
    ORPHAN,
    PENDING,
    Decision,
    Status,
)

if TYPE_CHECKING:  # pragma: no cover - import cycle broken for typing only
    from .convert import Report

#: The :class:`~epubconvert.run.convert.Report` fields the outcome table may bump.
ReportField = Literal["skipped", "collisions", "drm", "incomplete"]


@dataclass(frozen=True)
class _Outcome:
    """How one non-pending status is counted and reported."""

    #: Name of the :class:`~epubconvert.run.convert.Report` field to increment.
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
            {
                "name": printable(decision.package.name),
                "reason": printable(str(decision.reason)),
            },
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
        # Titles stay readable; only what printable() would escape is escaped.
        return printable_json(
            json.dumps(
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
        )

    if not decisions:
        return "No books found."

    width = max(len(decision.status) for decision in decisions)
    lines = [
        f"{decision.status:<{width}}  {printable(decision.display_name)}"
        + (f"  ({printable(decision.reason)})" if decision.reason else "")
        for decision in decisions
    ]
    counts = Counter(decision.status for decision in decisions)
    summary = ", ".join(f"{count} {status}" for status, count in sorted(counts.items()))
    lines.append("")
    lines.append(summary)
    return "\n".join(lines)
