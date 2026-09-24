"""
Tests for a file copied through and a package that want one shelf name.

A package directory ``a/Book.epub/`` and an already-zipped ``b/Book.epub`` are
given the same name, but the packages were named by the planner and the copies
by :func:`~epubconvert.run.planning.copy_target_name`, and the two never met.
The copy ran first, so the package found the zipped book's file under its name
and was reported exported from it, on every run for ever; the other way round
the copy found the package's archive, took it for its own and said nothing.
Two zipped editions under one ``--name-by author-title`` name lost the second
the same way.

The copies now take their names in the same claim pass as the packages, after
them, so the loser is a reported collision or, under ``--on-collision
suffix``, takes a suffix.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

from epubconvert.collect.validate import read_package
from epubconvert.export.archive import collect_package_dirs
from epubconvert.export.naming import PassthroughNaming
from epubconvert.run import copying, copynames, planning, run
from tests.conftest import make_metadata_package, make_package, remove_tree

SUFFIX = ["--on-collision", "suffix"]


def zipped(source: Path, target: Path) -> Path:
    """Zip a package directory into an already-valid epub file at *target*."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(target, "w") as opened:
        opened.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
        for member in sorted(source.rglob("*")):
            if member.is_file() and member.name != "mimetype":
                opened.write(
                    member,
                    member.relative_to(source).as_posix(),
                    compress_type=ZIP_DEFLATED,
                )
    return target


def zipped_book(
    tmp_path: Path, target: Path, identifier: str, title: str = "Zipped"
) -> Path:
    """An already-zipped epub declaring *identifier*."""
    staged = make_metadata_package(
        tmp_path / "stage" / identifier.rsplit(":", 1)[-1],
        "x.epub",
        title=title,
        creator="Frank Herbert",
        identifier=identifier,
    )
    return zipped(staged, target)


def identifier_of(archive: Path) -> str:
    with ZipFile(archive) as opened:
        return str(read_package(opened).identifier)


def listing(
    library: Path, output_dir: Path, capsys, *extra: str
) -> list[tuple[str, str]]:
    capsys.readouterr()
    run.main(["-s", str(library), "-o", str(output_dir), "--list", "--json", *extra])
    return [(row["name"], row["status"]) for row in json.loads(capsys.readouterr().out)]


def shelf(output_dir: Path) -> list[str]:
    return sorted(path.name for path in output_dir.glob("*.epub"))


def _package_and_zip(tmp_path: Path) -> Path:
    library = tmp_path / "lib"
    make_metadata_package(
        library / "a", "Book.epub", title="Package", identifier="urn:uuid:PACKAGE"
    )
    zipped_book(tmp_path, library / "b" / "Book.epub", "urn:uuid:ZIPPED")
    return library


class TestAPackageAndACopyOfOneName:
    def test_the_package_reaches_the_shelf_and_the_copy_is_a_collision(
        self, tmp_path, output_dir, capsys
    ):
        library = _package_and_zip(tmp_path)

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0"])
        captured = capsys.readouterr()

        assert code == 0
        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:PACKAGE"
        assert "Already exported" not in captured.err
        assert "Name collision, skipping: Book.epub" in captured.err
        assert "1 name collision(s)" in captured.out

    def test_the_listing_agrees_with_the_run(self, tmp_path, output_dir, capsys):
        library = _package_and_zip(tmp_path)

        before = listing(library, output_dir, capsys)
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        after = listing(library, output_dir, capsys)

        assert sorted(before) == [("Book.epub", "collision"), ("Book.epub", "pending")]
        assert sorted(after) == [("Book.epub", "collision"), ("Book.epub", "exported")]

    def test_suffix_mode_keeps_both(self, tmp_path, output_dir, capsys):
        library = _package_and_zip(tmp_path)
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", *SUFFIX]

        assert run.main(argv) == 0
        capsys.readouterr()
        assert run.main(argv) == 0
        again = capsys.readouterr()

        assert shelf(output_dir) == ["Book (2).epub", "Book.epub"]
        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:PACKAGE"
        assert identifier_of(output_dir / "Book (2).epub") == "urn:uuid:ZIPPED"
        assert "collision" not in again.out + again.err
        assert "orphan" not in again.out

    def test_force_does_not_write_the_package_over_the_copy(
        self, tmp_path, output_dir, capsys
    ):
        # Neither declares an identifier, so nothing but the claim tells them
        # apart: the copy landed first and the package was forced over it.
        library = tmp_path / "lib"
        make_package(library / "a", "Book.epub")
        (library / "b").mkdir(parents=True)
        with ZipFile(library / "b" / "Book.epub", "w") as opened:
            opened.writestr("mimetype", "application/epub+zip")
            opened.writestr("META-INF/container.xml", "<container/>")
            opened.writestr("ZIPPED_MARKER", "the zipped one")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "--force"]

        for _ in range(2):
            run.main(argv)
            captured = capsys.readouterr()
            with ZipFile(output_dir / "Book.epub") as opened:
                assert "ZIPPED_MARKER" not in opened.namelist()
            assert "copied" not in captured.out
            assert "Name collision, skipping: Book.epub" in captured.err


