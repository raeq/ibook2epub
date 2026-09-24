"""
Taking books along unchanged: PDFs, and epubs that are already zipped.

Split from :mod:`epubconvert.run.convert` when that module reached the line
limit. The copy shares the export's report, its lock and its pool sizing, and
is otherwise its own concern: nothing here converts anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ..collect.source import is_dataless
from ..export.archive import copy_through
from ..export.naming import filesystem_key
from ..utils.app_logger import logger
from ..utils.display import printable
from ..utils.policy import NamingPolicy
from .convert import _REPORT_LOCK, Report, default_workers
from .planning import copy_name_opens_file, copy_target_name


def _copy_and_record(group: Sequence[tuple[Path, Path]], report: Report) -> None:
    """
    Copy files that could share a name, in order, recording each as it lands.

    Runs in the worker thread, for the same reason
    :func:`~epubconvert.run.convert._zip_and_record` does: a copy counted
    anywhere else can be on disk and missing from the summary after a Ctrl-C.

    :param group: Sources and their targets, whose names a filesystem may
        treat as one.
    :param report: Report to record each copy in.
    """
    for source, target in group:
        if target.exists():
            continue
        try:
            copy_through(source, target)
        except OSError as exc:
            logger.error("Could not copy %s: %s", printable(source.name), exc)
            continue
        with _REPORT_LOCK:
            report.copied += 1
        logger.info("Copied %s", printable(source.name))


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
    #: Files ``--skip-incomplete`` leaves where they are.
    evicted: frozenset[Path] = frozenset()

    @property
    def claimed(self) -> list[str]:
        """The names these files hold on the shelf, for the orphan check."""
        return [name for _source, name in self.named if name is not None]

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

    :return: The plan.
    """
    if not copyable:
        return CopyPlan()
    evicted = (
        frozenset(source for source in copyable if is_dataless(source))
        if skip_incomplete
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
    return CopyPlan(tuple(zip(copyable, names, strict=True)), evicted)


def _group_copies(
    plan: CopyPlan, output_dir: Path, report: Report
) -> list[list[tuple[Path, Path]]]:
    """
    Group the copies that could land on one name, and set evicted files aside.

    An evicted file is settled against the shelf first, as a package is: one
    whose copy is already there is finished work, not a skipped book. Without
    that, every rerun after iCloud evicts the source again would report the
    whole PDF shelf as not downloaded. One left unnamed cannot be looked for,
    so it counts as not downloaded.

    :param plan: The files and their names, from :func:`plan_copies`.
    :param output_dir: Directory to copy into.
    :param report: Counted into for each evicted file not on the shelf.

    :return: Sources and targets, grouped by the name a filesystem sees.
    """
    groups: dict[str, list[tuple[Path, Path]]] = {}
    not_downloaded: list[Path] = []
    for source, name in plan.named:
        if source in plan.evicted or name is None:
            if name is None or not (output_dir / name).exists():
                not_downloaded.append(source)
            continue
        groups.setdefault(filesystem_key(name), []).append((source, output_dir / name))
    for source in not_downloaded:
        logger.warning(
            "Skipped, not downloaded from iCloud: %s", printable(source.name)
        )
    with _REPORT_LOCK:
        report.incomplete += len(not_downloaded)
    return list(groups.values())


def copy_through_all(
    plan: CopyPlan,
    output_dir: Path,
    report: Report,
    *,
    max_workers: int | None = None,
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

    Files that could land on one name are copied by one worker, in sorted
    order, so the first still wins and the rest find its file and skip -- what
    the loop did. Which of two same-named PDFs reaches the shelf therefore does
    not depend on which download finishes first. Names are compared the way
    APFS compares them, since that is where they collide.

    :param plan: The files and the names they take, from :func:`plan_copies`.
    :param output_dir: Directory to copy into.
    :param report: Counted into as each file lands, rather than totalled and
        returned at the end. A Ctrl-C part-way through left the summary saying
        nothing was copied while the files were already on disk.
    :param max_workers: Size of the thread pool, as for
        :func:`~epubconvert.run.convert.export_planned`.
    """
    groups = _group_copies(plan, output_dir, report)
    if not groups:
        return
    pool = ThreadPoolExecutor(
        max_workers=default_workers(max_workers), thread_name_prefix="copy"
    )
    try:
        futures = [pool.submit(_copy_and_record, group, report) for group in groups]
        # result() re-raises in this thread whatever escaped a worker, so a
        # surprise still stops the run as it did when the loop ran here.
        for future in futures:
            future.result()
    finally:
        # As for the exports: copies not yet started are dropped, and the ones
        # in flight finish, replace atomically and record themselves.
        pool.shutdown(wait=True, cancel_futures=True)
