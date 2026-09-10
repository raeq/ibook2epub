"""
The reference checker in ``epubconvert.collect.references``.

Each case is a small book written for the one rule it tests, and each finding
is named by the message ID epubcheck gives the same problem, so a change in what
the checker reports shows up here under the ID a reader would look up.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from zipfile import ZipFile

import pytest

from epubconvert.collect import references

XHTML = "application/xhtml+xml"
CSS = "text/css"
SVG = "image/svg+xml"
NCX = "application/x-dtbncx+xml"
JPEG = "image/jpeg"

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
              media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

NCX_DOCUMENT = (
    '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><navMap>'
    '<navPoint id="n1"><navLabel><text>One</text></navLabel>'
    '<content src="{}"/></navPoint></navMap></ncx>'
)


def page(body: str = "", head: str = "") -> str:
    """An XHTML content document, on one line unless the markup breaks it."""
    return (
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        f"<head><title>t</title>{head}</head><body>{body}</body></html>"
    )


def package(
    items: list[tuple[str, str, str]],
    spine: tuple[str, ...],
    *,
    version: str = "3.0",
    toc: str | None = None,
    extra: str = "",
    metadata: str = "",
) -> str:
    """A package document declaring *items* as (id, href, media type)."""
    manifest = "".join(
        f'<item id="{ident}" href="{href}" media-type="{media}"/>'
        for ident, href, media in items
    )
    itemrefs = "".join(f'<itemref idref="{ident}"/>' for ident in spine)
    toc_attribute = f' toc="{toc}"' if toc else ""
    return (
        '<?xml version="1.0"?>'
        f'<package xmlns="http://www.idpf.org/2007/opf" version="{version}" '
        'unique-identifier="bid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="bid">urn:isbn:9780553383041</dc:identifier>{metadata}'
        f"</metadata><manifest>{manifest}</manifest>"
        f"<spine{toc_attribute}>{itemrefs}</spine>{extra}</package>"
    )


def _media_type(name: str) -> str:
    extension = name.rsplit(".", 1)[-1]
    return {"css": CSS, "svg": SVG, "ncx": NCX, "jpg": JPEG}.get(extension, XHTML)


def write_book(
    tmp_path: Path,
    files: Mapping[str, str | bytes],
    *,
    items: list[tuple[str, str, str]] | None = None,
    spine: tuple[str, ...] = ("c1",),
    opf: str | None = None,
    name: str = "Book.epub",
) -> Path:
    """
    Write a book whose files live under OEBPS/.

    Unless *items* says otherwise every file is declared, in order, as c1, c2
    and so on, and the spine holds c1 alone.
    """
    if items is None:
        items = [
            (f"c{number}", member, _media_type(member))
            for number, member in enumerate(files, start=1)
        ]
    path = tmp_path / name
    with ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf or package(items, spine))
        for member, body in files.items():
            archive.writestr(f"OEBPS/{member}", body)
    return path


def findings(path: Path) -> list[tuple[str, str, int | None]]:
    return [(f.code, f.path, f.line) for f in references.check_file(path).findings]


def codes(path: Path) -> list[str]:
    return [f.code for f in references.check_file(path).findings]


def missing(path: Path) -> list[str]:
    """The files RSC-007 names, in the order the checker met them."""
    return [
        f.message.split('"')[1]
        for f in references.check_file(path).findings
        if f.code == "RSC-007"
    ]


class TestASoundBook:
    def test_links_ids_and_stylesheets_that_resolve_give_no_finding(self, tmp_path):
        book = write_book(
            tmp_path,
            {
                "c1.xhtml": page(
                    '<p id="top"><a href="c2.xhtml#end">on</a> <a href="#top">up</a>'
                    '<img src="images/cover.jpg" alt=""/></p>',
                    head='<link rel="stylesheet" href="style.css"/>',
                ),
                "c2.xhtml": page('<p id="end">end</p>'),
                "style.css": "p { background: url(images/cover.jpg) }",
                "images/cover.jpg": b"\xff\xd8",
            },
            spine=("c1", "c2"),
        )

        report = references.check_file(book)

        assert report.findings == []
        # Four manifest items, four links in the chapter, one in the stylesheet.
        assert report.references == 9


class TestMissingAndUndeclaredFiles:
    def test_a_link_to_a_missing_file_is_rsc_007(self, tmp_path):
        book = write_book(tmp_path, {"c1.xhtml": page('<a href="gone.xhtml">x</a>')})

        assert findings(book) == [("RSC-007", "OEBPS/c1.xhtml", 1)]

    def test_a_link_to_a_declared_file_that_is_missing_is_rsc_007_too(self, tmp_path):
        items = [("c1", "c1.xhtml", XHTML), ("c2", "gone.xhtml", XHTML)]
        book = write_book(
            tmp_path, {"c1.xhtml": page('<a href="gone.xhtml">x</a>')}, items=items
        )

        assert codes(book) == ["RSC-001", "RSC-007"]

    def test_an_undeclared_file_is_rsc_008_once_however_often_it_is_used(
        self, tmp_path
    ):
        body = '<img src="a.jpg" alt=""/><img src="a.jpg" alt=""/>'
        book = write_book(
            tmp_path,
            {"c1.xhtml": page(body), "a.jpg": b"x"},
            items=[("c1", "c1.xhtml", XHTML)],
        )

        assert codes(book) == ["RSC-008"]

    def test_a_font_a_stylesheet_uses_is_checked_at_the_stylesheet_line(self, tmp_path):
        css = "p { color: red }\n@font-face { src: url(fonts/gone.otf) }\n"
        book = write_book(tmp_path, {"c1.xhtml": page(), "style.css": css})

        assert findings(book) == [("RSC-007", "OEBPS/style.css", 2)]

    def test_style_elements_style_attributes_and_imports_are_all_read(self, tmp_path):
        head = "<style>\n\np { background: url('b.png') }</style>"
        body = '<p style="background: url(a.png)">x</p>'
        css = '@import "c.css";\n/* url(commented.png) */ p { mask: url("") }\n'
        book = write_book(tmp_path, {"c1.xhtml": page(body, head), "style.css": css})

        assert missing(book) == ["OEBPS/b.png", "OEBPS/a.png", "OEBPS/c.css"]

    def test_a_css_url_naming_only_a_fragment_is_not_a_file(self, tmp_path):
        css = "p { filter: url(#shadow) }"
        book = write_book(tmp_path, {"c1.xhtml": page(), "style.css": css})

        assert codes(book) == []


class TestFragments:
    def test_a_fragment_that_names_no_id_is_rsc_012(self, tmp_path):
        book = write_book(
            tmp_path,
            {
                "c1.xhtml": page('<a href="c2.xhtml#nowhere">x</a>'),
                "c2.xhtml": page('<p id="here">x</p>'),
            },
            spine=("c1", "c2"),
        )

        assert findings(book) == [("RSC-012", "OEBPS/c1.xhtml", 1)]

    def test_a_percent_encoded_fragment_matches_its_decoded_id(self, tmp_path):
        # From a real book: #Vi%C3%A8le names id="Vièle".
        body = '<a href="#Vi%C3%A8le">x</a><p id="Vièle">x</p>'
        book = write_book(tmp_path, {"c1.xhtml": page(body)})

        assert codes(book) == []

    @pytest.mark.parametrize(
        "fragment", ["epubcfi(/6/4!/4/2)", "t=10", "xywh=0,0,10,10", ":~:text=end"]
    )
    def test_fragments_that_are_not_ids_are_not_checked(self, tmp_path, fragment):
        book = write_book(tmp_path, {"c1.xhtml": page(f'<a href="#{fragment}">x</a>')})

        assert codes(book) == []

    def test_a_fragment_into_a_document_outside_the_spine_is_not_checked(
        self, tmp_path
    ):
        book = write_book(
            tmp_path,
            {"c1.xhtml": page('<a href="c2.xhtml#nowhere">x</a>'), "c2.xhtml": page()},
        )

        assert codes(book) == []

    def test_a_fragment_into_a_spine_item_that_is_not_markup_is_not_checked(
        self, tmp_path
    ):
        book = write_book(
            tmp_path,
            {"c1.xhtml": page('<a href="pic.jpg#x">x</a>'), "pic.jpg": b"x"},
            spine=("c1", "c2"),
        )

        assert codes(book) == []

    def test_ids_before_a_well_formedness_error_still_count(self, tmp_path):
        # As in epubcheck: a document that breaks keeps what came before it.
        body = '<p id="kept">x</p><a href="#kept">x</a><a href="#lost">y</a>\x00'
        book = write_book(tmp_path, {"c1.xhtml": page(body + '<p id="lost">z</p>')})

        assert codes(book) == ["RSC-016", "RSC-012"]


class TestTheContainer:
    @pytest.mark.parametrize("href", ["/", "../../outside.xhtml", "/OEBPS/c1.xhtml"])
    def test_a_link_that_leaves_the_container_is_rsc_026(self, tmp_path, href):
        book = write_book(tmp_path, {"c1.xhtml": page(f'<a href="{href}">x</a>')})

        assert codes(book) == ["RSC-026"]

    def test_a_query_is_rsc_033(self, tmp_path):
        book = write_book(tmp_path, {"c1.xhtml": page('<a href="c1.xhtml?p=2">x</a>')})

        assert codes(book) == ["RSC-033"]

    def test_a_file_url_is_rsc_030(self, tmp_path):
        body = '<img src="file:///etc/hosts" alt=""/>'
        book = write_book(tmp_path, {"c1.xhtml": page(body)})

        assert codes(book) == ["RSC-030"]

    def test_a_file_url_that_is_not_valid_is_rsc_020_as_well(self, tmp_path):
        # From a real book: 33 links left pointing at the author's Windows drive.
        body = '<a href="file:///E|/OReilly/appd_05.html">x</a>'
        book = write_book(tmp_path, {"c1.xhtml": page(body)})

        assert codes(book) == ["RSC-020", "RSC-030"]

    def test_a_url_on_a_test_root_host_is_an_outside_url(self, tmp_path):
        body = '<a href="https://a.example.org/A/gone.xhtml">x</a>'
        book = write_book(tmp_path, {"c1.xhtml": page(body)})

        assert codes(book) == []


class TestUrlStrings:
    @pytest.mark.parametrize(
        "src", ["kindle:embed:0002?mime=image/jpg", "data:image/png;base64,AAAA"]
    )
    def test_a_url_of_another_scheme_is_not_a_file(self, tmp_path, src):
        # epubveri read kindle:embed:0002?mime=image/jpg as a missing file in
        # 137 books on the shelf.
        book = write_book(tmp_path, {"c1.xhtml": page(f'<img src="{src}" alt=""/>')})

        assert codes(book) == []

    @pytest.mark.parametrize("href", [" c1.xhtml\t", "", ".", "  "])
    def test_whitespace_empty_and_dot_links_are_allowed(self, tmp_path, href):
        book = write_book(tmp_path, {"c1.xhtml": page(f'<a href="{href}">x</a>')})

        assert codes(book) == []

    def test_an_invalid_url_string_is_rsc_020_and_still_resolved(self, tmp_path):
        book = write_book(
            tmp_path,
            {"c1.xhtml": page('<a href="c1 x.xhtml">x</a>'), "c1 x.xhtml": page()},
            items=[("c1", "c1.xhtml", XHTML), ("c2", "c1%20x.xhtml", XHTML)],
        )

        assert codes(book) == ["RSC-020"]

    def test_a_url_that_fails_to_parse_is_rsc_020_alone(self, tmp_path):
        body = '<a href="http://exa mple.org/">x</a>'
        book = write_book(tmp_path, {"c1.xhtml": page(body)})

        assert codes(book) == ["RSC-020"]

    def test_an_email_address_written_as_a_web_link_is_rsc_020(self, tmp_path):
        # 22 links on the shelf, all passed by epubcheck: the URL Standard does
        # not allow credentials in a valid URL string.
        body = '<a href="http://orders@example.org">x</a>'
        book = write_book(tmp_path, {"c1.xhtml": page(body)})

        report = references.check_file(book)

        assert [f.code for f in report.findings] == ["RSC-020"]
        assert "invalid-credentials" in report.findings[0].message

    @pytest.mark.parametrize(
        ("href", "expected"),
        [
            ("foo:bar", ["HTM-025"]),
            ("news:comp.lang.python", []),
            ("mailto:a@example.org", []),
        ],
    )
    def test_a_link_with_an_unregistered_scheme_is_htm_025(
        self, tmp_path, href, expected
    ):
        book = write_book(tmp_path, {"c1.xhtml": page(f'<a href="{href}">x</a>')})

        assert codes(book) == expected


class TestWhatCountsAsAReference:
    def test_epub_3_elements_are_read_in_epub_3_only(self, tmp_path):
        body = (
            '<video src="v.mp4" poster="p.jpg"/>'
            '<img srcset="s1.jpg 1x, , s2.jpg 2x" src="s.jpg" alt=""/>'
            '<blockquote cite="q.xhtml">x</blockquote>'
        )
        files = {"c1.xhtml": page(body)}
        items = [("c1", "c1.xhtml", XHTML)]
        three = write_book(tmp_path, files, name="Three.epub")
        two = write_book(
            tmp_path,
            files,
            opf=package(items, ("c1",), version="2.0"),
            name="Two.epub",
        )

        assert missing(three) == [
            "OEBPS/v.mp4",
            "OEBPS/p.jpg",
            "OEBPS/s1.jpg",
            "OEBPS/s2.jpg",
            "OEBPS/s.jpg",
            "OEBPS/q.xhtml",
        ]
        assert missing(two) == ["OEBPS/s.jpg"]

    def test_a_link_element_counts_only_as_a_stylesheet(self, tmp_path):
        head = (
            '<link rel="stylesheet" href="gone.css"/>'
            '<link rel="alternate" href="gone.xml"/>'
        )
        book = write_book(tmp_path, {"c1.xhtml": page(head=head)})

        assert missing(book) == ["OEBPS/gone.css"]

    def test_svg_is_read_through_xlink_href_only(self, tmp_path):
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink">'
            '<image xlink:href="gone.jpg"/>'
            '<a xlink:href="#shape"><rect id="shape"/></a>'
            '<linearGradient xlink:href="#g"/><use href="alsogone.svg"/></svg>'
        )
        book = write_book(
            tmp_path, {"c1.xhtml": page(), "art.svg": svg}, spine=("c1", "c2")
        )

        assert missing(book) == ["OEBPS/gone.jpg"]

    def test_mathml_altimg_is_read(self, tmp_path):
        body = (
            '<math xmlns="http://www.w3.org/1998/Math/MathML" altimg="eq.png">'
            "<mi>x</mi></math>"
        )
        book = write_book(tmp_path, {"c1.xhtml": page(body)})

        assert missing(book) == ["OEBPS/eq.png"]


class TestThePackageDocument:
    def test_a_missing_manifest_file_is_rsc_001_once_per_file(self, tmp_path):
        items = [
            ("c1", "c1.xhtml", XHTML),
            ("a", "gone.xhtml#one", XHTML),
            ("b", "gone.xhtml#two", XHTML),
        ]
        book = write_book(tmp_path, {"c1.xhtml": page()}, items=items)

        assert codes(book) == ["RSC-001"]

    def test_manifest_items_outside_the_book_are_not_files(self, tmp_path):
        items = [
            ("c1", "c1.xhtml", XHTML),
            ("remote", "https://example.org/remote.mp3", "audio/mpeg"),
            ("broken", "http://exa mple.org/x", "audio/mpeg"),
            ("leak", "../../x.xhtml", XHTML),
        ]
        book = write_book(tmp_path, {"c1.xhtml": page()}, items=items)

        assert codes(book) == ["RSC-020", "RSC-026"]

    def test_a_guide_reference_has_its_fragment_checked(self, tmp_path):
        items = [("c1", "c1.xhtml", XHTML)]
        guide = '<guide><reference type="toc" href="c1.xhtml#contents"/></guide>'
        opf = package(items, ("c1",), extra=guide)
        book = write_book(tmp_path, {"c1.xhtml": page('<p id="toc">x</p>')}, opf=opf)

        assert findings(book) == [("RSC-012", "OEBPS/content.opf", 1)]

    def test_only_the_ncx_the_spine_names_is_read(self, tmp_path):
        files = {
            "c1.xhtml": page(),
            "toc.ncx": NCX_DOCUMENT.format("gone.xhtml"),
            "old.ncx": NCX_DOCUMENT.format("alsogone.xhtml"),
        }
        items = [
            ("c1", "c1.xhtml", XHTML),
            ("ncx", "toc.ncx", NCX),
            ("old", "old.ncx", NCX),
        ]
        opf = package(items, ("c1",), version="2.0", toc="ncx")
        book = write_book(tmp_path, files, opf=opf)

        assert missing(book) == ["OEBPS/gone.xhtml"]

    def test_a_metadata_link_to_a_missing_file_warns_in_epub_3_only(self, tmp_path):
        items = [("c1", "c1.xhtml", XHTML)]
        link = '<link rel="record" href="record.xml"/>'
        files = {"c1.xhtml": page()}
        three = write_book(
            tmp_path,
            files,
            opf=package(items, ("c1",), metadata=link),
            name="Three.epub",
        )
        two = write_book(
            tmp_path,
            files,
            opf=package(items, ("c1",), version="2.0", metadata=link),
            name="Two.epub",
        )

        report = references.check_file(three)

        assert [(f.code, f.severity) for f in report.findings] == [
            ("RSC-007w", "warning")
        ]
        assert codes(two) == []


class TestItNeverRaises:
    def test_a_file_that_is_not_a_zip_is_one_finding(self, tmp_path):
        path = tmp_path / "Broken.epub"
        path.write_bytes(b"not a zip")

        report = references.check_file(path)

        assert [(f.code, f.severity) for f in report.findings] == [("PKG-008", "fatal")]

    def test_a_book_without_a_container_is_one_finding(self, tmp_path):
        path = tmp_path / "Bare.epub"
        with ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")

        assert codes(path) == ["RSC-001"]

    def test_a_container_naming_a_missing_package_is_one_finding(self, tmp_path):
        path = tmp_path / "Hollow.epub"
        with ZipFile(path, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", CONTAINER)

        assert codes(path) == ["RSC-001"]

    def test_a_package_too_large_to_check_is_one_finding(self, tmp_path, monkeypatch):
        monkeypatch.setattr(references, "MAX_XML_BYTES", 10)
        book = write_book(tmp_path, {"c1.xhtml": page()})

        assert findings(book) == [("PKG-008", "OEBPS/content.opf", None)]

    def test_a_document_too_large_to_check_is_reported_and_skipped(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(references, "MAX_XML_BYTES", 2000)
        book = write_book(tmp_path, {"c1.xhtml": page("x" * 3000)})

        assert findings(book) == [("PKG-008", "OEBPS/c1.xhtml", None)]

    def test_a_member_that_fails_its_checksum_is_reported_and_skipped(self, tmp_path):
        book = write_book(tmp_path, {"c1.xhtml": page("original words")})
        book.write_bytes(
            book.read_bytes().replace(b"original words", b"damaged words!")
        )

        assert findings(book) == [("PKG-008", "OEBPS/c1.xhtml", None)]

    def test_a_document_that_is_not_well_formed_is_rsc_016_at_its_line(self, tmp_path):
        book = write_book(tmp_path, {"c1.xhtml": "<html>\n<body>\n<p></body></html>"})

        assert findings(book) == [("RSC-016", "OEBPS/c1.xhtml", 3)]
