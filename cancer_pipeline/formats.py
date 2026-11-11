"""Fixed output contracts. Clinical classification remains defined by each study."""

import datetime as dt
import re

CATEGORIES = ["Malignant", "Borderline", "Benign", "Other", "Uncertain"]


def validate_note(value, template, subtypes):
    errors = []
    if not isinstance(value, dict) or set(value) != set(template):
        return ["Output keys do not match the fixed NOTE schema"]
    for key in ["mrn", "note_id", "note_signed_time", "diagnosis_date"]:
        if value[key] is not None and not isinstance(value[key], str):
            errors.append(key + " must be a string or null")
    date = value["diagnosis_date"]
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
    if type(value["family_history_of_ovarian_cancer_documented"]) is not bool:
        errors.append("Family-history flag must be boolean")
    if value["derived_classification"] not in CATEGORIES:
        errors.append("Invalid derived_classification")
    if not isinstance(value["diagnoses"], list):
        return errors + ["diagnoses must be a list"]
    for d in value["diagnoses"]:
        if not isinstance(d, dict) or set(d) != set(template["diagnoses"][0]):
            errors.append("Diagnosis keys do not match schema")
            continue
        if any(v is not None and not isinstance(v, str) for v in d.values()):
            errors.append("Diagnosis fields must be strings or null")
            continue
        sub = d["histologic_subtype"]
        if (
            sub is not None
            and sub not in subtypes
            and not re.fullmatch(r"other: \S.*", sub)
        ):
            errors.append("Unknown histologic_subtype")
        for key in ["diagnostic_qualification", "primary_origin_certainty"]:
            if d[key] not in [None, "confirmed", "uncertain"]:
                errors.append("Invalid " + key)
        if sub is None and d["diagnostic_qualification"] is not None:
            errors.append("Absent subtype must have null qualification")
        if d["temporal_status"] not in [
            None,
            "current_disease",
            "history_only",
            "remission_or_ned",
            "uncertain",
        ]:
            errors.append("Invalid temporal_status")
    return errors


def validate_path(value, template, subtypes):
    errors = []

    def structure(v, t, path):
        if isinstance(t, dict):
            if not isinstance(v, dict) or set(v) != set(t):
                errors.append(path + ": fixed keys mismatch")
                return
            for k, x in t.items():
                structure(v[k], x, path + "." + k)
        elif isinstance(t, list):
            if not isinstance(v, list):
                errors.append(path + ": expected list")
                return
            for i, x in enumerate(v):
                structure(x, t[0], path + "[" + str(i) + "]")
        elif isinstance(v, (dict, list)):
            errors.append(path + ": expected scalar")

    structure(value, template, "output")
    if errors:
        return errors
    for key in [
        "mrn",
        "report_id",
        "collected_datetime",
        "diagnosis_date",
        "clinical_history",
    ]:
        if value[key] is not None and not isinstance(value[key], str):
            errors.append("Invalid " + key)
    for flag in ["history_of_ovarian_cancer_documented", "has_target_site_specimen"]:
        if type(value[flag]) is not bool:
            errors.append("Invalid flag " + flag)
    if value["derived_classification"] not in CATEGORIES:
        errors.append("Invalid category")
    date = value["diagnosis_date"]
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
        except (TypeError, ValueError):
            errors.append("Invalid diagnosis_date")
    ids = []
    for s in value["specimens"]:
        if any(v is not None and not isinstance(v, str) for v in s.values()):
            errors.append("Invalid specimen scalar")
        ids.append(s["specimen_id"])
        if s["laterality"] not in [None, "left", "right", "bilateral"]:
            errors.append("Invalid specimen laterality")
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        errors.append("Invalid/duplicate specimen IDs")
    dids = []
    for d in value["diagnoses"]:
        dids.append(d["diagnosis_id"])
        for k, t in template["diagnoses"][0].items():
            if not isinstance(t, list) and d[k] is not None:
                if k in ["architectural_score", "mitotic_score", "response_score"]:
                    if type(d[k]) not in [int, float] or d[k] < 0:
                        errors.append("Invalid score " + k)
                elif not isinstance(d[k], str):
                    errors.append("Invalid diagnosis scalar " + k)
        if any(not isinstance(i, str) or i not in ids for i in d["specimen_ids"]):
            errors.append("Invalid specimen link")
        for k in ["diagnostic_qualification", "primary_origin_certainty"]:
            if d[k] not in [None, "confirmed", "uncertain"]:
                errors.append("Invalid " + k)
        if d["laterality"] not in [None, "left", "right", "bilateral"]:
            errors.append("Invalid diagnosis laterality")
        sub = d["histologic_subtype"]
        if sub is not None and (
            not isinstance(sub, str)
            or sub not in subtypes
            and not re.fullmatch(r"other: \S.*", sub)
        ):
            errors.append("Unknown subtype")
        if sub is None and d["diagnostic_qualification"] is not None:
            errors.append("Absent subtype qualification")
        if any(type(n) not in [int, float] or n <= 0 for n in d["tumor_size_mm"]):
            errors.append("Invalid tumor dimension")
        if any(not isinstance(x, str) for x in d["involved_sites"]):
            errors.append("Invalid involved_sites")
        for node in d["lymph_nodes"]:
            for k in ["examined_count", "positive_count"]:
                if node[k] is not None and (type(node[k]) is not int or node[k] < 0):
                    errors.append("Invalid node count")
            if (
                type(node["examined_count"]) is int
                and type(node["positive_count"]) is int
                and node["positive_count"] > node["examined_count"]
            ):
                errors.append("Node counts contradictory")
    if any(not isinstance(i, str) or not i for i in dids) or len(dids) != len(
        set(dids)
    ):
        errors.append("Invalid/duplicate diagnosis IDs")
    return errors


def validate_organ_grouping(value):
    """v1.3 structural checks; do not reinterpret or repair model diagnoses."""
    errors = []
    for specimen in value["specimens"]:
        sid = specimen["specimen_id"]
        label = specimen["source_specimen_label"]
        pattern = (
            re.escape(label) + r"(?:\.[1-9][0-9]*)?"
            if label is not None
            else r"S[1-9][0-9]*(?:\.[1-9][0-9]*)?"
        )
        if not re.fullmatch(pattern, sid):
            errors.append(
                "Specimen ID must use source label plus numeric suffix only: " + sid
            )
    used = set()
    for diagnosis in value["diagnoses"]:
        links = diagnosis["specimen_ids"]
        if links and diagnosis["diagnosis_id"] != links[0]:
            errors.append("Diagnosis ID must equal linked specimen ID")
        if used.intersection(links):
            errors.append(
                "Multiple diagnoses for one organ-level specimen; group findings"
            )
        used.update(links)
    return errors
