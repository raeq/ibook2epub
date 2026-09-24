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
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from epubconvert.run import run
from epubconvert.utils import exits
from tests.conftest import make_package, needs_permissions
from tests.test_copy_through import _evict

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

    def test_through_a_symlink_on_every_python(self, tmp_path, monkeypatch, capsys):
        # Path.is_dir on 3.14 answers False to a refusal rather than raising,
        # so a link into a directory the run may not search was called "a
        # symlink to no directory". The link itself stays readable here.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "blocked" / "shelf")
        through = (str(link), str(link) + os.sep)

        def refusing(real: Callable[..., Any]) -> Callable[..., Any]:
            def call(path, *args, **kwargs):
                if isinstance(path, (str, os.PathLike)) and os.fspath(path).startswith(
                    through
                ):
                    raise PermissionError(errno.EACCES, "Permission denied", path)
                return real(path, *args, **kwargs)

            return call

        for name in ("stat", "scandir", "listdir"):
            monkeypatch.setattr(os, name, refusing(getattr(os, name)))

        code = run.main(["-s", str(library), "-o", str(link), "-d"])

        assert code == exits.NO_OUTPUT
        assert f"Cannot read output directory {link}" in capsys.readouterr().err

    def test_a_vault_named_against_it(self, tmp_path, monkeypatch, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        blocked = tmp_path / "blocked"
        blocked.mkdir()
        refuse_below(monkeypatch, blocked)

        code = run.main(
            ["-s", str(library), "-o", str(blocked / "shelf"), *_vault(tmp_path)]
        )

        assert code == exits.NO_OUTPUT
        assert "Cannot read output directory" in capsys.readouterr().err


def _vault(tmp_path: Path) -> list[str]:
    """Write the highlights as a vault of notes, and nothing else."""
    return ["-ao", str(tmp_path / "vault"), "--annotations-format", "markdown"]


def _refuse_listing(monkeypatch: pytest.MonkeyPatch, shelf: Path) -> None:
    """Make *shelf* unlistable, as mode 300 makes it for anyone but root."""
    listable = os.scandir

    def unlistable(path: Any, *args: Any, **kwargs: Any) -> Any:
        if os.fspath(path) == str(shelf):
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return listable(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", unlistable)


class TestAVaultNamedAgainstAShelfItCannotRead:
    """
    A vault names each note after the file its book is on the shelf, so it
    reads -o, and the check that refuses a shelf it cannot list was skipped
    for -ao: the notes were named as if the shelf were empty, which a later
    run, reading it, names differently.
    """

    @staticmethod
    def _shelf(tmp_path: Path) -> tuple[Path, Path]:
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        shelf = tmp_path / "shelf"
        assert run.main(["-s", str(library), "-o", str(shelf), "-q"]) == 0
        return library, shelf

    def test_is_refused(self, tmp_path, monkeypatch, capsys):
        library, shelf = self._shelf(tmp_path)
        _refuse_listing(monkeypatch, shelf)

        code = run.main(["-s", str(library), "-o", str(shelf), *_vault(tmp_path)])

        assert code == exits.NO_OUTPUT
        assert f"Cannot read output directory {shelf}" in capsys.readouterr().err
        assert not (tmp_path / "vault").exists()

    @needs_permissions
    def test_for_real(self, tmp_path):
        library, shelf = self._shelf(tmp_path)
        shelf.chmod(0o300)
        try:
            code = run.main(["-s", str(library), "-o", str(shelf), *_vault(tmp_path)])
        finally:
            shelf.chmod(0o755)

        assert code == exits.NO_OUTPUT

    def test_a_shelf_not_made_yet_is_no_obstacle(self, tmp_path):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "none"), *_vault(tmp_path)]
        )

        assert code == exits.SUCCESS

    def test_a_file_of_highlights_does_not_read_it(self, tmp_path, monkeypatch):
        library, shelf = self._shelf(tmp_path)
        _refuse_listing(monkeypatch, shelf)

        code = run.main(
            ["-s", str(library), "-o", str(shelf), "-ao", str(tmp_path / "h.json")]
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


class TestAShelfItMayListButNotSearch:
    """
    Mode 600 or 400: its names can be read, but nothing under them. The
    check listed it and let it pass, and --list and --verify then died on
    the first stat, in a traceback.
    """

    @staticmethod
    def _shelf(tmp_path: Path) -> tuple[Path, Path]:
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        shelf = tmp_path / "shelf"
        assert run.main(["-s", str(library), "-o", str(shelf), "-q"]) == 0
        return library, shelf

    @pytest.mark.parametrize("mode", SHELF_READERS)
    def test_is_refused_by_every_route(self, tmp_path, monkeypatch, capsys, mode):
        library, shelf = self._shelf(tmp_path)
        refuse_below(monkeypatch, shelf)

        code = run.main(["-s", str(library), "-o", str(shelf), *mode])

        assert code == exits.NO_OUTPUT
        assert f"Cannot read output directory {shelf}" in capsys.readouterr().err

    @needs_permissions
    @pytest.mark.parametrize("permissions", [0o600, 0o400])
    @pytest.mark.parametrize("mode", SHELF_READERS)
    def test_for_real(self, tmp_path, capsys, permissions, mode):
        library, shelf = self._shelf(tmp_path)
        shelf.chmod(permissions)
        try:
            code = run.main(["-s", str(library), "-o", str(shelf), *mode])
        finally:
            shelf.chmod(0o755)

        assert code == exits.NO_OUTPUT
        assert "Cannot read output directory" in capsys.readouterr().err


def _dropping_in_the_main_thread(monkeypatch: pytest.MonkeyPatch, lost: Path) -> None:
    """
    Make ``Path.exists`` raise EIO for *lost*, on the main thread only.

    What 3.10 and 3.11 do when a share or a USB volume drops mid-run; later
    Pythons answer False. Raised here on every Python.
    """
    exists = Path.exists

    def dropping(path: Path, *args: Any, **kwargs: Any) -> bool:
        if path == lost and threading.current_thread() is threading.main_thread():
            raise OSError(errno.EIO, os.strerror(errno.EIO), str(path))
        return exists(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", dropping)


class TestACopyTargetItCannotLookAt:
    """
    The run's own look at a copy's name on the shelf let EIO out of main: a
    traceback, exit 1 and no summary. A target that cannot be looked at is
    taken as not there, as the copy worker takes it, and the copy says why.
    """

    @pytest.mark.parametrize(
        ("mode", "said"),
        [
            pytest.param(["-m", "0"], "1 copied", id="convert"),
            pytest.param(["-m", "0", "-d"], "1 to copy", id="dry-run"),
            pytest.param(["--list"], "Lost.pdf", id="list"),
        ],
    )
    def test_is_taken_as_not_there(self, tmp_path, monkeypatch, capsys, mode, said):
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Lost.pdf").write_bytes(b"%PDF-1.4\n")
        _dropping_in_the_main_thread(monkeypatch, output_dir / "Lost.pdf")

        code = run.main(["-s", str(library), "-o", str(output_dir), *mode])

        assert code == exits.SUCCESS
        assert said in capsys.readouterr().out
        assert (output_dir / "Lost.pdf").is_file() == (mode == ["-m", "0"])

    def test_of_a_file_not_downloaded(self, tmp_path, output_dir, monkeypatch, capsys):
        library = tmp_path / "lib"
        library.mkdir()
        (library / "Lost.pdf").write_bytes(b"%PDF-1.4\n")
        _evict(monkeypatch, library / "Lost.pdf")
        _dropping_in_the_main_thread(monkeypatch, output_dir / "Lost.pdf")

        code = run.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "--skip-incomplete"]
        )

        assert code == exits.SUCCESS
        assert "1 not downloaded" in capsys.readouterr().out
