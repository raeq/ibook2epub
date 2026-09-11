"""
Parsing expression grammars, for the small syntaxes a book carries.

A book is full of short strings with a grammar of their own: an ISBN behind a
``urn:isbn:`` label, a canonical fragment identifier pointing into a chapter, a
media fragment, the ``properties`` of a manifest item. Taking them apart with
``split`` and regular expressions works until a case the expression's author
did not think of, and then it answers wrongly and says nothing. A grammar
written in the notation of the syntax's own specification can be read against
that specification rule by rule, and tested apart from the code that uses it.

The package is an engine and the grammars it runs, one module to each part:

======================  =======================================================
:mod:`.syntaxes`        the grammars of the syntaxes ibook2epub reads
:mod:`.engine`          :class:`Grammar`, which checks, compiles and runs one
:mod:`.notation`        the notation a grammar is written in
:mod:`.expressions`     what a grammar is made of, and how it is compiled
:mod:`.tree`            :class:`Node`, what a parse returns
======================  =======================================================

Like :mod:`epubconvert.utils`, it imports nothing else of ours, so every layer
may use it (#28).
"""

from __future__ import annotations

from .engine import Grammar
from .expressions import MAX_DEPTH
from .notation import GrammarError
from .syntaxes import CFI, FRAGMENTS, IDENTIFIERS, NOTES, PACKAGE, WHITESPACE
from .tree import Node

__all__ = [
    "CFI",
    "FRAGMENTS",
    "IDENTIFIERS",
    "MAX_DEPTH",
    "NOTES",
    "PACKAGE",
    "WHITESPACE",
    "Grammar",
    "GrammarError",
    "Node",
]
