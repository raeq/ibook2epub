"""
Tests for getting annotations out, and for the parts of reading them that need
a book to hand.

``test_annotations.py`` covers reading Apple's databases. This covers what a
user asks for -- five flags over three independent choices -- what those
choices do to the shelf, and the two fields that cannot be worked out from the
database alone: the href, which is resolved against the book's manifest, and
the locator, which has to survive matching a rendered DOM.

The W3C work defines two shapes and prefers neither. Detached annotations "can
be shared independently of the publication"; embedded ones, at
``META-INF/annotations.json``, are "always available to users". Both are here
because they answer different questions.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import json
import sqlite3
from pathlib import Path
from urllib.parse import unquote
from zipfile import ZIP_STORED, ZipFile

import pytest

from epubconvert import annotations, archive, run
from epubconvert.naming import (
    MetadataNaming,
    NamingPolicy,
    PassthroughNaming,
    StripNaming,
)
from tests.conftest import make_metadata_package
from tests.test_annotations import highlight, library_row, make_databases


class TestAnnotationsCanTravelInsideTheBook:
    """
    The W3C work defines two shapes and prefers neither. Detached annotations
    "can be shared independently of the publication"; embedded ones, at
    ``META-INF/annotations.json``, are "always available to users".

    Both are worth having and they answer different questions. A library-wide
    file survives losing the book, which is the requirement the work opens
    with. An embedded set travels with the book to whatever reads it next.
    """

    def test_a_converted_book_carries_its_annotations(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_metadata_package(library, "Leviathan Wakes.epub", title="Leviathan Wakes")
        found = [
            {
                "id": "U1",
                "book": {"title": "Leviathan Wakes", "source": "Leviathan Wakes.epub"},
                "text": "Summary roadside justice",
                "locator": ":~:text=Summary%20roadside%20justice",
                "created": "2018-12-25T22:44:28Z",
            }
        ]

        archive.zip_package(
            library / "Leviathan Wakes.epub",
            output_dir / "out.epub",
            annotations=annotations.for_book(
                "Leviathan Wakes.epub", annotations.index_by_book(found)
            ),
        )

        with ZipFile(output_dir / "out.epub") as opened:
            assert "META-INF/annotations.json" in opened.namelist()
            embedded = json.loads(opened.read("META-INF/annotations.json"))
        assert [item["id"] for item in embedded["annotations"]] == ["U1"]
        assert embedded["generator"]["name"] == "ibook2epub"

    def test_a_book_without_annotations_is_unchanged(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_metadata_package(library, "Plain.epub", title="Plain")

        archive.zip_package(
            library / "Plain.epub", output_dir / "out.epub", annotations=[]
        )

        with ZipFile(output_dir / "out.epub") as opened:
            assert "META-INF/annotations.json" not in opened.namelist()

    def test_the_embedded_set_is_only_this_book(self):
        found = [
            {
                "id": "A",
                "book": {"source": "One.epub", "title": "One"},
                "text": "x",
                "created": "2018-12-25T22:44:28Z",
            },
            {
                "id": "B",
                "book": {"source": "Two.epub", "title": "Two"},
                "text": "y",
                "created": "2018-12-25T22:44:28Z",
            },
        ]

        mine = annotations.for_book("One.epub", annotations.index_by_book(found))

        assert [item["id"] for item in mine] == ["A"]

    def test_a_book_with_no_annotations_gets_no_file(self):
        found = [
            {
                "id": "A",
                "book": {"source": "One.epub", "title": "One"},
                "text": "x",
                "created": "2018-12-25T22:44:28Z",
            }
        ]

        assert (
            annotations.for_book("Other.epub", annotations.index_by_book(found)) == []
        )


class TestItFailsSafely:
    """
    Apple's schema is undocumented and can change under an update, and the
    databases live behind a macOS permission. Every one of these paths is how
    that arrives, and each has to say what happened rather than raise.
    """

    def test_a_database_with_the_wrong_shape_is_reported(self, tmp_path):
        make_databases(tmp_path)
        database = next(tmp_path.rglob("AEAnnotation*.sqlite"))
        with sqlite3.connect(database) as connection:
            connection.execute("DROP TABLE ZAEANNOTATION")
            connection.execute("CREATE TABLE ZAEANNOTATION (SOMETHINGELSE TEXT)")

        with pytest.raises(
            annotations.AnnotationsUnavailableError, match="not shaped as expected"
        ):
            annotations.collect(tmp_path)

    def test_a_container_without_an_annotation_database_says_so(self, tmp_path):
        (tmp_path / "AEAnnotation").mkdir(parents=True)

        with pytest.raises(
            annotations.AnnotationsUnavailableError, match="Full Disk Access"
        ):
            annotations.collect(tmp_path)

    def test_an_unreadable_library_costs_titles_not_highlights(self, tmp_path):
        make_databases(tmp_path)
        library = next(tmp_path.rglob("BKLibrary*.sqlite"))
        with sqlite3.connect(library) as connection:
            connection.execute("DROP TABLE ZBKLIBRARYASSET")

        found = annotations.collect(tmp_path)

        assert len(found) == 1
        assert found[0]["book"]["title"] == "ASSET1"

    def test_a_missing_library_database_costs_titles_not_highlights(self, tmp_path):
        make_databases(tmp_path)
        next(tmp_path.rglob("BKLibrary*.sqlite")).unlink()

        assert len(annotations.collect(tmp_path)) == 1

    def test_an_annotation_with_no_creation_date_is_still_exported(self, tmp_path):
        make_databases(tmp_path, rows=[highlight(created=None, modified=None)])

        found = annotations.collect(tmp_path)[0]

        assert found["created"].endswith("Z")
        assert "modified" not in found

    def test_a_row_with_no_style_leaves_the_field_out(self, tmp_path):
        make_databases(tmp_path, rows=[highlight(style=None)])

        assert "style" not in annotations.collect(tmp_path)[0]


class TestTheSchemaCheckActuallyChecks:
    """A validator that never rejects anything is not a validator."""

    def test_a_missing_required_field_is_reported(self):
        document = annotations.build_document([])
        del document["generator"]

        assert annotations.schema_problems(document) == ["missing generator"]

    def test_a_set_with_no_generation_stamp_is_still_valid(self):
        # An embedded set carries none: a stamp that moves on every run makes
        # the archive holding it stop being byte-reproducible.
        document = annotations.build_document([], stamped=False)

        assert "generated" not in document
        assert annotations.schema_problems(document) == []

    def test_a_malformed_instant_is_reported(self):
        document = annotations.build_document([])
        document["generated"] = "yesterday"

        assert any(
            "not an instant" in problem
            for problem in annotations.schema_problems(document)
        )

    def test_an_annotation_missing_a_required_field_is_reported(self):
        document = annotations.build_document(
            [{"id": "A", "book": {"title": "T"}, "text": "x"}]
        )

        assert annotations.schema_problems(document) == [
            "annotations[0] missing created"
        ]

    def test_a_locator_that_is_not_a_text_fragment_is_reported(self):
        document = annotations.build_document(
            [
                {
                    "id": "A",
                    "book": {"title": "T"},
                    "text": "x",
                    "created": "2018-12-25T22:44:28Z",
                    "locator": "epubcfi(/6/4)",
                }
            ]
        )

        assert any(
            "locator does not match" in problem
            for problem in annotations.schema_problems(document)
        )

    def test_an_unknown_field_is_reported(self):
        document = annotations.build_document(
            [
                {
                    "id": "A",
                    "book": {"title": "T"},
                    "text": "x",
                    "created": "2018-12-25T22:44:28Z",
                    "colour": "yellow",
                }
            ]
        )

        assert any(
            "unknown ['colour']" in problem
            for problem in annotations.schema_problems(document)
        )


class TestTheCommandLineMode:
    """
    Its own mode, and deliberately not part of a conversion: somebody who wants
    their highlights out may not want three thousand epub files as well. It
    needs no library, no output directory, and writes nothing but the file it
    was given.
    """

    def _container(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """A container of annotations, and an empty library to point -s at."""
        make_databases(tmp_path / "container")
        library = tmp_path / "empty"
        library.mkdir(exist_ok=True)
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        return library

    def test_it_writes_a_file_and_converts_nothing(
        self, tmp_path, monkeypatch, output_dir
    ):
        library = self._container(monkeypatch, tmp_path)
        target = tmp_path / "mine.json"

        code = run.main(
            ["-s", str(library), "-ad", str(target), "-o", str(output_dir), "-q"]
        )

        assert code == 0
        assert json.loads(target.read_text(encoding="utf-8"))["annotations"]
        assert list(output_dir.glob("*.epub")) == []

    def test_a_rerun_reports_what_changed(self, tmp_path, monkeypatch, capsys):
        library = self._container(monkeypatch, tmp_path)
        target = tmp_path / "mine.json"
        flags = ["-s", str(library), "-o", str(tmp_path / "out"), "-ad", str(target)]
        run.main(flags)
        capsys.readouterr()

        run.main(flags)

        # -q would suppress it: the tally is information, not a warning.
        assert "0 added, 0 updated, 1 unchanged" in capsys.readouterr().err

    def test_an_unreadable_existing_file_is_refused_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        library = self._container(monkeypatch, tmp_path)
        target = tmp_path / "mine.json"
        target.write_text("this is not json", encoding="utf-8")

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "out"), "-ad", str(target), "-q"]
        )

        # NO_OUTPUT, not NO_SOURCE: the unreadable file is the destination.
        assert code == 5
        assert target.read_text(encoding="utf-8") == "this is not json"

    def test_an_unavailable_container_has_its_own_exit_code(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "absent", policy),
        )

        library = tmp_path / "empty"
        library.mkdir()
        assert (
            run.main(
                ["-s", str(library), "-o", str(tmp_path / "out"), "-ar", "-ae", "-q"]
            )
            == 4
        )

    def test_a_target_that_cannot_be_written_has_its_own_exit_code(
        self, tmp_path, monkeypatch
    ):
        library = self._container(monkeypatch, tmp_path)

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(tmp_path / "out"),
                "-ad",
                str(tmp_path / "nope" / "mine.json"),
                "-q",
            ]
        )

        assert code == 5


class TestRefreshingAnnotationsWithoutConverting:
    """
    A shelf is converted once and annotated for years afterwards. Re-running
    the conversion to pick up a new highlight would rewrite thousands of
    archives to change one member of one of them, and on a cloud-backed library
    that is minutes of work for nothing.

    ``--annotations-only`` walks what is already on the shelf and replaces just
    the embedded annotation set, leaving every other member and every other
    book alone.
    """

    def _shelf(
        self,
        tmp_path: Path,
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[tuple[object, ...]] | None = None,
    ) -> Path:
        """A converted shelf, and a container holding annotations for it."""
        library = tmp_path / "lib"
        make_metadata_package(library, "Leviathan Wakes.epub", title="Leviathan Wakes")
        make_metadata_package(library, "Other.epub", title="Other")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        make_databases(
            tmp_path / "container",
            rows=rows if rows is not None else [highlight()],
            books=[library_row(path="/x/Leviathan Wakes.epub")],
        )
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        return library

    def test_it_adds_annotations_to_a_book_already_on_the_shelf(
        self, tmp_path, output_dir, monkeypatch
    ):
        library = self._shelf(tmp_path, output_dir, monkeypatch)
        target = output_dir / "Leviathan Wakes.epub"
        with ZipFile(target) as opened:
            assert annotations.EMBEDDED_PATH not in opened.namelist()

        code = run.main(["-s", str(library), "-o", str(output_dir), "-ae", "-ar", "-q"])

        assert code == 0
        with ZipFile(target) as opened:
            embedded = json.loads(opened.read(annotations.EMBEDDED_PATH))
        assert [item["id"] for item in embedded["annotations"]] == ["U1"]

    def test_it_does_not_touch_a_book_with_no_annotations(
        self, tmp_path, output_dir, monkeypatch
    ):
        library = self._shelf(tmp_path, output_dir, monkeypatch)
        other = output_dir / "Other.epub"
        before = other.read_bytes()

        run.main(["-s", str(library), "-o", str(output_dir), "-ae", "-ar", "-q"])

        assert other.read_bytes() == before

    def test_every_other_member_survives_the_rewrite(
        self, tmp_path, output_dir, monkeypatch
    ):
        library = self._shelf(tmp_path, output_dir, monkeypatch)
        target = output_dir / "Leviathan Wakes.epub"
        with ZipFile(target) as opened:
            before = {name: opened.read(name) for name in opened.namelist()}

        run.main(["-s", str(library), "-o", str(output_dir), "-ae", "-ar", "-q"])

        with ZipFile(target) as opened:
            after = {name: opened.read(name) for name in opened.namelist()}
        assert set(after) == set(before) | {annotations.EMBEDDED_PATH}
        for name, content in before.items():
            assert after[name] == content, name

    def test_the_mimetype_stays_first_and_stored(
        self, tmp_path, output_dir, monkeypatch
    ):
        # The one member whose position and compression the spec fixes.
        library = self._shelf(tmp_path, output_dir, monkeypatch)

        run.main(["-s", str(library), "-o", str(output_dir), "-ae", "-ar", "-q"])

        with ZipFile(output_dir / "Leviathan Wakes.epub") as opened:
            first = opened.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == ZIP_STORED

    def test_a_second_refresh_replaces_rather_than_duplicates(
        self, tmp_path, output_dir, monkeypatch
    ):
        library = self._shelf(tmp_path, output_dir, monkeypatch)
        flags = ["-s", str(library), "-o", str(output_dir), "-ae", "-ar", "-q"]
        run.main(flags)

        run.main(flags)

        with ZipFile(output_dir / "Leviathan Wakes.epub") as opened:
            names = opened.namelist()
        assert names.count(annotations.EMBEDDED_PATH) == 1

    def test_it_converts_nothing(self, tmp_path, output_dir, monkeypatch):
        library = self._shelf(tmp_path, output_dir, monkeypatch)
        make_metadata_package(library, "Third.epub", title="Third")

        run.main(["-s", str(library), "-o", str(output_dir), "-ae", "-ar", "-q"])

        # Added to the library after the conversion, so it is not on the shelf
        # and this mode must not put it there.
        assert not (output_dir / "Third.epub").exists()


class TestAnnotationsOnly:
    """
    ``-ao`` is for somebody who wants their highlights and not three thousand
    epub files. It writes the detached file and stops: no conversion, no shelf,
    no output directory needed.

    Separate from ``-ad`` rather than a modifier on it, because "convert and
    also export" and "export instead of converting" are different intentions
    and a flag should not have to be read twice to tell which one it is.
    """

    def _container(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        make_databases(tmp_path / "container")
        library = tmp_path / "lib"
        make_metadata_package(library, "Leviathan Wakes.epub", title="Leviathan Wakes")
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        return library

    def test_it_writes_the_file_and_converts_nothing(
        self, tmp_path, output_dir, monkeypatch
    ):
        library = self._container(monkeypatch, tmp_path)
        target = tmp_path / "mine.json"

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-ao", str(target), "-q"]
        )

        assert code == 0
        assert json.loads(target.read_text(encoding="utf-8"))["annotations"]
        assert list(output_dir.glob("*.epub")) == []

    def test_it_needs_no_output_directory(self, tmp_path, monkeypatch):
        # Nothing is written to a shelf, so demanding one -- or creating one --
        # would be a typo in -o turning into a stray directory.
        library = self._container(monkeypatch, tmp_path)
        target = tmp_path / "mine.json"
        shelf = tmp_path / "never"

        code = run.main(
            ["-s", str(library), "-o", str(shelf), "-ao", str(target), "-q"]
        )

        assert code == 0
        assert not shelf.exists()

    def test_a_rerun_merges_rather_than_replaces(self, tmp_path, monkeypatch, capsys):
        library = self._container(monkeypatch, tmp_path)
        target = tmp_path / "mine.json"
        flags = ["-s", str(library), "-ao", str(target)]
        run.main(flags)
        capsys.readouterr()

        run.main(flags)

        assert "0 added, 0 updated, 1 unchanged" in capsys.readouterr().err

    def test_it_contradicts_embedding(self, tmp_path):
        # There is no conversion to embed into.
        with pytest.raises(SystemExit) as raised:
            run.main(["-ao", str(tmp_path / "x.json"), "-ae"])

        assert raised.value.code == 2

    def test_it_contradicts_the_other_file_flag(self, tmp_path):
        with pytest.raises(SystemExit) as raised:
            run.main(["-ao", str(tmp_path / "x.json"), "-ad", str(tmp_path / "y.json")])

        assert raised.value.code == 2


class TestWritingToStandardOutput:
    """
    With no filename the document goes to stdout, so it can be piped somewhere
    without a temporary file. Nothing else may then go to stdout: a run summary
    landing in the middle of the JSON would make it unparsable, which is the
    one thing a pipe cannot tolerate.
    """

    def _container(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        make_databases(tmp_path / "container")
        library = tmp_path / "lib"
        make_metadata_package(library, "Leviathan Wakes.epub", title="Leviathan Wakes")
        monkeypatch.setattr(
            "epubconvert.run.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        return library

    def test_annotations_only_with_no_file_writes_json_to_stdout(
        self, tmp_path, monkeypatch, capsys
    ):
        library = self._container(monkeypatch, tmp_path)

        code = run.main(["-s", str(library), "-ao", "-q"])

        assert code == 0
        document = json.loads(capsys.readouterr().out)
        assert document["annotations"][0]["id"] == "U1"

    def test_a_dash_means_the_same_thing(self, tmp_path, monkeypatch, capsys):
        library = self._container(monkeypatch, tmp_path)

        run.main(["-s", str(library), "-ao", "-", "-q"])

        assert json.loads(capsys.readouterr().out)["annotations"]

    def test_nothing_but_json_reaches_stdout_during_a_conversion(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        # -ad converts as well, and its summary would otherwise land in the
        # middle of the document.
        library = self._container(monkeypatch, tmp_path)

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-ad"])

        captured = capsys.readouterr()
        assert json.loads(captured.out)["annotations"]
        assert "Exported" in captured.err

    def test_stdout_has_nothing_to_merge_into(self, tmp_path, monkeypatch, capsys):
        library = self._container(monkeypatch, tmp_path)
        run.main(["-s", str(library), "-ao", "-q"])
        capsys.readouterr()

        run.main(["-s", str(library), "-ao", "-q"])

        # A pipe is not a file to add to, so every run emits the whole set and
        # says nothing about what changed.
        assert "unchanged" not in capsys.readouterr().err


class TestTheFlagsDoNotOverlap:
    def test_refresh_needs_embedding_to_have_anything_to_do(self, tmp_path):
        # -ar walks the shelf. With only -ad it never touches it, which is what
        # -ao already means, so the pair would be two spellings of one thing.
        with pytest.raises(SystemExit) as raised:
            run.main(["-ar", "-ad", str(tmp_path / "x.json")])

        assert raised.value.code == 2


class TestARefreshedArchiveIsStillReproducible:
    """
    Invariant: an archive is byte-reproducible -- pinned timestamps, fixed
    mode. ``replace_annotations`` rebuilds the whole zip, so it is the one
    place that could quietly break that.
    """

    def _book(self, tmp_path: Path, output_dir: Path, name: str = "Book.epub") -> Path:
        library = tmp_path / "lib"
        make_metadata_package(library, name, title="Book")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        return output_dir / name

    def test_two_refreshes_give_byte_identical_archives(self, tmp_path, output_dir):
        target = self._book(tmp_path, output_dir)
        mine: list[dict[str, object]] = [
            {
                "id": "U1",
                "book": {"title": "Book", "source": "Book.epub"},
                "text": "a highlight",
                "locator": ":~:text=a%20highlight",
                "created": "2018-12-25T22:44:28Z",
            }
        ]

        archive.replace_annotations(target, mine)
        first = target.read_bytes()
        # Forced, because an unchanged set is skipped by design.
        archive.replace_annotations(target, [])
        archive.replace_annotations(target, mine)

        assert target.read_bytes() == first

    def test_the_pinned_timestamps_survive(self, tmp_path, output_dir):
        target = self._book(tmp_path, output_dir)
        with ZipFile(target) as opened:
            before = {i.filename: i.date_time for i in opened.infolist()}

        archive.replace_annotations(
            target,
            [
                {
                    "id": "U1",
                    "book": {"title": "Book", "source": "Book.epub"},
                    "text": "x",
                    "created": "2018-12-25T22:44:28Z",
                }
            ],
        )

        with ZipFile(target) as opened:
            after = {i.filename: i.date_time for i in opened.infolist()}
        for name, stamp in before.items():
            assert after[name] == stamp, name
        assert after[annotations.EMBEDDED_PATH] == before["mimetype"]


class TestTheHrefIsResolvedNotGuessed:
    """
    The bracketed part of a CFI is an *ID assertion* -- the spine item's id
    attribute -- not its href. Four of five annotations in a real library carry
    an id there (``id761661``, ``uuid-6134c31c-...``); only one happens to
    carry a filename, which is exactly what makes the mistake easy.

    The book is at hand, so the id is resolved against its manifest rather than
    passed off as a path.
    """

    def _with_book(self, tmp_path: Path, cfi: str) -> dict[str, object]:
        library = tmp_path / "books"
        make_metadata_package(library, "Book.epub", title="Book")
        make_databases(
            tmp_path / "container",
            rows=[highlight(location=cfi)],
            books=[library_row(path=str(library / "Book.epub"))],
        )
        return annotations.collect(tmp_path / "container")[0]

    def test_an_id_assertion_becomes_a_real_path(self, tmp_path):
        found = self._with_book(tmp_path, "epubcfi(/6/4[ch1]!/4/2)")

        # Resolved against the OPF's directory, so it is a path within the
        # book rather than the raw href.
        assert found["href"] == "OEBPS/text/chapter1.xhtml"

    def test_an_id_that_is_in_no_manifest_leaves_the_href_out(self, tmp_path):
        found = self._with_book(tmp_path, "epubcfi(/6/4[nosuchid]!/4/2)")

        assert "href" not in found
        # The raw assertion is still recoverable from the cfi, so nothing is lost.
        assert found["cfi"] == "epubcfi(/6/4[nosuchid]!/4/2)"

    def test_a_book_that_cannot_be_read_leaves_the_href_out(self, tmp_path):
        make_databases(
            tmp_path / "container",
            rows=[highlight(location="epubcfi(/6/4[ch1]!/4)")],
            books=[library_row(path=str(tmp_path / "gone" / "Book.epub"))],
        )

        assert "href" not in annotations.collect(tmp_path / "container")[0]

    def test_each_book_is_read_once_however_many_highlights_it_has(self, tmp_path):
        library = tmp_path / "books"
        make_metadata_package(library, "Book.epub", title="Book")
        make_databases(
            tmp_path / "container",
            rows=[
                highlight(uuid=f"U{n}", location="epubcfi(/6/4[ch1]!/4)")
                for n in range(5)
            ],
            books=[library_row(path=str(library / "Book.epub"))],
        )

        assert all(
            item["href"] == "OEBPS/text/chapter1.xhtml"
            for item in annotations.collect(tmp_path / "container")
        )


class TestTheFragmentStaysUsable:
    """
    A text fragment has to match in a browser. Quoting a 249-character
    highlight whole -- which a real one is -- makes a 341-character locator
    that fails on any whitespace or entity difference in the rendered DOM.
    The WICG format has ``textStart,textEnd`` for exactly this.
    """

    def test_a_short_highlight_is_quoted_whole(self):
        assert annotations.text_fragment("A short one") == ":~:text=A%20short%20one"

    def test_a_long_highlight_becomes_a_range(self):
        text = " ".join(f"word{n}" for n in range(40))

        fragment = annotations.text_fragment(text)

        assert "," in fragment
        start, end = fragment[len(":~:text=") :].split(",")
        assert start and end
        assert len(fragment) < len(text)

    def test_the_range_ends_are_the_real_ends_of_the_highlight(self):
        text = "The beginning of it " + "middle " * 30 + "and the very end"

        start, end = annotations.text_fragment(text)[len(":~:text=") :].split(",")

        assert start.startswith("The%20beginning")
        assert end.endswith("very%20end")

    def test_the_ends_fall_on_word_boundaries(self):
        text = " ".join(f"word{n}" for n in range(40))

        start, _end = annotations.text_fragment(text)[len(":~:text=") :].split(",")

        assert not unquote(start).endswith(" ")
        assert " " not in unquote(start).strip().split()[-1]

    def test_whitespace_is_collapsed_before_quoting(self):
        # The DOM will have collapsed it, so quoting the source runs would not
        # match anything.
        assert annotations.text_fragment("two   \n words") == ":~:text=two%20words"


class TestMatchingABookWithoutASource:
    """
    ``book.source`` is only set when the library database knows where the book
    is. Matching on it alone meant an annotation whose book the library has
    forgotten could be exported but never embedded -- silently, since it looked
    like a book with no annotations.
    """

    def test_a_title_matches_when_there_is_no_source(self):
        found = [
            {
                "id": "A",
                "book": {"title": "Leviathan Wakes"},
                "text": "x",
                "created": "2018-12-25T22:44:28Z",
            }
        ]

        assert (
            annotations.for_book(
                "Leviathan Wakes.epub", annotations.index_by_book(found)
            )
            == found
        )

    def test_a_source_still_wins_over_a_title(self):
        # A title is a weaker key: two books can share one, a package name
        # cannot. So it is only consulted when there is no source at all.
        found = [
            {
                "id": "A",
                "book": {"title": "Other", "source": "Leviathan Wakes.epub"},
                "text": "x",
                "created": "2018-12-25T22:44:28Z",
            }
        ]

        assert (
            annotations.for_book(
                "Leviathan Wakes.epub", annotations.index_by_book(found)
            )
            == found
        )
        assert (
            annotations.for_book("Other.epub", annotations.index_by_book(found)) == []
        )

    def test_an_unrelated_title_still_does_not_match(self):
        found = [
            {
                "id": "A",
                "book": {"title": "Something Else"},
                "text": "x",
                "created": "2018-12-25T22:44:28Z",
            }
        ]

        assert (
            annotations.for_book(
                "Leviathan Wakes.epub", annotations.index_by_book(found)
            )
            == []
        )


class TestTheBookNamesTheFileItWillBeFoundIn:
    """
    An annotation is only useful if the book can be found again, and under a
    naming policy the shelf name is not the source name: a book read from
    ``Leviathan Wakes.epub`` is written as
    ``Corey, James S.A. - Leviathan Wakes.epub``.

    The name is asked of the naming policy rather than worked out here. Taking
    ``Path(ZPATH).name`` was a second implementation of a rule this codebase
    already owns -- which is how the two came to disagree.
    """

    def _found(self, tmp_path: Path, policy: NamingPolicy | None) -> dict[str, object]:
        library = tmp_path / "books"
        make_metadata_package(
            library,
            "Leviathan Wakes.epub",
            title="Leviathan Wakes",
            file_as="Corey, James S. A.",
        )
        make_databases(
            tmp_path / "container",
            books=[library_row(path=str(library / "Leviathan Wakes.epub"))],
        )
        book: dict[str, object] = annotations.collect(tmp_path / "container", policy)[
            0
        ]["book"]
        return book

    def test_the_shelf_name_follows_the_policy(self, tmp_path):
        book = self._found(tmp_path, MetadataNaming())

        assert book["filename"] == "Corey, James S. A. - Leviathan Wakes.epub"

    def test_the_source_name_is_still_recorded(self, tmp_path):
        # Both facts are worth having and they are different facts.
        book = self._found(tmp_path, MetadataNaming())

        assert book["source"] == "Leviathan Wakes.epub"

    def test_the_default_policy_leaves_them_the_same(self, tmp_path):
        book = self._found(tmp_path, PassthroughNaming())

        assert book["filename"] == book["source"] == "Leviathan Wakes.epub"

    def test_a_sanitising_policy_is_honoured(self, tmp_path):
        book = self._found(tmp_path, StripNaming())

        assert book["filename"] == "Leviathan Wakes.epub"

    def test_no_policy_means_no_claim_about_the_shelf(self, tmp_path):
        # Nothing is guessed. A caller that has not chosen a policy gets the
        # source name and no assertion about what the file will be called.
        book = self._found(tmp_path, None)

        assert "filename" not in book

    def test_a_book_the_library_has_lost_makes_no_claim_either(self, tmp_path):
        make_databases(tmp_path / "container", books=[])

        book = annotations.collect(tmp_path / "container", MetadataNaming())[0]["book"]

        assert "filename" not in book
        assert "source" not in book
