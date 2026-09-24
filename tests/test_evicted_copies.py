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

from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.run import run
from tests.test_copy_claims import zipped_book
from tests.test_copy_through import _evict

AUTHOR_TITLE = ["--name-by", "author-title"]


def _opened(monkeypatch: pytest.MonkeyPatch, watched: Path) -> list[str]:
    """Record every time *watched* is opened as a zip, and still open it."""
    opened: list[str] = []
    original = ZipFile.__init__

    def counting(self, file, *args, **kwargs):
        if Path(str(file)) == watched:
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
