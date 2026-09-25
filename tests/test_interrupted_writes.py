"""
A Ctrl-C just after a temporary is made leaves no temporary behind.

``write_atomically`` and the ``-ar`` rebuild each made their temporary beside
the file and only then entered the block that removes it on any error. A
Ctrl-C landing in between -- as the temporary's descriptor was closed --
left a ``.part`` file on the shelf or beside the export. So did one landing
as ``mkstemp`` returned, before the temporary's name was bound: that file is
now named before it is made.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from epubconvert.export import archive
from tests.conftest import make_package


def _interrupt_after_mkstemp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send a Ctrl-C as the descriptor of the next temporary made is closed."""
    made: list[int] = []
    mkstemp, close = tempfile.mkstemp, os.close

    def making(*args: Any, **kwargs: Any) -> tuple[int, str]:
        handle, name = mkstemp(*args, **kwargs)
        made.append(handle)
        return handle, name

    def closing(handle: int) -> None:
        close(handle)
        if made and handle == made[0]:
            made.clear()
            raise KeyboardInterrupt

    monkeypatch.setattr("epubconvert.export.archive.tempfile.mkstemp", making)
    monkeypatch.setattr("epubconvert.export.archive.os.close", closing)


def _interrupt_as_partial_made(
    monkeypatch: pytest.MonkeyPatch, *, closed: bool
) -> None:
    """
    Send a Ctrl-C just after the next temporary is made, before its name is
    back in the caller's hands: as the call that made it returns, or as its
    descriptor is closed.
    """
    made: list[int] = []
    opening, close = os.open, os.close

    def open_(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        handle = opening(path, flags, *args, **kwargs)
        if Path(path).name.startswith(archive.PARTIAL_PREFIX):
            if not closed:
                close(handle)
                raise KeyboardInterrupt
            made.append(handle)
        return handle

    def closing(handle: int) -> None:
        close(handle)
        if made and handle == made[0]:
            made.clear()
            raise KeyboardInterrupt

    # os itself, which tempfile makes its temporaries through too.
    monkeypatch.setattr(os, "open", open_)
    monkeypatch.setattr(os, "close", closing)


def _partials(directory: Path) -> list[str]:
    return sorted(
        path.name for path in directory.iterdir() if path.name.endswith(".part")
    )


class TestAnInterruptJustAfterTheTemporaryIsMade:
    @pytest.mark.parametrize(
        "closed",
        [
            # As the call that made it returned, before its name was bound: a
            # vault was left holding a .ibook2epub-*.part note of nobody's.
            pytest.param(False, id="as-it-is-made"),
            pytest.param(True, id="as-its-descriptor-is-closed"),
        ],
    )
    def test_writing_a_file(self, tmp_path, monkeypatch, closed):
        _interrupt_as_partial_made(monkeypatch, closed=closed)
        target = tmp_path / "highlights.json"
        target.write_text("old", encoding="utf-8")

        with pytest.raises(KeyboardInterrupt):
            archive.write_atomically(target, "new")

        assert _partials(tmp_path) == []
        assert target.read_text(encoding="utf-8") == "old"

    def test_refreshing_a_book(self, tmp_path, monkeypatch):
        package = make_package(tmp_path / "lib", "Book.epub")
        shelf = tmp_path / "shelf"
        shelf.mkdir()
        book = shelf / "Book.epub"
        archive.zip_package(package, book)
        before = book.read_bytes()
        _interrupt_after_mkstemp(monkeypatch)

        with pytest.raises(KeyboardInterrupt):
            archive.replace_annotations(book, [{"id": "U1", "text": "x"}])

        assert _partials(shelf) == []
        assert book.read_bytes() == before


class TestATemporaryNameAlreadyTaken:
    def test_is_left_alone_and_another_is_made(self, tmp_path, monkeypatch):
        # Named before it is made, so a name already there must not become
        # the temporary a failure removes.
        draws = iter([bytes(8), bytes(8), b"\x01" * 8])
        monkeypatch.setattr(os, "urandom", lambda _size: next(draws))
        taken = tmp_path / f"{archive.PARTIAL_PREFIX}{bytes(8).hex()}.part"
        taken.write_text("someone else's", encoding="utf-8")
        target = tmp_path / "highlights.json"

        archive.write_atomically(target, "new")

        assert target.read_text(encoding="utf-8") == "new"
        assert taken.read_text(encoding="utf-8") == "someone else's"
        assert _partials(tmp_path) == [taken.name]
