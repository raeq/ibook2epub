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
import shlex
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path

from . import app_logger, exits
from .app_logger import logger
from .archive import collect_package_dirs, count_ignored
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
from .defaults import SOURCE_CANDIDATES
from .inspect_output import verify_output
from .naming import (
    NamingPolicy,
    PortableNamesUnavailableError,
    PortableNaming,
    StripNaming,
    build_policy,
)
from .planning import PlanOptions, plan_exports, render_listing
from .validate import ValidationOptions, epubcheck_available


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


def _run_listing(args: argparse.Namespace, policy: NamingPolicy) -> int:
    """
    Render the plan without converting anything.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: A process exit code.
    """
    discovered = collect_package_dirs(args.source_dir)
    packages = filter_packages(discovered, args.match)
    decisions = plan_exports(packages, args.output_dir, policy, _plan_options(args))
    print(render_listing(decisions, args.as_json))
    ignored = count_ignored(args.source_dir, discovered)
    if ignored and not args.as_json:
        print(f"{ignored} ignored (not *.epub/ packages)")
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


def _run_export(args: argparse.Namespace, policy: NamingPolicy) -> tuple[Report, int]:
    """
    Collect, select and export, under the output directory lock.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: The run's report, and the number of books still to convert.

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
    report.ignored = count_ignored(args.source_dir, discovered)

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
            # Planning is inside the guard too: under --skip-incomplete it
            # walks every package in the library, which is minutes of work on
            # a cloud shelf, and a Ctrl-C there produced a raw traceback with
            # no summary and no 130.
            decisions = plan_exports(packages, args.output_dir, policy, options.plan)
            pending_before = count_pending_decisions(decisions)
            selected = cap_exports(
                decisions, args.max_export_files, randomise=not args.no_shuffle
            )
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

    return report, max(0, pending_before - report.exported)


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
    if not args.verify and not args.source_dir.is_dir():
        if args.source_auto:
            # Both known homes were probed and neither held books. Naming only
            # the fallback reads as "this one path is wrong" rather than "we
            # looked in these places, and here is what to do about it".
            probed = "\n  ".join(f"  {path}" for path in SOURCE_CANDIDATES)
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

    :return: A process exit code. 0 success; 1 an export failed, the output
        directory could not be created, or --verify found damage; 2 portable
        naming is unavailable or --verify was given a directory that is not
        there; 3 another run holds the output lock; 130 interrupted.
    """
    args = parse_args(argv)

    verbosity = 0 if args.quiet else 1 + args.verbose
    app_logger.configure(verbosity=verbosity, log_file=args.log_file)

    unusable = _check_environment(args)
    if unusable is not None:
        return unusable

    try:
        policy = build_policy(args.portable_names)
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

    try:
        report, remaining = _run_export(args, policy)
    except OutputLockedError as exc:
        logger.critical("%s", exc)
        return exits.LOCKED if "already using" in str(exc) else exits.NO_OUTPUT

    summary = format_summary(report, args.output_dir, args.dry_run, remaining)
    print(summary)
    # Recorded in the log file only: the console already has it from the
    # print above, and logging it plainly printed every run's summary twice.
    app_logger.file_only(summary)
    logger.debug("Run finished: %d exported, %d failed", report.exported, report.failed)

    return exit_code(report)
