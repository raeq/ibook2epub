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

from epubconvert import convert, planning
from tests.conftest import EXPECTED_MEMBERS, make_package


def export(packages: Sequence[Path], output_dir: Path, **kwargs: Any) -> convert.Report:
    """Run the async exporter from a synchronous test."""
    return asyncio.run(convert.export_packages(packages, output_dir, **kwargs))


def archive_mimetype(path: Path) -> bytes:
    """Read the mimetype member out of an exported archive."""
    with ZipFile(path) as archive:
        return archive.read("mimetype")


class TestIsExcluded:
    """The exclusion rules applied to package members."""

    @pytest.mark.parametrize(
        "name",
        ["mimetype", ".DS_Store", "iTunesMetadata.plist", "bookmarks.plist"],
    )
    def test_apple_bookkeeping_is_excluded_at_the_root(self, name):
        assert convert.is_excluded(name, at_root=True)

    @pytest.mark.parametrize(
        "name",
        ["content.opf", "chapter1.xhtml", "container.xml", "cover.jpg", "style.css"],
    )
    def test_content_is_kept(self, name):
        assert not convert.is_excluded(name, at_root=True)

    @pytest.mark.parametrize(
        "name",
        ["mimetype", "bookmarks.xhtml", "bookmarks.plist", "settings.plist"],
    )
    def test_root_only_patterns_do_not_apply_deeper(self, name):
        # Regression: these patterns used to match at every depth, so a real
        # chapter named bookmarks.xhtml or a .plist data asset was silently
        # dropped, corrupting the book.
        assert not convert.is_excluded(name, at_root=False)

    def test_ds_store_is_excluded_at_every_depth(self):
        assert convert.is_excluded(".DS_Store", at_root=True)
        assert convert.is_excluded(".DS_Store", at_root=False)


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

    def test_nested_content_is_not_mistaken_for_apple_bookkeeping(
        self, tmp_path, output_dir
    ):
        # Regression: a book whose content happens to be named like Apple's
        # root bookkeeping was exported with those files silently missing,
        # which breaks readers on the absent spine items.
        package = tmp_path / "Book.epub"
        for relative, content in {
            "mimetype": "bogus",
            "iTunesMetadata.plist": "<plist/>",
            "META-INF/container.xml": "<container/>",
            "OEBPS/content.opf": "<package/>",
            "OEBPS/bookmarks.xhtml": "<html>a real chapter</html>",
            "OEBPS/data/settings.plist": "<data/>",
            "OEBPS/mimetype": "a real content file",
        }.items():
            path = package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        target = output_dir / "Book.epub"
        convert.zip_package(package, target)

        with ZipFile(target) as archive:
            members = set(archive.namelist())

        assert "OEBPS/bookmarks.xhtml" in members
        assert "OEBPS/data/settings.plist" in members
        assert "OEBPS/mimetype" in members
        # The root bookkeeping is still dropped, and the root mimetype is the
        # one we rewrote rather than the bogus original.
        assert "iTunesMetadata.plist" not in members
        assert archive_mimetype(target) == b"application/epub+zip"

    def test_nested_ds_store_is_still_dropped(self, tmp_path, output_dir):
        package = tmp_path / "Book.epub"
        (package / "OEBPS").mkdir(parents=True)
        (package / "OEBPS" / ".DS_Store").write_bytes(b"junk")
        (package / "OEBPS" / "content.opf").write_text("<package/>", encoding="utf-8")

        target = output_dir / "Book.epub"
        convert.zip_package(package, target)

        with ZipFile(target) as archive:
            assert "OEBPS/.DS_Store" not in archive.namelist()

    def test_exports_a_name_already_at_the_filesystem_limit(self, tmp_path, output_dir):
        # Regression: the temporary name appended ".part" to a filename that
        # was already at the 255-byte limit, producing a 260-byte path that
        # the filesystem refuses. It fired only in portable mode, aimed at
        # exactly the filesystems with the tighter limits.
        name = "x" * (255 - len(".epub")) + ".epub"
        assert len(name.encode()) == 255
        package = make_package(tmp_path / "src", name)
        target = output_dir / name

        convert.zip_package(package, target)

        assert target.exists()
        assert list(output_dir.glob("*.part")) == []

    def test_exports_a_multibyte_name_at_the_limit(self, tmp_path, output_dir):
        # Same bug, but where truncating by characters would split a
        # multi-byte sequence.
        stem = "é" * 125  # 250 bytes
        name = stem + ".epub"
        assert len(name.encode()) == 255
        package = make_package(tmp_path / "src", name)
        target = output_dir / name

        convert.zip_package(package, target)

        assert target.exists()

    def test_leaves_no_partial_file_behind_on_success(self, library, output_dir):
        convert.zip_package(library / "Book One.epub", output_dir / "Book One.epub")

        assert list(output_dir.glob("*.part")) == []

    def test_failure_leaves_neither_target_nor_partial(
        self, library, output_dir, monkeypatch
    ):
        target = output_dir / "Book One.epub"

        def boom(_name, **_kwargs):
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
        # Counted as a collision rather than a skip: the second book was not
        # already exported, it lost a race for the name.
        assert report.collisions == 1
        assert report.skipped == 0

    def test_a_failing_package_is_reported_not_raised(
        self, library, output_dir, monkeypatch
    ):
        def boom(_name, **_kwargs):
            raise OSError("disk fell over")

        monkeypatch.setattr(convert, "is_excluded", boom)
        packages = convert.collect_package_dirs(library)

        report = export(packages, output_dir)

        assert report.failed == 2
        assert report.exported == 0

    def test_empty_input_is_a_no_op(self, output_dir):
        report = export([], output_dir)

        assert report == convert.Report()


