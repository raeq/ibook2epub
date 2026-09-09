"""
Deciding what a run does, and doing it.

The command line, the plan, the conversion, and the exit code. The only layer
that may import every other one.

``main`` is re-exported so the console script and ``python -m epubconvert``
both name the package rather than the module inside it.
"""

from .run import main

__all__ = ["main"]
