"""
Tests that text a book or Apple's database chose reaches the terminal escaped.

A manifest href, an item id, a member name and a rootfile path are all the
book's words, and ``--verify`` and ``--validate`` report them. ``_resolve``
percent-decodes an href, so ``%1B[2K%0D`` arrives as ``ESC[2K`` and a carriage
return: enough for a damaged book to erase the line reporting it and write
"sound" over it. An annotation's UUID is Apple's column, logged when its row
is skipped. Each goes through ``display.printable`` on its way out.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import logging
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import pytest

from epubconvert.collect import annotations, validate
from epubconvert.run import run
from tests.conftest import CONTAINER, make_metadata_package
from tests.test_annotations import highlight, make_databases

#: What a hostile book hides in its names: erase the line, return to its start.
ERASE = "\x1b[2K\r"

#: The same, as an href writes it: _resolve percent-decodes it back to ERASE.
ERASE_IN_HREF = "%1B[2K%0D"

#: The same again, as XML can carry it. XML 1.0 has no ESC, even as a
#: character reference, but it allows a CR and C1's CSI, which is ESC-[ in one.
ERASE_IN_XML = "&#x9B;2K&#13;"


def _opf(manifest: str, spine: str) -> str:
    return (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f"<metadata/><manifest>{manifest}</manifest><spine>{spine}</spine></package>"
    )


def _epub(
    path: Path, opf: str, container: str = CONTAINER, extra: str | None = None
) -> Path:
    with ZipFile(path, "w") as archive:
        archive.writestr(ZipInfo("mimetype"), "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", opf)
        if extra is not None:
            info = ZipInfo(extra)
            info.compress_type = ZIP_STORED
            archive.writestr(info, "<html/>")
    return path


def _assert_escaped(text: str) -> None:
    for control in ("\x1b", "\r", "\x9b"):
        assert control not in text, repr(text)


def _item(item_id: str, href: str) -> str:
    return f'<item id="{item_id}" href="{href}" media-type="application/xhtml+xml"/>'


class TestAProblemIsPrintable:
    def test_a_decoded_href_is_escaped(self, tmp_path):
        opf = _opf(_item("c", f"{ERASE_IN_HREF}All 3 books are sound.xhtml"), "")
        problems = validate.validate_archive(_epub(tmp_path / "a.epub", opf))

        assert any("\\x1b[2K\\x0dAll 3" in problem for problem in problems)
        _assert_escaped("\n".join(problems))

    def test_an_item_id_and_a_spine_idref_are_escaped(self, tmp_path):
        opf = _opf(
            _item(f"a{ERASE_IN_XML}b", "gone.xhtml"),
            f'<itemref idref="x{ERASE_IN_XML}y"/>',
        )
        problems = validate.validate_archive(_epub(tmp_path / "a.epub", opf))

        assert any("spine references unknown" in problem for problem in problems)
        assert any("manifest item is not in" in problem for problem in problems)
        _assert_escaped("\n".join(problems))

    def test_a_rootfile_path_is_escaped(self, tmp_path):
        container = CONTAINER.replace("OEBPS/content.opf", f"OEBPS/c{ERASE_IN_XML}.opf")
        problems = validate.validate_archive(
            _epub(tmp_path / "a.epub", _opf("", ""), container)
        )

        assert any("missing OEBPS/c" in problem for problem in problems)
        _assert_escaped("\n".join(problems))

    def test_a_rootfile_outside_the_package_is_escaped(self, tmp_path):
        container = CONTAINER.replace("OEBPS/content.opf", f"../{ERASE_IN_XML}x.opf")
        problems = validate.validate_archive(
            _epub(tmp_path / "a.epub", _opf("", ""), container)
        )

        assert any("outside the package" in problem for problem in problems)
        _assert_escaped("\n".join(problems))

    def test_a_corrupt_member_is_named_escaped(self, tmp_path):
        name = f"OEBPS/{ERASE}ch1.xhtml"
        path = _epub(
            tmp_path / "a.epub",
            _opf(_item("c", "ch1.xhtml"), '<itemref idref="c"/>'),
            extra=name,
        )
        raw = bytearray(path.read_bytes())
        at = raw.rindex(b"<html/>")
        raw[at] ^= 0xFF
        path.write_bytes(bytes(raw))

        problems = validate.validate_archive(path)

        assert any("corrupt member" in problem for problem in problems)
        _assert_escaped("\n".join(problems))


class TestTheCommandsPrintNoControlCharacters:
    # The logger does not propagate, so what reached the terminal is read
    # from the streams run.main configured its handler on.

    def test_verify_reports_a_hostile_href_escaped(self, tmp_path, output_dir, capsys):
        opf = _opf(_item("c", f"{ERASE_IN_HREF}All 3 books are sound.xhtml"), "")
        _epub(output_dir / "Hostile.epub", opf)
        (tmp_path / "lib").mkdir()

        code = run.main(
            ["-s", str(tmp_path / "lib"), "-o", str(output_dir), "--verify"]
        )

        captured = capsys.readouterr()
        assert code != 0
        assert "All 3 books are sound" in captured.err
        _assert_escaped(captured.out + captured.err)

    def test_verify_logs_a_hostile_archive_name_escaped(
        self, tmp_path, output_dir, capsys
    ):
        # Only the log line: the re-export hint on stdout is run.py's.
        _epub(output_dir / f"{ERASE}Hostile.epub", _opf("", ""))
        (tmp_path / "lib").mkdir()

        run.main(["-s", str(tmp_path / "lib"), "-o", str(output_dir), "--verify"])

        err = capsys.readouterr().err
        assert "Hostile.epub is damaged" in err
        _assert_escaped(err)

    def test_verify_advice_names_a_hostile_archive_escaped(
        self, tmp_path, output_dir, capsys
    ):
        # shlex.quote makes a name one shell word; it does nothing about ESC
        # and CR, which reached the terminal inside the repair advice.
        (output_dir / f"{ERASE}All fine.epub").write_bytes(b"not a zip")
        make_metadata_package(tmp_path / "lib", f"{ERASE}All fine.epub", title="x")

        run.main(["-s", str(tmp_path / "lib"), "-o", str(output_dir), "--verify"])

        out = capsys.readouterr().out
        assert "All fine" in out
        _assert_escaped(out)

    def test_validate_reports_a_hostile_href_escaped(
        self, tmp_path, output_dir, capsys
    ):
        package = make_metadata_package(tmp_path / "lib", "Book.epub", title="Book")
        opf = package / "OEBPS" / "content.opf"
        opf.write_text(
            opf.read_text(encoding="utf-8").replace(
                'href="text/chapter1.xhtml"', f'href="{ERASE_IN_HREF}sound.xhtml"'
            ),
            encoding="utf-8",
        )

        run.main(
            ["-s", str(tmp_path / "lib"), "-o", str(output_dir), "-m", "0"]
            + ["--validate"]
        )

        captured = capsys.readouterr()
        assert "sound.xhtml" in captured.err
        _assert_escaped(captured.out + captured.err)


class TestASkippedAnnotationIsNamedEscaped:
    @pytest.mark.parametrize("uuid", [f"{ERASE}all fine", b"\x1b[2K\rblob"])
    def test_its_id_is_escaped(self, tmp_path, caplog, uuid):
        # library.py already escaped the asset id of a row it skipped; the
        # annotation export logged the UUID as Apple stored it.
        make_databases(tmp_path, rows=[highlight(uuid=uuid, text=b"a blob")])

        with caplog.at_level(logging.WARNING):
            annotations.collect(tmp_path)

        assert "Skipped an unreadable annotation" in caplog.text
        _assert_escaped(caplog.text)
