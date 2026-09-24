"""
A library or a shelf this run may not reach is an exit code, never a traceback.

``Path.is_dir`` treats only "absent" as no; a directory under one the run may
not search raises PermissionError out of it. On macOS that is the library or
shelf behind Full Disk Access (EPERM); anywhere, a parent with no execute bit
(EACCES). Every check that asked ``is_dir`` of ``-s`` or ``-o`` ended the run
in a traceback and exit 1.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

from __future__ import annotations

import errno
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from epubconvert.run import run
from epubconvert.utils import exits
from tests.conftest import make_package, needs_permissions

#: Every route that reads the shelf, as extra arguments after -s and -o.
SHELF_READERS = [
    pytest.param([], id="convert"),
    pytest.param(["-d"], id="dry-run"),
    pytest.param(["--list"], id="list"),
    pytest.param(["--verify"], id="verify"),
    pytest.param(["-ae", "-ar"], id="refresh"),
]


def refuse_below(
    monkeypatch: pytest.MonkeyPatch, directory: Path, error: int = errno.EACCES
) -> None:
    """
    Make everything under *directory* unreachable, as mode 0 makes it.

    Root searches a directory whatever its mode, so this stands in for one
    and the test runs under any user. The directory itself stays reachable.
    """
    inside = str(directory) + os.sep

    def guarded(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and os.fspath(path).startswith(
                inside
            ):
                raise OSError(error, os.strerror(error), os.fspath(path))
            return real(path, *args, **kwargs)

        return call

    for name in ("stat", "lstat", "scandir", "listdir"):
        monkeypatch.setattr(os, name, guarded(getattr(os, name)))


@pytest.fixture(name="unsearchable")
def _unsearchable(tmp_path: Path) -> Iterator[Path]:
    """A directory this run may not search, for a user it can refuse."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0)
    yield blocked
    blocked.chmod(0o755)


@pytest.fixture(autouse=True, name="no_highlights")
def _no_highlights(monkeypatch: pytest.MonkeyPatch) -> None:
    """Apple's container is not what these tests are about."""
    monkeypatch.setattr(
        "epubconvert.run.annotating.collect_annotations", lambda **_kwargs: []
    )


class TestAShelfItMayNotSearch:
    @pytest.mark.parametrize("mode", SHELF_READERS)
    def test_is_refused_by_every_route(self, tmp_path, monkeypatch, capsys, mode):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        refuse_below(monkeypatch, blocked)

        code = run.main(["-s", str(library), "-o", str(blocked / "shelf"), *mode])

        assert code == exits.NO_OUTPUT
        assert f"Cannot read output directory {blocked / 'shelf'}" in (
            capsys.readouterr().err
        )

    @needs_permissions
    @pytest.mark.parametrize("mode", SHELF_READERS)
    def test_for_real(self, tmp_path, unsearchable, capsys, mode):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(["-s", str(library), "-o", str(unsearchable / "s"), *mode])

        assert code == exits.NO_OUTPUT
        assert "Cannot read output directory" in capsys.readouterr().err

    @needs_permissions
    def test_through_a_symlink(self, tmp_path, unsearchable, capsys):
        # Following the link is what is refused, which only a real
        # directory's mode can do.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        link = tmp_path / "link"
        link.symlink_to(unsearchable / "shelf")

        code = run.main(["-s", str(library), "-o", str(link), "-d"])

        assert code == exits.NO_OUTPUT
        assert f"Cannot read output directory {link}" in capsys.readouterr().err

    def test_a_vault_is_still_written(self, tmp_path, monkeypatch):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        refuse_below(monkeypatch, blocked)

        code = run.main(
            [
                "-s",
                str(library),
                "-o",
                str(blocked / "shelf"),
                "-ao",
                str(tmp_path / "vault"),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

        assert code == exits.SUCCESS


class TestALibraryItMayNotSearch:
    def test_is_a_refusal(self, tmp_path, monkeypatch, output_dir, capsys):
        blocked = tmp_path / "blocked"
        make_package(blocked / "lib", "Book.epub")
        refuse_below(monkeypatch, blocked, errno.EPERM)

        code = run.main(["-s", str(blocked / "lib"), "-o", str(output_dir)])

        err = capsys.readouterr().err
        assert code == exits.NO_PERMISSION
        assert f"Cannot read source directory {blocked / 'lib'}" in err
        assert "Full Disk Access" in err

    @needs_permissions
    def test_for_real(self, unsearchable, output_dir):
        code = run.main(["-s", str(unsearchable / "lib"), "-o", str(output_dir)])

        assert code == exits.NO_PERMISSION

    def test_another_error_is_no_library(
        self, tmp_path, monkeypatch, output_dir, capsys
    ):
        blocked = tmp_path / "blocked"
        make_package(blocked / "lib", "Book.epub")
        refuse_below(monkeypatch, blocked, errno.EIO)

        code = run.main(["-s", str(blocked / "lib"), "-o", str(output_dir)])

        assert code == exits.NO_SOURCE
        assert "Cannot read source directory" in capsys.readouterr().err

    def test_verify_still_advises(self, tmp_path, monkeypatch, output_dir, capsys):
        library = tmp_path / "blocked" / "lib"
        make_package(library, "Book.epub")
        assert run.main(["-s", str(library), "-o", str(output_dir), "-q"]) == 0
        (output_dir / "Book.epub").write_bytes(b"CORRUPTED")
        refuse_below(monkeypatch, tmp_path / "blocked")
        capsys.readouterr()

        code = run.main(["-s", str(library), "-o", str(output_dir), "--verify"])

        assert code == exits.DAMAGED
        assert "Move each of these out of" in capsys.readouterr().out
