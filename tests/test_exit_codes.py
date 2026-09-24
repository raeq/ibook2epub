"""
Tests for the exit codes, which are the tool's contract with a script.

The lock file and rerun safety invite scheduling this from cron or launchd, and
a scheduled run is read by its status, not its prose. Every distinct reason a
run cannot proceed gets its own code, because "retry in an hour", "install
something", "fix the path" and "a book is broken" are four different responses.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import errno
import os
import re
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.collect import annotations
from epubconvert.collect import library as library_module
from epubconvert.collect.coredata import ContainerPermissionError
from epubconvert.export import archive, naming
from epubconvert.run import annotating, convert, run
from epubconvert.utils import exits
from tests.conftest import make_package, needs_permissions
from tests.test_annotations import highlight, library_row, make_databases


class TestTheCodesAreDistinct:
    """A code that means two things tells a script nothing."""

    def test_every_code_has_exactly_one_meaning(self):
        named = {
            name: value
            for name, value in vars(exits).items()
            if name.isupper() and isinstance(value, int)
        }
        assert len(named) == len(set(named.values()))

    def test_every_code_is_documented(self):
        named = {
            value
            for name, value in vars(exits).items()
            if name.isupper() and isinstance(value, int)
        }
        assert named == set(exits.MEANINGS)


class TestEachFailureHasItsOwnCode:
    """Reproduced one by one; these all used to be 2."""

    def test_success(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert code == exits.SUCCESS

    def test_a_usage_error_keeps_the_conventional_code(self):
        with pytest.raises(SystemExit) as raised:
            run.main(["--no-such-flag"])

        assert raised.value.code == exits.USAGE

    def test_contradictory_flags_are_a_usage_error(self, tmp_path):
        source = tmp_path / "lib"
        source.mkdir()

        with pytest.raises(SystemExit) as raised:
            run.main(["-s", str(source), "-q", "-v"])

        assert raised.value.code == exits.USAGE

    def test_a_missing_source_has_its_own_code(self, tmp_path):
        code = run.main(
            ["-s", str(tmp_path / "absent"), "-o", str(tmp_path / "out"), "-q"]
        )

        assert code == exits.NO_SOURCE

    def test_a_missing_extra_has_its_own_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(naming, "disarm", None)
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "out"), "-p", "romanize", "-q"]
        )

        assert code == exits.MISSING_TOOL

    def test_a_missing_external_tool_has_its_own_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr("epubconvert.run.run.epubcheck_available", lambda: False)
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "out"), "--epubcheck", "-q"]
        )

        assert code == exits.MISSING_TOOL

    def test_a_verify_target_that_is_not_there_has_its_own_code(self, tmp_path):
        source = tmp_path / "lib"
        source.mkdir()

        code = run.main(
            ["-s", str(source), "-o", str(tmp_path / "absent"), "--verify", "-q"]
        )

        assert code == exits.NO_OUTPUT

    def test_damaged_archives_have_their_own_code(self, tmp_path, output_dir):
        source = tmp_path / "lib"
        source.mkdir()
        (output_dir / "Broken.epub").write_bytes(b"not a zip")

        code = run.main(["-s", str(source), "-o", str(output_dir), "--verify", "-q"])

        assert code == exits.DAMAGED

    def test_a_failed_conversion_has_its_own_code(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        (output_dir / "Book.epub").mkdir()

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert code == exits.FAILED

    def test_a_held_lock_has_its_own_code(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        with convert.output_lock(output_dir):
            code = run.main(
                ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
            )

        assert code == exits.LOCKED

    def test_an_unusable_output_directory_has_its_own_code(self, tmp_path):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")

        code = run.main(
            ["-s", str(library), "-o", str(blocked / "out"), "-m", "0", "-q"]
        )

        assert code == exits.NO_OUTPUT

    @pytest.mark.parametrize("mode", [[], ["-d"], ["--list"], ["--verify"]])
    def test_a_file_where_the_shelf_should_be_is_the_same_code_in_every_mode(
        self, tmp_path, mode
    ):
        # The real run failed at mkdir with 5, but a dry run and --list only
        # read, found nothing on a "shelf" that was a file, and exited 0: the
        # rehearsal said all was well for a run that could not start.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")

        code = run.main(["-s", str(library), "-o", str(blocked), "-q", *mode])

        assert code == exits.NO_OUTPUT

    def test_a_run_that_never_touches_the_shelf_is_not_refused_over_it(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations", lambda **_kwargs: []
        )
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory", encoding="utf-8")

        code = run.main(["-o", str(blocked), "-ao", str(tmp_path / "h.json"), "-q"])

        assert code == exits.SUCCESS

    @pytest.mark.parametrize("name", ["out", "already using"])
    def test_an_unopenable_lock_file_is_not_a_held_lock(self, tmp_path, name):
        # The code was chosen by looking for "already using" in the message,
        # and the message quotes the output path: a directory named for the
        # phrase turned "fix the path" into "retry in an hour".
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        output_dir = tmp_path / name
        (output_dir / convert.LOCK_NAME).mkdir(parents=True)

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert code == exits.NO_OUTPUT

    def test_a_refresh_under_a_held_lock_has_the_same_code(
        self, tmp_path, output_dir, monkeypatch
    ):
        # -ar takes the lock on its own route, and only the export's route
        # turned the refusal into a code: a refresh started while a scheduled
        # conversion ran died with a traceback and exit 1.
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations", lambda **_kwargs: []
        )
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        with convert.output_lock(output_dir):
            code = run.main(
                ["-s", str(library), "-o", str(output_dir), "-ae", "-ar", "-q"]
            )

        assert code == exits.LOCKED

    def test_a_refresh_with_an_unopenable_lock_file_has_the_same_code(
        self, tmp_path, output_dir, monkeypatch
    ):
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations", lambda **_kwargs: []
        )
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        (output_dir / convert.LOCK_NAME).mkdir()

        code = run.main(["-s", str(library), "-o", str(output_dir), "-ae", "-ar", "-q"])

        assert code == exits.NO_OUTPUT


class TestAReadOnlyShelfStopsTheRehearsalToo:
    """
    A dry run on a read-only output volume exited 0, and the real run could
    neither create the shelf nor lock it and exited 5: the rehearsal said all
    was well for a run that could not start.
    """

    @needs_permissions
    @pytest.mark.parametrize("below", ["", "books"])
    @pytest.mark.parametrize("mode", [[], ["-d"]])
    def test_an_unwritable_shelf_is_the_same_code_either_way(
        self, tmp_path, below, mode
    ):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        shelf = tmp_path / "shelf"
        shelf.mkdir()
        shelf.chmod(0o555)
        try:
            code = run.main(["-s", str(library), "-o", str(shelf / below), "-q", *mode])
        finally:
            shelf.chmod(0o755)

        assert code == exits.NO_OUTPUT

    @needs_permissions
    @pytest.mark.parametrize("mode", ["--list", "--verify"])
    def test_a_report_on_an_unwritable_shelf_still_reads_it(self, tmp_path, mode):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        shelf = tmp_path / "shelf"
        shelf.mkdir()
        shelf.chmod(0o555)
        try:
            code = run.main(["-s", str(library), "-o", str(shelf), "-q", mode])
        finally:
            shelf.chmod(0o755)

        assert code == exits.SUCCESS

    @pytest.mark.parametrize("below", ["", "books"])
    def test_a_read_only_volume_stops_a_dry_run(
        self, tmp_path, monkeypatch, capsys, below
    ):
        # A read-only mount refuses root as well, where chmod does not: this
        # stands in for one, so it runs under any user.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        shelf = tmp_path / "shelf"
        shelf.mkdir()
        allowed = os.access
        monkeypatch.setattr(
            os,
            "access",
            lambda path, mode, **kwargs: (
                Path(path) != shelf and allowed(path, mode, **kwargs)
            ),
        )

        code = run.main(["-s", str(library), "-o", str(shelf / below), "-d", "-q"])

        assert code == exits.NO_OUTPUT
        assert str(shelf) in capsys.readouterr().err


def _refuse_opening(monkeypatch, path: Path, error: int = errno.EACCES) -> None:
    """Make *path* refuse to open, as a read-only file does for anyone but root."""
    opener = os.open

    def refusing(file, flags, *args, **kwargs):
        if os.fspath(file) == str(path):
            raise OSError(error, os.strerror(error), str(file))
        return opener(file, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", refusing)


class TestTheRehearsalJudgesTheLockFile:
    """
    The rehearsal judged only the shelf directory, and the real run opens the
    lock file in it. A lock file that could not be opened (mode 444) let a dry
    run exit 0 where the real run exited 5; and a read-only shelf holding a
    writable lock file, with nothing to convert, refused every run with 5
    although the real lock would have been taken.
    """

    @staticmethod
    def _shelf(tmp_path: Path) -> tuple[list[str], Path]:
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        shelf = tmp_path / "shelf"
        base = ["-s", str(library), "-o", str(shelf), "-q"]
        assert run.main(base) == exits.SUCCESS
        return base, shelf

    @pytest.mark.parametrize("error", [errno.EACCES, errno.EROFS])
    @pytest.mark.parametrize("mode", [[], ["-d"]])
    def test_a_lock_file_that_cannot_be_opened_stops_both(
        self, tmp_path, monkeypatch, error, mode
    ):
        base, shelf = self._shelf(tmp_path)
        (shelf / "Book.epub").unlink()
        _refuse_opening(monkeypatch, shelf / convert.LOCK_NAME, error)

        assert run.main([*base, *mode]) == exits.NO_OUTPUT

    @pytest.mark.parametrize("mode", [[], ["-d"]])
    def test_a_lock_file_that_is_a_link_stops_both(self, tmp_path, mode):
        base, shelf = self._shelf(tmp_path)
        (shelf / convert.LOCK_NAME).unlink()
        (shelf / convert.LOCK_NAME).symlink_to(tmp_path / "elsewhere")

        assert run.main([*base, *mode]) == exits.NO_OUTPUT
        assert not (tmp_path / "elsewhere").exists()

    @pytest.mark.parametrize("mode", [[], ["-d"]])
    def test_a_read_only_shelf_with_a_writable_lock_file_is_not_refused(
        self, tmp_path, monkeypatch, mode
    ):
        base, shelf = self._shelf(tmp_path)
        allowed = os.access
        monkeypatch.setattr(
            os,
            "access",
            lambda path, mode, **kwargs: (
                Path(path) != shelf and allowed(path, mode, **kwargs)
            ),
        )

        assert run.main([*base, *mode]) == exits.SUCCESS

    @needs_permissions
    @pytest.mark.parametrize("mode", [[], ["-d"]])
    def test_a_read_only_lock_file_stops_both(self, tmp_path, mode):
        base, shelf = self._shelf(tmp_path)
        (shelf / "Book.epub").unlink()
        (shelf / convert.LOCK_NAME).chmod(0o444)

        assert run.main([*base, *mode]) == exits.NO_OUTPUT

    @needs_permissions
    @pytest.mark.parametrize("mode", [[], ["-d"], ["--list"]])
    def test_a_read_only_shelf_with_nothing_to_do(self, tmp_path, mode):
        base, shelf = self._shelf(tmp_path)
        shelf.chmod(0o555)
        try:
            code = run.main([*base, *mode])
        finally:
            shelf.chmod(0o755)

        assert code == exits.SUCCESS


#: Every route that reads the shelf, as extra arguments after -s and -o.
_SHELF_READERS = [
    pytest.param(["--list"], id="list"),
    pytest.param(["--verify"], id="verify"),
    pytest.param(["-d"], id="dry-run"),
    pytest.param([], id="convert"),
    pytest.param(["-ae", "-ar"], id="refresh"),
]


def _refuse_listing(monkeypatch, shelf: Path) -> None:
    """Make *shelf* unlistable, as mode 300 makes it for anyone but root."""
    listable = os.scandir

    def unlistable(path, *args, **kwargs):
        if os.fspath(path) == str(shelf):
            raise PermissionError(13, "Permission denied", str(path))
        return listable(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", unlistable)


class TestAnUnreadableShelfIsNotAnEmptyOne:
    """
    A shelf that could not be listed read as an empty one: --verify exited 0
    with "No archives found", --list showed every book pending, and a real run
    on a directory it could write but not read (mode 300) exported again every
    book already there.
    """

    @staticmethod
    def _shelf_with_a_book(tmp_path: Path) -> tuple[Path, Path]:
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        shelf = tmp_path / "shelf"
        assert run.main(["-s", str(library), "-o", str(shelf), "-q"]) == 0
        return library, shelf

    @pytest.mark.parametrize("mode", _SHELF_READERS)
    def test_every_route_that_reads_it_stops(self, tmp_path, monkeypatch, capsys, mode):
        # Root reads a directory whatever its mode; an unlistable one stands
        # in for it, so this runs under any user.
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations", lambda **_kwargs: []
        )
        library, shelf = self._shelf_with_a_book(tmp_path)
        before = (shelf / "Book.epub").stat().st_mtime_ns
        _refuse_listing(monkeypatch, shelf)

        code = run.main(["-s", str(library), "-o", str(shelf), "-q", *mode])

        out, err = capsys.readouterr()
        assert code == exits.NO_OUTPUT
        assert f"Cannot read output directory {shelf}" in err
        assert "No archives found" not in out
        assert (shelf / "Book.epub").stat().st_mtime_ns == before

    def test_the_directory_is_named_escaped(self, tmp_path, monkeypatch, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        shelf = tmp_path / "my\x1b[2Kshelf"
        shelf.mkdir()

        _refuse_listing(monkeypatch, shelf)

        code = run.main(["-s", str(library), "-o", str(shelf), "--list", "-q"])

        err = capsys.readouterr().err
        assert code == exits.NO_OUTPUT
        assert "Cannot read output directory" in err
        assert "\x1b" not in err

    @needs_permissions
    @pytest.mark.parametrize("mode", _SHELF_READERS)
    def test_a_shelf_it_may_write_but_not_read(self, tmp_path, monkeypatch, mode):
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations", lambda **_kwargs: []
        )
        library, shelf = self._shelf_with_a_book(tmp_path)
        shelf.chmod(0o300)
        try:
            code = run.main(["-s", str(library), "-o", str(shelf), "-q", *mode])
        finally:
            shelf.chmod(0o755)

        assert code == exits.NO_OUTPUT


@pytest.fixture(name="refused")
def _refused(tmp_path):
    """A Books container this run may not read; chmod stands in for TCC."""
    parent = tmp_path / "Containers"
    container = parent / "Documents"
    container.mkdir(parents=True)
    parent.chmod(0)
    yield container
    parent.chmod(0o700)


class TestARefusalHasItsOwnCode:
    """
    #19: a refused container and a missing library both exited 4, so a
    scheduled run could not tell "grant Full Disk Access" from "there is no
    library here". Every route that reads the container is checked, since each
    turns the failure into a code on its own.
    """

    @needs_permissions
    def test_the_annotation_export(self, refused, monkeypatch):
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(refused, policy),
        )

        assert run.main(["-ao", "-", "-q"]) == exits.NO_PERMISSION

    @needs_permissions
    def test_the_library_export(self, refused, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "epubconvert.export.detached.collect_library",
            lambda **_: library_module.collect(refused),
        )

        code = run.main(["-s", str(tmp_path), "--library-export", "-", "-q"])

        assert code == exits.NO_PERMISSION

    @needs_permissions
    def test_a_refresh_that_converts_nothing(self, refused, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(refused, policy),
        )
        source = tmp_path / "lib"
        source.mkdir()

        code = run.main(
            ["-s", str(source), "-o", str(tmp_path / "out"), "-ar", "-ae", "-q"]
        )

        assert code == exits.NO_PERMISSION

    def test_an_absent_container_keeps_its_code(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "epubconvert.run.annotating.collect_annotations",
            lambda policy=None: annotations.collect(tmp_path / "absent", policy),
        )

        assert run.main(["-ao", "-", "-q"]) == exits.NO_SOURCE

    def test_the_code_is_eight(self):
        # A number a script writes down, so it is pinned where it can be read.
        assert exits.NO_PERMISSION == 8


class TestTheDocumentedTableMatchesTheCode:
    """The README is the contract a script author reads."""

    def test_run_main_names_no_code_by_number(self):
        # Its docstring listed codes by number, and four of them had moved on:
        # 5 was given as 1 and as 2, 7 as 1, and 6 as 2 (#23). exits defines
        # every code once, so the docstring points there instead.
        _, marker, returns = (run.main.__doc__ or "").partition(":return:")

        assert marker
        assert "exits" in returns
        assert re.findall(r"\b\d+\b", returns) == []

    def test_every_code_appears_in_the_readme(self):
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )
        for code in exits.MEANINGS:
            assert f"| `{code}`" in readme, f"exit {code} is undocumented"

    def test_the_readme_table_says_what_meanings_says(self):
        # "Generated from MEANINGS, so the two cannot drift" was only true of
        # the numbers: five rows' text had drifted, one of them dropping
        # "malformed" from what 2 covers.
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )
        table = dict(re.findall(r"^\| `(\d+)` \| (.*) \|$", readme, re.MULTILINE))

        assert {int(code): text for code, text in table.items()} == exits.MEANINGS
        assert [int(code) for code in table] == list(exits.MEANINGS)

    def test_the_readme_gives_full_disk_access_its_own_code(self):
        # It said "Without it you get exit code 4" after #19 moved a refusal
        # to 8, sending a script to fix a path instead of grant a permission.
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )
        paragraphs = [p for p in readme.split("\n\n") if "Full Disk Access" in p]
        named = {
            int(code)
            for paragraph in paragraphs
            for code in re.findall(r"exit code `?(\d+)", paragraph.replace("\n", " "))
        }

        assert named == {exits.NO_PERMISSION}

    @pytest.mark.parametrize("flags", [["-ae"], ["-ad", "highlights.json"]])
    def test_the_readme_says_which_runs_a_refusal_does_not_stop(
        self, tmp_path, monkeypatch, capsys, flags
    ):
        # "Without it you get exit code 8" was said of every annotation flag,
        # but a run that converts books logs the refusal and goes on: the books
        # are the point. Only -ao and -ar, where the highlights are the whole
        # run, exit 8, so a script watching -ae for 8 never saw it.
        def refuse(**_kwargs):
            raise ContainerPermissionError("Operation not permitted")

        monkeypatch.setattr("epubconvert.run.annotating.collect_annotations", refuse)
        source = tmp_path / "lib"
        make_package(source, "Alpha.epub")
        out = tmp_path / "out"
        flags = [str(tmp_path / f) if f.endswith(".json") else f for f in flags]

        code = run.main(["-s", str(source), "-o", str(out), "-q", *flags])

        assert code == exits.SUCCESS
        assert (out / "Alpha.epub").is_file()
        logged = "Could not read annotations"
        assert logged in capsys.readouterr().err
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )
        paragraph = " ".join(
            next(p for p in readme.split("\n\n") if "needs Full Disk Access" in p)
            .replace("\n", " ")
            .split()
        )
        for route in ("`-ao`", "`-ar`", "`-ae`", "`-ad`", logged):
            assert route in paragraph

    def test_a_valid_archive_still_verifies_clean(self, output_dir, tmp_path):
        source = tmp_path / "lib"
        source.mkdir()
        with ZipFile(output_dir / "Fine.epub", "w") as opened:
            opened.writestr("mimetype", "application/epub+zip")
            opened.writestr(
                "META-INF/container.xml",
                '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                '<rootfiles><rootfile full-path="c.opf"/></rootfiles>'
                "</container>",
            )
            opened.writestr(
                "c.opf",
                '<package xmlns="http://www.idpf.org/2007/opf"><manifest>'
                '<item id="t" href="t.xhtml"/></manifest>'
                '<spine><itemref idref="t"/></spine></package>',
            )
            opened.writestr("t.xhtml", "<html/>")

        code = run.main(["-s", str(source), "-o", str(output_dir), "--verify", "-q"])

        assert code == exits.SUCCESS


@pytest.fixture(name="annotated")
def _annotated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A library of two books, each with a highlight in Apple's container."""
    library = tmp_path / "lib"
    rows, books = [], []
    for index, name in enumerate(("Old.epub", "Other.epub")):
        package = make_package(library, name)
        rows.append(highlight(uuid=f"U{index}", asset=f"A{index}"))
        books.append(library_row(asset=f"A{index}", path=str(package)))
    make_databases(tmp_path / "container", rows=rows, books=books)
    monkeypatch.setattr(
        annotating,
        "collect_annotations",
        lambda policy=None: annotations.collect(tmp_path / "container", policy),
    )
    return library


