"""Tests for planning: collision handling, refresh, and the listing modes."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import json
import os
from collections.abc import Sequence
from pathlib import Path

from epubconvert import convert, planning, run, source
from epubconvert.archive import collect_package_dirs
from epubconvert.naming import NamingPolicy, PassthroughNaming
from tests.conftest import make_package


def pending_count(
    packages: Sequence[Path],
    output_dir: Path,
    policy: NamingPolicy,
    options: planning.PlanOptions | None = None,
) -> int:
    """Count books still needing export, via the same path a run takes."""
    return convert.count_pending_decisions(
        planning.plan_exports(packages, output_dir, policy, options)
    )


class TestCollisionSuffix:
    def test_skip_mode_exports_only_the_first(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_suffix_mode_keeps_both(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")

        run.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--on-collision",
                "suffix",
                "-q",
            ]
        )

        names = sorted(p.name for p in output_dir.glob("*.epub"))
        assert names == ["Same (2).epub", "Same.epub"]

    def test_suffix_assignment_is_stable_across_runs(self, tmp_path, output_dir):
        # Assignment walks packages in sorted order, so a shuffled run still
        # gives the same book the same name.
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")
        argv = [
            "-s",
            str(library),
            "-o",
            str(output_dir),
            "-m",
            "0",
            "--on-collision",
            "suffix",
            "-q",
        ]
        run.main(argv)
        first = {p.name: p.stat().st_mtime_ns for p in output_dir.glob("*.epub")}

        run.main(argv)

        second = {p.name: p.stat().st_mtime_ns for p in output_dir.glob("*.epub")}
        assert first == second  # nothing re-exported, nothing renamed

    def test_rerun_skips_both_suffixed_books(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")
        argv = [
            "-s",
            str(library),
            "-o",
            str(output_dir),
            "-m",
            "0",
            "--on-collision",
            "suffix",
            "-q",
        ]
        run.main(argv)
        capsys.readouterr()

        run.main(argv)

        assert "Exported 0" in capsys.readouterr().out


class TestRefresh:
    def test_a_newer_source_is_re_exported(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        run.main(argv)
        exported = output_dir / "Book.epub"
        before = exported.stat().st_mtime_ns

        # Make the source look newer than the export.
        future = exported.stat().st_mtime + 100
        os.utime(library / "Book.epub", (future, future))
        run.main([*argv, "--refresh"])

        assert exported.stat().st_mtime_ns != before

    def test_an_unchanged_source_is_left_alone(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        run.main(argv)
        before = (output_dir / "Book.epub").stat().st_mtime_ns

        run.main([*argv, "--refresh"])

        assert (output_dir / "Book.epub").stat().st_mtime_ns == before


class TestListing:
    def test_table_reports_each_status(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Pending.epub")
        locked = make_package(library, "Locked.epub")
        (locked / "META-INF").mkdir(parents=True, exist_ok=True)
        (locked / "META-INF" / "sinf.xml").write_text("<sinf/>", encoding="utf-8")

        code = run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        out = capsys.readouterr().out
        assert code == 0
        assert "pending" in out
        assert "drm" in out
        assert "Locked.epub" in out

    def test_json_is_machine_readable(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        run.main(
            ["-s", str(library), "-o", str(output_dir), "--list", "--json", "-q"]
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["name"] == "Book.epub"
        assert payload[0]["status"] == "pending"
        assert payload[0]["target"].endswith("Book.epub")

    def test_already_exported_books_are_marked(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        capsys.readouterr()

        run.main(
            ["-s", str(library), "-o", str(output_dir), "--list", "--json", "-q"]
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["status"] == "exported"

    def test_listing_converts_nothing(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        run.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        assert list(output_dir.glob("*.epub")) == []


class TestCountPending:
    def test_unexported_books_are_counted(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        packages = collect_package_dirs(library)

        assert pending_count(packages, output_dir, PassthroughNaming()) == 1

    def test_books_that_can_never_be_exported_are_excluded(self, tmp_path, output_dir):
        # A remaining count that can never reach zero is not progress. A
        # DRM-protected book is reported on its own line instead.
        library = tmp_path / "lib"
        locked = make_package(library, "Locked.epub")
        (locked / "META-INF").mkdir(parents=True, exist_ok=True)
        (locked / "META-INF" / "sinf.xml").write_text("<sinf/>", encoding="utf-8")
        packages = collect_package_dirs(library)

        assert pending_count(packages, output_dir, PassthroughNaming()) == 0

    def test_suffixed_collisions_are_counted_separately(self, tmp_path, output_dir):
        # Both books need writing, under two different names. Comparing bare
        # filenames would see one.
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")
        packages = collect_package_dirs(library)

        count = pending_count(
            packages,
            output_dir,
            PassthroughNaming(),
            planning.PlanOptions(on_collision="suffix"),
        )

        assert count == 2

    def test_exported_books_are_not_counted(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        packages = collect_package_dirs(library)

        assert pending_count(packages, output_dir, PassthroughNaming()) == 0

    def test_undownloaded_books_are_not_counted(
        self, tmp_path, output_dir, monkeypatch
    ):
        # Regression: the count forced check_incomplete off to save the stub
        # walk, so an undownloaded book planned as pending here and as
        # incomplete in the run that followed. --skip-incomplete then reported
        # books remaining that it would never export, on that run and on every
        # rerun after it.
        library = tmp_path / "lib"
        make_package(library, "Stub.epub")
        monkeypatch.setattr(source, "has_dataless_files", lambda _package: True)
        packages = collect_package_dirs(library)
        options = planning.PlanOptions(check_incomplete=True)

        decisions = planning.plan_exports(
            packages, output_dir, PassthroughNaming(), options
        )

        assert [d.status for d in decisions] == [planning.INCOMPLETE]
        assert (
            pending_count(packages, output_dir, PassthroughNaming(), options)
            == 0
        )
