"""
A report that cannot be written ends the run cleanly, never in a traceback.

``--list``, ``--verify`` and a conversion's summary are written to standard
output, and to standard error when ``-ad -`` has standard output for its
document. A reader closing the pipe early is saying they have seen enough;
anything else that stops the write is a report lost, and a script is told.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import io
import os
import sys
from collections.abc import Iterator
from typing import TextIO

import pytest

from epubconvert.run import run
from epubconvert.utils import exits
from tests.conftest import make_package


@pytest.fixture(name="closed_pipe")
def _closed_pipe() -> Iterator[TextIO]:
    """
    A pipe whose reader has already gone, as after ``head -c0``. Each test
    puts it in place itself: pytest restores its own capture between a
    fixture's setup and the test.
    """
    reader, writer = os.pipe()
    os.close(reader)
    with os.fdopen(writer, "w", encoding="utf-8") as stream:
        yield stream


class TestASummaryOnAClosedStandardError:
    """
    Under ``-ad -`` the summary goes to standard error, and
    ``... -ad - 2>&1 | head -c0`` ended in a BrokenPipeError traceback and
    exit 1 after every book was written.
    """

    def test_ends_quietly(self, tmp_path, monkeypatch, closed_pipe):
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations", lambda **_kwargs: []
        )
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        monkeypatch.setattr(sys, "stdout", io.StringIO())
        monkeypatch.setattr(sys, "stderr", closed_pipe)

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "out"), "-m", "0", "-q"]
            + ["-ae", "-ad", "-"]
        )
        closed_pipe.flush()

        assert code == exits.SUCCESS
        assert (tmp_path / "out" / "Book.epub").exists()