def _interrupt(*_args, **_kwargs):
    raise KeyboardInterrupt


class TestAnInterruptedRunLeavesTheHighlightsAlone:
    """
    Ctrl-C stops the run. The annotation work after the export went on
    regardless: it wrote a vault of notes after the reader had asked the run
    to stop, and warned that highlights "reached no file" for books that were
    simply never attempted.
    """

    def test_no_note_is_written_after_ctrl_c(
        self, annotated, tmp_path, output_dir, monkeypatch, capsys
    ):
        monkeypatch.setattr(run, "export_planned", _interrupt)
        vault = tmp_path / "vault"
        argv = ["-s", str(annotated), "-o", str(output_dir), "-m", "0"]

        code = run.main([*argv, "-ad", str(vault), "--annotations-format", "markdown"])

        assert code == exits.INTERRUPTED
        assert not vault.exists()
        assert "highlights were not written" in capsys.readouterr().err

    def test_books_never_attempted_are_not_called_stranded(
        self, annotated, output_dir, monkeypatch, capsys
    ):
        monkeypatch.setattr(run, "export_planned", _interrupt)

        run.main(["-s", str(annotated), "-o", str(output_dir), "-m", "0", "-ae"])

        assert "reached no file" not in capsys.readouterr().err


class TestAnInterruptAfterTheBooksStillReportsThem:
    """
    "An interrupted run reports what it finished." A Ctrl-C while the
    highlights were written after the books, or while the lock was taken and
    the shelf swept, escaped to main's last-resort handler: exit 130 with no
    summary, nothing on stdout under -q, no word that the highlights were not
    written, and no summary in the log file.
    """

    def test_ctrl_c_while_the_highlights_are_written(
        self, annotated, tmp_path, output_dir, monkeypatch, capsys
    ):
        monkeypatch.setattr(annotating, "write_export", _interrupt)
        log = tmp_path / "run.log"
        argv = ["-s", str(annotated), "-o", str(output_dir), "-m", "0", "-q"]

        code = run.main(
            [*argv, "-ad", str(tmp_path / "h.json"), "--log-file", str(log)]
        )

        captured = capsys.readouterr()
        assert code == exits.INTERRUPTED
        assert captured.out.startswith("Interrupted. Exported 2 epub file(s)")
        assert "highlights were not written" in captured.err
        assert "Exported 2 epub file(s)" in log.read_text(encoding="utf-8")

    @pytest.mark.parametrize("phase", ["output_lock", "sweep_partials"])
    def test_ctrl_c_while_the_shelf_is_locked_or_swept(
        self, tmp_path, output_dir, monkeypatch, capsys, phase
    ):
        make_package(tmp_path / "lib", "Book.epub")
        monkeypatch.setattr(run, phase, _interrupt)

        code = run.main(["-s", str(tmp_path / "lib"), "-o", str(output_dir), "-q"])

        assert code == exits.INTERRUPTED
        assert capsys.readouterr().out.startswith("Interrupted. Exported 0")


