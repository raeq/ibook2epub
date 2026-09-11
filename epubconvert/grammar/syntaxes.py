"""
The grammars of the small syntaxes ibook2epub reads.

Each grammar here is transcribed from the specification that defines its
syntax, which it names, so the two can be read side by side, and
``tests/test_grammars.py`` runs each one against the examples its specification
gives, apart from any code that uses it. That code walks the parse a grammar
returns rather than cutting strings up itself.

URLs, CSS, XML and HTML's attribute syntaxes are not here. Their specifications
define parsing as an algorithm, with error recovery a grammar does not express,
and ibook2epub parses them that way: :mod:`epubconvert.utils.url` for URLs and
expat for XML.
"""

from __future__ import annotations

from .engine import Grammar

__all__ = ["CFI", "FRAGMENTS", "IDENTIFIERS", "NOTES", "PACKAGE", "WHITESPACE"]

#: The characters :meth:`str.isspace` calls whitespace. The code these grammars
#: replaced stripped and split on exactly these, so they read input the same way.
WHITESPACE = (
    r"[ \t\n\x0b\x0c\r\x1c-\x1f\x85\xa0\u1680\u2000-\u200a"
    r"\u2028\u2029\u202f\u205f\u3000]"
)

IDENTIFIERS = Grammar(
    r"""
# A dc:identifier as publishers write it, and the part ibook2epub can verify.
# EPUB leaves the identifier an opaque string. A UUID is RFC 9562's string form
# (section 4: 8-4-4-4-12 hexadecimal digits). An ISBN is ISO 2108's thirteen
# digits, or ten with X allowed as the check digit; publishers print hyphens or
# spaces between the digits. In front of either they write "urn:isbn:",
# "URN:ISBN:", "ISBN " and the like, and RFC 8141 makes the "urn" scheme and
# its namespace case-insensitive. Check digits are arithmetic, so the code
# reading the parse checks them.

identifier     <- WS* label? value !.
label          <- 'urn:'i? ('isbn'i / 'uuid'i / 'ean'i) (':' / WS)+
value          <- uuid WS* / isbn13 / isbn10
uuid           <- HEX{8} '-' HEX{4} '-' HEX{4} '-' HEX{4} '-' HEX{12}
isbn13         <- SEP* digit (SEP* digit){12} SEP*
isbn10         <- SEP* digit (SEP* digit){8} SEP* check SEP*
digit          <- [0-9]
check          <- check_digit / ten
check_digit    <- [0-9]
ten            <- [Xx]

# The form canonical_identifier writes for an ISBN, and the ISBN-13s that also
# have an ISBN-10: those in the 978 prefix, the only one ISBN-10s were issued in.
canonical_isbn <- 'urn:isbn:' isbn !.
isbn           <- [0-9]{13}
bookland       <- '978' isbn10_body [0-9] !.
isbn10_body    <- [0-9]{9}

SEP            <- '-' / WS
HEX            <- [0-9A-Fa-f]
WS             <- """
    + WHITESPACE
    + "\n"
)

CFI = Grammar(
    r"""
# EPUB Canonical Fragment Identifiers 1.1, the EBNF of its syntax section,
# rule for rule. Where the EBNF relies on backtracking a PEG does not do, the
# rule is written to match the same strings: a number's fraction must end in a
# non-zero digit, so it is written as runs of zeros each closed by one, and
# "value-no-space" is written directly rather than as "value" minus a space.
#
# A CFI is used as the fragment of a book's address ("book.epub#epubcfi(...)"),
# and Apple Books stores it that way for some books, so "location" takes one
# with or without that address. One departure from the EBNF: Apple Books writes
# a range whose shared part ends at an indirection ("...!,/4:0,/4/16:0"), which
# the EBNF does not allow; "redirected_path" allows it where a range follows,
# and nowhere else.

location        <- cfi / (!'#' .)* '#' cfi
cfi             <- 'epubcfi(' path range? ')' !.
path            <- step local_path
range           <- ',' local_path ',' local_path
local_path      <- step* (redirected_path / offset?)
redirected_path <- '!' (offset / path / &',')
step            <- '/' INTEGER ('[' assertion ']')?
offset          <- (':' INTEGER / '@' NUMBER ':' NUMBER
                    / '~' NUMBER ('@' NUMBER ':' NUMBER)?) ('[' assertion ']')?
assertion       <- (value (',' value)? / ',' value / parameter) parameter*
parameter       <- ';' NAME '=' value (',' value)*
value           <- (escaped / plain)+
escaped         <- '^' special
special         <- SPECIAL
plain           <- (!SPECIAL .)+

NAME            <- ('^' SPECIAL / ![ ] !SPECIAL .)+
NUMBER          <- [1-9] [0-9]* FRACTION? / '0' FRACTION?
FRACTION        <- '.' ('0'* [1-9])+
INTEGER         <- '0' / [1-9] [0-9]*
SPECIAL         <- [\^\[\](),;=]
"""
)

