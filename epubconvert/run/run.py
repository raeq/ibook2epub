"""
Driving one run from the command line.

Everything between parsing arguments and returning an exit code: what a run
announces before it starts, the read-only ``--list`` and ``--verify`` branches,
and the export itself under the output directory lock. Where the reader's
annotations go is :mod:`epubconvert.run.annotating`'s concern.

Held apart from :mod:`epubconvert.run.convert` so the exporter can be used as a
library without argparse in the call chain.
"""

from __future__ import annotations

import argparse
import asyncio
import shlex
import sys
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from ..collect.annotations import STDOUT
from ..collect.annotations import index_by_book as index_annotations
from ..collect.validate import (
    ValidationOptions,
    epubcheck_available,
)
from ..export.archive import (
    collect_copyable,
    collect_package_dirs,
    count_ignored,
)
from ..export.detached import vault_of
from ..export.inspect_output import verify_output
from ..export.naming import (
    PortableNamesUnavailableError,
    PortableNaming,
    StripNaming,
    build_policy,
)
from ..utils import app_logger, exits
from ..utils.app_logger import logger
from ..utils.defaults import SOURCE_CANDIDATES
from ..utils.policy import Assignment, NamingPolicy
from .annotating import (
    annotations_after_export,
    apply_annotations,
    gather_annotations,
    run_container_only,
)
from .cli import parse_args
from .convert import (
    ExportOptions,
    OutputLockedError,
    Report,
    cap_exports,
    count_pending_decisions,
    exit_code,
    export_planned,
    filter_packages,
    format_summary,
    output_lock,
    sweep_partials,
)
from .copying import CopyPlan, copy_through_all, plan_copies
from .planning import (
    CollisionMode,
    PlanOptions,
    assign_names,
    find_orphans,
    orphan_decisions,
    plan_exports,
    render_listing,
)


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
            logger.info("Using discovered iBooks library: %s", args.source_dir)
        else:
            logger.info("Examining source: %s", args.source_dir)
    if not converts_nothing:
        logger.info("Writing output to: %s", args.output_dir)
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
    packages: Sequence[Path],
    discovered: Sequence[Path],
    policy: NamingPolicy,
    on_collision: CollisionMode,
) -> list[Assignment] | None:
    """
    Name every package once, when both callers want the same answer.

    Planning and orphan detection each name a set of packages, and naming reads
    a package document per book under a metadata policy -- so computing it in
    both places read every book twice. Measured on a real 2,805-book library:
    5,610 reads for one listing.

    They want different sets whenever ``--match`` or ``-m`` narrows the
    selection: the shelf is judged against the whole library, the run against
    the subset. Sharing only when the two lists agree keeps that distinction,
    at the cost of the saving on a filtered run, which is the smaller run
    anyway.

    :param packages: What this run will convert.
    :param discovered: Every package in the library.
    :param policy: The naming policy in force.
    :param on_collision: The collision mode in force.

    :return: The shared assignment, or None when the callers disagree and each
        must name its own set.
    """
    if list(packages) != list(discovered):
        return None
    return assign_names(discovered, policy, on_collision)


def _plan_copies(args: argparse.Namespace, policy: NamingPolicy) -> CopyPlan:
    """
    Find the files to take along and name them, once, for every caller.

    The orphan check, the copy and the ignored count all read this one plan.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: The plan, empty under ``--no-copy-through``. When some files went
        unnamed, the run is told what that costs rather than left with an
        orphan count it cannot explain.
    """
    plan = plan_copies(
        [] if args.no_copy_through else collect_copyable(args.source_dir),
        policy,
        max_workers=args.workers,
        skip_incomplete=args.skip_incomplete,
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
    copies = _plan_copies(args, policy)
    packages = filter_packages(discovered, args.match)
    shared = _shared_names(packages, discovered, policy, args.on_collision)
    decisions = plan_exports(
        packages, args.output_dir, policy, _plan_options(args), assigned=shared
    )
    # Orphans come from the whole library, not this run's filtered subset:
    # --match narrows a run, not the shelf. Files copied through claim their
    # names too, or the shelf would report what this run just put there.
    orphans = orphan_decisions(
        find_orphans(
            args.output_dir,
            policy,
            discovered,
            args.on_collision,
            claimed_extra=copies.claimed,
            assigned=shared,
        )
    )
    print(render_listing(decisions + orphans, args.as_json))
    ignored = count_ignored(args.source_dir, discovered) - len(copies.named)
    if ignored and not args.as_json:
        print(f"{ignored} ignored (not books)")
    return 0


def _run_verify(args: argparse.Namespace) -> int:
    """
    Check the archives already in the output directory.

    :param args: Parsed command line arguments.

    :return: A process exit code; non-zero if anything is damaged.
    """
    # A glob over a missing directory yields nothing, which read as a clean
    # bill of health: the one command whose purpose is finding damage reported
    # success having checked not a single file.
    if not args.output_dir.is_dir():
        logger.critical("Output directory does not exist: %s", args.output_dir)
        return exits.NO_OUTPUT

    checked, damaged, broken = verify_output(args.output_dir, epubcheck=args.epubcheck)
    if not checked:
        print(f"No archives found in {args.output_dir}.")
        return 0
    print(f"Verified {checked} archive(s) in {args.output_dir}: {damaged} damaged.")
    if damaged:
        # Naming them matters: --force alone re-exports the whole library, and
        # the default cap then picks its subset at random, so following that
        # advice literally could leave every damaged book untouched and still
        # report success.
        print("Re-export each damaged book, for example:")
        for name in broken[:3]:
            print(f"  ibook2epub --match {shlex.quote(Path(name).stem)} --force")
        if len(broken) > 3:
            print(f"  ...and {len(broken) - 3} more")
    return exits.DAMAGED if damaged else exits.SUCCESS


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
    )


