"""
A shelf the run cannot write into is refused before anything is written.

The pre-flight check judges the lock file when there is one, since that is
what the run opens, so a read-only shelf whose lock file could still be
opened passed it. The dry run then exited 0 and the real run failed every
book with EACCES and exited 1: the rehearsal said all was well for a run that
could not do its work. Once the plan says there is work, the shelf itself is
judged, in the dry run and the real run alike.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from epubconvert.collect import annotations
from epubconvert.run import convert, run
from epubconvert.utils import exits
from tests.conftest import make_metadata_package, needs_permissions
from tests.test_annotations import highlight, library_row, make_databases

#: A dry run, and the run it rehearses.
EITHER_RUN = [pytest.param(["-d"], id="dry-run"), pytest.param([], id="real")]


def _deny_writing(monkeypatch: pytest.MonkeyPatch, shelf: Path) -> None:
    """
    Make *shelf* read-only, as a read-only mount makes it for root too.

    Only ``os.access`` is told: root writes whatever the mode, so this stands
    in for one and the test runs under any user.
    """
    allowed = os.access

    def access(path: Any, mode: int, **kwargs: Any) -> bool:
        if mode & os.W_OK and Path(os.fspath(path)) == shelf:
            return False
        return allowed(path, mode, **kwargs)

    monkeypatch.setattr(os, "access", access)


@pytest.fixture(name="shelved")
def _shelved(tmp_path: Path) -> tuple[Path, Path]:
    """A library with one book on the shelf, which holds its lock file."""
    library = tmp_path / "lib"
    make_metadata_package(library, "Alpha.epub", title="Alpha")
    shelf = tmp_path / "shelf"
    assert run.main(["-s", str(library), "-o", str(shelf), "-m", "0", "-q"]) == 0
    assert (shelf / convert.LOCK_NAME).exists()
    return library, shelf


@pytest.fixture(name="read_only")
def _read_only(shelved: tuple[Path, Path]) -> Iterator[Path]:
    """The shelf, mode 555, with its lock file still writable."""
    shelved[1].chmod(0o555)
    yield shelved[1]
    shelved[1].chmod(0o755)


class TestWorkForAShelfItCannotWrite:
    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_a_book_to_convert(self, shelved, monkeypatch, capsys, mode):
        library, shelf = shelved
        make_metadata_package(library, "Beta.epub", title="Beta")
        _deny_writing(monkeypatch, shelf)

        code = run.main(["-s", str(library), "-o", str(shelf), "-m", "0", *mode])

        assert code == exits.NO_OUTPUT
        assert f"Cannot write into output directory {shelf}" in (
            capsys.readouterr().err
        )
        assert not (shelf / "Beta.epub").exists()

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_a_file_to_copy(self, shelved, monkeypatch, capsys, mode):
        library, shelf = shelved
        (library / "Paper.pdf").write_bytes(b"%PDF-1.4\n")
        _deny_writing(monkeypatch, shelf)

        code = run.main(["-s", str(library), "-o", str(shelf), "-m", "0", *mode])

        assert code == exits.NO_OUTPUT
        assert "Cannot write into output directory" in capsys.readouterr().err
        assert not (shelf / "Paper.pdf").exists()

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_nothing_to_do_is_not_refused(self, shelved, monkeypatch, mode):
        library, shelf = shelved
        _deny_writing(monkeypatch, shelf)

        code = run.main(["-s", str(library), "-o", str(shelf), "-m", "0", *mode])

        assert code == exits.SUCCESS

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_highlights_to_refresh(self, shelved, tmp_path, monkeypatch, mode):
        library, shelf = shelved
        _highlight_alpha(tmp_path, monkeypatch)
        _deny_writing(monkeypatch, shelf)
        before = (shelf / "Alpha.epub").read_bytes()

        code = run.main(["-s", str(library), "-o", str(shelf), "-ae", "-ar", *mode])

        assert code == exits.NO_OUTPUT
        assert (shelf / "Alpha.epub").read_bytes() == before

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_no_highlights_to_refresh(self, shelved, tmp_path, monkeypatch, mode):
        library, shelf = shelved
        make_databases(tmp_path / "container")
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "container", policy),
        )
        _deny_writing(monkeypatch, shelf)

        code = run.main(["-s", str(library), "-o", str(shelf), "-ae", "-ar", *mode])

        assert code == exits.SUCCESS


class TestForReal:
    """The same, with the permission bits a user other than root obeys."""

    @needs_permissions
    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_a_book_to_convert(self, shelved, read_only, capsys, mode):
        library, _shelf = shelved
        make_metadata_package(library, "Beta.epub", title="Beta")

        code = run.main(["-s", str(library), "-o", str(read_only), "-m", "0", *mode])

        assert code == exits.NO_OUTPUT
        assert "Cannot write into output directory" in capsys.readouterr().err

    @needs_permissions
    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_highlights_to_refresh(
        self, shelved, read_only, tmp_path, monkeypatch, mode
    ):
        library, _shelf = shelved
        _highlight_alpha(tmp_path, monkeypatch)

        code = run.main(["-s", str(library), "-o", str(read_only), "-ae", "-ar", *mode])

        assert code == exits.NO_OUTPUT


def _highlight_alpha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the book on the shelf a highlight Apple holds and it does not."""
    make_databases(
        tmp_path / "container",
        rows=[highlight()],
        books=[library_row(title="Alpha", path="/x/Alpha.epub")],
    )
    monkeypatch.setattr(
        "epubconvert.run.annotating.collect_annotations",
        lambda policy=None: annotations.collect(tmp_path / "container", policy),
    )
