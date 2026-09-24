"""
Tests for the runtime schema check both exports share.

``epubconvert.utils.schema`` is the tool's own validator: not a JSON Schema library,
but the parts of a shipped schema a consumer will rely on, derived from the
schema rather than restated beside it. Its whole purpose is to report what is
wrong with a document, so every test here hands it something malformed and
asserts a problem rather than an exception.
"""

# Test names describe the behaviour under test; separate docstrings would only
# restate them. Explicit empty-list comparisons read better than truthiness here.
# pylint: disable=missing-function-docstring,missing-class-docstring
# pylint: disable=use-implicit-booleaness-not-comparison,too-few-public-methods

from typing import Any

import pytest

from epubconvert.collect import annotations
from epubconvert.utils import schema as epubconvert_schema


def _document(found: list[dict[str, Any]]) -> dict[str, Any]:
    """An annotation document carrying exactly *found*."""
    return annotations.build_document(found)


class TestTheSchemaCheckActuallyChecks:
    """A validator that never rejects anything is not a validator."""

    def test_a_missing_required_field_is_reported(self):
        document = annotations.build_document([])
        del document["generator"]

        assert annotations.schema_problems(document) == ["missing generator"]

    def test_a_trailing_newline_does_not_satisfy_a_pattern(self):
        # re.match lets "$" match before a final newline; the schema's
        # patterns are JSON Schema's, which do not.
        document = annotations.build_document([])
        document["generated"] = f"{document['generated']}\n"

        assert annotations.schema_problems(document) == [
            f"generated is not an instant: {document['generated']!r}"
        ]

    def test_a_field_with_a_trailing_newline_is_reported(self):
        rules = {"properties": {"year": {"pattern": "^[0-9]{4}$"}}}
        schema: dict[str, dict[str, object]] = {"$defs": {}}

        assert epubconvert_schema.object_problems(
            {"year": "2026\n"}, rules, "x", schema
        ) == ["x.year does not match ^[0-9]{4}$"]

    def test_a_document_that_is_not_an_object_is_reported(self):
        # A hand-edited file can hold any JSON; the checker raised on a list
        # rather than saying what was wrong with it.
        problems = annotations.schema_problems([])  # type: ignore[arg-type]

        assert problems == ["document is a list, not an object"]

    def test_a_set_with_no_generation_stamp_is_still_valid(self):
        # An embedded set carries none: a stamp that moves on every run makes
        # the archive holding it stop being byte-reproducible.
        document = annotations.build_document([], stamped=False)

        assert "generated" not in document
        assert annotations.schema_problems(document) == []

    def test_a_malformed_generator_is_reported(self):
        # Both schemas require a name and a version there, and the check was
        # never made: the envelope was checked for the field's presence only.
        document = annotations.build_document([])
        document["generator"] = {"name": "ibook2epub", "version": "banana"}

        problems = annotations.schema_problems(document)

        assert any("generator.version" in problem for problem in problems)

    def test_a_reference_to_a_missing_definition_is_reported(self):
        # Silently skipping it would leave the whole nested object unchecked,
        # so renaming a $defs key would remove a layer of validation and say
        # nothing -- the failure the runtime check exists to prevent.
        rules = {"properties": {"book": {"$ref": "#/$defs/absent"}}}
        schema: dict[str, dict[str, object]] = {"$defs": {}}

        problems = epubconvert_schema.object_problems(
            {"book": {"title": "T"}}, rules, "x", schema
        )

        assert problems == [
            "x.book refers to #/$defs/absent, which is not in this schema"
        ]

    def test_a_reference_to_another_document_is_left_to_the_consumer(self):
        # Not this validator's to resolve, and not a fault either.
        elsewhere = {"properties": {"book": {"$ref": "https://example/x.json"}}}
        schema: dict[str, dict[str, object]] = {"$defs": {}}

        assert (
            epubconvert_schema.object_problems(
                {"book": "not an object"}, elsewhere, "x", schema
            )
            == []
        )

    @pytest.mark.parametrize("held", [None, {"a": 1}, "text", 7])
    def test_entries_that_are_not_an_array_are_reported_not_raised(self, held):
        # None took the validator down; a dict was walked key by key and
        # blamed on its first entry.
        document = annotations.build_document([])
        document["annotations"] = held

        problems = annotations.schema_problems(document)

        assert problems == [f"annotations is {type(held).__name__}, not an array"]

    @pytest.mark.parametrize("stamp", [20260909, None, ["2026-09-09T00:00:00Z"]])
    def test_a_generated_stamp_that_is_not_text_is_reported_not_raised(self, stamp):
        # The validator's whole purpose is to report what is wrong, and a
        # hand-edited document holding a number raised out of it instead.
        document = annotations.build_document([])
        document["generated"] = stamp

        assert any(
            "not an instant" in problem
            for problem in annotations.schema_problems(document)
        )

    def test_a_malformed_instant_is_reported(self):
        document = annotations.build_document([])
        document["generated"] = "yesterday"

        assert any(
            "not an instant" in problem
            for problem in annotations.schema_problems(document)
        )

    def test_an_annotation_missing_a_required_field_is_reported(self):
        document = annotations.build_document(
            [{"id": "A", "book": {"title": "T"}, "created": "2018-12-25T22:44:28Z"}]
        )

        assert annotations.schema_problems(document) == ["annotations[0] missing text"]

    def test_an_annotation_without_a_creation_date_is_still_valid(self):
        # Optional, because stamping one in made an embedded set move with
        # the clock.
        document = annotations.build_document(
            [{"id": "A", "book": {"title": "T"}, "text": "x"}]
        )

        assert annotations.schema_problems(document) == []

    def test_a_locator_that_is_not_a_text_fragment_is_reported(self):
        document = annotations.build_document(
            [
                {
                    "id": "A",
                    "book": {"title": "T"},
                    "text": "x",
                    "created": "2018-12-25T22:44:28Z",
                    "locator": "epubcfi(/6/4)",
                }
            ]
        )

        assert any(
            "locator does not match" in problem
            for problem in annotations.schema_problems(document)
        )

    def test_an_unknown_field_is_reported(self):
        document = annotations.build_document(
            [
                {
                    "id": "A",
                    "book": {"title": "T"},
                    "text": "x",
                    "created": "2018-12-25T22:44:28Z",
                    "colour": "yellow",
                }
            ]
        )

        assert any(
            "unknown ['colour']" in problem
            for problem in annotations.schema_problems(document)
        )
