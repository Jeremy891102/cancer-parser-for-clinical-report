#!/usr/bin/env python3
"""Repair saved NOTE format errors locally. No network access or inference."""
import argparse
import collections
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path

from cancer_pipeline.formats import validate_note
from cancer_pipeline.note_repairs import repair_note

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input", type=Path, required=True, help="Gzipped original exception JSONL"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New private directory; originals are preserved",
    )
    args = parser.parse_args()
    os.umask(0o077)
    # Never overwrite a previous adjudication or mix policy versions.
    args.output_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    template = json.loads((ROOT / "schemas/note_output_format.json").read_text())
    subtypes = {
        r["name"] for r in json.loads((ROOT / "schemas/subtypes_56.json").read_text())
    }
    fixed, pending, logs, records = [], [], [], []
    seen, rules, remaining = set(), collections.Counter(), collections.Counter()
    for line in gzip.open(args.input, "rt", encoding="utf-8"):
        row = json.loads(line)
        if row["case_id"] in seen:
            raise ValueError("Duplicate source case ID")
        seen.add(row["case_id"])
        result = repair_note(row["prediction"], template, subtypes, row["metadata"])
        if "derived_classification" in row["prediction"]:
            assert (
                result["candidate"]["derived_classification"]
                == row["prediction"]["derived_classification"]
            )
        item = {
            key: row[key]
            for key in ("case_id", "source_file", "source_row", "metadata")
        }
        if result["status"] == "ok_offline":
            assert not validate_note(result["output"], template, subtypes)
            assert not repair_note(
                result["output"], template, subtypes, row["metadata"]
            )["changes"]
            fixed.append(dict(item, output=result["output"]))
        else:
            pending.append(
                dict(
                    item,
                    original_prediction=row["prediction"],
                    candidate=result["candidate"],
                    validation_errors=result["validation_errors"],
                    review_reasons=result["review_reasons"],
                )
            )
            remaining.update(
                set(result["validation_errors"] + result["review_reasons"])
            )
        for change in result["changes"]:
            logs.append(dict(case_id=row["case_id"], status=result["status"], **change))
            rules[change["rule"]] += 1
        records.append(
            {
                "case_id": row["case_id"],
                "MRN": row["metadata"]["mrn"],
                "note_id": row["metadata"]["note_id"],
                "status": result["status"],
                "original_diagnosis_date": row["prediction"].get("diagnosis_date"),
                "candidate_diagnosis_date": result["candidate"].get("diagnosis_date"),
                "remaining_validation_errors": json.dumps(result["validation_errors"]),
                "review_reasons": json.dumps(result["review_reasons"]),
                "changes": json.dumps(result["changes"], ensure_ascii=False),
            }
        )
    for filename, rows in [
        ("repaired_predictions.jsonl.gz", fixed),
        ("pending_review.jsonl.gz", pending),
        ("changes.jsonl.gz", logs),
    ]:
        with gzip.open(args.output_dir / filename, "wt", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (args.output_dir / "repair_records.csv").open("w", newline="") as stream:
        fields = [
            "case_id",
            "MRN",
            "note_id",
            "status",
            "original_diagnosis_date",
            "candidate_diagnosis_date",
            "remaining_validation_errors",
            "review_reasons",
            "changes",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    summary = {
        "input_records": len(seen),
        "accepted_offline_repairs": len(fixed),
        "pending_review": len(pending),
        "remaining_errors_records": dict(remaining),
        "change_rule_counts": dict(rules),
        "api_requests": 0,
        "estimated_new_usd": 0,
        "original_ledgers_unchanged": True,
        "merged_into_formal_csv": False,
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "normalizer_sha256": hashlib.sha256(
            (ROOT / "cancer_pipeline/note_repairs.py").read_bytes()
        ).hexdigest(),
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
