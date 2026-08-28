"""
Tests for writing Markdown notes from a run.

The formatter is pinned in ``test_notes.py``; this is the wiring around it --
the flag guards, the filenames, and the decision the second run makes about
each file already in the vault.

The naming rule is the one worth stating: the library is named **once**, with
``.epub``, and the suffix is swapped on the result. Naming again with ``.md``
would clamp long titles against a budget two bytes larger, so a book near the
limit would get a ``.md`` stem its ``.epub`` never had.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from pathlib import Path

import pytest

from epubconvert import annotations, cli, notes
from epubconvert.run import main
from tests.conftest import make_metadata_package
from tests.test_annotations import highlight, library_row, make_databases


def _library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows=None, books=None
) -> Path:
    library = tmp_path / "lib"
    package = make_metadata_package(
        library,
        "Leviathan Wakes.epub",
        title="Leviathan Wakes",
        creator="James S. A. Corey",
    )
    make_databases(
        tmp_path / "container",
        rows=rows if rows is not None else [highlight()],
        books=books
        if books is not None
        else [library_row(path=str(package), title="Leviathan Wakes")],
    )
    monkeypatch.setattr(
        "epubconvert.run.collect_annotations",
        lambda policy=None: annotations.collect(tmp_path / "container", policy),
    )
    return library


class TestTheFlagGuards:
    def test_standard_output_has_no_per_book_meaning(self):
        with pytest.raises(SystemExit):
            cli.parse_args(["-ad", "-", "--annotations-format", "markdown"])

    def test_a_markdown_run_with_no_detached_destination_is_refused(self):
        # Otherwise the run converts normally and writes no notes, silently.
        with pytest.raises(SystemExit):
            cli.parse_args(["--annotations-format", "markdown"])

    def test_json_remains_the_default(self):
        assert cli.parse_args([]).annotations_format == "json"

    def test_a_path_that_is_a_file_is_refused(
        self, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = _library(tmp_path, monkeypatch)
        clash = tmp_path / "notes.json"
        clash.write_text("{}", encoding="utf-8")

        code = main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "-ad",
                str(clash),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

        assert code != 0
        assert clash.read_text(encoding="utf-8") == "{}"


class TestWritingNotes:
    def test_a_note_is_written_per_annotated_book(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = _library(tmp_path, monkeypatch)
        vault = tmp_path / "vault"

        code = main(
            [
                "-s",
                str(library),
                "-ao",
                str(vault),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

        assert code == 0
        assert [p.name for p in vault.glob("*.md")] == ["Leviathan Wakes.md"]

    def test_the_note_carries_the_highlight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = _library(tmp_path, monkeypatch)
        vault = tmp_path / "vault"
        main(
            [
                "-s",
                str(library),
                "-ao",
                str(vault),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

        note = (vault / "Leviathan Wakes.md").read_text(encoding="utf-8")

        assert "> Summary roadside justice" in note
        assert notes.is_ours(note)

    def test_the_stem_matches_the_epub_under_a_naming_policy(
        self, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = _library(tmp_path, monkeypatch)
        vault = tmp_path / "vault"
        main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--name-by",
                "author-title",
                "-ad",
                str(vault),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

        shelf = {p.stem for p in output_dir.glob("*.epub")}
        written = {p.stem for p in vault.glob("*.md")}

        assert written == shelf
        assert written == {"James S. A. Corey - Leviathan Wakes"}

    def test_a_book_with_no_highlights_gets_no_note(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = _library(tmp_path, monkeypatch)
        make_metadata_package(library, "Other.epub", title="Other")
        vault = tmp_path / "vault"
        main(
            [
                "-s",
                str(library),
                "-ao",
                str(vault),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

        assert [p.name for p in vault.glob("*.md")] == ["Leviathan Wakes.md"]

    def test_a_dry_run_writes_nothing(
        self, tmp_path: Path, output_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = _library(tmp_path, monkeypatch)
        vault = tmp_path / "vault"
        main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "-d",
                "-ad",
                str(vault),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

        assert not vault.exists() or list(vault.glob("*.md")) == []


class TestTheSecondRun:
    def _vault(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        library = _library(tmp_path, monkeypatch)
        vault = tmp_path / "vault"
        main(
            [
                "-s",
                str(library),
                "-ao",
                str(vault),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )
        return vault

    def _again(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows=None):
        library = _library(tmp_path, monkeypatch, rows=rows)
        return main(
            [
                "-s",
                str(library),
                "-ao",
                str(tmp_path / "vault"),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

    def test_an_unchanged_book_is_not_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        vault = self._vault(tmp_path, monkeypatch)
        note = vault / "Leviathan Wakes.md"
        before = note.stat().st_mtime_ns, note.read_bytes()

        self._again(tmp_path, monkeypatch)

        assert (note.stat().st_mtime_ns, note.read_bytes()) == before

    def test_the_readers_prose_survives_a_new_highlight(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        vault = self._vault(tmp_path, monkeypatch)
        note = vault / "Leviathan Wakes.md"
        note.write_text(
            note.read_text(encoding="utf-8") + "My own thinking, at length.\n",
            encoding="utf-8",
        )

        self._again(
            tmp_path,
            monkeypatch,
            rows=[highlight(), highlight(uuid="U2", text="a second one")],
        )

        after = note.read_text(encoding="utf-8")
        assert after.endswith("My own thinking, at length.\n")
        assert "a second one" in after

    def test_a_tagged_note_still_gets_its_highlights_updated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        vault = self._vault(tmp_path, monkeypatch)
        note = vault / "Leviathan Wakes.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace(
                "---\n", "---\ntags:\n  - fantasy\naliases: [LW]\n", 1
            ),
            encoding="utf-8",
        )

        self._again(
            tmp_path,
            monkeypatch,
            rows=[highlight(), highlight(uuid="U2", text="a second one")],
        )

        after = note.read_text(encoding="utf-8")
        assert "a second one" in after
        assert "- fantasy" in after
        assert not list(vault.glob("*.new.md"))

    def test_an_edited_body_is_left_alone_and_sidecarred(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        vault = self._vault(tmp_path, monkeypatch)
        note = vault / "Leviathan Wakes.md"
        edited = note.read_text(encoding="utf-8").replace(
            "> Summary roadside justice", "> Summary roadside justice\n\nmine"
        )
        note.write_text(edited, encoding="utf-8")

        self._again(
            tmp_path,
            monkeypatch,
            rows=[highlight(), highlight(uuid="U2", text="a second one")],
        )

        assert note.read_text(encoding="utf-8") == edited
        sidecar = vault / "Leviathan Wakes.new.md"
        assert sidecar.is_file()
        assert "a second one" in sidecar.read_text(encoding="utf-8")

    def test_a_file_this_tool_never_wrote_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = _library(tmp_path, monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        mine = vault / "Leviathan Wakes.md"
        mine.write_text(
            "# My own note\n\nnothing to do with the tool.\n", encoding="utf-8"
        )

        main(
            [
                "-s",
                str(library),
                "-ao",
                str(vault),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

        assert mine.read_text(encoding="utf-8") == (
            "# My own note\n\nnothing to do with the tool.\n"
        )

    def test_the_run_reports_what_it_left_alone(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ):
        vault = self._vault(tmp_path, monkeypatch)
        note = vault / "Leviathan Wakes.md"
        note.write_text(
            note.read_text(encoding="utf-8").replace(
                "> Summary roadside justice", "> edited"
            ),
            encoding="utf-8",
        )
        capsys.readouterr()

        library = _library(tmp_path, monkeypatch)
        main(
            ["-s", str(library), "-ao", str(vault), "--annotations-format", "markdown"]
        )

        reported = capsys.readouterr().err
        assert "left alone" in reported
        assert ".new.md" in reported


class TestWhenTheVaultCannotBeWritten:
    def test_a_directory_that_cannot_be_created_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = _library(tmp_path, monkeypatch)
        blocked = tmp_path / "locked"
        blocked.mkdir(mode=0o500)

        code = main(
            [
                "-s",
                str(library),
                "-ao",
                str(blocked / "vault"),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

        blocked.chmod(0o700)
        assert code != 0

    def test_a_note_that_cannot_be_read_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        library = _library(tmp_path, monkeypatch)
        vault = tmp_path / "vault"
        vault.mkdir()
        unreadable = vault / "Leviathan Wakes.md"
        unreadable.write_text("# mine\n", encoding="utf-8")
        unreadable.chmod(0o000)

        code = main(
            [
                "-s",
                str(library),
                "-ao",
                str(vault),
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

        unreadable.chmod(0o600)
        assert code == 0
        assert unreadable.read_text(encoding="utf-8") == "# mine\n"

    def test_a_book_that_lost_a_collision_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A book with no filename has nowhere to be written.
        library = _library(tmp_path, monkeypatch)
        vault = tmp_path / "vault"
        main(
            [
                "-s",
                str(library),
                "-ao",
                str(vault),
                "--on-collision",
                "skip",
                "--annotations-format",
                "markdown",
                "-q",
            ]
        )

        assert list(vault.glob("*.md"))
