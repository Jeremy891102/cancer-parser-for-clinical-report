"""Explicit, offline NOTE format repair; never classify a patient or invent a date.

Opt-in only: this module is not part of the live inference validator. Every
accepted transformation is recorded; unresolved candidates are withheld.
"""

import calendar
import copy
import datetime as dt
import re

from .formats import validate_note

# Reviewed names absent from the study vocabulary. Preserve their wording;
# in particular, an unspecified carcinoid is not upgraded to a malignant type.
UNLISTED_NAMES = {
    "fibroadenoma",
    "non-melanoma skin cancer",
    "fibroid",
    "carcinoid tumor",
    "invasive ductal carcinoma",
    "serous cystadenofibroma",
}
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})


def normalized_date(value):
    """US month/day convention; require four-digit years and real calendar dates."""
    if value is None or not isinstance(value, str):
        return value
    if re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        return value
    month_year = re.fullmatch(r"(\d{1,2})/(\d{4})", value)
    month_day_year = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", value)
    written_month = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", value)
    within_year = re.fullmatch(r"(?:early|mid|late)\s+(\d{4})", value, re.I)
    try:
        if month_year:
            month, year = map(int, month_year.groups())
            dt.date(year, month, 1)
            return f"{year:04d}-{month:02d}"
        if month_day_year:
            month, day, year = map(int, month_day_year.groups())
            return dt.date(year, month, day).isoformat()
        if written_month and written_month[1].lower() in MONTHS:
            year, month = int(written_month[2]), MONTHS[written_month[1].lower()]
            dt.date(year, month, 1)
            return f"{year:04d}-{month:02d}"
        if within_year:
            year = int(within_year[1])
            dt.date(year, 1, 1)
            return f"{year:04d}"
    except ValueError:
        pass
    return value


def repair_note(original, template, subtypes, metadata=None):
    value = copy.deepcopy(original)
    changes = []
    pending = []

    def set_field(obj, key, new, path, rule):
        if key in obj and obj[key] == new:
            return
        changes.append(
            {
                "field": path,
                "before": copy.deepcopy(obj.get(key)),
                "after": new,
                "rule": rule,
            }
        )
        obj[key] = new

    def rename(obj, old, new, path):
        if old in obj and new not in obj:
            changes.append(
                {
                    "field": path,
                    "before_key": old,
                    "after_key": new,
                    "rule": "documented_key_typo",
                }
            )
            obj[new] = obj.pop(old)

    if isinstance(value, dict):
        rename(
            value,
            "family_history_of_ovarian_cancer",
            "family_history_of_ovarian_cancer_documented",
            "root",
        )
        for key in ("mrn", "note_id", "note_signed_time", "diagnosis_date"):
            if value.get(key) == "null":
                set_field(value, key, None, key, "json_null_literal")
        if metadata:
            for key in ("mrn", "note_id", "note_signed_time"):
                set_field(
                    value, key, metadata[key], key, "authoritative_source_metadata"
                )
        date = value.get("diagnosis_date")
        fixed_date = normalized_date(date)
        if fixed_date != date:
            set_field(
                value,
                "diagnosis_date",
                fixed_date,
                "diagnosis_date",
                "same_date_preserved_precision",
            )
        diagnoses = value.get("diagnoses", [])
        if isinstance(diagnoses, list):
            casefold_names = {name.casefold(): name for name in subtypes}
            for i, diagnosis in enumerate(diagnoses):
                if not isinstance(diagnosis, dict):
                    continue
                path = f"diagnoses[{i}]"
                rename(
                    diagnosis, "diagnosis_as_document", "diagnosis_as_documented", path
                )
                expected = set(template["diagnoses"][0])
                extra = set(diagnosis) - expected
                if (
                    set(diagnosis) & expected == expected - {"histologic_subtype"}
                    and len(extra) == 1
                ):
                    key = next(iter(extra))
                    existing_subtype = diagnosis[key]
                    if isinstance(existing_subtype, str) and (
                        existing_subtype in subtypes
                        or re.fullmatch(r"other: \S.*", existing_subtype)
                    ):
                        rename(diagnosis, key, "histologic_subtype", path)
                    elif (
                        key.startswith("other: ")
                        and len(key) > 7
                        and diagnosis[key] in (None, "null")
                    ):
                        del diagnosis[key]
                        changes.append(
                            {
                                "field": path + ".histologic_subtype",
                                "before_key": key,
                                "after": key,
                                "rule": "subtype_value_misplaced_as_key",
                            }
                        )
                        diagnosis["histologic_subtype"] = key
                for key in expected:
                    if diagnosis.get(key) == "null":
                        set_field(
                            diagnosis, key, None, path + "." + key, "json_null_literal"
                        )
                sub = diagnosis.get("histologic_subtype")
                if (
                    isinstance(sub, str)
                    and sub not in subtypes
                    and not re.fullmatch(r"other: \S.*", sub)
                ):
                    if sub.casefold() in casefold_names:
                        set_field(
                            diagnosis,
                            "histologic_subtype",
                            casefold_names[sub.casefold()],
                            path + ".histologic_subtype",
                            "reference_case_only",
                        )
                    elif sub.casefold() in UNLISTED_NAMES:
                        set_field(
                            diagnosis,
                            "histologic_subtype",
                            "other: " + sub,
                            path + ".histologic_subtype",
                            "reviewed_unlisted_name_preserved",
                        )
                    elif sub == "uncertain":
                        set_field(
                            diagnosis,
                            "histologic_subtype",
                            None,
                            path + ".histologic_subtype",
                            "uncertainty_marker_is_not_a_subtype",
                        )
                    else:
                        pending.append(
                            path + ".histologic_subtype: requires vocabulary review"
                        )
                if (
                    "histologic_subtype" in diagnosis
                    and diagnosis["histologic_subtype"] is None
                    and diagnosis.get("diagnostic_qualification") is not None
                ):
                    set_field(
                        diagnosis,
                        "diagnostic_qualification",
                        None,
                        path + ".diagnostic_qualification",
                        "absent_subtype_requires_null_qualification",
                    )
                if diagnosis.get("temporal_status") == "current":
                    set_field(
                        diagnosis,
                        "temporal_status",
                        "current_disease",
                        path + ".temporal_status",
                        "current_enum_alias",
                    )
    errors = validate_note(value, template, subtypes)
    return {
        "status": "needs_review" if pending or errors else "ok_offline",
        "output": value if not pending and not errors else None,
        "candidate": value,
        "changes": changes,
        "validation_errors": errors,
        "review_reasons": pending,
    }
