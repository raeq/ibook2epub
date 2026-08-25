"""
The process exit codes, which are this tool's contract with a script.

Rerun safety and the output lock invite scheduling this from cron or launchd,
and a scheduled run is read by its status rather than its prose. So every
distinct reason a run cannot proceed has its own code: "retry in an hour",
"install something", "fix the path" and "a book is broken" are four different
responses, and a caller should not have to grep stderr to tell them apart.

They used to collapse. Five unrelated conditions all exited ``2`` -- a typo'd
flag, a source directory that is not there, a missing optional extra, a missing
external tool, and ``--verify`` pointed at nothing -- so a scheduled run could
not distinguish a misconfiguration it should alert on from a transient state it
should retry.

``2`` still means a bad command line, because every tool means that by it and
argparse emits it directly. Everything else moved off it. That is a deliberate
break with the previous release, made while nothing had shipped to PyPI.

:data:`MEANINGS` is the single source of the table in the README, so the
documentation cannot drift from the codes.
"""

from __future__ import annotations

#: Every book that could be converted was.
SUCCESS = 0

#: At least one book failed to convert.
FAILED = 1

#: The command line was wrong: unknown, malformed or contradictory flags.
USAGE = 2

#: Another run holds the output lock. Worth retrying later.
LOCKED = 3

#: The source directory does not exist, or no Apple Books library was found.
NO_SOURCE = 4

#: The output directory could not be created, opened or found.
NO_OUTPUT = 5

#: A required extra or external tool is not installed.
MISSING_TOOL = 6

#: ``--verify`` found at least one damaged archive.
DAMAGED = 7

#: Stopped with Ctrl-C. Finished books are intact; rerun to continue.
INTERRUPTED = 130

#: What each code means, in the order the README lists them.
MEANINGS: dict[int, str] = {
    SUCCESS: "Every book that could be converted was.",
    FAILED: "At least one book failed to convert.",
    USAGE: "The command line was wrong: unknown, malformed or contradictory flags.",
    LOCKED: "Another run holds the output lock. Worth retrying later.",
    NO_SOURCE: "The source directory does not exist, or no library was found.",
    NO_OUTPUT: "The output directory could not be created, opened or found.",
    MISSING_TOOL: "A required extra or external tool is not installed.",
    DAMAGED: "`--verify` found at least one damaged archive.",
    INTERRUPTED: "Stopped with Ctrl-C. Finished books are intact; rerun to continue.",
}
