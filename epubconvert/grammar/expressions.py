"""
What a grammar is made of, and how each part becomes a matcher.

A rule that makes a node has its result remembered by position (packrat
parsing), so backtracking never runs it twice at one place. A token's result is
not remembered: a token is a run of characters, which costs no more to run
again than to look up, and remembering it at every character held memory in
proportion to a long input (#26).

A repeat of something that consumes one character at a time and makes no
node -- ``WS*``, ``[0-9]+``, ``(!':~:' .)+`` -- is scanned by one regular
expression rather than by calls for every character, which took hundreds of
times as long (#27). The translation is made only where it means the same:
every part of the repeated item consumes a fixed number of characters, so
PEG's ordered choice and a regular expression's backtracking cannot reach
different answers. Anything else runs as written.

Rules nest at most :data:`MAX_DEPTH` deep in one parse, and input that nests
deeper is no match. The input is usually a book's or Apple's, so a crafted
string must cost its own parse, never the interpreter's stack and with it the
run.
"""

from __future__ import annotations

import re
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeAlias

from .tree import Node

#: The deepest rules may nest in one parse. A rule level costs the interpreter
#: about four frames, so its default limit of 1,000 frames allows some 248
#: levels, and this leaves the caller room. A CFI with 100 indirections went
#: past that and raised RecursionError (#25); the deepest real input measured
#: is a CFI with one indirection, 8 levels deep, and 20 indirections reach 64.
MAX_DEPTH = 100

#: Whether a repeat of one character at a time is scanned by a regular
#: expression. Only the tests turn it off, to hold the scan to the answers of
#: the per-call path it stands in for (#27).
SCAN_RUNS = True


class TooDeepError(Exception):
    """Raised inside a parse whose rules nest past :data:`MAX_DEPTH`."""


class UntranslatableError(Exception):
    """Raised for an expression no regular expression means the same as."""


@dataclass(slots=True)
class State:
    """
    One parse: its input, the nodes made so far, what each rule that makes a
    node matched, and how deeply the rules running now are nested.
    """

    source: str
    nodes: list[Node] = field(default_factory=list)
    memo: dict[int, tuple[int, tuple[Node, ...]]] = field(default_factory=dict)
    depth: int = 0


#: A compiled expression: given a parse and a position, the position after its
#: match, or -1. A matcher that fails leaves the parse's nodes as it found them.
Matcher: TypeAlias = Callable[[State, int], int]


@dataclass
class Table:
    """
    Where a reference finds the rule it names, once every rule is compiled,
    and the rules as written, where a scan finds a token's body (#27).
    """

    index: dict[str, int]
    matchers: list[Matcher]
    rules: dict[str, Expression]


class Expression:
    """One piece of a parsed grammar."""

    def parts(self) -> tuple[Expression, ...]:
        """
        Name the expressions directly inside this one.

        :return: Them, in order.
        """
        return ()

    def nullable(self, empty: frozenset[str]) -> bool:
        """
        Say whether this can match without consuming input.

        :param empty: The rules known to be able to.

        :return: True if it can.
        """
        del empty
        return False

    def leftmost(self, empty: frozenset[str]) -> set[str]:
        """
        Name the rules this can call before it has consumed any input.

        :param empty: The rules that can match without consuming input.

        :return: Their names.
        """
        found: set[str] = set()
        for part in self.parts():
            found |= part.leftmost(empty)
        return found

    def as_regex(self, table: Table, seen: frozenset[str]) -> tuple[str, int]:
        """
        Translate this into a regular expression, where that means the same.

        Only an expression that makes no node and always consumes the same
        number of characters translates: then PEG's ordered choice and a
        regular expression's backtracking cannot reach different answers.

        :param table: Where a token's body is found.
        :param seen: The tokens already being translated, so that one reaching
            itself is left alone.

        :return: The pattern and how many characters it consumes.

        :raises UntranslatableError: If no regular expression means the same.
        """
        raise NotImplementedError  # pragma: no cover - every expression has one

    def compile(self, table: Table) -> Matcher:
        """
        Turn this into a matcher.

        :param table: Where references find their rules.

        :return: The matcher.
        """
        raise NotImplementedError  # pragma: no cover - every expression has one


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _escape(char: str) -> str:
    """A code point as a regular expression escape, safe in a class or out."""
    return f"\\U{ord(char):08x}"


