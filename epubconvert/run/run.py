"""
Driving one run from the command line.

Everything between parsing arguments and returning an exit code: what a run
announces before it starts, the read-only ``--list`` and ``--verify`` branches,
and the export itself under the output directory lock. What the run needs from
the machine is judged by :mod:`epubconvert.run.preflight`, ``--verify`` and its
advice are :mod:`epubconvert.run.repair`'s, and where the reader's annotations
go is :mod:`epubconvert.run.annotating`'s concern.

Held apart from :mod:`epubconvert.run.convert` so the exporter can be used as a
library without argparse in the call chain.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ..collect.annotations import STDOUT
from ..collect.validate import ValidationOptions
from ..export.archive import (
    collect_copyable,
    collect_package_dirs,
    count_ignored,
    index_by_package,
)
from ..export.detached import vault_of
from ..export.naming import (
    PortableNamesUnavailableError,
    PortableNaming,
    StripNaming,
    build_policy,
)
from ..utils import app_logger, display, exits
from ..utils.app_logger import logger
from ..utils.display import emit, printable
from ..utils.policy import Assignment, NamingPolicy
from .afterwards import after_export, outcome, pending_packages, selected_names
from .annotating import (
    apply_annotations,
    gather_annotations,
    run_container_only,
)
from .claims import shelf_names
from .cli import parse_args
from .convert import (
    ExportOptions,
    OutputLockedError,
    Report,
    cap_exports,
    count_pending_decisions,
    export_planned,
    filter_packages,
    output_lock,
    sweep_partials,
)
from .copying import (
    CopyPlan,
    copy_decisions,
    copy_through_all,
    on_shelf,
    placed_copies,
    plan_copies,
    select_copies,
)
from .copynames import Names, claim_copies
from .orphans import find_orphans, orphan_decisions
from .placing import settled
from .planning import (
    Decision,
    PlanOptions,
    assign_names,
    plan_exports,
)
from .preflight import ShelfUnwritableError, check_environment, check_writable
from .repair import run_verify
from .reporting import render_listing
from .summary import format_summary


def _log_preamble(args: argparse.Namespace, policy: NamingPolicy) -> None:
    """
    Say what this run is about to do, before it does it.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    """
    logger.debug("Naming policy: %s", policy.label)
    # A run that converts nothing touches neither, and announcing them named
    # a source it never opened and an output directory it never created.
    # A vault is the exception: it names its notes from the library.
    converts_nothing = bool(args.library_export or args.annotations_only)
    if not converts_nothing or vault_of(args) is not None:
        if args.source_auto:
            logger.info(
                "Using discovered iBooks library: %s", printable(str(args.source_dir))
            )
        else:
            logger.info("Examining source: %s", printable(str(args.source_dir)))
    if args.list_only or args.verify:
        # Both only read the shelf, and "Writing" said otherwise.
        logger.info("Reading output directory: %s", printable(str(args.output_dir)))
    elif not converts_nothing:
        logger.info("Writing output to: %s", printable(str(args.output_dir)))
    # Keyed off the policy object rather than re-derived from the raw argument.
    # Two independent statements of one fact drift apart the moment
    # build_policy's mapping changes, and the debug line above is the one that
    # would still be right.
    if isinstance(policy, StripNaming):
        logger.info("Portable naming: stripping characters other filesystems reject.")
    elif isinstance(policy, PortableNaming):
        logger.info(
            "Portable naming: romanizing (non-Latin titles are transliterated)."
        )
    if args.dry_run:
        logger.info(
            "Running in dry-run mode. No file system modifications will be performed."
        )


def _shared_names(
    args: argparse.Namespace,
    discovered: Sequence[Path],
    policy: NamingPolicy,
    copies: CopyPlan,
) -> tuple[Names, CopyPlan]:
    """
    Name every package and every file copied through once, for every caller.

    Always the whole library, never the subset ``--match`` selected. Naming
    the subset gave a matched book a different name from the one a full run
    gives it: alone in its selection, one edition of a crowded title got no
    marker, so a book already exported as ``Dune [digest]`` was pending under
    the plain name, written again, and the duplicate became an orphan. The
    run plans only the books it selected, looked up in this one assignment.

    It also saves work. Naming reads a package document per book under a
    metadata policy, and planning and orphan detection each named their own
    set: on a real 2,805-book library, 5,610 reads for one listing.

    The copies take their names in the same pass, after the packages
    (:func:`~epubconvert.run.copynames.claim_copies`), and are placed on the
    shelf as the plan places a book, so the copy writes where the plan and
    the orphan check expect it.

    :param args: Parsed command line arguments.
    :param discovered: Every package in the library.
    :param policy: The naming policy in force.
    :param copies: The files to take along, with the names they want.

    :return: Every name, and the copy plan under the names it is written to.
    """
    names = claim_copies(
        assign_names(
            discovered,
            policy,
            args.on_collision,
            shelf=shelf_names(args.output_dir),
            unopened=copies.unopened,
            library=args.source_dir,
        ),
        copies.named,
        policy,
        args.on_collision,
        output_dir=args.output_dir,
        unopened=copies.unopened,
    )
    if not names.copies:
        return names, copies
    placed = settled(
        [*names.packages, *names.copies],
        args.output_dir,
        policy,
        unopened=copies.unopened,
    )[len(names.packages) :]
    return Names(names.packages, placed), placed_copies(copies, placed)


def _plan_copies(args: argparse.Namespace, policy: NamingPolicy) -> CopyPlan:
    """
    Find the files to take along and name them, once, for every caller.

    The orphan check, the copy and the ignored count all read this one plan.
    Made under ``--no-copy-through`` too, which only stops the copying: left
    empty, a package was reported exported from a zipped book's file, a copy
    whose book is still in the library was listed as an orphan, and the
    zipped books were counted as not books.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: The plan. When some files went unnamed, the run is told what that
        costs rather than left with an orphan count it cannot explain.
    """
    plan = plan_copies(
        collect_copyable(args.source_dir),
        policy,
        max_workers=args.workers,
        skip_incomplete=args.skip_incomplete,
        copied=not args.no_copy_through,
    )
    if plan.unnamed:
        logger.warning(
            "%d book(s) not downloaded from iCloud could not be named without "
            "downloading them; a copy of one already on the shelf is counted "
            "as an orphan.",
            plan.unnamed,
        )
    return plan


def _run_listing(args: argparse.Namespace, policy: NamingPolicy) -> int:
    """
    Render the plan without converting anything.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: A process exit code.
    """
    discovered = collect_package_dirs(args.source_dir)
    packages = filter_packages(discovered, args.match)
    shared, copies = _shared_names(args, discovered, policy, _plan_copies(args, policy))
    everything = [*shared.packages, *shared.copies]
    decisions = plan_exports(
        packages, args.output_dir, policy, _plan_options(args), assigned=everything
    )
    # The files it copies, as the run settles them: a listing that left them
    # out said nothing where -d said "2 to copy".
    decisions += copy_decisions(_to_copy(args, copies), args.output_dir)
    # Orphans come from the whole library, not this run's filtered subset:
    # --match narrows a run, not the shelf. Files copied through claim their
    # names too, or the shelf would report what this run just put there.
    orphans = orphan_decisions(
        find_orphans(
            args.output_dir,
            policy,
            discovered,
            args.on_collision,
            assigned=everything,
            unopened=copies.unopened,
            copied=not args.no_copy_through,
        ),
        everything,
    )
    emit(render_listing(decisions + orphans, args.as_json))
    ignored = count_ignored(args.source_dir, discovered) - len(copies.sources)
    if not args.as_json:
        uncopied = len(select_copies(copies, args.match).sources)
        if args.no_copy_through and uncopied:
            emit(f"{uncopied} not copied (--no-copy-through)")
        if ignored:
            emit(f"{ignored} ignored (not books)")
    return 0


def _plan_options(args: argparse.Namespace) -> PlanOptions:
    """
    Build planning options from parsed arguments.

    :param args: Parsed command line arguments.

    :return: The planner's settings.
    """
    return PlanOptions(
        force=args.force,
        refresh=args.refresh,
        check_incomplete=args.skip_incomplete,
        on_collision=args.on_collision,
        library=args.source_dir,
    )


def _survey(
    args: argparse.Namespace, policy: NamingPolicy, report: Report
) -> tuple[list[Path], CopyPlan, list[Assignment]]:
    """
    Work out what the library holds and what the shelf already has.

    Everything the export needs before it takes the lock: the packages, the
    files to copy, the names, and the orphans on the shelf.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    :param report: Counted into for the ignored and orphaned files.

    :return: The packages this run converts, the files it copies, and the
        names it gives the packages.
    """
    discovered = collect_package_dirs(args.source_dir)
    packages = filter_packages(discovered, args.match)
    copies = _plan_copies(args, policy)
    # Not said when the run has files to copy: "No matching *.epub packages"
    # and then "Copied Paper.pdf" read as having found nothing and then done
    # something.
    if not packages and not _to_copy(args, copies).sources:
        logger.warning(
            "No matching *.epub packages found under %s",
            printable(str(args.source_dir)),
        )

    report.ignored = count_ignored(args.source_dir, discovered) - len(copies.sources)
    shared, copies = _shared_names(args, discovered, policy, copies)
    everything = [*shared.packages, *shared.copies]
    report.orphaned = len(
        find_orphans(
            args.output_dir,
            policy,
            discovered,
            args.on_collision,
            assigned=everything,
            unopened=copies.unopened,
            copied=not args.no_copy_through,
        )
    )
    return packages, copies, everything


def _run_export(
    args: argparse.Namespace,
    policy: NamingPolicy,
    found: list[dict[str, Any]] | None,
) -> tuple[Report, int, list[Assignment], list[Path], frozenset[Path]]:
    """
    Collect, select and export, under the output directory lock.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    :param found: The annotations this run read, or None. Embedded as each
        archive is written rather than by rebuilding it afterwards: applying
        them after the fact serialised every annotated book twice. Measured
        over 200 books, 1.09s the two-pass way against 0.62s in one.

    :return: The run's report, the number of books still to convert, and the
        names it gave every book it converted. Annotations are applied against
        that same assignment afterwards: naming the library again disagreed
        with it under ``--match`` with a collision suffix, so the archive the
        refresh looked for did not exist. The library's copyable files,
        which the annotation step needs for the same reason the embed does.
        And the books ``-m`` held back, which a later run converts.

    :raises OutputLockedError: If another run holds the output lock, or the
        lock file could not be opened.
    """
    # Held here rather than inside the exporter so the partial counts survive
    # a Ctrl-C.
    report = Report()
    try:
        packages, copies, assigned = _survey(args, policy, report)
    except KeyboardInterrupt:
        # Guarded as the export is. Under a metadata policy this reads every
        # package document, minutes on a cloud library, and a Ctrl-C here was
        # a traceback with no summary and no 130.
        report.interrupted = True
        logger.warning("Interrupted before anything was written.")
        return report, 0, [], [], frozenset()
    if args.force and args.max_export_files and len(packages) > args.max_export_files:
        logger.warning(
            "--force selected %d book(s) but -m limits this run to %d; "
            "pass -m 0, or --match to name the books you mean.",
            len(packages),
            args.max_export_files,
        )

    copyable = copies.sources if found is not None else []
    options = ExportOptions(
        covers=args.covers,
        min_free_mb=args.min_free,
        validation=ValidationOptions(enabled=args.validate, epubcheck=args.epubcheck),
        plan=_plan_options(args),
        annotations=(
            index_by_package(found, packages, copyable=copyable)
            if found is not None and args.annotations_embedded and not args.dry_run
            else None
        ),
    )

    # Planned exactly once, and inside the lock. Both the work list and the
    # count of what is left come from this one plan, so they cannot describe
    # different libraries; planning outside the lock would let a concurrent
    # run move the output directory underneath the decisions.
    pending_before = 0
    decisions: list[Decision] = []
    held: frozenset[Path] = frozenset()
    # The guard covers taking the lock and the sweep too. They were outside
    # it, and a Ctrl-C there escaped to main's last resort: 130, but no
    # summary, and nothing on stdout at all under -q.
    try:
        # A dry run writes nothing, so it needs no lock and must not create
        # one.
        lock = nullcontext(False) if args.dry_run else output_lock(args.output_dir)
        with lock as locked:
            # Only with real exclusivity. Unlocked, another run's in-flight
            # temporary looks exactly like an abandoned one, and deleting it
            # makes that run's closing replace fail.
            if locked and not args.dry_run:
                sweep_partials(args.output_dir)
            # Judged once there is something to write, in the dry run too:
            # see preflight.check_writable. Before the copies, which are
            # written before the books are planned.
            if _copies_pending(_to_copy(args, copies), args.output_dir):
                check_writable(args.output_dir)
            copy_through_all(
                _to_copy(args, copies),
                args.output_dir,
                report,
                max_workers=args.workers,
                min_free_mb=args.min_free,
                dry_run=args.dry_run,
            )
            # Planning is inside the guard too: under --skip-incomplete it
            # walks every package in the library, which is minutes of work on
            # a cloud shelf, and a Ctrl-C there produced a raw traceback with
            # no summary and no 130.
            decisions = plan_exports(
                packages, args.output_dir, policy, options.plan, assigned=assigned
            )
            pending_before = count_pending_decisions(decisions)
            selected = cap_exports(
                decisions, args.max_export_files, randomise=not args.no_shuffle
            )
            # Counted where the cap is applied, so the summary can tell books
            # it held back from books that failed rather than infer it.
            report.held_back = pending_before - count_pending_decisions(selected)
            held = pending_packages(decisions) - pending_packages(selected)
            if count_pending_decisions(selected):
                check_writable(args.output_dir)
            asyncio.run(
                export_planned(
                    selected,
                    args.output_dir,
                    dry_run=args.dry_run,
                    max_workers=args.workers,
                    report=report,
                    options=options,
                )
            )
    except KeyboardInterrupt:
        # Stopping is a normal way to end a long run: every finished book is
        # already complete and atomically in place, so a rerun simply
        # continues.
        report.interrupted = True
        logger.warning(
            "Interrupted; %d book(s) exported before stopping.", report.exported
        )

    # A dry run exports nothing, so what it would export is what it takes
    # off: counting only exports said "-m 0 -d" would leave every book it
    # had just listed.
    return (
        report,
        max(
            0,
            pending_before - (report.planned if args.dry_run else report.exported),
        ),
        # The files --match selects too, copied or not: a vault writes a note
        # for each. Under --no-copy-through their highlights are still the
        # point of a note, and the vault had none for a zipped book or a PDF.
        selected_names(
            assigned,
            [*packages, *select_copies(copies, args.match).sources],
            decisions,
            policy,
        ),
        copyable,
        held,
    )


def _copies_pending(copies: CopyPlan, output_dir: Path) -> bool:
    """
    Whether copying these files would write anything.

    :param copies: The files this run copies.
    :param output_dir: The shelf.

    :return: True when a file that is to be copied is not on the shelf yet.
    """
    return any(
        name is not None
        and source not in copies.evicted
        and not on_shelf(output_dir / name)
        for source, name in copies.named
    )


def _to_copy(args: argparse.Namespace, copies: CopyPlan) -> CopyPlan:
    """
    Narrow the library's copies to the ones this run copies.

    :param args: Parsed command line arguments.
    :param copies: Every file to take along, under the names it takes.

    :return: The files ``--match`` selects, or none under
        ``--no-copy-through``: the flag stops the copying, and the copies
        still claim their names and their files on the shelf.
    """
    return CopyPlan() if args.no_copy_through else select_copies(copies, args.match)


def _run_read_only(args: argparse.Namespace, policy: NamingPolicy) -> int | None:
    """
    Run whichever reporting mode was asked for, if either was.

    ``--list`` and ``--verify`` only read. Creating the destination for them
    would turn a typo in ``-o`` into a stray directory instead of a report
    about the one that was meant.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: An exit code, or None when this is an ordinary export run.
    """
    # Before --list and --verify: these read Apple's container rather than the
    # library or the shelf, so neither of their preconditions applies.
    if args.annotations_only or args.library_export:
        return run_container_only(args, policy)
    if args.annotations_refresh:
        return apply_annotations(args, policy)  # -ar converts nothing
    if args.list_only:
        return _run_listing(args, policy)
    if args.verify:
        return run_verify(args)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the conversion from the command line.

    :param argv: Argument list, defaulting to ``sys.argv[1:]``.

    :return: A process exit code, one defined with its meaning in
        :mod:`epubconvert.utils.exits` and listed in the README's exit-code
        table. A bad command line never returns here: argparse exits with
        :data:`~epubconvert.utils.exits.USAGE` itself.
    """
    args = parse_args(argv)

    verbosity = 0 if args.quiet else 1 + args.verbose
    app_logger.configure(verbosity=verbosity, log_file=args.log_file)

    display.start_report()
    try:
        code = _run(args)
        # A report that could not be written -- a listing, a verdict, a
        # summary -- is a destination the run could not use, and a script
        # that reads only the status would otherwise take a lost report for a
        # clean run. Any other failure outranks it, as it outranks a detached
        # file's. Asked inside the try: after it, a Ctrl-C here was a
        # traceback once all the work was done.
        if code == exits.SUCCESS and display.report_lost():
            code = exits.NO_OUTPUT
    except KeyboardInterrupt:
        # The export stops cleanly and says what it finished. Everything else
        # -- reading the highlights, --list, --verify, the -ar refresh -- has
        # no report to finish, and a Ctrl-C there was a traceback and exit 1.
        # Each writes by atomic replace, so nothing is left half-written.
        logger.warning("Interrupted; rerun to continue.")
        return exits.INTERRUPTED
    return code


