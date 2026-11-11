"""Validate the user-defined output contract, then source-independent date semantics."""

import datetime as dt
import json
import re

from .formats import validate_note, validate_path, validate_organ_grouping


def build_validator(study, module):
    settings = study.module(module)
    kind = settings["validator"]
    if kind in ("note", "path"):
        template = json.loads(study.artifact(settings["output_format"]).read_text())
        subtypes = set(study.subtypes())

        def validate(value):
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
