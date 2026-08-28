"""
Tests for rendering annotations as Markdown notes.

A note in a vault is not like the other things this tool writes. Every other
output is a file only it writes; a note is a file the reader writes in too, and
clobbering their work is the one unrecoverable failure here, because nothing
upstream can regenerate what they wrote.

So the file is four regions with an owner each: the frontmatter is the reader's
and Obsidian's, the generated body is the tool's, and everything below the end
marker is the reader's again. The tool hashes only the region it owns, and
preserves the other two byte for byte.

The rest of this file is the escaping. A blockquote does not neutralise a line
that opens a heading, and text that reaches the body from a book or a reader
could otherwise forge the marker that ends the tool's own region.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods
# Several rules live in private helpers, and the defect being pinned is in
# the helper rather than in what the public function does with it.
# pylint: disable=protected-access

import os
import signal
from pathlib import Path
from typing import Any

import pytest

from epubconvert import app_logger, notes
from epubconvert.archive import write_atomically
from epubconvert.planning import Assignment

# ------------------------------------------------------------------ rendering


def _annotation(**overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": "U1",
        "book": {"title": "Leviathan Wakes", "author": "James S. A. Corey"},
        "text": "Summary roadside justice",
        "created": "2018-12-25T22:44:28Z",
    }
    item.update(overrides)
    return item


class TestFrontmatter:
    def test_a_title_holding_a_colon_stays_one_property(self):
        # 298 titles in a surveyed library contain ": ", which starts a mapping.
        # Unquoted, Obsidian shows the note as having no properties at all.
        book = {"title": "A Dance with Dragons: A Song of Ice and Fire: Book Five"}

        assert '"A Dance with Dragons: A Song of Ice and Fire: Book Five"' in (
            notes.frontmatter(book)
        )

    @pytest.mark.parametrize(
        "title",
        [
            "A Dog's Way Home",
            'He said "hello"',
            "- leading dash",
            "true",
            "12345",
            "back\\slash",
        ],
    )
    def test_every_value_from_the_book_is_quoted(self, title: str):
        rendered = notes.frontmatter({"title": title})
        line = next(
            line_ for line_ in rendered.split("\n") if line_.startswith("title:")
        )

        assert line.startswith('title: "')
        assert line.endswith('"')

    def test_tool_owned_literals_are_bare(self):
        rendered = notes.frontmatter({"title": "T"})

        assert "category: book" in rendered
        assert "tags: [books]" in rendered
        assert "source: ibook2epub" in rendered

    def test_the_year_is_bare_because_it_is_already_an_integer(self):
        assert "year: 2011" in notes.frontmatter({"title": "T", "year": 2011})

    def test_a_year_that_is_not_an_integer_is_refused_at_the_boundary(self):
        # _book_of already suppresses TypeError/ValueError so this cannot
        # happen, and the guarantee is enforced where it is relied on.
        with pytest.raises(TypeError):
            notes.frontmatter({"title": "T", "year": "2011"})

    def test_an_isbn_is_surfaced_when_the_identifier_is_one(self):
        rendered = notes.frontmatter(
            {"title": "T", "identifier": "urn:isbn:9780553905656"}
        )

        assert 'identifier: "urn:isbn:9780553905656"' in rendered
        assert 'isbn: "9780553905656"' in rendered

    def test_a_uuid_identifier_yields_no_isbn(self):
        rendered = notes.frontmatter(
            {"title": "T", "identifier": "urn:uuid:583403b1-8c27-4dd0-a3f5"}
        )

        assert "isbn:" not in rendered

    @pytest.mark.parametrize("absent", ["author", "language", "year", "identifier"])
    def test_a_missing_value_omits_its_key(self, absent: str):
        assert f"{absent}:" not in notes.frontmatter({"title": "T"})

    def test_the_declared_identifier_has_no_place_in_a_note(self):
        rendered = notes.frontmatter(
            {
                "title": "T",
                "identifier": "urn:isbn:9780553905656",
                "declaredIdentifier": "978-0-553-90565-6",
            }
        )

        assert "declaredIdentifier" not in rendered

    def test_nothing_that_moves_with_the_clock_is_recorded(self):
        rendered = notes.frontmatter({"title": "T"})

        assert "exported" not in rendered
        assert "highlights:" not in rendered

    def test_the_fences_are_there_and_the_block_starts_at_byte_zero(self):
        rendered = notes.frontmatter({"title": "T"})

        assert rendered.startswith("---\n")
        assert rendered.endswith("---\n")


class TestBody:
    def test_the_highlight_is_a_blockquote(self):
        body = notes.body([_annotation()])

        assert "> Summary roadside justice" in body

    def test_every_line_of_a_multi_line_highlight_is_prefixed(self):
        body = notes.body([_annotation(text="First line.\nSecond line.")])

        assert "> First line." in body
        assert "> Second line." in body
        assert "\nSecond line." not in body

    def test_carriage_returns_are_normalised_first(self):
        body = notes.body([_annotation(text="First.\r\nSecond.\rThird.")])

        assert "\r" not in body
        for line in ("> First.", "> Second.", "> Third."):
            assert line in body

    def test_the_readers_note_is_outside_the_quotation(self):
        # A blockquote is a claim about who said something. Inside it, the
        # reader's own words render as part of the passage from the book.
        body = notes.body([_annotation(note="my own thought")])
        quoted = [line_ for line_ in body.split("\n") if line_.startswith(">")]

        assert "**Note:** my own thought" in body
        assert not any("my own thought" in line_ for line_ in quoted)

    def test_every_line_of_a_multi_line_note_stays_in_the_note(self):
        body = notes.body([_annotation(note="First thought.\nSecond thought.")])

        assert "**Note:** First thought." in body
        assert "Second thought." in body

    def test_chapters_become_headings(self):
        body = notes.body([_annotation(chapter="Chapter Fifteen: Holden")])

        assert "## Chapter Fifteen: Holden" in body

    def test_a_book_with_no_chapters_gets_one_heading(self):
        assert "## Highlights" in notes.body([_annotation()])

    def test_a_heading_is_emitted_whenever_the_chapter_changes(self):
        # A reader who returns to an earlier chapter gets a repeated heading
        # rather than reordered highlights: the order is theirs.
        body = notes.body(
            [
                _annotation(id="a", chapter="Three", text="first"),
                _annotation(id="b", chapter="Nine", text="second"),
                _annotation(id="c", chapter="Three", text="third"),
            ]
        )
        headings = [line_ for line_ in body.split("\n") if line_.startswith("## ")]

        assert headings == ["## Three", "## Nine", "## Three"]
        assert body.index("first") < body.index("second") < body.index("third")

    def test_the_title_and_author_head_the_note(self):
        body = notes.body([_annotation()])

        assert body.startswith("# Leviathan Wakes\n")
        assert "*James S. A. Corey*" in body

    def test_a_book_with_no_author_drops_the_subtitle(self):
        body = notes.body([_annotation(book={"title": "Leviathan Wakes"})])

        assert "*" not in body.split("##", maxsplit=1)[0].replace(
            "# Leviathan Wakes", ""
        )


class TestEscaping:
    """
    One rule for every book- or reader-derived string that reaches the body:
    normalise its newlines, escape what would open a block, escape what would
    forge a marker.
    """

    @pytest.mark.parametrize(
        "opener",
        [
            "# not a heading",
            "- not a list",
            "+ not a list",
            "> not a quote",
            "1. not a list",
        ],
    )
    def test_a_highlight_cannot_open_a_block_inside_the_quote(self, opener: str):
        body = notes.body([_annotation(text=opener)])
        line = next(line_ for line_ in body.split("\n") if "not a" in line_)

        assert line.startswith("> \\")

    @pytest.mark.parametrize("opener", ["# not a heading", "- not a list"])
    def test_a_note_cannot_open_a_block_either(self, opener: str):
        body = notes.body([_annotation(note=f"first\n{opener}")])

        assert "\n\\" in body

    def test_a_highlight_cannot_forge_the_end_marker(self):
        body = notes.body([_annotation(text=f"a\n{notes.END_MARKER}")])

        assert not any(
            line_.startswith("<!-- ibook2epub end") for line_ in body.split("\n")
        )

    def test_a_note_cannot_forge_the_end_marker(self):
        # The blockquote prefix protects highlights; notes had no rule at all,
        # so their second line sat at column zero in the generated region.
        body = notes.body([_annotation(note=f"first\n{notes.END_MARKER}")])

        assert not any(
            line_.startswith("<!-- ibook2epub end") for line_ in body.split("\n")
        )

    def test_a_title_containing_a_newline_gives_a_single_line_heading(self):
        # usable_title trims but does not collapse internal newlines.
        body = notes.body([_annotation(book={"title": "A Title\nwith a newline"})])

        assert body.startswith("# A Title with a newline\n")

    def test_an_author_containing_a_newline_is_collapsed_too(self):
        body = notes.body([_annotation(book={"title": "T", "author": "A\nWriter"})])

        assert "*A Writer*" in body

    def test_a_chapter_containing_a_newline_is_collapsed_too(self):
        body = notes.body([_annotation(chapter="Ch\nOne")])

        assert "## Ch One" in body


# ------------------------------------------------------------------ ownership


class TestTheFourRegions:
    def test_the_start_marker_sits_below_the_frontmatter(self):
        # Frontmatter is recognised only at byte 0. A marker on line 1 turns
        # the fences into a thematic break and every property disappears.
        note = notes.compose([_annotation()])

        assert note.startswith("---\n")
        lines = note.split("\n")
        close = lines.index("---", 1)
        assert lines[close + 1].startswith("<!-- ibook2epub sha256=")

    def test_a_new_note_carries_an_end_marker_even_with_nothing_below_it(self):
        # Without one the reader has no signposted place to write, and their
        # first paragraph lands inside the generated region.
        assert notes.END_MARKER in notes.compose([_annotation()])

    def test_a_freshly_written_note_is_recognised_as_ours(self):
        assert notes.is_ours(notes.compose([_annotation()])) is True

    def test_the_regions_divide_the_whole_file(self):
        note = notes.compose([_annotation()], tail=f"{notes.END_MARKER}\nmine\n")
        held = notes.split(note)

        assert held is not None
        assert (
            held.head
            + f"<!-- ibook2epub sha256={held.digest} -->\n"
            + held.generated
            + held.tail
            == note
        )


class TestTheReadersRegionsSurvive:
    def test_editing_the_frontmatter_does_not_make_a_note_unrecognisable(self):
        # Obsidian's properties UI rewrites frontmatter whenever anyone adds a
        # tag, and tagging a new note is the first thing a reader does.
        note = notes.compose([_annotation()])
        tagged = note.replace("---\n", "---\ntags:\n  - fantasy\naliases: [LW]\n", 1)

        assert notes.is_ours(tagged) is True

    def test_prose_below_the_end_marker_survives_a_new_highlight(self):
        note = notes.compose([_annotation()])
        note += "My own thinking, at length.\n"
        assert notes.is_ours(note) is True

        updated = notes.rewrite(
            note, [_annotation(), _annotation(id="U2", text="a second")]
        )

        assert updated.endswith("My own thinking, at length.\n")
        assert "a second" in updated

    def test_the_readers_frontmatter_survives_a_rewrite_byte_for_byte(self):
        note = notes.compose([_annotation()])
        tagged = note.replace("---\n", "---\ntags:\n  - fantasy\n", 1)

        updated = notes.rewrite(tagged, [_annotation()])

        assert "tags:\n  - fantasy" in updated

    def test_a_title_corrected_upstream_does_not_overwrite_the_frontmatter(self):
        # Frontmatter is written once. Deleting the note is the refresh.
        note = notes.compose([_annotation()])
        updated = notes.rewrite(
            note, [_annotation(book={"title": "A Different Title"})]
        )

        assert 'title: "Leviathan Wakes"' in updated
        assert "# A Different Title" in updated


class TestEditsInsideTheGeneratedRegionAreDetected:
    def test_editing_the_body_is_detected(self):
        note = notes.compose([_annotation()])
        edited = note.replace(
            "> Summary roadside justice",
            "> Summary roadside justice\n\nmy thought here",
        )

        assert notes.is_ours(edited) is False

    def test_deleting_the_end_marker_is_treated_as_an_edit(self):
        note = notes.compose([_annotation()])

        assert notes.is_ours(note.replace(notes.END_MARKER, "")) is False

    def test_moving_prose_above_the_end_marker_is_detected(self):
        note = notes.compose([_annotation()])
        moved = note.replace(notes.END_MARKER, f"mine\n\n{notes.END_MARKER}")

        assert notes.is_ours(moved) is False

    def test_a_file_this_tool_never_wrote_is_not_ours(self):
        assert notes.is_ours("---\ntitle: mine\n---\n\n# My own note\n") is False
        assert notes.split("just some prose\n") is None


class TestARerunThatChangesNothingRendersTheSameBytes:
    def test_the_same_annotations_give_the_same_note(self):
        assert notes.compose([_annotation()]) == notes.compose([_annotation()])

    def test_rewriting_an_unchanged_note_is_a_no_op(self):
        note = notes.compose([_annotation()])

        assert notes.rewrite(note, [_annotation()]) == note


class TestEncoding:
    @pytest.mark.parametrize(
        "mangle",
        [
            lambda t: t.replace("\n", "\r\n"),
            lambda t: t.replace("\n", "\r"),
            lambda t: "﻿" + t,
        ],
    )
    def test_a_note_that_round_tripped_through_another_tool_is_still_ours(self, mangle):
        # Reported as "not written by ibook2epub" about a file it wrote.
        note = notes.compose([_annotation()])
        mangled = mangle(note)
        if mangled.startswith("﻿"):
            mangled = mangled.encode("utf-8").decode("utf-8-sig")

        assert notes.is_ours(mangled) is True

    def test_what_this_tool_writes_has_no_carriage_returns(self):
        assert "\r" not in notes.compose([_annotation()])


class TestThingsThatGoWrong:
    def test_rewriting_a_file_this_tool_never_wrote_is_refused(self):
        with pytest.raises(ValueError):
            notes.rewrite("# just a note\n", [_annotation()])

    def test_frontmatter_that_closes_at_end_of_file_is_not_ours(self):
        assert notes.split('---\ntitle: "T"\n---') is None

    def test_frontmatter_that_never_closes_is_not_ours(self):
        assert notes.split('---\ntitle: "T"\n') is None

    def test_a_note_with_no_end_marker_is_not_ours(self):
        note = notes.compose([_annotation()])
        assert notes.split(note.replace(notes.END_MARKER, "")) is None


class TestOutcomesTheCallerCanTrust:
    """
    ``write_vault`` counts what ``_write_one`` returns and tells the reader
    what happened. Every one of those sentences has to be true, because the
    reader acts on them: "your new highlights are in a .md.new beside it" is
    a promise that something exists.
    """

    def test_a_note_missing_only_its_end_marker_is_ours_and_kept(self, tmp_path):
        # split() returning None conflated "not written by this tool" with
        # "written by it and since edited", so the sidecar was never written
        # and the reader was told the file was somebody else's.
        target = tmp_path / "Book.md"
        target.write_text(
            notes.compose([_annotation()]).replace(notes.END_MARKER, ""),
            encoding="utf-8",
        )

        assert notes.wrote_it(target.read_text(encoding="utf-8")) is True

    def test_a_file_this_tool_never_wrote_is_not_claimed(self):
        assert notes.wrote_it("# my own note\n") is False

    def test_a_fifo_is_not_read(self, tmp_path):
        # read_text on a FIFO blocks until a writer appears, which is never.
        # One of those in a vault froze the whole run.
        fifo = tmp_path / "F.md"
        os.mkfifo(fifo)

        def blocked(*_):
            raise AssertionError("the read blocked")

        signal.signal(signal.SIGALRM, blocked)
        signal.alarm(3)
        try:
            assert notes.readable(fifo) is False
        finally:
            signal.alarm(0)

    def test_a_directory_named_like_a_note_is_not_read(self, tmp_path):
        (tmp_path / "D.md").mkdir()
        assert notes.readable(tmp_path / "D.md") is False

    def test_an_ordinary_note_is_readable(self, tmp_path):
        target = tmp_path / "N.md"
        target.write_text("hello\n", encoding="utf-8")
        assert notes.readable(target) is True

    def test_a_note_larger_than_the_cap_is_not_read(self, tmp_path):
        # No cap meant a planted or runaway file was read whole every run.
        target = tmp_path / "Big.md"
        target.write_text("x" * (notes.MAX_NOTE_BYTES + 1), encoding="utf-8")

        assert notes.readable(target) is False

    def test_a_note_at_the_cap_is_still_read(self, tmp_path):
        target = tmp_path / "AtCap.md"
        target.write_text("x" * notes.MAX_NOTE_BYTES, encoding="utf-8")

        assert notes.readable(target) is True


class TestTheSidecarCannotTakeAnotherBooksName:
    def test_the_sidecar_name_is_one_no_book_can_claim(self):
        # A book titled "Foo.new" is named "Foo.new.md", which was exactly the
        # sidecar name for a book titled "Foo" -- so writing one book's
        # sidecar overwrote a different book's note with the wrong content.
        assert notes.sidecar_for(Path("Foo.md")).name != "Foo.new.md"

    def test_two_different_books_cannot_share_a_sidecar(self):
        first = notes.sidecar_for(Path("Foo.md"))
        second = notes.sidecar_for(Path("Foo.new.md"))

        assert first != second

    def test_a_sidecar_is_not_a_markdown_note_the_next_run_will_adopt(self):
        # Whatever it is called, it must not be picked up as some book's
        # primary note on a later run.
        assert not notes.sidecar_for(Path("Foo.md")).name.endswith(".new.md")


class TestFailuresThatDoNotNeedAPermissionBit:
    """
    The same failures, driven without ``chmod``.

    CI runs as root, where permission bits are ignored, so every test that
    reaches these branches by making a file unwritable skips there -- which is
    how the branches that handle a failed write went uncovered on the machine
    that gates the build.
    """

    def _annotations(self) -> list[dict[str, Any]]:
        return [_annotation()]

    def test_a_write_that_fails_costs_one_note_not_the_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        def refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(notes, "write_atomically", refuse)

        assert notes._write_one(tmp_path / "N.md", self._annotations()) == "failed"

    def test_a_read_that_fails_is_not_called_somebody_elses_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        target = tmp_path / "N.md"
        target.write_text(notes.compose(self._annotations()), encoding="utf-8")

        def refuse(*_args: object, **_kwargs: object) -> str:
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(Path, "read_text", refuse)

        assert notes._write_one(target, self._annotations()) == "unreadable"

    def test_a_path_that_cannot_be_stat_ed_is_not_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        target = tmp_path / "N.md"
        target.write_text("x", encoding="utf-8")

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(Path, "stat", refuse)

        assert notes.readable(target) is False

    def test_an_unwritable_sidecar_is_reported_as_blocked_not_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The reader is told their highlights are in a file beside the note.
        # If that file could not be written, saying so is a false promise.
        target = tmp_path / "N.md"
        target.write_text(
            notes.compose(self._annotations()).replace("> Summary", "> edited"),
            encoding="utf-8",
        )

        def refuse_the_sidecar(path: Path, text: str) -> None:
            if path.name.endswith(notes.SIDECAR_SUFFIX):
                raise OSError(28, "No space left on device")
            write_atomically(path, text)

        monkeypatch.setattr(notes, "write_atomically", refuse_the_sidecar)

        assert notes._write_one(target, self._annotations()) == "blocked"

    def test_an_oversized_file_is_skipped_with_a_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        app_logger.configure(verbosity=1)
        target = tmp_path / "Big.md"
        target.write_text("x" * (notes.MAX_NOTE_BYTES + 1), encoding="utf-8")

        assert notes._write_one(target, self._annotations()) == "unreadable"
        assert "not a readable note" in capsys.readouterr().err

    def test_a_directory_where_a_note_should_be_is_skipped(self, tmp_path: Path):
        (tmp_path / "D.md").mkdir()

        assert notes._write_one(tmp_path / "D.md", self._annotations()) == "unreadable"

    def test_a_vault_directory_that_cannot_be_created_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        def refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError(13, "Permission denied")

        monkeypatch.setattr(Path, "mkdir", refuse)

        assert notes.write_vault(self._annotations(), str(tmp_path / "v"), []) != 0

    def test_the_run_reports_a_failed_write(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        # The package logger does not propagate, so it has to be attached to a
        # handler for the message to reach stderr the way a real run does.
        app_logger.configure(verbosity=1)

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(notes, "write_atomically", refuse)
        named = [
            Assignment(
                package=Path("Leviathan Wakes.epub"),
                filename="Leviathan Wakes.epub",
                identity="Leviathan Wakes.epub",
            )
        ]

        code = notes.write_vault(self._annotations(), str(tmp_path / "v"), named)

        assert code != 0
        assert "could not be written" in capsys.readouterr().err


class TestTheDocumentedSidecarNameIsTheRealOne:
    """
    The suffix is defined once in code and described in prose in three places,
    which is how the two drifted apart: the sidecar was renamed and the README
    went on naming the old one, sending readers to look for a file that is
    never written.

    Derived rather than restated, so a future rename fails here instead of
    going quiet.
    """

    def _readme(self) -> str:
        return (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )

    def test_the_readme_names_the_suffix_the_code_defines(self):
        assert notes.SIDECAR_SUFFIX in self._readme()

    def test_the_readme_names_no_other_sidecar_suffix(self):
        # The README describes only current behaviour -- it has no section
        # explaining what the sidecar used to be called, unlike the changelog.
        # ".new" alone is a substring of the real suffix, so the check names
        # the shape a sidecar could plausibly be called instead.
        stale = {
            candidate
            for candidate in (".new.md", ".md.old", ".sidecar.md")
            if candidate != notes.SIDECAR_SUFFIX and candidate in self._readme()
        }

        assert stale == set()

    def test_the_suffix_is_what_sidecar_for_actually_appends(self):
        assert notes.sidecar_for(Path("Book.md")).name == "Book" + notes.SIDECAR_SUFFIX
