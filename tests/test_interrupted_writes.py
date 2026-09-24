"""
A Ctrl-C just after a temporary is made leaves no temporary behind.

``write_atomically`` and the ``-ar`` rebuild each made their temporary beside
the file and only then entered the block that removes it on any error. A
Ctrl-C landing in between -- as the temporary's descriptor was closed --
left a ``.part`` file on the shelf or beside the export.
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


def _partials(directory: Path) -> list[str]:
    return sorted(
        path.name for path in directory.iterdir() if path.name.endswith(".part")
    )


class TestAnInterruptJustAfterTheTemporaryIsMade:
    def test_writing_a_file(self, tmp_path, monkeypatch):
        _interrupt_after_mkstemp(monkeypatch)
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
