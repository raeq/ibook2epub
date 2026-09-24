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
from tests.conftest import make_metadata_package, remove_tree
from tests.test_copy_claims import zipped_book
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
