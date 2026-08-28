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

from typing import Any

import pytest

from epubconvert import notes

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
