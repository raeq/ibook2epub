"""
Parsing expression grammars, for the small syntaxes a book carries.

A book is full of short strings with a grammar of their own: an ISBN behind a
``urn:isbn:`` label, a canonical fragment identifier pointing into a chapter, a
media fragment, the ``properties`` of a manifest item. Taking them apart with
``split`` and regular expressions works until a case the expression's author
did not think of, and then it answers wrongly and says nothing. A grammar
written in the notation of the syntax's own specification can be read against
that specification rule by rule, and tested apart from the code that uses it.

This module runs such grammars. The notation is Bryan Ford's ("Parsing
Expression Grammars: A Recognition-Based Syntactic Foundation", POPL 2004),
with the additions most PEG tools make:

- ``'text'i`` matches the ASCII letters in *text* in either case;
- ``e{n}``, ``e{m,}`` and ``e{m,n}`` repeat a bounded number of times;
- ``[^...]`` is a negated character class;
- ``\\xHH`` and ``\\uHHHH`` name a code point in a literal or a class;
- ``#`` starts a comment that runs to the end of the line.

A rule whose name begins with a lower-case letter becomes a :class:`Node` in
the parse, holding the nodes of the rules matched inside it. One whose name
begins with an upper-case letter is a token: it matches but leaves no node, and
its text is part of the node above it. A rule that makes a node has its result
remembered by position (packrat parsing), so backtracking never runs it twice at
one place. A token's result is not remembered: a token is a run of characters,
which costs no more to run again than to look up, and remembering it at every
character held memory in proportion to a long input (#26).

Rules nest at most :data:`MAX_DEPTH` deep in one parse, and input that nests
deeper is no match. The input is usually a book's or Apple's, so a crafted
string must cost its own parse, never the interpreter's stack and with it the
run.

A grammar is checked when it is built, and each of these is a
:class:`GrammarError` rather than a parse that fails or never ends: a rule used
but not defined, or defined twice; a rule that can reach itself without
consuming input (left recursion, which a PEG cannot parse); and an expression
that can match nothing, repeated without bound.
"""

from __future__ import annotations

import string
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TypeAlias

__all__ = ["MAX_DEPTH", "Grammar", "GrammarError", "Node"]

#: The deepest rules may nest in one parse. A rule level costs the interpreter
#: about four frames, so its default limit of 1,000 frames allows some 248
#: levels, and this leaves the caller room. A CFI with 100 indirections went
#: past that and raised RecursionError (#25); the deepest real input measured
#: is a CFI with one indirection, 8 levels deep, and 20 indirections reach 64.
MAX_DEPTH = 100


class GrammarError(ValueError):
    """A grammar that cannot be run: bad notation, or rules that cannot work."""


class _TooDeepError(Exception):
    """Raised inside a parse whose rules nest past :data:`MAX_DEPTH`."""


@dataclass(frozen=True)
class Node:
    """One rule's match: where it lay in the input, and the rules inside it."""

    source: str = field(repr=False)
    name: str
    start: int
    end: int
    children: tuple[Node, ...] = ()

    @property
    def text(self) -> str:
        """The part of the input the rule matched."""
        return self.source[self.start : self.end]

    def walk(self) -> Iterator[Node]:
        """
        Visit every node inside this one.

        :return: The nodes, in the order they begin.
        """
        for child in self.children:
            yield child
            yield from child.walk()

    def find(self, name: str) -> Node | None:
        """
        Find the first node inside this one with a given name.

        :param name: The rule's name.

        :return: The node, or None when the rule did not match.
        """
        return next((node for node in self.walk() if node.name == name), None)

    def find_all(self, name: str) -> list[Node]:
        """
        Find every node inside this one with a given name.

        :param name: The rule's name.

        :return: The nodes, in the order they begin.
        """
        return [node for node in self.walk() if node.name == name]

    def child(self, name: str) -> Node:
        """
        Find a node the grammar guarantees is there.

        :param name: The rule's name.

        :return: The first node with that name.

        :raises LookupError: If there is none, which means the grammar and the
            code reading its parse disagree.
        """
        found = self.find(name)
        if found is None:
            raise LookupError(f"no {name!r} inside this {self.name!r}")
        return found


@dataclass(slots=True)
class _State:
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
_Matcher: TypeAlias = Callable[[_State, int], int]


@dataclass
class _Table:
    """Where a reference finds the rule it names, once every rule is compiled."""

    index: dict[str, int]
    matchers: list[_Matcher]


# ------------------------------------------------------------------ expressions


