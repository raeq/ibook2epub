"""Tests for source-package inspection: DRM and undownloaded iCloud stubs."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import io
import os
from pathlib import Path

import pytest

from epubconvert.collect import source
from epubconvert.run import run
from tests.conftest import make_package

#: A FIFO case, skipped on a platform that has none.
FIFO = pytest.param(
    "fifo",
    marks=pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here"),
)

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

    def test_an_encryption_file_that_grows_while_read_fails_closed(
        self, tmp_path, monkeypatch
    ):
        # The size is measured before the open, and the read after it was
        # unbounded, so a file that grew in between was read whole however
        # large it had become -- the gap validate's reader already closed.
        package = make_package(tmp_path, "Growing.epub")
        add_meta(package, "META-INF/encryption.xml", "<x/>")
        grown = FONT_ENCRYPTION.encode() + b" " * 1000
        monkeypatch.setattr(source, "MAX_ENCRYPTION_BYTES", 64)
        monkeypatch.setattr(source, "open_contained", lambda _path: io.BytesIO(grown))

        with pytest.raises(source.UnreadableEncryptionError, match="grew while read"):
            source.encryption_algorithms(package)

    def test_a_package_with_no_encryption_file_is_readable(self, tmp_path):
        package = make_package(tmp_path, "Open.epub")

        assert source.inspect_package(package).convertible


class TestAMarkerThatIsNotAFileFailsClosed:
    """
    A symlinked ``encryption.xml`` was refused as protection that could not
    be ruled out, but one that was a directory or a FIFO was taken for no file
    at all, and so for no protection: the book exported. The question decides
    whether a book is exported, so anything but a plain file answers "yes".
    """

    @staticmethod
    def _entry(package: Path, relative: str, kind: str) -> None:
        path = package / relative
        if kind == "directory":
            path.mkdir()
            (path / "x").write_text("<sinf/>", encoding="utf-8")
        else:
            os.mkfifo(path)

    @pytest.mark.parametrize("kind", ["directory", FIFO])
    def test_an_encryption_declaration_that_is_not_a_file(self, tmp_path, kind):
        package = make_package(tmp_path, "Odd.epub")
        self._entry(package, source.ENCRYPTION_PATH, kind)

        with pytest.raises(source.UnreadableEncryptionError):
            source.encryption_algorithms(package)
        assert source.inspect_package(package).drm

    @pytest.mark.parametrize("kind", ["directory", FIFO])
    def test_a_fairplay_marker_that_is_not_a_file(self, tmp_path, kind):
        package = make_package(tmp_path, "Odd.epub")
        self._entry(package, source.SINF_PATH, kind)

        assert source.inspect_package(package).drm

    def test_a_marker_that_cannot_be_looked_at_fails_closed(
        self, tmp_path, monkeypatch
    ):
        # The containment rule refuses a path it cannot stat, so this is the
        # state a race leaves: cleared by the rule, then unanswerable.
        package = make_package(tmp_path, "Odd.epub")
        meta_inf = package / "META-INF"
        for child in meta_inf.iterdir():
            child.unlink()
        meta_inf.rmdir()
        meta_inf.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(source, "resolve", lambda root, relative: root / relative)

        with pytest.raises(source.UnreadableEncryptionError):
            source.encryption_algorithms(package)
        assert source.has_drm(package) == (
            True,
            f"{source.SINF_PATH} could not be read",
        )

    @pytest.mark.parametrize("marker", [source.ENCRYPTION_PATH, source.SINF_PATH])
    def test_the_book_is_not_exported(self, tmp_path, output_dir, marker):
        library = tmp_path / "lib"
        package = make_package(library, "Odd.epub")
        self._entry(package, marker, "directory")

        run.main(["-s", str(library), "-o", str(output_dir), "-m", "0", "-q"])

        assert list(output_dir.glob("*.epub")) == []


class TestTheEncryptionDeclarationDeclaresNoEntities:
    """
    ``container.xml`` and the package document are refused for declaring an
    XML entity, which lets a small file expand into a large one. The
    encryption declaration was parsed without that rule, so it was the one
    document a book could still inflate that way.
    """

    def test_an_entity_is_refused_and_fails_closed(self, tmp_path):
        package = make_package(tmp_path, "Entities.epub")
        declared = IDPF_ENCRYPTION.replace(
            '<?xml version="1.0"?>',
            '<?xml version="1.0"?><!DOCTYPE encryption [<!ENTITY a "x">]>',
        )
        add_meta(package, source.ENCRYPTION_PATH, declared)

        with pytest.raises(source.UnreadableEncryptionError):
            source.encryption_algorithms(package)
        assert source.inspect_package(package).drm

    def test_a_doctype_without_entities_is_still_read(self, tmp_path):
        package = make_package(tmp_path, "Doctype.epub")
        declared = IDPF_ENCRYPTION.replace(
            '<?xml version="1.0"?>', '<?xml version="1.0"?><!DOCTYPE encryption>'
        )
        add_meta(package, source.ENCRYPTION_PATH, declared)

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
