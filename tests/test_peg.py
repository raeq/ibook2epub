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

import random
import re
import time
from collections.abc import Callable

import pytest

from epubconvert.utils import grammars, peg
from epubconvert.utils.peg import Grammar, GrammarError
from tests.conftest import peak_memory


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


class TestInputThatNestsTooDeep:
    """
    Each rule level costs the interpreter a few frames, so input that nests
    without limit would exhaust its stack: a CFI with 100 indirections did
    (#25). Past ``MAX_DEPTH`` levels the input is no match instead.
    """

    GRAMMAR = "nested <- '(' nested ')' / 'x'"

    @staticmethod
    def _nested(levels: int) -> str:
        return "(" * levels + "x" + ")" * levels

    def test_input_at_the_limit_still_matches(self):
        assert Grammar(self.GRAMMAR).match(self._nested(peg.MAX_DEPTH)) is not None

    def test_input_past_the_limit_is_no_match_rather_than_a_crash(self):
        grammar = Grammar(self.GRAMMAR)

        assert grammar.match(self._nested(peg.MAX_DEPTH + 1)) is None
        assert grammar.match(self._nested(5000)) is None

    def test_a_token_that_nests_counts_towards_the_limit(self):
        grammar = Grammar("s <- NESTED\nNESTED <- '(' NESTED ')' / 'x'")

        assert grammar.match(self._nested(5000)) is None


class TestALongInputHoldsNoMemoryPerCharacter:
    """
    Every rule's result was remembered at every position, so a token repeated
    over a long input held an entry per character: 170 MB for a million (#26).
    """

    def test_a_long_run_of_a_token(self):
        grammar = Grammar("run <- A* !.\nA <- [a]")
        text = "a" * 100_000

        assert peak_memory(lambda: grammar.match(text)) < 1_000_000

    def test_a_long_run_of_a_character_behind_a_lookahead(self):
        # Scanned by a regular expression repeat, a run like this held the
        # regex engine's backtracking record for every character (#27).
        grammar = Grammar("run <- (!':~:' .)* !.")
        text = "a" * 100_000

        assert peak_memory(lambda: grammar.match(text)) < 1_000_000


def _fastest(call: Callable[[], object]) -> float:
    """The quickest of three runs of *call*, in seconds."""
    times = []
    for _ in range(3):
        start = time.perf_counter()
        call()
        times.append(time.perf_counter() - start)
    return min(times)


class TestARunOfOneCharacterIsScannedInOneCall:
    """
    A repeat cost several Python calls per character, 930 times what ``re``
    takes over a run of a million (#27). A repeat of something that consumes
    one character at a time is scanned by a regular expression now, and it must
    answer exactly as the per-call path does.
    """

    #: Repeats that take the scan, and some that must not: a choice whose
    #: options differ in width, and a repeat inside a lookahead.
    EXTRA = [
        "s <- [a-c]* !.",
        "s <- [^a-c]{2,4} .*",
        "s <- ('x'i)+ 'y'?",
        "s <- (!'ab' .)* 'ab'?",
        "s <- (&[a-m] .)+ .*",
        "s <- ('a' / 'b' / [c-d])* !.",
        "s <- ('\\u00e9' / .)*",
        "s <- HEX{3} HEX{2,}\nHEX <- [0-9A-Fa-f]",
        "s <- (!END .)* END?\nEND <- ':~:' / !.",
        "s <- (!('a' / 'bc') .)*",
        "s <- (!('a'+ 'b') .)* .*",
        "s <- ('ab')* 'a'?",
    ]
    #: Real inputs to mutate, and the pieces a mutation inserts.
    SEEDS = [
        "epubcfi(/6/46[ch15.xhtml]!/4,/80/2/1:25,/82/2/1:25)",
        "book.epub#epubcfi(/6/44[n-1]!,/4:0,/4/16[p7]:0)",
        "epubcfi(/6/46[ch^[15^].xhtml;s=b]!/4/2/1:0.5)",
        "urn:isbn:978-0-553-38304-1",
        "ISBN 0-553-38304-X",
        "urn:uuid:0f7e4b56-3b1f-4c5e-9d8a-1a2b3c4d5e6f",
        "name:~:text=foo",
        "t=10,20&track=a",
        "xywh=0,0,5,5",
        "svgView(viewBox(0,0,1,1))",
        " 3.0 ",
        "nav cover-image x:y",
        "<!-- ibook2epub sha256=0123456789abcdef -->  ",
        "<!-- ibook2epub end",
        "---",
        "  12. item",
    ]
    PIECES = [
        *"abcxXy0F3-_.:;,=()[]^!/@~&# \t é",
        "ab",
        "bc",
        ":~:",
        "epubcfi(",
        "urn:isbn:",
        "svgView(",
        "t=",
        "xywh=",
        " -->",
        "---",
    ]

    @classmethod
    def _texts(cls, seed: str) -> list[str]:
        generator = random.Random(seed)
        texts = []
        for _ in range(300):
            text = generator.choice(cls.SEEDS) if generator.random() < 0.5 else ""
            for _ in range(generator.randint(0, 4)):
                at = generator.randint(0, len(text))
                if text and generator.random() < 0.4:
                    text = text[:at] + text[at + 1 :]
                else:
                    text = text[:at] + generator.choice(cls.PIECES) + text[at:]
            texts.append(text)
        return texts

    @staticmethod
    def _assert_agree(source: str, texts: list[str], monkeypatch) -> None:
        scanning = Grammar(source)
        monkeypatch.setattr(peg, "_SCAN_RUNS", False)
        per_call = Grammar(source)
        for text in texts:
            for rule in scanning.rules:
                assert scanning.match(text, rule) == per_call.match(text, rule), (
                    rule,
                    text,
                )
                assert scanning.match_prefix(text, rule) == per_call.match_prefix(
                    text, rule
                ), (rule, text)

    def test_a_long_run_takes_little_longer_than_re(self):
        grammar = Grammar("run <- A* !.\nA <- [a]")
        pattern = re.compile("[a]*")
        text = "a" * 1_000_000

        engine = _fastest(lambda: grammar.match(text))
        regex = _fastest(lambda: pattern.fullmatch(text))

        # It was 930 times; the slack is for the parse's fixed cost and a busy
        # machine.
        assert engine < 10 * regex + 0.02

    @pytest.mark.parametrize(
        "name", ["CFI", "FRAGMENTS", "IDENTIFIERS", "NOTES", "PACKAGE"]
    )
    def test_the_scan_answers_as_the_per_call_path_does(self, name, monkeypatch):
        source = getattr(grammars, name).source
        self._assert_agree(source, self._texts(name), monkeypatch)

    @pytest.mark.parametrize("source", EXTRA)
    def test_the_scan_and_the_per_call_path_agree_on_the_notation(
        self, source, monkeypatch
    ):
        self._assert_agree(source, self._texts(source), monkeypatch)


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
