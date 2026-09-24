"""
Tests for what a vault run tells the reader, and its caller, about each book.

``test_notes.py`` pins the note itself. This pins the run around it: every
book whose highlights were read either reaches a note or is named in the
summary, and the exit code says whether anything could not be saved. A cron
job reads nothing but the exit code, so a book that got no note and a run
that said 0 is the silent loss this module exists to catch.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring

from pathlib import Path
from typing import Any

import pytest

from epubconvert.export import notes
from epubconvert.utils import app_logger, exits
from epubconvert.utils.policy import Assignment


def _highlight(source: str, text: str) -> dict[str, Any]:
    return {
        "id": text,
        "text": text,
        "chapter": "C",
        "created": "2020-01-01T00:00:00Z",
        "book": {"title": "Dune", "author": "Frank Herbert", "source": source},
    }


#: Two copies of one book: the first takes the name, the second loses it, as
#: the planner assigns them under the default ``--on-collision skip``.
COLLIDED = [
    Assignment(Path("One.epub"), "Frank Herbert - Dune.epub", "dune"),
    Assignment(
        Path("Two.epub"),
        "",
        "dune",
        reason="another book takes Frank Herbert - Dune.epub",
    ),
]


class TestABookThatLostACollision:
    """
    Under ``--on-collision skip`` the losing copy has no filename, so no note
    stem either, and ``-ao`` runs no planner that would have said so. Its
    highlights were dropped without a word.
    """

    def test_its_highlights_are_named_as_not_written(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        app_logger.configure(verbosity=0)
        found = [_highlight("One.epub", "first"), _highlight("Two.epub", "second")]

        notes.write_vault(found, str(tmp_path / "vault"), COLLIDED)

        reported = capsys.readouterr().err
        assert "Two.epub" in reported
        assert "--on-collision suffix" in reported

    def test_the_exit_code_follows_the_collision_rule_not_the_failure_rule(
        self, tmp_path: Path
    ):
        # A collision does not change the exit code anywhere else in this
        # tool, and the fix is a flag, not a retry.
        found = [_highlight("One.epub", "first"), _highlight("Two.epub", "second")]

        code = notes.write_vault(found, str(tmp_path / "vault"), COLLIDED)

        assert code == exits.SUCCESS

    def test_a_losing_book_with_no_highlights_is_not_mentioned(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        app_logger.configure(verbosity=0)

        notes.write_vault(
            [_highlight("One.epub", "first")], str(tmp_path / "vault"), COLLIDED
        )

        assert "--on-collision" not in capsys.readouterr().err

    def test_it_is_not_reported_as_a_book_without_highlights(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # With only the loser highlighted, no note is written at all, and the
        # summary said none of the books considered had a highlight.
        app_logger.configure(verbosity=0)

        notes.write_vault(
            [_highlight("Two.epub", "second")], str(tmp_path / "vault"), COLLIDED
        )

        reported = capsys.readouterr().err
        assert "none of them has a highlight" not in reported
        assert "Two.epub" in reported
