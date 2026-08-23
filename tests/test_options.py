"""Tests for --version, --force, --match, --workers, the run lock and totals."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from pathlib import Path

import pytest

from epubconvert import __version__, cli, convert
from epubconvert.naming import PassthroughNaming
from tests.conftest import make_package


@pytest.fixture(name="small_library")
def small_library_fixture(tmp_path: Path) -> Path:
    """A source directory holding three recognisable books."""
    source = tmp_path / "lib"
    for title in ["The Hobbit.epub", "Dune.epub", "Hobbit Notes.epub"]:
        make_package(source, title)
    return source


class TestVersion:
    def test_version_flag_prints_the_package_version(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            cli.parse_args(["--version"])

        assert excinfo.value.code == 0
        assert __version__ in capsys.readouterr().out


class TestMatch:
    def test_bare_word_matches_anywhere(self, small_library):
        packages = convert.collect_package_dirs(small_library)

        matched = convert.filter_packages(packages, "hobbit")

        assert {p.name for p in matched} == {"The Hobbit.epub", "Hobbit Notes.epub"}

    def test_matching_is_case_insensitive(self, small_library):
        packages = convert.collect_package_dirs(small_library)

        assert len(convert.filter_packages(packages, "HOBBIT")) == 2

    def test_glob_is_anchored(self, small_library):
        packages = convert.collect_package_dirs(small_library)

        matched = convert.filter_packages(packages, "The*")

        assert {p.name for p in matched} == {"The Hobbit.epub"}

    def test_no_pattern_keeps_everything(self, small_library):
        packages = convert.collect_package_dirs(small_library)

        assert len(convert.filter_packages(packages, None)) == 3

    def test_no_match_yields_nothing(self, small_library):
        packages = convert.collect_package_dirs(small_library)

        assert convert.filter_packages(packages, "silmarillion") == []

    def test_end_to_end(self, small_library, output_dir):
        convert.main(
            [
                "-s",
                str(small_library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--match",
                "dune",
                "-q",
            ]
        )

        assert [p.name for p in output_dir.glob("*.epub")] == ["Dune.epub"]


class TestForce:
    def test_existing_output_is_re_exported(self, small_library, output_dir):
        argv = ["-s", str(small_library), "-o", str(output_dir), "-m", "0", "-q"]
        convert.main(argv)
        stamp = (output_dir / "Dune.epub").stat().st_mtime_ns

        convert.main([*argv, "--force"])

        assert (output_dir / "Dune.epub").stat().st_mtime_ns != stamp
        assert len(list(output_dir.glob("*.epub"))) == 3

    def test_without_force_nothing_is_rewritten(self, small_library, output_dir):
        argv = ["-s", str(small_library), "-o", str(output_dir), "-m", "0", "-q"]
        convert.main(argv)
        stamp = (output_dir / "Dune.epub").stat().st_mtime_ns

        convert.main(argv)

        assert (output_dir / "Dune.epub").stat().st_mtime_ns == stamp

    def test_short_flag(self, small_library):
        assert cli.parse_args(["-s", str(small_library), "-f"]).force is True


class TestWorkers:
    def test_defaults_to_none(self, small_library):
        assert cli.parse_args(["-s", str(small_library)]).workers is None

    def test_accepts_a_count(self, small_library):
        assert cli.parse_args(["-s", str(small_library), "-w", "16"]).workers == 16

    def test_rejects_zero_and_negatives(self, small_library):
        for bad in ["0", "-4"]:
            with pytest.raises(SystemExit):
                cli.parse_args(["-s", str(small_library), "-w", bad])

    def test_a_high_count_still_produces_correct_output(
        self, small_library, output_dir
    ):
        code = convert.main(
            [
                "-s",
                str(small_library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "-w",
                "32",
                "-q",
            ]
        )

        assert code == 0
        assert len(list(output_dir.glob("*.epub"))) == 3


class TestRemainingCount:
    def test_counts_books_not_yet_exported(self, small_library, output_dir):
        packages = convert.collect_package_dirs(small_library)

        assert convert.count_pending(packages, output_dir, PassthroughNaming()) == 3

    def test_drops_as_books_are_exported(self, small_library, output_dir):
        convert.main(
            [
                "-s",
                str(small_library),
                "-o",
                str(output_dir),
                "-m",
                "1",
                "--no-shuffle",
                "-q",
            ]
        )
        packages = convert.collect_package_dirs(small_library)

        assert convert.count_pending(packages, output_dir, PassthroughNaming()) == 2

    def test_summary_reports_what_is_left(self, small_library, output_dir, capsys):
        convert.main(
            [
                "-s",
                str(small_library),
                "-o",
                str(output_dir),
                "-m",
                "1",
                "--no-shuffle",
                "-q",
            ]
        )

        assert "2 remaining; rerun to continue." in capsys.readouterr().out

    def test_summary_omits_remaining_when_done(self, small_library, output_dir, capsys):
        convert.main(["-s", str(small_library), "-o", str(output_dir), "-m", "0", "-q"])

        # Match the phrase, not the bare word: pytest names its tmp directory
        # after the test, so the output path itself contains "remaining".
        assert "rerun to continue" not in capsys.readouterr().out


class TestOutputLock:
    def test_lock_is_released_after_a_run(self, small_library, output_dir):
        argv = ["-s", str(small_library), "-o", str(output_dir), "-m", "0", "-q"]
        convert.main(argv)

        # A second sequential run must not be blocked by the first.
        assert convert.main([*argv, "--force"]) == 0

    def test_a_held_lock_stops_a_concurrent_run(self, small_library, output_dir):
        pytest.importorskip("fcntl", reason="advisory locking needs fcntl")

        with convert.output_lock(output_dir):
            code = convert.main(
                ["-s", str(small_library), "-o", str(output_dir), "-m", "0", "-q"]
            )

        assert code == 3
        assert list(output_dir.glob("*.epub")) == []

    def test_lock_raises_for_a_second_holder(self, output_dir):
        pytest.importorskip("fcntl", reason="advisory locking needs fcntl")

        # The third context manager raises as it is entered, while the first
        # still holds the lock.
        with (
            convert.output_lock(output_dir),
            pytest.raises(convert.OutputLockedError),
            convert.output_lock(output_dir),
        ):
            pass

    def test_dry_run_takes_no_lock(self, small_library, output_dir):
        convert.main(
            ["-s", str(small_library), "-o", str(output_dir), "-m", "0", "-d", "-q"]
        )

        assert not (output_dir / convert.LOCK_NAME).exists()


class TestArchivePermissions:
    def test_exported_archives_are_world_readable(self, small_library, output_dir):
        # mkstemp creates 0600; exported books should not inherit that.
        convert.main(["-s", str(small_library), "-o", str(output_dir), "-m", "0", "-q"])

        for archive in output_dir.glob("*.epub"):
            assert archive.stat().st_mode & 0o044
