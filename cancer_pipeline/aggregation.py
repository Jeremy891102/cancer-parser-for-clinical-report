"""Patient aggregation preserves source-specific reviewed behavior."""

from collections import defaultdict
import datetime as dt
import json
from .dates import date_bounds


def aggregate_notes(records, *, target_sites, subtype_groups, validate, unresolved):
    groups = defaultdict(list)
    issues = []
    for number, record in enumerate(records, 1):
        output = record.get("output") if isinstance(record, dict) else None
        case_id = (
            record.get("case_id", "line-" + str(number))
            if isinstance(record, dict)
            else "line-" + str(number)
        )
        mrn = output.get("mrn") if isinstance(output, dict) else None
        note_id = output.get("note_id") if isinstance(output, dict) else None
        errors = validate(output)
        if not isinstance(mrn, str) or not mrn.strip():
            issues.append(
                {
                    "MRN": "",
                    "reason": "missing_patient_id",
                    "case_id": case_id,
                    "note_ids": [note_id] if isinstance(note_id, str) else [],
                    "details": errors,
                }
            )
            continue
        groups[mrn].append((case_id, output, errors))
    patients = []
    for mrn, items in sorted(groups.items()):
        local_issues = []

        def issue(reason, case="", notes=None, details=None):
            local_issues.append(
                {
                    "MRN": mrn,
                    "reason": reason,
                    "case_id": case,
                    "note_ids": notes or [],
                    "details": details or [],
                }
            )

        valid = []
        blocked = False
        for case, value, errors in items:
            if errors:
                issue(
                    "invalid_output",
                    case,
                    [value["note_id"]] if isinstance(value.get("note_id"), str) else [],
                    errors,
                )
                blocked = True
            elif not value["note_id"] or not value["note_id"].strip():
                issue("missing_note_id", case)
                blocked = True
            else:
                valid.append((case, value))
        by_note = defaultdict(list)
        for case, value in valid:
            by_note[value["note_id"]].append((case, value))
        unique = []
        for note_id, versions in by_note.items():
            if len({json.dumps(v, sort_keys=True) for _, v in versions}) > 1:
                issue("conflicting_note_versions", notes=[note_id])
                blocked = True
            else:
                unique.append(versions[0])
        target = []
        uncertain = []
        for case, value in unique:
            category = value["derived_classification"]
            if category == "Uncertain" and unresolved(value):
                uncertain.append(value["note_id"])
            if category != "Malignant":
                if any(
                    d["primary_origin_site"] in target_sites
                    and d["primary_origin_certainty"] == "confirmed"
                    and d["diagnostic_qualification"] == "confirmed"
                    and subtype_groups.get(d["histologic_subtype"]) == "Malignant"
                    for d in value["diagnoses"]
                ):
                    issue("target_category_conflict", case, [value["note_id"]])
                    blocked = True
                continue
            supported = any(
                d["primary_origin_site"] in target_sites
                and d["primary_origin_certainty"] == "confirmed"
                and subtype_groups.get(d["histologic_subtype"])
                not in ["Borderline", "Benign", "Other", "Separate precursor"]
                for d in value["diagnoses"]
            )
            if not supported:
                issue("target_origin_not_confirmed", case, [value["note_id"]])
                blocked = True
                continue
            date = value["diagnosis_date"]
            if date and value["note_signed_time"]:
                try:
                    signed = dt.date.fromisoformat(value["note_signed_time"][:10])
                    if date_bounds(date)[0] > signed:
                        issue("diagnosis_after_note_date", case, [value["note_id"]])
                        blocked = True
                        continue
                except ValueError:
                    issue("unusable_note_timestamp", case, [value["note_id"]])
                    blocked = True
                    continue
            target.append(value)
        chosen = None
        source_ids = []
        dates = sorted({v["diagnosis_date"] for v in target if v["diagnosis_date"]})
        if dates:
            earliest = min(dates, key=lambda d: date_bounds(d)[0])
            _, first_end = date_bounds(earliest)
            overlap = [
                d for d in dates if d != earliest and date_bounds(d)[0] <= first_end
            ]
            if overlap:
                issue(
                    "overlapping_date_precision",
                    notes=sorted(
                        v["note_id"]
                        for v in target
                        if v["diagnosis_date"] in [earliest] + overlap
                    ),
                )
                blocked = True
            elif not blocked:
                chosen = earliest
                source_ids = sorted(
                    {v["note_id"] for v in target if v["diagnosis_date"] == chosen}
                )
        if target and not dates:
            issue("no_usable_target_date", notes=sorted({v["note_id"] for v in target}))
        if uncertain:
            issue("unresolved_target_classification", notes=sorted(set(uncertain)))
        if chosen:
            status = "target_positive"
        elif blocked:
            status = "needs_review"
        elif target:
            status = "target_date_unknown"
        elif uncertain:
            status = "needs_review"
        else:
            status = "no_established_target_malignancy"
        patients.append(
            {
                "MRN": mrn,
                "progress_note_positive_date": chosen,
                "in_progress_note": True,
                "progress_note_source_id": source_ids,
                "status": status,
                "categories": sorted({v["derived_classification"] for _, v in unique}),
                "all_note_ids": sorted(
                    {
                        v["note_id"]
                        for _, v, _ in items
                        if isinstance(v.get("note_id"), str)
                    }
                ),
                "target_malignancy_documented": bool(target),
            }
        )
        issues.extend(local_issues)
    return {"patients": patients, "issues": issues}


