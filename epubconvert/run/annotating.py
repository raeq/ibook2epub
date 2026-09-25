"""
Putting the reader's annotations where a run asked for them.

Embedded in the books on the shelf, detached into a file or a vault, or both,
and the runs that read only Apple's container and convert nothing. Split from
:mod:`epubconvert.run.run` when that module reached the line limit; ``main``
still decides which of these a run does.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Collection, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..collect.annotations import STDOUT
from ..collect.annotations import collect as collect_annotations
from ..collect.annotations import for_book as annotations_for_book
from ..collect.coredata import ContainerUnavailableError
from ..collect.package import UNREADABLE_MEMBER
from ..collect.validate import ArchiveInvalidError
from ..export.archive import (
    NoRoomError,
    collect_copyable,
    collect_package_dirs,
    index_by_package,
    replace_annotations,
)
from ..export.detached import (
    LIBRARY_SKIPPED,
    library_export,
    library_refusal,
    vault_of,
    write_export,
)
from ..export.naming import filesystem_key
from ..utils import exits
from ..utils.app_logger import logger
from ..utils.display import printable
from ..utils.policy import Assignment, NamingPolicy
from .claims import NUMBERED, shelf_names
from .convert import OutputLockedError, output_lock, progress_for
from .copying import plan_copies
from .copynames import Names, claim_copies
from .holders import Unopened
from .placing import placed, settled
from .planning import assign_names
from .preflight import ShelfUnwritableError, check_writable
from .stranded import warn_about_bookless, warn_about_copies, warn_about_stranded


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
        return _read_annotations(args, policy)
    except ContainerUnavailableError as exc:
        if required:
            raise
        logger.error("Could not read annotations: %s", printable(str(exc)))
        return None


def annotations_after_export(
    args: argparse.Namespace,
    policy: NamingPolicy,
    named: Sequence[Assignment],
    found: list[dict[str, Any]] | None,
    *,
    copyable: Sequence[Path],
    held_back: Collection[Path] = frozenset(),
    stopped: Collection[Path] = frozenset(),
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
    :param policy: The naming policy, so a book's archive is found the way the
        plan finds it.
    :param named: The names the export just used, the files it copied
        through included, so a vault writes a note for each.
    :param found: The annotations this run read, or None.
    :param copyable: The library's already-zipped books and PDFs, the other
        half of telling a highlight's book apart; see
        :func:`~epubconvert.export.archive.index_by_package`.
    :param held_back: The books ``-m`` held back for a later run, which will
        embed their highlights: not converted is not unconvertible.
    :param stopped: The books the ``--min-free`` floor kept from starting,
        which a rerun converts just the same.

    :return: An exit code when something went wrong, None otherwise.
    """
    if args.dry_run:
        _unopened_notes(args)
        return None
    # -ar never reaches here: it is its own route, apply_annotations, which
    # converts nothing.
    code = exits.SUCCESS
    if args.annotations_detached and found is not None:
        code = write_export(
            args, found, args.annotations_detached, named, copyable=copyable
        )
    if args.annotations_embedded and not args.annotations_detached and found:
        # A file copied through is copied as it is, with nothing embedded.
        kept = set(copyable)
        packages = [item for item in named if item.package not in kept]
        warn_about_stranded(
            args,
            policy,
            found,
            packages,
            copyable,
            held_back=held_back,
            stopped=stopped,
        )
        warn_about_copies(
            index_by_package(
                found, [item.package for item in named], copyable=copyable, quiet=True
            ),
            [item.package for item in named if item.package in kept],
            copied=not args.no_copy_through,
        )
        warn_about_bookless(found)
    return None if code == exits.SUCCESS else code


