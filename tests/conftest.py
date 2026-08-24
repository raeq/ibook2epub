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
``test_cli.py``              ``cli`` argument parsing and ``run.main``
``test_options.py``          individual flags, the run lock, remaining counts
``test_source.py``           ``source``: DRM and iCloud stub detection
===========================  ==================================================

``app_logger``, ``defaults`` and ``spec`` are exercised through the modules
that use them rather than directly.
"""

from pathlib import Path

import pytest

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
