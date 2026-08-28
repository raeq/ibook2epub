"""
Tests for the defects a hardening review found in the annotations work.

Three themes run through these, and they are worth naming because each one is
a rule the rest of the codebase already keeps and this feature did not.

**Nothing read from Apple is trusted.** The two SQLite databases are
undocumented, their columns are untyped, and a filename inside the container is
enough to change how a database is opened. One unusable row must cost one
annotation, never the whole export.

**A rule is enforced at every route into it.** ``--dry-run`` reached
``_apply_annotations`` by two paths and was checked on one of them.
``schema_problems`` restated part of the schema instead of deriving it. Both
are the shape this project calls a parallel rule.

**Nothing already written is destroyed.** The detached export is the artifact
the merge machinery exists to protect, and it was written with a call that
truncates first.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods
# Several rules here live in private helpers, and the defect being pinned is in
# the helper rather than in what the public function does with it.
# pylint: disable=protected-access

import json
import sqlite3
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from epubconvert import __version__, annotations, archive, cli
from epubconvert.run import main
from epubconvert.validate import Package, canonical_identifier
from tests.conftest import make_metadata_package
from tests.test_annotations import highlight, library_row, make_databases

# ---------------------------------------------------------------- Apple's data


class TestNothingReadFromAppleIsTrusted:
    def test_a_database_name_cannot_smuggle_uri_parameters(self, tmp_path: Path):
        # "file:{path}?mode=ro" lets a "?" in the name inject its own
        # parameters ahead of mode=ro, which opens the database read-write.
        database = tmp_path / "AEAnnotation_v1?mode=rwc&x=.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE T (a)")
        before = sorted(p.name for p in tmp_path.iterdir())

        with pytest.raises(annotations.AnnotationsUnavailableError):
            annotations._rows(database, "SELECT * FROM MISSING")

        assert sorted(p.name for p in tmp_path.iterdir()) == before

    def test_a_percent_in_the_path_still_opens(self, tmp_path: Path):
        directory = tmp_path / "50% off"
        directory.mkdir()
        database = directory / "BKLibrary-1-1.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE T (a)")
            connection.execute("INSERT INTO T VALUES (1)")

        assert len(annotations._rows(database, "SELECT * FROM T")) == 1

    def test_the_database_is_still_opened_read_only(self, tmp_path: Path):
        database = tmp_path / "plain.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE T (a)")

        with pytest.raises(annotations.AnnotationsUnavailableError):
            annotations._rows(database, "INSERT INTO T VALUES (1)")

    def test_a_dangling_symlink_does_not_break_finding_the_newest(self, tmp_path: Path):
        real = tmp_path / "AEAnnotation_v1.sqlite"
        real.write_bytes(b"")
        (tmp_path / "AEAnnotation_gone.sqlite").symlink_to(tmp_path / "nowhere")

        assert annotations._newest(tmp_path, "AEAnnotation") == real

    @pytest.mark.parametrize("seconds", [1e18, -1e18, float("nan")])
    def test_an_out_of_range_timestamp_is_dropped_not_raised(self, seconds: float):
        assert annotations._moment(seconds) is None

    def test_the_epoch_boundary_still_converts(self):
        assert annotations._moment(0) == "2001-01-01T00:00:00Z"
        assert annotations._moment(-annotations.APPLE_EPOCH_OFFSET) == (
            "1970-01-01T00:00:00Z"
        )

    @pytest.mark.parametrize("year", ["2011-03-01", "MMXI", None, ""])
    def test_a_year_that_is_not_a_number_is_left_out(self, year: object):
        book = annotations._book_of(
            "A", {"A": {"ZTITLE": "T", "ZYEAR": year}}, None, None
        )
        assert "year" not in book

    def test_a_year_that_is_a_number_is_kept(self):
        book = annotations._book_of(
            "A", {"A": {"ZTITLE": "T", "ZYEAR": "2011"}}, None, None
        )
        assert book["year"] == 2011

    def test_an_unusable_row_costs_one_annotation_not_the_export(self, tmp_path: Path):
        # Whole-export loss is the failure mode: collect() built the list in
        # one comprehension, so a single bad row took every good one with it.
        container = make_databases(
            tmp_path / "container",
            rows=[
                highlight(uuid="GOOD1", text="a real highlight"),
                highlight(uuid="BAD", text=b"\xff\xfe not text"),
                highlight(uuid="GOOD2", text="another real one"),
            ],
        )
        found = annotations.collect(container=container)

        assert sorted(item["id"] for item in found) == ["GOOD1", "GOOD2"]

    def test_a_date_that_is_not_a_date_loses_the_date_not_the_highlight(
        self, tmp_path: Path
    ):
        # The highlight is the thing worth keeping. A creation date that will
        # not convert falls back to now rather than dropping the row.
        container = make_databases(
            tmp_path / "container", rows=[highlight(uuid="U", created="soon")]
        )
        found = annotations.collect(container=container)

        assert [item["id"] for item in found] == ["U"]
        assert found[0]["created"].endswith("Z")

    def test_a_style_that_is_not_a_number_is_left_out(self, tmp_path: Path):
        container = make_databases(
            tmp_path / "container", rows=[highlight(uuid="U", style="blue")]
        )
        found = annotations.collect(container=container)

        assert len(found) == 1
        assert "style" not in found[0]


# ------------------------------------------------------------------- locators


class TestTextFragments:
    def test_long_text_with_no_word_boundary_is_quoted_whole(self):
        # _leading_words and _trailing_words each take the first word
        # unconditionally, so a string with no spaces produced "text=X,X" --
        # a start and end that are the same, which selects nothing.
        text = "これは非常に長い日本語のハイライトです" * 5
        locator = annotations.text_fragment(text)

        assert "," not in locator

    def test_a_long_url_is_quoted_whole(self):
        text = "https://example.com/" + "a" * 88
        assert "," not in annotations.text_fragment(text)

    def test_long_text_with_word_boundaries_is_still_quoted_by_its_ends(self):
        text = (
            "The dead give up their secrets slowly, and the Belt keeps its "
            "own counsel longer than most stations care to wait for it."
        )
        locator = annotations.text_fragment(text)

        assert "," in locator
        start, _, end = locator[len(":~:text=") :].partition(",")
        assert start != end

    def test_short_text_is_quoted_whole(self):
        assert annotations.text_fragment("a short one") == ":~:text=a%20short%20one"

    @pytest.mark.parametrize("text", ["", "   \n\t "])
    def test_whitespace_only_text_yields_no_locator(self, text: str):
        assert annotations.text_fragment(text) == ""

    def test_an_empty_locator_is_never_recorded(self, tmp_path: Path):
        container = make_databases(
            tmp_path / "container", rows=[highlight(uuid="U", text="   ")]
        )
        found = annotations.collect(container=container)

        assert all("locator" not in item for item in found)


class TestCfiResolution:
    def test_the_last_assertion_before_the_bang_wins(self):
        # The leftmost bracket is a spine-level assertion, not the document.
        cfi = "epubcfi(/6[spine]/46[ch15.xhtml]!/4/2/1:0)"
        assert annotations._assertion_of(cfi) == "ch15.xhtml"

    def test_apples_usual_shape_still_resolves(self):
        cfi = "epubcfi(/6/46[ch15.xhtml]!/4,/80/2/1:25,/82/2/1:25)"
        assert annotations._assertion_of(cfi) == "ch15.xhtml"

    def test_an_assertion_after_the_bang_is_not_the_document(self):
        assert annotations._assertion_of("epubcfi(/6/46!/4/2[ch15]/1:0)") is None

    def test_a_cfi_with_no_assertion_resolves_to_nothing(self):
        assert annotations._assertion_of("epubcfi(/6/46!/4/2/1:0)") is None


class TestHrefsStayInsideTheBook:
    def test_a_manifest_href_that_climbs_out_is_refused(self):
        book = Package(
            opf_path="content.opf",
            manifest={"ch15": "../../../../../../etc/passwd"},
        )
        cfi = "epubcfi(/6/46[ch15]!/4/2/1:0)"

        assert annotations._href_of(cfi, book) is None

    def test_an_ordinary_href_still_resolves(self):
        book = Package(
            opf_path="OPS/content.opf", manifest={"ch15": "OPS/Text/chapter15.xhtml"}
        )
        cfi = "epubcfi(/6/46[ch15]!/4/2/1:0)"

        assert annotations._href_of(cfi, book) == "OPS/Text/chapter15.xhtml"


# --------------------------------------------------------------- book identity


class TestBookIdentity:
    def test_the_publications_own_identifier_is_recorded(self):
        # dc:identifier is the one key that is neither Apple's nor this run's.
        # Every other field in "book" is a name that can change.
        parsed = Package(opf_path="content.opf", identifier="urn:uuid:0f3a-4c21")
        book = annotations._book_of("A", {"A": {"ZTITLE": "T"}}, parsed, None)

        assert book["identifier"] == "urn:uuid:0f3a-4c21"

    def test_a_junk_identifier_is_not_recorded(self):
        # "none" identifies 92 books in a real library, so it identifies none.
        parsed = Package(opf_path="content.opf", identifier="none")
        book = annotations._book_of("A", {"A": {"ZTITLE": "T"}}, parsed, None)

        assert "identifier" not in book

    def test_no_identifier_is_claimed_when_the_book_cannot_be_read(self):
        book = annotations._book_of("A", {"A": {"ZTITLE": "T"}}, None, None)
        assert "identifier" not in book

    @pytest.mark.parametrize("path", ["/Users/someone/Books/..", "/", "..", ""])
    def test_a_path_that_is_not_a_book_name_is_not_published(self, path: str):
        book = annotations._book_of(
            "A", {"A": {"ZTITLE": "T", "ZPATH": path}}, None, None
        )

        assert "source" not in book
        assert "filename" not in book

    def test_a_book_with_no_title_and_no_asset_id_still_says_something(self):
        assert annotations._book_of("", {}, None, None)["title"] != ""


# --------------------------------------------------------------- the contract


class TestTheSchemaIsTheOneStatementOfTheRules:
    def test_a_book_missing_its_title_is_a_problem(self):
        document = _document([_annotation(book={})])
        assert annotations.schema_problems(document)

    def test_a_book_with_an_unknown_field_is_a_problem(self):
        document = _document([_annotation(book={"title": "T", "colour": "yellow"})])
        assert annotations.schema_problems(document)

    def test_an_empty_title_is_a_problem(self):
        document = _document([_annotation(book={"title": ""})])
        assert annotations.schema_problems(document)

    def test_an_empty_id_is_a_problem(self):
        document = _document([_annotation(id="")])
        assert annotations.schema_problems(document)

    def test_a_good_document_has_no_problems(self):
        assert annotations.schema_problems(_document([_annotation()])) == []

    def test_the_identifier_field_is_in_the_schema(self):
        schema = json.loads(annotations.SCHEMA_PATH.read_text(encoding="utf-8"))
        assert "identifier" in schema["$defs"]["book"]["properties"]

    def test_a_real_export_validates_against_the_shipped_schema(self, tmp_path: Path):
        container = make_databases(tmp_path / "container")
        document = annotations.build_document(annotations.collect(container=container))

        assert annotations.schema_problems(document) == []


def _annotation(**overrides: object) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": "U1",
        "book": {"title": "Leviathan Wakes"},
        "text": "Summary roadside justice",
        "created": "2018-12-25T22:44:28Z",
    }
    item.update(overrides)
    return item


def _document(found: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generator": {"name": "ibook2epub", "version": "2.0.4"},
        "generated": "2026-08-28T10:00:00Z",
        "annotations": found,
    }


# -------------------------------------------------------------------- merging


class TestMergingSurvivesAHostileFile:
    @pytest.mark.parametrize(
        "existing",
        [
            {"annotations": [{"id": "X"}]},
            {"annotations": [{"id": "X", "book": "not a dict"}]},
            {"annotations": [{"id": "X", "book": {"title": [1]}}]},
            {"annotations": ["a bare string"]},
            {"annotations": {"not": "a list"}},
            {"annotations": [{"id": "X", "book": {"title": "T"}}]},
        ],
    )
    def test_a_malformed_entry_does_not_crash_the_merge(self, existing: dict[str, Any]):
        merged, tally = annotations.merge(existing, [])
        assert isinstance(merged, list)
        assert sum(tally.values()) >= 0

    def test_a_stale_shelf_name_counts_as_changed(self):
        # The "unchanged" branch kept the old entry wholesale, so book.filename
        # went stale after a --name-by change and was reported as unchanged.
        was = _annotation(
            book={"title": "T", "source": "T.epub", "filename": "T.epub"},
            modified="2020-01-01T00:00:00Z",
        )
        now = _annotation(
            book={"title": "T", "source": "T.epub", "filename": "Corey - T.epub"},
            modified="2020-01-01T00:00:00Z",
        )
        existing = {
            "generator": {"name": "ibook2epub", "version": __version__},
            "annotations": [was],
        }

        merged, tally = annotations.merge(existing, [now])

        assert tally["updated"] == 1
        assert merged[0]["book"]["filename"] == "Corey - T.epub"

    def test_an_unchanged_annotation_is_still_counted_unchanged(self):
        item = _annotation(modified="2020-01-01T00:00:00Z")
        existing = {
            "generator": {"name": "ibook2epub", "version": __version__},
            "annotations": [item],
        }

        _, tally = annotations.merge(existing, [dict(item)])

        assert tally["unchanged"] == 1

    def test_an_annotation_books_has_lost_is_kept(self):
        orphan = _annotation(id="GONE")
        existing = {
            "generator": {"name": "ibook2epub", "version": __version__},
            "annotations": [orphan],
        }

        merged, tally = annotations.merge(existing, [])

        assert tally["kept"] == 1
        assert merged[0]["id"] == "GONE"


class TestPickingOutOneBook:
    def test_two_books_with_the_same_name_do_not_share_annotations(self):
        # Matching on the basename gave one highlight to every book whose
        # package directory happened to have the same name.
        mine = _annotation(book={"title": "A", "source": "Leviathan Wakes.epub"})
        index = annotations.index_by_book([mine])

        assert annotations.for_book("Leviathan Wakes.epub", index) == [mine]
        assert annotations.for_book("Other.epub", index) == []

    def test_a_book_the_library_forgot_still_matches_on_its_title(self):
        orphan = _annotation(book={"title": "Leviathan Wakes"})
        index = annotations.index_by_book([orphan])

        assert annotations.for_book("Leviathan Wakes.epub", index) == [orphan]

    def test_the_index_answers_every_book_in_one_pass(self):
        found = [
            _annotation(id=str(n), book={"title": f"B{n}", "source": f"B{n}.epub"})
            for n in range(50)
        ]
        index = annotations.index_by_book(found)

        for n in range(50):
            assert len(annotations.for_book(f"B{n}.epub", index)) == 1


# -------------------------------------------------------------------- archives


class TestRefreshingAnArchive:
    def test_an_annotation_set_this_run_did_not_author_is_not_deleted(
        self, tmp_path: Path
    ):
        target = _book_with(tmp_path, held=b'{"annotations": [{"id": "THEIRS"}]}')

        assert archive.replace_annotations(target, []) is False

        with ZipFile(target) as reading:
            assert annotations.EMBEDDED_PATH in reading.namelist()

    @pytest.mark.parametrize(
        "held", [b"[1, 2]", b'"a string"', b"[" * 200000, b"not json at all"]
    )
    def test_a_malformed_embedded_set_does_not_abort_the_run(
        self, tmp_path: Path, held: bytes
    ):
        target = _book_with(tmp_path, held=held)
        mine = [_annotation()]

        assert archive.replace_annotations(target, mine) is True

    def test_an_unchanged_set_is_not_rewritten(self, tmp_path: Path):
        target = _book_with(tmp_path)
        mine = [_annotation()]
        archive.replace_annotations(target, mine)
        before = target.read_bytes()

        assert archive.replace_annotations(target, mine) is False
        assert target.read_bytes() == before

    def test_a_refreshed_archive_is_byte_reproducible(self, tmp_path: Path):
        mine = [_annotation()]
        first = _book_with(tmp_path / "a")
        second = _book_with(tmp_path / "b")
        archive.replace_annotations(first, mine)
        archive.replace_annotations(second, mine)

        assert first.read_bytes() == second.read_bytes()

    def test_an_embedded_set_does_not_move_with_the_clock(self):
        # The envelope stamped "generated" with the current instant, so the
        # same library exported twice produced different bytes.
        mine = [_annotation()]
        held = annotations.embedded_json(mine)

        assert "generated" not in json.loads(held)


def _book_with(directory: Path, held: bytes | None = None) -> Path:
    """A minimal valid archive on the shelf, optionally already annotated."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "Leviathan Wakes.epub"
    with ZipFile(target, "w") as writing:
        writing.writestr(archive.entry("mimetype", ZIP_STORED), "application/epub+zip")
        writing.writestr(
            archive.entry("META-INF/container.xml", ZIP_DEFLATED),
            '<?xml version="1.0"?><container xmlns='
            '"urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="content.opf" media-type='
            '"application/oebps-package+xml"/></rootfiles></container>',
        )
        writing.writestr(archive.entry("content.opf", ZIP_DEFLATED), "<opf/>")
        if held is not None:
            writing.writestr(
                archive.entry(annotations.EMBEDDED_PATH, ZIP_DEFLATED), held
            )
    return target