class TestCapExports:
    """Application of the export cap."""

    @staticmethod
    def pending(count: int) -> list[planning.Decision]:
        return [
            planning.Decision(Path(f"{i}.epub"), planning.PENDING, Path(f"{i}.epub"))
            for i in range(count)
        ]

    def test_zero_means_no_limit(self):
        assert len(convert.cap_exports(self.pending(10), 0)) == 10

    def test_cap_is_applied(self):
        assert len(convert.cap_exports(self.pending(10), 3)) == 3

    def test_no_shuffle_takes_them_in_order(self):
        decisions = self.pending(10)

        selected = convert.cap_exports(decisions, 3, randomise=False)

        assert selected == decisions[:3]

    def test_selection_does_not_mutate_the_input(self):
        decisions = self.pending(10)
        original = list(decisions)

        convert.cap_exports(decisions, 3)

        assert decisions == original

    def test_the_cap_counts_only_pending_books(self):
        # The cap is documented as a limit on files exported. Counting
        # already-exported books against it stalls the run: every book the cap
        # admits is one it will not write.
        done = [
            planning.Decision(Path(f"done{i}.epub"), planning.ALREADY)
            for i in range(5)
        ]
        decisions = [*done, *self.pending(4)]

        selected = convert.cap_exports(decisions, 2, randomise=False)

        assert sum(1 for d in selected if d.status == planning.PENDING) == 2

    def test_books_that_will_not_be_written_are_all_kept(self):
        # They carry the skipped, DRM and undownloaded counts for the summary,
        # which should describe the library rather than the capped slice.
        done = [
            planning.Decision(Path(f"done{i}.epub"), planning.ALREADY)
            for i in range(5)
        ]
        decisions = [*done, *self.pending(4)]

        selected = convert.cap_exports(decisions, 1, randomise=False)

        assert sum(1 for d in selected if d.status == planning.ALREADY) == 5


class TestSweepPartials:
    """Cleanup of temporaries left by a run that was killed outright."""

    def test_abandoned_temporaries_are_removed(self, output_dir):
        stale = output_dir / f"tmpabcd1234{convert.PARTIAL_SUFFIX}"
        stale.write_bytes(b"half an archive")

        assert convert.sweep_partials(output_dir) == 1
        assert not stale.exists()

    def test_real_exports_are_left_alone(self, output_dir):
        book = output_dir / "Book.epub"
        book.write_bytes(b"a whole archive")

        convert.sweep_partials(output_dir)

        assert book.exists()

    def test_a_run_clears_what_an_earlier_one_abandoned(self, tmp_path, output_dir):
        # Every glob in the tool looks for *.epub, so nothing else would ever
        # see these again and they would accumulate on the volume --min-free
        # exists to protect.
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        stale = output_dir / f"tmpdeadbeef{convert.PARTIAL_SUFFIX}"
        stale.write_bytes(b"half an archive")

        convert.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert not stale.exists()
        assert (output_dir / "Book.epub").exists()


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
