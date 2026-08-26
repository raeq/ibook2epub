"""Tests for structural validation, the OPF reader, and --validate/--verify."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from epubconvert import cli, exits, run, validate
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
            zip_package(source, target, validate.ValidationOptions(enabled=True))

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

        run.main(["-s", str(tmp_path / "lib"), "-o", str(output_dir), "-m", "0", "-q"])

        assert len(list(output_dir.glob("*.epub"))) == 1


class TestVerifyMode:
    def test_sound_archives_pass(self, tmp_path, output_dir, capsys):
        write_epub(output_dir / "Good.epub")
        source = tmp_path / "lib"
        source.mkdir()

        code = run.main(["-s", str(source), "-o", str(output_dir), "--verify", "-q"])

        assert code == 0
        assert "0 damaged" in capsys.readouterr().out

    def test_damaged_archives_are_reported(self, tmp_path, output_dir, capsys):
        write_epub(output_dir / "Good.epub")
        (output_dir / "Broken.epub").write_bytes(b"not a zip at all")
        source = tmp_path / "lib"
        source.mkdir()

        code = run.main(["-s", str(source), "-o", str(output_dir), "--verify", "-q"])

        out = capsys.readouterr().out
        assert code == exits.DAMAGED
        assert "1 damaged" in out
        assert "--force" in out

    def test_verify_converts_nothing(self, tmp_path, output_dir):
        _package_from_members(tmp_path / "lib" / "Book.epub")

        run.main(["-s", str(tmp_path / "lib"), "-o", str(output_dir), "--verify", "-q"])

        assert list(output_dir.glob("*.epub")) == []


class TestEpubcheck:
    def test_availability_probe_does_not_raise(self):
        assert isinstance(validate.epubcheck_available(), bool)

    def test_missing_tool_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr("epubconvert.validate.shutil.which", lambda _name: None)

        assert validate.run_epubcheck(tmp_path / "x.epub") == [
            "epubcheck is not on PATH"
        ]

    def test_a_missing_tool_has_its_own_exit_code(self, library, tmp_path, monkeypatch):
        # Was a usage error, indistinguishable from a typo'd flag. Whether the
        # tool is installed is a fact about the machine, not about the command
        # line, so it is checked in run.main and carries its own code.
        monkeypatch.setattr("epubconvert.run.epubcheck_available", lambda: False)

        code = run.main(
            ["-s", str(library), "-o", str(tmp_path / "out"), "--epubcheck", "-q"]
        )

        assert code == exits.MISSING_TOOL

    def test_flag_implies_validate(self, library):
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


def _opf_with(metadata: str, unique_id: str | None = "bid") -> str:
    """Build a package document with a chosen metadata block."""
    attribute = "" if unique_id is None else f' unique-identifier="{unique_id}"'
    return (
        '<?xml version="1.0"?>\n'
        f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0"{attribute}>\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"{metadata}\n"
        "  </metadata>\n"
        "  <manifest>\n"
        '    <item id="ch1" href="text/chapter1.xhtml"'
        ' media-type="application/xhtml+xml"/>\n'
        "  </manifest>\n"
        '  <spine><itemref idref="ch1"/></spine>\n'
        "</package>\n"
    )


def _read_both_ways(
    tmp_path: Path, opf: str
) -> tuple[validate.Package, validate.Package]:
    """Parse one package document through both readers.

    Identifier selection has two entry points -- ``read_package`` for an
    archive and ``read_package_dir`` for a source directory -- and both go
    through ``_package_from_root``. Returning both keeps one test per site.
    """
    members = {
        "META-INF/container.xml": CONTAINER,
        "OEBPS/content.opf": opf,
        "OEBPS/text/chapter1.xhtml": "<html><body>Ged</body></html>",
    }
    archive_path = write_epub(tmp_path / "archive.epub", members)
    with ZipFile(archive_path) as opened:
        from_archive = validate.read_package(opened)

    package = tmp_path / "source" / "Book.epub"
    for relative, body in {"mimetype": "application/epub+zip", **members}.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return from_archive, validate.read_package_dir(package)


#: A retail id listed first, and the UUID the package actually declares.
RETAIL_ID = "B07691HV88"
CANONICAL_ID = "urn:uuid:fb2c92bb-c9e4-49a7-bc9e-3b06350c44a5"

TWO_IDENTIFIERS = (
    "    <dc:title>A Wizard of Earthsea</dc:title>\n"
    f'    <dc:identifier id="asin">{RETAIL_ID}</dc:identifier>\n'
    f'    <dc:identifier id="bid">{CANONICAL_ID}</dc:identifier>'
)


class TestCanonicalIdentifier:
    """
    The spec names the canonical identifier through the ``unique-identifier``
    IDREF on ``<package>``. Taking the first ``dc:identifier`` in document
    order returned a retailer ASIN or ISBN instead for 798 of 2,805 books in a
    real library, because publishers list the retail id first.
    """

    def test_the_archive_reader_honours_the_idref(self, tmp_path):
        from_archive, _ = _read_both_ways(tmp_path, _opf_with(TWO_IDENTIFIERS))

        assert from_archive.identifier == CANONICAL_ID

    def test_the_directory_reader_honours_the_idref(self, tmp_path):
        _, from_directory = _read_both_ways(tmp_path, _opf_with(TWO_IDENTIFIERS))

        assert from_directory.identifier == CANONICAL_ID

    def test_a_dangling_idref_falls_back_to_the_first(self, tmp_path):
        # 17 books in the surveyed library point at an id that is not there.
        opf = _opf_with(TWO_IDENTIFIERS, unique_id="absent")

        from_archive, from_directory = _read_both_ways(tmp_path, opf)

        assert from_archive.identifier == RETAIL_ID
        assert from_directory.identifier == RETAIL_ID

    def test_a_missing_attribute_falls_back_to_the_first(self, tmp_path):
        # 2 books in the surveyed library omit unique-identifier entirely.
        opf = _opf_with(TWO_IDENTIFIERS, unique_id=None)

        from_archive, from_directory = _read_both_ways(tmp_path, opf)

        assert from_archive.identifier == RETAIL_ID
        assert from_directory.identifier == RETAIL_ID

    def test_an_empty_canonical_element_falls_back_to_the_first(self, tmp_path):
        metadata = (
            "    <dc:title>A Wizard of Earthsea</dc:title>\n"
            f'    <dc:identifier id="asin">{RETAIL_ID}</dc:identifier>\n'
            '    <dc:identifier id="bid"></dc:identifier>'
        )

        from_archive, from_directory = _read_both_ways(tmp_path, _opf_with(metadata))

        assert from_archive.identifier == RETAIL_ID
        assert from_directory.identifier == RETAIL_ID

    def test_a_single_identifier_is_still_read(self, tmp_path):
        metadata = """    <dc:title>A Wizard of Earthsea</dc:title>
    <dc:identifier id="bid">  urn:isbn:9780553383041  </dc:identifier>"""

        from_archive, from_directory = _read_both_ways(tmp_path, _opf_with(metadata))

        assert from_archive.identifier == "urn:isbn:9780553383041"
        assert from_directory.identifier == "urn:isbn:9780553383041"


EPUB3_REFINES = (
    "    <dc:title>Sapiens</dc:title>\n"
    '    <dc:creator id="author">Yuval Noah Harari</dc:creator>\n'
    '    <meta refines="#author" property="file-as">Harari, Yuval Noah</meta>\n'
    f'    <dc:identifier id="bid">{CANONICAL_ID}</dc:identifier>'
)


class TestSortNameFromEpub3Refines:
    """
    EPUB2 put the sort name in an ``opf:file-as`` attribute on ``dc:creator``.
    EPUB3 moved it to a ``<meta refines="#id" property="file-as">`` element and
    deprecated the attribute. Apple's library is overwhelmingly EPUB3, so
    reading only the attribute left 301 of 2,793 books with a creator -- 10.8%
    -- named in display order under a policy whose whole purpose is sorting by
    author.
    """

    def test_the_archive_reader_finds_it(self, tmp_path):
        from_archive, _ = _read_both_ways(tmp_path, _opf_with(EPUB3_REFINES))

        assert from_archive.creator_sort == "Harari, Yuval Noah"

    def test_the_directory_reader_finds_it(self, tmp_path):
        _, from_directory = _read_both_ways(tmp_path, _opf_with(EPUB3_REFINES))

        assert from_directory.creator_sort == "Harari, Yuval Noah"

    def test_the_display_name_is_still_the_creator(self, tmp_path):
        from_archive, _ = _read_both_ways(tmp_path, _opf_with(EPUB3_REFINES))

        assert from_archive.creator == "Yuval Noah Harari"

    def test_the_legacy_attribute_still_wins_when_both_are_present(self, tmp_path):
        metadata = (
            "    <dc:title>Sapiens</dc:title>\n"
            '    <dc:creator id="author" opf:file-as="Attribute, The"'
            ' xmlns:opf="http://www.idpf.org/2007/opf">Yuval Noah Harari</dc:creator>\n'
            '    <meta refines="#author" property="file-as">Refines, The</meta>'
        )

        from_archive, _ = _read_both_ways(tmp_path, _opf_with(metadata))

        assert from_archive.creator_sort == "Attribute, The"

    def test_the_first_file_as_wins_when_several_refine_one_id(self, tmp_path):
        # A real publisher error: the author and the illustrator both refine
        # #creator, so the element carries two sort names. Taking the last gave
        # the illustrator's.
        metadata = (
            "    <dc:title>A Boy Called Christmas</dc:title>\n"
            '    <dc:creator id="creator">Matt Haig</dc:creator>\n'
            '    <meta property="role" refines="#creator">aut</meta>\n'
            '    <meta property="file-as" refines="#creator">Haig, Matt</meta>\n'
            '    <meta property="role" refines="#creator">ill</meta>\n'
            '    <meta property="file-as" refines="#creator">Mould, Chris</meta>'
        )

        from_archive, _ = _read_both_ways(tmp_path, _opf_with(metadata))

        assert from_archive.creator_sort == "Haig, Matt"

    def test_a_meta_refining_something_else_is_ignored(self, tmp_path):
        metadata = (
            "    <dc:title>Sapiens</dc:title>\n"
            '    <dc:creator id="author">Yuval Noah Harari</dc:creator>\n'
            '    <meta refines="#publisher" property="file-as">Wrong, Very</meta>'
        )

        from_archive, _ = _read_both_ways(tmp_path, _opf_with(metadata))

        assert from_archive.creator_sort is None

    def test_a_creator_with_no_id_has_nothing_to_refine(self, tmp_path):
        metadata = (
            "    <dc:title>Sapiens</dc:title>\n"
            "    <dc:creator>Yuval Noah Harari</dc:creator>\n"
            '    <meta refines="#author" property="file-as">Harari, Yuval Noah</meta>'
        )

        from_archive, _ = _read_both_ways(tmp_path, _opf_with(metadata))

        assert from_archive.creator_sort is None

    def test_an_empty_refines_value_is_ignored(self, tmp_path):
        metadata = (
            "    <dc:title>Sapiens</dc:title>\n"
            '    <dc:creator id="author">Yuval Noah Harari</dc:creator>\n'
            '    <meta refines="#author" property="file-as">   </meta>'
        )

        from_archive, _ = _read_both_ways(tmp_path, _opf_with(metadata))

        assert from_archive.creator_sort is None

    def test_a_different_property_is_not_a_sort_name(self, tmp_path):
        metadata = (
            "    <dc:title>Sapiens</dc:title>\n"
            '    <dc:creator id="author">Yuval Noah Harari</dc:creator>\n'
            '    <meta refines="#author" property="role">aut</meta>'
        )

        from_archive, _ = _read_both_ways(tmp_path, _opf_with(metadata))

        assert from_archive.creator_sort is None


class _Completed:
    """A stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRunningEpubcheck:
    """
    ``epubcheck`` is an optional external tool and is rarely installed, so
    every branch past "is it on PATH" ran unexercised. Mocking the subprocess
    keeps the suite free of a JVM while still pinning what the wrapper does
    with what the tool says.
    """

    @staticmethod
    def _installed(
        monkeypatch, result=None, raises=None
    ) -> list[tuple[list[str], dict[str, object]]]:
        """Pretend the tool is installed, and record how it was invoked."""
        calls: list[tuple[list[str], dict[str, object]]] = []
        monkeypatch.setattr(
            "epubconvert.validate.shutil.which", lambda _name: "/usr/bin/epubcheck"
        )

        def fake_run(command, **options):
            calls.append((command, options))
            if raises is not None:
                raise raises
            return result

        monkeypatch.setattr("epubconvert.validate.subprocess.run", fake_run)
        return calls

    def test_a_clean_archive_reports_nothing(self, tmp_path, monkeypatch):
        self._installed(monkeypatch, _Completed(returncode=0))

        assert validate.run_epubcheck(tmp_path / "Book.epub") == []

    def test_error_lines_are_returned_stripped(self, tmp_path, monkeypatch):
        stderr = (
            "  ERROR(RSC-005): bad markup\nINFO: checking\n  ERROR(OPF-030): oops\n"
        )
        self._installed(monkeypatch, _Completed(returncode=1, stderr=stderr))

        assert validate.run_epubcheck(tmp_path / "Book.epub") == [
            "ERROR(RSC-005): bad markup",
            "ERROR(OPF-030): oops",
        ]

    def test_stdout_is_read_when_stderr_is_empty(self, tmp_path, monkeypatch):
        # The tool writes to either stream depending on how it was built.
        self._installed(
            monkeypatch, _Completed(returncode=1, stdout="ERROR(PKG-001): broken")
        )

        assert validate.run_epubcheck(tmp_path / "Book.epub") == [
            "ERROR(PKG-001): broken"
        ]

    def test_a_failure_with_no_error_lines_names_the_exit_code(
        self, tmp_path, monkeypatch
    ):
        # Silence plus a non-zero status still has to say something, or the
        # archive is reported as failing validation for no stated reason.
        self._installed(monkeypatch, _Completed(returncode=3, stderr="WARNING: hmm"))

        assert validate.run_epubcheck(tmp_path / "Book.epub") == [
            "epubcheck failed with exit code 3"
        ]

    def test_at_most_ten_errors_are_reported(self, tmp_path, monkeypatch):
        stderr = "\n".join(f"ERROR({index}): bad" for index in range(25))
        self._installed(monkeypatch, _Completed(returncode=1, stderr=stderr))

        assert len(validate.run_epubcheck(tmp_path / "Book.epub")) == 10

    def test_a_tool_that_cannot_be_run_is_reported(self, tmp_path, monkeypatch):
        self._installed(monkeypatch, raises=OSError("Exec format error"))

        problems = validate.run_epubcheck(tmp_path / "Book.epub")

        assert problems == ["epubcheck could not be run: Exec format error"]

    def test_a_timeout_is_reported_rather_than_raised(self, tmp_path, monkeypatch):
        # A book big enough to exceed the timeout must fail the check, not the
        # run: everything already converted stays converted.
        self._installed(monkeypatch, raises=subprocess.TimeoutExpired("epubcheck", 120))

        problems = validate.run_epubcheck(tmp_path / "Book.epub")

        assert len(problems) == 1
        assert problems[0].startswith("epubcheck could not be run:")

    def test_the_tool_is_invoked_on_the_archive_with_no_shell(
        self, tmp_path, monkeypatch
    ):
        # A fixed executable and a list argument, so a filename containing
        # shell metacharacters is an argument rather than a command.
        archive_path = tmp_path / "Book; rm -rf x.epub"
        calls = self._installed(monkeypatch, _Completed(returncode=0))

        validate.run_epubcheck(archive_path)

        command, options = calls[0]
        assert command == ["/usr/bin/epubcheck", str(archive_path)]
        assert "shell" not in options

    def test_the_timeout_is_passed_through_and_failure_is_not_raised(
        self, tmp_path, monkeypatch
    ):
        # check=False because a non-zero status is the answer, not an error:
        # raising here would abort a run over thousands of books on one bad
        # archive.
        calls = self._installed(monkeypatch, _Completed(returncode=0))

        validate.run_epubcheck(tmp_path / "Book.epub", timeout=7)

        _command, options = calls[0]
        assert options["timeout"] == 7
        assert options["check"] is False
        assert options["capture_output"] is True
