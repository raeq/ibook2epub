"""
What a run hands on once its books are written.

The names the annotation step works from -- the books this run selected, as
the plan left them -- that step itself, and the one exit code a run that
converted and then annotated ends with. Split from
:mod:`epubconvert.run.run` when that module neared the line limit.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..utils import exits
from ..utils.app_logger import logger
from ..utils.policy import Assignment, NamingPolicy
from .annotating import annotations_after_export
from .convert import Report, exit_code
from .planning import COLLISION, PENDING, Decision


def pending_packages(decisions: Sequence[Decision]) -> frozenset[Path]:
    """Return the packages of the decisions that still have an export ahead."""
    return frozenset(
        decision.package for decision in decisions if decision.status == PENDING
    )


def selected_names(
    assigned: Sequence[Assignment],
    packages: Sequence[Path],
    decisions: Sequence[Decision],
    policy: NamingPolicy,
) -> list[Assignment]:
    """
    Keep the assignments of this run's own books, as the plan left them.

    The assignment names the whole library, and ``--match`` narrows what the
    run touches: only the books it selected go on to the annotation step.

    A book the plan found to be a collision has no name there either. Under a
    policy that names from the folder the plan reads a book's identifier only
    before it writes, so ``--force`` and ``--refresh`` could call a book a
    collision whose name the annotation step then trusted: its highlights
    reached no file, and the warning, finding a file of that name, said
    nothing. A book the plan placed at another file, such as its marked name,
    is renamed to it; see :func:`_as_decided`.

    :param assigned: The assignment of every package in the library.
    :param packages: The packages this run selected.
    :param decisions: What the plan decided about them, where it got that far.
    :param policy: The naming policy the names came from.

    :return: Their assignments, in the library's order.
    """
    chosen = set(packages)
    decided = {decision.package: decision for decision in decisions}
    return [
        _as_decided(entry, decided.get(entry.package), policy)
        for entry in assigned
        if entry.package in chosen
    ]


def _as_decided(
    entry: Assignment, decision: Decision | None, policy: NamingPolicy
) -> Assignment:
    """
    Rename one book's assignment to the file the plan placed it at.

    :param entry: Its assignment.
    :param decision: What the plan decided about it, if it got that far.
    :param policy: The naming policy the names came from.

    :return: The assignment with no name for a collision, or the name of the
        file the plan exports it to or found it at. A vault note shares that
        file's stem: named from the assignment, an edition that moved on to
        its marked name wrote its highlights into the other edition's note.
    """
    if decision is None:
        return entry
    if decision.status == COLLISION:
        return replace(entry, filename="", reason=decision.reason)
    if decision.target is not None and decision.target.name != entry.filename:
        name = decision.target.name
        return replace(entry, filename=name, identity=policy.identity(name))
    return entry


def after_export(
    args: argparse.Namespace,
    policy: NamingPolicy,
    report: Report,
    *,
    named: Sequence[Assignment],
    found: list[dict[str, Any]] | None,
    copyable: Sequence[Path],
    held_back: frozenset[Path] = frozenset(),
) -> int | None:
    """
    Do the annotation work the export leaves over, unless the run was stopped.

    Ctrl-C asks the run to stop, and this went on regardless: it wrote a vault
    of notes after the reader had asked for nothing more to be written, and
    warned that highlights "reached no file" for books that were never
    attempted, which says a book cannot be converted when it was only not
    reached.

    :param args: Parsed command line arguments.
    :param policy: The naming policy the names came from.
    :param report: The export's report, which says whether it was stopped.
    :param named: The names the export used.
    :param found: The annotations this run read, or None.
    :param copyable: The library's already-zipped books and PDFs.
    :param held_back: The books ``-m`` held back for a later run.

    :return: What :func:`~epubconvert.run.annotating.annotations_after_export`
        returns, or None when the run was stopped, before this or during it.
        Stopped during it, *report* is marked interrupted, so the summary
        still says what the run finished and the run still exits 130.
    """
    if not report.interrupted:
        try:
            return annotations_after_export(
                args, policy, named, found, copyable=copyable, held_back=held_back
            )
        except KeyboardInterrupt:
            # A Ctrl-C while the detached file or the vault was written
            # escaped to main's last resort, which prints no summary: the
            # books this run had finished went unreported, on stdout and in
            # the log file. Each write is atomic, so nothing is half-written.
            report.interrupted = True
    # Said only when there was somewhere else for them to go. Under -ae alone
    # every book converted before the Ctrl-C already carries its own.
    elsewhere = args.annotations_detached or args.annotations_refresh
    if found is not None and elsewhere and not args.dry_run:
        logger.warning(
            "Your highlights were not written: the run was interrupted. "
            "Rerun to write them."
        )
    return None


def outcome(report: Report, annotated: int | None) -> int:
    """
    Choose the one exit code for a run that converted and then annotated.

    The first of these that applies: 130 if the run was stopped with Ctrl-C;
    1 if a book failed or the run could not proceed; the annotation step's
    own code, such as 5 for a destination it could not write; otherwise 0.
    The README's exit-code section states the same order.

    The annotation code used to win outright, so a run stopped with Ctrl-C,
    or one whose book had failed, exited 5 under a summary that said
    "Interrupted" or "failed 1". The summary describes the books, and the
    books are what the run is for, so their outcome comes first; the
    annotation step has already logged its own reason on stderr.

    :param report: The export's report.
    :param annotated: The annotation step's exit code, or None when it had
        nothing to report.

    :return: A process exit code.
    """
    converted = exit_code(report)
    if converted != exits.SUCCESS or annotated is None:
        return converted
    return annotated
