"""Tests for naming policies and identity-based deduplication."""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from epubconvert import convert, naming
from tests.conftest import make_package

disarm = pytest.importorskip("disarm", reason="portable naming needs the disarm extra")

# Characters that are legal on APFS/ext4 but illegal on Windows and exFAT.
WINDOWS_ILLEGAL = set(naming.WINDOWS_ILLEGAL)


def export(packages: Sequence[Path], output_dir: Path, **kwargs: Any) -> convert.Report:
    """Run the async exporter from a synchronous test."""
    return asyncio.run(convert.export_packages(packages, output_dir, **kwargs))


class TestPassthroughNaming:
    def test_filename_is_unchanged(self):
        policy = naming.PassthroughNaming()

        assert policy.filename("Sapiens: A Brief History.epub") == (
            "Sapiens: A Brief History.epub"
        )

    def test_identity_is_the_filename(self):
        policy = naming.PassthroughNaming()

        assert policy.identity("Dune.epub") == "Dune.epub"

    def test_case_variants_are_distinct(self):
        policy = naming.PassthroughNaming()

        assert policy.identity("Dune.epub") != policy.identity("DUNE.epub")

    def test_satisfies_the_protocol(self):
        assert isinstance(naming.PassthroughNaming(), naming.NamingPolicy)


class TestPortableNaming:
    def test_strips_windows_illegal_characters(self):
        policy = naming.PortableNaming()

        for title in [
            "Sapiens: A Brief History.epub",
            "War and Peace (Vol. 1/2).epub",
            "Où sont les enfants ?.epub",
        ]:
            assert not WINDOWS_ILLEGAL & set(policy.filename(title))

    def test_preserves_spaces(self):
        # separator=" " — spaces are legal everywhere, so underscoring them
        # would be gratuitous churn against existing exports.
        policy = naming.PortableNaming()

        assert policy.filename("The Hobbit.epub") == "The Hobbit.epub"

    def test_keeps_the_epub_extension(self):
        policy = naming.PortableNaming()

        assert policy.filename("Sapiens: A Brief History.epub").endswith(".epub")

    def test_identity_ignores_case(self):
        policy = naming.PortableNaming()

        assert policy.identity("The Hobbit.epub") == policy.identity("THE HOBBIT.epub")

    def test_identity_ignores_accents(self):
        policy = naming.PortableNaming()

        assert policy.identity(policy.filename("Düne.epub")) == policy.identity(
            policy.filename("Dune.epub")
        )

    def test_identity_survives_a_round_trip_through_the_filesystem(self):
        # The whole no-state-file design rests on this: the identity of an
        # already-exported book must be recomputable from its filename alone.
        policy = naming.PortableNaming()
        title = "Gödel, Escher, Bach: An Eternal Golden Braid.epub"

        on_disk = policy.filename(title)

        assert policy.identity(on_disk) == policy.identity(policy.filename(title))

    def test_distinct_books_keep_distinct_identities(self):
        policy = naming.PortableNaming()

        assert policy.identity(policy.filename("Dune.epub")) != policy.identity(
            policy.filename("The Hobbit.epub")
        )

    def test_satisfies_the_protocol(self):
        assert isinstance(naming.PortableNaming(), naming.NamingPolicy)

    def test_build_policy_selects_by_mode(self):
        assert isinstance(naming.build_policy(None), naming.PassthroughNaming)
        assert isinstance(naming.build_policy(naming.STRIP), naming.StripNaming)
        assert isinstance(naming.build_policy(naming.ROMANIZE), naming.PortableNaming)


