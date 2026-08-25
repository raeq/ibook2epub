"""
Exporting a batch of packages: concurrency, bookkeeping and the output lock.

:mod:`epubconvert.archive` writes one book and :mod:`epubconvert.planning`
decides which books to write. This module runs the writes -- a thread pool, a
shared :class:`Report` the workers update under a lock, an advisory lock over
the output directory, and the arithmetic that turns the result into a summary
line and an exit code.

Recording happens in the worker thread that did the work, not in the awaiting
coroutine, so a Ctrl-C cannot leave the summary disagreeing with the directory.
"""

from __future__ import annotations

import asyncio
import errno
import fnmatch
import os
import socket
import threading
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from random import shuffle
from typing import TextIO

from .app_logger import logger
from .archive import PARTIAL_PREFIX, PARTIAL_SUFFIX, zip_package
from .display import printable
from .inspect_output import extract_cover, free_megabytes
from .naming import NamingPolicy, PassthroughNaming
from .planning import (
    PENDING,
    Decision,
    PlanOptions,
    plan_exports,
    record_decisions,
)
from .validate import ValidationOptions

try:
    import fcntl

    HAVE_FLOCK = True
except ImportError:  # pragma: no cover - Windows has no fcntl
    HAVE_FLOCK = False

LOCK_NAME = ".ibook2epub.lock"

#: The errnos that mean another process holds the lock. Anything else means
#: the filesystem does not do advisory locking at all.
_CONTENDED = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})


@dataclass(frozen=True)
class ExportOptions:
    """Everything about a run that is not the packages or the destination."""

    covers: bool = False
    min_free_mb: int = 0
    validation: ValidationOptions | None = None
    plan: PlanOptions | None = None


class OutputLockedError(RuntimeError):
    """
    Raised when the output directory cannot be locked for this run.

    Two causes: another run already holds the lock, or the lock file itself
    could not be opened -- a read-only output directory gets past ``main``'s
    ``mkdir(exist_ok=True)`` and fails here.
    """


@dataclass
class Report:
    """Outcome of an export run."""

    exported: int = 0
    files_written: int = 0
    skipped: int = 0
    failed: int = 0
    planned: int = 0  # Dry-run only: exports that would have been attempted.
    collisions: int = 0  # Distinct packages that share one output name.
    drm: int = 0  # Packages skipped as DRM-protected.
    incomplete: int = 0  # Packages skipped as not downloaded from iCloud.
    interrupted: bool = False  # The run was stopped with Ctrl-C.
    aborted: bool = False  # The run could not proceed, e.g. no disk space.


#: Guards the shared Report and progress counter, which worker threads update.
_REPORT_LOCK = threading.Lock()


class _Progress:  # pylint: disable=too-few-public-methods
    """Counts finished books so each log line shows how far along the run is."""

    #: Upper bound on how often the output volume is re-measured, in books.
    #: The floor is a safety margin rather than an accounting, and --min-free's
    #: own help names SD cards and Kindles, where statvfs is slowest.
    ROOM_INTERVAL = 32

    def __init__(self, total: int, interval: int = ROOM_INTERVAL) -> None:
        self.total = total
        self.done = 0
        self.checks = 0
        # Never wider than the pool. Sampling one book in 32 while 64 run at
        # once means up to 31 further books are written after the floor has
        # already been crossed.
        self.interval = max(1, min(interval, self.ROOM_INTERVAL))

    def should_check_room(self) -> bool:
        """
        Report whether this book should re-measure the output volume.

        Counts the checks rather than the completions: workers start together
        and would all read the same completion count, so every one of them
        sampled. The caller holds the report lock, which makes this atomic.
        """
        self.checks += 1
        return (self.checks - 1) % self.interval == 0

    def tick(self) -> str:
        """Advance the counter and render it as ``[12/240]``."""
        self.done += 1
        return f"[{self.done}/{self.total}]"


