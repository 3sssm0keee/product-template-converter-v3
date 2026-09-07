from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def load_schema(path: Path) -> dict[str, Any]:
    schema = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(schema, dict) or schema.get("$schema") != JSON_SCHEMA_DIALECT:
        raise ValueError(f"not a Draft 2020-12 JSON Schema: {path}")
    return schema


def _pointer(root: dict[str, Any], reference: str) -> Any:
    if not reference.startswith("#/"):
        raise ValueError(f"only local JSON Pointer references are supported: {reference}")
    current: Any = root
    for token in reference[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        current = current[token]
    return current


def _json_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise ValueError(f"unsupported JSON Schema type: {expected}")


def _matches(instance: Any, schema: Any, root: dict[str, Any]) -> bool:
    errors: list[dict[str, Any]] = []
    _validate(instance, schema, root, "$", errors)
    return not errors


def _error(errors: list[dict[str, Any]], path: str, keyword: str, message: str) -> None:
    errors.append({"path": path, "keyword": keyword, "message": message})


def _validate(instance: Any, schema: Any, root: dict[str, Any], path: str, errors: list[dict[str, Any]]) -> None:
    if schema is True:
        return
    if schema is False:
        _error(errors, path, "falseSchema", "instance is forbidden by the schema")
        return
    if not isinstance(schema, dict):
        raise ValueError("schema nodes must be objects or booleans")

    reference = schema.get("$ref")
    if reference is not None:
        _validate(instance, _pointer(root, str(reference)), root, path, errors)

    for subschema in schema.get("allOf", []):
        _validate(instance, subschema, root, path, errors)

    any_of = schema.get("anyOf")
    if isinstance(any_of, list) and not any(_matches(instance, subschema, root) for subschema in any_of):
        _error(errors, path, "anyOf", "instance does not match any allowed schema")

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        matches = sum(1 for subschema in one_of if _matches(instance, subschema, root))
        if matches != 1:
            _error(errors, path, "oneOf", f"instance matches {matches} branches; exactly one is required")

    if "not" in schema and _matches(instance, schema["not"], root):
        _error(errors, path, "not", "instance matches a forbidden schema")

    if_schema = schema.get("if")
    if isinstance(if_schema, dict):
        branch = schema.get("then") if _matches(instance, if_schema, root) else schema.get("else")
        if branch is not None:
            _validate(instance, branch, root, path, errors)

    expected_type = schema.get("type")
    if expected_type is not None:
        allowed_types = [expected_type] if isinstance(expected_type, str) else list(expected_type)
        if not any(_is_type(instance, value) for value in allowed_types):
            _error(errors, path, "type", f"expected {' or '.join(allowed_types)}")
            return

    if "const" in schema and _json_key(instance) != _json_key(schema["const"]):
        _error(errors, path, "const", "value does not match the required constant")
    if "enum" in schema and _json_key(instance) not in {_json_key(value) for value in schema["enum"]}:
        _error(errors, path, "enum", "value is not in the allowed enumeration")

    if isinstance(instance, dict):
        required = schema.get("required", [])
        for name in required:
            if name not in instance:
                _error(errors, path, "required", f"missing required property: {name}")
        properties = schema.get("properties", {})
        pattern_properties = schema.get("patternProperties", {})
        for name, value in instance.items():
            child_path = f"{path}.{name}"
            matched = False
            if name in properties:
                _validate(value, properties[name], root, child_path, errors)
                matched = True
            for pattern, subschema in pattern_properties.items():
                if re.search(pattern, name):
                    _validate(value, subschema, root, child_path, errors)
                    matched = True
            if not matched:
                additional = schema.get("additionalProperties", True)
                if additional is False:
                    _error(errors, child_path, "additionalProperties", "property is not allowed")
                elif isinstance(additional, dict):
                    _validate(value, additional, root, child_path, errors)
        if "minProperties" in schema and len(instance) < int(schema["minProperties"]):
            _error(errors, path, "minProperties", "object has too few properties")
        if "maxProperties" in schema and len(instance) > int(schema["maxProperties"]):
            _error(errors, path, "maxProperties", "object has too many properties")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < int(schema["minItems"]):
            _error(errors, path, "minItems", "array has too few items")
        if "maxItems" in schema and len(instance) > int(schema["maxItems"]):
            _error(errors, path, "maxItems", "array has too many items")
        if schema.get("uniqueItems"):
            keys = [_json_key(value) for value in instance]
            if len(keys) != len(set(keys)):
                _error(errors, path, "uniqueItems", "array items are not unique")
        items_schema = schema.get("items")
        if items_schema is not None:
            for index, value in enumerate(instance):
                _validate(value, items_schema, root, f"{path}[{index}]", errors)

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < int(schema["minLength"]):
            _error(errors, path, "minLength", "string is shorter than allowed")
        if "maxLength" in schema and len(instance) > int(schema["maxLength"]):
            _error(errors, path, "maxLength", "string is longer than allowed")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(str(pattern), instance) is None:
            _error(errors, path, "pattern", "string does not match the required pattern")
        # Implement the format used by the image review schemas rather than
        # silently accepting it as an annotation.  RFC 3339 date-times must
        # contain a date/time separator and an explicit UTC offset.
        if schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(instance.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is None or "T" not in instance or parsed.tzinfo is None or parsed.utcoffset() is None:
                _error(errors, path, "format", "string is not a valid date-time")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            _error(errors, path, "minimum", "number is below the allowed minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            _error(errors, path, "maximum", "number is above the allowed maximum")


def validate_instance(instance: Any, schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate the Draft 2020-12 subset used by V3's shipped schemas.

    The project runtime intentionally has no model SDK or third-party JSON Schema
    dependency.  The schemas remain ordinary Draft 2020-12 documents and can also
    be checked by any conforming external validator.
    """

    if schema.get("$schema") != JSON_SCHEMA_DIALECT:
        raise ValueError("schema dialect must be Draft 2020-12")
    errors: list[dict[str, Any]] = []
    _validate(instance, schema, schema, "$", errors)
    return errors
