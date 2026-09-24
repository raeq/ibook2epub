"""
Tests for a book numbered by ``--on-collision suffix`` once its namesake leaves.

A book with no digest to mark it -- named from the folder, or sharing its
identifier -- is numbered by its place in its group: ``Dune (2).epub``. When
the book holding the plain name, or the number before it, left the library
with its archive, the numbered book took the name it had given up and was
written again, and its own archive was listed as an orphan.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

import json
import shutil
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

from epubconvert.collect import package as package_reader
from epubconvert.run import run
from tests.conftest import make_metadata_package, make_package, remove_tree
from tests.test_copy_claims import SUFFIX, identifier_of, listing, shelf, zipped_book

AUTHOR_TITLE = ["--name-by", "author-title"]


def _argv(library: Path, output_dir: Path, *extra: str) -> list[str]:
    return ["-s", str(library), "-o", str(output_dir), "-m", "0", *SUFFIX, *extra]


class TestNamedFromTheFolder:
    @staticmethod
    def _left(tmp_path: Path, output_dir: Path) -> Path:
        library = tmp_path / "lib"
        for folder in ("a", "b"):
            make_package(library / folder, "Dune.epub")
        run.main([*_argv(library, output_dir), "-q"])
        assert shelf(output_dir) == ["Dune (2).epub", "Dune.epub"]
        remove_tree(library / "a")
        (output_dir / "Dune.epub").unlink()
        return library

    def test_the_numbered_book_keeps_its_file(self, tmp_path, output_dir, capsys):
        library = self._left(tmp_path, output_dir)
        capsys.readouterr()

        listed = listing(library, output_dir, capsys, *SUFFIX)
        run.main(_argv(library, output_dir))
        ran = capsys.readouterr()

        assert listed == [("Dune.epub", "exported")]
        assert "Exported 0 epub file(s)" in ran.out
        assert "orphan" not in ran.out
        assert shelf(output_dir) == ["Dune (2).epub"]

    def test_not_while_another_book_wants_the_plain_name(
        self, tmp_path, output_dir, capsys
    ):
        # Nothing but the name says whose the numbered file is, so it is
        # kept only by the one book that wants the name.
        library = self._left(tmp_path, output_dir)
        make_package(library / "c", "Dune.epub")
        capsys.readouterr()

        run.main([*_argv(library, output_dir), "-q"])

        assert shelf(output_dir) == ["Dune (2).epub", "Dune.epub"]

    def test_not_a_numbered_file_a_copy_keeps(self, tmp_path, output_dir, capsys):
        # A zipped book copied as "Dune (2)" while a package of its name was
        # left out of a run: the package, alone among the packages, kept the
        # copy's file and was reported exported from it.
        # formal/RerunPlanner.tla found it.
        library = tmp_path / "lib"
        zipped = zipped_book(tmp_path, library / "z" / "Dune.epub", "urn:uuid:Z")
        shutil.copy2(zipped, output_dir / "Dune (2).epub")
        make_metadata_package(
            library / "p", "Dune.epub", title="Dune", identifier="urn:uuid:P"
        )
        capsys.readouterr()

        listed = listing(library, output_dir, capsys, *SUFFIX)
        run.main([*_argv(library, output_dir), "-q"])

        assert sorted(listed) == [("Dune.epub", "copied"), ("Dune.epub", "pending")]
        assert identifier_of(output_dir / "Dune.epub") == "urn:uuid:P"
        assert identifier_of(output_dir / "Dune (2).epub") == "urn:uuid:Z"

    def test_nor_one_that_leaves_the_plain_name_to_a_copy(
        self, tmp_path, output_dir, capsys
    ):
        # Two zipped books and a package that -p strip names alike. A run
        # narrowed to the first copy put it at "Dune (2)"; one narrowed to
        # the package kept that numbered file, found it the copy's, and
        # moved on to "Dune (3)", past the plain name the second copy had
        # claimed. The next run found two numbered files, kept neither, and
        # listed the package's only archive as an orphan.
        # formal/RerunPlanner.tla found it.
        library = tmp_path / "lib"
        zipped_book(tmp_path, library / "Dune<.epub", "urn:uuid:A")
        zipped_book(tmp_path, library / "Dune>.epub", "urn:uuid:B")
        make_metadata_package(
            library / "p", "Dune.epub", title="Dune", identifier="urn:uuid:P"
        )
        argv = [*_argv(library, output_dir, "-p", "strip"), "-q", "--match"]
        run.main([*argv, "dune<"])
        run.main([*argv, "dune.epub"])
        capsys.readouterr()

        run.main([*argv[:-2], "--match", "dune<"])
        ran = capsys.readouterr()

        assert identifier_of(output_dir / "Dune.epub") == "urn:uuid:P"
        assert identifier_of(output_dir / "Dune (2).epub") == "urn:uuid:A"
        assert "orphan" not in ran.out

    def test_a_rerun_reads_only_for_the_numbered_name(
        self, tmp_path, output_dir, monkeypatch
    ):
        # Named from the folder, a book's identifier is read to tell which
        # numbered file is its own, and only for a name with numbered files
        # on the shelf: nothing is read for the rest of the library.
        library = tmp_path / "lib"
        for folder in ("a", "b"):
            make_metadata_package(
                library / folder, "Dune.epub", title="Dune", identifier=f"urn:{folder}"
            )
        for index in range(3):
            make_metadata_package(
                library, f"Other {index}.epub", title="Other", identifier=f"urn:{index}"
            )
        run.main([*_argv(library, output_dir), "-q"])
        opened: Counter[str] = Counter()
        original_zip = ZipFile.__init__
        original_read = package_reader.read_package_dir

        def zip_counting(self, file, *args, **kwargs):
            opened[Path(str(file)).name] += 1
            original_zip(self, file, *args, **kwargs)

        def read_counting(package: Path):
            opened[package.relative_to(library).as_posix()] += 1
            return original_read(package)

        monkeypatch.setattr(ZipFile, "__init__", zip_counting)
        for module in ("holders", "placing", "planning"):
            monkeypatch.setattr(
                f"epubconvert.run.{module}.read_package_dir", read_counting
            )

        run.main([*_argv(library, output_dir), "-q"])

        assert set(opened) <= {
            "a/Dune.epub",
            "b/Dune.epub",
            "Dune.epub",
            "Dune (2).epub",
        }
        assert set(opened.values()) <= {1}


class TestAPackageMovedOnPastACopy:
    def test_it_keeps_its_file_when_the_copy_leaves(self, tmp_path, output_dir, capsys):
        # Moved on to "Dune (2)" past a zipped book's file, it took the plain
        # name back once that book left the library: reported exported from
        # the other book's file, and its own listed as an orphan.
        library = tmp_path / "lib"
        zipped = zipped_book(tmp_path, library / "a" / "Dune.epub", "urn:uuid:Z")
        run.main([*_argv(library, output_dir), "-q"])
        make_metadata_package(
            library / "b", "Dune.epub", title="Dune", identifier="urn:uuid:P"
        )
        run.main([*_argv(library, output_dir), "-q"])
        assert identifier_of(output_dir / "Dune (2).epub") == "urn:uuid:P"
        zipped.unlink()
        capsys.readouterr()

        run.main(
            ["-s", str(library), "-o", str(output_dir), "--list", "--json"] + SUFFIX
        )
        rows = json.loads(capsys.readouterr().out)

        assert sorted((row["status"], Path(row["target"]).name) for row in rows) == [
            ("exported", "Dune (2).epub"),
            ("orphan", "Dune.epub"),
        ]


class TestSharingOneIdentifier:
    def test_the_last_keeps_its_file_when_the_middle_one_leaves(
        self, tmp_path, output_dir, capsys
    ):
        library = tmp_path / "lib"
        for folder in ("a", "b", "c"):
            make_metadata_package(
                library / folder,
                "Dune.epub",
                title="Dune",
                creator="Frank Herbert",
                identifier="urn:isbn:9780441013593",
            )
        argv = _argv(library, output_dir, *AUTHOR_TITLE)
        run.main([*argv, "-q"])
        [first, second, third] = sorted(output_dir.glob("*.epub"), key=len_then_name)
        remove_tree(library / "b")
        second.unlink()
        capsys.readouterr()

        run.main(argv)
        ran = capsys.readouterr()

        assert "Exported 0 epub file(s)" in ran.out
        assert "orphan" not in ran.out
        assert sorted(output_dir.glob("*.epub")) == sorted([first, third])


def len_then_name(path: Path) -> tuple[int, str]:
    """Order ``X.epub``, ``X (2).epub``, ``X (3).epub`` by their number."""
    return len(path.name), path.name
