"""
The parsing expression grammar engine in ``epubconvert.utils.peg``.

Every piece of the notation, the shape of a parse, and each rule a grammar must
pass before it is used: a grammar that breaks one is refused when it is built,
never discovered when some input fails to parse.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods

from __future__ import annotations

import re

import pytest

from epubconvert.utils.peg import Grammar, GrammarError


def matches(grammar: str, text: str) -> bool:
    return Grammar(grammar).match(text) is not None


class TestTheNotation:
    @pytest.mark.parametrize(
        ("grammar", "yes", "no"),
        [
            ("s <- 'ab'", "ab", "abc"),
            ('s <- "ab"', "ab", "a"),
            ("s <- 'ab'i", "aB", "ac"),
            ("s <- [a-c]", "b", "d"),
            ("s <- [^a-c]", "d", "b"),
            ("s <- [a-]", "-", "b"),
            ("s <- .", "x", ""),
            ("s <- 'a'?", "", "aa"),
            ("s <- 'a'*", "aaa", "b"),
            ("s <- 'a'+", "a", ""),
            ("s <- 'a'{2}", "aa", "aaa"),
            ("s <- 'a'{2,}", "aaaa", "a"),
            ("s <- 'a'{1,2}", "aa", "aaa"),
            ("s <- &'a' .", "a", "b"),
            ("s <- !'a' .", "b", "a"),
            ("s <- ('a' / 'b') 'c'", "bc", "cc"),
        ],
    )
    def test_each_operator(self, grammar, yes, no):
        assert matches(grammar, yes)
        assert not matches(grammar, no)

    def test_escapes_in_literals_and_classes(self):
        grammar = r"s <- '\n\r\t\\\'\"\x41\u00e9' [\[\]\-\^]"

        assert matches(grammar, "\n\r\t\\'\"A\u00e9]")

    def test_an_i_that_begins_a_rule_name_is_not_a_flag(self):
        grammar = "s <- 'x'ident\nident <- 'y'"

        assert matches(grammar, "xy")
        assert not matches(grammar, "Xy")

    def test_the_either_case_flag_is_ascii_only(self):
        # "K" is the Kelvin sign, which lower-cases to "k" outside ASCII.
        assert not matches("s <- 'k'i", "\u212a")

    def test_comments_and_rules_over_several_lines(self):
        grammar = """
        # a comment
        s <- 'a'   # another
             'b'
        """

        assert matches(grammar, "ab")

    def test_a_bounded_repeat_of_something_that_can_match_nothing(self):
        assert matches("s <- ('a'?){3}", "")
        assert matches("s <- ('a'?){3}", "aa")


class TestTheParse:
    GRAMMAR = """
    pair  <- key '=' value
    key   <- LETTER+
    value <- LETTER+ / DIGIT+
    LETTER <- [a-z]
    DIGIT <- [0-9]
    """

    def test_lower_case_rules_are_nodes_and_upper_case_ones_are_not(self):
        parse = Grammar(self.GRAMMAR).match("id=42")

        assert parse is not None
        assert [node.name for node in parse.walk()] == ["key", "value"]
        assert parse.child("value").text == "42"
        assert parse.find("LETTER") is None

    def test_find_all_returns_every_match_in_order(self):
        grammar = Grammar("s <- (item ',')*\nitem <- [a-z]+")

        parse = grammar.match("ab,c,")

        assert parse is not None
        assert [node.text for node in parse.find_all("item")] == ["ab", "c"]

    def test_child_says_when_the_grammar_and_its_reader_disagree(self):
        parse = Grammar(self.GRAMMAR).match("id=42")

        assert parse is not None
        with pytest.raises(LookupError):
            parse.child("nothing")

    def test_match_wants_all_of_the_input_and_match_prefix_does_not(self):
        grammar = Grammar(self.GRAMMAR)

        prefix = grammar.match_prefix("id=42 and more")

        assert grammar.match("id=42 and more") is None
        assert prefix is not None
        assert prefix.end == 5

    def test_any_rule_can_be_the_start(self):
        assert Grammar(self.GRAMMAR).match("abc", "key") is not None
        assert Grammar(self.GRAMMAR).rules[0] == "pair"

    def test_an_unknown_start_rule_is_refused(self):
        with pytest.raises(GrammarError):
            Grammar(self.GRAMMAR).match("id=42", "nothing")

    def test_a_remembered_rule_gives_its_node_once_after_backtracking(self):
        grammar = Grammar("s <- word '1' / word '2'\nword <- [a-z]+")

        parse = grammar.match("abc2")

        assert parse is not None
        assert [node.text for node in parse.find_all("word")] == ["abc"]

    def test_a_failed_branch_leaves_no_nodes_behind(self):
        grammar = Grammar("s <- (word '!')? rest\nword <- [a-z]+\nrest <- .*")

        parse = grammar.match("abc?")

        assert parse is not None
        assert [node.name for node in parse.walk()] == ["rest"]


class TestGrammarsThatCannotWork:
    @pytest.mark.parametrize(
        ("grammar", "reason"),
        [
            ("", "no rules"),
            ("# only a comment", "no rules"),
            ("s <- t", "not defined"),
            ("s <- 'a'\ns <- 'b'", "defined twice"),
            ("s 'a'", "expected '<-'"),
            ("s <- /", "expected an expression"),
            ("s <- ('a'", "expected ')'"),
            ("s <- !)", "expected a rule name"),
            ("s <- 'a", "not closed"),
            ("s <- [a", "not closed"),
            ("s <- []", "empty"),
            ("s <- [z-a]", "backwards"),
            (r"s <- '\q'", "unknown escape"),
            (r"s <- '\x4'", "hexadecimal"),
            ("s <- 'a'{}", "expected a number"),
            ("s <- 'a'{3,2}", "upper bound"),
            ("s <- 'a'{2", "expected '}'"),
            ("s <- s 'a'", "left recursion"),
            ("s <- t 'a'\nt <- s", "left recursion"),
            ("s <- 'a'? s", "left recursion"),
            ("s <- ('a'?)*", "without bound"),
            ("s <- (&'a')+", "without bound"),
        ],
    )
    def test_it_is_refused_when_built(self, grammar, reason):
        with pytest.raises(GrammarError, match=re.escape(reason)):
            Grammar(grammar)
