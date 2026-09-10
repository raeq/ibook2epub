"""
Conformance of the URL Standard parser in ``epubconvert.utils.url``,
``epubconvert.utils.urlhost`` and ``epubconvert.utils.percent``.

The cases in ``tests/data/urltestdata.json`` are the web-platform-tests URL
suite, the one browsers are held to: copied unchanged from
web-platform-tests/wpt ``url/resources/urltestdata.json`` at commit c23755a
(2026-08-28), under that project's 3-Clause BSD License. All 893 cases run and
none is skipped, so a case that stops passing is a regression against the
standard rather than a matter of opinion.

The validation-error cases are the examples the standard gives in its own
table of validation errors, and the writing cases are its table of valid and
invalid strings. A renamed or lost error fails here under the standard's name.
"""

# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=too-few-public-methods
# The step count is read through the parser's own table of state handlers.
# pylint: disable=protected-access

from __future__ import annotations

import json
from pathlib import Path

import pytest

from epubconvert.utils import percent, url, urlhost

DATA = Path(__file__).parent / "data" / "urltestdata.json"
CASES = [
    case
    for case in json.loads(DATA.read_text(encoding="utf-8"))
    if isinstance(case, dict)
]
FIELDS = (
    "href",
    "protocol",
    "username",
    "password",
    "host",
    "hostname",
    "port",
    "pathname",
    "search",
    "hash",
    "origin",
)


def _components(parsed: url.URL) -> dict[str, str]:
    """The URL API's getters, which is what the suite records."""
    return {
        "href": parsed.href,
        "protocol": parsed.protocol,
        "username": parsed.username,
        "password": parsed.password,
        "host": parsed.host_and_port,
        "hostname": parsed.hostname,
        "port": parsed.port_string,
        "pathname": parsed.pathname,
        "search": parsed.search,
        "hash": parsed.hash,
        "origin": parsed.origin,
    }


def _parse(text: str, base: str | None = None) -> url.Parsed:
    base_url = url.parse(base).url if base is not None else None
    return url.parse(text, base_url)


class TestWebPlatformTests:
    """Every case of the suite browsers are measured against."""

    def test_the_suite_is_the_full_one(self):
        # A truncated copy would pass trivially.
        assert len(CASES) == 893

    @pytest.mark.parametrize("case", CASES, ids=[f"wpt-{i}" for i in range(len(CASES))])
    def test_case(self, case):
        base = None
        if case.get("base") is not None:
            base = url.parse(case["base"]).url
            if base is None:
                # The URL constructor throws when its base does not parse.
                assert case.get("failure"), case
                return
        result = url.parse(case["input"], base)
        if case.get("failure"):
            assert result.failed, (case["input"], result.url and result.url.href)
            return
        assert result.url is not None, (case["input"], result.errors)
        got = _components(result.url)
        expected = {field: case[field] for field in FIELDS if field in case}
        assert {field: got[field] for field in expected} == expected


