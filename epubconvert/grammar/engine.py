"""
A grammar: read from its notation, checked, compiled and run.

A grammar is checked when it is built, and each of these is a
:class:`~epubconvert.grammar.notation.GrammarError` rather than a parse that
fails or never ends: a rule used but not defined, or defined twice; a rule that
can reach itself without consuming input (left recursion, which a PEG cannot
parse); and an expression that can match nothing, repeated without bound.
"""

from __future__ import annotations

from collections.abc import Iterator

from .expressions import (
    Expression,
    Reference,
    Repeat,
    State,
    Table,
    TooDeepError,
    make_rule,
)
from .notation import GrammarError, Reader
from .tree import Node


def _walk(expression: Expression) -> Iterator[Expression]:
    yield expression
    for part in expression.parts():
        yield from _walk(part)


def _nullable_rules(rules: dict[str, Expression]) -> frozenset[str]:
    """The rules that can match without consuming input, to a fixed point."""
    empty: frozenset[str] = frozenset()
    while True:
        grown = frozenset(name for name, body in rules.items() if body.nullable(empty))
        if grown == empty:
            return empty
        empty = grown


def _refuse_left_recursion(rules: dict[str, Expression], empty: frozenset[str]) -> None:
    calls = {name: body.leftmost(empty) for name, body in rules.items()}
    done: set[str] = set()

    def visit(name: str, path: list[str]) -> None:
        if name in path:
            cycle = " -> ".join([*path[path.index(name) :], name])
            raise GrammarError(f"left recursion: {cycle} before reading any input")
        if name in done:
            return
        path.append(name)
        for callee in sorted(calls[name]):
            visit(callee, path)
        path.pop()
        done.add(name)

    for name in rules:
        visit(name, [])


def _check(rules: dict[str, Expression]) -> None:
    """Refuse rules that cannot work, before any input meets them."""
    for name, body in rules.items():
        for part in _walk(body):
            if isinstance(part, Reference) and part.name not in rules:
                raise GrammarError(
                    f"rule {name!r} uses {part.name!r}, which is not defined"
                )
    empty = _nullable_rules(rules)
    for name, body in rules.items():
        for part in _walk(body):
            if (
                isinstance(part, Repeat)
                and part.high is None
                and part.item.nullable(empty)
            ):
                raise GrammarError(
                    f"rule {name!r} repeats without bound an expression that can "
                    "match nothing"
                )
    _refuse_left_recursion(rules, empty)


class Grammar:
    """
    A parsing expression grammar, checked and compiled.

    :param source: The grammar, in the notation that
        :mod:`epubconvert.grammar.notation` reads.

    :raises GrammarError: If the notation is malformed or the rules cannot
        work.
    """

    def __init__(self, source: str) -> None:
        #: The notation the grammar was built from.
        self.source = source
        rules = Reader(source).rules()
        _check(rules)
        self._names = tuple(rules)
        table = Table(
            {name: index for index, name in enumerate(self._names)}, [], rules
        )
        self._bodies = [rules[name].compile(table) for name in self._names]
        table.matchers.extend(
            make_rule(index, len(self._names), name, body)
            for index, (name, body) in enumerate(
                zip(self._names, self._bodies, strict=True)
            )
        )
        self._index = table.index

    @property
    def rules(self) -> tuple[str, ...]:
        """The rule names, in the order they were written."""
        return self._names

    def match(self, text: str, rule: str | None = None) -> Node | None:
        """
        Parse the whole of *text* as one rule.

        :param text: The input.
        :param rule: The rule to match; the grammar's first rule by default.

        :return: The parse, or None when the rule does not match all of it or
            the input nests rules deeper than
            :data:`~epubconvert.grammar.expressions.MAX_DEPTH`.
        """
        return self._parse(text, rule, whole=True)

    def match_prefix(self, text: str, rule: str | None = None) -> Node | None:
        """
        Parse the start of *text* as one rule, however much of it that covers.

        :param text: The input.
        :param rule: The rule to match; the grammar's first rule by default.

        :return: The parse, whose ``end`` is where it stopped, or None, as for
            :meth:`match`.
        """
        return self._parse(text, rule, whole=False)

    def _parse(self, text: str, rule: str | None, whole: bool) -> Node | None:
        name = self._names[0] if rule is None else rule
        index = self._index.get(name)
        if index is None:
            raise GrammarError(f"the grammar has no rule {name!r}")
        state = State(text)
        try:
            end = self._bodies[index](state, 0)
        except TooDeepError:
            return None
        if end < 0 or (whole and end != len(text)):
            return None
        return Node(text, name, 0, end, tuple(state.nodes))
