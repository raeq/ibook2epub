"""
Driving one run from the command line.

Everything between parsing arguments and returning an exit code: what a run
announces before it starts, the read-only ``--list`` and ``--verify`` branches,
and the export itself under the output directory lock.

Held apart from :mod:`epubconvert.convert` so the exporter can be used as a
library without argparse in the call chain.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from contextlib import nullcontext

from . import app_logger
from .app_logger import logger
from .archive import collect_package_dirs
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
from .inspect_output import verify_output
from .naming import STRIP, NamingPolicy, PortableNamesUnavailableError, build_policy
from .planning import PlanOptions, plan_exports, render_listing
from .validate import ValidationOptions


def _log_preamble(args: argparse.Namespace, policy: NamingPolicy) -> None:
    """
    Say what this run is about to do, before it does it.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    """
    logger.debug("Naming policy: %s", policy.label)
    if args.source_auto:
        logger.info("Using discovered iBooks library: %s", args.source_dir)
    else:
        logger.info("Examining source: %s", args.source_dir)
    logger.info("Writing output to: %s", args.output_dir)
    if args.portable_names == STRIP:
        logger.info("Portable naming: stripping characters other filesystems reject.")
    elif args.portable_names:
        logger.info(
            "Portable naming: romanizing (non-Latin titles are transliterated)."
        )
    if args.dry_run:
        logger.info(
            "Running in dry-run mode. No file system modifications will be performed."
        )


def _run_listing(args: argparse.Namespace, policy: NamingPolicy) -> int:
    """
    Render the plan without converting anything.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: A process exit code.
    """
    packages = filter_packages(collect_package_dirs(args.source_dir), args.match)
    decisions = plan_exports(packages, args.output_dir, policy, _plan_options(args))
    print(render_listing(decisions, args.as_json))
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
        return 2

    checked, damaged = verify_output(args.output_dir, epubcheck=args.epubcheck)
    if not checked:
        print(f"No archives found in {args.output_dir}.")
        return 0
    print(f"Verified {checked} archive(s) in {args.output_dir}: {damaged} damaged.")
    if damaged:
        print("Re-export the damaged books with --force.")
    return 1 if damaged else 0


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


def _run_export(args: argparse.Namespace, policy: NamingPolicy) -> tuple[Report, int]:
    """
    Collect, select and export, under the output directory lock.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: The run's report, and the number of books still to convert.

    :raises OutputLockedError: If another run holds the output lock.
    """
    packages = filter_packages(collect_package_dirs(args.source_dir), args.match)
    if not packages:
        logger.warning("No matching *.epub packages found under %s", args.source_dir)

    options = ExportOptions(
        covers=args.covers,
        min_free_mb=args.min_free,
        validation=ValidationOptions(enabled=args.validate, epubcheck=args.epubcheck),
        plan=_plan_options(args),
    )

    # Held here rather than inside the exporter so the partial counts survive
    # a Ctrl-C.
    report = Report()

    # A dry run writes nothing, so it needs no lock and must not create one.
    lock = nullcontext() if args.dry_run else output_lock(args.output_dir)
    with lock:
        if not args.dry_run:
            sweep_partials(args.output_dir)

        # Planned exactly once, and inside the lock. Both the work list and
        # the count of what is left come from this one plan, so they cannot
        # describe different libraries; planning outside the lock would let a
        # concurrent run move the output directory underneath the decisions.
        decisions = plan_exports(packages, args.output_dir, policy, options.plan)
        pending_before = count_pending_decisions(decisions)
        selected = cap_exports(
            decisions, args.max_export_files, randomise=not args.no_shuffle
        )

        try:
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

    return report, max(0, pending_before - report.exported)


def main(argv: Sequence[str] | None = None) -> int:
    """
    Run the conversion from the command line.

    :param argv: Argument list, defaulting to ``sys.argv[1:]``.

    :return: A process exit code; non-zero if any export failed.
    """
    args = parse_args(argv)

    verbosity = 0 if args.quiet else 1 + args.verbose
    app_logger.configure(verbosity=verbosity, log_file=args.log_file)

    try:
        policy = build_policy(args.portable_names)
    except PortableNamesUnavailableError as exc:
        logger.critical("%s", exc)
        return 2

    _log_preamble(args, policy)

    # --list and --verify only read. Creating the destination for them would
    # turn a typo in -o into a stray directory instead of a report about the
    # one that was meant.
    if args.list_only:
        return _run_listing(args, policy)

    if args.verify:
        return _run_verify(args)

    if not args.dry_run:
        try:
            args.output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.critical("Could not create output directory: %s", exc)
            return 1

    try:
        report, remaining = _run_export(args, policy)
    except OutputLockedError as exc:
        logger.critical("%s", exc)
        return 3

    print(format_summary(report, args.output_dir, args.dry_run, remaining))
    logger.debug("Run finished: %d exported, %d failed", report.exported, report.failed)

    return exit_code(report)
