"""
Tests for which note in a vault a book's highlights are written into.

A note shares its stem with the book's file on the shelf. It was named from
the book's assigned name, not from the file the plan placed the book at: a
book moved on to its marked name, because the file under its plain name holds
another edition, had its highlights written into that edition's note. And a
book with no package -- already zipped, or a PDF -- got no note at all.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import json
from pathlib import Path

import pytest

from epubconvert.collect import annotations
from epubconvert.collect import package as package_reader
from epubconvert.export.naming import disambiguator
from epubconvert.run.run import main
from tests.conftest import make_metadata_package
from tests.test_annotations import highlight, library_row, make_databases
from tests.test_copy_claims import zipped_book
from tests.test_copy_through import _count_opens, _evict
from tests.test_source import REAL_ENCRYPTION, add_meta

PLAIN_NOTE = "Frank Herbert - Dune.md"


def _read_from(monkeypatch: pytest.MonkeyPatch, container: Path) -> None:
    monkeypatch.setattr(
        "epubconvert.run.annotating.collect_annotations",
        lambda policy=None, **kwargs: annotations.collect(container, policy, **kwargs),
    )


def _edition(library: Path, container: Path, asset: str, identifier: str) -> None:
    """Put one edition of Dune in *library*, with one highlight in Apple's."""
    book = make_metadata_package(
        library,
        "Dune.epub",
        title="Dune",
        creator="Frank Herbert",
        identifier=identifier,
    )
    make_databases(
        container,
        rows=[highlight(asset=asset, uuid=f"U{asset}", text=f"EDITION {asset} TEXT")],
        books=[library_row(asset=asset, path=str(book), title="Dune")],
    )