class TestPortableExport:
    def test_output_names_are_portable(self, tmp_path, output_dir):
        source = tmp_path / "lib"
        make_package(source, "Sapiens: A Brief History.epub")

        export(
            convert.collect_package_dirs(source),
            output_dir,
            naming=naming.PortableNaming(),
        )

        written = [p.name for p in output_dir.glob("*.epub")]
        assert written == ["Sapiens A Brief History.epub"]
        assert not WINDOWS_ILLEGAL & set(written[0])

    def test_case_variants_export_once(self, tmp_path, output_dir):
        source = tmp_path / "lib"
        make_package(source / "a", "The Hobbit.epub")
        make_package(source / "b", "THE HOBBIT.epub")

        report = export(
            convert.collect_package_dirs(source),
            output_dir,
            naming=naming.PortableNaming(),
        )

        assert report.exported == 1
        assert report.collisions == 1
        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_rerun_skips_via_recomputed_identity(self, tmp_path, output_dir):
        source = tmp_path / "lib"
        make_package(source, "Sapiens: A Brief History.epub")
        packages = convert.collect_package_dirs(source)
        policy = naming.PortableNaming()
        export(packages, output_dir, naming=policy)

        report = export(packages, output_dir, naming=policy)

        assert report.exported == 0
        assert report.skipped == 1
        assert len(list(output_dir.glob("*.epub"))) == 1

    def test_collisions_are_reported_not_silent(self, tmp_path, output_dir):
        # ':' and '?' both become the separator, so these two distinct books
        # want the same output name.
        source = tmp_path / "lib"
        make_package(source / "a", "Vol 1:2.epub")
        make_package(source / "b", "Vol 1?2.epub")

        report = export(
            convert.collect_package_dirs(source),
            output_dir,
            naming=naming.PortableNaming(),
        )

        assert report.collisions == 1
        assert "collision" in convert.format_summary(report, output_dir, dry_run=False)


class TestPortableCli:
    def test_flag_produces_portable_names(self, tmp_path, output_dir):
        source = tmp_path / "lib"
        make_package(source, "Sapiens: A Brief History.epub")

        code = convert.main(
            [
                "-s",
                str(source),
                "-o",
                str(output_dir),
                "-m",
                "0",
                "-p",
                "romanize",
                "-q",
            ]
        )

        assert code == 0
        assert [p.name for p in output_dir.glob("*.epub")] == [
            "Sapiens A Brief History.epub"
        ]

    def test_without_the_flag_names_are_untouched(self, tmp_path, output_dir):
        source = tmp_path / "lib"
        make_package(source, "Sapiens: A Brief History.epub")

        convert.main(["-s", str(source), "-o", str(output_dir), "-m", "0", "-q"])

        assert [p.name for p in output_dir.glob("*.epub")] == [
            "Sapiens: A Brief History.epub"
        ]

    def test_long_and_short_flags_agree(self, tmp_path):
        source = tmp_path / "lib"
        make_package(source, "Dune.epub")

        short = convert.parse_args(["-s", str(source), "-p"])
        long = convert.parse_args(["-s", str(source), "--portable-names"])
        explicit = convert.parse_args(["-s", str(source), "-p", "romanize"])

        # A bare -p selects the loss-free mode, which needs no extra package.
        assert short.portable_names == naming.STRIP
        assert long.portable_names == naming.STRIP
        assert explicit.portable_names == naming.ROMANIZE

    def test_help_warns_about_romanization(self):
        help_text = convert.build_parser().format_help()

        assert "-p" in help_text
        assert "omaniz" in help_text  # "romanizes"/"Romanizes"


class TestMissingExtra:
    def test_build_policy_raises_a_actionable_error(self, monkeypatch):
        monkeypatch.setattr(naming, "disarm", None)

        with pytest.raises(naming.PortableNamesUnavailableError) as excinfo:
            naming.build_policy(naming.ROMANIZE)

        assert "pip install" in str(excinfo.value)

    def test_passthrough_still_works_without_disarm(self, monkeypatch):
        monkeypatch.setattr(naming, "disarm", None)

        assert naming.build_policy(None).filename("Dune.epub") == "Dune.epub"

    def test_cli_exits_2_when_the_extra_is_missing(self, tmp_path, output_dir):
        monkeypatch = pytest.MonkeyPatch()
        source = tmp_path / "lib"
        make_package(source, "Dune.epub")
        monkeypatch.setattr(naming, "disarm", None)
        try:
            code = convert.main(
                ["-s", str(source), "-o", str(output_dir), "-p", "romanize", "-q"]
            )
        finally:
            monkeypatch.undo()

        assert code == 2
        assert list(output_dir.iterdir()) == []


