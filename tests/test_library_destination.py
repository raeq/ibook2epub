"""
A library export the run cannot write is refused before the library is read.

``--library-export`` judged only that its file was not a directory, that the
directory above it was one, and that the name was free -- never whether that
directory could be written into or searched. A dry run into a read-only
directory exited 0 and the real run read the whole library, then exited 5;
beside ``-ao`` the highlights were written first and the catalogue refused
after them; and a directory the run may not search made ``Path.is_dir`` raise
on Python 3.10 and 3.11, a traceback in either run. Each is refused now, with
the write's own words, in the dry run and the real run alike, before
anything is read or written.

The permissions are faked, as root ignores them: these run anywhere.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring

import errno
import os
from pathlib import Path
from typing import Any

import pytest

from epubconvert.collect import annotations
from epubconvert.run import run
from epubconvert.utils import exits
from tests.test_annotations import highlight, library_row, make_databases
from tests.test_detached_destination import _deny, _read_only
from tests.test_read_only_shelf import EITHER_RUN


@pytest.fixture(name="flags")
def _flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A container with one highlight, and a library that must not be read."""
    (tmp_path / "lib").mkdir()
    make_databases(
        tmp_path / "container",
        rows=[highlight()],
        books=[library_row(title="Alpha", path=str(tmp_path / "Alpha.epub"))],
    )
    monkeypatch.setattr(
        "epubconvert.run.annotating.collect_annotations",
        lambda policy=None: annotations.collect(tmp_path / "container", policy),
    )
    monkeypatch.setattr(
        "epubconvert.export.detached.collect_library",
        lambda **_: pytest.fail("read the library before refusing"),
    )
    return ["-s", str(tmp_path / "lib"), "-o", str(tmp_path / "out")]


def _unsearchable(monkeypatch: pytest.MonkeyPatch, closed: Path) -> None:
    """
    Make *closed* a directory this run may not search, as mode 000 is.

    Every name inside it raises EACCES when looked at, and Path.is_dir raises
    as it does on 3.10 and 3.11, rather than answering False as later ones do.
    """
    _deny(monkeypatch, closed)
    measure = os.stat
    is_dir = Path.is_dir

    def refused(path: Any) -> bool:
        return Path(os.fspath(path)).parent == closed

    def stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if refused(path):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return measure(path, *args, **kwargs)

    def unsearchable_is_dir(path: Path, *args: Any, **kwargs: Any) -> bool:
        if refused(path):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return is_dir(path, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(Path, "is_dir", unsearchable_is_dir)


class TestInADirectoryItCannotWrite:
    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_one_it_may_not_write_into(
        self, flags, tmp_path, monkeypatch, capsys, mode
    ):
        closed = tmp_path / "closed"
        closed.mkdir()
        _deny(monkeypatch, closed)
        target = closed / "library.csv"

        code = run.main([*flags, "--library-export", str(target), *mode])

        assert code == exits.NO_OUTPUT
        assert f"Could not write {target}: Permission denied" in (
            capsys.readouterr().err
        )

    @pytest.mark.skipif(not hasattr(os, "statvfs"), reason="no statvfs here")
    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_one_on_a_read_only_volume(
        self, flags, tmp_path, monkeypatch, capsys, mode
    ):
        # Named as the write would name it, not as a permission the reader
        # could change with chmod.
        mounted = tmp_path / "mounted"
        mounted.mkdir()
        _read_only(monkeypatch, mounted)
        target = mounted / "library.csv"

        code = run.main([*flags, "--library-export", str(target), *mode])

        assert code == exits.NO_OUTPUT
        assert f"Could not write {target}: Read-only file system" in (
            capsys.readouterr().err
        )

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_one_it_may_not_search(self, flags, tmp_path, monkeypatch, capsys, mode):
        closed = tmp_path / "closed"
        closed.mkdir()
        _unsearchable(monkeypatch, closed)
        target = closed / "library.csv"

        code = run.main([*flags, "--library-export", str(target), *mode])

        assert code == exits.NO_OUTPUT
        assert f"Could not write {target}: Permission denied" in (
            capsys.readouterr().err
        )

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_one_under_a_file(self, flags, tmp_path, capsys, mode):
        above = tmp_path / "a-file"
        above.write_text("not a directory", encoding="utf-8")
        target = above / "library.csv"

        code = run.main([*flags, "--library-export", str(target), *mode])

        assert code == exits.NO_OUTPUT
        assert f"Could not write {target}: Not a directory" in (capsys.readouterr().err)

    @pytest.mark.parametrize("mode", EITHER_RUN)
    def test_one_that_is_not_there_keeps_its_words(self, flags, tmp_path, capsys, mode):
        target = tmp_path / "nope" / "library.csv"

        code = run.main([*flags, "--library-export", str(target), *mode])

        assert code == exits.NO_OUTPUT
        assert "the library export does not create directories" in (
            capsys.readouterr().err
        )


class TestBesideTheHighlights:
    """
    Both files are judged before either is written: the highlights were
    written, and the catalogue then refused, when its directory was one the
    run could not write into.
    """

    @pytest.mark.parametrize(
        "annotations_format",
        [
            pytest.param([], id="json"),
            pytest.param(["--annotations-format", "markdown"], id="vault"),
        ],
    )
    def test_neither_is_written(
        self, flags, tmp_path, monkeypatch, capsys, annotations_format
    ):
        closed = tmp_path / "closed"
        closed.mkdir()
        _deny(monkeypatch, closed)
        highlights = tmp_path / "highlights"

        code = run.main(
            [
                *flags,
                "--library-export",
                str(closed / "library.csv"),
                "-ao",
                str(highlights),
                *annotations_format,
            ]
        )

        err = capsys.readouterr().err
        assert code == exits.NO_OUTPUT
        assert not os.path.lexists(highlights)
        assert "highlights were not written either" in err
        # --force replaces a file; it does not open a directory.
        assert "--force" not in err

    def test_an_occupied_catalogue_still_points_at_force(self, flags, tmp_path, capsys):
        catalogue = tmp_path / "library.csv"
        catalogue.write_text("precious", encoding="utf-8")

        code = run.main(
            [
                *flags,
                "--library-export",
                str(catalogue),
                "-ao",
                str(tmp_path / "highlights.json"),
            ]
        )

        assert code == exits.NO_OUTPUT
        assert "pass --force to replace it" in capsys.readouterr().err