class _Expression:
    """One piece of a parsed grammar."""

    def parts(self) -> tuple[_Expression, ...]:
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

    def compile(self, table: _Table) -> _Matcher:
        """
        Turn this into a matcher.

        :param table: Where references find their rules.

        :return: The matcher.
        """
        raise NotImplementedError  # pragma: no cover - every expression has one


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


@dataclass(frozen=True)
class _Literal(_Expression):
    text: str
    ignore_case: bool = False

    def nullable(self, empty: frozenset[str]) -> bool:
        del empty
        return not self.text

    def compile(self, table: _Table) -> _Matcher:
        del table
        text, size = self.text, len(self.text)
        if self.ignore_case:
            folded = text.translate(_ASCII_LOWER)

            def either_case(state: _State, pos: int) -> int:
                window = state.source[pos : pos + size]
                return pos + size if window.translate(_ASCII_LOWER) == folded else -1

            return either_case

        def exact(state: _State, pos: int) -> int:
            return pos + size if state.source.startswith(text, pos) else -1

        return exact


@dataclass(frozen=True)
class _Class(_Expression):
    ranges: tuple[tuple[str, str], ...]
    negated: bool = False

    def compile(self, table: _Table) -> _Matcher:
        del table
        members = frozenset(
            chr(point)
            for low, high in self.ranges
            for point in range(ord(low), ord(high) + 1)
        )
        negated = self.negated

        def one_of(state: _State, pos: int) -> int:
            source = state.source
            if pos < len(source) and (source[pos] in members) is not negated:
                return pos + 1
            return -1

        return one_of


@dataclass(frozen=True)
class _Any(_Expression):
    def compile(self, table: _Table) -> _Matcher:
        del table

        def anything(state: _State, pos: int) -> int:
            return pos + 1 if pos < len(state.source) else -1

        return anything


@dataclass(frozen=True)
class _Reference(_Expression):
    name: str

    def nullable(self, empty: frozenset[str]) -> bool:
        return self.name in empty

    def leftmost(self, empty: frozenset[str]) -> set[str]:
        del empty
        return {self.name}

    def compile(self, table: _Table) -> _Matcher:
        matchers, index = table.matchers, table.index[self.name]

        def call(state: _State, pos: int) -> int:
            return matchers[index](state, pos)

        return call


@dataclass(frozen=True)
class _Sequence(_Expression):
    items: tuple[_Expression, ...]

    def parts(self) -> tuple[_Expression, ...]:
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

    def compile(self, table: _Table) -> _Matcher:
        items = tuple(item.compile(table) for item in self.items)

        def in_order(state: _State, pos: int) -> int:
            nodes = state.nodes
            mark = len(nodes)
            for item in items:
                pos = item(state, pos)
                if pos < 0:
                    del nodes[mark:]
                    return -1
            return pos

        return in_order


@dataclass(frozen=True)
class _Choice(_Expression):
    options: tuple[_Expression, ...]

    def parts(self) -> tuple[_Expression, ...]:
        return self.options

    def nullable(self, empty: frozenset[str]) -> bool:
        return any(option.nullable(empty) for option in self.options)

    def compile(self, table: _Table) -> _Matcher:
        options = tuple(option.compile(table) for option in self.options)

        def first_of(state: _State, pos: int) -> int:
            for option in options:
                end = option(state, pos)
                if end >= 0:
                    return end
            return -1

        return first_of


@dataclass(frozen=True)
class _Repeat(_Expression):
    item: _Expression
    low: int
    high: int | None

    def parts(self) -> tuple[_Expression, ...]:
        return (self.item,)

    def nullable(self, empty: frozenset[str]) -> bool:
        return self.low == 0 or self.item.nullable(empty)

    def compile(self, table: _Table) -> _Matcher:
        item, low, high = self.item.compile(table), self.low, self.high

        def repeatedly(state: _State, pos: int) -> int:
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


@dataclass(frozen=True)
class _Lookahead(_Expression):
    item: _Expression
    positive: bool

    def parts(self) -> tuple[_Expression, ...]:
        return (self.item,)

    def nullable(self, empty: frozenset[str]) -> bool:
        del empty
        return True

    def compile(self, table: _Table) -> _Matcher:
        item, positive = self.item.compile(table), self.positive

        def looking(state: _State, pos: int) -> int:
            nodes = state.nodes
            mark = len(nodes)
            found = item(state, pos) >= 0
            del nodes[mark:]
            return pos if found is positive else -1

        return looking


