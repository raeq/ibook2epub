"""
Shared fixtures: synthetic iBooks libraries on disk.

Which file covers what. The split is by behaviour rather than one file per
module, so this map saves a search:

===========================  ==================================================
``test_convert.py``          discovery and exclusion (``archive``), the export
                             cap, the partial sweep, the summary line
``test_export.py``           archive writing (``archive``), determinism,
                             interrupts, the disk floor, covers
                             (``inspect_output.extract_cover``)
``test_validate.py``         ``validate``, and ``--verify``
                             (``inspect_output.verify_output``)
``test_planning.py``         ``planning``: collisions, refresh, the listings
``test_naming.py``           ``naming``, and identity round trips
``test_cli.py``              ``cli`` parsing, ``run.main``, ``app_logger``
                             levels and ``defaults`` values
``test_options.py``          individual flags, the run lock, remaining counts
``test_source.py``           ``source``: DRM and iCloud stub detection
===========================  ==================================================

``test_integrity.py``        the guarantees that keep the output directory
                             trustworthy
``test_hardening.py``        untrusted input and hostile filesystems
``test_efficiency.py``       work done per book, and work declined
``test_containment.py``      the one path-trust rule (``contained``)
``test_rules.py``            one class per rule, one test per call site
``test_packaging.py``        what ships, and the version it reports
``test_experience.py``       what the tool tells the person running it
``test_exit_codes.py``       the exit codes, as a contract for scripts
``test_shelf.py``            orphans: what is on the shelf and not in the library
``test_copy_through.py``     books taken along without converting
===========================  ==================================================

``spec`` is exercised through the modules that use it rather than directly.
"""

import os
from pathlib import Path

import pytest

#: Root ignores permission bits, so a test that revokes them asserts nothing.
#: hasattr guards Windows, which has no geteuid at all.
needs_permissions = pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root ignores permission bits, so this test cannot fail",
)

# Files every synthetic package gets. The bogus ``mimetype`` and the Apple
# bookkeeping files are the ones the converter is expected to drop.
PACKAGE_FILES = {
    "mimetype": "this should be replaced",
    "iTunesMetadata.plist": "<plist/>",
    "bookmarks.plist": "<plist/>",
    "META-INF/container.xml": "<container/>",
    "OEBPS/content.opf": "<package/>",
    "OEBPS/text/chapter1.xhtml": "<html><body>Chapter one</body></html>",
}

# Members that should survive into the exported archive, mimetype aside.
EXPECTED_MEMBERS = {
    "META-INF/container.xml",
    "OEBPS/content.opf",
    "OEBPS/text/chapter1.xhtml",
}


def make_package(parent: Path, name: str) -> Path:
    """Create one fake ``*.epub/`` package directory and return its path."""
    package = parent / name
    for relative, content in PACKAGE_FILES.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return package


CONTAINER = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles>'
    "</container>"
)


def make_metadata_package(
    parent: Path,
    name: str,
    *,
    title: str,
    creator: str | None = None,
    file_as: str | None = None,
    identifier: str = "urn:uuid:1",
) -> Path:
    """Create a package directory whose OPF carries real Dublin Core."""
    attribute = f' opf:file-as="{file_as}"' if file_as else ""
    person = creator or file_as
    creator_element = f"<dc:creator{attribute}>{person}</dc:creator>" if person else ""
    opf = (
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"'
        ' unique-identifier="bid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:opf="http://www.idpf.org/2007/opf">'
        f"<dc:title>{title}</dc:title>{creator_element}"
        f'<dc:identifier id="bid">{identifier}</dc:identifier>'
        "</metadata>"
        '<manifest><item id="ch1" href="text/chapter1.xhtml"'
        ' media-type="application/xhtml+xml"/></manifest>'
        '<spine><itemref idref="ch1"/></spine></package>'
    )
    layout = {
        "mimetype": "application/epub+zip",
        "META-INF/container.xml": CONTAINER,
        "OEBPS/content.opf": opf,
        "OEBPS/text/chapter1.xhtml": "<html><body>Ged</body></html>",
    }
    package = parent / name
    for relative, body in layout.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return package


@pytest.fixture(name="library")
def library_fixture(tmp_path: Path) -> Path:
    """
    A source directory holding a top-level package, a nested package, and a
    plain directory that must not be mistaken for either.
    """
    source = tmp_path / "library"
    source.mkdir()

    make_package(source, "Book One.epub")
    make_package(source / "Nested" / "Deep", "Book Two.epub")
    (source / "NotABook").mkdir()
    (source / "NotABook" / "readme.txt").write_text("nope", encoding="utf-8")

    return source


@pytest.fixture(name="output_dir")
def output_dir_fixture(tmp_path: Path) -> Path:
    """An existing, empty output directory."""
    out = tmp_path / "out"
    out.mkdir()
    return out


def remove_tree(path: Path) -> None:
    """Delete a directory and everything under it, deepest entries first."""
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        else:
            child.rmdir()
    path.rmdir()