def _run_export(
    args: argparse.Namespace,
    policy: NamingPolicy,
    found: list[dict[str, Any]] | None,
) -> tuple[Report, int, list[Assignment]]:
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
        refresh looked for did not exist.

    :raises OutputLockedError: If another run holds the output lock, or the
        lock file could not be opened.
    """
    discovered = collect_package_dirs(args.source_dir)
    packages = filter_packages(discovered, args.match)
    if not packages:
        logger.warning("No matching *.epub packages found under %s", args.source_dir)

    # Held here rather than inside the exporter so the partial counts survive
    # a Ctrl-C.
    report = Report()
    copies = _plan_copies(args, policy)
    report.ignored = count_ignored(args.source_dir, discovered) - len(copies.named)
    shared = _shared_names(packages, discovered, policy, args.on_collision)
    # The names this run actually uses, computed once. plan_exports would
    # otherwise work them out again from the same inputs.
    assigned = (
        shared
        if shared is not None
        else assign_names(packages, policy, args.on_collision)
    )
    report.orphaned = len(
        find_orphans(
            args.output_dir,
            policy,
            discovered,
            args.on_collision,
            claimed_extra=copies.claimed,
            assigned=shared,
        )
    )

    if args.force and args.max_export_files and len(packages) > args.max_export_files:
        logger.warning(
            "--force selected %d book(s) but -m limits this run to %d; "
            "pass -m 0, or --match to name the books you mean.",
            len(packages),
            args.max_export_files,
        )

    options = ExportOptions(
        covers=args.covers,
        min_free_mb=args.min_free,
        validation=ValidationOptions(enabled=args.validate, epubcheck=args.epubcheck),
        plan=_plan_options(args),
        annotations=(
            index_annotations(found)
            if found is not None and args.annotations_embedded and not args.dry_run
            else None
        ),
    )

    # A dry run writes nothing, so it needs no lock and must not create one.
    lock = nullcontext(False) if args.dry_run else output_lock(args.output_dir)
    with lock as locked:
        # Only with real exclusivity. Unlocked, another run's in-flight
        # temporary looks exactly like an abandoned one, and deleting it makes
        # that run's closing replace fail.
        if locked and not args.dry_run:
            sweep_partials(args.output_dir)

        # Planned exactly once, and inside the lock. Both the work list and
        # the count of what is left come from this one plan, so they cannot
        # describe different libraries; planning outside the lock would let a
        # concurrent run move the output directory underneath the decisions.
        pending_before = 0
        try:
            if not args.dry_run:
                copy_through_all(
                    copies,
                    args.output_dir,
                    report,
                    max_workers=args.workers,
                    min_free_mb=args.min_free,
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
            # Stopping is a normal way to end a long run: every finished book
            # is already complete and atomically in place, so a rerun simply
            # continues.
            report.interrupted = True
            logger.warning(
                "Interrupted; %d book(s) exported before stopping.", report.exported
            )

    return report, max(0, pending_before - report.exported), assigned


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
        return _run_verify(args)
    return None


def _check_environment(args: argparse.Namespace) -> int | None:
    """
    Check what the run needs from the machine, before it does anything.

    Kept out of argparse deliberately. ``parser.error`` always exits 2, so
    validating the environment there made a missing library, a missing extra
    and a typo'd flag indistinguishable to a script.

    :param args: Parsed command line arguments.

    :return: An exit code, or None when the environment is usable.
    """
    # A vault names its notes the way the shelf names its books, so writing
    # one needs the library even though -ao otherwise does not. Without this
    # the run reported "Wrote 0 note(s)" and exited 0, having written none.
    #
    # An independent reason rather than an exception to the convert-nothing
    # modes: written as one, adding --library-export to the same command
    # cancelled it and the empty vault came back.
    writes_a_vault = vault_of(args) is not None
    converts = not (args.verify or args.annotations_only or args.library_export)
    if (converts or writes_a_vault) and not args.source_dir.is_dir():
        if args.source_auto:
            # Both known homes were probed and neither held books. Naming only
            # the fallback reads as "this one path is wrong" rather than "we
            # looked in these places, and here is what to do about it".
            probed = "\n".join(f"  {path}" for path in SOURCE_CANDIDATES)
            logger.critical(
                "No Apple Books library found. Looked in:\n%s\n"
                "If your books are somewhere else, pass -s DIR.",
                probed,
            )
        else:
            logger.critical("Source directory does not exist: %s", args.source_dir)
        return exits.NO_SOURCE

    if args.epubcheck and not epubcheck_available():
        logger.critical(
            "--epubcheck needs the 'epubcheck' tool on PATH "
            "(brew install epubcheck, or see w3c.github.io/epubcheck)"
        )
        return exits.MISSING_TOOL

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

    unusable = _check_environment(args)
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
        report, remaining, named = _run_export(args, policy, found)
    except OutputLockedError as exc:
        logger.critical("%s", exc)
        return exc.exit_code

    # After the books are on the shelf, so annotations reach them by the same
    # path --annotations-refresh uses. A dry run writes nothing, here included.
    annotated = annotations_after_export(args, policy, named, found)
    summary = format_summary(report, args.output_dir, args.dry_run, remaining)
    # Standard output belongs to the document when one is going there; a
    # summary in the middle of it would make the JSON unparsable, which is the
    # one thing a pipe cannot tolerate.
    print(summary, file=sys.stderr if args.annotations_detached == STDOUT else None)
    # Recorded in the log file only: the console already has it from the
    # print above, and logging it plainly printed every run's summary twice.
    app_logger.file_only(summary)
    logger.debug("Run finished: %d exported, %d failed", report.exported, report.failed)

    return annotated if annotated is not None else exit_code(report)
