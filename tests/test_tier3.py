"""Tests for DRM detection, listing, collisions, refresh, covers and disk floor."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import json
import os
from pathlib import Path

import pytest

from epubconvert import convert, source
from epubconvert.naming import PassthroughNaming
from tests.conftest import make_package

FONT_ENCRYPTION = """<?xml version="1.0"?>
<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <EncryptedData xmlns="http://www.w3.org/2001/04/xmlenc#">
    <EncryptionMethod Algorithm="http://ns.adobe.com/pdf/enc#RC"/>
  </EncryptedData>
</encryption>
"""

IDPF_ENCRYPTION = FONT_ENCRYPTION.replace(
    "http://ns.adobe.com/pdf/enc#RC", "http://www.idpf.org/2008/embedding"
)

REAL_ENCRYPTION = FONT_ENCRYPTION.replace(
    "http://ns.adobe.com/pdf/enc#RC", "http://www.w3.org/2001/04/xmlenc#aes256-cbc"
)


def add_meta(package: Path, relative: str, body: str) -> None:
    """Drop a file into a package's META-INF directory."""
    path = package / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestDrmDetection:
    def test_a_plain_package_is_convertible(self, tmp_path):
        package = make_package(tmp_path, "Plain.epub")

        assert source.inspect_package(package).convertible

    @pytest.mark.parametrize("body", [FONT_ENCRYPTION, IDPF_ENCRYPTION])
    def test_font_obfuscation_is_not_drm(self, tmp_path, body):
        # The critical distinction. Treating encryption.xml as DRM would have
        # wrongly skipped ~9% of a real library, every one of them readable.
        package = make_package(tmp_path, "Fonts.epub")
        add_meta(package, "META-INF/encryption.xml", body)

        status = source.inspect_package(package)

        assert status.convertible
        assert not status.drm

    def test_real_encryption_is_drm(self, tmp_path):
        package = make_package(tmp_path, "Locked.epub")
        add_meta(package, "META-INF/encryption.xml", REAL_ENCRYPTION)

        status = source.inspect_package(package)

        assert status.drm
        assert "aes256" in (status.reason or "")

    def test_fairplay_marker_is_drm(self, tmp_path):
        package = make_package(tmp_path, "Bought.epub")
        add_meta(package, "META-INF/sinf.xml", "<sinf/>")

        status = source.inspect_package(package)

        assert status.drm
        assert "FairPlay" in (status.reason or "")

    def test_mixed_algorithms_are_drm(self, tmp_path):
        package = make_package(tmp_path, "Mixed.epub")
        add_meta(
            package,
            "META-INF/encryption.xml",
            FONT_ENCRYPTION.replace(
                "</encryption>",
                "<EncryptedData><EncryptionMethod "
                'Algorithm="http://example.invalid/secret"/></EncryptedData></encryption>',
            ),
        )

        assert source.inspect_package(package).drm

    def test_unreadable_encryption_file_does_not_raise(self, tmp_path):
        package = make_package(tmp_path, "Odd.epub")
        add_meta(package, "META-INF/encryption.xml", "not xml at all")

        assert source.inspect_package(package).convertible

    def test_drm_books_are_skipped_and_counted(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Good.epub")
        locked = make_package(library, "Locked.epub")
        add_meta(locked, "META-INF/sinf.xml", "<sinf/>")

        code = convert.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        )

        assert code == 0
        assert [p.name for p in output_dir.glob("*.epub")] == ["Good.epub"]
        assert "1 DRM-protected" in capsys.readouterr().out


class TestIncompleteDetection:
    def test_the_walk_is_opt_in(self, tmp_path, monkeypatch):
        package = make_package(tmp_path, "Book.epub")
        called = {"n": 0}

        def counting(_package):
            called["n"] += 1
            return False

        monkeypatch.setattr(source, "has_dataless_files", counting)

        source.inspect_package(package, check_incomplete=False)
        assert called["n"] == 0

        source.inspect_package(package, check_incomplete=True)
        assert called["n"] == 1

    def test_a_dataless_package_is_reported(self, tmp_path, monkeypatch):
        package = make_package(tmp_path, "Cloudy.epub")
        monkeypatch.setattr(source, "has_dataless_files", lambda _p: True)

        status = source.inspect_package(package, check_incomplete=True)

        assert status.incomplete
        assert not status.convertible

    def test_normal_files_are_not_dataless(self, tmp_path):
        package = make_package(tmp_path, "Local.epub")

        assert source.has_dataless_files(package) is False


