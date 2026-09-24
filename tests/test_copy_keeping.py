"""
Tests for which file on the shelf a copy keeps as its own, and when it may not.

A copy already on the shelf keeps its file rather than being copied again
(:func:`~epubconvert.run.copynames.claim_copies`). Each test here starts from a
shelf where that rule could hand one file to two books: two copies of one
book, a copy and a package of one book, or two files of one size.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.run import run
from tests.conftest import make_metadata_package
from tests.test_copy_claims import SUFFIX, identifier_of, listing, shelf, zipped_book

DUNE = "urn:isbn:9780441013593"

#: Modification times, in nanoseconds: one long past, one after every test ran.
EARLIER = 1_000_000_000 * 1_000_000_000
LATER = 4_000_000_000 * 1_000_000_000


def _argv(library: Path, output_dir: Path, mode: str) -> list[str]:
    return ["-s", str(library), "-o", str(output_dir), "--on-collision", mode]


def _two_copies_of_one_book(tmp_path: Path) -> Path:
    """Two zipped copies of one book, one identifier, two sizes."""
    library = tmp_path / "lib"
    zipped_book(tmp_path, library / "a" / "Dune.epub", DUNE, "Dune")
    grown = zipped_book(tmp_path / "b", library / "b" / "Dune.epub", DUNE, "Dune")
    with ZipFile(grown, "a") as opened:
        opened.writestr("OEBPS/extra.txt", "x" * 100)
    return library


class TestTwoCopiesOfOneBook:
    """
    Two zipped copies of one book, told apart by nothing but their size, both
    took the one file on the shelf for their own: in skip mode the collision
    the first run reported was gone from the next, and in suffix mode the
    second copy's own file was listed as an orphan.
    """

    def test_skip_mode_reports_the_collision_on_every_run(
        self, tmp_path, output_dir, capsys
    ):
        library = _two_copies_of_one_book(tmp_path)
        argv = _argv(library, output_dir, "skip")
        run.main([*argv, "-m", "0", "-q"])
        capsys.readouterr()

        run.main([*argv, "-m", "0"])
        again = capsys.readouterr()
        listed = listing(library, output_dir, capsys, "--on-collision", "skip")

        assert "1 name collision(s)" in again.out
        assert sorted(listed) == [("Dune.epub", "collision"), ("Dune.epub", "copied")]

    def test_suffix_mode_lists_neither_file_as_an_orphan(
        self, tmp_path, output_dir, capsys
    ):
        library = _two_copies_of_one_book(tmp_path)
        argv = _argv(library, output_dir, "suffix")
        run.main([*argv, "-m", "0", "-q"])
        capsys.readouterr()

        run.main([*argv, "-m", "0"])
        again = capsys.readouterr()
        listed = listing(library, output_dir, capsys, *SUFFIX)

        assert shelf(output_dir) == ["Dune (2).epub", "Dune.epub"]
        assert "orphan" not in again.out
        assert " copied" not in again.out
        assert sorted(listed) == [("Dune.epub", "copied"), ("Dune.epub", "copied")]


class TestACopyOfAPackagesBook:
    """
    A zipped copy declaring a package's identifier took the package's archive
    for its own, though the package had just written it: the collision
    vanished on the next run, and in suffix mode the copy's own file was an
    orphan.
    """

    @staticmethod
    def _package_and_copy(tmp_path: Path) -> Path:
        library = tmp_path / "lib"
        make_metadata_package(
            library / "a",
            "Dune.epub",
            title="Dune",
            creator="Frank Herbert",
            identifier=DUNE,
        )
        zipped_book(tmp_path, library / "b" / "Dune.epub", DUNE, "Dune")
        return library

    def test_skip_mode_reports_the_collision_on_every_run(
        self, tmp_path, output_dir, capsys
    ):
        library = self._package_and_copy(tmp_path)
        argv = _argv(library, output_dir, "skip")
        run.main([*argv, "-m", "0", "-q"])
        capsys.readouterr()

        run.main([*argv, "-m", "0"])
        again = capsys.readouterr()
        listed = listing(library, output_dir, capsys)

        assert "1 name collision(s)" in again.out
        assert sorted(listed) == [
            ("Dune.epub", "collision"),
            ("Dune.epub", "exported"),
        ]

    @pytest.mark.parametrize("policy", [[], ["--name-by", "author-title"]])
    def test_suffix_mode_lists_no_orphan(self, tmp_path, output_dir, capsys, policy):
        library = self._package_and_copy(tmp_path)
        argv = [*_argv(library, output_dir, "suffix"), *policy]
        run.main([*argv, "-m", "0", "-q"])
        capsys.readouterr()

        run.main([*argv, "-m", "0"])
        again = capsys.readouterr()

        assert "orphan" not in again.out
        assert " copied" not in again.out
        assert sorted(identifier_of(path) for path in output_dir.glob("*.epub")) == [
            DUNE,
            DUNE,
        ]


class TestTwoPdfsOfOneSize:
    def test_skip_mode_reports_the_collision_on_every_run(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        for folder, text in (("a", b"AAAA"), ("b", b"BBBB")):
            (library / folder).mkdir(parents=True)
            (library / folder / "Paper.pdf").write_bytes(b"%PDF-1.4 " + text)
        argv = _argv(library, output_dir, "skip")
        run.main([*argv, "-m", "0", "-q"])
        capsys.readouterr()

        listed = listing(library, output_dir, capsys)

        assert sorted(listed) == [("Paper.pdf", "collision"), ("Paper.pdf", "copied")]
        assert (output_dir / "Paper.pdf").read_bytes() == b"%PDF-1.4 AAAA"


class TestAPdfOfAnotherSizeUnderItsName:
    """
    A PDF copied, then deleted from the library, and a different PDF of its
    name added: the claim pass saw the sizes differ, but placing trusted the
    name, so the newcomer was listed as copied and never copied, and the
    deleted book's file was not listed as an orphan.
    """

    @staticmethod
    def _replaced(tmp_path: Path, output_dir: Path, mode: str) -> list[str]:
        library = tmp_path / "lib"
        (library / "a").mkdir(parents=True)
        (library / "a" / "Paper.pdf").write_bytes(b"%PDF-1.4 the old paper")
        argv = [*_argv(library, output_dir, mode), "-m", "0"]
        run.main([*argv, "-q"])
        (library / "a" / "Paper.pdf").unlink()
        (library / "b").mkdir()
        (library / "b" / "Paper.pdf").write_bytes(b"%PDF-1.4 a longer, newer paper")
        return argv

    def test_skip_mode_reports_a_collision(self, tmp_path, output_dir, capsys):
        argv = self._replaced(tmp_path, output_dir, "skip")
        capsys.readouterr()

        run.main(argv)
        ran = capsys.readouterr()

        assert "Name collision, skipping: Paper.pdf" in ran.err
        assert "1 orphaned" in ran.out
        assert (output_dir / "Paper.pdf").read_bytes() == b"%PDF-1.4 the old paper"

    def test_suffix_mode_copies_it_beside_the_old_file(
        self, tmp_path, output_dir, capsys
    ):
        argv = self._replaced(tmp_path, output_dir, "suffix")
        library = Path(argv[1])
        capsys.readouterr()

        listed = listing(library, output_dir, capsys, *SUFFIX)
        run.main(argv)
        ran = capsys.readouterr()

        assert sorted(listed) == [("Paper.pdf", "copy"), ("Paper.pdf", "orphan")]
        assert "1 copied" in ran.out
        assert (output_dir / "Paper (2).pdf").read_bytes() == (
            b"%PDF-1.4 a longer, newer paper"
        )


class TestAZippedBookOfOneSizeReplacingAnother:
    """
    A zipped book copied, deleted from the library, and a different one of
    its name and size added: with no other book wanting the name, the size
    alone said the file on the shelf was the newcomer's, so it was listed as
    copied and never copied. A copy now keeps its source's modification
    time, and the pair is compared, which still downloads nothing.
    """

    @staticmethod
    def _replaced(tmp_path: Path, output_dir: Path, mode: str) -> list[str]:
        library = tmp_path / "lib"
        first = zipped_book(tmp_path, library / "a" / "Book.epub", "urn:uuid:A", "A")
        os.utime(first, ns=(EARLIER, EARLIER))
        argv = [*_argv(library, output_dir, mode), "-m", "0"]
        run.main([*argv, "-q"])
        first.unlink()
        added = zipped_book(tmp_path, library / "0" / "Book.epub", "urn:uuid:C", "C")
        assert added.stat().st_size == (output_dir / "Book.epub").stat().st_size
        return argv

    def test_the_copy_keeps_its_sources_modification_time(self, tmp_path, output_dir):
        self._replaced(tmp_path, output_dir, "skip")

        assert (output_dir / "Book.epub").stat().st_mtime_ns == EARLIER

    def test_skip_mode_reports_a_collision(self, tmp_path, output_dir, capsys):
        argv = self._replaced(tmp_path, output_dir, "skip")
        capsys.readouterr()

        run.main(argv)
        ran = capsys.readouterr()

        assert "Book.epub holds another book, urn:uuid:A" in ran.err
        assert "1 orphaned" in ran.out

    def test_suffix_mode_copies_it_beside_the_old_file(
        self, tmp_path, output_dir, capsys
    ):
        argv = self._replaced(tmp_path, output_dir, "suffix")
        capsys.readouterr()

        run.main(argv)
        ran = capsys.readouterr()

        assert "1 copied" in ran.out
        assert identifier_of(output_dir / "Book.epub") == "urn:uuid:A"
        assert identifier_of(output_dir / "Book (2).epub") == "urn:uuid:C"


class TestACopyMadeBeforeCopiesKeptTheirTime:
    """
    A copy used to take the time it was written. A shelf of those is newer
    than every source, and must not be copied again, or taken for another
    book's, on the first run that compares the times.
    """

    @pytest.mark.parametrize("mode", ["skip", "suffix"])
    def test_it_is_still_the_books_copy(self, tmp_path, output_dir, capsys, mode):
        library = tmp_path / "lib"
        zipped_book(tmp_path, library / "a" / "Book.epub", "urn:uuid:A", "A")
        (library / "b").mkdir()
        (library / "b" / "Paper.pdf").write_bytes(b"%PDF-1.4 paper")
        argv = [*_argv(library, output_dir, mode), "-m", "0"]
        run.main([*argv, "-q"])
        for copied in output_dir.glob("[BP]*"):
            os.utime(copied, ns=(LATER, LATER))
        capsys.readouterr()

        listed = listing(library, output_dir, capsys, "--on-collision", mode)
        run.main(argv)
        ran = capsys.readouterr()

        assert sorted(listed) == [("Book.epub", "copied"), ("Paper.pdf", "copied")]
        assert " copied" not in ran.out
        assert "collision" not in ran.out
        assert "orphan" not in ran.out