def _zip_and_record(
    package: Path,
    target: Path,
    report: Report,
    progress: _Progress,
    run: ExportOptions,
) -> None:
    """
    Export one package and record the outcome, all in the worker thread.

    The bookkeeping deliberately happens here rather than in the awaiting
    coroutine. On Ctrl-C the event loop is torn down and those coroutines
    never resume, so counting there would report a book as not exported while
    its file was already atomically in place. Recording in the same thread
    that did the work keeps the summary consistent with the directory.

    :param package: The package directory to compress.
    :param target: The epub file to create.
    :param report: Report to record the outcome in.
    :param progress: Shared counter for the ``[n/total]`` prefix.
    :param run: Options for this run.
    """
    with _REPORT_LOCK:
        measure = progress.should_check_room()
    if measure and not _has_room(target.parent, run.min_free_mb):
        with _REPORT_LOCK:
            report.failed += 1
            marker = progress.tick()
        logger.error(
            "%s Skipped %s: not enough free space", marker, printable(package.name)
        )
        return

    try:
        file_count = zip_package(package, target, run.validation)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
        with _REPORT_LOCK:
            report.failed += 1
            marker = progress.tick()
        logger.error("%s Failed to export %s: %s", marker, printable(package.name), exc)
        return

    if run.covers:
        # Inside the guard, and catching broadly on purpose. A cover is a
        # convenience; the book is already complete and atomically in place by
        # now. Letting anything escape here loses the bookkeeping for a book
        # that is on disk, and takes the whole run and its summary with it.
        try:
            extract_cover(package, target)
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-except
            logger.warning("No cover for %s: %s", printable(target.name), exc)

    with _REPORT_LOCK:
        report.exported += 1
        report.files_written += file_count
        marker = progress.tick()
    logger.info("%s Exported %s (%d files)", marker, printable(target.name), file_count)


