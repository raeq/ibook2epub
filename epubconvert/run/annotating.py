"""
Putting the reader's annotations where a run asked for them.

Embedded in the books on the shelf, detached into a file or a vault, or both,
and the runs that read only Apple's container and convert nothing. Split from
:mod:`epubconvert.run.run` when that module reached the line limit; ``main``
still decides which of these a run does.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..collect.annotations import STDOUT
from ..collect.annotations import collect as collect_annotations
from ..collect.annotations import for_book as annotations_for_book
from ..collect.coredata import ContainerUnavailableError
from ..collect.validate import UNREADABLE_MEMBER, ArchiveInvalidError
from ..export.archive import (
    NoRoomError,
    collect_copyable,
    collect_package_dirs,
    index_by_package,
    replace_annotations,
)
from ..export.detached import library_export, library_refusal, vault_of, write_export
from ..utils import exits
from ..utils.app_logger import logger
from ..utils.display import printable
from ..utils.policy import Assignment, NamingPolicy
from .convert import OutputLockedError, output_lock, progress_for
from .planning import assign_names


def gather_annotations(
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


def annotations_after_export(
    args: argparse.Namespace,
    named: Sequence[Assignment],
    found: list[dict[str, Any]] | None,
    *,
    copyable: Sequence[Path],
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
    :param named: The names the export just used, so the refresh looks for the
        archives the export actually wrote.
    :param found: The annotations this run read, or None.
    :param copyable: The library's already-zipped books and PDFs, the other
        half of telling a highlight's book apart; see
        :func:`~epubconvert.export.archive.index_by_package`.

    :return: An exit code when something went wrong, None otherwise.
    """
    if args.dry_run:
        return None
    code = exits.SUCCESS
    if args.annotations_refresh and found is not None:
        code = _embed_in_shelf(args, found, True, named, copyable)
    if code == exits.SUCCESS and args.annotations_detached and found is not None:
        code = write_export(
            args, found, args.annotations_detached, named, copyable=copyable
        )
    if args.annotations_embedded and not args.annotations_detached and found:
        _warn_about_stranded(args, found, named, copyable)
    return None if code == exits.SUCCESS else code


