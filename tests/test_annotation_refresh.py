"""
Tests for ``-ar``, which rebuilds archives already on the shelf.

A refresh writes to the output volume exactly as a conversion does: each
archive is rebuilt beside the original and moved over it. So it answers to
the same ``--min-free`` floor, and a book it could not refresh is a failure
the exit code reports, as a book that could not convert is. It skipped the
floor and exited 0 whatever went wrong, so a scheduled refresh on a full SD
card rebuilt books onto it and reported success after ``ENOSPC``.

A dry refresh reads what the real one reads, so it predicts the real one's
exit code rather than promising success before looking.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import errno
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.collect import annotations
from epubconvert.collect.coredata import ContainerPermissionError
from epubconvert.run import annotating, convert, run
from epubconvert.utils import exits
from tests.conftest import make_package
from tests.test_annotations import highlight, library_row, make_databases


def _carries(archive: Path) -> bool:
    with ZipFile(archive) as opened:
        return annotations.EMBEDDED_PATH in opened.namelist()


@pytest.fixture(name="shelved")
def _shelved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """
    Two annotated books already converted without their highlights.

    :return: The arguments naming the library and the shelf.
    """
    library = tmp_path / "lib"
    rows, books = [], []
    for index, name in enumerate(("Old.epub", "Older.epub")):
        package = make_package(library, name)
        rows.append(highlight(uuid=f"U{index}", asset=f"A{index}"))
        books.append(library_row(asset=f"A{index}", path=str(package)))
    make_databases(tmp_path / "container", rows=rows, books=books)
    monkeypatch.setattr(
        annotating,
        "collect_annotations",
        lambda policy=None: annotations.collect(tmp_path / "container", policy),
    )
    argv = ["-s", str(library), "-o", str(tmp_path / "out")]
    assert run.main([*argv, "-m", "0", "-q"]) == exits.SUCCESS
    return argv


def _shelf(argv: list[str]) -> Path:
    return Path(argv[argv.index("-o") + 1])


class TestARefreshAnswersToTheFloor:
    def test_the_default_floor_stops_the_rebuild(self, shelved, monkeypatch, capsys):
        monkeypatch.setattr(convert, "free_megabytes", lambda _path: 1)

        code = run.main([*shelved, "-ae", "-ar"])

        assert code == exits.FAILED
        assert not any(_carries(book) for book in _shelf(shelved).glob("*.epub"))
        assert "--min-free" in capsys.readouterr().err

    def test_a_floor_given_on_the_command_line_is_honoured(self, shelved, monkeypatch):
        monkeypatch.setattr(convert, "free_megabytes", lambda _path: 50)

        code = run.main([*shelved, "-ae", "-ar", "--min-free", "100"])

        assert code == exits.FAILED
        assert not any(_carries(book) for book in _shelf(shelved).glob("*.epub"))

    def test_a_floor_of_zero_turns_the_check_off(self, shelved, monkeypatch):
        monkeypatch.setattr(convert, "free_megabytes", lambda _path: 1)

        code = run.main([*shelved, "-ae", "-ar", "--min-free", "0"])

        assert code == exits.SUCCESS
        assert all(_carries(book) for book in _shelf(shelved).glob("*.epub"))

    def test_a_refresh_with_nothing_to_rewrite_is_not_stopped(
        self, shelved, monkeypatch
    ):
        # The floor guards writes. A shelf already up to date writes nothing,
        # so a full volume is no reason to report a failure.
        assert run.main([*shelved, "-ae", "-ar", "-q"]) == exits.SUCCESS
        monkeypatch.setattr(convert, "free_megabytes", lambda _path: 1)

        assert run.main([*shelved, "-ae", "-ar", "-q"]) == exits.SUCCESS


class TestARefreshThatFailsSaysSo:
    def test_a_book_that_could_not_be_refreshed_fails_the_run(
        self, shelved, monkeypatch, capsys
    ):
        def full(*_args, **_kwargs):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(annotating, "replace_annotations", full)

        code = run.main([*shelved, "-ae", "-ar"])

        assert code == exits.FAILED
        assert "could not refresh 2" in capsys.readouterr().err

    def test_the_detached_file_is_still_written(self, shelved, tmp_path, monkeypatch):
        # A book on the shelf that could not be refreshed says nothing about
        # a file somewhere else. Now that such a book fails the refresh, it
        # must not also cost the reader the file they asked for.
        def damaged(*_args, **_kwargs):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(annotating, "replace_annotations", damaged)
        detached = tmp_path / "highlights.json"
        code = run.main([*shelved, "-ae", "-ar", "-ad", str(detached), "-q"])

        assert code == exits.FAILED
        assert detached.is_file()


class TestADryRefreshPredictsTheRealOne:
    def test_a_container_it_may_not_read_is_reported(
        self, tmp_path, output_dir, monkeypatch
    ):
        # The dry run returned before reading anything and exited 0, saying
        # the annotations "were read"; the real run read them and exited 8.
        make_package(tmp_path / "lib", "Old.epub")
        attempts = []

        def refused(**_kwargs):
            attempts.append(1)
            raise ContainerPermissionError("Operation not permitted")

        monkeypatch.setattr(annotating, "collect_annotations", refused)
        argv = ["-s", str(tmp_path / "lib"), "-o", str(output_dir), "-ae", "-ar"]

        dry = run.main([*argv, "-d"])
        real = run.main(argv)

        assert dry == real == exits.NO_PERMISSION
        assert len(attempts) == 2
