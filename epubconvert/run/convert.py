"""
Exporting a batch of packages: concurrency, bookkeeping and the output lock.

:mod:`epubconvert.export.archive` writes one book and :mod:`epubconvert.run.planning`
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
import stat
import threading
import time
import unicodedata
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from random import shuffle
from typing import BinaryIO

from ..collect.annotations import for_book as annotations_for_book
from ..collect.validate import ValidationOptions
from ..export.archive import PARTIAL_PREFIX, PARTIAL_SUFFIX, zip_package
from ..export.inspect_output import extract_cover, free_megabytes
from ..export.naming import PassthroughNaming, encode_name
from ..utils import exits
from ..utils.app_logger import logger
from ..utils.display import printable
from ..utils.policy import NamingPolicy
from .planning import (
    PENDING,
    Decision,
    PlanOptions,
    plan_exports,
    record_decisions,
)

try:
    import fcntl

    HAVE_FLOCK = True
except ImportError:  # pragma: no cover - Windows has no fcntl
    HAVE_FLOCK = False

LOCK_NAME = ".ibook2epub.lock"

#: How long a temporary must have gone unmodified before a sweep takes it for
#: abandoned. Holding the lock does not make this run the only writer: a run
#: that was refused locking with an errno outside _CONTENDED carries on
#: unlocked, and on NFS, where flock is emulated with fcntl locks, fcntl
#: answers ENOLCK when "a remote locking protocol failed" -- transiently, so a
#: later run can take the lock while that one is mid-write. Sweeping everything
#: deleted its temporary and failed its book with FileNotFoundError; a TLA+
#: model found the interleaving (formal/OutputProtocol.tla).
#:
#: A live run touches its temporary continuously while it writes. The longest
#: it leaves one alone is between closing the archive and renaming it, while
#: the archive is checked: 0.35 s under --validate for a 400 MB book, measured
#: on Linux 6.18, and at most the 120 s timeout run_epubcheck gives
#: --epubcheck. An hour is thirty times that, with room for clock skew between
#: NFS clients, and still clears what a killed run left behind.
STALE_PARTIAL_SECONDS = 60 * 60

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
    #: Annotations to store inside each book, keyed the way
    #: :func:`~epubconvert.collect.annotations.for_book` looks them up. Embedding
    #: during the one write the conversion already does; rebuilding the
    #: finished archive afterwards serialised every book twice.
    annotations: dict[str, list[dict[str, object]]] | None = None


class OutputLockedError(RuntimeError):
    """
    Raised when the output directory cannot be locked for this run.

    Two causes: another run already holds the lock, or the lock file itself
    could not be opened -- a read-only output directory gets past ``main``'s
    ``mkdir(exist_ok=True)`` and fails here.

    Which one is carried as :attr:`contended` rather than left in the prose.
    ``main`` used to look for "already using" in the message, which quotes the
    output path, so a directory named for the phrase turned an unopenable lock
    file into a held lock and "fix the path" into "retry later".
    """

    def __init__(self, message: str, *, contended: bool) -> None:
        super().__init__(message)
        #: True when another run holds the lock; False when this one could not
        #: open the lock file at all.
        self.contended = contended

    @property
    def exit_code(self) -> int:
        """
        What a run ends with when this is why it could not proceed.

        Carried by the error, as
        :class:`~epubconvert.collect.coredata.ContainerUnavailableError`
        carries its own, so every route that takes the lock maps it the same
        way.
        """
        return exits.LOCKED if self.contended else exits.NO_OUTPUT


@dataclass
class Report:
    """Outcome of an export run."""

    exported: int = 0
    files_written: int = 0
    skipped: int = 0
    failed: int = 0  # Books whose conversion failed; failed copies are apart.
    planned: int = 0  # Dry-run only: exports that would have been attempted.
    collisions: int = 0  # Distinct packages that share one output name.
    drm: int = 0  # Packages skipped as DRM-protected.
    incomplete: int = 0  # Books skipped as not downloaded from iCloud.
    interrupted: bool = False  # The run was stopped with Ctrl-C.
    aborted: bool = False  # The run could not proceed, e.g. no disk space.
    ignored: int = 0  # Things in the source that were not *.epub/ packages.
    orphaned: int = 0  # Archives on the shelf no book in the library claims.
    copied: int = 0  # Files taken along unchanged rather than converted.
    #: Files whose copy failed. Kept out of ``failed`` because the summary's
    #: advice about what remains is about books to convert, and a failed copy
    #: counted there turned a book the floor stopped into one that "failed".
    copies_failed: int = 0
    held_back: int = 0  # Pending books the export cap left for a later run.
    #: Packages the --min-free floor kept from starting. Not attempted, like
    #: the books the cap held back: their highlights wait for a rerun, and
    #: were said to have reached no file, with the DRM advice.
    stopped: set[Path] = field(default_factory=set)


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
        # once let up to 31 further books start after the floor had been
        # crossed and before any sample noticed.
        self.interval = max(1, min(interval, self.ROOM_INTERVAL))
        #: Sticky once any sample finds the volume below the floor. Without
        #: it only the sampled book stopped, and every unsampled book after it
        #: went on writing: twelve books on four workers wrote ten.
        self.floor_crossed = False

    def should_check_room(self) -> bool:
        """
        Report whether this book should re-measure the output volume.

        Counts the checks rather than the completions: workers start together
        and would all read the same completion count, so every one of them
        sampled. The caller holds the report lock, which makes this atomic.
        """
        self.checks += 1
        return (self.checks - 1) % self.interval == 0

    def has_room(self, output_dir: Path, min_free_mb: int) -> bool:
        """
        Report whether one more write may start, measuring when it is due.

        Once a sample finds the floor crossed, every later caller is refused
        without measuring: the volume does not get emptier by being asked
        again, and asking only every ``interval`` writes is what let the
        writes in between carry on.

        :param output_dir: Directory being written to.
        :param min_free_mb: Floor in MiB; 0 disables the check.

        :return: True when the write may go ahead.
        """
        with _REPORT_LOCK:
            if self.floor_crossed:
                return False
            measure = self.should_check_room()
        if not measure or _has_room(output_dir, min_free_mb):
            return True
        with _REPORT_LOCK:
            self.floor_crossed = True
        return False

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
    if not progress.has_room(target.parent, run.min_free_mb):
        # Not started, so not failed: counted the way the pre-flight check
        # counts a full volume, and the summary sends it back to a rerun.
        with _REPORT_LOCK:
            report.aborted = True
            report.stopped.add(package)
        logger.info(
            "Not started, the volume is below --min-free: %s", printable(package.name)
        )
        return

    try:
        file_count = zip_package(
            package,
            target,
            run.validation,
            annotations_for_book(package.name, run.annotations or {}),
        )
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
    the output lock, and calls :func:`export_planned`.

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
        report.stopped.update(package for package, _ in pending)
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


def matches_pattern(name: str, pattern: str) -> bool:
    """
    Whether one package name answers to a ``--match`` pattern.

    Apart from :func:`filter_packages` so the advice ``--verify`` prints can
    be checked against the very rule that will read it, without logging a
    match count for a run that is not happening.

    :param name: A package directory's name, suffix included.
    :param pattern: The user's pattern.

    :return: True when :func:`filter_packages` would keep the package.
    """
    # Both composed first: a name that has lived on HFS+ is stored decomposed
    # and a pattern is typed composed, so "--match café" found nothing. Only
    # lowered after that, as matching always was, rather than case-folded as
    # filesystem_key is: folding turns "ß" into "ss" and would change what a
    # bracket expression such as "[ß]" means.
    needle = unicodedata.normalize("NFC", pattern).lower()
    if not any(char in needle for char in "*?["):
        needle = f"*{needle}*"
    return fnmatch.fnmatch(unicodedata.normalize("NFC", name).lower(), needle)


def filter_packages(packages: Sequence[Path], pattern: str | None) -> list[Path]:
    """
    Narrow the package list to those matching a user pattern.

    A pattern with no glob metacharacter matches anywhere in the name, so
    ``--match hobbit`` finds ``The Hobbit.epub``. Anything else is treated as
    a glob against the whole name. Matching is case-insensitive, and blind to
    whether an accent is stored composed or decomposed.

    :param packages: Discovered package directories.
    :param pattern: The user's pattern, or None to keep everything.

    :return: The packages that matched.
    """
    if pattern is None:
        return list(packages)

    matched = [p for p in packages if matches_pattern(p.name, pattern)]
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
    handle = _open_lock_file(path, output_dir)
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
                    printable(str(output_dir)),
                    printable(str(exc)),
                )
                handle.close()
                yield False
                return
            raise OutputLockedError(
                f"another ibook2epub run is already using "
                f"{printable(str(output_dir))} "
                f"({_read_lock_holder(handle)})",
                contended=True,
            ) from exc

        # The PID is recorded for diagnostics only. It is deliberately NOT
        # used to detect or remove an orphaned lock: flock is released by the
        # kernel when the holder dies, even on SIGKILL, so a leftover lock
        # file is inert. Checking liveness by PID would add a PID-reuse race
        # to solve a problem that does not exist.
        _record_holder(handle, path)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def _open_lock_file(path: Path, output_dir: Path) -> BinaryIO:
    """
    Open the lock file for writing, creating it if absent, never through a link.

    Opened read-write always, creating it only if absent. The old
    exists()-then-open("w") had a window where a concurrent run created the
    file between the two calls and this one truncated the holder details it
    was about to report.

    Opened by name, it was followed: a symlink planted at the lock's name had
    its target truncated for the holder's pid, a dangling one created a file
    wherever it pointed, and a hard link truncated its other name. The rule is
    :func:`~epubconvert.utils.contained.open_contained`'s -- ``O_NOFOLLOW`` at
    open, then the descriptor judged: a regular file with one name.

    :param path: The lock file.
    :param output_dir: The directory it locks, for the message.

    :return: The open lock file, not yet locked.

    :raises OutputLockedError: If it cannot be opened, or is not a plain file.
        Nothing has been written to it either way.
    """
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    shown = printable(str(output_dir))
    not_plain = f"cannot lock {shown}: its lock file is not a plain file"
    try:
        descriptor = os.open(path, flags, 0o644)
    except OSError as exc:
        # ELOOP is O_NOFOLLOW refusing a symlink. Anything else -- a read-only
        # output directory got past main's mkdir(exist_ok=True) and died here
        # with a raw traceback -- is an unopenable lock file. main turns either
        # into a clean exit 5.
        message = (
            not_plain
            if exc.errno == errno.ELOOP
            else f"cannot lock {shown}: {printable(str(exc))}"
        )
        raise OutputLockedError(message, contended=False) from exc
    try:
        info = os.fstat(descriptor)
    except OSError as exc:  # pragma: no cover - fstat on an open descriptor
        os.close(descriptor)
        raise OutputLockedError(
            f"cannot lock {shown}: {printable(str(exc))}", contended=False
        ) from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise OutputLockedError(not_plain, contended=False)
    return os.fdopen(descriptor, "rb+", buffering=0)


def _record_holder(handle: BinaryIO, path: Path) -> None:
    """
    Note this run's pid and host in the lock file, for a refused run to quote.

    Diagnostic, so a failure is logged and passed over. It was unguarded: on a
    full volume the write raised ENOSPC, the buffered handle raised it again as
    it closed, and the run died with two tracebacks and exit 1 before
    ``--min-free`` could stop it cleanly. Written to the descriptor rather than
    through the handle, so nothing is left buffered for ``close()`` to retry.

    :param handle: The open, locked lock file.
    :param path: Its path, for the log.
    """
    # Through the surrogate-safe encoder: a host name is decoded from bytes
    # the operating system chose, and a plain encode() raises on an escape.
    line = encode_name(f"pid={os.getpid()} host={socket.gethostname()}\n")
    try:
        descriptor = handle.fileno()
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, line)
    except OSError as exc:
        logger.debug(
            "Could not record the lock holder in %s: %s",
            printable(str(path)),
            printable(str(exc)),
        )


def _read_lock_holder(handle: BinaryIO) -> str:
    """
    Describe whoever currently holds the lock, for the error message.

    :param handle: The open lock file.

    :return: A short description, or a fallback when nothing is readable.
        Escaped for display: it is read back from the output directory, so
        anyone who can write there decides what it says.
    """
    try:
        # Read as bytes and decoded as a filename is, with surrogate escapes.
        # A host name is whatever bytes the system chose, and reading the file
        # as UTF-8 text raised UnicodeDecodeError on one that is not: a
        # traceback where a held lock exits 3. pread reads from the start
        # without moving the handle, and no more than a holder writes.
        details = os.fsdecode(os.pread(handle.fileno(), 4096, 0)).strip()
    except OSError:  # pragma: no cover - unreadable lock file
        return "holder unknown"
    return printable(details) if details else "holder unknown"


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


def sweep_partials(output_dir: Path, now: float | None = None) -> int:
    """
    Remove temporary archives left by a run that was killed outright.

    :func:`zip_package` unlinks its own temporary on any ordinary failure,
    Ctrl-C included. A SIGKILL or a power loss gives it no chance, and every
    glob in this tool looks for ``*.epub``, so the leftovers are invisible to
    everything afterwards and accumulate on the very volume ``--min-free``
    exists to protect.

    The glob is anchored on :data:`~epubconvert.export.archive.PARTIAL_PREFIX` as
    well as the suffix. A bare ``*.part`` also matches a browser's in-progress
    download or a user's own file, and the default output directory is
    ``~/Books`` -- so the sweep deleted real user data.

    Only called while holding the output lock, and even then only a temporary
    unmodified for :data:`STALE_PARTIAL_SECONDS` is taken for abandoned: the
    lock does not exclude a run that could not lock at all.

    :param output_dir: Directory to sweep.
    :param now: The current time, for tests; defaults to the clock.

    :return: The number of files removed.
    """
    cutoff = (time.time() if now is None else now) - STALE_PARTIAL_SECONDS
    removed = 0
    for stale in output_dir.glob(f"{PARTIAL_PREFIX}*{PARTIAL_SUFFIX}"):
        try:
            if stale.stat().st_mtime > cutoff:
                logger.debug("Left recent temporary %s", printable(stale.name))
                continue
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

    :return: The summary line, with the output directory escaped for display:
        a path that is not UTF-8 raised UnicodeEncodeError under a strict
        stdout after the books were written.
    """
    shelf = printable(str(output_dir))
    if dry_run:
        summary = (
            f"Dry run: would export {report.planned} epub file(s) to "
            f"{shelf} (skipped {report.skipped} already present"
        )
        summary += _clauses(report, failures=False)
        summary += ")."
        if remaining:
            # The same advice a real run gives. A bare count left out that
            # the cap was what held these back.
            summary += _remaining_hint(report, remaining)
        return summary

    summary = (
        f"Exported {report.exported} epub file(s) "
        f"({report.files_written} member files) to {shelf}"
    )
    if report.skipped:
        summary += f", skipped {report.skipped}"
    summary += _clauses(report, failures=True)
    summary += "."
    if report.interrupted:
        summary = f"Interrupted. {summary}"
    if report.aborted:
        summary = f"Aborted: not enough free space on {shelf}. {summary}"
    if remaining:
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
            printable(str(output_dir)),
            min_free_mb,
        )
        return False
    logger.debug("%d MiB free on %s", free, printable(str(output_dir)))
    return True


def exit_code(report: Report) -> int:
    """
    Translate a run's outcome into a process exit code.

    :param report: The run's report.

    :return: 130 if interrupted, 1 if a conversion or a copy failed or the run
        could not proceed at all, otherwise 0.
    """
    if report.interrupted:
        return exits.INTERRUPTED
    failed = report.failed or report.copies_failed
    return exits.FAILED if failed or report.aborted else exits.SUCCESS