def _warn_about_stranded(
    args: argparse.Namespace,
    found: list[dict[str, Any]],
    named: Sequence[Assignment],
    copyable: Sequence[Path],
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
    :param copyable: The library's already-zipped books and PDFs.
    """
    # Quiet: the conversion before this read the same annotations against
    # the same library and has already said which it could not place.
    index = index_by_package(
        found, [item.package for item in named], copyable=copyable, quiet=True
    )
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
    # The copyable files are wanted for the same reason: only a vault matches
    # highlights to books, and a zipped book shares its name with a package.
    markdown = args.annotations_format == "markdown"
    named = _named(args, policy) if markdown else []
    copyable = collect_copyable(args.source_dir) if markdown else []
    return write_export(args, found, args.annotations_only, named, copyable=copyable)


def apply_annotations(
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
    # through annotations_after_export and not on the -ar route, so
    # "--dry-run -ae -ar" rewrote every archive on the shelf.
    if args.dry_run:
        logger.info("Dry run: annotations were read but nothing was written.")
        return exits.SUCCESS

    try:
        found = gather_annotations(args, policy, required=not converted)
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
    # Walked whether or not a conversion would copy them: a zipped book in the
    # library shares its name with a package either way.
    copyable = collect_copyable(args.source_dir)

    code = exits.SUCCESS
    if args.annotations_embedded:
        code = _embed_in_shelf(args, found, converted, assignments, copyable)
        # A book the refresh could not rebuild is that book's failure, and the
        # detached file is somewhere else, so it is still written. Anything
        # else -- no shelf, the lock held -- stops here as it always did.
        if code not in (exits.SUCCESS, exits.FAILED):
            return code

    if args.annotations_detached:
        written = write_export(
            args, found, args.annotations_detached, assignments, copyable=copyable
        )
        # A failed book outranks the destination's own error: the books on
        # the shelf are what the run is for, and the error is logged anyway.
        return code if code != exits.SUCCESS else written
    return code


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
    found: list[dict[str, Any]],
    converted: bool,
    assignments: Sequence[Assignment],
    copyable: Sequence[Path],
) -> int:
    """
    Put each book's annotations inside the archive already on the shelf.

    :param args: Parsed command line arguments.
    :param found: Every annotation read from Apple.
    :param converted: Whether this run also converted books.
    :param assignments: The names every package was given.
    :param copyable: The library's already-zipped books and PDFs, which
        answer to a package's name too.

    :return: A process exit code.
    """
    # A glob over a missing directory yields nothing, which read as a clean
    # run over an empty shelf: -ar with a typo in -o said it had refreshed
    # every book it found, having looked at none.
    if not args.output_dir.is_dir():
        logger.critical("Output directory does not exist: %s", args.output_dir)
        return exits.NO_OUTPUT

    # Quiet after a conversion, which embedded from the same index and has
    # already said which annotations it could not place.
    index = index_by_package(
        found,
        [item.package for item in assignments],
        copyable=copyable,
        quiet=converted,
    )

    # The same lock the export takes. These writes go into the output
    # directory and leave partials there, and a concurrent run's sweep cannot
    # tell one of those from an abandoned one. Refused on this route, the
    # error escaped main -- which maps it only around the export -- as a
    # traceback and exit 1.
    try:
        with output_lock(args.output_dir):
            changed, failed, stopped = _refresh_each(args, assignments, index)
    except OutputLockedError as exc:
        logger.critical("%s", exc)
        return exc.exit_code

    # Every book it could not refresh is a failure, and so is a refresh the
    # floor stopped, as for a conversion. Both were logged and the run exited
    # 0, so a scheduled refresh that hit ENOSPC on every book reported success.
    parts = [f"Refreshed annotations in {changed} book(s)"]
    if failed:
        parts.append(f"could not refresh {failed}")
    if stopped:
        parts.append("stopped at the --min-free floor before the rest")
    if not converted:
        parts.append("converted nothing")
    summary = "; ".join(parts) + "."
    if failed or stopped:
        logger.error("%s", summary)
        return exits.FAILED
    logger.info("%s", summary)
    return exits.SUCCESS


def _refresh_each(
    args: argparse.Namespace,
    assignments: Sequence[Assignment],
    index: dict[str, list[dict[str, Any]]],
) -> tuple[int, int, bool]:
    """
    Rebuild every archive on the shelf whose annotations changed.

    Each rebuild writes a whole copy of the book beside the original, so it
    answers to ``--min-free`` as a conversion does, through the conversions'
    own sticky sampler. It never did: a refresh went on rebuilding onto a
    volume already below the floor, which is the SD card or Kindle the floor
    exists for.

    :param args: Parsed command line arguments.
    :param assignments: The names every package was given.
    :param index: The annotations, by book.

    :return: How many archives were rewritten, how many could not be, and
        whether the floor stopped the refresh before the rest.
    """
    changed = failed = 0
    # An interval of one: rebuilds run one at a time, so every one is
    # measured, one statvfs per book that is actually rewritten.
    progress = progress_for(len(assignments), 1)

    def room() -> bool:
        return progress.has_room(args.output_dir, args.min_free)

    for item in assignments:
        marker = progress.tick()
        target = args.output_dir / item.filename if item.filename else None
        if target is None or not target.is_file():
            continue
        mine = annotations_for_book(item.package.name, index)
        if not mine:
            continue
        try:
            if replace_annotations(target, mine, room=room):
                changed += 1
                logger.info(
                    "%s Refreshed %d annotation(s) in %s",
                    marker,
                    len(mine),
                    printable(target.name),
                )
        except NoRoomError:
            # Sticky, like the conversions' floor: the volume does not get
            # emptier by asking again, so every book after this one stops too.
            return changed, failed, True
        except UNREADABLE_MEMBER + (ArchiveInvalidError,) as exc:
            # BadZipFile is not an OSError, so one damaged archive used to
            # abort the whole refresh and every book after it went untouched;
            # nor is what a damaged compressed stream raises, which did the
            # same until #21. A damaged archive is an expected state: --verify
            # exists to find them. Counted as well as logged, so the run's
            # exit code says a book was left behind.
            failed += 1
            logger.error("Could not refresh %s: %s", printable(target.name), exc)
    return changed, failed, False


def run_container_only(args: argparse.Namespace, policy: NamingPolicy) -> int:
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