class TestACopyAddedBesideAnExportedPackage:
    def test_is_reported_rather_than_dropped(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_metadata_package(
            library / "a", "Book.epub", title="P", identifier="urn:uuid:PACKAGE"
        )
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0"]
        run.main([*argv, "-q"])
        zipped_book(tmp_path, library / "b" / "Book.epub", "urn:uuid:ZIPPED")
        capsys.readouterr()

        code = run.main(argv)
        captured = capsys.readouterr()

        assert code == 0
        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:PACKAGE"
        assert "Name collision, skipping: Book.epub" in captured.err
        assert "1 name collision(s)" in captured.out

    def test_suffix_mode_copies_it_under_a_suffix(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_metadata_package(
            library / "a", "Book.epub", title="P", identifier="urn:uuid:PACKAGE"
        )
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q", *SUFFIX]
        run.main(argv)
        zipped_book(tmp_path, library / "b" / "Book.epub", "urn:uuid:ZIPPED")

        run.main(argv)

        assert identifier_of(output_dir / "Book (2).epub") == "urn:uuid:ZIPPED"


class TestAPackageAddedBesideACopiedBook:
    """The copy is on the shelf first; the package cannot claim its file."""

    @staticmethod
    def _copied_then_package(
        tmp_path: Path, output_dir: Path
    ) -> tuple[Path, list[str]]:
        library = tmp_path / "lib"
        zipped_book(tmp_path, library / "b" / "Book.epub", "urn:uuid:ZIPPED")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0"]
        run.main([*argv, "-q"])
        make_metadata_package(
            library / "a", "Book.epub", title="P", identifier="urn:uuid:PACKAGE"
        )
        return library, argv

    def test_the_package_is_not_reported_exported_from_the_copy(
        self, tmp_path, output_dir, capsys
    ):
        library, argv = self._copied_then_package(tmp_path, output_dir)
        capsys.readouterr()

        run.main(argv)
        captured = capsys.readouterr()
        listed = listing(library, output_dir, capsys)

        assert "Already exported" not in captured.err
        assert "holds another book, urn:uuid:ZIPPED" in captured.err
        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:ZIPPED"
        assert ("Book.epub", "exported") not in listed
        assert ("Book.epub", "orphan") not in listed

    def test_force_does_not_write_the_package_over_it(self, tmp_path, output_dir):
        _, argv = self._copied_then_package(tmp_path, output_dir)

        run.main([*argv, "-q", "--force"])

        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:ZIPPED"


class TestTwoCopiesOfOneName:
    AUTHOR_TITLE = ["--name-by", "author-title"]

    @staticmethod
    def _editions(tmp_path: Path) -> Path:
        library = tmp_path / "lib"
        for folder, identifier in (("a", "urn:uuid:1965"), ("b", "urn:uuid:ACE")):
            zipped_book(
                tmp_path,
                library / folder / f"Dune {folder}.epub",
                identifier,
                title="Dune",
            )
        return library

    def test_suffix_mode_keeps_both(self, tmp_path, output_dir):
        library = self._editions(tmp_path)

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
            + self.AUTHOR_TITLE
            + SUFFIX
        )

        assert code == 0
        assert shelf(output_dir) == [
            "Frank Herbert - Dune (2).epub",
            "Frank Herbert - Dune.epub",
        ]

    def test_skip_mode_reports_the_second(self, tmp_path, output_dir, capsys):
        library = self._editions(tmp_path)

        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0"] + self.AUTHOR_TITLE
        )
        captured = capsys.readouterr()

        assert shelf(output_dir) == ["Frank Herbert - Dune.epub"]
        assert identifier_of(output_dir / "Frank Herbert - Dune.epub") == (
            "urn:uuid:1965"
        )
        assert "Name collision, skipping: Dune b.epub" in captured.err
        assert "1 copied" in captured.out
        assert "1 name collision(s)" in captured.out


class TestACopyWhoseNameHoldsAnotherBook:
    """A book deleted from the library left its archive under the copy's name."""

    @staticmethod
    def _left_behind(tmp_path: Path, output_dir: Path, *extra: str) -> list[str]:
        library = tmp_path / "lib"
        make_metadata_package(
            library / "a", "Book.epub", title="Old", identifier="urn:uuid:OLD"
        )
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", *extra]
        run.main([*argv, "-q"])
        remove_tree(library / "a")
        zipped_book(tmp_path, library / "b" / "Book.epub", "urn:uuid:ZIPPED")
        return argv

    def test_it_is_reported_and_the_archive_kept(self, tmp_path, output_dir, capsys):
        argv = self._left_behind(tmp_path, output_dir)
        capsys.readouterr()

        run.main(argv)
        captured = capsys.readouterr()

        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:OLD"
        assert "holds another book, urn:uuid:OLD" in captured.err
        assert "1 name collision(s)" in captured.out
        # The deleted book's archive is still nobody's in the library.
        assert "1 orphaned" in captured.out

    def test_suffix_mode_moves_it_on(self, tmp_path, output_dir, capsys):
        argv = self._left_behind(tmp_path, output_dir, *SUFFIX)

        run.main([*argv, "-q"])
        capsys.readouterr()
        run.main(argv)
        again = capsys.readouterr()

        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:OLD"
        assert identifier_of(output_dir / "Book (2).epub") == "urn:uuid:ZIPPED"
        assert "copied" not in again.out
        assert "collision" not in again.out


