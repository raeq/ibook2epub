"""
Tests for the books iCloud has evicted, which a run must not download by asking.

Opening a file iCloud has evicted downloads it. ``--skip-incomplete`` exists to
leave such books where they are, and ``--no-copy-through`` says the zipped
books and PDFs are not to be copied at all, so neither has any reason to open
one.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import json
import os
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.collect import package as package_reader
from epubconvert.run import run
from epubconvert.utils.display import printable
from tests.conftest import make_metadata_package, remove_tree
from tests.test_copy_claims import zipped_book
from tests.test_copy_keeping import LATER
from tests.test_copy_through import _evict

AUTHOR_TITLE = ["--name-by", "author-title"]


def _is(file: object, watched: Path) -> bool:
    """Whether what ZipFile was handed, a path or an open stream, is *watched*."""
    try:
        fileno = getattr(file, "fileno", None)
        if fileno is not None:
            return os.path.samestat(os.fstat(fileno()), watched.stat())
        return Path(str(file)) == watched
    except (OSError, ValueError):
        return False


def _opened(monkeypatch: pytest.MonkeyPatch, watched: Path) -> list[str]:
    """Record every time *watched* is opened as a zip, and still open it."""
    opened: list[str] = []
    original = ZipFile.__init__

    def counting(self, file, *args, **kwargs):
        if _is(file, watched):
            opened.append(str(file))
        original(self, file, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "__init__", counting)
    return opened


class TestNoCopyThroughOpensNoEvictedBook:
    """
    Under ``--no-copy-through --name-by author-title`` every zipped book was
    opened to be named, and so downloaded, though nothing was to be copied;
    only ``--skip-incomplete`` left them alone.
    """

    @pytest.mark.parametrize("listing", [[], ["--list"]])
    def test_an_evicted_zipped_book_is_not_opened(
        self, tmp_path, output_dir, monkeypatch, listing
    ):
        library = tmp_path / "lib"
        evicted = zipped_book(tmp_path, library / "Zipped.epub", "urn:uuid:Z")
        _evict(monkeypatch, evicted)
        opened = _opened(monkeypatch, evicted)
        cap = [] if listing else ["-m", "0"]

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-q", *cap, *listing]
            + ["--no-copy-through", *AUTHOR_TITLE]
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

    for module in ("holders", "placing", "planning"):
        monkeypatch.setattr(f"epubconvert.run.{module}.read_package_dir", counting)
    return read


class TestSkipIncompleteReadsNoEvictedSource:
    """
    A file under another spelling of a book's name may be the book's own,
    and reading the book's identifier settles it (holders.foreign). That
    read ignored ``--skip-incomplete``, so it downloaded the book the flag
    exists to leave where it is; so did the identifier read before a book
    is written over an archive, ahead of the inspection that says the book
    is not downloaded.
    """

    @pytest.mark.parametrize("listing", [[], ["--list"]])
    def test_a_copy_renamed_by_case(self, tmp_path, output_dir, monkeypatch, listing):
        library = tmp_path / "lib"
        first = zipped_book(tmp_path, library / "Book.epub", "urn:uuid:Z", "Book")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        first.unlink()
        evicted = zipped_book(
            tmp_path / "again", library / "book.epub", "urn:uuid:Z", "Book, revised"
        )
        _evict(monkeypatch, evicted)
        opened = _opened(monkeypatch, evicted)
        cap = [] if listing else ["-m", "0"]

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-q", *cap, *listing]
            + ["--skip-incomplete"]
        )

        assert code == 0
        assert not opened

    @pytest.mark.parametrize("listing", [[], ["--list"]])
    def test_a_package_renamed_by_case(
        self, tmp_path, output_dir, monkeypatch, listing
    ):
        library = tmp_path / "lib"
        make_metadata_package(
            library / "b", "dune.epub", title="Dune", identifier="urn:uuid:D"
        )
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        package = (library / "b" / "dune.epub").rename(library / "b" / "Dune.epub")
        _evict(monkeypatch, *(path for path in package.rglob("*") if path.is_file()))
        read = _package_reads(monkeypatch)
        cap = [] if listing else ["-m", "0"]

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-q", *cap, *listing]
            + ["--skip-incomplete"]
        )

        assert code == 0
        assert not read

    def test_a_package_about_to_be_written_over_an_archive(
        self, tmp_path, output_dir, monkeypatch, capsys
    ):
        library = tmp_path / "lib"
        make_metadata_package(
            library / "a", "Dune.epub", title="Dune", identifier="urn:uuid:A"
        )
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        remove_tree(library / "a")
        package = make_metadata_package(
            library / "b", "Dune.epub", title="Dune", identifier="urn:uuid:B"
        )
        _evict(monkeypatch, *(path for path in package.rglob("*") if path.is_file()))
        read = _package_reads(monkeypatch)
        capsys.readouterr()

        run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0"]
            + ["--force", "--skip-incomplete"]
        )

        assert not read
        assert (
            "Skipped, not downloaded from iCloud: Dune.epub" in capsys.readouterr().err
        )


class TestARefreshOpensNoEvictedBook:
    """
    ``-ae -ar`` copies nothing, but named the library's zipped books as a
    run that copies them does, and under a metadata policy naming one opens
    it: every book iCloud had evicted was downloaded to refresh the others'
    highlights. ``--skip-incomplete``, which would have stopped it, was
    refused beside ``-ar``.
    """

    @pytest.mark.parametrize(
        "extra", [[], ["-ad", "notes.json"], ["--skip-incomplete"], ["-w", "2"]]
    )
    def test_an_evicted_zipped_book_is_not_opened(
        self, tmp_path, output_dir, monkeypatch, extra
    ):
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations", lambda **_kwargs: []
        )
        library = tmp_path / "lib"
        make_metadata_package(
            library, "Alpha.epub", title="Alpha", creator="A", identifier="urn:uuid:A"
        )
        evicted = zipped_book(tmp_path, library / "Zipped.epub", "urn:uuid:Z")
        base = ["-s", str(library), "-o", str(output_dir), "-q", *AUTHOR_TITLE]
        assert run.main([*base, "-m", "0"]) == 0
        _evict(monkeypatch, evicted)
        opened = _opened(monkeypatch, evicted)
        monkeypatch.chdir(tmp_path)

        code = run.main([*base, "-ae", "-ar", *extra])

        assert code == 0
        assert not opened


class TestAnEvictedPackageNamedFromItsMetadata:
    """
    Under ``--name-by author-title`` a package is named from its package
    document, and under ``--skip-incomplete`` every evicted one was read to
    be named: downloaded, ahead of the inspection that then called it not
    downloaded. It is left unnamed, as an evicted zipped book is.
    """

    @pytest.mark.parametrize("listing", [[], ["--list", "--json"]])
    def test_it_is_not_read_and_is_not_downloaded(
        self, tmp_path, output_dir, monkeypatch, capsys, listing
    ):
        library = tmp_path / "lib"
        package = make_metadata_package(
            library / "a", "Dune.epub", title="Dune", identifier="urn:uuid:P"
        )
        make_metadata_package(
            library / "b", "Other.epub", title="Other", identifier="urn:uuid:O"
        )
        _evict(monkeypatch, *(path for path in package.rglob("*") if path.is_file()))
        read = _package_reads(monkeypatch)
        cap = [] if listing else ["-m", "0"]

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), *cap, *listing]
            + [*AUTHOR_TITLE, "--skip-incomplete"]
        )
        ran = capsys.readouterr()

        assert code == 0
        assert package not in read
        assert "1 book(s) not downloaded from iCloud could not be named" in ran.err
        if listing:
            rows = {row["name"]: row for row in json.loads(ran.out)}
            assert rows["Dune.epub"]["status"] == "incomplete"
            assert rows["Dune.epub"]["reason"] == "not downloaded from iCloud"
            assert rows["Other.epub"]["status"] == "pending"
        else:
            assert "1 not downloaded" in ran.out
            assert "Skipped, not downloaded from iCloud: Dune.epub" in ran.err
            assert [path.name for path in output_dir.glob("*.epub")] == ["Other.epub"]


class TestAnEvictedPackageBesideACopyOfItsName:
    """
    Under ``--skip-incomplete`` an evicted package is not opened, so it has
    no identifier to go by. A zipped book copied first keeps its file under
    the name both want, and the package moved on to a number of its own:
    with the package evicted, the name was trusted, and the package was
    reported exported from the copy's file while its own archive was listed
    as an orphan. The claim pass needs no identifier to know the copy's own
    bytes.
    """

    @pytest.mark.parametrize("policy", [[], ["-p", "strip"]])
    def test_it_is_still_exported_from_its_own_archive(
        self, tmp_path, output_dir, monkeypatch, capsys, policy
    ):
        library = tmp_path / "lib"
        zipped_book(tmp_path, library / "b" / "Dune A.epub", "urn:uuid:Z", "Dune")
        argv = ["-s", str(library), "-o", str(output_dir), "--on-collision"]
        argv += ["suffix", *policy]
        run.main([*argv, "-m", "0", "-q"])
        package = make_metadata_package(
            library / "a", "Dune A.epub", title="dune", identifier="urn:uuid:P"
        )
        run.main([*argv, "-m", "0", "-q"])
        _evict(monkeypatch, *(path for path in package.rglob("*") if path.is_file()))
        capsys.readouterr()

        run.main([*argv, "--list", "--json", "--skip-incomplete"])
        rows = json.loads(capsys.readouterr().out)
        run.main([*argv, "-m", "0", "--skip-incomplete"])
        ran = capsys.readouterr().out

        [mine] = [row for row in rows if row["source"] == str(package)]
        assert mine["status"] == "exported"
        assert Path(mine["target"]).name == "Dune A (2).epub"
        assert "orphan" not in {row["status"] for row in rows}
        assert "orphaned" not in ran


class TestAnEvictedZippedBookUnderADeletedBooksName:
    """
    A zipped book copied, then deleted from the library with its copy left on
    the shelf, and another zipped book of its name added and evicted. Left
    unopened, the newcomer declares nothing to tell the file from its own
    copy, and a copy whose target exists was taken as copied: it was listed
    as copied from the deleted book's file and never copied, and under
    ``--no-copy-through`` the deleted book's archive left the orphan list.
    """

    @staticmethod
    def _replaced(
        tmp_path: Path, output_dir: Path, monkeypatch, mode: str, name: str
    ) -> list[str]:
        library = tmp_path / "lib"
        old = zipped_book(tmp_path, library / "a" / name, "urn:uuid:OLD", "Old")
        argv = ["-s", str(library), "-o", str(output_dir), "--on-collision", mode]
        run.main([*argv, "-m", "0", "-q"])
        old.unlink()
        added = zipped_book(
            tmp_path, library / "b" / name, "urn:uuid:NEW", "A different title"
        )
        assert added.stat().st_size != (output_dir / name).stat().st_size
        _evict(monkeypatch, added)
        return argv

    @pytest.mark.parametrize(
        "case",
        [
            (mode, name)
            for mode in ("skip", "suffix")
            for name in ("Dune.epub", "Dune\x1b[2K\r.epub")
        ],
    )
    def test_skip_incomplete_says_it_cannot_tell(
        self, tmp_path, output_dir, monkeypatch, capsys, case
    ):
        mode, name = case
        argv = self._replaced(tmp_path, output_dir, monkeypatch, mode, name)
        before = (output_dir / name).read_bytes()
        capsys.readouterr()

        run.main([*argv, "--list", "--json", "--skip-incomplete"])
        rows = json.loads(capsys.readouterr().out)
        run.main([*argv, "--list", "--skip-incomplete"])
        table = capsys.readouterr().out
        run.main([*argv, "-m", "0", "--skip-incomplete"])
        ran = capsys.readouterr()

        reason = f"not downloaded from iCloud; cannot tell whether {name} is its copy"
        assert [(row["status"], row["reason"]) for row in rows] == [
            ("incomplete", reason)
        ]
        assert printable(reason) in table
        assert "\x1b" not in table + ran.err
        assert "1 not downloaded" in ran.out
        assert f"Skipped, {printable(reason)}: {printable(name)}" in ran.err
        assert (output_dir / name).read_bytes() == before

    @pytest.mark.parametrize("mode", ["skip", "suffix"])
    def test_no_copy_through_still_lists_the_deleted_books_archive(
        self, tmp_path, output_dir, monkeypatch, capsys, mode
    ):
        argv = self._replaced(tmp_path, output_dir, monkeypatch, mode, "Dune.epub")
        capsys.readouterr()

        run.main([*argv, "--list", "--json", "--no-copy-through"])
        rows = json.loads(capsys.readouterr().out)
        run.main([*argv, "-m", "0", "--no-copy-through"])
        ran = capsys.readouterr()

        [row] = rows
        assert (row["status"], Path(row["target"]).name) == ("orphan", "Dune.epub")
        assert "Dune.epub is not downloaded from iCloud" in row["reason"]
        assert "1 orphaned" in ran.out

    @pytest.mark.parametrize("mode", ["skip", "suffix"])
    def test_nor_by_its_size_alone(
        self, tmp_path, output_dir, monkeypatch, capsys, mode
    ):
        # The deleted book's copy is of the newcomer's size and newer, as a
        # copy made before copies kept their time is: only the identifiers
        # could say whose it is, and the newcomer's is not read.
        library = tmp_path / "lib"
        old = zipped_book(tmp_path, library / "a" / "Book.epub", "urn:uuid:A", "A")
        argv = ["-s", str(library), "-o", str(output_dir), "--on-collision", mode]
        run.main([*argv, "-m", "0", "-q"])
        os.utime(output_dir / "Book.epub", ns=(LATER, LATER))
        old.unlink()
        added = zipped_book(tmp_path, library / "b" / "Book.epub", "urn:uuid:C", "C")
        assert added.stat().st_size == (output_dir / "Book.epub").stat().st_size
        _evict(monkeypatch, added)
        capsys.readouterr()

        run.main([*argv, "--list", "--json", "--skip-incomplete"])
        rows = json.loads(capsys.readouterr().out)

        assert [(row["status"], row["reason"]) for row in rows] == [
            (
                "incomplete",
                "not downloaded from iCloud; cannot tell whether Book.epub is its copy",
            )
        ]
