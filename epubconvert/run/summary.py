"""
The one line a run ends with.

Kept apart from :mod:`epubconvert.run.convert`, which does the work, because
the summary only reads what the work recorded in its
:class:`~epubconvert.run.convert.Report`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..utils.display import printable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .convert import Report


def format_summary(
    report: Report, output_dir: Path, dry_run: bool, remaining: int | None = None
) -> str:
    """
    Render a one-line human readable summary of a run.

    :param report: The report to render.
    :param output_dir: The directory the run targeted.
    :param dry_run: Whether the run was a dry run.
    :param remaining: Books still to convert after this run, if known.

    :return: The summary line, with the output directory escaped for display:
        a path that is not UTF-8 raised UnicodeEncodeError under a strict
        stdout after the books were written.
    """
    shelf = printable(str(output_dir))
    if dry_run:
        summary = (
            f"Dry run: would export {report.planned} epub file(s) to "
            f"{shelf} (skipped {report.skipped} already present"
            f"{_clauses(report, failures=False)})."
        )
    else:
        summary = (
            f"Exported {report.exported} epub file(s) "
            f"({report.files_written} member files) to {shelf}"
        )
        if report.skipped:
            summary += f", skipped {report.skipped}"
        summary += _clauses(report, failures=True) + "."
    # A dry run too: stopped, its summary read as a finished rehearsal.
    if report.interrupted:
        summary = f"Interrupted. {summary}"
    if report.aborted:
        summary = f"Aborted: not enough free space on {shelf}. {summary}"
    if remaining:
        # The same advice from a dry run as from a real one. A bare count
        # left out that the cap was what held these back.
        summary += _remaining_hint(report, remaining)
    return summary


def _remaining_hint(report: Report, remaining: int) -> str:
    """
    Say what is left, and why, since each reason wants different advice.

    ``remaining`` counts every pending book this run did not export. Advising a
    rerun and ``-m 0`` for all of them was wrong twice over when the only book
    left had failed under ``-m 0``: the flag was already given, and a rerun
    fails the same book again (#15). The count itself is unchanged; only the
    advice is split by cause.

    The held-back count is taken first because it is exact: the cap counts it
    where it is applied. Failed copies are not among the books remaining, so
    they are counted apart in ``report.copies_failed``: counted in
    ``report.failed`` they turned a book the cap held back, and then a book
    the ``--min-free`` floor stopped, into one that had failed.

    :param report: The run's report.
    :param remaining: Pending books this run did not export.

    :return: The sentences to append, each prefixed with a space.
    """
    held = min(report.held_back, remaining)
    failed = min(report.failed, remaining - held)
    unattempted = remaining - failed - held
    parts = [f" {remaining} remaining."]
    if held:
        parts.append(
            f" {held} held back by --max-export-files: rerun to continue, "
            "or pass -m 0 to convert everything."
        )
    if unattempted:
        parts.append(f" {unattempted} not attempted: rerun to continue.")
    if failed:
        parts.append(f" {failed} failed: see the errors above for why.")
    return "".join(parts)


def _clauses(report: Report, *, failures: bool) -> str:
    """
    Render the optional counts a summary mentions only when they are non-zero.

    Stated once rather than repeated per branch, which is what pushed
    :func:`format_summary` past the branch limit and would have grown with
    every new counter.

    :param report: The report to read.
    :param failures: Whether to include the failure count, which a dry run has
        no meaning for.

    :return: The clauses, each already prefixed with ", ".
    """
    parts = [
        (report.collisions, "{} name collision(s)"),
        (report.drm, "{} DRM-protected"),
        (report.incomplete, "{} not downloaded"),
        # A dry run counts what it would copy there; "copied" said it had.
        (report.copied, "{} copied" if failures else "{} to copy"),
        (report.ignored, "{} ignored"),
        (report.orphaned, "{} orphaned"),
    ]
    if failures:
        # One figure for both: a PDF that did not reach the shelf is as much
        # a failure as a book that did not convert, and the exit code says so.
        parts.append((report.failed + report.copies_failed, "failed {}"))
    return "".join(f", {phrase.format(count)}" for count, phrase in parts if count)
