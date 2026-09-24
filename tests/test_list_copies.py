"""
Tests for what ``--list`` says of the files a run copies through.

``-d`` said "2 to copy" while ``--list`` showed nothing about the copies at
all, though its help said everything that is not a package is counted; and
under ``--no-copy-through`` it counted a zipped book as "ignored (not books)".
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring

import json
from pathlib import Path

from epubconvert.run import run
from tests.conftest import make_metadata_package
from tests.test_copy_claims import zipped_book


def _library(tmp_path: Path, output_dir: Path) -> Path:
    """A package, a zipped book, and a PDF already copied to the shelf."""
    library = tmp_path / "lib"
    make_metadata_package(library, "Dune.epub", title="Dune", identifier="urn:d")
    zipped_book(tmp_path, library / "Beta.epub", "urn:uuid:B")
    (library / "Paper.pdf").write_bytes(b"%PDF-1.4 fake")
    (output_dir / "Paper.pdf").write_bytes(b"%PDF-1.4 fake")
    return library


class TestTheListingShowsTheCopies:
    def test_each_copy_is_a_row(self, tmp_path, output_dir, capsys):
        library = _library(tmp_path, output_dir)

        run.main(["-s", str(library), "-o", str(output_dir), "--list"])
        out = capsys.readouterr().out

        rows = [line.split() for line in out.splitlines() if line.strip()]
        assert ["copy", "Beta.epub"] in rows
        assert ["copied", "Paper.pdf"] in rows
        assert ["pending", "Dune.epub"] in rows
        assert "1 copied, 1 copy, 1 pending" in out
        assert "ignored" not in out

    def test_the_json_carries_them(self, tmp_path, output_dir, capsys):
        library = _library(tmp_path, output_dir)

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "--json"])
        rows = {row["name"]: row for row in json.loads(capsys.readouterr().out)}

        assert rows["Beta.epub"]["status"] == "copy"
        assert rows["Beta.epub"]["source"] == str(library / "Beta.epub")
        assert rows["Beta.epub"]["target"] == str(output_dir / "Beta.epub")
        assert rows["Paper.pdf"]["status"] == "copied"

    def test_it_agrees_with_a_dry_run(self, tmp_path, output_dir, capsys):
        library = _library(tmp_path, output_dir)

        run.main(["-s", str(library), "-o", str(output_dir), "-d"])

        assert "1 to copy" in capsys.readouterr().out

    def test_without_copy_through_they_are_counted_as_not_copied(
        self, tmp_path, output_dir, capsys
    ):
        library = _library(tmp_path, output_dir)

        run.main(
            ["-s", str(library), "-o", str(output_dir), "--list", "--no-copy-through"]
        )
        out = capsys.readouterr().out

        assert "copy " not in out
        assert "2 not copied (--no-copy-through)" in out
        assert "ignored" not in out
