"""
A highlights file the run cannot write is refused before anything is done.

The destination of ``-ad FILE`` and ``-ao FILE`` was judged only as it was
written, after every book had been converted. A dry run never got that far
and exited 0, the real run exited 5, and ``-ad`` into a directory that is not
there converted the whole library first. It is judged up front now, in the
dry run and the real run alike, as a read-only shelf is.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods,protected-access

import json
import os
from pathlib import Path
from typing import Any

import pytest

from epubconvert.collect import annotations
from epubconvert.export import detached
from epubconvert.run import run
from epubconvert.utils import exits
from tests.conftest import make_metadata_package, needs_permissions
from tests.test_annotations import highlight, library_row, make_databases
from tests.test_read_only_shelf import EITHER_RUN

#: Each route that writes a highlights file: beside a conversion, beside a
#: refresh, and on its own.
ROUTES = [
    pytest.param(["-m", "0", "-ae", "-ad"], id="convert"),
    pytest.param(["-ae", "-ar", "-ad"], id="refresh"),
    pytest.param(["-ao"], id="only"),
]


@pytest.fixture(name="library")
def _library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A library of one book with a highlight, and a shelf it is on."""
    library = tmp_path / "lib"
    make_metadata_package(library, "Alpha.epub", title="Alpha")
    make_metadata_package(library, "Beta.epub", title="Beta")
    make_databases(
        tmp_path / "container",
        rows=[highlight()],
        books=[library_row(title="Alpha", path=str(library / "Alpha.epub"))],
    )
    monkeypatch.setattr(
        "epubconvert.run.annotating.collect_annotations",
        lambda policy=None: annotations.collect(tmp_path / "container", policy),
    )
    (tmp_path / "shelf").mkdir()
    return library


def _run(library: Path, route: list[str], destination: Path | str, mode: list[str]):
    shelf = library.parent / "shelf"
    return run.main(
        ["-s", str(library), "-o", str(shelf), *route, str(destination), *mode]
    )


class TestAFileItCannotWrite:
    @pytest.mark.parametrize("mode", EITHER_RUN)
    @pytest.mark.parametrize("route", ROUTES)
    def test_in_a_directory_that_is_not_there(
        self, library, tmp_path, capsys, route, mode
    ):
        target = tmp_path / "no-such-dir" / "highlights.json"

        code = _run(library, route, target, mode)

        err = capsys.readouterr().err
        assert code == exits.NO_OUTPUT
        # The file named, not the temporary written beside it.
        assert f"Could not write {target}: No such file or directory" in err
        assert ".part" not in err
        assert not list((tmp_path / "shelf").glob("*.epub"))

    @pytest.mark.parametrize("mode", EITHER_RUN)
    @pytest.mark.parametrize("route", ROUTES)
    def test_that_is_not_an_annotation_export(
        self, library, tmp_path, capsys, route, mode
    ):
        target = tmp_path / "notes.txt"
        target.write_text("my own notes, not an export\n", encoding="utf-8")

        code = _run(library, route, target, mode)

        err = capsys.readouterr().err
        assert code == exits.NO_OUTPUT
        assert "notes.txt is already there and could not be read" in err
        assert target.read_text(encoding="utf-8") == "my own notes, not an export\n"
        assert not list((tmp_path / "shelf").glob("*.epub"))

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_in_a_directory_it_may_not_write(
        self, library, tmp_path, monkeypatch, capsys, mode
    ):
        closed = tmp_path / "closed"
        closed.mkdir()
        allowed = os.access

        def access(path: Any, how: int, **kwargs: Any) -> bool:
            if how & os.W_OK and Path(os.fspath(path)) == closed:
                return False
            return allowed(path, how, **kwargs)

        monkeypatch.setattr(os, "access", access)

        code = _run(library, ["-ao"], closed / "h.json", mode)

        assert code == exits.NO_OUTPUT
        assert f"Could not write {closed / 'h.json'}: Permission denied" in (
            capsys.readouterr().err
        )

    @pytest.mark.parametrize("mode", EITHER_RUN)
    @pytest.mark.parametrize(
        ("shelf", "named"),
        [
            pytest.param("Books", "Books", id="the-shelf"),
            pytest.param("Books/", "Books/", id="the-shelf-spelt-as-a-directory"),
            pytest.param("Library/epub", "Library", id="a-directory-above-it"),
        ],
    )
    def test_that_the_run_makes_as_a_directory(
        self, library, capsys, shelf, named, mode
    ):
        # Only the file's parent was judged, so the shelf the run was about
        # to make passed: the dry run exited 0, and the real run converted
        # the library and then refused a file that was now a directory.
        here = f"{library.parent}{os.sep}"

        code = run.main(
            ["-s", str(library), "-o", here + shelf, "-m", "0", "-ae", "-ad"]
            + [here + named, *mode]
        )

        err = capsys.readouterr().err
        assert code == exits.NO_OUTPUT
        assert f"Could not write {Path(here + named)}: Is a directory" in err
        assert not Path(here + shelf).exists()

    @needs_permissions
    def test_for_real(self, library, tmp_path, capsys):
        closed = tmp_path / "closed"
        closed.mkdir()
        closed.chmod(0o555)
        try:
            code = _run(library, ["-m", "0", "-ae", "-ad"], closed / "h.json", [])
        finally:
            closed.chmod(0o755)

        assert code == exits.NO_OUTPUT
        assert "Permission denied" in capsys.readouterr().err
        assert not list((tmp_path / "shelf").glob("*.epub"))


class TestAFileItCanWrite:
    @pytest.mark.parametrize("mode", EITHER_RUN)
    @pytest.mark.parametrize("route", ROUTES)
    def test_a_new_file(self, library, tmp_path, route, mode):
        if "-ar" in route:
            assert run.main(["-s", str(library), "-o", str(tmp_path / "shelf")]) == 0
        target = tmp_path / "highlights.json"

        code = _run(library, route, target, mode)

        assert code == exits.SUCCESS
        assert target.exists() == (not mode)

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_one_of_ours_already_there(self, library, tmp_path, mode):
        target = tmp_path / "highlights.json"
        assert _run(library, ["-ao"], target, []) == exits.SUCCESS

        assert _run(library, ["-ao"], target, mode) == exits.SUCCESS

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_inside_the_shelf_the_run_makes(self, library, tmp_path, mode):
        shelf = tmp_path / "new" / "shelf"
        target = shelf / "highlights.json"

        code = run.main(
            ["-s", str(library), "-o", str(shelf), "-m", "0", "-ae", "-ad"]
            + [str(target), *mode]
        )

        assert code == exits.SUCCESS
        assert target.exists() == (not mode)

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_standard_output(self, library, capsys, mode):
        code = _run(library, ["-m", "0", "-ae", "-ad"], "-", mode)

        assert code == exits.SUCCESS
        if not mode:
            assert json.loads(capsys.readouterr().out)["annotations"]

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_a_vault_not_made_yet(self, library, tmp_path, mode):
        vault = tmp_path / "vault"

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "shelf"), "-ao", str(vault)]
            + ["--annotations-format", "markdown", *mode]
        )

        assert code == exits.SUCCESS
        assert vault.is_dir() == (not mode)


class TestTheWriteItself:
    def test_names_the_file_it_could_not_write(self, tmp_path, caplog):
        # The destination can still go between the check and the write.
        target = tmp_path / "gone" / "highlights.json"

        code = detached._write_detached([], str(target))

        assert code == exits.NO_OUTPUT
        assert f"Could not write {target}: No such file or directory" in caplog.text
        assert ".part" not in caplog.text
