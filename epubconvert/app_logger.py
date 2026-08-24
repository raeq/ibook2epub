"""
Logging setup for the epub conversion tool.

Importing this module has no side effects beyond registering a custom TRACE
level and creating a package logger with a null handler. Handlers are only
attached when :func:`configure` is called, which keeps the library importable
from tests without spraying an ``app.log`` into the current directory.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

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

_CONSOLE_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)s %(message)s"
# ISO 8601 with UTC offset: sorts lexicographically and is unambiguous across
# timezones, unlike a 12-hour local clock.
_FILE_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def level_for_verbosity(verbosity: int) -> int:
    """
    Map a verbosity count onto a logging level.

    :param verbosity: 0 quiet, 1 normal, 2 debug, 3+ trace.

    :return: The corresponding logging level.
    """
    return _LEVELS[max(0, min(verbosity, len(_LEVELS) - 1))]


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
    console_handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    logger.addHandler(console_handler)

    if log_file is not None:
        # The console handler is already attached, so a failure here can be
        # reported rather than crashing the process. This runs before anything
        # else in main, so an unwritable --log-file used to exit with a raw
        # traceback before any logging existed to explain it.
        log_path = Path(log_file)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
        except OSError as exc:
            logger.warning("Not logging to %s: %s", log_path, exc)
        else:
            file_handler.setLevel(level)
            file_handler.setFormatter(
                logging.Formatter(_FILE_FORMAT, datefmt=_FILE_DATEFMT)
            )
            logger.addHandler(file_handler)

    return logger
