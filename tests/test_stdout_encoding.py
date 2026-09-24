"""
Tests for what reaches standard output when it is not UTF-8.

``-ao -`` and ``--library-export -`` wrote through the text layer, so under a
locale or ``PYTHONIOENCODING`` that is not UTF-8 the first title in another
script raised UnicodeEncodeError out of the run. JSON is UTF-8 (RFC 8259), and
the CSV a tracker imports is too, so both go out as UTF-8 bytes whatever the
terminal was set to.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=protected-access,too-few-public-methods

import io
import json
import os
from pathlib import Path

import pytest

from epubconvert.collect import annotations, library
from epubconvert.export import detached
from epubconvert.run.run import main
from tests.test_annotations import highlight, library_row, make_databases

TITLE = "こころ"


def _latin1(monkeypatch: pytest.MonkeyPatch) -> io.BytesIO:
    raw = io.BytesIO()
    monkeypatch.setattr("sys.stdout", io.TextIOWrapper(raw, encoding="latin-1"))
    return raw


def _container(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    container = tmp_path / "container"
    make_databases(
        container,
        rows=[highlight(asset="A", uuid="UA", text=TITLE)],
        books=[library_row(asset="A", path="/x/Kokoro.epub", title=TITLE)],
    )
    monkeypatch.setattr(
        "epubconvert.run.annotating.collect_annotations",
        lambda policy=None: annotations.collect(container, policy),
    )
    monkeypatch.setattr(
        "epubconvert.export.detached.collect_library",
        lambda policy=None, identifiers=True: library.collect(
            container, policy, identifiers=identifiers
        ),
    )


class TestANonUtf8StandardOutput:
    def test_the_annotation_export_goes_out_as_utf8(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _container(monkeypatch, tmp_path)
        raw = _latin1(monkeypatch)

        code = main(["-ao", "-", "-q"])

        assert code == 0
        document = json.loads(raw.getvalue().decode("utf-8"))
        assert document["annotations"][0]["text"] == TITLE

    @pytest.mark.parametrize("shape", ["csv", "json"])
    def test_the_library_export_goes_out_as_utf8(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
    ):
        _container(monkeypatch, tmp_path)
        raw = _latin1(monkeypatch)

        code = main(["--library-export", "-", "--library-format", shape, "-q"])

        assert code == 0
        assert TITLE in raw.getvalue().decode("utf-8")


class TestAStandardOutputWithNoBytesLayer:
    def test_the_text_is_written_as_text(self, monkeypatch: pytest.MonkeyPatch):
        text = io.StringIO()
        monkeypatch.setattr("sys.stdout", text)

        detached._emit(f"{TITLE}\n")

        assert text.getvalue() == f"{TITLE}\n"


class _ClosedPipe(io.BytesIO):
    def write(self, _data: object) -> int:
        raise BrokenPipeError(32, "Broken pipe")


class _Closing(io.TextIOWrapper):
    def __init__(self, descriptor: int) -> None:
        super().__init__(_ClosedPipe(), encoding="utf-8")
        self.descriptor = descriptor

    def fileno(self) -> int:
        return self.descriptor


class TestAPipeClosedByTheReader:
    def test_it_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch):
        # "-ao - | head" is how the flag's own help says to use it.
        reading, writing = os.pipe()
        try:
            monkeypatch.setattr("sys.stdout", _Closing(writing))

            detached._emit(f"{TITLE}\n")
        finally:
            os.close(reading)
            os.close(writing)