class TestAnInterruptedRefreshSaysWhatItDid:
    """
    A Ctrl-C during --annotations-refresh escaped to main's last resort:
    exit 130, but nothing said how many books already had their highlights
    rewritten. --list and --verify have no summary to finish; they end with
    130 and no traceback.
    """

    def test_the_books_already_refreshed_are_counted(
        self, annotated, output_dir, monkeypatch, capsys
    ):
        argv = ["-s", str(annotated), "-o", str(output_dir)]
        run.main([*argv, "-m", "0", "-q"])
        refreshed: list[Path] = []
        real = archive.replace_annotations

        def second_interrupts(target, *args, **kwargs):
            refreshed.append(target)
            if len(refreshed) == 2:
                raise KeyboardInterrupt
            return real(target, *args, **kwargs)

        monkeypatch.setattr(annotating, "replace_annotations", second_interrupts)
        capsys.readouterr()

        code = run.main([*argv, "-ae", "-ar", "-q"])

        err = capsys.readouterr().err
        assert code == exits.INTERRUPTED
        assert (
            "Refreshed annotations in 1 book(s); interrupted before the rest; "
            "converted nothing."
        ) in err

    @pytest.mark.parametrize(
        ("mode", "phase"),
        [("--list", "render_listing"), ("--verify", "verify_output")],
    )
    def test_a_report_ends_with_130(self, tmp_path, monkeypatch, capsys, mode, phase):
        make_package(tmp_path / "lib", "Book.epub")
        (tmp_path / "out").mkdir()
        monkeypatch.setattr(run, phase, _interrupt)

        code = run.main(
            ["-s", str(tmp_path / "lib"), "-o", str(tmp_path / "out"), mode]
        )

        assert code == exits.INTERRUPTED
        assert "Interrupted" in capsys.readouterr().err