@dataclass(frozen=True)
class Literal(Expression):
    """``'text'``: these characters, the ASCII letters in either case under ``i``."""

    text: str
    ignore_case: bool = False

    def nullable(self, empty: frozenset[str]) -> bool:
        del empty
        return not self.text

    def compile(self, table: Table) -> Matcher:
        del table
        text, size = self.text, len(self.text)
        if self.ignore_case:
            folded = text.translate(_ASCII_LOWER)

            def either_case(state: State, pos: int) -> int:
                window = state.source[pos : pos + size]
                return pos + size if window.translate(_ASCII_LOWER) == folded else -1

            return either_case

        def exact(state: State, pos: int) -> int:
            return pos + size if state.source.startswith(text, pos) else -1

        return exact

    def as_regex(self, table: Table, seen: frozenset[str]) -> tuple[str, int]:
        del table, seen
        folded = self.ignore_case
        pattern = "".join(
            f"[{char.lower()}{char.upper()}]"
            if folded and char in string.ascii_letters
            else _escape(char)
            for char in self.text
        )
        return pattern, len(self.text)


@dataclass(frozen=True)
class CharacterClass(Expression):
    """``[...]``: one character in the ranges, or outside them when negated."""

    ranges: tuple[tuple[str, str], ...]
    negated: bool = False

    def compile(self, table: Table) -> Matcher:
        del table
        members = frozenset(
            chr(point)
            for low, high in self.ranges
            for point in range(ord(low), ord(high) + 1)
        )
        negated = self.negated

        def one_of(state: State, pos: int) -> int:
            source = state.source
            if pos < len(source) and (source[pos] in members) is not negated:
                return pos + 1
            return -1

        return one_of

    def pattern(self) -> str:
        """The class as a regular expression character class."""
        members = "".join(
            _escape(low) if low == high else f"{_escape(low)}-{_escape(high)}"
            for low, high in self.ranges
        )
        return f"[{'^' if self.negated else ''}{members}]"

    def as_regex(self, table: Table, seen: frozenset[str]) -> tuple[str, int]:
        del table, seen
        return self.pattern(), 1


@dataclass(frozen=True)
class AnyCharacter(Expression):
    """``.``: any one character."""

    def compile(self, table: Table) -> Matcher:
        del table

        def anything(state: State, pos: int) -> int:
            return pos + 1 if pos < len(state.source) else -1

        return anything

    def as_regex(self, table: Table, seen: frozenset[str]) -> tuple[str, int]:
        del table, seen
        return "(?s:.)", 1


@dataclass(frozen=True)
class Reference(Expression):
    """A rule named inside another, found once every rule is compiled."""

    name: str

    def nullable(self, empty: frozenset[str]) -> bool:
        return self.name in empty

    def leftmost(self, empty: frozenset[str]) -> set[str]:
        del empty
        return {self.name}

    def compile(self, table: Table) -> Matcher:
        matchers, index = table.matchers, table.index[self.name]

        def call(state: State, pos: int) -> int:
            return matchers[index](state, pos)

        return call

    def as_regex(self, table: Table, seen: frozenset[str]) -> tuple[str, int]:
        # A rule that makes a node cannot be skipped over; a token that reaches
        # itself cannot be written out.
        if self.name[0].islower() or self.name in seen:
            raise UntranslatableError
        return table.rules[self.name].as_regex(table, seen | {self.name})


