"""Validate the user-defined output contract, then source-independent date semantics."""

import datetime as dt
import json
import re

from .formats import validate_note, validate_path, validate_organ_grouping


def response_schema(study, module):
    """Load the API contract without rewriting the study's schema."""
    settings = study.module(module)
    if settings.get("response_mode", "json") != "structured":
        return None
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    schema = json.loads(study.artifact(settings.get("output_schema")).read_text())
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("Invalid study output JSON Schema") from None
    if schema.get("type") != "object" or "anyOf" in schema:
        raise ValueError("Structured Outputs requires a root object")

    def check(node):
        if not isinstance(node, dict):
            raise ValueError("Structured Outputs requires explicit schema objects")
        if set(node) & {
            "allOf",
            "oneOf",
            "not",
            "dependentRequired",
            "dependentSchemas",
            "if",
            "then",
            "else",
        }:
            raise ValueError("Unsupported Structured Outputs composition")
        if "$ref" in node and not node["$ref"].startswith("#"):
            raise ValueError("Use self-contained schema references")
        types = node.get("type", [])
        if types == "object" or "object" in types:
            properties = node.get("properties", {})
            if node.get("additionalProperties") is not False or set(
                node.get("required", [])
            ) != set(properties):
                raise ValueError(
                    "Strict objects require every property and additionalProperties false"
                )
            for child in properties.values():
                check(child)
        if types == "array" or "array" in types:
            if "items" not in node:
                raise ValueError("Strict arrays require items")
            check(node["items"])
        for child in node.get("anyOf", []):
            check(child)
        for child in node.get("$defs", {}).values():
            check(child)

    check(schema)
    return schema


def build_validator(study, module):
    settings = study.module(module)
    strict_schema = response_schema(study, module)
    kind = settings["validator"]
    if kind in ("note", "path"):
        template = json.loads(study.artifact(settings["output_format"]).read_text())
        subtypes = set(study.subtypes())

        strict_validator = None
        if strict_schema is not None:
            from jsonschema import Draft202012Validator
            from referencing import Registry

            strict_validator = Draft202012Validator(strict_schema, registry=Registry())
            if set(strict_schema.get("properties", {})) != set(template):
                raise ValueError("Strict schema and legacy output template keys differ")

        def validate(value):
            if strict_validator is not None and not strict_validator.is_valid(value):
                return ["Output violates strict study schema"]
            if kind == "note":
                return validate_note(value, template, subtypes)
            errors = validate_path(value, template, subtypes)
            return errors or validate_organ_grouping(value)

        return validate
    if kind != "json_schema":
        raise ValueError("Unsupported output validator")
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    from referencing import Registry

    schema = json.loads(study.artifact(settings["output_schema"]).read_text())
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("Invalid study output JSON Schema") from None
    properties = schema.get("properties", {})
    fields = settings["output_fields"]
    if schema.get("type") != "object" or not set(fields.values()) <= set(
        schema.get("required", [])
    ):
        raise ValueError(
            "The output schema must require the mapped top-level evidence fields"
        )
    if not set(fields.values()) <= set(properties):
        raise ValueError("Mapped evidence fields are missing from the output schema")
    labels = properties[fields["classification"]].get("enum", [])
    if study.settings["positive_classification"] not in labels:
        raise ValueError(
            "The classification enum must include the study-positive label"
        )
    validator = Draft202012Validator(schema, registry=Registry())
    date_field = settings["output_fields"]["diagnosis_date"]

    def validate(value):
        # Messages expose paths and rules, never clinical values or patient identifiers.
        errors = [
            "schema:"
            + "/".join(map(str, error.absolute_path))
            + ":"
            + str(error.validator)
            for error in validator.iter_errors(value)
        ]
        if errors:
            return errors
        if not isinstance(value, dict) or date_field not in value:
            return ["Missing mapped diagnosis date field"]
        date = value[date_field]
        if date is not None:
            try:
                if not isinstance(date, str) or not re.fullmatch(
                    r"\d{4}(?:-\d{2}(?:-\d{2})?)?", date
                ):
                    raise ValueError()
                parts = list(map(int, date.split("-")))
                dt.date(
                    parts[0],
                    parts[1] if len(parts) > 1 else 1,
                    parts[2] if len(parts) > 2 else 1,
                )
            except (ValueError, TypeError):
                errors.append("Invalid diagnosis_date")
        return errors

    return validate
