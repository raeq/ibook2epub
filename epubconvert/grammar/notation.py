"""
The notation a grammar is written in, read into expressions.

The notation is Bryan Ford's ("Parsing Expression Grammars: A Recognition-Based
Syntactic Foundation", POPL 2004), with the additions most PEG tools make:

- ``'text'i`` matches the ASCII letters in *text* in either case;
- ``e{n}``, ``e{m,}`` and ``e{m,n}`` repeat a bounded number of times;
- ``[^...]`` is a negated character class;
- ``\\xHH`` and ``\\uHHHH`` name a code point in a literal or a class;
- ``#`` starts a comment that runs to the end of the line.

A rule whose name begins with a lower-case letter makes a node in the parse,
and one whose name begins with an upper-case letter is a token that makes
none: :mod:`epubconvert.grammar.tree` says what a parse holds.
"""

from __future__ import annotations

import string

from .expressions import (
    AnyCharacter,
    CharacterClass,
    Choice,
    Expression,
    Literal,
    Lookahead,
    Reference,
    Repeat,
    Sequence,
)


class GrammarError(ValueError):
    """A grammar that cannot be run: bad notation, or rules that cannot work."""


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


class Reader:  # pylint: disable=too-few-public-methods
    """Reads grammar notation into rules, one token at a time."""

    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0

    def rules(self) -> dict[str, Expression]:
        """
        Read every rule.

        :return: The rules by name, in the order written.

        :raises GrammarError: If the notation is malformed.
        """
        rules: dict[str, Expression] = {}
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

    def _expression(self) -> Expression:
        options = [self._sequence()]
        while self._accept("/"):
            options.append(self._sequence())
        return options[0] if len(options) == 1 else Choice(tuple(options))

    def _sequence(self) -> Expression:
        items: list[Expression] = []
        while self._at_item():
            items.append(self._prefixed())
        if not items:
            raise self._error("expected an expression")
        return items[0] if len(items) == 1 else Sequence(tuple(items))

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

    def _prefixed(self) -> Expression:
        if self._accept("&"):
            return Lookahead(self._suffixed(), True)
        if self._accept("!"):
            return Lookahead(self._suffixed(), False)
        return self._suffixed()

    def _suffixed(self) -> Expression:
        item = self._primary()
        for token, low, high in (("?", 0, 1), ("*", 0, None), ("+", 1, None)):
            if self._accept(token):
                return Repeat(item, low, high)
        if self._accept("{"):
            return self._bounded(item)
        return item

    def _bounded(self, item: Expression) -> Repeat:
        low = self._number()
        high: int | None = low
        if self._accept(","):
            high = None if self._peek() == "}" else self._number()
        self._expect("}")
        if high is not None and high < low:
            raise self._error("a repetition's upper bound is below its lower bound")
        return Repeat(item, low, high)

    def _number(self) -> int:
        self._skip()
        start = self.pos
        while self.source[self.pos : self.pos + 1] in _DIGITS:
            self.pos += 1
        if start == self.pos:
            raise self._error("expected a number")
        return int(self.source[start : self.pos])

    def _primary(self) -> Expression:
        char = self._peek()
        if char == "(":
            self.pos += 1
            inner = self._expression()
            self._expect(")")
            return inner
        if char == ".":
            self.pos += 1
            return AnyCharacter()
        if char in ("'", '"'):
            return self._literal(char)
        if char == "[":
            return self._class()
        return Reference(self._name())

    def _literal(self, quote: str) -> Literal:
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
        return Literal("".join(text), folded)

    def _class(self) -> CharacterClass:
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
        return CharacterClass(tuple(ranges), negated)

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
