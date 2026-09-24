"""
Every pointer into the source must still point at something.

The move into utils, collect, export and run left docstring references to
modules that no longer existed at those paths, a cross-reference to a helper
renamed long before, and every step of every CodeTour opening a file that was
not there. Nothing failed: a stale reference is only found by a reader who
follows it.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from __future__ import annotations

import importlib
import inspect
import json
import re
from pathlib import Path
from types import ModuleType

import pytest

#: Anchored on this file rather than the working directory: run from anywhere
#: else, a relative glob found nothing and every test here passed unchecked.
ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "epubconvert"
TOURS = ROOT / ".tours"


def _sources() -> list[Path]:
    """Every module of the package; an empty list is a fault, not a pass."""
    found = sorted(PACKAGE.rglob("*.py"))
    assert found, f"no modules under {PACKAGE}"
    return found


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(ROOT).with_suffix("").parts).removesuffix(
        ".__init__"
    )


def _walk(start: object, names: list[str]) -> bool:
    target = start
    for name in names:
        if not hasattr(target, name):
            return False
        target = getattr(target, name)
    return True


def _resolves_absolute(reference: str) -> bool | None:
    """Resolve a full dotted name; None when no prefix of it is a module."""
    parts = reference.split(".")
    for split in range(len(parts), 0, -1):
        try:
            module = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        return _walk(module, parts[split:])
    return None


class TestEveryModuleReferenceResolves:
    """A dotted ``epubconvert.…`` name in the package's source must import."""

    REFERENCE = re.compile(r"\bepubconvert(?:\.[A-Za-z_][A-Za-z0-9_]*)+")

    def test_every_reference_in_the_package_names_something_real(self):
        unresolved = [
            f"{path.relative_to(ROOT)}: {match}"
            for path in _sources()
            for match in sorted(set(self.REFERENCE.findall(path.read_text("utf-8"))))
            if not _resolves_absolute(match)
        ]

        assert unresolved == []


class TestEveryCrossReferenceResolves:
    """
    A Sphinx role such as ``:func:`file_mode``` must name something real.

    Short forms resolve the way a reader would look them up: against the
    module the docstring is in, then against each class it defines (a method
    documenting its sibling), then as a full dotted name. The dotted-name test
    above cannot see these, which is how a reference to ``_file_mode`` outlived
    the helper's rename.
    """

    ROLE = re.compile(
        r":(?:func|meth|class|data|attr|exc|mod|obj|const):`~?([^`<>]+?)(?:\(\))?`"
    )

    @staticmethod
    def _resolves(module: ModuleType, target: str) -> bool:
        if target.startswith("."):
            package = module.__name__
            if not hasattr(module, "__path__"):
                package = package.rpartition(".")[0]
            level = len(target) - len(target.lstrip("."))
            for _ in range(level - 1):
                package = package.rpartition(".")[0]
            target = f"{package}.{target.lstrip('.')}"
        parts = target.split(".")
        if _walk(module, parts):
            return True
        classes = [
            value
            for value in vars(module).values()
            if inspect.isclass(value) and value.__module__ == module.__name__
        ]
        if any(_walk(cls, parts) for cls in classes):
            return True
        return bool(_resolves_absolute(target))

    def test_every_role_in_the_package_names_something_real(self):
        unresolved = []
        for path in _sources():
            module = importlib.import_module(_module_name(path))
            text = path.read_text("utf-8")
            for match in self.ROLE.finditer(text):
                if not self._resolves(module, match.group(1).strip()):
                    line = text.count("\n", 0, match.start()) + 1
                    unresolved.append(
                        f"{path.relative_to(ROOT)}:{line}: {match.group(0)}"
                    )

        assert unresolved == []


def _tour_steps() -> list[tuple[str, dict[str, object]]]:
    """Every tour step that opens a file, labelled for an assertion message."""
    tours = sorted(TOURS.glob("*.tour"))
    assert tours, f"no tours under {TOURS}"
    return [
        (f"{tour.name} step {index}", step)
        for tour in tours
        for index, step in enumerate(json.loads(tour.read_text("utf-8"))["steps"])
        if "file" in step
    ]


def _text_as_checked_out(path: Path) -> str:
    # newline="" keeps a CRLF checkout's line endings, which CodeTour's
    # JavaScript regex sees and a pattern spanning lines would not match.
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


# MANIFEST.in prunes .tours, so a test run from an sdist has none to check.
@pytest.mark.skipif(not TOURS.is_dir(), reason="no .tours directory")
class TestEveryTourStepFindsItsCode:
    """
    A CodeTour step opens its file and finds its place by a regex.

    CodeTour reads a step's ``line`` first and consults ``pattern`` only when
    there is none, so a step carrying both is placed by a number that every
    edit above it moves. The steps carry a pattern alone, and each pattern must
    still match the file it names.
    """

    def test_every_step_opens_a_file_that_exists(self):
        missing = [
            f"{label}: {step['file']}"
            for label, step in _tour_steps()
            if not (ROOT / str(step["file"])).is_file()
        ]

        assert missing == []

    def test_every_step_is_placed_by_a_pattern_not_a_line_number(self):
        numbered = [
            label
            for label, step in _tour_steps()
            if "line" in step or "pattern" not in step
        ]

        assert numbered == []

    def test_every_pattern_still_finds_its_code(self):
        unmatched = [
            f"{label}: {step['pattern']!r}"
            for label, step in _tour_steps()
            if "pattern" in step
            and (ROOT / str(step["file"])).is_file()
            and not re.search(
                str(step["pattern"]),
                _text_as_checked_out(ROOT / str(step["file"])),
                re.MULTILINE,
            )
        ]

        assert unmatched == []

    def test_no_description_points_at_a_line_number(self):
        # Every "line 196" the tours were written with had drifted by the
        # time the package moved into layers. A name survives the edits that
        # move a line.
        cited = [
            f"{label}: {match}"
            for label, step in _tour_steps()
            for match in re.findall(r"\blines? \d+", str(step.get("description", "")))
        ]

        assert cited == []
