"""
Checking the shelf with ``--verify``, and saying how to repair what it finds.

A damaged archive is only half a report: the other half is a command that
replaces it, spelt so that a shell reads it back exactly and ``--match``
selects that book and no other. Split from :mod:`epubconvert.run.run` when
that module reached the line limit; ``main`` still decides when to verify.
"""

from __future__ import annotations

import argparse
import glob
import os
import shlex
import unicodedata
from collections.abc import Sequence
from pathlib import Path

from ..export.archive import collect_package_dirs
from ..export.inspect_output import verify_output
from ..utils import exits
from ..utils.app_logger import logger
from ..utils.display import emit, printable
from .convert import matches_pattern


def run_verify(args: argparse.Namespace) -> int:
    """
    Check the archives already in the output directory.

    :param args: Parsed command line arguments.

    :return: A process exit code; non-zero if anything is damaged.
    """
    # A glob over a missing directory yields nothing, which read as a clean
    # bill of health: the one command whose purpose is finding damage reported
    # success having checked not a single file.
    if not args.output_dir.is_dir():
        logger.critical(
            "Output directory does not exist: %s", printable(str(args.output_dir))
        )
        return exits.NO_OUTPUT

    checked, damaged, broken = verify_output(args.output_dir, epubcheck=args.epubcheck)
    if not checked:
        emit(f"No archives found in {printable(str(args.output_dir))}.")
        return 0
    shelf = printable(str(args.output_dir))
    emit(f"Verified {checked} archive(s) in {shelf}: {damaged} damaged.")
    if damaged:
        _advise_repair(args, broken)
    return exits.DAMAGED if damaged else exits.SUCCESS


def _advise_repair(args: argparse.Namespace, broken: Sequence[str]) -> None:
    """
    Tell the reader how to replace each damaged archive, in words that work.

    Naming them matters: ``--force`` alone re-exports the whole library, and
    the default cap then picks its subset at random, so following that advice
    literally could leave every damaged book untouched and still report
    success.

    Only an archive named exactly as a package in the library is sent to
    ``--match``, with a pattern checked against the rule that will read it.
    The stem used to be printed for every file, and ``--match`` reads ``?``,
    ``[`` and ``*`` as a glob against the whole package name, so ``Who Moved
    My Cheese?`` and ``Foundation [Asimov]`` matched nothing and the run it
    advised exited 0 having repaired nothing. A file copied through from the
    library, or named by a suffix or a naming option, is no package's name,
    and ``--force`` does not copy a file again: it is moved aside instead,
    and any run puts back a book missing from the shelf.

    :param args: Parsed command line arguments.
    :param broken: The damaged archives' names.
    """
    # Read only now, and only if it is there: --verify checks a shelf on a
    # machine that may never have had a library.
    packages = collect_package_dirs(args.source_dir) if args.source_dir.is_dir() else []
    patterns = {name: _repair_pattern(name, packages) for name in broken}
    forced = [name for name in broken if patterns[name] is not None]
    aside = [name for name in broken if patterns[name] is None]
    # Quoting makes a name one shell word, and does nothing about the ESC and
    # CR that rewrite the line: a path is spelt in $'...' (see _shell_word), a
    # name to move aside is escaped with printable, and a pattern holds "?"
    # for each such character instead, since --match would read the escape
    # literally (see _repair_pattern).
    if forced:
        emit("Re-export each damaged book, for example:")
        shelf = _shelf_flags(args)
        for name in forced[:3]:
            # Joined to the flag, so a name that starts with a dash is not
            # read as one.
            quoted = shlex.quote(patterns[name] or "")
            emit(f"  ibook2epub --match={quoted} --force {shelf}")
        if len(forced) > 3:
            emit(f"  ...and {len(forced) - 3} more")
        # --verify refuses them, so it cannot know what the shelf was named by.
        emit("  (add the --name-by/-p/--on-collision flags you export with)")
    if aside:
        emit(
            f"Move each of these out of {printable(str(args.output_dir))} and "
            "rerun as before: --force cannot single it out, and a run puts "
            "back a book missing from the shelf."
        )
        for name in aside[:3]:
            emit(f"  {printable(name)}")
        if len(aside) > 3:
            emit(f"  ...and {len(aside) - 3} more")


def _shelf_flags(args: argparse.Namespace) -> str:
    """
    Spell out the shelf and the library a repair command has to name.

    The advice named neither. Run as printed, it looked for the library in
    its default home and exited 4; given ``-s`` it wrote a fresh copy to
    ``~/Books`` and left the damaged file where it was.

    :param args: Parsed command line arguments.

    :return: ``-s`` when the library was given rather than discovered, and
        ``-o`` always, each quoted as one shell word safe to display.
    """
    flags = [] if args.source_auto else ["-s", _as_word(args.source_dir)]
    return " ".join(
        _shell_word(word) for word in [*flags, "-o", _as_word(args.output_dir)]
    )


def _shell_word(word: str) -> str:
    """
    Quote one word so a shell reads it back exactly, and a terminal shows it.

    Escaping the quoted command for display turned a TAB into the four
    characters ``\\x09``, which a shell reads literally: the repair command
    created a directory named that, converted into it, and exited 0. A word
    that needs escaping is written in bash and zsh's ANSI-C quoting instead,
    where ``\\xNN`` means that byte, so it is both safe to print and the path
    it names. Every other word keeps POSIX quoting.

    :param word: One word of the command.

    :return: The word, quoted.
    """
    if printable(word) == word:
        return shlex.quote(word)
    spelt = "".join(
        char
        if printable(char) == char and char not in "'\\"
        else "".join(f"\\x{byte:02x}" for byte in os.fsencode(char))
        for char in word
    )
    return f"$'{spelt}'"


def _as_word(path: Path) -> str:
    """
    Spell a path so that argparse cannot take it for a flag.

    :param path: A path as the user gave it.

    :return: The path, led by ``./`` when it would otherwise start with a dash.
    """
    text = str(path)
    return f"./{text}" if text.startswith("-") else text


def _repair_pattern(name: str, packages: Sequence[Path]) -> str | None:
    """
    Find a ``--match`` pattern that selects exactly the packages named *name*.

    :param name: A damaged archive's name on the shelf.
    :param packages: Every package in the library.

    :return: The plainest pattern that selects them and nothing else, or None
        when no package has that name, or none can be printed that does.
    """
    # Composed on both sides, as --match reads them: a shelf on HFS+ hands a
    # name back decomposed, and lower() alone found no package for it.
    key = unicodedata.normalize("NFC", name).lower()
    wanted = {
        package
        for package in packages
        if unicodedata.normalize("NFC", package.name).lower() == key
    }
    if not wanted:
        return None
    stem, suffix = Path(name).stem, Path(name).suffix
    # The stem reads best but matches anywhere, so "Plain" finds Complain too.
    # The escaped name is anchored only if escaping gave it a bracket. The
    # bracketed dot makes the last a glob, which matches the whole name.
    for pattern in (
        stem,
        glob.escape(name),
        f"{glob.escape(stem)}[.]{suffix[1:]}",
    ):
        # What the advice prints is escaped for display, and --match reads
        # "\x1b" as four characters: a character printable would escape
        # becomes "?", which makes the pattern a glob, and is then checked
        # like any other, since "?" matches more than that one character.
        masked = "".join(char if printable(char) == char else "?" for char in pattern)
        chosen = {p for p in packages if matches_pattern(p.name, masked)}
        if chosen == wanted:
            return masked
    return None