class TestTheExitCodeAgreesWithTheSummary:
    """
    An annotation destination's error replaced the run's own code, so a run
    stopped with Ctrl-C, or one whose book failed, exited 5 under a summary
    that said "Interrupted" or "failed 1".
    """

    @pytest.fixture(name="unwritable")
    def _unwritable(self, tmp_path: Path) -> Path:
        # Not JSON, so the detached export will not merge into it.
        destination = tmp_path / "highlights.json"
        destination.write_text("not json", encoding="utf-8")
        return destination

    def test_the_destination_alone_gives_its_own_code(
        self, annotated, output_dir, unwritable
    ):
        argv = ["-s", str(annotated), "-o", str(output_dir), "-m", "0", "-q"]

        assert run.main([*argv, "-ad", str(unwritable)]) == exits.NO_OUTPUT

    def test_an_interrupt_outranks_the_destination(
        self, annotated, output_dir, unwritable, monkeypatch
    ):
        monkeypatch.setattr(run, "export_planned", _interrupt)
        argv = ["-s", str(annotated), "-o", str(output_dir), "-m", "0", "-q"]

        assert run.main([*argv, "-ad", str(unwritable)]) == exits.INTERRUPTED

    def test_a_failed_book_outranks_the_destination(
        self, annotated, output_dir, unwritable, monkeypatch, capsys
    ):
        def broken(*_args, **_kwargs):
            raise OSError("disk error")

        monkeypatch.setattr(convert, "zip_package", broken)
        argv = ["-s", str(annotated), "-o", str(output_dir), "-m", "0"]

        code = run.main([*argv, "-ad", str(unwritable)])

        assert code == exits.FAILED
        assert "failed 2" in capsys.readouterr().out.strip().splitlines()[-1]


class TestAnOutputPathUnderAFileIsRefusedEverywhere:
    """
    ``-o afile/books`` does not exist, so the check for a file where the shelf
    should be passed it: a dry run and --list read an empty "shelf" and exited
    0, and the real run failed at mkdir with 5.
    """

    @pytest.mark.parametrize("mode", [["-d"], ["--list"], []])
    def test_every_mode_exits_as_the_real_run_does(self, tmp_path, mode, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        blocker = tmp_path / "afile"
        blocker.write_text("x", encoding="utf-8")

        code = run.main(["-s", str(library), "-o", str(blocker / "books"), *mode])

        assert code == exits.NO_OUTPUT
        assert "afile" in capsys.readouterr().err

    def test_a_missing_directory_under_a_directory_is_still_made(
        self, tmp_path, output_dir
    ):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        shelf = output_dir / "new" / "shelf"

        code = run.main(["-s", str(library), "-o", str(shelf), "-q"])

        assert code == exits.SUCCESS
        assert (shelf / "Book.epub").is_file()