def aggregate_path(outputs, invalid, date_adjudications=None):
    groups = defaultdict(list)
    for o in outputs:
        if o["mrn"]:
            groups[o["mrn"]].append(o)
    for mrn in invalid:
        if mrn:
            groups[mrn]
    patients = []
    for mrn, items in sorted(groups.items()):
        reasons = []
        positive = [o for o in items if o["derived_classification"] == "Malignant"]
        dated = [o for o in positive if o["diagnosis_date"]]
        if mrn in invalid:
            reasons.append("invalid_output")
        if positive and not dated:
            reasons.append("target_date_unknown")
        if any(o["derived_classification"] == "Uncertain" for o in items):
            reasons.append("unresolved_target_classification")
        chosen = None
        if dated:
            candidates = sorted(
                {o["diagnosis_date"] for o in dated}, key=lambda x: date_bounds(x)[0]
            )
            first = candidates[0]
            # Compatible but distinct partial/full dates require source adjudication before collapse.
            if any(date_bounds(x)[0] <= date_bounds(first)[1] for x in candidates[1:]):
                reasons.append("overlapping_date_precision")
            elif "invalid_output" not in reasons:
                chosen = first
        adjudication = (date_adjudications or {}).get(mrn)
        if adjudication:
            date = adjudication["diagnosis_date"]
            source_ids = adjudication["source_ids"]
            if not source_ids or not all(
                any(
                    o["report_id"] == rid and o["diagnosis_date"] == date for o in dated
                )
                for rid in source_ids
            ):
                raise ValueError("Adjudicated date lacks matching Malignant source")
            lo, hi = date_bounds(date)
            if not all(
                date_bounds(o["diagnosis_date"])[0] <= lo
                and hi <= date_bounds(o["diagnosis_date"])[1]
                for o in dated
            ):
                raise ValueError("Adjudicated date conflicts with dated evidence")
            if "invalid_output" not in reasons:
                chosen = date
                reasons = [r for r in reasons if r != "overlapping_date_precision"]
        ids = sorted(
            {
                o["report_id"]
                for o in items
                if (
                    chosen
                    and o["diagnosis_date"] == chosen
                    and o["derived_classification"] == "Malignant"
                )
                or (
                    not chosen
                    and o["derived_classification"] in ["Malignant", "Uncertain"]
                )
            }
        )
        ids = sorted(set(ids) | set(invalid.get(mrn, [])))
        patients.append(
            dict(
                MRN=mrn,
                pathology_positive_date=chosen,
                pathology_source_id=ids,
                categories=sorted({o["derived_classification"] for o in items}),
                pathology_reason=reasons,
            )
        )
    return patients


def unresolved_target(value, study):
    import re

    sites = set(study.settings["target_sites"])
    unresolved_sites = set(study.settings.get("unresolved_origin_sites", []))
    pattern = study.settings.get("unresolved_target_pattern")
    for diagnosis in value["diagnoses"]:
        site = diagnosis["primary_origin_site"]
        if site in unresolved_sites or (
            site is None and diagnosis["primary_origin_certainty"] == "uncertain"
        ):
            return True
        if site in sites and diagnosis["temporal_status"] != "history_only":
            return True
        if (
            pattern
            and site is None
            and diagnosis["temporal_status"] != "history_only"
            and re.search(pattern, diagnosis["diagnosis_as_documented"] or "", re.I)
        ):
            return True
    return False


