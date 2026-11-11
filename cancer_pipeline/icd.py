"""Deterministic ICD candidates use only the study's supplied code and date rules."""

from collections import defaultdict
import csv
import re
from .aggregation import earliest
from .dates import date_bounds
from .metadata import normalize_metadata


def normalize_code(value):
    return value.strip().upper().replace(".", "")


def process_icd(paths, study):
    settings = study.settings["icd"]
    columns = settings["columns"]
    positive = {normalize_code(code) for code in settings["positive_codes"]}
    auxiliary = {normalize_code(code) for code in settings["auxiliary_codes"]}
    excluded_categories = {
        re.sub(r"[^a-z0-9]", "", value.lower())
        for value in settings["excluded_categories"]
    }
    excluded_dates = set(settings["excluded_dates"])
    records, groups = [], defaultdict(list)
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not set(columns.values()) <= set(reader.fieldnames or []):
                raise ValueError("ICD CSV is missing mapped columns")
            for row_number, source in enumerate(reader, 1):
                mrn, code, date = (
                    source[columns[key]].strip()
                    for key in ("patient_id", "code", "date")
                )
                category = re.sub(r"[^a-z0-9]", "", source[columns["category"]].lower())
                if category in excluded_categories:
                    outcome = "family_history"
                elif normalize_code(code) in positive:
                    outcome = "positive"
                elif normalize_code(code) in auxiliary:
                    outcome = "auxiliary"
                else:
                    outcome = "not_target"
                reason = None
                if date in excluded_dates:
                    date, reason = None, "excluded_placeholder_date"
                elif date:
                    try:
                        if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", date):
                            raise ValueError()
                        date_bounds(date)
                    except ValueError:
                        date, reason = None, "invalid_date"
                else:
                    date = None
                mrn = normalize_metadata({"mrn": mrn})["mrn"]
                record = {
                    "MRN": mrn,
                    "icd10_source_id": f"{path.name}:{row_number}",
                    "icd10_code": code,
                    "icd10_date": date,
                    "outcome": outcome,
                    "date_reason": reason,
                    "source_file": path.name,
                    "source_row": row_number,
                }
                records.append(record)
                if mrn:
                    groups[mrn].append(record)
    patients = []
    for mrn, rows in sorted(groups.items()):
        candidates = [row for row in rows if row["outcome"] == "positive"]
        evidence = [
            (row["icd10_source_id"], row["icd10_date"])
            for row in candidates
            if row["icd10_date"]
        ]
        date, ids, reasons = earliest(evidence)
        if candidates and not date and not reasons:
            reasons.append("target_date_unknown")
        if date:
            status = "target_positive"
        elif reasons:
            status = "needs_review"
        else:
            status = "no_established_target_malignancy"
        patients.append(
            {
                "MRN": mrn,
                "positive_date": date,
                "source_ids": ids,
                "all_source_ids": [row["icd10_source_id"] for row in rows],
                "categories": sorted({row["outcome"] for row in rows}),
                "reasons": reasons,
                "target_malignancy_documented": bool(candidates),
                "status": status,
            }
        )
    return {"records": records, "patients": patients}