def _run(args: argparse.Namespace) -> int:
    """
    Do what the command line asked, once logging is set up.

    :param args: Parsed command line arguments.

    :return: A process exit code, as for :func:`main`.
    """
    unusable = check_environment(args)
    if unusable is not None:
        return unusable

    try:
        policy = build_policy(args.portable_names, args.name_by)
    except PortableNamesUnavailableError as exc:
        logger.critical("%s", exc)
        return exits.MISSING_TOOL

    _log_preamble(args, policy)

    # --list and --verify only read. Creating the destination for them would
    # turn a typo in -o into a stray directory instead of a report about the
    # one that was meant.
    read_only = _run_read_only(args, policy)
    if read_only is not None:
        return read_only

    if not args.dry_run:
        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.critical("Could not create output directory: %s", exc)
            return exits.NO_OUTPUT

    # Read once, before anything is written, and handed to both the export
    # that embeds them and the write that detaches them.
    found = gather_annotations(args, policy)

    try:
        report, remaining, named, copyable, held = _run_export(args, policy, found)
    except (OutputLockedError, ShelfUnwritableError) as exc:
        logger.critical("%s", exc)
        return exc.exit_code

    # After the books are on the shelf, so annotations reach them by the same
    # path --annotations-refresh uses. A dry run writes nothing, here included.
    annotated = after_export(
        args,
        policy,
        report,
        named=named,
        found=found,
        copyable=copyable,
        held_back=held,
    )
    summary = format_summary(report, args.output_dir, args.dry_run, remaining)
    # Standard output belongs to the document when one is going there; a
    # summary in the middle of it would make the JSON unparsable, which is the
    # one thing a pipe cannot tolerate.
    # Either way, `ibook2epub | head` closes the pipe before it.
    emit(summary, stderr=args.annotations_detached == STDOUT)
    # Recorded in the log file only: the console already has it from the
    # print above, and logging it plainly printed every run's summary twice.
    app_logger.file_only(summary)
    logger.debug(
        "Run finished: %d exported, %d failed",
        report.exported,
        report.failed + report.copies_failed,
    )

    return outcome(report, annotated)
