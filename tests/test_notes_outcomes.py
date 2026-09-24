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

from epubconvert.export import noteformat, notes
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

        notes.write_vault(found, str(tmp_path / "vault"), COLLIDED, copyable=())

        reported = capsys.readouterr().err
        assert "Two.epub" in reported
        assert "--on-collision suffix" in reported

    def test_the_exit_code_follows_the_collision_rule_not_the_failure_rule(
        self, tmp_path: Path
    ):
        # A collision does not change the exit code anywhere else in this
        # tool, and the fix is a flag, not a retry.
        found = [_highlight("One.epub", "first"), _highlight("Two.epub", "second")]

        code = notes.write_vault(found, str(tmp_path / "vault"), COLLIDED, copyable=())

        assert code == exits.SUCCESS

    def test_a_losing_book_with_no_highlights_is_not_mentioned(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        app_logger.configure(verbosity=0)

        notes.write_vault(
            [_highlight("One.epub", "first")],
            str(tmp_path / "vault"),
            COLLIDED,
            copyable=(),
        )

        assert "--on-collision" not in capsys.readouterr().err

    def test_it_is_not_reported_as_a_book_without_highlights(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # With only the loser highlighted, no note is written at all, and the
        # summary said none of the books considered had a highlight.
        app_logger.configure(verbosity=0)

        notes.write_vault(
            [_highlight("Two.epub", "second")],
            str(tmp_path / "vault"),
            COLLIDED,
            copyable=(),
        )

        reported = capsys.readouterr().err
        assert "none of them has a highlight" not in reported
        assert "Two.epub" in reported


#: One book, named as the planner names it.
ALONE = [Assignment(Path("Alpha.epub"), "Alpha.epub", "alpha")]


class TestNothingSavedForABookFailsTheRun:
    """
    Every outcome that leaves a book's highlights in no file counts toward the
    exit code, whichever file it was that stood in the way.

    An unreadable note exited 0 while an unreadable sidecar exited 1, and a
    file somebody else wrote at a note's path exited 0 while one at its
    sidecar's path exited 1. The book was equally unsaved in each pair, and a
    cron run that wrote no notes at all reported success.
    """

    def _run(self, vault: Path) -> int:
        return notes.write_vault(
            [_highlight("Alpha.epub", "hl")], str(vault), ALONE, copyable=()
        )

    def test_a_directory_where_the_note_should_be_fails_the_run(self, tmp_path: Path):
        vault = tmp_path / "vault"
        (vault / "Alpha.md").mkdir(parents=True)

        assert self._run(vault) == exits.FAILED

    def test_a_note_that_cannot_be_read_fails_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Alpha.md").write_text("mine", encoding="utf-8")

        def refuse(*_args: object, **_kwargs: object) -> str:
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(Path, "read_text", refuse)

        assert self._run(vault) == exits.FAILED

    def test_a_file_this_tool_did_not_write_fails_the_run_and_is_kept(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        # Decided, not inherited: the book's highlights reach no file, exactly
        # as when the same file sits at the sidecar's path, which already
        # failed the run. The file is still never touched.
        app_logger.configure(verbosity=0)
        vault = tmp_path / "vault"
        vault.mkdir()
        mine = vault / "Alpha.md"
        mine.write_text("# My own note\n", encoding="utf-8")

        code = self._run(vault)

        assert code == exits.FAILED
        assert mine.read_text(encoding="utf-8") == "# My own note\n"
        assert "highlights were not written" in capsys.readouterr().err

    def test_an_edited_note_whose_sidecar_was_written_is_not_a_failure(
        self, tmp_path: Path
    ):
        vault = tmp_path / "vault"
        self._run(vault)
        note = vault / "Alpha.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace("> hl", "> edited"),
            encoding="utf-8",
        )

        assert self._run(vault) == exits.SUCCESS
        assert notes.sidecar_for(note).is_file()


#: A reader's own note saved in Windows-1252: not UTF-8, so not decodable.
NOT_UTF8 = "Café notes\n".encode("cp1252")


class TestANoteThatIsNotUtf8IsLeftAlone:
    """
    A file at a note's path that does not decode raised UnicodeDecodeError,
    which the read's handler did not name. It ended the whole run, and every
    later book's note was lost with it.
    """

    def test_it_is_unreadable_and_later_books_still_get_their_notes(
        self, tmp_path: Path
    ):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Alpha.md").write_bytes(NOT_UTF8)
        named = [*ALONE, Assignment(Path("Beta.epub"), "Beta.epub", "beta")]
        found = [_highlight("Alpha.epub", "a"), _highlight("Beta.epub", "b")]

        code = notes.write_vault(found, str(vault), named, copyable=())

        assert code == exits.FAILED
        assert (vault / "Alpha.md").read_bytes() == NOT_UTF8
        assert (vault / "Beta.md").is_file()

    def test_one_at_the_sidecar_path_is_left_alone_too(self, tmp_path: Path):
        vault = tmp_path / "vault"
        found = [_highlight("Alpha.epub", "hl")]
        notes.write_vault(found, str(vault), ALONE, copyable=())
        note = vault / "Alpha.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace("> hl", "> edited"),
            encoding="utf-8",
        )
        sidecar = notes.sidecar_for(note)
        sidecar.write_bytes(NOT_UTF8)

        code = notes.write_vault(found, str(vault), ALONE, copyable=())

        assert code == exits.FAILED
        assert sidecar.read_bytes() == NOT_UTF8


def _without_frontmatter(note: str) -> str:
    return note[note.index("<!-- ibook2epub sha256=") :]


class TestAReaderMayDeleteTheFrontmatter:
    """
    The frontmatter is the reader's, so removing every property -- and the
    fences with them -- is theirs to do. The note was then called "not written
    by ibook2epub", the run exited 1 every time, and the note was never
    updated again.
    """

    def test_the_note_is_still_recognised_as_ours(self):
        note = _without_frontmatter(notes.compose([_highlight("Alpha.epub", "hl")]))

        assert noteformat.wrote_it(note) is True
        assert noteformat.is_ours(note) is True

    def test_an_edit_inside_the_generated_region_is_still_detected(self):
        note = _without_frontmatter(notes.compose([_highlight("Alpha.epub", "hl")]))
        edited = note.replace("> hl", "> edited")

        assert noteformat.wrote_it(edited) is True
        assert noteformat.is_ours(edited) is False

    def test_a_vault_run_updates_it_and_leaves_the_frontmatter_deleted(
        self, tmp_path: Path
    ):
        # Written once and never rewritten, as the README promises: putting
        # it back would undo the reader's edit on every run.
        vault = tmp_path / "vault"
        found = [_highlight("Alpha.epub", "first")]
        notes.write_vault(found, str(vault), ALONE, copyable=())
        note = vault / "Alpha.md"
        note.write_text(
            _without_frontmatter(note.read_text(encoding="utf-8")) + "mine\n",
            encoding="utf-8",
        )
        found.append(_highlight("Alpha.epub", "second"))

        code = notes.write_vault(found, str(vault), ALONE, copyable=())

        updated = note.read_text(encoding="utf-8")
        assert code == exits.SUCCESS
        assert updated.startswith("<!-- ibook2epub sha256=")
        assert "> second" in updated
        assert updated.endswith(f"{noteformat.END_MARKER}\nmine\n")
        assert sorted(path.name for path in vault.iterdir()) == ["Alpha.md"]