#: (input, base, error, whether the parse fails), from the standard's table.
VALIDATION_ERRORS = [
    ("https://exa%23mple.org", None, urlhost.DOMAIN_TO_ASCII, True),
    ("https://exam%70le.org", None, urlhost.DOMAIN_PERCENT_ENCODED, False),
    ("foo://exa[mple.org", None, urlhost.HOST_INVALID_CODE_POINT, True),
    ("https://127.0.0.1./", None, urlhost.IPV4_EMPTY_PART, False),
    ("https://1.2.3/", None, urlhost.IPV4_TOO_FEW_PARTS, False),
    ("https://1.2.3.4.5/", None, urlhost.IPV4_TOO_MANY_PARTS, True),
    ("https://test.42", None, urlhost.IPV4_NON_NUMERIC_PART, True),
    ("https://127.0.0x0.1", None, urlhost.IPV4_NON_DECIMAL_PART, False),
    ("https://255.255.4000.1", None, urlhost.IPV4_OUT_OF_RANGE_PART, True),
    ("https://①.②.③.④", None, urlhost.IPV4_NON_ASCII_INPUT, False),
    ("https://[::1", None, urlhost.IPV6_UNCLOSED, True),
    ("https://[:1]", None, urlhost.IPV6_INVALID_COMPRESSION, True),
    ("https://[1:2:3:4:5:6:7:8:9]", None, urlhost.IPV6_TOO_MANY_PIECES, True),
    ("https://[1::1::1]", None, urlhost.IPV6_MULTIPLE_COMPRESSION, True),
    ("https://[1:2:3!:4]", None, urlhost.IPV6_INVALID_CODE_POINT, True),
    ("https://[1:2:3:]", None, urlhost.IPV6_INVALID_CODE_POINT, True),
    ("https://[1:2:3]", None, urlhost.IPV6_TOO_FEW_PIECES, True),
    ("https://[::01]", None, urlhost.IPV6_PIECE_LEADING_ZERO, False),
    (
        "https://[1:1:1:1:1:1:1:127.0.0.1]",
        None,
        urlhost.IPV4_IN_IPV6_TOO_MANY_PIECES,
        True,
    ),
    ("https://[ffff::.0.0.1]", None, urlhost.IPV4_IN_IPV6_INVALID_CODE_POINT, True),
    (
        "https://[ffff::127.0.xyz.1]",
        None,
        urlhost.IPV4_IN_IPV6_INVALID_CODE_POINT,
        True,
    ),
    ("https://[ffff::127.0xyz]", None, urlhost.IPV4_IN_IPV6_INVALID_CODE_POINT, True),
    ("https://[ffff::127.00.0.1]", None, urlhost.IPV4_IN_IPV6_INVALID_CODE_POINT, True),
    (
        "https://[ffff::127.0.0.1.2]",
        None,
        urlhost.IPV4_IN_IPV6_INVALID_CODE_POINT,
        True,
    ),
    (
        "https://[ffff::127.0.0.4000]",
        None,
        urlhost.IPV4_IN_IPV6_OUT_OF_RANGE_PART,
        True,
    ),
    ("https://[ffff::127.0.0]", None, urlhost.IPV4_IN_IPV6_TOO_FEW_PARTS, True),
    ("https://example.org/>", None, url.INVALID_URL_UNIT, False),
    (" https://example.org ", None, url.INVALID_URL_UNIT, False),
    ("ht\ntps://example.org", None, url.INVALID_URL_UNIT, False),
    ("https://example.org/%s", None, url.INVALID_URL_UNIT, False),
    (
        "file:c:/my-secret-folder",
        None,
        url.SPECIAL_SCHEME_MISSING_FOLLOWING_SOLIDUS,
        False,
    ),
    ("https:example.org", None, url.SPECIAL_SCHEME_MISSING_FOLLOWING_SOLIDUS, False),
    (
        "https:foo.html",
        "https://example.org/",
        url.SPECIAL_SCHEME_MISSING_FOLLOWING_SOLIDUS,
        False,
    ),
    ("💩", None, url.MISSING_SCHEME_NON_RELATIVE_URL, True),
    ("💩", "mailto:user@example.org", url.MISSING_SCHEME_NON_RELATIVE_URL, True),
    ("https://example.org\\path\\to\\file", None, url.INVALID_REVERSE_SOLIDUS, False),
    ("https://user@example.org", None, url.INVALID_CREDENTIALS, False),
    ("ssh://user@example.org", None, url.INVALID_CREDENTIALS, False),
    ("https://#fragment", None, url.HOST_MISSING, True),
    ("https://:443", None, url.HOST_MISSING, True),
    ("https://user:pass@", None, url.HOST_MISSING, True),
    ("https://example.org:70000", None, url.PORT_OUT_OF_RANGE, True),
    ("https://example.org:7z", None, url.PORT_INVALID, True),
    # The standard's own example, "/c:/path/to/file" against "file:///c:/",
    # passes through the file slash state and never reaches the step that
    # records this error; a drive letter written with "|" reaches it.
    ("c|/path/to/file", "file:///c:/", url.FILE_INVALID_WINDOWS_DRIVE_LETTER, False),
    ("file://c:", None, url.FILE_INVALID_WINDOWS_DRIVE_LETTER_HOST, False),
]


class TestValidationErrors:
    """The standard's table of validation errors, example by example."""

    @pytest.mark.parametrize(("text", "base", "error", "fails"), VALIDATION_ERRORS)
    def test_the_example_records_its_error(self, text, base, error, fails):
        result = _parse(text, base)

        assert error in result.errors
        assert result.failed is fails
        assert not result.valid