async def _export_one(
    pool: ThreadPoolExecutor,
    package: Path,
    target: Path,
    *,
    report: Report,
    progress: _Progress,
    run: ExportOptions,
) -> None:
    """
    Hand one package to a worker thread.

    :param pool: The executor running the compression work.
    :param package: The package directory to compress.
    :param target: The epub file to create.
    :param report: Report to record the outcome in.
    :param progress: Shared counter for the ``[n/total]`` prefix.
    :param run: Options for this run.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        pool, _zip_and_record, package, target, report, progress, run
    )


async def export_packages(
    packages: Sequence[Path],
    output_dir: Path,
    *,
    dry_run: bool = False,
    max_workers: int | None = None,
    policy: NamingPolicy | None = None,
    report: Report | None = None,
    options: ExportOptions | None = None,
) -> Report:
    """
    Plan a batch of packages and export them concurrently.

    Convenience wrapper for callers holding packages rather than a plan.
    :func:`epubconvert.run.main` does not use it: it plans once itself, under
    and calls :func:`export_planned`.

    Whether a book has already been exported is decided by its *identity*
    under the naming policy, not by an exact filename match. Identities of
    completed work are recomputed by reading the output directory, so that
    directory remains the sole record of what has been converted and no state
    file is needed.

    :param packages: Package directories to export.
    :param output_dir: Directory to write the epub files into.
    :param dry_run: When True, report what would happen without writing.
    :param max_workers: Size of the compression thread pool.
    :param policy: Naming policy; defaults to :class:`PassthroughNaming`.
    :param report: Report to accumulate into. Pass one to retain the partial
        counts if the run is interrupted.
    :param options: Everything else about the run.

    :return: A report of what was exported, skipped and failed.
    """
    resolved = policy if policy is not None else PassthroughNaming()
    run = options if options is not None else ExportOptions()
    decisions = plan_exports(packages, output_dir, resolved, run.plan)
    return await export_planned(
        decisions,
        output_dir,
        dry_run=dry_run,
        max_workers=max_workers,
        report=report,
        options=run,
    )


async def export_planned(
    decisions: Sequence[Decision],
    output_dir: Path,
    *,
    dry_run: bool = False,
    max_workers: int | None = None,
    report: Report | None = None,
    options: ExportOptions | None = None,
) -> Report:
    """
    Act on decisions the planner has already made.

    Kept separate from :func:`export_packages` so a caller that needs the plan
    for something else — the count of work remaining, or the export cap — can
    plan once and hand the result straight here. Planning twice let the two
    passes disagree about the same library.

    :param decisions: The planner's output. Only the pending ones are written;
        the rest are folded into the report and logged.
    :param output_dir: Directory to write the epub files into.
    :param dry_run: When True, report what would happen without writing.
    :param max_workers: Size of the compression thread pool.
    :param report: Report to accumulate into. Pass one to retain the partial
        counts if the run is interrupted.
    :param options: Everything else about the run.

    :return: A report of what was exported, skipped and failed.
    """
    report = report if report is not None else Report()
    run = options if options is not None else ExportOptions()

    record_decisions(decisions, report)

    pending = [
        (decision.package, decision.target)
        for decision in decisions
        if decision.status == PENDING and decision.target is not None
    ]

    if dry_run:
        for package, target in pending:
            logger.info(
                "Would export: %s -> %s",
                printable(str(package)),
                printable(str(target)),
            )
        report.planned += len(pending)
        return report

    if not pending:
        return report

    if not _has_room(output_dir, run.min_free_mb):
        # Nothing was attempted, so nothing failed. Reporting these as
        # failures would claim work that never started.
        logger.warning("Nothing exported: %d book(s) left unattempted.", len(pending))
        report.aborted = True
        return report

    progress = _Progress(len(pending), default_workers(max_workers))
    pool = ThreadPoolExecutor(
        max_workers=default_workers(max_workers), thread_name_prefix="zip"
    )
    try:
        # The worker catches Exception, but not every call site inside it is
        # covered. A surprise should cost one book, not the run -- and it must
        # still be counted, or the summary reports a clean run with a book
        # missing and exit_code returns 0.
        outcomes = await asyncio.gather(
            *(
                _export_one(
                    pool, package, target, report=report, progress=progress, run=run
                )
                for package, target in pending
            ),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                with _REPORT_LOCK:
                    report.failed += 1
                logger.error("Export failed unexpectedly: %r", outcome)
    finally:
        # cancel_futures drops books that have not started, so an interrupt
        # does not wait for the whole queued backlog. wait=True still joins
        # the handful already being written: they finish, replace atomically,
        # and record themselves, which keeps the summary honest about what is
        # on disk.
        pool.shutdown(wait=True, cancel_futures=True)

    return report


def filter_packages(packages: Sequence[Path], pattern: str | None) -> list[Path]:
    """
    Narrow the package list to those matching a user pattern.

    A pattern with no glob metacharacter matches anywhere in the name, so
    ``--match hobbit`` finds ``The Hobbit.epub``. Anything else is treated as
    a glob against the whole name. Matching is case-insensitive.

    :param packages: Discovered package directories.
    :param pattern: The user's pattern, or None to keep everything.

    :return: The packages that matched.
    """
    if pattern is None:
        return list(packages)

    needle = pattern.lower()
    if not any(char in needle for char in "*?["):
        needle = f"*{needle}*"

    matched = [p for p in packages if fnmatch.fnmatch(p.name.lower(), needle)]
    logger.info(
        "Matched %d of %d package(s) against %r", len(matched), len(packages), pattern
    )
    return matched


def count_pending_decisions(decisions: Sequence[Decision]) -> int:
    """
    Count the decisions that still have an export ahead of them.

    :param decisions: The planner's output.

    :return: The number of books still to export.
    """
    return sum(1 for decision in decisions if decision.status == PENDING)


@contextmanager
def output_lock(output_dir: Path) -> Iterator[bool]:
    """
    Hold an advisory lock on the output directory for the duration of a run.

    Rerun safety invites scheduling this from cron or launchd, where two runs
    can overlap. Both would read the output directory before either wrote to
    it and export the same books; ``os.replace`` keeps that from corrupting
    anything, but it wastes the work and confuses the logs.

    On platforms without ``fcntl`` (Windows) this is a no-op.

    :param output_dir: Directory to lock.

    :return: True when the lock was really taken. False means the filesystem
        does not do advisory locking, and the caller must not do anything that
        assumes exclusivity -- sweeping temporaries, above all, since a
        concurrent run's in-flight file is indistinguishable from an abandoned
        one.

    :raises OutputLockedError: If another run already holds the lock.
    """
    if not HAVE_FLOCK:  # pragma: no cover - exercised only on Windows
        yield False
        return

    path = output_dir / LOCK_NAME
    # Opened "r+" where possible so a failed lock can still read the holder's
    # details; "w" would truncate them before we got to report them.
    try:
        handle = path.open("r+") if path.exists() else path.open("w")
    except OSError as exc:
        # A read-only output directory got past main's mkdir(exist_ok=True) and
        # died here with a raw traceback. main already turns this into a clean
        # exit 3.
        raise OutputLockedError(f"cannot lock {output_dir}: {exc}") from exc
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            # Only these errnos mean somebody else holds it. A share without
            # advisory locking answers ENOTSUP/ENOLCK, which was reported as
            # "another run is already using ..." -- quoting a pid from a run
            # that had long since exited, because the lock file is not
            # truncated on release.
            if exc.errno not in _CONTENDED:
                logger.warning(
                    "Locking is not supported on %s (%s); continuing unlocked.",
                    output_dir,
                    exc,
                )
                handle.close()
                yield False
                return
            raise OutputLockedError(
                f"another ibook2epub run is already using {output_dir} "
                f"({_read_lock_holder(handle)})"
            ) from exc

        # The PID is recorded for diagnostics only. It is deliberately NOT
        # used to detect or remove an orphaned lock: flock is released by the
        # kernel when the holder dies, even on SIGKILL, so a leftover lock
        # file is inert. Checking liveness by PID would add a PID-reuse race
        # to solve a problem that does not exist.
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} host={socket.gethostname()}\n")
        handle.flush()
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _read_lock_holder(handle: TextIO) -> str:
    """
    Describe whoever currently holds the lock, for the error message.

    :param handle: The open lock file.

    :return: A short description, or a fallback when nothing is readable.
    """
    try:
        handle.seek(0)
        details = handle.read().strip()
    except OSError:  # pragma: no cover - unreadable lock file
        return "holder unknown"
    return details or "holder unknown"


def cap_exports(
    decisions: Sequence[Decision], max_export_files: int, randomise: bool = True
) -> list[Decision]:
    """
    Apply the export cap to the books that still need writing.

    ``--max-export-files`` is documented as a cap on files exported, so it is
    applied to the pending decisions rather than to the whole library. Capping
    the library instead let the cap land on books already exported: ``-m 3
    --no-shuffle`` on a shelf whose first three books were done exported
    nothing, reported work remaining, and did exactly the same on every rerun.
    Shuffling hid the stall behind random progress rather than fixing it.

    Decisions that are not pending are all kept, so the summary counts the
    whole library's skipped, DRM-protected and undownloaded books rather than
    whatever slice the cap happened to admit.

    :param decisions: The planner's output.
    :param max_export_files: The cap, where 0 means no limit.
    :param randomise: Choose the capped subset at random.

    :return: The decisions to act on this run.
    """
    pending = [
        position
        for position, decision in enumerate(decisions)
        if decision.status == PENDING
    ]

    if not max_export_files or len(pending) <= max_export_files:
        logger.info("All %d pending epub package(s) will be processed.", len(pending))
        return list(decisions)

    chosen = list(pending)
    if randomise:
        shuffle(chosen)
    keep = set(chosen[:max_export_files])
    logger.info(
        "Limiting activity to %d of %d pending epub package(s).",
        max_export_files,
        len(pending),
    )
    return [
        decision
        for position, decision in enumerate(decisions)
        if decision.status != PENDING or position in keep
    ]


def sweep_partials(output_dir: Path) -> int:
    """
    Remove temporary archives left by a run that was killed outright.

    :func:`zip_package` unlinks its own temporary on any ordinary failure,
    Ctrl-C included. A SIGKILL or a power loss gives it no chance, and every
    glob in this tool looks for ``*.epub``, so the leftovers are invisible to
    everything afterwards and accumulate on the very volume ``--min-free``
    exists to protect.

    The glob is anchored on :data:`~epubconvert.archive.PARTIAL_PREFIX` as
    well as the suffix. A bare ``*.part`` also matches a browser's in-progress
    download or a user's own file, and the default output directory is
    ``~/Books`` -- so the sweep deleted real user data.

    Only safe to call while holding the output lock: a concurrent run's
    temporary is indistinguishable from an abandoned one.

    :param output_dir: Directory to sweep.

    :return: The number of files removed.
    """
    removed = 0
    for stale in output_dir.glob(f"{PARTIAL_PREFIX}*{PARTIAL_SUFFIX}"):
        try:
            stale.unlink()
        except OSError as exc:  # pragma: no cover - racing removal
            logger.debug("Could not remove %s: %s", printable(stale.name), exc)
            continue
        removed += 1

    if removed:
        logger.info("Removed %d abandoned temporary file(s).", removed)
    return removed


def format_summary(
    report: Report, output_dir: Path, dry_run: bool, remaining: int | None = None
) -> str:
    """
    Render a one-line human readable summary of a run.

    :param report: The report to render.
    :param output_dir: The directory the run targeted.
    :param dry_run: Whether the run was a dry run.
    :param remaining: Books still to convert after this run, if known.

    :return: The summary line.
    """
    if dry_run:
        summary = (
            f"Dry run: would export {report.planned} epub file(s) to "
            f"{output_dir} (skipped {report.skipped} already present"
        )
        summary += _clauses(report, failures=False)
        summary += ")."
        if remaining:
            summary += f" {remaining} remaining."
        return summary

    summary = (
        f"Exported {report.exported} epub file(s) "
        f"({report.files_written} member files) to {output_dir}"
    )
    if report.skipped:
        summary += f", skipped {report.skipped}"
    summary += _clauses(report, failures=True)
    summary += "."
    if report.interrupted:
        summary = f"Interrupted. {summary}"
    if report.aborted:
        summary = f"Aborted: not enough free space on {output_dir}. {summary}"
    if remaining:
        summary += f" {remaining} remaining; rerun to continue."
    return summary


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
    ]
    if failures:
        parts.append((report.failed, "failed {}"))
    return "".join(f", {phrase.format(count)}" for count, phrase in parts if count)


def progress_for(total: int, interval: int) -> _Progress:
    """
    Build a progress counter, for callers that need to inspect its cadence.

    :param total: Books to be written.
    :param interval: Pool size; the sampling cadence is clamped to it.

    :return: The counter.
    """
    return _Progress(total, interval)


def default_workers(max_workers: int | None = None) -> int:
    """
    Choose the size of the compression pool.

    ``ThreadPoolExecutor``'s own default is ``min(32, cpu + 4)``, which is
    tuned for CPU-bound work. This work is not: the README records 50 books
    taking 80 seconds at 8% CPU, almost all of it blocked on iCloud
    materialising files. Measured under that stall model, 14 workers took
    19.2 s, 48 took 7.12 s and 64 took 4.76 s; on a purely local library
    raising the count cost 6%. Deep queues cost only thread stacks when every
    worker spends its time blocked.

    :param max_workers: An explicit count from ``-w``, which always wins.

    :return: The number of worker threads to use.
    """
    if max_workers is not None:
        return max_workers
    return min(64, 4 * (os.cpu_count() or 4))


def _has_room(output_dir: Path, min_free_mb: int) -> bool:
    """
    Report whether the output volume has enough space to keep writing.

    :param output_dir: Directory being written to.
    :param min_free_mb: Floor in MiB; 0 disables the check.

    :return: True when it is safe to continue.
    """
    if not min_free_mb:
        return True
    free = free_megabytes(output_dir)
    if free < min_free_mb:
        logger.critical(
            "Only %d MiB free on %s, below the --min-free floor of %d MiB.",
            free,
            output_dir,
            min_free_mb,
        )
        return False
    logger.debug("%d MiB free on %s", free, output_dir)
    return True


def exit_code(report: Report) -> int:
    """
    Translate a run's outcome into a process exit code.

    :param report: The run's report.

    :return: 130 if interrupted, 1 if anything failed or the run could not
        proceed at all, otherwise 0.
    """
    if report.interrupted:
        return 130  # Conventional exit code for termination by SIGINT.
    return 1 if report.failed or report.aborted else 0
