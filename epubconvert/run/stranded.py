"""
Saying where the reader's highlights did not go.

``-ae`` puts a book's highlights inside the book, so a book not converted,
one taken along byte for byte and a highlight Apple recorded against no book
all leave highlights with nowhere to be. Each is said once the run knows,
with the flag that would keep them. Split from
:mod:`epubconvert.run.annotating` when that module neared the line limit.
"""

from __future__ import annotations

import argparse
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from ..collect.annotations import for_book as annotations_for_book
from ..export.archive import index_by_package
from ..utils.app_logger import logger
from ..utils.display import printable
from ..utils.policy import Assignment, NamingPolicy
from .placing import placed


def warn_about_copies(
    index: dict[str, list[dict[str, Any]]],
    copies: Sequence[Path],
    *,
    copied: bool | None,
) -> None:
    """
    Say so when highlights belong to books taken along rather than converted.

    ``-ae`` puts highlights in as a book is converted, and a zipped book or a
    PDF is copied byte for byte, so there is nothing to put them in. The
    warning about highlights that reached no file left the copies out, and
    ``-ae -ar`` walked the packages alone, so both said nothing. Like that
    warning, this one is not given under ``-ad``, where the highlights are
    in a file already, and changes no exit code.

    :param index: The annotations, by book.
    :param copies: The books copied through, those that lost their name too.
    :param copied: Whether the run copies them; under ``--no-copy-through``
        it does not. None when the run cannot know: ``-ae -ar`` converts
        nothing, and said "copied through unchanged" of a shelf built under
        ``--no-copy-through``, which holds no copy.
    """
    books = sorted(
        {copy.name for copy in copies if annotations_for_book(copy.name, index)}
    )
    if not books:
        return
    count = sum(len(annotations_for_book(name, index)) for name in books)
    shown = ", ".join(printable(name) for name in books[:3])
    if len(books) > 3:
        shown += f", and {len(books) - 3} more"
    if copied is None:
        how = (
            "not converted by ibook2epub were not embedded (zipped books and PDFs "
            "are taken as they are)"
        )
    elif copied:
        how = "copied through unchanged were not embedded (copies are byte-for-byte)"
    else:
        how = "not copied (--no-copy-through) were not embedded"
    logger.warning(
        "%d annotation(s) from %d book(s) %s: %s. Use -ad FILE or -ao FILE.",
        count,
        len(books),
        how,
        shown,
    )


def warn_about_bookless(found: Sequence[dict[str, Any]]) -> None:
    """
    Say so when highlights were recorded against no book at all.

    Apple records some highlights with no asset id. They name no book, so
    none is embedded anywhere (index_by_package leaves them out), and only a
    detached file carries them; ``-ae`` and ``-ae -ar`` said nothing of
    them. Like the copies' warning, this one is not given under ``-ad`` and
    changes no exit code.

    :param found: Every annotation this run read.
    """
    bookless = sum(
        1
        for item in found
        if not (isinstance(book := item.get("book"), dict) and book.get("assetId"))
    )
    if bookless:
        logger.warning(
            "%d highlight(s) Apple recorded against no book were not embedded; "
            "use -ad FILE or -ao FILE.",
            bookless,
        )


def warn_about_stranded(
    args: argparse.Namespace,
    policy: NamingPolicy,
    found: list[dict[str, Any]],
    named: Sequence[Assignment],
    copyable: Sequence[Path],
    *,
    held_back: Collection[Path] = frozenset(),
    stopped: Collection[Path] = frozenset(),
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

    A book ``-m`` held back is left out, as a run stopped with Ctrl-C leaves
    out the books it did not reach: the summary says it is held back, and
    the next run embeds its highlights. It was counted here, and the reader
    of a plain ``-ae`` under the default cap was told those books' highlights
    reached no file and pointed at DRM. One line says they wait instead, and
    one more for the books the ``--min-free`` floor stopped, which are as
    unattempted.

    :param args: Parsed command line arguments.
    :param policy: The naming policy the names came from.
    :param found: Every annotation this run read.
    :param named: The names the export used.
    :param copyable: The library's already-zipped books and PDFs.
    :param held_back: The books ``-m`` held back for a later run.
    :param stopped: The books the ``--min-free`` floor kept from starting.
    """
    # Quiet: the conversion before this read the same annotations against
    # the same library and has already said which it could not place.
    index = index_by_package(
        found, [item.package for item in named], copyable=copyable, quiet=True
    )
    # Found where the plan finds it, not by name: a file under the book's name
    # may hold another book, and then these highlights went nowhere and the
    # warning, seeing a file there, said nothing.
    places = placed(named, args.output_dir, policy)
    stranded_books: list[str] = []
    stranded = 0
    # What a rerun embeds: those -m held back, and those the floor stopped.
    waiting = [0, 0]
    for item in named:
        if places.get(item.package) is not None:
            continue
        mine = annotations_for_book(item.package.name, index)
        if item.package in held_back:
            waiting[0] += len(mine)
        elif item.package in stopped:
            waiting[1] += len(mine)
        elif mine:
            stranded_books.append(item.package.name)
            stranded += len(mine)

    if waiting[0]:
        logger.info(
            "%d annotation(s) wait for books -m held back; they go in when those "
            "are converted.",
            waiting[0],
        )
    if waiting[1]:
        logger.info(
            "%d annotation(s) wait for books the --min-free floor stopped; they go "
            "in when a rerun converts those.",
            waiting[1],
        )
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
