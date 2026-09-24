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
import re
from pathlib import Path


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