class TestStripUnsafe:
    @pytest.mark.parametrize(
        "name",
        [
            "Sapiens: A Brief History.epub",
            "War and Peace (Vol. 1/2).epub",
            "Où sont les enfants ?.epub",
            'A "Quoted" Title.epub',
            "Pipe|Star*.epub",
        ],
    )
    def test_illegal_characters_are_removed(self, name):
        assert not WINDOWS_ILLEGAL & set(naming.strip_unsafe(name))

    def test_script_is_preserved(self):
        # The whole point of strip mode: every target filesystem stores these
        # correctly, so romanizing them would be gratuitous loss.
        assert naming.strip_unsafe("こころ.epub") == "こころ.epub"
        assert naming.strip_unsafe("Хождение.epub") == "Хождение.epub"
        assert naming.strip_unsafe("L'Étranger.epub") == "L'Étranger.epub"

    def test_clean_names_are_untouched(self):
        assert naming.strip_unsafe("The Hobbit.epub") == "The Hobbit.epub"

    def test_whitespace_runs_collapse(self):
        assert naming.strip_unsafe("A   B.epub") == "A B.epub"

    def test_reserved_device_names_are_escaped(self):
        assert naming.strip_unsafe("CON.epub").startswith("_")
        assert naming.strip_unsafe("LPT1.epub").startswith("_")
        assert naming.strip_unsafe("NUL").startswith("_")

    def test_trailing_dot_and_space_are_dropped(self):
        assert naming.strip_unsafe("Trailing .") == "Trailing"

    def test_result_fits_the_byte_budget(self):
        long_name = "Ä" * 400 + ".epub"

        result = naming.strip_unsafe(long_name)

        assert len(result.encode()) <= naming.MAX_FILENAME_BYTES
        assert result.endswith(".epub")

    def test_truncation_does_not_split_a_character(self):
        result = naming.strip_unsafe("é" * 400 + ".epub")

        result.encode().decode()  # would raise if a sequence were split
        assert len(result.encode()) <= naming.MAX_FILENAME_BYTES

    def test_empty_input_yields_a_usable_name(self):
        assert naming.strip_unsafe("") == "_"
        assert naming.strip_unsafe("///") != ""

    def test_is_idempotent(self):
        for name in ["Sapiens: A Brief.epub", "CON.epub", "Ä" * 400 + ".epub"]:
            once = naming.strip_unsafe(name)
            assert naming.strip_unsafe(once) == once


class TestStripNaming:
    def test_identity_folds_case(self):
        policy = naming.StripNaming()

        assert policy.identity("The Hobbit.epub") == policy.identity("THE HOBBIT.epub")

    def test_identity_round_trips_from_disk(self):
        policy = naming.StripNaming()
        on_disk = policy.filename("Sapiens: A Brief History.epub")

        assert policy.identity(on_disk) == policy.identity(
            policy.filename("Sapiens: A Brief History.epub")
        )

    def test_satisfies_the_protocol(self):
        assert isinstance(naming.StripNaming(), naming.NamingPolicy)

    def test_needs_no_disarm(self, monkeypatch):
        monkeypatch.setattr(naming, "disarm", None)

        policy = naming.build_policy(naming.STRIP)

        assert policy.filename("A: B.epub") == "A B.epub"

    def test_end_to_end_keeps_the_script(self, tmp_path, output_dir):
        source = tmp_path / "lib"
        make_package(source, "こころ: Kokoro.epub")

        convert.main(["-s", str(source), "-o", str(output_dir), "-m", "0", "-p", "-q"])

        names = [p.name for p in output_dir.glob("*.epub")]
        assert names == ["こころ Kokoro.epub"]