# ------------------------------------------------------------------- the run


class TestNothingAlreadyWrittenIsDestroyed:
    def _library(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """A one-book library, with a container holding one highlight for it."""
        library = tmp_path / "lib"
        make_metadata_package(library, "Leviathan Wakes.epub", title="Leviathan Wakes")
        make_databases(
            tmp_path / "container",
            rows=[highlight()],
            books=[library_row(path="/x/Leviathan Wakes.epub")],
        )
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        return library

    def test_the_export_is_never_written_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The whole defect: write_text truncates before it writes, so any
        # failure partway left the export as a prefix of itself -- neither
        # valid JSON nor recoverable, and every later run then refused to
        # write to that path at all. Nothing may open the target for writing;
        # a complete file is moved over it instead.
        library = self._library(tmp_path, monkeypatch)
        target = tmp_path / "export.json"
        written: list[Path] = []
        moved: list[tuple[Path, Path]] = []
        real_write, real_replace = Path.write_text, Path.replace

        def record_write(self, *args, **kwargs):
            written.append(Path(self))
            return real_write(self, *args, **kwargs)

        def record_replace(self, other, *args, **kwargs):
            moved.append((Path(self), Path(other)))
            return real_replace(self, other, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", record_write)
        monkeypatch.setattr(Path, "replace", record_replace)
        assert main(["-s", str(library), "-ao", str(target), "-q"]) == 0

        assert target not in written
        assert any(destination == target for _, destination in moved)
        assert json.loads(target.read_text(encoding="utf-8"))["annotations"]

    def test_a_failed_write_leaves_the_old_export_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = self._library(tmp_path, monkeypatch)
        target = tmp_path / "export.json"
        main(["-s", str(library), "-ao", str(target), "-q"])
        before = target.read_bytes()

        def refuse(self, *args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(Path, "write_text", refuse)
        code = main(["-s", str(library), "-ao", str(target), "-q"])

        assert code != 0
        assert target.read_bytes() == before
        assert json.loads(target.read_text(encoding="utf-8"))["annotations"]

    def test_no_partial_file_is_left_behind(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = self._library(tmp_path, monkeypatch)
        target = tmp_path / "export.json"

        def refuse(self, *args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(Path, "write_text", refuse)
        main(["-s", str(library), "-ao", str(target), "-q"])

        assert [p.name for p in tmp_path.glob(".ibook2epub-*")] == []

    @pytest.mark.parametrize(
        "content", ['["a", "list"]', '"a string"', "42", "null", "{}"]
    )
    def test_a_file_that_is_not_an_export_is_refused_not_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
    ):
        # Only malformed JSON was refused. Valid JSON of the wrong shape fell
        # through to "nothing to merge into" and was silently replaced.
        library = self._library(tmp_path, monkeypatch)
        target = tmp_path / "notes.json"
        target.write_text(content, encoding="utf-8")

        code = main(["-s", str(library), "-ao", str(target), "-q"])

        assert code != 0
        assert target.read_text(encoding="utf-8") == content

    def test_a_malformed_file_is_still_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = self._library(tmp_path, monkeypatch)
        target = tmp_path / "notes.json"
        target.write_text("{ not json", encoding="utf-8")

        assert main(["-s", str(library), "-ao", str(target), "-q"]) != 0

    def test_a_real_export_is_merged_into(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = self._library(tmp_path, monkeypatch)
        target = tmp_path / "export.json"
        main(["-s", str(library), "-ao", str(target), "-q"])

        assert main(["-s", str(library), "-ao", str(target), "-q"]) == 0
        assert len(json.loads(target.read_text(encoding="utf-8"))["annotations"]) == 1


class TestADryRunWritesNothing:
    def test_a_dry_run_does_not_rewrite_the_shelf(
        self, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # --dry-run was checked on the route through _annotations_after_export
        # and not on the -ar route, so "--dry-run -ae -ar" rewrote every
        # archive on the shelf.
        library = tmp_path / "lib"
        make_metadata_package(library, "Leviathan Wakes.epub", title="Leviathan Wakes")
        main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        make_databases(
            tmp_path / "container",
            rows=[highlight()],
            books=[library_row(path="/x/Leviathan Wakes.epub")],
        )
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        target = output_dir / "Leviathan Wakes.epub"
        before = target.read_bytes()

        code = main(
            ["-s", str(library), "-o", str(output_dir), "-ae", "-ar", "-d", "-q"]
        )

        assert code == 0
        assert target.read_bytes() == before

    def test_a_dry_run_writes_no_detached_file(
        self, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = tmp_path / "lib"
        make_metadata_package(library, "One.epub", title="One")
        make_databases(tmp_path / "container")
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        target = tmp_path / "export.json"

        main(
            ["-s", str(library), "-o", str(output_dir), "-ad", str(target), "-d", "-q"]
        )

        assert not target.exists()


class TestExitCodesMeanOneThing:
    def test_a_clean_conversion_that_cannot_read_annotations_still_succeeds(
        self, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Exit 4 is "the source directory does not exist". Reporting it after
        # every book converted told a scheduled run to go and fix a path that
        # was found and used.
        library = tmp_path / "lib"
        make_metadata_package(library, "One.epub", title="One")
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            _unreadable,
        )

        code = main(["-s", str(library), "-o", str(output_dir), "-ae", "-q"])

        assert code == 0
        assert (output_dir / "One.epub").is_file()

    def test_annotations_only_still_fails_when_it_can_read_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            _unreadable,
        )

        assert main(["-ao", str(tmp_path / "out.json"), "-q"]) != 0

    def test_a_refresh_against_a_missing_shelf_does_not_report_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A glob over a missing directory yields nothing, which read as a
        # clean run over an empty shelf.
        library = tmp_path / "lib"
        make_metadata_package(library, "One.epub", title="One")
        make_databases(tmp_path / "container")
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )

        code = main(
            ["-s", str(library), "-o", str(tmp_path / "shefl"), "-ae", "-ar", "-q"]
        )

        assert code != 0
        assert not (tmp_path / "shefl").exists()


class TestGettingHighlightsOutWithoutTheBooks:
    def test_annotations_only_does_not_need_the_library(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Its docstring says it reads Apple's container and nothing else, but
        # the source-directory check ran first, so a reader whose books are on
        # another disk could not get their highlights out at all.
        make_databases(tmp_path / "container")
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        target = tmp_path / "out.json"

        code = main(["-s", str(tmp_path / "no-such-library"), "-ao", str(target), "-q"])

        assert code == 0
        assert json.loads(target.read_text(encoding="utf-8"))["annotations"]

    def test_an_ordinary_run_still_needs_the_library(self, tmp_path: Path):
        assert main(["-s", str(tmp_path / "no-such-library"), "-q"]) != 0


class TestTheFlagsRefuseWhatTheyCannotDo:
    @pytest.mark.parametrize("flag", ["--annotations-detached", "--annotations-only"])
    def test_an_empty_filename_is_refused(self, flag: str):
        # An empty FILE is falsy, so every later check read as "not asked
        # for" and "-ad ''" quietly ran an ordinary conversion instead.
        with pytest.raises(SystemExit):
            cli.parse_args([f"{flag}="])


def _unreadable(policy: object = None) -> list[dict[str, Any]]:
    """Stand in for a container this machine cannot read."""
    raise annotations.AnnotationsUnavailableError("no Books container here")


# --------------------------------------------------- one identifier, one shape


class TestIdentifiersAreCanonical:
    """
    An identifier is only a matching key if the same book yields the same
    string. A surveyed 2,805-book library writes the same two kinds of
    identifier at least six ways: 1,092 valid ISBN-13s appear bare, as
    ``urn:isbn:``, as ``URN:ISBN:``, hyphenated, as ``ISBN 978...`` and as
    ``urn:ean:``; 1,448 UUIDs appear bare and as ``urn:uuid:``.

    Recognition is by check digit, never by shape. 68 identifiers in that
    library are 10 or 13 digits and fail their check digit, so a rule that
    counted digits would have called every one of them an ISBN.
    """

    @pytest.mark.parametrize(
        "declared",
        [
            "urn:isbn:9781449340360",
            "URN:ISBN:9781449340360",
            "9781449340360",
            "978-1-44934-036-0",
            "ISBN 9781449340360",
            "urn:ean:9781449340360",
            "  urn:isbn:978 1 44934 036 0  ",
        ],
    )
    def test_every_spelling_of_one_isbn_gives_one_string(self, declared: str):
        assert canonical_identifier(declared) == "urn:isbn:9781449340360"

    @pytest.mark.parametrize(
        "declared",
        [
            "urn:uuid:583403B1-8C27-4DD0-A3F5-3D81942B6A40",
            "583403b1-8c27-4dd0-a3f5-3d81942b6a40",
            "URN:UUID:583403b1-8c27-4dd0-a3f5-3d81942b6a40",
        ],
    )
    def test_every_spelling_of_one_uuid_gives_one_string(self, declared: str):
        assert canonical_identifier(declared) == (
            "urn:uuid:583403b1-8c27-4dd0-a3f5-3d81942b6a40"
        )

    def test_an_isbn_10_becomes_the_isbn_13_that_means_the_same_book(self):
        # Exact and reversible: prefix 978, recompute the check digit. Without
        # it the same book catalogued twice never matches itself.
        assert canonical_identifier("0-306-40615-2") == "urn:isbn:9780306406157"

    def test_an_isbn_10_ending_in_x_is_understood(self):
        assert canonical_identifier("043942089X") == "urn:isbn:9780439420891"

    @pytest.mark.parametrize(
        "declared",
        [
            "3290122707",  # 10 digits, check digit fails
            "3521884621",
            "1245016905",
            "9781449340361",  # one digit off a real ISBN-13
        ],
    )
    def test_digits_that_fail_their_check_are_not_called_an_isbn(self, declared: str):
        assert canonical_identifier(declared) == declared

    @pytest.mark.parametrize(
        "declared",
        [
            "epubmerge-uid-1557091862",
            "CBUSCESPR0000T00",
            "https://leanpub.com/D3-Tips-and-Tricks",
            "_id286448481",
            "d325dg04-25ld-g893-25ls-f987352lj590",
        ],
    )
    def test_anything_unrecognised_is_left_exactly_as_it_was(self, declared: str):
        assert canonical_identifier(declared) == declared

    def test_the_book_records_the_canonical_form(self):
        parsed = Package(opf_path="c.opf", identifier="978-1-78883-568-8")
        book = annotations._book_of("A", {"A": {"ZTITLE": "T"}}, parsed, None)

        assert book["identifier"] == "urn:isbn:9781788835688"

    def test_the_declared_form_is_kept_when_it_differed(self):
        parsed = Package(opf_path="c.opf", identifier="978-1-78883-568-8")
        book = annotations._book_of("A", {"A": {"ZTITLE": "T"}}, parsed, None)

        assert book["declaredIdentifier"] == "978-1-78883-568-8"

    def test_nothing_is_said_twice_when_the_book_was_already_canonical(self):
        parsed = Package(opf_path="c.opf", identifier="urn:isbn:9781788835688")
        book = annotations._book_of("A", {"A": {"ZTITLE": "T"}}, parsed, None)

        assert book["identifier"] == "urn:isbn:9781788835688"
        assert "declaredIdentifier" not in book

    def test_a_junk_identifier_is_still_refused_before_any_of_this(self):
        parsed = Package(opf_path="c.opf", identifier="none")
        book = annotations._book_of("A", {"A": {"ZTITLE": "T"}}, parsed, None)

        assert "identifier" not in book
        assert "declaredIdentifier" not in book

    def test_two_books_written_differently_now_match(self):
        one = annotations._book_of(
            "A",
            {"A": {"ZTITLE": "T"}},
            Package(opf_path="c.opf", identifier="urn:isbn:9781449340360"),
            None,
        )
        other = annotations._book_of(
            "B",
            {"B": {"ZTITLE": "T"}},
            Package(opf_path="c.opf", identifier="978-1-4493-4036-0"),
            None,
        )

        assert one["identifier"] == other["identifier"]

    def test_the_schema_describes_both_fields(self):
        schema = json.loads(annotations.SCHEMA_PATH.read_text(encoding="utf-8"))
        properties = schema["$defs"]["book"]["properties"]

        assert "identifier" in properties
        assert "declaredIdentifier" in properties

    def test_a_document_carrying_both_still_validates(self):
        document = _document(
            [
                _annotation(
                    book={
                        "title": "T",
                        "identifier": "urn:isbn:9781788835688",
                        "declaredIdentifier": "978-1-78883-568-8",
                    }
                )
            ]
        )

        assert annotations.schema_problems(document) == []