class TestANoteFollowsTheBooksArchive:
    """
    Edition A is exported, leaves the library, and edition B arrives: A's
    archive keeps the plain name and B moves on to its marked name.
    """

    FLAGS = ["-m", "0", "-q", "--name-by", "author-title", "--on-collision", "suffix"]

    def _replace_the_edition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vault: list[str]
    ) -> tuple[Path, Path, Path]:
        container, output = tmp_path / "container", tmp_path / "out"
        _read_from(monkeypatch, container)
        first = tmp_path / "lib1"
        _edition(first, container, "A", "urn:isbn:9780441013593")
        assert main(["-s", str(first), "-o", str(output), *self.FLAGS, *vault]) == 0
        second = tmp_path / "lib2"
        _edition(second, container, "B", "urn:isbn:9780593099322")
        return second, output, container

    @staticmethod
    def _notes(vault: Path) -> dict[str, str]:
        return {note.name: note.read_text() for note in vault.glob("*.md")}

    def test_after_a_conversion(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        flags = ["-ad", str(vault), "--annotations-format", "markdown"]
        library, output, _ = self._replace_the_edition(tmp_path, monkeypatch, flags)

        assert main(["-s", str(library), "-o", str(output), *self.FLAGS, *flags]) == 0

        [moved] = [
            path for path in output.glob("*.epub") if path.stem + ".md" != (PLAIN_NOTE)
        ]
        notes = self._notes(vault)
        assert "EDITION A TEXT" in notes[PLAIN_NOTE]
        assert "EDITION B TEXT" not in notes[PLAIN_NOTE]
        assert "EDITION B TEXT" in notes[moved.stem + ".md"]

    def test_for_a_book_that_cannot_be_converted(self, tmp_path, monkeypatch):
        # DRM-protected, so the plan decided it with no target: the note was
        # named after its plain name, the other edition's, and the run exited
        # 1 telling the reader two books want one note.
        vault = tmp_path / "vault"
        flags = ["-ad", str(vault), "--annotations-format", "markdown"]
        library, output, _ = self._replace_the_edition(tmp_path, monkeypatch, flags)
        add_meta(library / "Dune.epub", "META-INF/encryption.xml", REAL_ENCRYPTION)

        code = main(["-s", str(library), "-o", str(output), *self.FLAGS, *flags])

        marked = f"Frank Herbert - Dune [{disambiguator('urn:isbn:9780593099322')}].md"
        notes = self._notes(vault)
        assert code == 0
        assert "EDITION B TEXT" not in notes[PLAIN_NOTE]
        assert "EDITION B TEXT" in notes[marked]

    def test_after_a_refresh(self, tmp_path, monkeypatch):
        vault = tmp_path / "vault"
        flags = ["-ad", str(vault), "--annotations-format", "markdown"]
        library, output, _ = self._replace_the_edition(tmp_path, monkeypatch, flags)
        assert main(["-s", str(library), "-o", str(output), *self.FLAGS]) == 0

        code = main(
            ["-s", str(library), "-o", str(output), "-ae", "-ar", "-q"]
            + self.FLAGS[3:]
            + flags
        )

        assert code == 0
        [moved] = [
            path for path in output.glob("*.epub") if path.stem + ".md" != (PLAIN_NOTE)
        ]
        notes = self._notes(vault)
        assert "EDITION B TEXT" not in notes[PLAIN_NOTE]
        assert "EDITION B TEXT" in notes[moved.stem + ".md"]


class TestABookWithNoPackageGetsANote:
    """
    A vault wrote notes for the packages alone, so the highlights of a book
    that arrived already zipped reached no file: "Wrote 1 note(s)", exit 0,
    and nothing said.
    """

    @staticmethod
    def _library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        library, container = tmp_path / "lib", tmp_path / "container"
        _read_from(monkeypatch, container)
        alpha = make_metadata_package(
            library / "pkg", "Alpha.epub", title="Alpha", identifier="urn:uuid:A"
        )
        beta = zipped_book(tmp_path, library / "zipped" / "Beta.epub", "urn:uuid:B")
        make_databases(
            container,
            rows=[
                highlight(asset="A", uuid="UA", text="ALPHA TEXT"),
                highlight(asset="B", uuid="UB1", text="BETA ONE"),
                highlight(asset="B", uuid="UB2", text="BETA TWO"),
            ],
            books=[
                library_row(asset="A", path=str(alpha), title="Alpha"),
                library_row(asset="B", path=str(beta), title="Beta"),
            ],
        )
        return library

    @staticmethod
    def _assert_both(vault: Path) -> None:
        assert sorted(note.name for note in vault.glob("*.md")) == [
            "Alpha.md",
            "Beta.md",
        ]
        beta = (vault / "Beta.md").read_text()
        assert "BETA ONE" in beta
        assert "BETA TWO" in beta

    def test_after_a_conversion(self, tmp_path, monkeypatch):
        library = self._library(tmp_path, monkeypatch)
        vault = tmp_path / "vault"

        code = main(
            ["-s", str(library), "-o", str(tmp_path / "out"), "-m", "0", "-q"]
            + ["-ad", str(vault), "--annotations-format", "markdown"]
        )

        assert code == 0
        self._assert_both(vault)

    def test_after_a_refresh(self, tmp_path, monkeypatch):
        library = self._library(tmp_path, monkeypatch)
        output, vault = tmp_path / "out", tmp_path / "vault"
        main(["-s", str(library), "-o", str(output), "-m", "0", "-q"])

        code = main(
            ["-s", str(library), "-o", str(output), "-ae", "-ar", "-q"]
            + ["-ad", str(vault), "--annotations-format", "markdown"]
        )

        assert code == 0
        self._assert_both(vault)

    def test_with_nothing_converted(self, tmp_path, monkeypatch):
        library = self._library(tmp_path, monkeypatch)
        vault = tmp_path / "vault"

        code = main(
            ["-s", str(library), "-ao", str(vault), "--annotations-format", "markdown"]
            + ["-q"]
        )

        assert code == 0
        self._assert_both(vault)


class TestAVaultLeavesAnEvictedBookAlone:
    """
    A vault names the books copied through as the shelf would, and under
    ``--name-by author-title`` that opens a zipped epub: for an evicted one,
    a download. ``--skip-incomplete`` is what says not to, and it was refused.
    """

    def test_skip_incomplete_is_honoured(self, tmp_path, monkeypatch):
        library, container = tmp_path / "lib", tmp_path / "container"
        _read_from(monkeypatch, container)
        book = make_metadata_package(
            library, "Package.epub", title="Package", identifier="urn:uuid:P"
        )
        evicted = zipped_book(tmp_path, library / "Zipped.epub", "urn:uuid:Z")
        make_databases(
            container,
            rows=[highlight(asset="P", uuid="UP", text="PACKAGE TEXT")],
            books=[library_row(asset="P", path=str(book), title="Package")],
        )
        _evict(monkeypatch, evicted)
        opened = _count_opens(monkeypatch)

        code = main(
            ["-s", str(library), "-ao", str(tmp_path / "vault"), "-q"]
            + ["--annotations-format", "markdown", "--name-by", "author-title"]
            + ["--skip-incomplete"]
        )

        assert code == 0
        assert not opened


def _package_reads(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record every package document read from a package directory."""
    read: list[Path] = []
    original = package_reader.read_package_dir

    def counting(package: Path):
        read.append(package)
        return original(package)

    for module in ("run.holders", "run.placing", "run.planning", "collect.library"):
        monkeypatch.setattr(f"epubconvert.{module}.read_package_dir", counting)
    return read


class TestAVaultLeavesAnEvictedPackageAlone:
    """
    Every vault route placed the books without ``--skip-incomplete``'s
    books left unopened, so a package renamed by case had its identifier
    read to judge the file under its old spelling, and ``-ar`` read it
    again before the write; and the annotations read each highlighted
    book's package document. Each downloaded the book the flag exists to
    leave where it is.
    """

    ROUTES = [
        ["-ao", "{vault}"],
        ["-m", "0", "-ad", "{vault}"],
        ["-ae", "-ar", "-ad", "{vault}"],
    ]

    @pytest.mark.parametrize("route", ROUTES)
    def test_no_route_reads_it(self, tmp_path, monkeypatch, route):
        library, container = tmp_path / "lib", tmp_path / "container"
        output, vault = tmp_path / "out", tmp_path / "vault"
        _read_from(monkeypatch, container)
        book = make_metadata_package(
            library / "a", "dune.epub", title="Dune", identifier="urn:uuid:P"
        )
        main(["-s", str(library), "-o", str(output), "-m", "0", "-q"])
        book = book.rename(library / "a" / "Dune.epub")
        make_databases(
            container,
            rows=[highlight(asset="P", uuid="UP", text="THE TEXT")],
            books=[library_row(asset="P", path=str(book), title="Dune")],
        )
        _evict(monkeypatch, *(path for path in book.rglob("*") if path.is_file()))
        read = _package_reads(monkeypatch)

        code = main(
            ["-s", str(library), "-o", str(output), "-q", "--skip-incomplete"]
            + [part.format(vault=vault) for part in route]
            + ["--annotations-format", "markdown"]
        )

        assert code == 0
        assert book not in read
        assert [note.name for note in vault.glob("*.md")] == ["dune.md"]

    @pytest.mark.parametrize("route", ROUTES[:2])
    def test_nor_names_it_from_its_package_document(
        self, tmp_path, monkeypatch, capsys, route
    ):
        library, container = tmp_path / "lib", tmp_path / "container"
        output, vault = tmp_path / "out", tmp_path / "vault"
        _read_from(monkeypatch, container)
        _edition(library, container, "A", "urn:uuid:A")
        book = library / "Dune.epub"
        _evict(monkeypatch, *(path for path in book.rglob("*") if path.is_file()))
        read = _package_reads(monkeypatch)

        code = main(
            ["-s", str(library), "-o", str(output), "-q", "--skip-incomplete"]
            + [part.format(vault=vault) for part in route]
            + ["--annotations-format", "markdown", "--name-by", "author-title"]
        )

        err = capsys.readouterr().err
        assert code == 0
        assert book not in read
        assert "lost a name collision" not in err
        assert (
            "1 book(s) not downloaded from iCloud could not be named without "
            "downloading them, so their highlights were not written: Dune.epub"
        ) in err


class TestANoteForABookRenamedByCase:
    """
    Under ``-p`` a book renamed from ``dune.epub`` to ``Dune.epub`` is found
    at its archive ``dune.epub``, and the conversion route names its note
    ``dune.md`` after that file. The refresh route kept the assigned name and
    wrote ``Dune.md``, a second note for one book.
    """

    FLAGS = ["-p", "strip", "--annotations-format", "markdown", "-q"]

    def test_every_route_names_it_after_the_file(self, tmp_path, monkeypatch):
        library, container = tmp_path / "lib", tmp_path / "container"
        output = tmp_path / "out"
        _read_from(monkeypatch, container)
        book = make_metadata_package(
            library / "b", "dune.epub", title="Dune", identifier="urn:uuid:D"
        )
        main(["-s", str(library), "-o", str(output), "-m", "0", "-q", "-p", "strip"])
        book = book.rename(library / "b" / "Dune.epub")
        make_databases(
            container,
            rows=[highlight(asset="D", uuid="UD", text="DUNE TEXT")],
            books=[library_row(asset="D", path=str(book), title="Dune")],
        )
        converted, refreshed = tmp_path / "converted", tmp_path / "refreshed"
        argv = ["-s", str(library), "-o", str(output), *self.FLAGS]

        main([*argv, "-m", "0", "-ad", str(converted)])
        main([*argv, "-ae", "-ar", "-ad", str(refreshed)])

        assert sorted(note.name for note in converted.glob("*.md")) == ["dune.md"]
        assert sorted(note.name for note in refreshed.glob("*.md")) == ["dune.md"]


class TestANoteWithoutAConversionFollowsTheRun:
    """
    ``-ao`` places only the books with highlights by their identifiers, so as
    not to open every archive on the shelf. But where a book moves on past
    another book's archive depends on the books placed before it: a book
    without highlights that the run moves on, ``-ao`` left at its name, and
    the highlighted book after it took a different number, so its note was
    named after a file the run never writes.
    """

    def test_it_is_named_after_the_file_the_run_writes(
        self, tmp_path, monkeypatch, capsys
    ):
        library, container = tmp_path / "lib", tmp_path / "container"
        output = tmp_path / "out"
        _read_from(monkeypatch, container)
        # Two books since deleted from the library left their archives
        # under the name the package and the zipped book want.
        zipped_book(tmp_path / "r1", output / "Dune.epub", "urn:uuid:R1")
        zipped_book(tmp_path / "r2", output / "Dune (2).epub", "urn:uuid:R2")
        make_metadata_package(
            library / "a", "Dune.epub", title="Dune", identifier="urn:uuid:X"
        )
        # A name of its own in the library, which -p strip makes the other's.
        book = zipped_book(tmp_path, library / "b" / "Dune<.epub", "urn:uuid:H")
        make_databases(
            container,
            rows=[highlight(asset="H", uuid="UH", text="H TEXT")],
            books=[library_row(asset="H", path=str(book), title="Dune")],
        )
        argv = ["-s", str(library), "-o", str(output), "-p", "strip"]
        argv += ["--on-collision", "suffix"]
        capsys.readouterr()
        main([*argv, "--list", "--json"])
        [target] = [
            Path(row["target"]).stem
            for row in json.loads(capsys.readouterr().out)
            if row["source"] == str(book)
        ]
        vault = tmp_path / "vault"

        main([*argv, "-ao", str(vault), "--annotations-format", "markdown", "-q"])

        assert sorted(note.name for note in vault.glob("*.md")) == [f"{target}.md"]
