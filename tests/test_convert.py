"""Tests for the package discovery, archiving and export logic."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from zipfile import ZIP_STORED, ZipFile

import pytest

from epubconvert import convert
from tests.conftest import EXPECTED_MEMBERS, make_package


def export(packages: Sequence[Path], output_dir: Path, **kwargs: Any) -> convert.Report:
    """Run the async exporter from a synchronous test."""
    return asyncio.run(convert.export_packages(packages, output_dir, **kwargs))


class TestIsExcluded:
    """The exclusion rules applied to package members."""

    @pytest.mark.parametrize(
        "name",
        ["mimetype", ".DS_Store", "iTunesMetadata.plist", "bookmarks.plist"],
    )
    def test_apple_bookkeeping_is_excluded(self, name):
        assert convert.is_excluded(name)

    @pytest.mark.parametrize(
        "name",
        ["content.opf", "chapter1.xhtml", "container.xml", "cover.jpg", "style.css"],
    )
    def test_content_is_kept(self, name):
        assert not convert.is_excluded(name)


class TestCollectPackageDirs:
    """Discovery of ``*.epub/`` package directories."""

    def test_finds_top_level_and_nested_packages(self, library):
        found = convert.collect_package_dirs(library)

        assert [path.name for path in found] == ["Book One.epub", "Book Two.epub"]
        assert found[1] == library / "Nested" / "Deep" / "Book Two.epub"

    def test_returns_full_paths_so_nested_packages_resolve(self, library):
        for path in convert.collect_package_dirs(library):
            assert path.is_dir()

    def test_does_not_descend_into_a_package(self, library):
        # A directory that merely lives inside a package must not be reported.
        inner = library / "Book One.epub" / "Inner.epub"
        inner.mkdir()

        found = convert.collect_package_dirs(library)

        assert inner not in found

    def test_ignores_plain_directories(self, library):
        found = convert.collect_package_dirs(library)

        assert all(path.name.endswith(".epub") for path in found)

    def test_missing_source_yields_nothing(self, tmp_path):
        assert convert.collect_package_dirs(tmp_path / "absent") == []


class TestZipPackage:
    """The archive written for a single package."""

    def test_mimetype_is_first_and_stored(self, library, output_dir):
        target = output_dir / "Book One.epub"

        convert.zip_package(library / "Book One.epub", target)

        with ZipFile(target) as archive:
            names = archive.namelist()
            assert names[0] == "mimetype"
            assert names.count("mimetype") == 1
            info = archive.getinfo("mimetype")
            assert info.compress_type == ZIP_STORED
            assert archive.read("mimetype") == b"application/epub+zip"

    def test_archive_is_valid_and_holds_only_content(self, library, output_dir):
        target = output_dir / "Book One.epub"

        file_count = convert.zip_package(library / "Book One.epub", target)

        with ZipFile(target) as archive:
            assert archive.testzip() is None
            members = set(archive.namelist()) - {"mimetype"}
            assert members == EXPECTED_MEMBERS
            assert b"Chapter one" in archive.read("OEBPS/text/chapter1.xhtml")
        assert file_count == len(EXPECTED_MEMBERS)

    def test_leaves_no_partial_file_behind_on_success(self, library, output_dir):
        convert.zip_package(library / "Book One.epub", output_dir / "Book One.epub")

        assert list(output_dir.glob("*.part")) == []

    def test_failure_leaves_neither_target_nor_partial(
        self, library, output_dir, monkeypatch
    ):
        target = output_dir / "Book One.epub"

        def boom(_name):
            raise OSError("disk fell over")

        monkeypatch.setattr(convert, "is_excluded", boom)

        with pytest.raises(OSError):
            convert.zip_package(library / "Book One.epub", target)

        # The critical property: an interrupted run must not leave a truncated
        # archive that the "already exported" check would skip forever.
        assert not target.exists()
        assert list(output_dir.glob("*.part")) == []


class TestExportPackages:
    """Batch export behaviour."""

    def test_exports_every_package(self, library, output_dir):
        packages = convert.collect_package_dirs(library)

        report = export(packages, output_dir)

        assert report.exported == 2
        assert report.failed == 0
        assert report.skipped == 0
        assert {path.name for path in output_dir.glob("*.epub")} == {
            "Book One.epub",
            "Book Two.epub",
        }

    def test_rerun_skips_completed_work(self, library, output_dir):
        packages = convert.collect_package_dirs(library)
        export(packages, output_dir)

        report = export(packages, output_dir)

        assert report.exported == 0
        assert report.skipped == 2

    def test_dry_run_writes_nothing_and_reports_plans(self, library, output_dir):
        packages = convert.collect_package_dirs(library)

        report = export(packages, output_dir, dry_run=True)

        assert report.planned == 2
        assert report.exported == 0
        assert report.files_written == 0
        assert list(output_dir.iterdir()) == []

    def test_duplicate_package_names_do_not_collide(self, tmp_path, output_dir):
        source = tmp_path / "dupes"
        first = make_package(source / "a", "Same.epub")
        second = make_package(source / "b", "Same.epub")

        report = export([first, second], output_dir)

        assert report.exported == 1
        assert report.skipped == 1

    def test_a_failing_package_is_reported_not_raised(
        self, library, output_dir, monkeypatch
    ):
        def boom(_name):
            raise OSError("disk fell over")

        monkeypatch.setattr(convert, "is_excluded", boom)
        packages = convert.collect_package_dirs(library)

        report = export(packages, output_dir)

        assert report.failed == 2
        assert report.exported == 0

    def test_empty_input_is_a_no_op(self, output_dir):
        report = export([], output_dir)

        assert report == convert.Report()


class TestSelectPackages:
    """Application of the export cap."""

    def test_zero_means_no_limit(self):
        packages = [Path(f"{i}.epub") for i in range(10)]

        assert len(convert.select_packages(packages, 0)) == 10

    def test_cap_is_applied(self):
        packages = [Path(f"{i}.epub") for i in range(10)]

        assert len(convert.select_packages(packages, 3)) == 3

    def test_no_shuffle_takes_them_in_order(self):
        packages = [Path(f"{i}.epub") for i in range(10)]

        selected = convert.select_packages(packages, 3, randomise=False)

        assert selected == packages[:3]

    def test_selection_does_not_mutate_the_input(self):
        packages = [Path(f"{i}.epub") for i in range(10)]
        original = list(packages)

        convert.select_packages(packages, 3)

        assert packages == original


class TestFormatSummary:
    """The summary line printed at the end of a run."""

    def test_dry_run_never_claims_an_export(self, tmp_path):
        report = convert.Report(planned=4, skipped=1)

        summary = convert.format_summary(report, tmp_path, dry_run=True)

        assert "would export 4" in summary
        assert "Exported" not in summary

    def test_real_run_reports_counts(self, tmp_path):
        report = convert.Report(exported=2, files_written=7, skipped=1, failed=1)

        summary = convert.format_summary(report, tmp_path, dry_run=False)

        assert "Exported 2" in summary
        assert "skipped 1" in summary
        assert "failed 1" in summary