#: (input, base, valid, serialization or None for failure), from the
#: standard's URL writing table.
WRITING = [
    ("https:example.org", None, False, "https://example.org/"),
    ("https://////example.com///", None, False, "https://example.com///"),
    ("https://example.com/././foo", None, True, "https://example.com/foo"),
    ("hello:world", "https://example.com/", True, "hello:world"),
    (
        "https:example.org",
        "https://example.com/",
        False,
        "https://example.com/example.org",
    ),
    (
        "\\example\\..\\demo/.\\",
        "https://example.com/",
        False,
        "https://example.com/demo/",
    ),
    ("example", "https://example.com/demo", True, "https://example.com/example"),
    ("file:///C|/demo", None, False, "file:///C:/demo"),
    ("..", "file:///C:/demo", True, "file:///C:/"),
    ("file://localhost/", None, True, "file:///"),
    ("file://loc%61lhost/", None, False, "file:///"),
    (
        "https://user:password@example.org/",
        None,
        False,
        "https://user:password@example.org/",
    ),
    ("https://example.org/foo bar", None, False, "https://example.org/foo%20bar"),
    ("https://EXAMPLE.com/../x", None, True, "https://example.com/x"),
    ("https://ex ample.org/", None, False, None),
    ("example", None, False, None),
    ("https://example.com:demo", None, False, None),
    ("http://[www.example.com]/", None, False, None),
    ("https://example.org//", None, True, "https://example.org//"),
    ("https://example.com/[]?[]#[]", None, False, "https://example.com/[]?[]#[]"),
    ("https://example/%?%#%", None, False, "https://example/%?%#%"),
    ("https://example/%25?%25#%25", None, True, "https://example/%25?%25#%25"),
]


class TestUrlWriting:
    """What counts as a valid URL string, per the standard's own examples."""

    @pytest.mark.parametrize(("text", "base", "valid", "output"), WRITING)
    def test_the_example(self, text, base, valid, output):
        result = _parse(text, base)

        assert result.valid is valid
        assert (result.url.href if result.url else None) == output


class TestDomains:
    """Strict domain checks that report without failing, as the standard does."""

    def test_a_label_over_63_characters_parses_but_is_not_valid(self):
        # From a real book: a path written with hyphens instead of slashes, so
        # the whole path landed in one 92-character label of the host.
        host = "www.fda.gov-Food-FoodSafety-HazardAnalysisCriticalControlPointsHACCP"
        result = url.parse(f"http://{host}-HACCPPrinciplesApplicationGuide")

        assert result.url is not None
        assert result.errors == (urlhost.DOMAIN_TO_ASCII,)

    def test_a_name_over_253_characters_is_not_valid(self):
        name = ".".join(["a" * 60] * 5)

        assert urlhost.DOMAIN_TO_ASCII in url.parse(f"http://{name}/").errors

    @pytest.mark.parametrize("label", ["-a", "a-", "ab--c", "exa_mple"])
    def test_hyphen_and_std3_rules(self, label):
        assert url.parse(f"http://{label}.example/").errors == (
            urlhost.DOMAIN_TO_ASCII,
        )

    def test_a_canonical_punycode_label_is_valid(self):
        assert url.parse("http://xn--fa-hia.example/").valid

    def test_punycode_that_decodes_to_something_mapped_is_not_valid(self):
        # The standard's note: xn--8i7caa decodes to fullwidth "www", whose code
        # points map, so the label is not the canonical form of anything.
        result = url.parse("http://xn--8i7caa/")

        assert result.url is not None
        assert result.url.hostname == "xn--8i7caa"
        assert urlhost.DOMAIN_TO_ASCII in result.errors

    # "xn--99" stops in the middle of a number, which the codec itself refuses.
    @pytest.mark.parametrize("label", ["xn--", "xn--a", "xn--abc-", "xn--99"])
    def test_broken_punycode_is_not_valid(self, label):
        assert urlhost.DOMAIN_TO_ASCII in url.parse(f"http://{label}.example/").errors

    def test_sharp_s_is_kept_as_uts46_keeps_it(self):
        result = url.parse("https://faß.example/")

        assert result.url is not None
        assert result.url.hostname == "xn--fa-hia.example"

    def test_a_right_to_left_label_mixed_with_left_to_right_fails(self):
        assert url.parse("http://\u05d0a\u05d1.example/").failed

    def test_a_prohibited_code_point_fails(self):
        assert url.parse("http://a\ufffdb.example/").failed

    def test_a_label_that_maps_to_nothing_fails(self):
        assert url.parse("http://\u00ad.example/").failed


