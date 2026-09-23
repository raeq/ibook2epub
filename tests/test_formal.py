"""
Run the TLA+ models in ``formal/`` through TLC, and hold each to its outcome.

A model is only worth its claims while it still checks, and a configuration
expected to fail matters as much as one expected to pass: it is what shows the
model still detects the defect a guard exists for, rather than having drifted
into checking nothing. So every configuration has an expected outcome below,
and every configuration on disk must have one.

TLC needs Java and ``tla2tools.jar``, neither of which a Python test run has,
so this module runs only when ``TLA2TOOLS_JAR`` names the jar. CI's ``formal``
job sets it; ``formal/README.md`` says how to run it locally.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

FORMAL = Path(__file__).resolve().parent.parent / "formal"
JAR = os.environ.get("TLA2TOOLS_JAR")

#: The line TLC prints when every property of a configuration holds.
HOLDS = "Model checking completed. No error has been found."

#: Each configuration, and what TLC must report for it: HOLDS, or the line
#: naming the property it must violate.
EXPECTED = {
    "OutputProtocol.Locking": HOLDS,
    "OutputProtocol.LockingUnguarded": HOLDS,
    "OutputProtocol.MixedLocking": HOLDS,
    "OutputProtocol.MixedLockingUnguarded": "Invariant NoLiveWorkSwept is violated.",
    "OutputProtocol.MixedLockingStall": "Invariant NoLiveWorkSwept is violated.",
    "OutputProtocol.NoLocking": "Temporal properties were violated.",
    "RerunPlanner.Stable": HOLDS,
    "RerunPlanner.StableSuffix": HOLDS,
    # Open defects, pinned so that fixing one has to update this table and the
    # model with it: see formal/README.md.
    "RerunPlanner.Match": "Invariant ExportedMeansTheBooksOwnFile is violated.",
    "RerunPlanner.MatchWithIdentifiers": (
        "Invariant ExportedMeansTheBooksOwnFile is violated."
    ),
    "RerunPlanner.LibraryChanges": (
        "Invariant ExportedMeansTheBooksOwnFile is violated."
    ),
    "RerunPlanner.RefreshOverwrites": (
        "Invariant NeverWritesOverAnotherBook is violated."
    ),
}


def test_every_configuration_has_an_expected_outcome():
    # Runs without TLC: a configuration added without an outcome would never
    # be checked, and one removed would leave a claim nothing supports.
    on_disk = {path.name.removesuffix(".cfg") for path in FORMAL.glob("*.cfg")}

    assert on_disk == set(EXPECTED)


@pytest.mark.skipif(JAR is None, reason="TLA2TOOLS_JAR names no tla2tools.jar")
@pytest.mark.parametrize("configuration", sorted(EXPECTED))
def test_the_model_checker_reports_the_expected_outcome(configuration, tmp_path):
    spec = configuration.split(".", 1)[0]
    completed = subprocess.run(  # noqa: S603 - fixed program, no shell
        [
            "java",
            "-XX:+UseParallelGC",
            "-cp",
            str(JAR),
            "tlc2.TLC",
            "-deadlock",  # every run finishing is an end state, not a fault
            "-workers",
            "auto",
            "-metadir",
            str(tmp_path),
            "-config",
            f"{configuration}.cfg",
            f"{spec}.tla",
        ],
        cwd=FORMAL,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    output = completed.stdout + completed.stderr

    assert EXPECTED[configuration] in output, output[-3000:]
    if EXPECTED[configuration] != HOLDS:
        assert HOLDS not in output
