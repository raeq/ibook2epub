"""
Driving one run from the command line.

Everything between parsing arguments and returning an exit code: what a run
announces before it starts, the read-only ``--list`` and ``--verify`` branches,
and the export itself under the output directory lock.

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
from ..collect.annotations import collect as collect_annotations
from ..collect.annotations import for_book as annotations_for_book
from ..collect.annotations import index_by_book as index_annotations
from ..collect.coredata import ContainerUnavailableError
from ..collect.validate import (
    UNREADABLE_MEMBER,
    ArchiveInvalidError,
    ValidationOptions,
    epubcheck_available,
)
from ..export.archive import (
    collect_copyable,
    collect_package_dirs,
    count_ignored,
    replace_annotations,
)
from ..export.detached import library_export, library_refusal, vault_of, write_export
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
from ..utils.display import printable
from ..utils.policy import Assignment, NamingPolicy
from .cli import parse_args
from .convert import (
    CopyPlan,
    ExportOptions,
    OutputLockedError,
    Report,
    cap_exports,
    copy_through_all,
    count_pending_decisions,
    exit_code,
    export_planned,
    filter_packages,
    format_summary,
    output_lock,
    plan_copies,
    progress_for,
    sweep_partials,
)
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


def _gather_annotations(
    args: argparse.Namespace, policy: NamingPolicy, *, required: bool = False
) -> list[dict[str, Any]] | None:
    """
    Read the reader's highlights, if this run wants any.

    :param args: Parsed command line arguments.
    :param policy: The naming policy, so each book can say what its file on
        the shelf is called.
    :param required: Whether the highlights are the whole run. Then why they
        could not be read is its outcome, so the failure is raised for the
        caller to turn into an exit code rather than logged and passed over.

    :return: The annotations, or None when the run asked for none or they
        could not be read. A failure here does not stop a conversion: the books
        are the point, and the highlights are an extra.

    :raises ContainerUnavailableError: If they could not be read and
        *required* is set.
    """
    if not (args.annotations_embedded or args.annotations_detached):
        return None
    try:
        return collect_annotations(policy=policy)
    except ContainerUnavailableError as exc:
        if required:
            raise
        logger.error("Could not read annotations: %s", exc)
        return None


def _annotations_after_export(
    args: argparse.Namespace,
    policy: NamingPolicy,
    named: Sequence[Assignment],
    found: list[dict[str, Any]] | None,
) -> int | None:
    """
    Finish the annotation work the conversion could not do itself.

    A book converted by this run already carries its annotations: they went in
    as the archive was written. Two things are left over.

    Books that were already on the shelf are untouched by a conversion that
    skipped them, so ``-ar`` says to go back over the whole shelf. Without it,
    ``-ae`` means what it says -- the books this run wrote carry their
    highlights -- and a shelf built over several runs is brought up to date by
    asking for it.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    :param named: The names the export just used, so the refresh looks for the
        archives the export actually wrote.
    :param found: The annotations this run read, or None.

    :return: An exit code when something went wrong, None otherwise.
    """
    if args.dry_run:
        return None
    code = exits.SUCCESS
    if args.annotations_refresh and found is not None:
        code = _embed_in_shelf(args, policy, found, True, named)
    if code == exits.SUCCESS and args.annotations_detached and found is not None:
        code = write_export(args, found, args.annotations_detached, named)
    if args.annotations_embedded and not args.annotations_detached and found:
        _warn_about_stranded(args, found, named)
    return None if code == exits.SUCCESS else code


def _warn_about_stranded(
    args: argparse.Namespace,
    found: list[dict[str, Any]],
    named: Sequence[Assignment],
) -> None:
    """
    Say so when highlights had nowhere to go.

    ``-ae`` puts a book's highlights inside the book, which needs the book to
    be on the shelf. A book that was not converted has no archive to put them
    in, so its highlights are read out of Apple's database and then reach
    nothing at all.

    A DRM-protected book is the permanent case, and the one that matters most.
    Its file cannot be opened, so no rerun will ever produce an archive to
    embed into -- and it is exactly the book the reader cannot take with them,
    which makes the highlights the only part they can keep. Saying nothing left
    them believing the export had covered everything.

    Not called when ``-ad`` is also in force: those highlights are already in a
    file, so there is nothing to warn about.

    :param args: Parsed command line arguments.
    :param found: Every annotation this run read.
    :param named: The names the export used, which is the only place that knows
        what each book's archive would be called.
    """
    index = index_annotations(found)
    stranded_books: list[str] = []
    stranded = 0
    for item in named:
        target = args.output_dir / item.filename if item.filename else None
        if target is not None and target.is_file():
            continue
        mine = annotations_for_book(item.package.name, index)
        if mine:
            stranded_books.append(item.package.name)
            stranded += len(mine)

    if not stranded:
        return

    shown = ", ".join(printable(name) for name in sorted(stranded_books)[:3])
    if len(stranded_books) > 3:
        shown += f", and {len(stranded_books) - 3} more"
    logger.warning(
        "%d annotation(s) from %d book(s) reached no file: %s. Those books are "
        "not on the shelf, so there was nothing to embed them in -- a "
        "DRM-protected book can never be converted, and its highlights are the "
        "only part of it you can keep. Run again with --annotations-detached "
        "FILE to write them to a file of their own, or --annotations-only FILE "
        "to do that without converting anything.",
        stranded,
        len(stranded_books),
        shown,
    )


def _annotations_only(args: argparse.Namespace, policy: NamingPolicy) -> int:
    """
    Write the detached file and stop.

    Reads Apple's container and nothing else: no library walk, no shelf, no
    output directory. Somebody who wants their highlights out should not have
    to convert a library to get them.

    :param args: Parsed command line arguments.
    :param policy: The naming policy, so each book names the file it will be
        found in rather than the one it came from.

    :return: A process exit code.
    """
    try:
        found = collect_annotations(policy=policy)
    except ContainerUnavailableError as exc:
        logger.critical("Could not read annotations: %s", exc)
        return exc.exit_code
    if args.dry_run:
        # Guarded here, where the write is decided, rather than at the call
        # site: this route composes with --library-export, whose dry run was
        # honoured while this one went on to write the file.
        logger.info("Dry run: %d annotation(s) read; nothing was written.", len(found))
        return exits.SUCCESS
    # -ao reads Apple's container and nothing else, but a note's filename comes
    # from the naming policy, so the library still has to be named. Naming is
    # cheap under the default policy and only reached for markdown.
    named = _named(args, policy) if args.annotations_format == "markdown" else []
    return write_export(args, found, args.annotations_only, named)


def _apply_annotations(
    args: argparse.Namespace,
    policy: NamingPolicy,
    *,
    converted: bool = False,
    named: Sequence[Assignment] | None = None,
) -> int:
    """
    Put the reader's annotations wherever this run asked for them.

    Applied to the shelf after a conversion rather than threaded through it, so
    that ``-ar`` -- which converts nothing -- and an ordinary run reach the
    books by exactly the same path. There is one place that decides what a book
    carries.

    Only archives already on the shelf are touched. Under ``-ar`` a book added
    to the library since the conversion is therefore not converted: that mode
    says what it does.

    :param args: Parsed command line arguments.
    :param policy: The naming policy, so a shelf name is worked out the way the
        conversion worked it out.
    :param converted: Whether books were put on the shelf by this run, which
        only changes what is said afterwards.
    :param named: The names the export already worked out, when there was one.
        Recomputing them re-parses every package document a second time under
        a metadata naming policy, which is the 2x read this project has
        already fixed once elsewhere.

    :return: A process exit code.
    """
    # Guarded here rather than at the call sites. It was checked on the route
    # through _annotations_after_export and not on the -ar route, so
    # "--dry-run -ae -ar" rewrote every archive on the shelf.
    if args.dry_run:
        logger.info("Dry run: annotations were read but nothing was written.")
        return exits.SUCCESS

    try:
        found = _gather_annotations(args, policy, required=not converted)
    except ContainerUnavailableError as exc:
        # Nothing was converted, so the highlights were the whole run and why
        # they could not be read is its outcome: 4 for a missing library, 8
        # for a refusal (#19). This route only ever saw None before.
        logger.error("Could not read annotations: %s", exc)
        return exc.exit_code
    if found is None:
        # The books are the point and they are already on the shelf. Reporting
        # NO_SOURCE here told a scheduled run the source directory was missing
        # when it had been found and used.
        return exits.SUCCESS if converted else exits.NO_SOURCE

    # Named once, here, and passed to everything that needs it. Under a
    # metadata policy naming re-parses every package document, and computing it
    # in two places is the 2x read this project has already fixed twice.
    assignments = list(named) if named is not None else _named(args, policy)

    if args.annotations_embedded:
        code = _embed_in_shelf(args, policy, found, converted, assignments)
        if code != exits.SUCCESS:
            return code

    if args.annotations_detached:
        return write_export(args, found, args.annotations_detached, assignments)
    return exits.SUCCESS


def _named(args: argparse.Namespace, policy: NamingPolicy) -> list[Assignment]:
    """
    Name every book in the library, once.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: One assignment per package.
    """
    return assign_names(
        collect_package_dirs(args.source_dir), policy, args.on_collision
    )


def _embed_in_shelf(
    args: argparse.Namespace,
    policy: NamingPolicy,
    found: list[dict[str, Any]],
    converted: bool,
    named: Sequence[Assignment] | None,
) -> int:
    """
    Put each book's annotations inside the archive already on the shelf.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    :param found: Every annotation read from Apple.
    :param converted: Whether this run also converted books.
    :param named: The names the export worked out, or None to work them out.

    :return: A process exit code.
    """
    # A glob over a missing directory yields nothing, which read as a clean
    # run over an empty shelf: -ar with a typo in -o said it had refreshed
    # every book it found, having looked at none.
    if not args.output_dir.is_dir():
        logger.critical("Output directory does not exist: %s", args.output_dir)
        return exits.NO_OUTPUT

    assignments = list(named) if named is not None else _named(args, policy)
    index = index_annotations(found)
    ambiguous = _ambiguous_names(assignments)

    changed = 0
    progress = progress_for(len(assignments), 1)
    # The same lock the export takes. These writes go into the output
    # directory and leave partials there, and a concurrent run's sweep cannot
    # tell one of those from an abandoned one.
    with output_lock(args.output_dir):
        for item in assignments:
            marker = progress.tick()
            target = args.output_dir / item.filename if item.filename else None
            if target is None or not target.is_file():
                continue
            if item.package.name in ambiguous:
                logger.warning(
                    "Skipped annotations for %s: more than one package "
                    "directory has that name, so which book they belong to "
                    "cannot be told apart.",
                    printable(item.package.name),
                )
                continue
            mine = annotations_for_book(item.package.name, index)
            if not mine:
                continue
            try:
                if replace_annotations(target, mine):
                    changed += 1
                    logger.info(
                        "%s Refreshed %d annotation(s) in %s",
                        marker,
                        len(mine),
                        printable(target.name),
                    )
            except UNREADABLE_MEMBER + (ArchiveInvalidError,) as exc:
                # BadZipFile is not an OSError, so one damaged archive used to
                # abort the whole refresh and every book after it went
                # untouched; nor is what a damaged compressed stream raises,
                # which did the same until #21. A damaged archive is an
                # expected state: --verify exists to find them.
                logger.error("Could not refresh %s: %s", printable(target.name), exc)
    if converted:
        logger.info("Refreshed annotations in %d book(s).", changed)
    else:
        logger.info("Refreshed annotations in %d book(s); converted nothing.", changed)
    return exits.SUCCESS


def _ambiguous_names(assignments: Sequence[Assignment]) -> set[str]:
    """
    Find package names that more than one directory answers to.

    An annotation records its book's package *name*, not its path, because the
    path runs through the reader's home directory. Two directories with the
    same name in different places are therefore indistinguishable to
    :func:`~epubconvert.collect.annotations.for_book`, and embedding by name gave each
    of them the other's highlights. This is the only place that knows every
    path, so it is the place that settles it.

    :param assignments: Every book this run knows about.

    :return: The names that are not unique.
    """
    seen: dict[str, Path] = {}
    ambiguous: set[str] = set()
    for item in assignments:
        name = item.package.name
        if name in seen and seen[name] != item.package:
            ambiguous.add(name)
        seen.setdefault(name, item.package)
    return ambiguous


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
                    copies, args.output_dir, report, max_workers=args.workers
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


def _run_container_only(args: argparse.Namespace, policy: NamingPolicy) -> int:
    """
    Write whatever a run that reads only Apple's container was asked for.

    The two compose: the reader who wants their catalogue out is the reader
    who wants their highlights out, and both come from the same container.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: A process exit code.
    """
    # Both destinations are judged before either is written. A composed run
    # that wrote one file and was then refused the other left the reader with
    # half an answer and, worse, something a retry tripped over.
    unwritable = _unwritable_destination(args)
    if unwritable is not None:
        logger.critical("%s", unwritable)
        if args.annotations_only:
            # The highlights merge and would have been safe to rerun, so a
            # reader repeating the README's composed command sees only the
            # catalogue's refusal and no sign that the rest was skipped too.
            logger.error(
                "Your highlights were not written either, because both files "
                "are judged before either is written. Pass --force to replace "
                "the library export, or write the two separately."
            )
        return exits.NO_OUTPUT
    # The highlights first: their export merges into its file, so a refusal
    # afterwards costs a rerun rather than a file.
    if args.annotations_only:
        code = _annotations_only(args, policy)
        if code != exits.SUCCESS:
            if args.library_export:
                # Skipped rather than written, so a retry has nothing to
                # refuse: the catalogue does not merge, and one left behind
                # would need --force next time. Said rather than silent,
                # because a per-note vault failure is stable -- a note the
                # reader edited whose sidecar is itself foreign is blocked on
                # every run -- and the catalogue then never appeared at all
                # with nothing said about why.
                logger.error(
                    "The library was not exported, because the highlights "
                    "above could not be written. Fix that, or run "
                    "--library-export on its own."
                )
            return code
        if not args.library_export:
            return code
    return library_export(args, policy)


def _unwritable_destination(args: argparse.Namespace) -> str | None:
    """
    Judge where a convert-nothing run would write, before it writes anything.

    Only the library export is judged here. The annotation export's own check
    reads the file back to merge into it, which is the read it exists for and
    not a check that can be lifted out of it; but it is written first, and it
    merges, so a refusal after it costs a rerun rather than a file.

    :param args: Parsed command line arguments.

    :return: Why the run cannot write, or None when it can.
    """
    if not args.library_export or args.library_export == STDOUT or args.dry_run:
        return None
    # The vault is made by the run that is about to write it, so a catalogue
    # inside one is not homeless. Judged before that happens, it looked it.
    vault = vault_of(args)
    return library_refusal(
        Path(args.library_export),
        force=args.force,
        pending=() if vault is None else (vault,),
    )


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
        return _run_container_only(args, policy)
    if args.annotations_refresh:
        return _apply_annotations(args, policy)  # -ar converts nothing
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
    found = _gather_annotations(args, policy)

    try:
        report, remaining, named = _run_export(args, policy, found)
    except OutputLockedError as exc:
        logger.critical("%s", exc)
        return exc.exit_code

    # After the books are on the shelf, so annotations reach them by the same
    # path --annotations-refresh uses. A dry run writes nothing, here included.
    annotated = _annotations_after_export(args, policy, named, found)
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