class TestCollisionSuffix:
    def test_skip_mode_exports_only_the_first(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")

        convert.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_suffix_mode_keeps_both(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library / "a", "Same.epub")
        make_package(library / "b", "Same.epub")

        convert.main(
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
        convert.main(argv)
        first = {p.name: p.stat().st_mtime_ns for p in output_dir.glob("*.epub")}

        convert.main(argv)

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
        convert.main(argv)
        capsys.readouterr()

        convert.main(argv)

        assert "Exported 0" in capsys.readouterr().out


class TestRefresh:
    def test_a_newer_source_is_re_exported(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        convert.main(argv)
        exported = output_dir / "Book.epub"
        before = exported.stat().st_mtime_ns

        # Make the source look newer than the export.
        future = exported.stat().st_mtime + 100
        os.utime(library / "Book.epub", (future, future))
        convert.main([*argv, "--refresh"])

        assert exported.stat().st_mtime_ns != before

    def test_an_unchanged_source_is_left_alone(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"]
        convert.main(argv)
        before = (output_dir / "Book.epub").stat().st_mtime_ns

        convert.main([*argv, "--refresh"])

        assert (output_dir / "Book.epub").stat().st_mtime_ns == before


class TestListing:
    def test_table_reports_each_status(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Pending.epub")
        locked = make_package(library, "Locked.epub")
        (locked / "META-INF").mkdir(parents=True, exist_ok=True)
        (locked / "META-INF" / "sinf.xml").write_text("<sinf/>", encoding="utf-8")

        code = convert.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        out = capsys.readouterr().out
        assert code == 0
        assert "pending" in out
        assert "drm" in out
        assert "Locked.epub" in out

    def test_json_is_machine_readable(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        convert.main(
            ["-s", str(library), "-o", str(output_dir), "--list", "--json", "-q"]
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["name"] == "Book.epub"
        assert payload[0]["status"] == "pending"
        assert payload[0]["target"].endswith("Book.epub")

    def test_already_exported_books_are_marked(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        convert.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])
        capsys.readouterr()

        convert.main(
            ["-s", str(library), "-o", str(output_dir), "--list", "--json", "-q"]
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["status"] == "exported"

    def test_listing_converts_nothing(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")

        convert.main(["-s", str(library), "-o", str(output_dir), "--list", "-q"])

        assert list(output_dir.glob("*.epub")) == []


class TestDiskFloor:
    def test_export_stops_when_space_is_short(self, tmp_path, output_dir, monkeypatch):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        monkeypatch.setattr(convert, "free_megabytes", lambda _p: 5)

        code = convert.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--min-free",
                "100",
                "-q",
            ]
        )

        assert code == 1
        assert list(output_dir.glob("*.epub")) == []

    def test_zero_disables_the_check(self, tmp_path, output_dir, monkeypatch):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        monkeypatch.setattr(convert, "free_megabytes", lambda _p: 0)

        code = convert.main(
            [
                "-s",
                str(library),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--min-free",
                "0",
                "-q",
            ]
        )

        assert code == 0
        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_free_space_is_reported_as_an_int(self, tmp_path):
        assert isinstance(convert.free_megabytes(tmp_path), int)


class TestCovers:
    def test_cover_is_written_beside_the_book(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        _cover_package(library / "Book.epub")

        convert.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "--covers", "-q"]
        )

        assert (output_dir / "Book.jpg").exists()
        assert (output_dir / "Book.jpg").read_bytes() == b"JPEGDATA"

    def test_no_cover_flag_writes_no_image(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        _cover_package(library / "Book.epub")

        convert.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert list(output_dir.glob("*.jpg")) == []

    def test_covers_do_not_confuse_the_export_record(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        _cover_package(library / "Book.epub")
        argv = ["-s", str(library), "-o", str(output_dir), "-m", "0", "--covers", "-q"]
        convert.main(argv)
        capsys.readouterr()

        convert.main(argv)

        # Identity globs *.epub, so the .jpg beside it must not affect reruns.
        assert "skipped 1" in capsys.readouterr().out

    def test_a_book_without_a_cover_is_fine(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Plain.epub")

        code = convert.main(
            ["-s", str(library), "-o", str(output_dir), "-m", "0", "--covers", "-q"]
        )

        assert code == 0
        assert list(output_dir.glob("*.jpg")) == []


class TestCountPendingWithStatuses:
    def test_drm_books_still_count_as_not_exported(self, tmp_path, output_dir):
        library = tmp_path / "lib"
        make_package(library, "Book.epub")
        packages = convert.collect_package_dirs(library)

        assert convert.count_pending(packages, output_dir, PassthroughNaming()) == 1


def _cover_package(package: Path) -> Path:
    """Build a package whose OPF declares a cover image."""
    opf = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Covered</dc:title>
    <dc:identifier id="bid">urn:uuid:1</dc:identifier>
  </metadata>
  <manifest>
    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover" href="images/cover.jpg" media-type="image/jpeg"
          properties="cover-image"/>
  </manifest>
  <spine><itemref idref="ch1"/></spine>
</package>
"""
    container = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    layout = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": container,
        "OEBPS/content.opf": opf,
        "OEBPS/text/ch1.xhtml": "<html><body>hi</body></html>",
    }
    for relative, body in layout.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    cover = package / "OEBPS" / "images" / "cover.jpg"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"JPEGDATA")
    return package
