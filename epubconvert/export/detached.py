"""
Writing the files a run produces besides books.

Two of them: the detached annotation export, as a JSON document or a vault of
Markdown notes, and the library export, as a Goodreads-format CSV or a JSON
document. Each goes to a file the reader named or to standard output, and
each has one rule about a file that is already there. The annotation export
is a file the reader keeps and adds to, so it is merged into and refused when
it is not one of ours. The library export is a snapshot with nothing to merge
on, so it is left alone unless ``--force`` says to replace it.

Held apart from :mod:`epubconvert.run` so that driving a run and writing what
it read are two modules rather than one that does both.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..collect.annotations import STDOUT
from ..collect.annotations import build_document as build_annotation_document
from ..collect.annotations import merge as merge_annotations
from ..collect.coredata import ContainerUnavailableError
from ..collect.library import collect as collect_library
from ..utils import exits
from ..utils.app_logger import logger
from ..utils.contained import is_free
from ..utils.display import printable
from ..utils.policy import Assignment, NamingPolicy
from . import catalogue, notes
from .archive import write_atomically
from .notes import SIDECAR_SUFFIX


def write_export(
    args: argparse.Namespace,
    found: list[dict[str, Any]],
    destination: str,
    named: Sequence[Assignment],
) -> int:
    """
    Write the detached export, in whichever shape this run asked for.

    The one place that decides between a vault of notes and a single JSON
    document. It was decided at three separate call sites, so a fourth route
    could have been added without anyone seeing the other three as precedent --
    the shape of defect this project has shipped four times.

    :param args: Parsed command line arguments.
    :param found: Every annotation this run read.
    :param destination: The file or directory named on the command line.
    :param named: The names this run gave every book.

    :return: A process exit code.
    """
    if args.annotations_format == "markdown":
        return notes.write_vault(found, destination, named)
    return _write_detached(found, destination)


def _write_detached(found: list[dict[str, Any]], destination: str) -> int:
    """
    Write the one-file-per-library export.

    To a file, a rerun merges into what is there. To standard output there is
    nothing to merge into -- a pipe is not a file to add to -- so the whole set
    is emitted and nothing is said about what changed.

    :param found: What was read from Apple.
    :param destination: A path, or ``-`` for standard output.

    :return: A process exit code.
    """
    if destination == STDOUT:
        _emit(
            json.dumps(build_annotation_document(found), indent=2, ensure_ascii=False)
            + "\n"
        )
        return exits.SUCCESS

    target = Path(destination)
    try:
        existing = _existing_annotations(target)
    except ContainerUnavailableError as exc:
        logger.critical("%s", exc)
        return exits.NO_OUTPUT

    merged, tally = merge_annotations(existing, found)
    try:
        write_atomically(
            target,
            json.dumps(build_annotation_document(merged), indent=2, ensure_ascii=False)
            + "\n",
        )
    except OSError as exc:
        logger.critical("Could not write %s: %s", printable(str(target)), exc)
        return exits.NO_OUTPUT

    logger.info("Wrote %d annotation(s) to %s", len(merged), printable(str(target)))
    if existing is not None:
        logger.info(
            "%d added, %d updated, %d unchanged, %d kept (no longer in Books)",
            tally["added"],
            tally["updated"],
            tally["unchanged"],
            tally["kept"],
        )
    return exits.SUCCESS


def _existing_annotations(target: Path) -> dict[str, Any] | None:
    """
    Read back an export so a rerun can add to it rather than replace it.

    A file that is there and is not an export of this shape is refused rather
    than overwritten or treated as empty. Either would throw away annotations
    the reader may no longer be able to get back out of Books.

    "Not the document I expect" is one condition, not two. Refusing only
    malformed JSON meant a file holding a valid JSON *list* fell through to
    "nothing to merge into" and was silently replaced.

    :param target: The file about to be written.

    :return: The document, or None when there is nothing to merge into.

    :raises ContainerUnavailableError: If it is there and is not one of ours.
    """
    try:
        text = _read_back(target)
        if text is None:
            return None
        loaded = json.loads(text)
    except (OSError, ValueError, RecursionError) as exc:
        # RecursionError is neither: deeply nested JSON raises it out of
        # json.loads, and it used to escape as a traceback.
        raise ContainerUnavailableError(
            f"{target.name} is already there and could not be read ({exc}); "
            "move it aside rather than have this overwrite it"
        ) from exc
    # Checked on the parsed value, never on its truthiness: a file holding
    # "null" parses to None and would otherwise read as nothing to merge into.
    if not isinstance(loaded, dict) or not isinstance(loaded.get("annotations"), list):
        raise ContainerUnavailableError(
            f"{target.name} is already there and is not an annotation export; "
            "move it aside rather than have this overwrite it"
        )
    # A "\ud83d" escape is valid JSON and decodes to a lone surrogate, which
    # UTF-8 cannot encode. Merged, it made the write raise UnicodeEncodeError
    # -- not an OSError -- out of main as a traceback. Found here, it is
    # refused like any other file that cannot be read back, before anything
    # is written. Searched for rather than encoded to find out: a bare
    # .encode() is what the surrogate-safe naming rule forbids.
    if _holds_lone_surrogate(loaded):
        raise ContainerUnavailableError(
            f"{target.name} is already there and holds a lone surrogate, which "
            "is not valid Unicode; move it aside rather than have this "
            "overwrite it"
        )
    return loaded


def _read_back(target: Path) -> str | None:
    """
    Read a file this run is about to replace.

    :param target: The file.

    :return: Its contents, or None when there is nothing there.

    :raises OSError: If it could not be read.
    :raises ValueError: If it is not an ordinary file of a plausible size. A
        FIFO blocks ``read_text`` until a writer appears, which is never.
    """
    if not target.exists():
        return None
    if not target.is_file():
        raise ValueError("not a regular file")
    if target.stat().st_size > MAX_EXPORT_BYTES:
        raise ValueError(f"larger than {MAX_EXPORT_BYTES} bytes")
    return target.read_text(encoding="utf-8")


def _emit(text: str) -> None:
    """
    Write a document to standard output.

    :param text: The whole document, ending in a newline.
    """
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        # "-ao - | head" and "| less" then q are how the flag's own help text
        # says to use it. Closing the pipe is the reader saying they have seen
        # enough, not an error to report. The descriptor is replaced so the
        # interpreter's shutdown flush cannot raise again.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


#: How large an export this will read back to merge into. Generous next to a
#: real one -- 400 annotations is under half a megabyte -- and finite, so a
#: file that is not an export cannot be read into memory whole before the
#: check that would have refused it.
MAX_EXPORT_BYTES = 256 * 1024 * 1024

#: A surrogate code point on its own. A paired ``😀`` escape decodes
#: to one astral character, so only an unpaired half is left to match.
LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _holds_lone_surrogate(document: object) -> bool:
    """
    Report whether any key or string in a parsed document is a lone surrogate.

    Searched where it lies rather than serialised back to one string first: an
    export may be up to :data:`MAX_EXPORT_BYTES`, and a second full-size copy
    only to search it doubled the peak. Iterative, so nesting depth costs
    nothing on the stack.

    :param document: What :func:`json.loads` returned.

    :return: True if a surrogate code point stands on its own anywhere.
    """
    pending: list[object] = [document]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if LONE_SURROGATE.search(item):
                return True
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def vault_of(args: argparse.Namespace) -> Path | None:
    """
    The directory of notes this run will write, if it writes one.

    ``--verify`` is excluded: it reads the output directory and returns before
    any vault is written, and requiring a library for it would stop anyone
    checking a shelf on a machine that never had one.

    Answered here rather than at each caller, because two of them differ only
    in whether the vault has been created yet: judged twice, a dry run
    predicted a refusal the real run did not make.

    :param args: Parsed command line arguments.

    :return: The vault directory, or None when no notes are written.
    """
    if args.verify or args.annotations_format != "markdown":
        return None
    destination = args.annotations_only or args.annotations_detached
    return Path(destination) if destination else None


def library_refusal(
    target: Path, *, force: bool, pending: Sequence[Path] = ()
) -> str | None:
    """
    Say why the library export cannot be written where it was asked to.

    Asked before the library is read, because the read opens every book's
    package document and a run that was never going to write should not pay
    for 2,805 of them first. A directory is named as a directory rather than
    answered with "pass --force": ``--force`` does not make a directory
    writable, and sending the reader there wasted their next run.

    :param target: The file the run would write.
    :param force: Whether the reader asked to replace what is there.
    :param pending: Directories this run will create before writing, so a
        catalogue written into a vault the same run makes is not called
        homeless for being judged before it exists.

    :return: The reason, or None when the write can go ahead.
    """
    if target.is_dir():
        return f"{printable(str(target))} is a directory; name a file to write."
    # Compared as the filesystem resolves them, never as they were typed:
    # "-ao vault --library-export ~/vault/l.csv" names one directory twice
    # and was refused for the spelling. The same normalisation the two
    # destinations are already compared with.
    coming = {os.path.realpath(path) for path in pending}
    inside_vault = os.path.realpath(target.parent) in coming
    if inside_vault and _note_name(target.name):
        # A vault is written before the catalogue, so this target is a note
        # that does not exist yet: is_free says the name is available and
        # --force then wrote a CSV over the reader's highlights. Every other
        # write in this tool refuses to go through a file it did not author.
        return (
            f"{printable(str(target))} is the name of a note in that vault; "
            "the library export would write over it. Name a file the vault "
            "does not use, or put the catalogue outside it."
        )
    if not inside_vault and not target.parent.is_dir():
        return (
            f"{printable(str(target.parent))} is not there; "
            "the library export does not create directories."
        )
    if not force and not is_free(target):
        return (
            f"{printable(str(target))} is already there. The library export is a "
            "snapshot rather than a file that is merged into, so pass --force to "
            "replace it, or name another file."
        )
    return None


def _note_name(name: str) -> bool:
    """
    Whether a filename is one a vault gives a note or its sidecar.

    :param name: The filename, without its directory.

    :return: True when writing it would take a note's place.
    """
    return name.endswith(".md") or name.endswith(SIDECAR_SUFFIX)


def library_export(args: argparse.Namespace, policy: NamingPolicy) -> int:
    """
    Write the library and stop.

    Reads Apple's container and nothing else, like ``-ao``: somebody who wants
    a catalogue of what they own should not have to convert a library to get
    one. Unlike the annotation export it is a snapshot rather than a file that
    is merged into -- there is nothing in a CSV to merge on -- so an existing
    file is left alone unless ``--force`` says to replace it.

    :param args: Parsed command line arguments.
    :param policy: The naming policy, so each book can say what its file on
        the shelf is called.

    :return: A process exit code.
    """
    target = None if args.library_export == STDOUT else Path(args.library_export)
    # Refused before the read, not after it: the read opens every book's
    # package document, and a run that was never going to write should not
    # pay for 2,805 of them first.
    vault = vault_of(args)
    refusal = (
        None
        if target is None
        else library_refusal(
            target, force=args.force, pending=() if vault is None else (vault,)
        )
    )
    if refusal is not None and not args.dry_run:
        logger.critical("%s", refusal)
        return exits.NO_OUTPUT
    try:
        found = collect_library(policy=policy, identifiers=not args.no_isbn)
    except ContainerUnavailableError as exc:
        logger.critical("Could not read the library: %s", exc)
        return exc.exit_code
    if args.library_format == "csv":
        # The advice is about the CSV's ISBN columns and the tracker that
        # reads them. The JSON carries every identifier, UUIDs included, and
        # is not the file anyone imports.
        _report_matchable(found, read_identifiers=not args.no_isbn)
    else:
        logger.info("Read %d book(s) from the library.", len(found))
    if args.dry_run:
        if refusal is not None:
            # Said rather than exited on: the estimate is what a dry run is
            # for, and a real run would stop here with exit code 5.
            logger.warning("A real run would refuse to write: %s", refusal)
        logger.info("Dry run: %d book(s) read; nothing was written.", len(found))
        return exits.SUCCESS

    text = catalogue.render(
        found, args.library_format, unknown_shelf=args.unknown_shelf
    )
    if target is None:
        _emit(text)
        return exits.SUCCESS
    try:
        write_atomically(target, text)
    except OSError as exc:
        logger.critical("Could not write %s: %s", printable(str(target)), exc)
        return exits.NO_OUTPUT
    logger.info("Wrote %d book(s) to %s", len(found), printable(str(target)))
    return exits.SUCCESS


def _report_matchable(found: list[dict[str, Any]], *, read_identifiers: bool) -> None:
    """
    Say how much of the export a tracker will be able to match.

    Said before anything is written and whether or not it is, because the
    reader deserves it *before* a 3,620-row import into a service where
    undoing one is manual. "N books had no ISBN" reads like an edge case;
    on a real library the unmatched rows are most of the file.

    :param found: The catalogue.
    :param read_identifiers: Whether the package documents were opened at all.
    """
    logger.info("Read %d book(s) from the library.", len(found))
    if not found:
        return
    if not read_identifiers:
        logger.info(
            "ISBNs were not read (--no-isbn), so a tracker will match none of "
            "these rows by ISBN."
        )
        return
    matched = catalogue.matchable_count(found)
    if matched == len(found):
        logger.info("Every book carries an ISBN a tracker can match on.")
        return
    logger.warning(
        "%d of %d book(s) carry an ISBN a tracker can match on. The other %d "
        "will import unmatched and need adding by hand.",
        matched,
        len(found),
        len(found) - matched,
    )
