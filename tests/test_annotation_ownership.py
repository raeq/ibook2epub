"""
Tests for whose work an annotation is, and where it can go.

An annotation is the reader's: their selection, and their note beside it. That
is true of a DRM-protected book exactly as of any other, because the annotation
was never inside the protected file -- Apple keeps it in a separate database,
and the licensing of a book says nothing about who owns the sentence somebody
chose to mark.

Two consequences, and this file pins both. A book that cannot be converted
still gives up its highlights in full. And because ``-ae`` needs an archive to
put them in, a book that never reached the shelf leaves its highlights with
nowhere to go, so the reader is told and pointed at a detached export instead.
That is the case where taking them with you matters most: the book itself is
the one thing that cannot come along.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.collect import annotations
from epubconvert.run.run import main
from tests.conftest import make_metadata_package, make_package
from tests.test_annotations import highlight, library_row, make_databases


class TestAnnotationsAreTheReadersOwnWork:
    """
    A highlight is the reader's: their selection, and their note beside it.
    That is true of a DRM-protected book exactly as it is of any other. The
    book cannot be converted, and this tool does not try -- but the annotation
    was never part of the protected file. Apple keeps it in a separate
    database, and it belongs to the person who wrote it.

    This is the case where taking them with you matters most, because the book
    itself is the one thing that cannot come. So it is pinned here: a reader
    whose library is entirely DRM-protected must still get every highlight out,
    with its text intact.
    """

    def _drm_library(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        library = tmp_path / "lib"
        package = make_metadata_package(
            library, "Locked Book.epub", title="Locked Book", creator="A Writer"
        )
        (package / "META-INF" / "encryption.xml").write_text(
            '<?xml version="1.0"?>'
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">'
            '<EncryptionMethod Algorithm="http://www.apple.com/technology/fps"/>'
            '<CipherData><CipherReference URI="OPS/ch1.xhtml"/></CipherData>'
            "</EncryptedData></encryption>",
            encoding="utf-8",
        )
        make_databases(
            tmp_path / "container",
            rows=[highlight(uuid="MINE", text="a sentence I chose myself")],
            books=[library_row(path=str(package), title="Locked Book")],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        return library

    def test_the_book_is_still_skipped_because_it_cannot_be_converted(
        self, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = self._drm_library(tmp_path, monkeypatch)

        main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert list(output_dir.glob("*.epub")) == []

    def test_the_highlight_still_comes_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = self._drm_library(tmp_path, monkeypatch)
        target = tmp_path / "mine.json"

        assert main(["-s", str(library), "-ao", str(target), "-q"]) == 0

        document = json.loads(target.read_text(encoding="utf-8"))
        assert [a["id"] for a in document["annotations"]] == ["MINE"]

    def test_the_text_is_not_withheld_or_redacted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The reader's own words, verbatim. Nothing about the book's licensing
        # makes their selection something to hold back from them.
        library = self._drm_library(tmp_path, monkeypatch)
        target = tmp_path / "mine.json"
        main(["-s", str(library), "-ao", str(target), "-q"])

        annotation = json.loads(target.read_text(encoding="utf-8"))["annotations"][0]

        assert annotation["text"] == "a sentence I chose myself"
        assert annotation["locator"] == ":~:text=a%20sentence%20I%20chose%20myself"

    def test_the_book_is_still_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = self._drm_library(tmp_path, monkeypatch)
        target = tmp_path / "mine.json"
        main(["-s", str(library), "-ao", str(target), "-q"])

        book = json.loads(target.read_text(encoding="utf-8"))["annotations"][0]["book"]

        assert book["title"] == "Locked Book"

    def test_nothing_in_the_annotations_path_consults_encryption(self):
        # A guard rather than a promise. Reaching for encryption state here
        # would be the first step towards withholding a reader's own writing
        # from them, and it is easier to refuse the import than the idea.
        body = Path(annotations.__file__).read_text(encoding="utf-8")
        code = body.split('"""', 2)[2]  # past the module docstring

        assert "encryption" not in code
        assert "drm" not in code.lower()