class TestLinearTime:
    """A long URL costs one step per run of code points, not one per code point."""

    @pytest.mark.parametrize(
        "text",
        [
            "data:image/png;base64," + "A" * 100_000,
            "http://" + "a" * 100_000 + "/",
            "http://example.org/" + "a" * 100_000,
            "http://example.org/?" + "a" * 100_000,
            "http://example.org/#" + "a" * 100_000,
            "a" * 100_000 + ":x",
            "file://" + "a" * 100_000 + "/",
            "sc://" + "a" * 100_000 + "/",
            "http://user" + "a" * 100_000 + "@example.org/",
            "http://example.org:" + "0" * 100_000 + "80/",
        ],
        ids=[
            "opaque-path",
            "host",
            "path",
            "query",
            "fragment",
            "scheme",
            "file-host",
            "opaque-host",
            "credentials",
            "port",
        ],
    )
    def test_each_state_takes_a_run_in_one_step(self, text, monkeypatch):
        steps = 0
        for state, handler in list(url._HANDLERS.items()):

            def counted(machine, c, handler=handler):
                nonlocal steps
                steps += 1
                handler(machine, c)

            monkeypatch.setitem(url._HANDLERS, state, counted)

        result = url.parse(text)

        assert result.url is not None
        assert steps < 50

    def test_a_port_of_thousands_of_digits_is_out_of_range_not_an_exception(self):
        # int() refuses decimal strings longer than 4,300 digits.
        result = url.parse("http://example.org:" + "9" * 5000 + "/")

        assert result.failed
        assert url.PORT_OUT_OF_RANGE in result.errors

    def test_an_ipv4_number_of_thousands_of_digits_is_out_of_range(self):
        result = url.parse("http://" + "9" * 5000 + "/")

        assert result.failed
        assert urlhost.IPV4_OUT_OF_RANGE_PART in result.errors


class TestHelpers:
    """The small public pieces the parser is built from."""

    def test_percent_decode_leaves_a_stray_percent(self):
        assert percent.percent_decode(b"%25%s%1G") == b"%%s%1G"

    def test_percent_decode_decodes_bytes(self):
        assert percent.percent_decode("‽%25%2E".encode()) == b"\xe2\x80\xbd%."

    def test_utf8_encode_replaces_a_lone_surrogate(self):
        assert percent.utf8_encode("a\ud800b") == b"a\xef\xbf\xbdb"

    def test_utf8_decode_keeps_a_bom_and_replaces_bad_bytes(self):
        assert percent.utf8_decode_without_bom(b"\xef\xbb\xbfa\xff") == "\ufeffa\ufffd"

    def test_utf8_percent_encode_encodes_the_set_and_all_non_ascii(self):
        assert (
            percent.utf8_percent_encode("a b/\u00e9", frozenset(" /"))
            == "a%20b%2F%C3%A9"
        )

    def test_ipv6_compresses_the_first_longest_run(self):
        # The standard's own note: in 0:f:0:0:f:f:0:0 it is the second 0.
        address = url.IPv6Address((0, 0xF, 0, 0, 0xF, 0xF, 0, 0))

        assert url.serialize_host(address) == "[0:f::f:f:0:0]"

    def test_the_empty_host_serializes_empty(self):
        assert url.serialize_host(url.EmptyHost()) == ""

    def test_an_empty_host_does_not_end_in_a_number(self):
        # The host parser never passes one; the standard's step is kept anyway.
        assert not urlhost.ends_in_a_number("")

    def test_an_empty_ipv4_host_fails_with_the_standards_errors(self):
        errors: list[str] = []

        assert urlhost.parse_ipv4("", errors) is None
        assert errors == [
            urlhost.IPV4_EMPTY_PART,
            urlhost.IPV4_TOO_FEW_PARTS,
            urlhost.IPV4_NON_NUMERIC_PART,
        ]

    def test_a_lone_surrogate_becomes_the_replacement_character(self):
        result = url.parse("https://example.org/\ud800")

        assert result.url is not None
        assert result.url.pathname == "/%EF%BF%BD"

    def test_an_opaque_path_keeps_a_space_before_a_query(self):
        result = url.parse("sc:a ?q")

        assert result.url is not None
        assert result.url.pathname == "a%20"
        assert url.INVALID_URL_UNIT in result.errors

    def test_serialize_can_leave_the_fragment_off(self):
        result = url.parse("https://example.org/a#b")

        assert result.url is not None
        assert result.url.serialize(exclude_fragment=True) == "https://example.org/a"

    def test_a_blob_url_takes_the_origin_of_its_path(self):
        result = url.parse("blob:https://example.org:8443/uuid")

        assert result.url is not None
        assert result.url.origin == "https://example.org:8443"

    def test_a_blob_of_something_else_is_opaque(self):
        result = url.parse("blob:about:blank")

        assert result.url is not None
        assert result.url.origin == "null"
