"""
What a parse returns: a node for each rule that makes one.

A rule whose name begins with a lower-case letter becomes a :class:`Node` in
the parse, holding the nodes of the rules matched inside it. One whose name
begins with an upper-case letter is a token: it matches but leaves no node, and
its text is part of the node above it. Code that reads a parse walks its nodes
rather than cutting the string up again.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field


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