def _rule(index: int, count: int, name: str, body: _Matcher) -> _Matcher:
    """
    A rule: its body, kept as a node and remembered by position if lower-case.

    A token is neither: it runs its body each time it is called (#26).
    Running a body is one level of nesting for either kind; a result looked up
    in the memo is none, because it runs nothing.
    """
    if not name[0].islower():

        def token(state: _State, pos: int) -> int:
            state.depth += 1
            if state.depth > MAX_DEPTH:
                raise _TooDeepError
            end = body(state, pos)
            state.depth -= 1
            return end

        return token

    def rule(state: _State, pos: int) -> int:
        key = pos * count + index
        remembered = state.memo.get(key)
        if remembered is not None:
            state.nodes.extend(remembered[1])
            return remembered[0]
        state.depth += 1
        if state.depth > MAX_DEPTH:
            raise _TooDeepError
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


# --------------------------------------------------------------------- notation


_SIMPLE_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "[": "[",
    "]": "]",
    "-": "-",
    "^": "^",
}
_NAME_START = frozenset(string.ascii_letters)
_NAME_CHARS = _NAME_START | frozenset(string.digits + "_")
_DIGITS = frozenset(string.digits)
_ITEM_STARTS = frozenset("&!(.'\"[")


class _Reader:  # pylint: disable=too-few-public-methods
    """Reads grammar notation into rules, one token at a time."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0

    def rules(self) -> dict[str, _Expression]:
        """
        Read every rule.

        :return: The rules by name, in the order written.

        :raises GrammarError: If the notation is malformed.
        """
        rules: dict[str, _Expression] = {}
        while self._peek():
            name = self._name()
            if name in rules:
                raise self._error(f"rule {name!r} is defined twice")
            self._expect("<-")
            rules[name] = self._expression()
        if not rules:
            raise GrammarError("the grammar has no rules")
        return rules

    def _error(self, message: str) -> GrammarError:
        line = self.source.count("\n", 0, self.pos) + 1
        return GrammarError(f"line {line}: {message}")

    def _skip(self) -> None:
        source, size = self.source, len(self.source)
        while self.pos < size:
            char = source[self.pos]
            if char == "#":
                end = source.find("\n", self.pos)
                self.pos = size if end < 0 else end
            elif char in " \t\r\n":
                self.pos += 1
            else:
                return

    def _peek(self) -> str:
        self._skip()
        return self.source[self.pos : self.pos + 1]

    def _accept(self, token: str) -> bool:
        self._skip()
        if self.source.startswith(token, self.pos):
            self.pos += len(token)
            return True
        return False

    def _expect(self, token: str) -> None:
        if not self._accept(token):
            raise self._error(f"expected {token!r}")

    def _name(self) -> str:
        self._skip()
        start = self.pos
        if self.source[start : start + 1] not in _NAME_START:
            raise self._error("expected a rule name")
        self.pos += 1
        while self.source[self.pos : self.pos + 1] in _NAME_CHARS:
            self.pos += 1
        return self.source[start : self.pos]

    def _expression(self) -> _Expression:
        options = [self._sequence()]
        while self._accept("/"):
            options.append(self._sequence())
        return options[0] if len(options) == 1 else _Choice(tuple(options))

    def _sequence(self) -> _Expression:
        items: list[_Expression] = []
        while self._at_item():
            items.append(self._prefixed())
        if not items:
            raise self._error("expected an expression")
        return items[0] if len(items) == 1 else _Sequence(tuple(items))

    def _at_item(self) -> bool:
        char = self._peek()
        if char in _ITEM_STARTS:
            return True
        if char not in _NAME_START:
            return False
        saved = self.pos
        self._name()
        defines = self._accept("<-")
        self.pos = saved
        return not defines

    def _prefixed(self) -> _Expression:
        if self._accept("&"):
            return _Lookahead(self._suffixed(), True)
        if self._accept("!"):
            return _Lookahead(self._suffixed(), False)
        return self._suffixed()

    def _suffixed(self) -> _Expression:
        item = self._primary()
        for token, low, high in (("?", 0, 1), ("*", 0, None), ("+", 1, None)):
            if self._accept(token):
                return _Repeat(item, low, high)
        if self._accept("{"):
            return self._bounded(item)
        return item

    def _bounded(self, item: _Expression) -> _Repeat:
        low = self._number()
        high: int | None = low
        if self._accept(","):
            high = None if self._peek() == "}" else self._number()
        self._expect("}")
        if high is not None and high < low:
            raise self._error("a repetition's upper bound is below its lower bound")
        return _Repeat(item, low, high)

    def _number(self) -> int:
        self._skip()
        start = self.pos
        while self.source[self.pos : self.pos + 1] in _DIGITS:
            self.pos += 1
        if start == self.pos:
            raise self._error("expected a number")
        return int(self.source[start : self.pos])

    def _primary(self) -> _Expression:
        char = self._peek()
        if char == "(":
            self.pos += 1
            inner = self._expression()
            self._expect(")")
            return inner
        if char == ".":
            self.pos += 1
            return _Any()
        if char in ("'", '"'):
            return self._literal(char)
        if char == "[":
            return self._class()
        return _Reference(self._name())

    def _literal(self, quote: str) -> _Literal:
        self.pos += 1
        text: list[str] = []
        while not self.source.startswith(quote, self.pos):
            text.append(self._character())
        self.pos += 1
        folded = self.source[self.pos : self.pos + 1] == "i" and (
            self.source[self.pos + 1 : self.pos + 2] not in _NAME_CHARS
        )
        if folded:
            self.pos += 1
        return _Literal("".join(text), folded)

    def _class(self) -> _Class:
        self.pos += 1
        negated = self.source.startswith("^", self.pos)
        if negated:
            self.pos += 1
        ranges: list[tuple[str, str]] = []
        while not self.source.startswith("]", self.pos):
            low = high = self._character()
            if self.source.startswith("-", self.pos) and not self.source.startswith(
                "-]", self.pos
            ):
                self.pos += 1
                high = self._character()
                if high < low:
                    raise self._error(f"the range {low!r}-{high!r} runs backwards")
            ranges.append((low, high))
        self.pos += 1
        if not ranges:
            raise self._error("a character class is empty")
        return _Class(tuple(ranges), negated)

    def _character(self) -> str:
        """One character of a literal or a class, with its escape undone."""
        if self.pos >= len(self.source):
            raise self._error("a literal or a character class is not closed")
        char = self.source[self.pos]
        if char != "\\":
            self.pos += 1
            return char
        code = self.source[self.pos + 1 : self.pos + 2]
        if code in ("x", "u"):
            width = 2 if code == "x" else 4
            digits = self.source[self.pos + 2 : self.pos + 2 + width]
            if len(digits) != width or not all(d in string.hexdigits for d in digits):
                raise self._error(f"\\{code} needs {width} hexadecimal digits")
            self.pos += 2 + width
            return chr(int(digits, 16))
        if code not in _SIMPLE_ESCAPES:
            raise self._error(f"unknown escape \\{code}")
        self.pos += 2
        return _SIMPLE_ESCAPES[code]


# ----------------------------------------------------------------------- checks


def _walk(expression: _Expression) -> Iterator[_Expression]:
    yield expression
    for part in expression.parts():
        yield from _walk(part)


def _nullable_rules(rules: dict[str, _Expression]) -> frozenset[str]:
    """The rules that can match without consuming input, to a fixed point."""
    empty: frozenset[str] = frozenset()
    while True:
        grown = frozenset(name for name, body in rules.items() if body.nullable(empty))
        if grown == empty:
            return empty
        empty = grown


def _refuse_left_recursion(
    rules: dict[str, _Expression], empty: frozenset[str]
) -> None:
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


def _check(rules: dict[str, _Expression]) -> None:
    """Refuse rules that cannot work, before any input meets them."""
    for name, body in rules.items():
        for part in _walk(body):
            if isinstance(part, _Reference) and part.name not in rules:
                raise GrammarError(
                    f"rule {name!r} uses {part.name!r}, which is not defined"
                )
    empty = _nullable_rules(rules)
    for name, body in rules.items():
        for part in _walk(body):
            if (
                isinstance(part, _Repeat)
                and part.high is None
                and part.item.nullable(empty)
            ):
                raise GrammarError(
                    f"rule {name!r} repeats without bound an expression that can "
                    "match nothing"
                )
    _refuse_left_recursion(rules, empty)


# ---------------------------------------------------------------------- grammar


class Grammar:
    """
    A parsing expression grammar, checked and compiled.

    :param source: The grammar, in the notation described above.

    :raises GrammarError: If the notation is malformed or the rules cannot
        work.
    """

    def __init__(self, source: str) -> None:
        rules = _Reader(source).rules()
        _check(rules)
        self._names = tuple(rules)
        table = _Table({name: index for index, name in enumerate(self._names)}, [])
        self._bodies = [rules[name].compile(table) for name in self._names]
        table.matchers.extend(
            _rule(index, len(self._names), name, body)
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
            the input nests rules more than :data:`MAX_DEPTH` deep.
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
        state = _State(text)
        try:
            end = self._bodies[index](state, 0)
        except _TooDeepError:
            return None
        if end < 0 or (whole and end != len(text)):
            return None
        return Node(text, name, 0, end, tuple(state.nodes))