def aggregate_classifications(records, study, module):
    """User-defined schemas map their study-positive category into patient evidence."""
    from .validation import build_validator

    validate = build_validator(study, module)
    fields = study.module(module)["output_fields"]
    groups = defaultdict(list)
    issues = []
    for record in records:
        output = record.get("output")
        metadata = record.get("metadata", {})
        safe_output = output if isinstance(output, dict) else {}
        mrn = metadata.get("patient_id") or safe_output.get(fields["patient_id"])
        source_id = metadata.get("source_id") or safe_output.get(fields["source_id"])
        if not isinstance(mrn, str) or not mrn:
            issues.append(
                {"reason": "missing_patient_id", "case_id": record.get("case_id")}
            )
            continue
        errors = validate(output)
        groups[mrn].append((source_id, output, errors))
    patients = []
    for mrn, rows in sorted(groups.items()):
        reasons = []
        by_id = defaultdict(list)
        valid = []
        for source_id, output, errors in rows:
            if errors or not source_id:
                reasons.append("invalid_output")
            else:
                by_id[source_id].append(output)
        for source_id, versions in by_id.items():
            if len({json.dumps(v, sort_keys=True) for v in versions}) != 1:
                reasons.append("conflicting_record_versions")
            else:
                valid.append((source_id, versions[0]))
        positive = [
            (source_id, output)
            for source_id, output in valid
            if output[fields["classification"]]
            == study.settings["positive_classification"]
        ]
        dated = [
            (source_id, output[fields["diagnosis_date"]])
            for source_id, output in positive
            if output[fields["diagnosis_date"]]
        ]
        date, sources, date_reasons = earliest(dated)
        reasons.extend(date_reasons)
        if positive and not dated:
            reasons.append("target_date_unknown")
        if "invalid_output" in reasons or "conflicting_record_versions" in reasons:
            date, sources = None, []
        patients.append(
            {
                "MRN": mrn,
                "positive_date": date,
                "source_ids": sources,
                "all_source_ids": sorted(
                    {source_id for source_id, _, _ in rows if source_id}
                ),
                "categories": sorted(
                    {output[fields["classification"]] for _, output in valid}
                ),
                "reasons": sorted(set(reasons)),
                "target_malignancy_documented": bool(positive),
                "status": (
                    "needs_review"
                    if reasons
                    else (
                        "target_positive"
                        if date
                        else "no_established_target_malignancy"
                    )
                ),
            }
        )
    return {"patients": patients, "issues": issues}


def earliest(evidence):
    dates = sorted(
        {date for _, date in evidence}, key=lambda date: (date_bounds(date)[0], date)
    )
    if not dates:
        return None, [], []
    first = dates[0]
    if any(date_bounds(date)[0] <= date_bounds(first)[1] for date in dates[1:]):
        return None, [], ["overlapping_date_precision"]
    return (
        first,
        sorted({source_id for source_id, date in evidence if date == first}),
        [],
    )


def aggregate_module(records, study, module):
    from .validation import build_validator

    settings = study.module(module)
    if settings["aggregation"] == "classification":
        return aggregate_classifications(records, study, module)
    if settings["aggregation"] == "note":
        result = aggregate_notes(
            records,
            target_sites=set(study.settings["target_sites"]),
            subtype_groups=study.subtypes(),
            validate=build_validator(study, module),
            unresolved=lambda output: unresolved_target(output, study),
        )
        issues_by_mrn = defaultdict(list)
        for issue in result["issues"]:
            issues_by_mrn[issue["MRN"]].append(issue["reason"])
        return {
            "patients": [
                {
                    "MRN": row["MRN"],
                    "positive_date": row["progress_note_positive_date"],
                    "source_ids": row["progress_note_source_id"],
                    "all_source_ids": row["all_note_ids"],
                    "categories": row["categories"],
                    "reasons": sorted(set(issues_by_mrn[row["MRN"]])),
                    "status": row["status"],
                    "target_malignancy_documented": row["target_malignancy_documented"],
                }
                for row in result["patients"]
            ],
            "issues": result["issues"],
        }
    if settings["aggregation"] != "path":
        raise ValueError("Unsupported aggregation policy")
    validate = build_validator(study, module)
    valid, invalid, issues = [], defaultdict(list), []
    for record in records:
        output = record.get("output")
        metadata = record.get("metadata", {})
        safe_output = output if isinstance(output, dict) else {}
        mrn = metadata.get("patient_id") or safe_output.get("mrn")
        source_id = metadata.get("source_id") or safe_output.get("report_id")
        errors = validate(output)
        if not mrn:
            issues.append(
                {"reason": "missing_patient_id", "case_id": record.get("case_id")}
            )
        elif errors:
            invalid[mrn].append(source_id)
        else:
            valid.append(output)
    rows = aggregate_path(valid, invalid)
    return {
        "patients": [
            {
                "MRN": row["MRN"],
                "positive_date": row["pathology_positive_date"],
                "source_ids": row["pathology_source_id"],
                "all_source_ids": sorted(
                    {
                        value["report_id"]
                        for value in valid
                        if value["mrn"] == row["MRN"]
                    }
                    | set(invalid.get(row["MRN"], []))
                ),
                "categories": row["categories"],
                "reasons": row["pathology_reason"],
                "target_malignancy_documented": "Malignant" in row["categories"],
                "status": (
                    "target_positive"
                    if row["pathology_positive_date"]
                    else (
                        "needs_review"
                        if row["pathology_reason"]
                        else "no_established_target_malignancy"
                    )
                ),
            }
            for row in rows
        ],
        "issues": issues,
    }
