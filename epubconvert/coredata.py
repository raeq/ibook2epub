"""
Reading Apple Books' own databases.

Apple keeps a reader's library and their annotations in Core Data SQLite
databases inside the Books container. Neither schema is documented or promised,
and both are read the same way: the newest file matching a prefix, opened
read-only, with every column treated as untyped. This module holds what the
two readers share -- where the container is, how its clock is counted, and how
a database is opened without ever writing to it -- so that the annotation
export and the library export cannot drift apart on any of them.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

#: Where Apple keeps the databases, relative to the user's home.
CONTAINER = Path(
    "Library/Containers/com.apple.iBooksX/Data/Documents",
)

#: Core Data counts seconds from 2001-01-01, not from the Unix epoch.
APPLE_EPOCH_OFFSET = 978307200

#: The one way an instant is written by every export, matching the schemas.
INSTANT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ContainerUnavailableError(RuntimeError):
    """Raised when Apple's databases cannot be found or read."""


def container_directory(container: Path | None) -> Path:
    """
    Find the Books container, or say why it cannot be.

    :param container: The container directory, or None for Apple's.

    :return: The directory.

    :raises ContainerUnavailableError: If it is not there.
    """
    directory = container if container is not None else Path.home() / CONTAINER
    if not directory.is_dir():
        raise ContainerUnavailableError(
            f"{directory} is not there; Apple Books may never have run here"
        )
    return directory


def database_in(directory: Path, folder: str, what: str) -> Path:
    """
    Find the live copy of one of Apple's databases, or say why it cannot be.

    :param directory: The container directory.
    :param folder: The subdirectory, which is also the filename prefix:
        ``AEAnnotation`` or ``BKLibrary``.
    :param what: What to call the database in a message.

    :return: The database.

    :raises ContainerUnavailableError: If there is none. On macOS this usually
        means the terminal has not been granted Full Disk Access.
    """
    database = newest(directory / folder, folder)
    if database is None:
        raise ContainerUnavailableError(
            f"no {what} database under {directory / folder}; on macOS this "
            "usually means the terminal needs Full Disk Access"
        )
    return database


def newest(directory: Path, prefix: str) -> Path | None:
    """
    Find the database Apple is currently using.

    The filenames carry a version and a build stamp, and old ones are left in
    place across upgrades, so the name cannot be hard-coded and the newest is
    the live one.

    :param directory: The container subdirectory to look in.
    :param prefix: The filename prefix to match.

    :return: The most recently modified match, or None if there is none.
    """
    # Stat inside the sort key raised on a dangling symlink among the
    # candidates, out of a function whose whole contract is "or None". Books
    # leaves old files here across upgrades, which is why the glob exists.
    dated: list[tuple[float, Path]] = []
    for path in directory.glob(f"{prefix}*.sqlite"):
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not dated:
        return None
    return max(dated, key=lambda pair: pair[0])[1]


def rows(database: Path, query: str) -> list[sqlite3.Row]:
    """
    Run one query against a database, without writing to it.

    Opened read-only through a URI so that reading somebody's library cannot
    modify it, and so that a live Books process holding the write lock does not
    stop the export.

    :param database: The database file.
    :param query: The query to run.

    :return: The rows, as mappings.

    :raises ContainerUnavailableError: If the database cannot be read.
    """
    # as_uri() percent-encodes "?", "#" and "%" itself. Building the URI by
    # concatenation let a filename carrying "?" append its own parameters
    # ahead of mode=ro -- a read path that could open the database writable.
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    except (sqlite3.Error, ValueError, OSError) as exc:
        raise ContainerUnavailableError(f"could not open {database.name}") from exc
    try:
        connection.row_factory = sqlite3.Row
        return list(connection.execute(query))
    except sqlite3.Error as exc:
        # A schema change in a Books update lands here rather than as a
        # traceback: the columns the readers name are Apple's, and Apple never
        # promised them.
        raise ContainerUnavailableError(
            f"{database.name} is not shaped as expected: {exc}"
        ) from exc
    finally:
        connection.close()


def moment(seconds: object) -> str | None:
    """
    Render a Core Data timestamp as UTC.

    :param seconds: Seconds since 2001-01-01, or None.

    :return: An RFC 3339 instant ending in ``Z``, or None.
    """
    # Apple's column is untyped and undocumented. A string, a NaN or a value
    # past the year 9999 all reach here, and every one of them used to take
    # the whole export down with it.
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return None
    if seconds == 0:
        # Apple writes 0 where it has no date. Rendered, that claimed every
        # such book was acquired, and every such highlight modified, at
        # midnight on New Year's Day 2001. Decided here, once, so that the
        # two readers cannot read the same storage convention two ways.
        return None
    try:
        when = datetime.fromtimestamp(seconds + APPLE_EPOCH_OFFSET, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return when.strftime(INSTANT_FORMAT)


def now() -> str:
    """Return this instant as UTC, to the second, ending in ``Z``."""
    return datetime.now(tz=timezone.utc).strftime(INSTANT_FORMAT)
