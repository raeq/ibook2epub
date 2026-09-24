"""
Taking books along unchanged: PDFs, and epubs that are already zipped.

Split from :mod:`epubconvert.run.convert` when that module reached the line
limit. The copy shares the export's report, its lock and its pool sizing, and
is otherwise its own concern: nothing here converts anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

from ..collect.source import is_dataless
from ..export.archive import copy_through
from ..export.naming import filesystem_key
from ..utils.app_logger import logger
from ..utils.display import printable
from ..utils.policy import Assignment, NamingPolicy
from .convert import (
    _REPORT_LOCK,
    Report,
    _Progress,
    default_workers,
    matches_pattern,
    progress_for,
)
from .holders import Unopened
from .planning import (
    COLLISION,
    COPIED,
    COPY,
    INCOMPLETE,
    Decision,
    copy_name_opens_file,
    copy_target_name,
)
from .workers import WritingPool


def _copy_and_record(
    group: Sequence[tuple[Path, Path]],
    report: Report,
    progress: _Progress,
    min_free_mb: int,
) -> None:
    """
    Copy files that could share a name, in order, recording each as it lands.

    Runs in the worker thread, for the same reason
    :func:`~epubconvert.run.convert._zip_and_record` does: a copy counted
    anywhere else can be on disk and missing from the summary after a Ctrl-C.

    :param group: Sources and their targets, whose names a filesystem may
        treat as one.
    :param report: Report to record each copy in.
    :param progress: The sampler every copy in this run shares, which
        re-measures the volume and remembers once it is below the floor.
    :param min_free_mb: The ``--min-free`` floor in MiB. A copy writes to the
        same volume a conversion does, and the floor guarded only conversions:
        PDFs went on being copied onto an SD card already below it.
    """
    for source, target in group:
        try:
            there = target.exists()
        except OSError as exc:
            # Raised on 3.10 and 3.11 for an EIO -- a share or a USB volume
            # dropping mid-run -- and it escaped the worker: a traceback, exit
            # 1 and no summary. A copy that cannot be made, as any other.
            _count_failed(report, source, exc)
            continue
        if there:
            # Settled before the copy: a file under the name it was given is
            # its own copy (copynames.claim_copies), one holding another book
            # having made it a collision, or moved it on.
            continue
        if not progress.has_room(target.parent, min_free_mb):
            # Not started, so not failed, as for a conversion the floor stops.
            with _REPORT_LOCK:
                report.aborted = True
            logger.info(
                "Not copied, the volume is below --min-free: %s",
                printable(source.name),
            )
            return
        try:
            copy_through(source, target)
        except OSError as exc:
            _count_failed(report, source, exc)
            continue
        with _REPORT_LOCK:
            report.copied += 1
        logger.info("Copied %s", printable(source.name))


def _count_failed(report: Report, source: Path, exc: OSError) -> None:
    """
    Count and say a copy that could not be made.

    Counted, not only logged: a copy that failed silently left the run
    exiting 0 with a clean summary and the book not on the shelf.

    :param report: Report to count it in.
    :param source: The file that was not copied.
    :param exc: Why.
    """
    with _REPORT_LOCK:
        report.copies_failed += 1
    logger.error("Could not copy %s: %s", printable(source.name), exc)


def on_shelf(target: Path) -> bool:
    """
    Whether a copy's name already holds a file, never raising.

    :param target: Where the copy goes.

    :return: True when something is there. One that cannot be told is taken
        as not there: its copy is attempted, and fails as a copy. Asked by
        every look at a copy's name, in the workers and on the main thread:
        ``Path.exists`` raises EIO there on 3.10 and 3.11, and one look that
        did not catch it ended the run in a traceback with no summary.
    """
    try:
        return target.exists()
    except OSError:
        return False


@dataclass(frozen=True)
class CopyPlan:
    """
    Every file to take along, with the name it takes on the shelf.

    Named once per run and handed to both the orphan check and the copy. Each
    used to name them for itself, and under a metadata policy naming a zipped
    book opens it: the orphan check did so in a loop, one download at a time,
    before the run started, and the copy then opened every one again (#14).
    """

    #: Each file with its name, or None when it was left unnamed.
    named: tuple[tuple[Path, str | None], ...] = ()
    #: Files iCloud has evicted that are left where they are, unopened:
    #: under ``--skip-incomplete``, or ``--no-copy-through``, which copies
    #: nothing.
    evicted: frozenset[Path] = frozenset()
    #: Files that lost the name they wanted to another book, with why. Set by
    #: :func:`placed_copies`; each is a collision, reported and counted.
    lost: tuple[tuple[Path, str], ...] = ()
    #: ``--skip-incomplete``, which leaves an evicted package unopened too.
    skip_incomplete: bool = False
    #: Evicted files whose name holds a file nothing could tell from their
    #: copy (:attr:`~epubconvert.utils.policy.Assignment.unverified`). Set by
    #: :func:`placed_copies`.
    unverified: frozenset[Path] = frozenset()

    @property
    def unopened(self) -> Unopened:
        """The books not to open for their identifier, packages included."""
        return Unopened(self.evicted, packages=self.skip_incomplete)

    @property
    def sources(self) -> list[Path]:
        """Every file this plan was made for, named, unnamed or not."""
        return sorted(
            [source for source, _name in self.named]
            + [source for source, _reason in self.lost]
        )

    @property
    def unnamed(self) -> int:
        """How many files could only have been named by downloading them."""
        return sum(1 for _source, name in self.named if name is None)


def plan_copies(
    copyable: Sequence[Path],
    policy: NamingPolicy,
    *,
    max_workers: int | None = None,
    skip_incomplete: bool = False,
    copied: bool = True,
) -> CopyPlan:
    """
    Name each file for the shelf, in a pool, without opening an evicted one.

    Naming an already-zipped epub under a metadata policy opens it, which is a
    download of its own, so it runs in a pool as the copy does. Under
    ``--skip-incomplete`` an evicted file that could only be named that way
    gets no name: opening it is the download the flag exists to avoid. A stat
    tells which files are evicted, and a stat downloads nothing.

    :param copyable: Files found beside the packages, sorted.
    :param policy: The naming policy this run is using.
    :param max_workers: Size of the thread pool, as for
        :func:`~epubconvert.run.convert.export_planned`.
    :param skip_incomplete: Whether evicted files are to be left alone.
    :param copied: Whether the run copies the files. Under
        ``--no-copy-through`` it does not, so an evicted file is left alone
        whatever ``--skip-incomplete`` says: every zipped book was opened to
        be named, and so downloaded, for a copy that never happens.

    :return: The plan.
    """
    if not copyable:
        return CopyPlan(skip_incomplete=skip_incomplete)
    evicted = (
        frozenset(source for source in copyable if is_dataless(source))
        if skip_incomplete or not copied
        else frozenset()
    )

    def name(source: Path) -> str | None:
        if source in evicted and copy_name_opens_file(source, policy):
            return None
        return copy_target_name(source, policy)

    pool = ThreadPoolExecutor(
        max_workers=default_workers(max_workers), thread_name_prefix="name"
    )
    try:
        names = list(pool.map(name, copyable))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return CopyPlan(
        tuple(zip(copyable, names, strict=True)),
        evicted,
        skip_incomplete=skip_incomplete,
    )


def placed_copies(plan: CopyPlan, copies: Sequence[Assignment]) -> CopyPlan:
    """
    Give each file the name the claim pass and the shelf left it.

    :param plan: The files and the names they wanted, from :func:`plan_copies`.
    :param copies: Their assignments, from
        :func:`~epubconvert.run.copynames.claim_copies`, placed on the shelf
        (:func:`~epubconvert.run.placing.settled`).

    :return: The plan, each file under the name it is written to, or lost.
    """
    by_source = {item.package: item for item in copies}
    named: list[tuple[Path, str | None]] = []
    lost: list[tuple[Path, str]] = []
    for source, name in plan.named:
        item = by_source.get(source)
        if item is None:
            named.append((source, name))
        elif item.filename:
            named.append((source, item.filename))
        else:
            lost.append((source, item.reason or "another book claims this name"))
    return replace(
        plan,
        named=tuple(named),
        lost=tuple(lost),
        unverified=frozenset(
            item.package for item in copies if item.filename and item.unverified
        ),
    )


def select_copies(plan: CopyPlan, pattern: str | None) -> CopyPlan:
    """
    Narrow the copying to the files a ``--match`` pattern names.

    ``--match hobbit -m 1`` converted one book and copied every PDF and zipped
    book in the library, which on an iCloud library is a download of all of
    them. Only the copying is narrowed: the names were claimed against the
    whole library (:func:`~epubconvert.run.copynames.claim_copies`), so a
    matched file is written under the name a full run gives it.

    :param plan: The whole library's copies, under the names they are written
        to.
    :param pattern: The ``--match`` pattern, or None for every file.

    :return: The plan for the matching files.
    """
    if pattern is None:
        return plan
    chosen = {
        source for source in plan.sources if matches_pattern(source.name, pattern)
    }
    return replace(
        plan,
        named=tuple(entry for entry in plan.named if entry[0] in chosen),
        evicted=plan.evicted & chosen,
        lost=tuple(entry for entry in plan.lost if entry[0] in chosen),
        unverified=plan.unverified & chosen,
    )


def copy_decisions(plan: CopyPlan, output_dir: Path) -> list[Decision]:
    """
    Say what a run does with each file it copies, as ``--list`` shows it.

    ``-d`` said "2 to copy" while the listing showed nothing of the copies.
    Settled as the copy settles them (:func:`_group_copies`): a file whose
    name holds its copy is already on the shelf, one not downloaded that is
    not there, or cannot be told from the file there, is incomplete, and one
    that lost its name is a collision.

    :param plan: The files this run copies, under the names they take.
    :param output_dir: Directory the copies go into.

    :return: One decision per file, in sorted order.
    """
    decisions = [
        Decision(source, COLLISION, reason=reason) for source, reason in plan.lost
    ]
    for source, name in plan.named:
        reason = _not_downloaded(plan, output_dir, source, name)
        if reason is not None or name is None:
            decisions.append(Decision(source, INCOMPLETE, reason=reason))
        elif on_shelf(output_dir / name):
            decisions.append(Decision(source, COPIED, output_dir / name))
        else:
            decisions.append(Decision(source, COPY, output_dir / name))
    return sorted(decisions, key=lambda decision: decision.package)


def _not_downloaded(
    plan: CopyPlan, output_dir: Path, source: Path, name: str | None
) -> str | None:
    """
    Say why an evicted file is skipped as not downloaded, if it is.

    One whose copy is already on the shelf is finished work. One left
    unnamed cannot be looked for, and nor can one whose name holds a file
    nothing could tell from its copy (:attr:`CopyPlan.unverified`): taken
    for its copy, a zipped book added under a deleted book's name was
    reported copied from that book's file, and never copied.

    :param plan: The files and their names.
    :param output_dir: Directory the copies go into.
    :param source: The file.
    :param name: The name it takes, or None when it was left unnamed.

    :return: The reason, or None when the file is not skipped for it.
    """
    if source not in plan.evicted and name is not None:
        return None
    if name is None or not on_shelf(output_dir / name):
        return "not downloaded from iCloud"
    if source in plan.unverified:
        return f"not downloaded from iCloud; cannot tell whether {name} is its copy"
    return None


def _group_copies(
    plan: CopyPlan, output_dir: Path, report: Report
) -> list[list[tuple[Path, Path]]]:
    """
    Group the copies that could land on one name, and set evicted files aside.

    An evicted file is settled against the shelf first, as a package is: one
    whose copy is already there is finished work, not a skipped book. Without
    that, every rerun after iCloud evicts the source again would report the
    whole PDF shelf as not downloaded. One left unnamed cannot be looked for,
    so it counts as not downloaded, as does one whose name holds a file it
    cannot be told from (:func:`_not_downloaded`).

    :param plan: The files and their names, from :func:`plan_copies`.
    :param output_dir: Directory to copy into.
    :param report: Counted into for each evicted file not on the shelf.

    :return: Sources and targets, grouped by the name a filesystem sees.
    """
    # A copy that lost its name is a collision, as a package that lost one is,
    # and named as one: it used to find the winner's file under its name,
    # take it for its own and say nothing.
    for source, reason in plan.lost:
        logger.warning(
            "Name collision, skipping: %s (%s)",
            printable(source.name),
            printable(reason),
        )
    groups: dict[str, list[tuple[Path, Path]]] = {}
    not_downloaded: list[tuple[Path, str]] = []
    for source, name in plan.named:
        if source in plan.evicted or name is None:
            skipped = _not_downloaded(plan, output_dir, source, name)
            if skipped is not None:
                not_downloaded.append((source, skipped))
            continue
        groups.setdefault(filesystem_key(name), []).append((source, output_dir / name))
    for source, skipped in not_downloaded:
        logger.warning("Skipped, %s: %s", printable(skipped), printable(source.name))
    with _REPORT_LOCK:
        report.incomplete += len(not_downloaded)
        report.collisions += len(plan.lost)
    return list(groups.values())


def copy_through_all(
    plan: CopyPlan,
    output_dir: Path,
    report: Report,
    *,
    max_workers: int | None = None,
    min_free_mb: int = 0,
    dry_run: bool = False,
) -> None:
    """
    Put already-valid books on the shelf without converting them.

    Rerun-safe on the same terms as everything else: a file already there is
    left alone rather than rewritten, so a second run does nothing and says
    nothing.

    Concurrent, because the work is downloading rather than copying. Reading a
    file iCloud has evicted makes macOS fetch it, and this used to be a loop
    run before the pool existed: a run with ``-w 64`` was seen pulling PDFs one
    at a time at about 4.5 MB/s while every worker sat idle (#10).

    Two files wanting one name were copied by one worker, in sorted order, so
    the first won and the rest found its file and skipped without a word. The
    claim pass now settles that before the copy
    (:func:`~epubconvert.run.copynames.claim_copies`): the first in sorted order
    still wins, whichever download finishes first, and the rest are reported
    as collisions or take a suffix. Grouping by the name APFS sees is kept as
    the guard that no two workers write one file.

    :param plan: The files and the names they take, from :func:`plan_copies`.
    :param output_dir: Directory to copy into.
    :param report: Counted into as each file lands, rather than totalled and
        returned at the end. A Ctrl-C part-way through left the summary saying
        nothing was copied while the files were already on disk.
    :param max_workers: Size of the thread pool, as for
        :func:`~epubconvert.run.convert.export_planned`.
    :param min_free_mb: The ``--min-free`` floor in MiB; 0 disables it.
        Measured before the pool starts, as the export measures, and then
        sampled as each file is copied. A dry run measures it once.
    :param dry_run: Copy nothing; count in ``report.copied`` the files that
        would be copied, and report the rest as a real run does. A dry run
        said nothing about copies at all, and the real run then said
        "3 copied".
    """
    groups = _group_copies(plan, output_dir, report)
    waiting = sum(
        1 for group in groups for _source, target in group if not on_shelf(target)
    )
    # Measured only when there is a copy to make, as the shelf is judged
    # (preflight.check_writable): a rerun with every file already copied was
    # stopped by a full volume it had nothing to write to. And in the dry
    # run too, which said "to copy" of files the real run left unattempted.
    if not waiting:
        return
    progress = progress_for(
        sum(len(group) for group in groups), default_workers(max_workers)
    )
    if not progress.has_room(output_dir, min_free_mb):
        logger.warning("Nothing copied: the volume is below --min-free.")
        report.aborted = True
        return
    if dry_run:
        report.copied += waiting
        return
    pool = WritingPool(
        max_workers=default_workers(max_workers), thread_name_prefix="copy"
    )
    try:
        futures = [
            pool.submit(_copy_and_record, group, report, progress, min_free_mb)
            for group in groups
        ]
        # result() re-raises in this thread whatever escaped a worker, so a
        # surprise still stops the run as it did when the loop ran here.
        for future in futures:
            future.result()
    finally:
        # As for the exports: copies not yet started are dropped, and the ones
        # in flight finish, replace atomically and record themselves, however
        # often the wait for them is interrupted.
        pool.finish("file(s)")
