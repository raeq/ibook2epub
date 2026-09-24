"""
Every pointer into the source must still point at something.

The move into utils, collect, export and run left seventeen docstring
references to modules that no longer existed at those paths, and every step of
every CodeTour opening a file that was not there. Nothing failed: a stale
reference is only found by a reader who follows it.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

import importlib
import json
import re
from pathlib import Path

import pytest


class TestEveryModuleReferenceResolves:
    """A dotted ``epubconvert.…`` name in the package's source must import."""

    REFERENCE = re.compile(r"\bepubconvert(?:\.[A-Za-z_][A-Za-z0-9_]*)+")

    @staticmethod
    def _resolves(reference: str) -> bool:
        parts = reference.split(".")
        for split in range(len(parts), 0, -1):
            try:
                target = importlib.import_module(".".join(parts[:split]))
            except ImportError:
                continue
            for name in parts[split:]:
                if not hasattr(target, name):
                    return False
                target = getattr(target, name)
            return True
        return False

    def test_every_reference_in_the_package_names_something_real(self):
        unresolved = [
            f"{path}: {match}"
            for path in sorted(Path("epubconvert").rglob("*.py"))
            for match in sorted(set(self.REFERENCE.findall(path.read_text("utf-8"))))
            if not self._resolves(match)
        ]

        assert unresolved == []


TOURS = Path(".tours")


def _tour_steps() -> list[tuple[str, dict[str, object]]]:
    """Every tour step that opens a file, labelled for an assertion message."""
    return [
        (f"{tour.name} step {index}", step)
        for tour in sorted(TOURS.glob("*.tour"))
        for index, step in enumerate(json.loads(tour.read_text("utf-8"))["steps"])
        if "file" in step
    ]


# MANIFEST.in prunes .tours, so a test run from an sdist has none to check.
@pytest.mark.skipif(not TOURS.is_dir(), reason="no .tours directory")
class TestEveryTourStepFindsItsCode:
    """
    A CodeTour step opens its file and finds its place by a regex.

    A step whose file is gone opens nothing, and one whose pattern no longer
    matches falls back to its line number, which is wherever the code was when
    the step was written. The ``line`` fields themselves are not checked:
    CodeTour prefers the pattern, and pinning every line would fail this test
    on any edit above a step.
    """

    def test_every_step_opens_a_file_that_exists(self):
        missing = [
            f"{label}: {step['file']}"
            for label, step in _tour_steps()
            if not Path(str(step["file"])).is_file()
        ]

        assert missing == []

    def test_every_pattern_still_finds_its_code(self):
        unmatched = [
            f"{label}: {step['pattern']!r}"
            for label, step in _tour_steps()
            if "pattern" in step
            and Path(str(step["file"])).is_file()
            and not re.search(
                str(step["pattern"]),
                Path(str(step["file"])).read_text("utf-8"),
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