class TestTheClaimPass:
    """What :func:`copynames.claim_copies` decides, without a run around it."""

    def test_a_copy_already_on_the_shelf_keeps_its_file(self, tmp_path, output_dir):
        # Re-zipped since it was copied, so only its identifier says it is
        # the same book.
        library = _package_and_zip(tmp_path)
        zipped_book(tmp_path / "old", output_dir / "Book.epub", "urn:uuid:ZIPPED", "Z")
        policy = PassthroughNaming()
        packages = collect_package_dirs(library)

        names = copynames.claim_copies(
            planning.assign_names(packages, policy, "skip"),
            [(library / "b" / "Book.epub", "Book.epub")],
            policy,
            "skip",
            output_dir=output_dir,
        )

        [package] = names.packages
        [copy] = names.copies
        assert copy.filename == "Book.epub"
        assert copy.identifier == "urn:uuid:ZIPPED"
        assert package.identifier == "urn:uuid:PACKAGE"

    def test_a_file_left_unnamed_stays_unnamed(self, tmp_path):
        unnamed = tmp_path / "Evicted.epub"
        plan = copying.CopyPlan(((unnamed, None),), frozenset({unnamed}))

        assert copying.placed_copies(plan, []) == plan


class TestMatchNarrowsTheCopies:
    """
    ``--match hobbit -m 1`` converted one book and copied every PDF and
    zipped book in the library: on an iCloud library, a full download for
    "convert one book". The match narrows the copying; the names are still
    claimed against the whole library, so a matched copy gets the name a full
    run gives it.
    """

    @staticmethod
    def _library(tmp_path: Path) -> Path:
        library = tmp_path / "lib"
        make_package(library, "The Hobbit.epub")
        make_package(library, "Dune.epub")
        for index in range(3):
            (library / f"unrelated{index}.pdf").write_bytes(b"%PDF-1.4 fake")
        (library / "Hobbit Maps.pdf").write_bytes(b"%PDF-1.4 maps")
        return library

    def test_only_the_matching_files_are_copied(self, tmp_path, output_dir, capsys):
        library = self._library(tmp_path)

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "--match", "hobbit", "-m", "1"]
        )

        assert code == 0
        assert sorted(path.name for path in output_dir.iterdir()) == [
            ".ibook2epub.lock",
            "Hobbit Maps.pdf",
            "The Hobbit.epub",
        ]
        assert "1 copied" in capsys.readouterr().out

    def test_the_cap_on_exports_does_not_hold_copies_back(self, tmp_path, output_dir):
        # -m counts books to convert, the slow part; a copy is not one.
        library = self._library(tmp_path)

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "1", "-q"])

        assert len(list(output_dir.glob("*.pdf"))) == 4
        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_a_matched_copy_is_named_against_the_whole_library(
        self, tmp_path, output_dir, capsys
    ):
        library = TestTwoCopiesOfOneName._editions(  # pylint: disable=protected-access
            tmp_path
        )
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0"]
        argv += TestTwoCopiesOfOneName.AUTHOR_TITLE

        run.main([*argv, "--match", "dune b"])
        narrowed = capsys.readouterr()
        run.main([*argv, "-q"])

        assert "Name collision, skipping: Dune b.epub" in narrowed.err
        assert "Dune a.epub" not in narrowed.err
        assert identifier_of(output_dir / "Frank Herbert - Dune.epub") == (
            "urn:uuid:1965"
        )


class TestADryRunSaysWhatItWouldCopy:
    """
    ``-d`` said "would export 1" and nothing about copies, and the real run
    then said "3 copied": the rehearsal left out the files it would take
    along, and the ones it would skip.
    """

    def test_the_copies_are_counted(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        for index in range(3):
            (library / f"paper{index}.pdf").write_bytes(b"%PDF-1.4 fake")
        (output_dir / "paper0.pdf").write_bytes(b"%PDF-1.4 fake")
        argv = ["-s", str(library), "-o", str(output_dir)]

        run.main([*argv, "-d"])
        rehearsed = capsys.readouterr().out
        run.main(argv)
        done = capsys.readouterr().out

        assert "2 to copy" in rehearsed
        assert "2 copied" in done
        assert sorted(path.name for path in output_dir.glob("*.pdf")) == [
            "paper0.pdf",
            "paper1.pdf",
            "paper2.pdf",
        ]

    def test_a_collision_is_rehearsed_too(self, tmp_path, output_dir, capsys):
        library = _package_and_zip(tmp_path)

        run.main(["-s", str(library), "-o", str(output_dir), "-d"])
        captured = capsys.readouterr()

        assert "Name collision, skipping: Book.epub" in captured.err
        assert "1 name collision(s)" in captured.out
        assert "to copy" not in captured.out
        assert not output_dir.joinpath("Book.epub").exists()