def _unopened_notes(args: argparse.Namespace) -> None:
    """
    Say that a dry run leaves a vault's notes unopened, if it writes a vault.

    What the real run finds in a note -- a file ibook2epub did not write at
    its name, one it cannot read, an edited sidecar -- leaves that book's
    highlights unwritten and exits 1. A dry run opens none of them, so it
    exited 0 without a word about any of it.

    :param args: Parsed command line arguments.
    """
    vault = vault_of(args)
    if args.dry_run and vault is not None:
        logger.info(
            "Dry run: the notes in %s were not opened, so a note the real run "
            "would leave alone, and exit 1 for, is not named here.",
            printable(str(vault)),
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
        found = _read_annotations(args, policy)
    except ContainerUnavailableError as exc:
        logger.critical("Could not read annotations: %s", printable(str(exc)))
        return exc.exit_code
    if args.dry_run:
        # Guarded here, where the write is decided, rather than at the call
        # site: this route composes with --library-export, whose dry run was
        # honoured while this one went on to write the file.
        logger.info("Dry run: %d annotation(s) read; nothing was written.", len(found))
        _unopened_notes(args)
        return exits.SUCCESS
    # -ao reads Apple's container and nothing else, but a note's filename comes
    # from the naming policy, so the library still has to be named. Naming is
    # cheap under the default policy and only reached for markdown.
    # The copyable files are wanted for the same reason: only a vault matches
    # highlights to books, and a zipped book shares its name with a package.
    markdown = args.annotations_format == "markdown"
    copyable = collect_copyable(args.source_dir) if markdown else []
    # Named after the file each book is placed at when there is a shelf to
    # place it on, as -ad names it. Named from the assignment, an edition
    # moved on to its marked name wrote its highlights into the note of the
    # edition that holds the plain name.
    named = (
        _with_copies(
            args,
            policy,
            _named(args, policy),
            copyable,
            # Never raises: a shelf it may not search is placed on as none.
            shelf=os.path.isdir(args.output_dir),  # noqa: PTH112
            highlighted={_source_of(item) for item in found},
        )
        if markdown
        else []
    )
    return write_export(args, found, args.annotations_only, named, copyable=copyable)


def _read_annotations(
    args: argparse.Namespace, policy: NamingPolicy
) -> list[dict[str, Any]]:
    """
    Read the annotations, leaving unopened what ``--skip-incomplete`` says to.

    Each highlighted book's package document is read for what it says of
    the book, which downloaded a package iCloud had evicted. Only under the
    flag are the books left unopened, so every other run reads the
    annotations as it always did.

    :param args: Parsed command line arguments.
    :param policy: The naming policy.

    :return: The annotations.
    """
    if args.skip_incomplete:
        return collect_annotations(policy=policy, unopened=Unopened(packages=True))
    return collect_annotations(policy=policy)


def _source_of(item: dict[str, Any]) -> object:
    """The package name an annotation's book was read from, if it says."""
    book = item.get("book")
    return book.get("source") if isinstance(book, dict) else None


def _with_copies(
    args: argparse.Namespace,
    policy: NamingPolicy,
    assignments: Sequence[Assignment],
    copyable: Sequence[Path],
    *,
    shelf: bool,
    highlighted: Collection[object] | None = None,
) -> list[Assignment]:
    """
    Name the library's books without a package too, for a vault's notes.

    A vault wrote notes for the packages alone, so the highlights of a book
    that arrived already zipped, or as a PDF, reached no file, and the run
    said "Wrote 1 note(s)" and exited 0. Each takes the name it is copied
    through under, claimed after every package's
    (:func:`~epubconvert.run.copynames.claim_copies`).

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    :param assignments: Every package's name.
    :param copyable: The library's already-zipped books and PDFs.
    :param shelf: Whether to place each book on the shelf, so its note is
        named after the file it is found at, as the conversion route names it.
    :param highlighted: The package names that have highlights, when only
        those need the archive under their name read to be placed. Every
        other book is placed by its name alone, so a run that writes three
        notes does not open every archive on a 2,000-book shelf.

    :return: The packages' names, then the other books'.
    """
    names, unopened = _claimed(
        args,
        policy,
        assignments,
        copyable,
        output_dir=args.output_dir if shelf else None,
    )
    everything = [*names.packages, *names.copies]
    if not shelf:
        return everything
    if highlighted is not None:
        everything = _read_only_for(everything, highlighted, policy)
    return settled(everything, args.output_dir, policy, unopened=unopened)


def _read_only_for(
    everything: Sequence[Assignment],
    highlighted: Collection[object],
    policy: NamingPolicy,
) -> list[Assignment]:
    """
    Keep the identifiers that placing the highlighted books depends on.

    A book with highlights is placed by its identifier, and every other book
    by its name alone, so writing three notes does not open every archive on
    a 2,000-book shelf. Except one whose place a highlighted book's depends
    on: a book moving on past another book's archive speaks for the first
    free position of its marked name, so one the run moves on, left at its
    name, left that position to the highlighted book after it, whose note
    was then named after a file the run never writes.

    :param everything: Every book's name, in the order they are placed.
    :param highlighted: The package names that have highlights.
    :param policy: The naming policy in force.

    :return: The names, with no identifier for the books it cannot matter for.
    """
    families = {
        _family(item, policy) for item in everything if item.package.name in highlighted
    }
    return [
        item
        if item.package.name in highlighted
        or (item.marked is not None and _family(item, policy) in families)
        else replace(item, identifier=None)
        for item in everything
    ]


def _family(item: Assignment, policy: NamingPolicy) -> str | None:
    """The name a book moves on through, as a filesystem key less any number."""
    if item.marked is None:
        return None
    key = filesystem_key(policy.identity(item.marked))
    numbered = NUMBERED.fullmatch(key)
    return key if numbered is None else numbered["stem"] + (numbered["extension"] or "")


def _claimed(
    args: argparse.Namespace,
    policy: NamingPolicy,
    assignments: Sequence[Assignment],
    copyable: Sequence[Path],
    *,
    output_dir: Path | None,
) -> tuple[Names, Unopened]:
    """
    Name the library's copies in the claim pass the packages were named in.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    :param assignments: Every package's name.
    :param copyable: The library's already-zipped books and PDFs.
    :param output_dir: The shelf to weigh, or None to name without one.

    :return: The packages' names, some with an identifier read, and the
        copies', as :func:`~epubconvert.run.copynames.claim_copies` gives
        them; and the books not to open to place them, as the run places
        them (run._shared_names).
    """
    copies = plan_copies(
        copyable,
        policy,
        max_workers=args.workers,
        skip_incomplete=args.skip_incomplete,
        # A refresh copies nothing, so an evicted file is left unopened, as
        # under --no-copy-through: named as a copy, every zipped book was
        # opened, and so downloaded, to refresh the other books' highlights.
        copied=not args.annotations_refresh,
    )
    names = claim_copies(
        assignments,
        copies.named,
        policy,
        args.on_collision,
        output_dir=output_dir,
        unopened=copies.unopened,
    )
    return names, copies.unopened


def _shelved(
    args: argparse.Namespace,
    policy: NamingPolicy,
    assignments: Sequence[Assignment],
    copyable: Sequence[Path],
) -> list[Assignment]:
    """
    Name every book as the run names it, to place the packages as it does.

    The packages, with the identifier read of each a copy wanted the name of,
    then the copies, placed on the shelf (run._shared_names). ``-ar`` placed
    the packages alone and read no identifier, so a package named from the
    folder that the run had moved on past a copy's file took that file for
    its own; compared before the write, it then had no archive at all, and
    its highlights were never refreshed.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.
    :param assignments: Every package's name.
    :param copyable: The library's already-zipped books and PDFs.

    :return: The packages' names, then the copies'.
    """
    names, unopened = _claimed(
        args, policy, assignments, copyable, output_dir=args.output_dir
    )
    if not names.copies:
        return names.packages
    copies = settled(
        [*names.packages, *names.copies], args.output_dir, policy, unopened=unopened
    )
    return [*names.packages, *copies[len(names.packages) :]]


def apply_annotations(args: argparse.Namespace, policy: NamingPolicy) -> int:
    """
    Put the reader's annotations into the books already on the shelf: ``-ar``.

    Converts nothing, so a book added to the library since the conversion is
    not converted: that mode says what it does. A conversion embeds as each
    book is written instead, and never comes here; the ``converted`` and
    ``named`` parameters that served it were never passed, and are gone.

    :param args: Parsed command line arguments.
    :param policy: The naming policy, so a shelf name is worked out the way the
        conversion worked it out.

    :return: A process exit code.
    """
    try:
        found = gather_annotations(args, policy, required=True)
    except ContainerUnavailableError as exc:
        # Nothing was converted, so the highlights were the whole run and why
        # they could not be read is its outcome: 4 for a missing library, 8
        # for a refusal (#19). This route only ever saw None before.
        logger.error("Could not read annotations: %s", printable(str(exc)))
        return exc.exit_code
    if found is None:  # pragma: no cover - -ar requires -ae, which reads them
        return exits.NO_SOURCE

    stopped = _before_writing(args)
    if stopped is not None:
        return stopped

    # Named once, here, and passed to everything that needs it. Under a
    # metadata policy naming re-parses every package document, and computing it
    # in two places is the 2x read this project has already fixed twice.
    assignments = _named(args, policy)
    # Walked whether or not a conversion would copy them: a zipped book in the
    # library shares its name with a package either way.
    copyable = collect_copyable(args.source_dir)

    code = exits.SUCCESS
    if args.annotations_embedded:
        code = _embed_in_shelf(
            args,
            policy,
            found,
            assignments=assignments,
            copyable=copyable,
        )
        # A book the refresh could not rebuild is that book's failure, and the
        # detached file is somewhere else, so it is still written. Anything
        # else -- no shelf, the lock held -- stops here as it always did.
        if code not in (exits.SUCCESS, exits.FAILED):
            return code

    # Guarded here rather than at the call sites. It was checked on the route
    # through annotations_after_export and not on the -ar route, so
    # "--dry-run -ae -ar" rewrote every archive on the shelf. And after the
    # read rather than before it: returning first, the dry run said the
    # annotations "were read" having read nothing, and exited 0 where the
    # real run was refused the container and exited 8. -ao -d reads first.
    # After the shelf is judged too, which a dry run does as the run does,
    # and has said what it would refresh.
    if args.dry_run or not args.annotations_detached:
        _unopened_notes(args)
        return code

    # Each note named after the file the book is placed at, as the
    # conversion route names it: named from the assignment, an edition
    # moved on to its marked name wrote into the other edition's note.
    # Only a vault uses the names, as for -ao: a JSON or CSV file named
    # and opened every zipped book for nothing.
    written = write_export(
        args,
        found,
        args.annotations_detached,
        (
            _with_copies(args, policy, assignments, copyable, shelf=True)
            if vault_of(args) is not None
            else []
        ),
        copyable=copyable,
    )
    # A failed book outranks the destination's own error, the order a
    # conversion run uses too: see afterwards.outcome.
    return code if code != exits.SUCCESS else written


def _before_writing(args: argparse.Namespace) -> int | None:
    """
    Decide whether a refresh that has read its annotations stops short.

    :param args: Parsed command line arguments.

    :return: The exit code to stop with, or None to go on and write.
    """
    # A glob over a missing directory yields nothing, which read as a clean
    # run over an empty shelf: -ar with a typo in -o said it had refreshed
    # every book it found, having looked at none. Checked before the dry run
    # returns, for the same reason as the read: it returned first, and "-d"
    # exited 0 over the typo the real run exited 5 for.
    # os.path.isdir, which never raises, where Path.is_dir raised
    # PermissionError for a shelf in a directory the run may not search.
    if args.annotations_embedded and not os.path.isdir(args.output_dir):  # noqa: PTH112
        logger.critical(
            "Output directory does not exist: %s", printable(str(args.output_dir))
        )
        return exits.NO_OUTPUT
    return None


def _named(args: argparse.Namespace, policy: NamingPolicy) -> list[Assignment]:
    """
    Name every book in the library, once.

    :param args: Parsed command line arguments.
    :param policy: The naming policy in force.

    :return: One assignment per package.
    """
    return assign_names(
        collect_package_dirs(args.source_dir),
        policy,
        args.on_collision,
        # The shelf the conversion weighed, so every route names alike.
        shelf=shelf_names(args.output_dir),
        unopened=Unopened(packages=args.skip_incomplete),
    )


def _embed_in_shelf(
    args: argparse.Namespace,
    policy: NamingPolicy,
    found: list[dict[str, Any]],
    *,
    assignments: Sequence[Assignment],
    copyable: Sequence[Path],
) -> int:
    """
    Put each book's annotations inside the archive already on the shelf.

    The archive is the one the plan would call the book's own, not the file
    under its name: that file may hold another book -- the edition the name
    was given to first, likely the last copy of one deleted from the library
    -- and a refresh wrote this book's highlights into it, while the book
    itself, moved on to its marked name, got none.

    :param args: Parsed command line arguments.
    :param policy: The naming policy the names came from.
    :param found: Every annotation read from Apple.
    :param assignments: The names every package was given.
    :param copyable: The library's already-zipped books and PDFs, which
        answer to a package's name too.

    :return: A process exit code.
    """
    index = index_by_package(
        found, [item.package for item in assignments], copyable=copyable
    )

    # The same lock the export takes. These writes go into the output
    # directory and leave partials there, and a concurrent run's sweep cannot
    # tell one of those from an abandoned one. Refused on this route, the
    # error escaped main -- which maps it only around the export -- as a
    # traceback and exit 1.
    # A dry run takes no lock, as a conversion's does not, and writes nothing.
    tally = _Tally()
    highlighted = {
        item.package
        for item in assignments
        if annotations_for_book(item.package.name, index)
    }
    try:
        with nullcontext() if args.dry_run else output_lock(args.output_dir):
            # Read under the lock, and as before a write: a policy that names
            # from the folder reads the book's own identifier to compare --
            # only for the books with highlights, the ones rewritten.
            places = placed(
                _shelved(args, policy, assignments, copyable),
                args.output_dir,
                policy,
                writing=True,
                unopened=Unopened(packages=args.skip_incomplete),
                only=highlighted,
            )
            # Before anything is rewritten, and in the dry run as well: a
            # read-only shelf with a lock file that opened failed every book.
            if any(places.get(package) is not None for package in highlighted):
                check_writable(args.output_dir)
            _refresh_each(args, assignments, index, places, tally)
    except (OutputLockedError, ShelfUnwritableError) as exc:
        logger.critical("%s", exc)
        return exc.exit_code
    except KeyboardInterrupt:
        # Escaped to main's last resort, which said 130 and nothing about the
        # books already rewritten. Each rewrite is an atomic replace, so the
        # count is exact and a rerun carries on.
        tally.interrupted = True
    if not args.annotations_detached and not tally.interrupted:
        # Rewritten are the packages' archives; a copy is not rebuilt. Whether
        # there is one on the shelf is the conversion's business, not known here.
        warn_about_copies(index, copyable, copied=None)
        warn_about_bookless(found)
    return _refresh_outcome(tally, dry_run=args.dry_run)


def _refresh_outcome(tally: _Tally, *, dry_run: bool = False) -> int:
    """
    Say what a refresh did, and choose its exit code.

    Every book it could not refresh is a failure, and so is a refresh the
    floor stopped, as for a conversion. Both were logged and the run exited 0,
    so a scheduled refresh that hit ENOSPC on every book reported success.

    :param tally: What the refresh did.
    :param dry_run: Whether it only said what it would do. Its exit code is
        the real run's, as a dry conversion's is.

    :return: 130 if interrupted, 1 if a book was left behind, otherwise 0.
    """
    done = "Dry run: would refresh" if dry_run else "Refreshed"
    parts = [f"{done} annotations in {tally.changed} book(s)"]
    if tally.failed:
        parts.append(f"could not refresh {tally.failed}")
    if tally.stopped:
        parts.append("stopped at the --min-free floor before the rest")
    if tally.interrupted:
        parts.append("interrupted before the rest")
    parts.append("converted nothing")
    summary = "; ".join(parts) + "."
    if tally.interrupted:
        logger.warning("%s", summary)
        return exits.INTERRUPTED
    if tally.failed or tally.stopped:
        logger.error("%s", summary)
        return exits.FAILED
    logger.info("%s", summary)
    return exits.SUCCESS


@dataclass
class _Tally:
    """What a refresh has done so far, kept where an interrupt cannot lose it."""

    changed: int = 0  # Archives rewritten, or that a dry run would rewrite.
    failed: int = 0  # Archives that could not be.
    stopped: bool = False  # The --min-free floor stopped the rest.
    interrupted: bool = False  # Ctrl-C stopped the rest.


class _DueError(Exception):
    """Raised in a dry run, in place of the rebuild an archive is due."""


def _refresh_each(
    args: argparse.Namespace,
    assignments: Sequence[Assignment],
    index: dict[str, list[dict[str, Any]]],
    places: dict[Path, Path | None],
    tally: _Tally,
) -> None:
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
    :param places: Each book's own archive on the shelf, or None.
    :param tally: Counted into as each book is done, so a Ctrl-C leaves it
        saying how many archives were rewritten, how many could not be, and
        whether the floor stopped the refresh before the rest.

    A dry run reads each archive as the refresh does, to tell which are due,
    and rewrites none. It measures the volume once, at the first archive
    due, where the refresh first measures: it returned before, and said all
    was well of a refresh the floor stopped with exit 1.
    """
    # An interval of one: rebuilds run one at a time, so every one is
    # measured, one statvfs per book that is actually rewritten.
    progress = progress_for(len(assignments), 1)
    measured: list[bool] = []

    def room() -> bool:
        if not args.dry_run:
            return progress.has_room(args.output_dir, args.min_free)
        if not measured:
            measured.append(progress.has_room(args.output_dir, args.min_free))
        if measured[0]:
            raise _DueError
        return False

    for item in assignments:
        marker = progress.tick()
        target = places.get(item.package)
        if target is None:
            continue
        mine = annotations_for_book(item.package.name, index)
        if not mine:
            continue
        try:
            if replace_annotations(target, mine, room=room):
                tally.changed += 1
                logger.info(
                    "%s Refreshed %d annotation(s) in %s",
                    marker,
                    len(mine),
                    printable(target.name),
                )
        except _DueError:
            tally.changed += 1
            logger.info(
                "%s Would refresh %d annotation(s) in %s",
                marker,
                len(mine),
                printable(target.name),
            )
        except NoRoomError:
            # Sticky, like the conversions' floor: the volume does not get
            # emptier by asking again, so every book after this one stops too.
            tally.stopped = True
            return
        except UNREADABLE_MEMBER + (ArchiveInvalidError,) as exc:
            _reraise_interrupt(exc)
            # BadZipFile is not an OSError, so one damaged archive used to
            # abort the whole refresh and every book after it went untouched;
            # nor is what a damaged compressed stream raises, which did the
            # same until #21. A damaged archive is an expected state: --verify
            # exists to find them. Counted as well as logged, so the run's
            # exit code says a book was left behind.
            tally.failed += 1
            logger.error("Could not refresh %s: %s", printable(target.name), exc)


def _reraise_interrupt(exc: BaseException) -> None:
    """
    Raise the Ctrl-C behind an error, if one is there.

    A Ctrl-C landing in zipfile's close of one member leaves that member's
    writing handle open, and closing the archive then raises ValueError with
    the KeyboardInterrupt as its context. Taken for a book that could not be
    read, the refresh went on past the Ctrl-C and exited 1.

    :param exc: The error a rebuild ended with.

    :raises KeyboardInterrupt: If one is in its chain of causes.
    """
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        if isinstance(cause, KeyboardInterrupt):
            raise cause
        seen.add(id(cause))
        cause = cause.__cause__ or cause.__context__


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
            # Pointed at the refusal above rather than at --force: a
            # directory the run may not write into is not opened by it.
            logger.error(
                "Your highlights were not written either, because both files "
                "are judged before either is written. Settle the library "
                "export's refusal above, or write the two separately."
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
                logger.error("%s", LIBRARY_SKIPPED)
            return code
        if not args.library_export:
            return code
    return library_export(args, policy)


def _unwritable_destination(args: argparse.Namespace) -> str | None:
    """
    Judge where a convert-nothing run would write, before it writes anything.

    Only the library export is judged here. The annotation export is judged
    before the run, by the check every route that writes one makes
    (:func:`~epubconvert.run.preflight.check_environment`).

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
