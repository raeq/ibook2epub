"""
Logging setup for the epub conversion tool.

Importing this module has no side effects beyond registering a custom TRACE
level and creating a package logger with a null handler. Handlers are only
attached when :func:`configure` is called, which keeps the library importable
from tests without spraying an ``app.log`` into the current directory.
"""

from __future__ import annotations

import contextlib
import logging
import sys
from pathlib import Path
from typing import Any, cast

from .display import printable

TRACE = 5  # Below DEBUG (10), for very chatty per-file messages.
logging.addLevelName(TRACE, "TRACE")


class TraceLogger(logging.Logger):
    """A logger with an extra TRACE level below DEBUG."""

    def trace(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a message at the custom TRACE level."""
        if self.isEnabledFor(TRACE):
            self._log(TRACE, message, args, **kwargs)


# Register the subclass only for the duration of our own getLogger call, so
# that loggers created elsewhere in the process are left alone.
_previous_class = logging.getLoggerClass()
logging.setLoggerClass(TraceLogger)
logger = cast(TraceLogger, logging.getLogger("epubconvert"))
logging.setLoggerClass(_previous_class)

logger.addHandler(logging.NullHandler())
logger.propagate = False

# verbosity 0 = -q, 1 = default, 2 = -v, 3 = -vv (and above)
_LEVELS = (logging.WARNING, logging.INFO, logging.DEBUG, TRACE)

#: A terminal is not a transcript. At default verbosity the message is the
#: whole point, and a timestamp on every line of an interactive run is noise
#: the file format already carries for the runs that need it.
_CONSOLE_PLAIN = "%(message)s"
_CONSOLE_VERBOSE = "%(asctime)s - %(levelname)s - %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)s %(message)s"
# ISO 8601 with UTC offset: sorts lexicographically and is unambiguous across
# timezones, unlike a 12-hour local clock.
_FILE_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


class _LogFile(logging.FileHandler):
    """
    The ``--log-file`` handler, which says once that it cannot be written.

    A log file on a volume that filled up during the run printed logging's
    "--- Logging error ---" traceback for every line after, on a run that
    still exited 0. The first failure is said, on the console, and the file
    is left alone for the rest of the run. The exit code is not changed, as
    it is not for a log file that could not be opened at all: the file is a
    copy of what the console already shows.
    """

    #: Whether a write to the file has failed.
    failed = False

    def emit(self, record: logging.LogRecord) -> None:
        """Write a record, unless the file has already failed."""
        if not self.failed:
            super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802
        """Say once why the file could not be written, then stop writing it."""
        del record
        if self.failed:
            return
        self.failed = True
        error = sys.exc_info()[1]
        reason = error.strerror if isinstance(error, OSError) else None
        logger.warning(
            "Could not write to the log file %s: %s; not logging to it for the "
            "rest of the run.",
            printable(self.baseFilename),
            printable(reason or str(error)),
        )

    def close(self) -> None:
        """Close the file, whatever is left in it that cannot be written."""
        # What could not be written was said when it failed.
        with contextlib.suppress(OSError):
            super().close()


def level_for_verbosity(verbosity: int) -> int:
    """
    Map a verbosity count onto a logging level.

    :param verbosity: 0 quiet, 1 normal, 2 debug, 3+ trace.

    :return: The corresponding logging level.
    """
    return _LEVELS[max(0, min(verbosity, len(_LEVELS) - 1))]


def file_only(message: str) -> None:
    """
    Record a line in the log file without repeating it on the console.

    The run summary is printed to stdout for the person watching, and also
    logged so a ``--log-file`` transcript of an interrupted run is not
    indistinguishable from a complete one. Logging it plainly put it on the
    terminal twice.

    :param message: The line to record.
    """
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            handler.handle(
                logger.makeRecord(
                    logger.name, logging.INFO, __file__, 0, "%s", (message,), None
                )
            )


def configure(verbosity: int = 1, log_file: Path | None = None) -> TraceLogger:
    """
    Attach handlers to the package logger.

    Calling this more than once replaces the previously attached handlers, so
    it is safe to use from tests.

    :param verbosity: 0 quiet, 1 normal, 2 debug, 3+ trace.
    :param log_file: Optional path to also write log records to.

    :return: The configured package logger.
    """
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    level = level_for_verbosity(verbosity)
    logger.setLevel(level)

    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(
        logging.Formatter(_CONSOLE_VERBOSE if verbosity > 1 else _CONSOLE_PLAIN)
    )
    logger.addHandler(console_handler)

    if log_file is not None:
        # The console handler is already attached, so a failure here can be
        # reported rather than crashing the process. This runs before anything
        # else in main, so an unwritable --log-file used to exit with a raw
        # traceback before any logging existed to explain it.
        log_path = Path(log_file)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # backslashreplace: os.walk hands back an undecodable filename
            # as lone surrogates, which strict UTF-8 cannot write, and the
            # handler dropped the whole line for a traceback on stderr.
            file_handler = _LogFile(
                log_path, encoding="utf-8", errors="backslashreplace"
            )
        except OSError as exc:
            logger.warning(
                "Not logging to %s: %s", printable(str(log_path)), printable(str(exc))
            )
        else:
            file_handler.setLevel(level)
            file_handler.setFormatter(
                logging.Formatter(_FILE_FORMAT, datefmt=_FILE_DATEFMT)
            )
            logger.addHandler(file_handler)

    return logger