@dataclass(frozen=True)
class Sequence(Expression):
    """``a b``: each part in turn, from where the one before it stopped."""

    items: tuple[Expression, ...]

    def parts(self) -> tuple[Expression, ...]:
        return self.items

    def nullable(self, empty: frozenset[str]) -> bool:
        return all(item.nullable(empty) for item in self.items)

    def leftmost(self, empty: frozenset[str]) -> set[str]:
        found: set[str] = set()
        for item in self.items:
            found |= item.leftmost(empty)
            if not item.nullable(empty):
                break
        return found

    def compile(self, table: Table) -> Matcher:
        items = tuple(item.compile(table) for item in self.items)

        def in_order(state: State, pos: int) -> int:
            nodes = state.nodes
            mark = len(nodes)
            for item in items:
                pos = item(state, pos)
                if pos < 0:
                    del nodes[mark:]
                    return -1
            return pos

        return in_order

    def as_regex(self, table: Table, seen: frozenset[str]) -> tuple[str, int]:
        patterns: list[str] = []
        width = 0
        for item in self.items:
            pattern, consumed = item.as_regex(table, seen)
            patterns.append(f"(?:{pattern})")
            width += consumed
        return "".join(patterns), width


@dataclass(frozen=True)
class Choice(Expression):
    """``a / b``: the first option that matches, and no other after it."""

    options: tuple[Expression, ...]

    def parts(self) -> tuple[Expression, ...]:
        return self.options

    def nullable(self, empty: frozenset[str]) -> bool:
        return any(option.nullable(empty) for option in self.options)

    def compile(self, table: Table) -> Matcher:
        options = tuple(option.compile(table) for option in self.options)

        def first_of(state: State, pos: int) -> int:
            for option in options:
                end = option(state, pos)
                if end >= 0:
                    return end
            return -1

        return first_of

    def as_regex(self, table: Table, seen: frozenset[str]) -> tuple[str, int]:
        # Options of one width end in one place, so which of them PEG picks
        # cannot change what follows; options of different widths could.
        translated = [option.as_regex(table, seen) for option in self.options]
        widths = {width for _, width in translated}
        if len(widths) != 1:
            raise UntranslatableError
        return f"(?:{'|'.join(pattern for pattern, _ in translated)})", widths.pop()


@dataclass(frozen=True)
class Repeat(Expression):
    """``e*``, ``e+``, ``e?``, ``e{m,n}``: as often as it matches, giving none back."""

    item: Expression
    low: int
    high: int | None

    def parts(self) -> tuple[Expression, ...]:
        return (self.item,)

    def nullable(self, empty: frozenset[str]) -> bool:
        return self.low == 0 or self.item.nullable(empty)

    def as_regex(self, table: Table, seen: frozenset[str]) -> tuple[str, int]:
        # A repeat's width depends on its input, and PEG's repeat never gives
        # back what it took, where a regular expression's does.
        del table, seen
        raise UntranslatableError

    def compile(self, table: Table) -> Matcher:
        scan = self._scan(table) if SCAN_RUNS else None
        if scan is not None:
            return scan
        item, low, high = self.item.compile(table), self.low, self.high

        def repeatedly(state: State, pos: int) -> int:
            nodes = state.nodes
            mark = len(nodes)
            count = 0
            while high is None or count < high:
                end = item(state, pos)
                if end < 0:
                    break
                count += 1
                if end == pos:
                    # Matching nothing once matches nothing every time after.
                    count = max(count, low)
                    break
                pos = end
            if count < low:
                del nodes[mark:]
                return -1
            return pos

        return repeatedly

    def _scan(self, table: Table) -> Matcher | None:
        """
        Scan the whole run in one regular expression call, when the item
        always consumes exactly one character (#27).

        Such a repeat takes characters until the item first fails, up to its
        upper bound. An item that is one character class is repeated as a
        regex class, which the regex engine runs keeping nothing per
        character. Any other item, such as a character behind a lookahead, is
        found by searching for its first failure, ``(?!item)``: repeating that
        as a regular expression kept a backtracking record for every character.
        """
        unit = _unit_class(self.item, table, frozenset())
        if unit is not None:
            bound = "" if self.high is None else str(self.high)
            match = re.compile(f"{unit}{{{self.low},{bound}}}").match

            def running(state: State, pos: int) -> int:
                found = match(state.source, pos)
                return found.end() if found else -1

            return running
        try:
            pattern, width = self.item.as_regex(table, frozenset())
        except UntranslatableError:
            return None
        if width != 1:
            return None
        search = re.compile(f"(?!{pattern})").search
        low, high = self.low, self.high

        def scanning(state: State, pos: int) -> int:
            # The item needs a character, so the search stops at the end at
            # the latest.
            stop = search(state.source, pos)
            end = stop.start() if stop else len(state.source)
            if end - pos < low:
                return -1
            return end if high is None else min(end, pos + high)

        return scanning


