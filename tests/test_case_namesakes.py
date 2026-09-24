"""
Tests for books whose names differ only in case.

``b/dune.epub`` is exported alone; ``a/Dune.epub`` is added later. The two are
different books under the default policy, and one file on a case-insensitive
volume, so the claim pass is what decides which of them holds ``dune.epub``.
It walked the library in sorted order and never looked at the shelf, so the
newcomer, sorting first, took the name: in suffix mode the book already on the
shelf was written again under a suffix and its archive listed as an orphan;
in skip mode both were collisions, on every run.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.collect import annotations
from epubconvert.export.naming import PassthroughNaming
from epubconvert.run import holders, planning, run
from tests.conftest import make_metadata_package
from tests.test_annotations import highlight, library_row, make_databases
from tests.test_copy_claims import identifier_of, listing, zipped_book


def files(output_dir: Path) -> dict[str, str]:
    """Each archive on the shelf, and the book it holds."""
    return {path.name: identifier_of(path) for path in output_dir.glob("*.epub")}


def _exported_then_namesake(
    tmp_path: Path, output_dir: Path, mode: str
) -> tuple[Path, list[str]]:
    library = tmp_path / "lib"
    make_metadata_package(library / "b", "dune.epub", title="Dune", identifier="urn:b")
    argv = ["-s", str(library), "-o", str(output_dir), "--on-collision", mode]
    run.main([*argv, "-m", "0", "-q"])
    make_metadata_package(library / "a", "Dune.epub", title="Dune", identifier="urn:a")
    return library, argv


class TestTheBookOnTheShelfKeepsItsName:
    def test_suffix_mode_writes_only_the_newcomer(self, tmp_path, output_dir, capsys):
        library, argv = _exported_then_namesake(tmp_path, output_dir, "suffix")
        capsys.readouterr()

        run.main([*argv, "-m", "0"])
        first = capsys.readouterr()
        run.main([*argv, "-m", "0"])
        again = capsys.readouterr()

        assert files(output_dir) == {"dune.epub": "urn:b", "Dune (2).epub": "urn:a"}
        assert "Exported 1 epub file(s)" in first.out
        assert "orphan" not in first.out + again.out
        assert "Exported 0 epub file(s)" in again.out
        assert sorted(listing(library, output_dir, capsys, *argv[4:])) == [
            ("Dune.epub", "exported"),
            ("dune.epub", "exported"),
        ]

    def test_skip_mode_has_one_collision(self, tmp_path, output_dir, capsys):
        library, argv = _exported_then_namesake(tmp_path, output_dir, "skip")
        capsys.readouterr()

        run.main([*argv, "-m", "0"])
        captured = capsys.readouterr()

        assert files(output_dir) == {"dune.epub": "urn:b"}
        assert "Already exported, skipping: dune.epub" in captured.err
        assert "1 name collision(s)" in captured.out
        assert sorted(listing(library, output_dir, capsys, *argv[4:])) == [
            ("Dune.epub", "collision"),
            ("dune.epub", "exported"),
        ]

    def test_the_collision_names_the_file_that_holds_the_name(
        self, tmp_path, output_dir, capsys
    ):
        # The default policy gives the two different identities, so the
        # holder was looked up by identity, found nothing, and the reason
        # said "another book already claims this name".
        _, argv = _exported_then_namesake(tmp_path, output_dir, "skip")
        capsys.readouterr()

        run.main([*argv, "-m", "0"])

        assert (
            "Name collision, skipping: Dune.epub (dune.epub already holds this name"
            in capsys.readouterr().err
        )

    @pytest.mark.parametrize("mode", ["skip", "suffix"])
    def test_a_refresh_finds_the_books_archive(
        self, tmp_path, output_dir, monkeypatch, mode
    ):
        library, argv = _exported_then_namesake(tmp_path, output_dir, mode)
        container = tmp_path / "container"
        make_databases(
            container,
            rows=[highlight(asset="B", uuid="UB", text="B TEXT")],
            books=[
                library_row(asset="B", path=str(library / "b" / "dune.epub")),
            ],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(container, policy),
        )

        assert run.main([*argv, "-ae", "-ar", "-q"]) == 0

        with ZipFile(output_dir / "dune.epub") as opened:
            embedded = json.loads(opened.read(annotations.EMBEDDED_PATH))
        assert [entry["text"] for entry in embedded["annotations"]] == ["B TEXT"]


class TestCopiesOfOneNameInTwoCases:
    @pytest.mark.parametrize("mode", ["skip", "suffix"])
    def test_the_copy_on_the_shelf_keeps_its_name(
        self, tmp_path, output_dir, capsys, mode
    ):
        library = tmp_path / "lib"
        zipped_book(tmp_path, library / "b" / "dune.epub", "urn:uuid:B", "Dune")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0"]
        argv += ["--on-collision", mode]
        run.main([*argv, "-q"])
        zipped_book(tmp_path, library / "a" / "Dune.epub", "urn:uuid:A", "Dune!")
        run.main([*argv, "-q"])
        capsys.readouterr()

        run.main(argv)
        again = capsys.readouterr()

        expected = {"dune.epub": "urn:uuid:B"}
        if mode == "suffix":
            expected["Dune (2).epub"] = "urn:uuid:A"
        assert files(output_dir) == expected
        assert "orphan" not in again.out
        assert again.out.count("name collision") == (mode == "skip")

    def test_a_copy_changed_at_its_source_keeps_its_name(
        self, tmp_path, output_dir, capsys
    ):
        # Apple rewrote the zipped book since it was copied: the file on the
        # shelf is no longer its size, and has only its name and identifier.
        library = tmp_path / "lib"
        zipped_book(tmp_path, library / "b" / "dune.epub", "urn:uuid:B", "Dune")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0"]
        argv += ["--on-collision", "suffix"]
        run.main([*argv, "-q"])
        zipped_book(tmp_path / "new", library / "b" / "dune.epub", "urn:uuid:B", "D")
        zipped_book(tmp_path, library / "a" / "Dune.epub", "urn:uuid:A", "Dune!")
        run.main([*argv, "-q"])
        capsys.readouterr()

        run.main(argv)
        again = capsys.readouterr()

        assert files(output_dir) == {
            "dune.epub": "urn:uuid:B",
            "Dune (2).epub": "urn:uuid:A",
        }
        assert "orphan" not in again.out
        assert " copied" not in again.out


class TestAssignNamesWeighsTheShelf:
    def test_a_book_whose_name_is_on_the_shelf_claims_first(self, tmp_path):
        library = tmp_path / "lib"
        first = make_metadata_package(library / "a", "Dune.epub", title="Dune")
        second = make_metadata_package(library / "b", "dune.epub", title="Dune")

        named = planning.assign_names(
            [first, second], PassthroughNaming(), "suffix", shelf={"dune.epub"}
        )

        assert [item.package for item in named] == [first, second]
        assert [item.filename for item in named] == ["Dune (2).epub", "dune.epub"]

    def test_without_a_shelf_sorted_order_decides(self, tmp_path):
        library = tmp_path / "lib"
        first = make_metadata_package(library / "a", "Dune.epub", title="Dune")
        second = make_metadata_package(library / "b", "dune.epub", title="Dune")

        named = planning.assign_names([second, first], PassthroughNaming(), "suffix")

        assert [item.filename for item in named] == ["Dune.epub", "dune (2).epub"]


def _exported_then_renamed(
    tmp_path: Path, output_dir: Path, mode: str, identifier: str = "urn:b"
) -> tuple[Path, list[str]]:
    library = tmp_path / "lib"
    make_metadata_package(
        library / "b", "dune.epub", title="Dune", identifier=identifier
    )
    argv = ["-s", str(library), "-o", str(output_dir), "--on-collision", mode]
    run.main([*argv, "-m", "0", "-q"])
    (library / "b" / "dune.epub").rename(library / "b" / "Dune.epub")
    return library, argv


class TestABookRenamedByCaseOnly:
    """
    Its archive is found under the name's other spelling, and was another
    book's for ever: the identity compared exactly while the lookup folded
    case, so the book was a collision with its own archive, or in suffix
    mode was written again beside it and its archive listed as an orphan.
    """

    @pytest.mark.parametrize("mode", ["skip", "suffix"])
    def test_its_archive_is_its_own(self, tmp_path, output_dir, capsys, mode):
        library, argv = _exported_then_renamed(tmp_path, output_dir, mode)
        capsys.readouterr()

        run.main([*argv, "-m", "0"])
        captured = capsys.readouterr()

        assert files(output_dir) == {"dune.epub": "urn:b"}
        assert "Already exported, skipping: Dune.epub" in captured.err
        assert "collision" not in captured.out + captured.err
        assert "orphan" not in captured.out
        assert listing(library, output_dir, capsys, *argv[4:]) == [
            ("Dune.epub", "exported")
        ]

    def test_force_rewrites_its_own_archive(self, tmp_path, output_dir, capsys):
        _, argv = _exported_then_renamed(tmp_path, output_dir, "skip")
        capsys.readouterr()

        run.main([*argv, "-m", "0", "--force"])
        captured = capsys.readouterr()

        assert files(output_dir) == {"dune.epub": "urn:b"}
        assert "Exported 1 epub file(s)" in captured.out
        assert "collision" not in captured.out

    def test_a_namesake_of_its_old_spelling_takes_no_file_of_it(
        self, tmp_path, output_dir, capsys
    ):
        # a/dune.epub, added under the old spelling, claimed first by its
        # exact name: reported exported from the renamed book's archive,
        # which was written again as "Dune (2)", and the next run wrote the
        # newcomer as "dune (3)" and left that second copy as an orphan.
        library, argv = _exported_then_renamed(tmp_path, output_dir, "suffix")
        make_metadata_package(
            library / "a", "dune.epub", title="Dune", identifier="urn:a"
        )
        capsys.readouterr()

        run.main([*argv, "--list", "--json"])
        rows = json.loads(capsys.readouterr().out)
        run.main([*argv, "-m", "0"])
        first = capsys.readouterr()
        run.main([*argv, "-m", "0"])
        again = capsys.readouterr()

        assert sorted(
            (row["status"], Path(row["target"]).name, Path(row["source"]).parent.name)
            for row in rows
        ) == [("exported", "dune.epub", "b"), ("pending", "dune (2).epub", "a")]
        assert files(output_dir) == {"dune.epub": "urn:b", "dune (2).epub": "urn:a"}
        assert "Exported 1 epub file(s)" in first.out
        assert "Exported 0 epub file(s)" in again.out
        assert "orphan" not in first.out + again.out

    def test_without_an_identifier_the_name_is_trusted(
        self, tmp_path, output_dir, capsys
    ):
        _, argv = _exported_then_renamed(tmp_path, output_dir, "suffix", "none")
        capsys.readouterr()

        run.main([*argv, "-m", "0"])
        captured = capsys.readouterr()

        assert sorted(path.name for path in output_dir.glob("*.epub")) == ["dune.epub"]
        assert "Already exported, skipping: Dune.epub" in captured.err


class TestACopyRenamedByCaseOnly:
    @pytest.mark.parametrize("mode", ["skip", "suffix"])
    def test_it_is_not_copied_again(self, tmp_path, output_dir, capsys, mode):
        library = tmp_path / "lib"
        zipped_book(tmp_path, library / "b" / "dune.epub", "urn:uuid:B", "Dune")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0"]
        argv += ["--on-collision", mode]
        run.main([*argv, "-q"])
        (library / "b" / "dune.epub").rename(library / "b" / "Dune.epub")
        capsys.readouterr()

        run.main(argv)
        captured = capsys.readouterr()

        assert files(output_dir) == {"dune.epub": "urn:uuid:B"}
        assert " copied" not in captured.out
        assert "orphan" not in captured.out


class TestForeign:
    """What :func:`holders.foreign` says of a file of another spelling."""

    @staticmethod
    def _archive(tmp_path: Path, identifier: str) -> Path:
        return zipped_book(tmp_path, tmp_path / "out" / "dune.epub", identifier)

    def test_without_the_source_it_is_another_books(self, tmp_path):
        found = self._archive(tmp_path, "urn:other")

        assert holders.foreign(found, "dune.epub", "Dune.epub", None) == (
            "dune.epub already holds this name"
        )

    def test_a_pdf_declares_no_identifier(self, tmp_path):
        paper = tmp_path / "Paper.pdf"
        paper.write_bytes(b"%PDF-1.4 fake")

        assert holders.source_identifier(paper) is None

    def test_another_books_archive_is_foreign(self, tmp_path):
        found = self._archive(tmp_path, "urn:other")
        source = make_metadata_package(
            tmp_path / "lib", "Dune.epub", title="Dune", identifier="urn:mine"
        )

        reason = holders.foreign(
            found, "dune.epub", "Dune.epub", None, source=source, live=set()
        )

        assert reason == (
            "dune.epub holds another book, urn:other; this book is urn:mine"
        )

    def test_unread_it_is_another_live_books_by_its_exact_name(self, tmp_path):
        found = self._archive(tmp_path, "none")
        source = make_metadata_package(
            tmp_path / "lib", "Dune.epub", title="Dune", identifier="none"
        )

        claimed = holders.foreign(
            found, "dune.epub", "Dune.epub", None, source=source, live={"dune.epub"}
        )
        trusted = holders.foreign(
            found, "dune.epub", "Dune.epub", None, source=source, live=set()
        )

        assert claimed == "dune.epub already holds this name"
        assert trusted is None


class TestAShelfFileWhoseExtensionIsUppercase:
    """
    The shelf was read with ``glob("*.epub")``, which is case-sensitive, so a
    zipped book copied through as ``Foo.EPUB`` was invisible to the plan, the
    orphan check and the placing: a package ``Foo.epub`` was judged free and,
    on a case-insensitive volume, written over it.
    """

    def test_a_package_is_not_written_over_it(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        zipped_book(tmp_path, library / "b" / "Foo.EPUB", "urn:uuid:ZIPPED")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0"]
        run.main([*argv, "-q"])
        make_metadata_package(library / "a", "Foo.epub", title="F", identifier="urn:p")
        capsys.readouterr()

        listed = listing(library, output_dir, capsys)
        run.main(argv)
        captured = capsys.readouterr()

        assert ("Foo.epub", "collision") in listed
        assert "holds another book, urn:uuid:ZIPPED" in captured.err
        assert sorted(path.name for path in output_dir.iterdir()) == [
            ".ibook2epub.lock",
            "Foo.EPUB",
        ]

    def test_one_no_book_claims_is_an_orphan(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_metadata_package(library, "Dune.epub", title="Dune")
        zipped_book(tmp_path, output_dir / "Old.EPUB", "urn:uuid:OLD")
        (output_dir / "Old.PDF").write_bytes(b"%PDF-1.4 old")

        assert ("Old.EPUB", "orphan") in listing(library, output_dir, capsys)
        assert ("Old.PDF", "orphan") in listing(library, output_dir, capsys)