class TestHighlightsThatReachedNoFileAreReported:
    """
    ``-ae`` puts a book's highlights inside the book. A book that was never
    converted has no archive to put them in, so those highlights reach nothing
    at all -- read out of Apple's database and then dropped on the floor.

    A DRM-protected book is the permanent case: its file cannot be opened, so
    no rerun will ever produce an archive to embed into. It is also the case
    where the reader most wants their highlights, because the book itself
    cannot come with them. Saying nothing left them believing the export had
    covered everything.
    """

    def _mixed_library(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """One book that converts, and one locked book that cannot."""
        library = tmp_path / "lib"
        make_metadata_package(library, "Open Book.epub", title="Open Book")
        locked = make_metadata_package(library, "Locked Book.epub", title="Locked Book")
        (locked / "META-INF" / "encryption.xml").write_text(
            '<?xml version="1.0"?>'
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">'
            '<EncryptionMethod Algorithm="http://www.apple.com/technology/fps"/>'
            '<CipherData><CipherReference URI="OPS/ch1.xhtml"/></CipherData>'
            "</EncryptedData></encryption>",
            encoding="utf-8",
        )
        make_databases(
            tmp_path / "container",
            rows=[
                highlight(uuid="OPEN", asset="A1", text="from a book I can open"),
                highlight(uuid="LOCKED", asset="A2", text="from a locked one"),
            ],
            books=[
                library_row(
                    asset="A1", title="Open Book", path=str(library / "Open Book.epub")
                ),
                library_row(asset="A2", title="Locked Book", path=str(locked)),
            ],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        return library

    def test_the_reader_is_told_their_highlights_reached_nothing(
        self,
        tmp_path: Path,
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        library = self._mixed_library(tmp_path, monkeypatch)

        main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-ae"])

        warned = capsys.readouterr().err
        assert "1 annotation(s)" in warned
        assert "1 book(s)" in warned
        assert "Locked Book.epub" in warned

    def test_the_warning_recommends_a_detached_export(
        self,
        tmp_path: Path,
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        library = self._mixed_library(tmp_path, monkeypatch)

        main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-ae"])

        assert "--annotations-detached" in capsys.readouterr().err

    def test_the_book_that_converted_still_carries_its_own(
        self, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = self._mixed_library(tmp_path, monkeypatch)
        main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-ae", "-q"])

        with ZipFile(output_dir / "Open Book.epub") as opened:
            held = json.loads(opened.read(annotations.EMBEDDED_PATH))

        assert [a["id"] for a in held["annotations"]] == ["OPEN"]

    def test_nothing_is_said_when_a_detached_file_already_holds_them(
        self,
        tmp_path: Path,
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        # -ae -ad FILE saved them. There is nothing to warn about, and a
        # warning that fires when the problem is already solved is noise.
        library = self._mixed_library(tmp_path, monkeypatch)

        main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "-ae",
                "-ad",
                str(tmp_path / "notes.json"),
            ]
        )

        assert "reached no file" not in capsys.readouterr().err

    def test_nothing_is_said_when_every_book_converted(
        self,
        tmp_path: Path,
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        library = tmp_path / "lib"
        make_metadata_package(library, "Open Book.epub", title="Open Book")
        make_databases(
            tmp_path / "container",
            rows=[highlight(uuid="OPEN", asset="A1")],
            books=[
                library_row(
                    asset="A1", title="Open Book", path=str(library / "Open Book.epub")
                )
            ],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )

        main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-ae"])

        assert "reached no file" not in capsys.readouterr().err

    def test_a_dry_run_says_nothing_because_it_wrote_nothing(
        self,
        tmp_path: Path,
        output_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        library = self._mixed_library(tmp_path, monkeypatch)

        main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-ae", "-d"])

        assert "reached no file" not in capsys.readouterr().err


def _embedded(output_dir: Path) -> list[str]:
    """The archives on the shelf that carry an embedded annotation set."""
    carrying = []
    for book in sorted(output_dir.glob("*.epub")):
        with ZipFile(book) as opened:
            if annotations.EMBEDDED_PATH in opened.namelist():
                carrying.append(book.name)
    return carrying


class TestAHighlightTwoBooksCouldOwnGoesToNeither:
    """
    An annotation names its book by package directory name, not by path, so
    two packages with one name in different folders look alike to it. Only
    the -ar refresh used to notice: the conversion, a vault and the stranded
    warning each gave the highlight to both books, one of which never had it.
    """

    @pytest.fixture(name="twins")
    def _twins(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        library = tmp_path / "lib"
        make_package(library / "one", "Book.epub")
        make_package(library / "two", "Book.epub")
        mine = {
            "id": "U1",
            "book": {"title": "Book", "source": "Book.epub"},
            "text": "which of the two?",
            "created": "2018-12-25T22:44:28Z",
        }
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda **_kwargs: [mine],
        )
        return library

    def test_the_conversion_embeds_it_in_neither(self, twins, output_dir, capsys):
        argv = ["-s", str(twins), "-o", str(output_dir), "-m", "0", "-ae"]

        main([*argv, "--on-collision", "suffix"])

        assert len(list(output_dir.glob("*.epub"))) == 2
        assert _embedded(output_dir) == []
        assert "more than one package" in capsys.readouterr().err

    def test_a_refresh_embeds_it_in_neither(self, twins, output_dir):
        argv = ["-s", str(twins), "-o", str(output_dir), "--on-collision", "suffix"]
        main([*argv, "-m", "0", "-q"])

        main([*argv, "-ae", "-ar", "-q"])

        assert _embedded(output_dir) == []

    def test_a_vault_gives_it_to_neither(self, twins, tmp_path):
        vault = tmp_path / "vault"
        argv = ["-s", str(twins), "--on-collision", "suffix", "-q"]

        main([*argv, "-ao", str(vault), "--annotations-format", "markdown"])

        assert list(vault.glob("*.md")) == []

    def test_it_is_not_reported_twice_as_stranded(self, twins, output_dir, capsys):
        # Both books collide and are skipped, so neither is on the shelf. The
        # highlight was counted once per book: "2 annotation(s) from 2
        # book(s)", from one highlight that belongs to at most one of them.
        main(["-s", str(twins), "-o", str(output_dir), "-m", "0", "-ae"])

        warned = capsys.readouterr().err
        assert "reached no file" not in warned
        assert "more than one package" in warned


class TestAZippedBookSharesItsNameWithAPackage:
    """
    An already-zipped ``b/Book.epub`` answers to the same name as a package
    ``a/Book.epub/``, and a highlight names its book by that name alone. Only
    package directories were counted when looking for a name two books share,
    so the zipped book's highlight was embedded in the package's archive and
    written into the package's vault note -- another book's words, on the
    wrong book. Whether the file is copied through changes nothing: it is in
    the library, and so are its highlights.
    """

    #: Named by their metadata, so the package and the zipped book land on
    #: the shelf side by side and either could be handed the highlight.
    NAMING = ["--name-by", "author-title"]

    @pytest.fixture(name="zipped_twin")
    def _zipped_twin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        library = tmp_path / "lib"
        make_metadata_package(
            library / "a", "Book.epub", title="Package", creator="P Writer"
        )
        other = make_metadata_package(
            tmp_path / "staging", "Z.epub", title="Zipped", creator="Z Writer"
        )
        zipped = library / "b" / "Book.epub"
        zipped.parent.mkdir(parents=True)
        with ZipFile(zipped, "w") as opened:
            for member in sorted(other.rglob("*")):
                if member.is_file():
                    opened.write(member, member.relative_to(other).as_posix())
        theirs = {
            "id": "Z1",
            "book": {"title": "Book", "source": "Book.epub"},
            "text": "a line from the zipped book",
            "created": "2018-12-25T22:44:28Z",
        }
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda **_kwargs: [theirs],
        )
        return library

    @pytest.mark.parametrize("copy_through", [[], ["--no-copy-through"]])
    def test_the_conversion_embeds_it_in_neither(
        self, zipped_twin, tmp_path, copy_through, capsys
    ):
        shelf = tmp_path / "shelf"
        argv = ["-s", str(zipped_twin), "-o", str(shelf), "-m", "0", "-ae"]

        main([*argv, *self.NAMING, *copy_through])

        assert len(list(shelf.glob("*.epub"))) == 2 - len(copy_through)
        assert _embedded(shelf) == []
        assert "more than one package" in capsys.readouterr().err

    def test_a_refresh_embeds_it_in_neither(self, zipped_twin, tmp_path):
        shelf = tmp_path / "shelf"
        argv = ["-s", str(zipped_twin), "-o", str(shelf), *self.NAMING]
        main([*argv, "-m", "0", "-q"])
        assert len(list(shelf.glob("*.epub"))) == 2

        main([*argv, "-ae", "-ar", "-q"])

        assert _embedded(shelf) == []

    @pytest.mark.parametrize("route", ["-ao", "-ad"])
    def test_a_vault_gives_it_to_neither(self, zipped_twin, tmp_path, route):
        vault = tmp_path / "vault"
        argv = ["-s", str(zipped_twin), *self.NAMING, "-q"]
        if route == "-ad":
            argv += ["-o", str(tmp_path / "shelf"), "-m", "0"]

        main([*argv, route, str(vault), "--annotations-format", "markdown"])

        assert list(vault.glob("*.md")) == []


class TestAHighlightWithNoBookGoesToNoBook:
    """
    A highlight Apple recorded against no asset is exported under "Unknown
    book", and a book with no source was matched on its title as a package
    name. So every such highlight, from any number of books, was embedded in
    whichever package happened to be called ``Unknown book.epub``.
    """

    @pytest.fixture(name="unowned")
    def _unowned(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        library = tmp_path / "lib"
        package = make_metadata_package(
            library, "Unknown book.epub", title="My Own Notes"
        )
        make_databases(
            tmp_path / "container",
            rows=[
                highlight(uuid="A", asset=None, text="from some other book"),
                highlight(uuid="B", asset=None, text="and from yet another"),
            ],
            books=[library_row(asset="X", path=str(package), title="My Own Notes")],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        return library

    def test_it_is_not_matched_on_the_title_it_was_given(self):
        unowned = {"id": "A", "book": {"title": "Unknown book"}, "text": "x"}

        index = annotations.index_by_book([unowned])

        assert annotations.for_book("Unknown book.epub", index) == []

    def test_a_book_with_an_asset_id_still_matches_on_its_title(self):
        forgotten = {"id": "A", "book": {"title": "Dune", "assetId": "A1"}, "text": "x"}

        index = annotations.index_by_book([forgotten])

        assert annotations.for_book("Dune.epub", index) == [forgotten]

    def test_the_conversion_embeds_it_in_no_book(self, unowned, output_dir):
        main(["-s", str(unowned), "-o", str(output_dir), "-m", "0", "-ae", "-q"])

        assert list(output_dir.glob("*.epub")) == [output_dir / "Unknown book.epub"]
        assert _embedded(output_dir) == []

    def test_a_vault_gives_it_to_no_book(self, unowned, tmp_path):
        vault = tmp_path / "vault"

        main(
            ["-s", str(unowned), "-ao", str(vault), "--annotations-format", "markdown"]
        )

        assert list(vault.glob("*.md")) == []

    def test_a_detached_export_still_carries_it(self, unowned, tmp_path):
        exported = tmp_path / "annotations.json"

        main(["-s", str(unowned), "-ao", str(exported), "-q"])

        held = json.loads(exported.read_text(encoding="utf-8"))["annotations"]
        assert sorted((a["id"], a["book"]["title"]) for a in held) == [
            ("A", "Unknown book"),
            ("B", "Unknown book"),
        ]
