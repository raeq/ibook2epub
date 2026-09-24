"""
Checking an export against the schema shipped beside it.

Deliberately not a JSON Schema library: this tool has no runtime dependencies,
and the parts of a schema worth enforcing at runtime are the required fields
and the patterns a consumer will actually rely on. The full schema is for
consumers, who may use whatever validator they like.

The checks are *derived* from the schema rather than restated beside it.
Restating them is how the annotation export's nested ``book`` came to be
unchecked: the schema required a title there and the checker never looked, so
a book with no identity at all passed the tool's own validator.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def load(path: Path) -> dict[str, Any]:
    """
    Read a shipped schema.

    :param path: The schema file beside the module that owns it.

    :return: The schema, parsed.
    """
    schema: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return schema


def _matches(pattern: re.Pattern[str], value: str) -> bool:
    """
    Match as JSON Schema does, where ``$`` ends the string.

    Python's ``$`` also matches before a final newline, so ``"2026\\n"``
    passed a four-digit pattern that every other validator rejects.
    """
    found = pattern.search(value)
    return found is not None and (
        not pattern.pattern.endswith("$") or found.end() == len(value)
    )


def document_problems(
    document: object, schema: dict[str, Any], items: str, item: str
) -> list[str]:
    """
    Check a whole export: its envelope, and each entry in its list.

    :param document: The document to check.
    :param schema: The schema it should satisfy.
    :param items: The name of the list of entries.
    :param item: The definition under ``$defs`` each entry must satisfy.

    :return: What is wrong with it, empty if nothing is.
    """
    if not isinstance(document, dict):
        # A hand-edited file can hold any JSON, and this raised on a list
        # rather than reporting it.
        return [f"document is a {type(document).__name__}, not an object"]
    problems: list[str] = []
    for name in schema["required"]:
        if name not in document:
            problems.append(f"missing {name}")

    instant = re.compile(schema["properties"]["generated"]["pattern"])
    # str(), like every other check here: a hand-edited document holding a
    # number raised out of the validator whose whole purpose is to report.
    if "generated" in document and not _matches(instant, str(document["generated"])):
        problems.append(f"generated is not an instant: {document['generated']!r}")

    if "generator" in document:
        # Only when it is there: its absence is already reported above, and
        # saying so twice reads as two faults.
        problems.extend(
            object_problems(
                document["generator"],
                schema["properties"]["generator"],
                "generator",
                schema,
            )
        )

    held = document.get(items, [])
    if not isinstance(held, list):
        # Reported, not raised: a dict here used to be walked key by key and
        # blamed on its first entry, and None took the validator down.
        problems.append(f"{items} is {type(held).__name__}, not an array")
        return problems

    rules = schema["$defs"][item]
    for index, entry in enumerate(held):
        problems.extend(object_problems(entry, rules, f"{items}[{index}]", schema))
    return problems


def object_problems(
    value: Any, rules: dict[str, Any], where: str, schema: dict[str, Any] | None = None
) -> list[str]:
    """
    Check one object against one schema definition.

    The three constraints worth enforcing at runtime, taken from the schema
    itself: what must be present, what may be present, and the patterns and
    minimum lengths a consumer will rely on.

    A property that is a ``$ref`` is followed, given the schema to resolve it
    against. Naming the nested object here instead is how the annotation
    export came to check a ``book`` the library export does not have, and to
    report a stray one twice.

    :param value: The object to check.
    :param rules: The schema definition it should satisfy.
    :param where: What to call it in a message.
    :param schema: The whole schema, when nested objects should be followed.

    :return: What is wrong with it.
    """
    if not isinstance(value, dict):
        return [f"{where} is {type(value).__name__}, not an object"]

    problems: list[str] = []
    for name in rules.get("required", []):
        if name not in value:
            problems.append(f"{where} missing {name}")

    properties = rules.get("properties", {})
    for name, rule in properties.items():
        if name not in value:
            continue
        held = value[name]
        if _refers(rule, schema):
            nested = _referenced(rule, schema)
            if nested is None:
                # Silently skipping it would leave a whole nested object
                # unchecked, which is the failure this module exists to
                # prevent: a renamed $defs key would remove a layer of
                # validation and say nothing.
                problems.append(
                    f"{where}.{name} refers to {rule['$ref']}, which "
                    "is not in this schema"
                )
                continue
            problems.extend(object_problems(held, nested, f"{where}.{name}", schema))
            continue
        pattern = rule.get("pattern")
        if pattern and not _matches(re.compile(pattern), str(held)):
            problems.append(f"{where}.{name} does not match {pattern}")
        minimum = rule.get("minLength")
        if minimum is not None and len(str(held)) < minimum:
            problems.append(f"{where}.{name} is shorter than {minimum}")

    if rules.get("additionalProperties") is False:
        extra = set(value) - set(properties)
        if extra:
            problems.append(f"{where} has unknown {sorted(extra)}")
    return problems


#: How a property names a definition in the same schema.
LOCAL_REF = "#/$defs/"


def _refers(rule: dict[str, Any], schema: dict[str, Any] | None) -> bool:
    """
    Whether a property points at a definition this schema should hold.

    :param rule: The property's rule.
    :param schema: The whole schema, or None to follow nothing.

    :return: True when the rule names a local definition.
    """
    reference = rule.get("$ref")
    if schema is None or not isinstance(reference, str):
        return False
    return reference.startswith(LOCAL_REF)


def _referenced(rule: dict[str, Any], schema: dict[str, Any] | None) -> Any:
    """
    Resolve a property that points at a definition in this schema.

    :param rule: The property's rule.
    :param schema: The whole schema.

    :return: The definition, or None when the schema does not hold it.
    """
    if not _refers(rule, schema):
        return None
    assert schema is not None
    return schema["$defs"].get(rule["$ref"][len(LOCAL_REF) :])
