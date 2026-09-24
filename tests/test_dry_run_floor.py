"""
A dry run judges the --min-free floor where the real run does.

A dry run on a volume below the floor said "would export" every book and
exited 0, while the real run measured the volume before its first write,
wrote nothing and exited 1: the rehearsal said all was well for a run that
could not do its work. The default floor is 128 MiB, so a nearly full SD
card or Kindle met this without anyone typing the flag. The volume is now
measured once, at the point the real run measures it before its first write,
in the dry run and the real run alike.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

from pathlib import Path

import pytest

from epubconvert.collect import annotations
from epubconvert.run import run
from epubconvert.utils import exits
from tests.conftest import make_metadata_package
from tests.test_annotations import highlight, library_row, make_databases
from tests.test_read_only_shelf import EITHER_RUN

#: Below any volume's free space, as the floor is compared.
FLOOR = ["--min-free", "10"]


@pytest.fixture(name="full")
def _full(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every volume has 1 MiB free."""
    monkeypatch.setattr("epubconvert.run.convert.free_megabytes", lambda _path: 1)


@pytest.fixture(name="library")
def _library(tmp_path: Path) -> Path:
    """A library of two books and a PDF."""
    library = tmp_path / "lib"
    make_metadata_package(library, "Alpha.epub", title="Alpha")
    make_metadata_package(library, "Beta.epub", title="Beta")
    (library / "Paper.pdf").write_bytes(b"%PDF-1.4\n")
    return library


class TestAConversion:
    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_below_the_floor_is_stopped(self, library, tmp_path, full, capsys, mode):
        del full
        shelf = tmp_path / "shelf"

        code = run.main(
            ["-s", str(library), "-o", str(shelf), "-m", "0", *FLOOR, *mode]
        )

        out, err = capsys.readouterr()
        assert code == exits.FAILED
        assert f"Aborted: not enough free space on {shelf}." in out
        assert "2 not attempted: rerun to continue." in out
        assert "would export 0 epub file(s)" in out or "Exported 0 epub" in out
        assert "copied" not in out and "to copy" not in out
        # Measured once: the copies and the books share the one answer.
        assert err.count("below the --min-free floor of 10 MiB") == 1
        assert not list(shelf.glob("*.epub"))
        assert not (shelf / "Paper.pdf").exists()

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_only_a_copy_to_make(self, tmp_path, full, capsys, mode):
        del full
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Paper.pdf").write_bytes(b"%PDF-1.4\n")
        shelf = tmp_path / "shelf"

        code = run.main(["-s", str(library), "-o", str(shelf), *FLOOR, *mode])

        assert code == exits.FAILED
        assert "Aborted: not enough free space" in capsys.readouterr().out
        assert not (shelf / "Paper.pdf").exists()

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_nothing_to_write_is_not_stopped(
        self, library, tmp_path, monkeypatch, capsys, mode
    ):
        shelf = tmp_path / "shelf"
        assert run.main(["-s", str(library), "-o", str(shelf), "-m", "0", "-q"]) == 0
        monkeypatch.setattr("epubconvert.run.convert.free_megabytes", lambda _path: 1)

        code = run.main(
            ["-s", str(library), "-o", str(shelf), "-m", "0", *FLOOR, *mode]
        )

        assert code == exits.SUCCESS
        assert "Aborted" not in capsys.readouterr().out

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_above_the_floor_goes_ahead(self, library, tmp_path, capsys, mode):
        shelf = tmp_path / "shelf"

        code = run.main(["-s", str(library), "-o", str(shelf), "-m", "0", *mode])

        out = capsys.readouterr().out
        assert code == exits.SUCCESS
        assert "would export 2" in out or "Exported 2" in out


class TestARefresh:
    """``-ae -ar`` stops at the floor as a conversion does, and exits 1."""

    @staticmethod
    def _shelved(
        library: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Path:
        shelf = tmp_path / "shelf"
        assert run.main(["-s", str(library), "-o", str(shelf), "-m", "0", "-q"]) == 0
        make_databases(
            tmp_path / "container",
            rows=[highlight()],
            books=[library_row(title="Alpha", path="/x/Alpha.epub")],
        )
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        return shelf

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_a_book_to_rewrite_below_the_floor(
        self, library, tmp_path, monkeypatch, capsys, mode
    ):
        shelf = self._shelved(library, tmp_path, monkeypatch)
        before = (shelf / "Alpha.epub").read_bytes()
        monkeypatch.setattr("epubconvert.run.convert.free_megabytes", lambda _path: 1)

        code = run.main(
            ["-s", str(library), "-o", str(shelf), "-ae", "-ar", *FLOOR, *mode]
        )

        err = capsys.readouterr().err
        assert code == exits.FAILED
        assert "stopped at the --min-free floor before the rest" in err
        assert (shelf / "Alpha.epub").read_bytes() == before

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_a_shelf_up_to_date_is_not_stopped(
        self, library, tmp_path, monkeypatch, mode
    ):
        shelf = self._shelved(library, tmp_path, monkeypatch)
        base = ["-s", str(library), "-o", str(shelf), "-ae", "-ar", "-q"]
        assert run.main(base) == exits.SUCCESS
        monkeypatch.setattr("epubconvert.run.convert.free_megabytes", lambda _path: 1)

        assert run.main([*base, *FLOOR, *mode]) == exits.SUCCESS

    def test_the_dry_run_says_what_it_would_refresh(
        self, library, tmp_path, monkeypatch, capsys
    ):
        shelf = self._shelved(library, tmp_path, monkeypatch)
        before = (shelf / "Alpha.epub").read_bytes()

        code = run.main(["-s", str(library), "-o", str(shelf), "-ae", "-ar", "-d"])

        assert code == exits.SUCCESS
        assert "Dry run: would refresh annotations in 1 book(s)" in (
            capsys.readouterr().err
        )
        assert (shelf / "Alpha.epub").read_bytes() == before
        assert not list(shelf.glob(".ibook2epub-*"))
