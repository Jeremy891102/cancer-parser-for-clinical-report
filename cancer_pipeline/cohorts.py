"""Merge module-level patient evidence using the study's explicit date policy."""

from collections import defaultdict
from .aggregation import earliest

SOURCES = {"path": "pathology", "note": "progress_note", "icd": "icd10"}
FINAL_COLUMNS = [
    "MRN",
    "onset_date",
    "source",
    "pathology_positive_date",
    "progress_note_positive_date",
    "icd10_positive_date",
    "in_pathology",
    "in_progress_note",
    "in_icd10",
    "pathology_source_id",
    "progress_note_source_id",
    "icd10_source_id",
]
EXCLUSION_COLUMNS = FINAL_COLUMNS + [
    "pathology_categories",
    "progress_note_categories",
    "pathology_reason",
    "progress_note_reason",
    "icd10_reason",
    "exclusion_reason",
]


def merge_patients(modules, study):
    patients = defaultdict(dict)
    for module, rows in modules.items():
        if module not in SOURCES:
            raise ValueError("Unknown evidence source")
        for row in rows:
            mrn = row["MRN"]
            if not isinstance(mrn, str) or not mrn:
                raise ValueError("Patient evidence requires a string MRN")
            if module in patients[mrn]:
                raise ValueError("Duplicate MRN in patient-level module output")
            patients[mrn][module] = row
    target, exclusion = [], []
    blocking = set(study.settings.get("merge_blocking_reasons", []))
    for mrn, sources in sorted(patients.items()):
        row = {"MRN": mrn}
        for module, prefix in SOURCES.items():
            evidence = sources.get(module, {})
            row[prefix + "_positive_date"] = evidence.get("positive_date")
            row["in_" + prefix] = module in sources
            row[prefix + "_source_id"] = evidence.get("source_ids", [])
        clinical = [
            (SOURCES[module], sources[module]["positive_date"])
            for module in ("path", "note")
            if module in sources and sources[module].get("positive_date")
        ]
        candidates = clinical
        if not candidates and sources.get("icd", {}).get("positive_date"):
            candidates = [("icd10", sources["icd"]["positive_date"])]
        date, chosen, reasons = earliest(candidates)
        source_reasons = {
            reason
            for source in sources.values()
            for reason in source.get("reasons", [])
        }
        if source_reasons & blocking:
            reasons.extend(sorted(source_reasons & blocking))
        row.update(onset_date=date, source="|".join(chosen) if chosen else None)
        if date and not reasons:
            target.append(row)
            continue
        if not reasons:
            reasons.append(
                "target_date_unknown"
                if any(
                    source.get("target_malignancy_documented")
                    for source in sources.values()
                )
                else "no_established_target_malignancy"
            )
        row.update(
            pathology_categories=sources.get("path", {}).get("categories", []),
            progress_note_categories=sources.get("note", {}).get("categories", []),
            pathology_reason=sources.get("path", {}).get("reasons", []),
            progress_note_reason=sources.get("note", {}).get("reasons", []),
            icd10_reason=sources.get("icd", {}).get("reasons", []),
            exclusion_reason=sorted(set(reasons)),
        )
        exclusion.append(row)
    return {"target": target, "exclusion": exclusion}