FRAGMENTS = Grammar(
    r"""
# Fragment identifiers into XHTML and SVG, sorted as epubcheck 5.3.0 sorts them
# (URLFragment.java) before it looks for an id. In XHTML: a fragment directive
# (URL Fragment Text Directives, "#name:~:text=...") is set aside; then a
# scheme-based pointer ("epubcfi(...)", "xpointer(...)") or a media fragment
# (Media Fragments URI 1.0: t, xywh, track and id, and the xyn and xyr that
# epubcheck also accepts) names no id. In SVG, the part before the first "&":
# a view specification ("svgView(...)"), a time or space media fragment, or
# anything else holding "=" names no id. What is left is a bare name.

html_fragment  <- (scheme_based / media_fragment / html_name)? directive? !.
scheme_based   <- WORD+ '(' (!(')' END) !':~:' .)* ')' &END
media_fragment <- MEDIA_KEY '=' VALUE ('&' KEY '=' VALUE)* &END
html_name      <- (!':~:' .)+
directive      <- ':~:' .*

svg_fragment   <- (svg_view / svg_media / svg_keyed / svg_name)? ('&' .*)? !.
svg_view       <- 'svgView(' COMPONENT
svg_media      <- ('t=' / 'xywh=') COMPONENT
svg_keyed      <- (![&=] .)* '=' COMPONENT
svg_name       <- COMPONENT

END            <- &':~:' / !.
MEDIA_KEY      <- 'track' / 'xywh' / 'xyn' / 'xyr' / 'id' / 't'
VALUE          <- (!'&' !':~:' .)+
KEY            <- (![&=] !':~:' .)+
COMPONENT      <- (!'&' .)*
WORD           <- [A-Za-z0-9_]
"""
)

PACKAGE = Grammar(
    r"""
# Two attribute values of the package document. "version" names the EPUB
# version the package conforms to ("3.0" in EPUB 3, "2.0" in OPF 2.0.1), read
# here only for whether it is 3. "properties" is a list of property values
# separated by white space, each an optional prefix and colon and then a
# reference (EPUB 3.3's property data type).

epub3_version  <- XML_WS* '0'* '3' ('.' [0-9]+)* XML_WS* !.
properties     <- XML_WS* (property (XML_WS+ property)*)? XML_WS* !.
property       <- (prefix ':')? reference
prefix         <- [A-Za-z_] [A-Za-z0-9_.-]*
reference      <- (!XML_WS .)+

XML_WS         <- [ \t\r\n]
"""
)

NOTES = Grammar(
    r"""
# The lines ibook2epub writes around the part of a Markdown note it owns, the
# frontmatter fence above them, and the characters that open a block at the
# start of a Markdown line (CommonMark: ATX headings, block quotes, bullet list
# items, and ordered list items, numbered in ASCII digits), which it escapes in
# text it did not write. The end marker is matched on a stable prefix, so its
# human-readable tail can be reworded without orphaning a note already in a
# vault. White space may follow a marker, as an editor may leave it.

start_marker   <- '<!-- ibook2epub sha256=' digest ' -->' WS* !.
digest         <- [0-9a-f]{16,64}
end_marker     <- '<!-- ibook2epub end'
fence          <- '---' WS* !.
block_opener   <- WS* opener
opener         <- [#>+*-] / [0-9]+ [.)]

WS             <- """
    + WHITESPACE
    + "\n"
)
