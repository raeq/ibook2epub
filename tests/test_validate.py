"""Tests for structural validation, the OPF reader, and --validate/--verify."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from epubconvert import cli, run, validate
from epubconvert.archive import zip_package

CONTAINER = """<?xml version="1.0"?>
<container version="1.0"
           xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>A Wizard of Earthsea</dc:title>
    <dc:creator opf:file-as="Le Guin, Ursula K."
                xmlns:opf="http://www.idpf.org/2007/opf">Ursula K. Le Guin</dc:creator>
    <dc:identifier id="bid">urn:isbn:9780553383041</dc:identifier>
  </metadata>
  <manifest>
    <item id="ch1" href="text/chapter1.xhtml" media-type="application/xhtml+xml"/>
    <item id="cover" href="images/cover.jpg" media-type="image/jpeg"
          properties="cover-image"/>
  </manifest>
  <spine>
    <itemref idref="ch1"/>
  </spine>
</package>
"""

MEMBERS = {
    "META-INF/container.xml": CONTAINER,
    "OEBPS/content.opf": OPF,
    "OEBPS/text/chapter1.xhtml": "<html><body>Ged</body></html>",
    "OEBPS/images/cover.jpg": "not really a jpeg",
}


def write_epub(path: Path, members: dict[str, str] | None = None, **kwargs) -> Path:
    """Write a minimal epub, optionally with members omitted or altered."""
    contents = dict(MEMBERS if members is None else members)
    mimetype_first = kwargs.get("mimetype_first", True)
    stored = kwargs.get("stored", True)
    mimetype = kwargs.get("mimetype", "application/epub+zip")

    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        if mimetype_first and mimetype is not None:
            archive.writestr(
                ZipInfo("mimetype"),
                mimetype,
                compress_type=ZIP_STORED if stored else ZIP_DEFLATED,
            )
        for name, body in contents.items():
            archive.writestr(name, body)
        if not mimetype_first and mimetype is not None:
            archive.writestr(
                ZipInfo("mimetype"),
                mimetype,
                compress_type=ZIP_STORED if stored else ZIP_DEFLATED,
            )
    return path


@pytest.fixture(name="good_epub")
def good_epub_fixture(tmp_path: Path) -> Path:
    """A structurally sound epub."""
    return write_epub(tmp_path / "Good.epub")


class TestValidArchive:
    def test_a_sound_archive_has_no_problems(self, good_epub):
        assert validate.validate_archive(good_epub) == []


class TestMimetypeChecks:
    def test_mimetype_must_be_first(self, tmp_path):
        path = write_epub(tmp_path / "Late.epub", mimetype_first=False)

        problems = validate.validate_archive(path)

        assert any("first member" in p for p in problems)

    def test_mimetype_must_be_stored(self, tmp_path):
        path = write_epub(tmp_path / "Squashed.epub", stored=False)

        problems = validate.validate_archive(path)

        assert any("compressed" in p for p in problems)

    def test_mimetype_content_is_checked(self, tmp_path):
        path = write_epub(tmp_path / "Wrong.epub", mimetype="application/zip")

        problems = validate.validate_archive(path)

        assert any("application/epub+zip" in p for p in problems)

    def test_missing_mimetype_is_reported(self, tmp_path):
        path = write_epub(tmp_path / "None.epub", mimetype=None)

        problems = validate.validate_archive(path)

        assert problems


class TestManifestChecks:
    def test_a_dropped_content_file_is_caught(self, tmp_path):
        # This is the bug that actually shipped: a manifest item silently
        # missing from the archive. The zip stays valid; the book does not.
        members = dict(MEMBERS)
        del members["OEBPS/text/chapter1.xhtml"]
        path = write_epub(tmp_path / "Gutted.epub", members)

        problems = validate.validate_archive(path)

        assert any("manifest item is not in the archive" in p for p in problems)
        assert any("ch1" in p for p in problems)

    def test_a_dropped_cover_is_caught(self, tmp_path):
        members = dict(MEMBERS)
        del members["OEBPS/images/cover.jpg"]
        path = write_epub(tmp_path / "NoCover.epub", members)

        assert validate.validate_archive(path)

    def test_missing_container_is_caught(self, tmp_path):
        members = dict(MEMBERS)
        del members["META-INF/container.xml"]
        path = write_epub(tmp_path / "NoContainer.epub", members)

        problems = validate.validate_archive(path)

        assert any("META-INF/container.xml" in p for p in problems)

    def test_missing_opf_is_caught(self, tmp_path):
        members = dict(MEMBERS)
        del members["OEBPS/content.opf"]
        path = write_epub(tmp_path / "NoOpf.epub", members)

        problems = validate.validate_archive(path)

        assert any("OEBPS/content.opf" in p for p in problems)

    def test_malformed_xml_is_caught(self, tmp_path):
        members = dict(MEMBERS)
        members["OEBPS/content.opf"] = "<package><unclosed>"
        path = write_epub(tmp_path / "Broken.epub", members)

        problems = validate.validate_archive(path)

        assert any("not valid XML" in p for p in problems)

    def test_dangling_spine_reference_is_caught(self, tmp_path):
        members = dict(MEMBERS)
        members["OEBPS/content.opf"] = OPF.replace('idref="ch1"', 'idref="ghost"')
        path = write_epub(tmp_path / "Ghost.epub", members)

        problems = validate.validate_archive(path)

        assert any("spine references unknown manifest id: ghost" in p for p in problems)


class TestUnreadableArchives:
    def test_a_non_zip_is_reported_not_raised(self, tmp_path):
        path = tmp_path / "Truncated.epub"
        path.write_bytes(b"this is not a zip file")

        problems = validate.validate_archive(path)

        assert any("not a readable zip" in p for p in problems)

    def test_a_missing_file_is_reported_not_raised(self, tmp_path):
        problems = validate.validate_archive(tmp_path / "absent.epub")

        assert any("could not open" in p for p in problems)


class TestReadPackage:
    def test_metadata_is_extracted(self, good_epub):
        with ZipFile(good_epub) as archive:
            package = validate.read_package(archive)

        assert package.title == "A Wizard of Earthsea"
        assert package.creator == "Ursula K. Le Guin"
        assert package.identifier == "urn:isbn:9780553383041"

    def test_publisher_supplied_sort_name_is_preferred(self, good_epub):
        # Beats guessing at inversion: "Le Guin, Ursula K." is not something a
        # whitespace split would produce.
        with ZipFile(good_epub) as archive:
            package = validate.read_package(archive)

        assert package.creator_sort == "Le Guin, Ursula K."

    def test_manifest_hrefs_resolve_against_the_opf_directory(self, good_epub):
        with ZipFile(good_epub) as archive:
            package = validate.read_package(archive)

        assert package.manifest["ch1"] == "OEBPS/text/chapter1.xhtml"

    def test_spine_is_read_in_order(self, good_epub):
        with ZipFile(good_epub) as archive:
            package = validate.read_package(archive)

        assert package.spine == ["ch1"]

    def test_cover_is_identified(self, good_epub):
        with ZipFile(good_epub) as archive:
            package = validate.read_package(archive)

        assert package.cover_id == "cover"

    def test_percent_encoded_hrefs_are_decoded(self, tmp_path):
        members = dict(MEMBERS)
        members["OEBPS/content.opf"] = OPF.replace(
            'href="text/chapter1.xhtml"', 'href="text/chapter%201.xhtml"'
        )
        members["OEBPS/text/chapter 1.xhtml"] = members.pop("OEBPS/text/chapter1.xhtml")
        path = write_epub(tmp_path / "Encoded.epub", members)

        assert validate.validate_archive(path) == []


class TestValidationOptions:
    def test_disabled_checks_nothing(self, tmp_path):
        path = tmp_path / "junk.epub"
        path.write_bytes(b"not a zip")

        assert validate.ValidationOptions(enabled=False).check(path) == []

    def test_enabled_reports_problems(self, tmp_path):
        path = tmp_path / "junk.epub"
        path.write_bytes(b"not a zip")

        assert validate.ValidationOptions(enabled=True).check(path)


class TestValidateDuringExport:
    def test_a_valid_book_is_written(self, output_dir):
        # The real fixture library lacks an OPF, so build a faithful package.
        source = _package_from_members(output_dir.parent / "src" / "Book.epub")

        count = zip_package(
            source,
            output_dir / "Book.epub",
            validate.ValidationOptions(enabled=True),
        )

        assert count > 0
        assert (output_dir / "Book.epub").exists()

    def test_an_invalid_book_is_not_written(self, tmp_path, output_dir):
        source = _package_from_members(
            tmp_path / "Bad.epub", drop="text/chapter1.xhtml"
        )
        target = output_dir / "Bad.epub"

        with pytest.raises(validate.ArchiveInvalidError):
            zip_package(
                source, target, validate.ValidationOptions(enabled=True)
            )

        # The whole point of validating before the replace: nothing lands, so
        # the book is retried rather than recorded as done.
        assert not target.exists()
        assert list(output_dir.glob("*.part")) == []

    def test_export_reports_the_failure(self, tmp_path, output_dir, capsys):
        _package_from_members(tmp_path / "lib" / "Bad.epub", drop="text/chapter1.xhtml")

        code = run.main(
            [
                "-s",
                str(tmp_path / "lib"),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "--validate",
                "-q",
            ]
        )

        assert code == 1
        assert "failed 1" in capsys.readouterr().out
        assert list(output_dir.glob("*.epub")) == []

    def test_without_the_flag_the_bad_book_is_written(self, tmp_path, output_dir):
        _package_from_members(tmp_path / "lib" / "Bad.epub", drop="text/chapter1.xhtml")

        run.main(
            ["-s", str(tmp_path / "lib"), "-o", str(output_dir), "-m", "0", "-q"]
        )

        assert len(list(output_dir.glob("*.epub"))) == 1


class TestVerifyMode:
    def test_sound_archives_pass(self, tmp_path, output_dir, capsys):
        write_epub(output_dir / "Good.epub")
        source = tmp_path / "lib"
        source.mkdir()

        code = run.main(
            ["-s", str(source), "-o", str(output_dir), "--verify", "-q"]
        )

        assert code == 0
        assert "0 damaged" in capsys.readouterr().out

    def test_damaged_archives_are_reported(self, tmp_path, output_dir, capsys):
        write_epub(output_dir / "Good.epub")
        (output_dir / "Broken.epub").write_bytes(b"not a zip at all")
        source = tmp_path / "lib"
        source.mkdir()

        code = run.main(
            ["-s", str(source), "-o", str(output_dir), "--verify", "-q"]
        )

        out = capsys.readouterr().out
        assert code == 1
        assert "1 damaged" in out
        assert "--force" in out

    def test_verify_converts_nothing(self, tmp_path, output_dir):
        _package_from_members(tmp_path / "lib" / "Book.epub")

        run.main(
            ["-s", str(tmp_path / "lib"), "-o", str(output_dir), "--verify", "-q"]
        )

        assert list(output_dir.glob("*.epub")) == []


class TestEpubcheck:
    def test_availability_probe_does_not_raise(self):
        assert isinstance(validate.epubcheck_available(), bool)

    def test_missing_tool_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr("epubconvert.validate.shutil.which", lambda _name: None)

        assert validate.run_epubcheck(tmp_path / "x.epub") == [
            "epubcheck is not on PATH"
        ]

    def test_flag_is_rejected_without_the_tool(self, library, monkeypatch):
        monkeypatch.setattr(cli, "epubcheck_available", lambda: False)

        with pytest.raises(SystemExit):
            cli.parse_args(["-s", str(library), "--epubcheck"])

    def test_flag_implies_validate(self, library, monkeypatch):
        monkeypatch.setattr(cli, "epubcheck_available", lambda: True)

        args = cli.parse_args(["-s", str(library), "--epubcheck"])

        assert args.validate is True


def _package_from_members(package: Path, drop: str | None = None) -> Path:
    """Build a source package directory mirroring the sample epub."""
    layout = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": CONTAINER,
        "OEBPS/content.opf": OPF,
        "OEBPS/text/chapter1.xhtml": "<html><body>Ged</body></html>",
        "OEBPS/images/cover.jpg": "not really a jpeg",
    }
    if drop is not None:
        layout.pop(f"OEBPS/{drop}")
    for relative, body in layout.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return package
