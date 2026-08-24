"""Tests for source-package inspection: DRM and undownloaded iCloud stubs."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from pathlib import Path

import pytest

from epubconvert import run, source
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

    def test_an_unreadable_encryption_file_fails_closed(self, tmp_path):
        # A truncated encryption.xml is a realistic partly-synced state. It
        # must not read as "no protection": doing so exports the book as an
        # unopenable archive that is then recorded as finished work.
        package = make_package(tmp_path, "Odd.epub")
        add_meta(package, "META-INF/encryption.xml", "not xml at all")

        status = source.inspect_package(package)

        assert status.drm
        assert "could not be checked" in (status.reason or "")

    def test_a_truncated_encryption_file_fails_closed(self, tmp_path):
        package = make_package(tmp_path, "Cut.epub")
        add_meta(package, "META-INF/encryption.xml", REAL_ENCRYPTION[:60])

        assert source.inspect_package(package).drm

    def test_an_oversized_encryption_file_fails_closed(self, tmp_path):
        package = make_package(tmp_path, "Huge.epub")
        add_meta(package, "META-INF/encryption.xml", "<x/>")
        (package / "META-INF" / "encryption.xml").write_text(
            "<x>" + "y" * (source.MAX_ENCRYPTION_BYTES + 1) + "</x>", encoding="utf-8"
        )

        assert source.inspect_package(package).drm

    def test_a_package_with_no_encryption_file_is_readable(self, tmp_path):
        package = make_package(tmp_path, "Open.epub")

        assert source.inspect_package(package).convertible

    def test_drm_books_are_skipped_and_counted(self, tmp_path, output_dir, capsys):
        library = tmp_path / "lib"
        make_package(library, "Good.epub")
        locked = make_package(library, "Locked.epub")
        add_meta(locked, "META-INF/sinf.xml", "<sinf/>")

        code = run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

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