@dataclass(frozen=True)
class Lookahead(Expression):
    """``&e`` or ``!e``: whether *e* matches here, without consuming it."""

    item: Expression
    positive: bool

    def parts(self) -> tuple[Expression, ...]:
        return (self.item,)

    def nullable(self, empty: frozenset[str]) -> bool:
        del empty
        return True

    def compile(self, table: Table) -> Matcher:
        item, positive = self.item.compile(table), self.positive

        def looking(state: State, pos: int) -> int:
            nodes = state.nodes
            mark = len(nodes)
            found = item(state, pos) >= 0
            del nodes[mark:]
            return pos if found is positive else -1

        return looking

    def as_regex(self, table: Table, seen: frozenset[str]) -> tuple[str, int]:
        pattern, _ = self.item.as_regex(table, seen)
        return f"(?{'=' if self.positive else '!'}{pattern})", 0


#: "." as a regular expression character class: every code point.
_ANY_CLASS = f"[{_escape(chr(0))}-{_escape(chr(0x10FFFF))}]"


def _unit_class(
    expression: Expression, table: Table, seen: frozenset[str]
) -> str | None:
    """
    The one regular expression character class *expression* is, if it is one.

    A class is the one thing the regex engine repeats without keeping a record
    for every character, so a run of one is scanned that way (#27): a class,
    ".", a one-character literal, or a token that is one of these.
    """
    # A token stands for its body; one that reaches itself cannot be written out.
    while isinstance(expression, Reference):
        name = expression.name
        if name[0].islower() or name in seen:
            return None
        seen = seen | {name}
        expression = table.rules[name]
    if isinstance(expression, CharacterClass):
        return expression.pattern()
    if isinstance(expression, AnyCharacter):
        return _ANY_CLASS
    if not isinstance(expression, Literal) or len(expression.text) != 1:
        return None
    char = expression.text
    if expression.ignore_case and char in string.ascii_letters:
        return f"[{char.lower()}{char.upper()}]"
    return f"[{_escape(char)}]"


def make_rule(index: int, count: int, name: str, body: Matcher) -> Matcher:
    """
    A rule: its body, kept as a node and remembered by position if lower-case.

    A token is neither: it runs its body each time it is called (#26).
    Running a body is one level of nesting for either kind; a result looked up
    in the memo is none, because it runs nothing.
    """
    if not name[0].islower():

        def token(state: State, pos: int) -> int:
            state.depth += 1
            if state.depth > MAX_DEPTH:
                raise TooDeepError
            end = body(state, pos)
            state.depth -= 1
            return end

        return token

    def rule(state: State, pos: int) -> int:
        key = pos * count + index
        remembered = state.memo.get(key)
        if remembered is not None:
            state.nodes.extend(remembered[1])
            return remembered[0]
        state.depth += 1
        if state.depth > MAX_DEPTH:
            raise TooDeepError
        nodes = state.nodes
        mark = len(nodes)
        end = body(state, pos)
        state.depth -= 1
        found: tuple[Node, ...] = ()
        if end >= 0:
            found = (Node(state.source, name, pos, end, tuple(nodes[mark:])),)
            del nodes[mark:]
            nodes.extend(found)
        state.memo[key] = (end, found)
        return end

    return rule
