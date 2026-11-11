"""Streaming document exports and common patient evidence CSVs."""

from collections import Counter
import csv
import gzip
from itertools import groupby
import json
from pathlib import Path
import sqlite3
import tempfile

from .aggregation import aggregate_module
from .engine import add, finish, setup
from .storage import save_csv, save_gz, save_json

PATIENT_COLUMNS = [
    "MRN",
    "positive_date",
    "source_ids",
    "all_source_ids",
    "categories",
    "reasons",
    "target_malignancy_documented",
    "status",
]


def export_run(db, directory, study, module, batches=True):
    from .runner import ledger_records

    settings = study.module(module)
    fields = settings["output_fields"]
    artifacts = directory / module
    artifacts.mkdir(mode=0o700, exist_ok=True)
    csv_dir = directory / "csv"
    csv_dir.mkdir(mode=0o700, exist_ok=True)
    counts = Counter()
    batch, batch_number = [], 0

    def record_rows():
        nonlocal batch_number
        for record in ledger_records(db, fields):
            output = record["output"]
            metadata = record["metadata"]
            category = (
                output.get(fields["classification"])
                if record["status"] in ("ok", "reused")
                else None
            )
            if category is not None:
                counts[category] += 1
                if batches:
                    batch.append(output)
                    if len(batch) == 500:
                        batch_number += 1
                        save_gz(artifacts / f"batch_{batch_number:04d}.jsonl.gz", batch)
                        batch.clear()
            yield {
                "MRN": metadata["patient_id"],
                "source_id": metadata["source_id"],
                "document_date": metadata["document_date"],
                "derived_classification": category,
                "diagnosis_date": (
                    output.get(fields["diagnosis_date"])
                    if category is not None
                    else None
                ),
                "source_file": record["source_file"],
                "source_row": record["source_row"],
                "status": record["status"],
            }

    save_csv(
        csv_dir / f"{module}_record_level.csv",
        [
            "MRN",
            "source_id",
            "document_date",
            "derived_classification",
            "diagnosis_date",
            "source_file",
            "source_row",
            "status",
        ],
        record_rows(),
    )
    if batch:
        save_gz(artifacts / f"batch_{batch_number + 1:04d}.jsonl.gz", batch)
    patient_statuses = Counter()

    def patients():
        for _, records in groupby(
            ledger_records(db, fields, "mrn,position"),
            key=lambda record: record["metadata"]["patient_id"],
        ):
            result = aggregate_module(records, study, module)
            for patient in result["patients"]:
                patient_statuses[patient["status"]] += 1
                yield patient

    save_csv(csv_dir / f"{module}_patient_level.csv", PATIENT_COLUMNS, patients())
    summary = dict(
        study=study.settings["study_id"],
        module=module,
        classification_counts=counts,
        patient_status_counts=patient_statuses,
        state="export_complete",
        clinical_accuracy_evaluated=False,
        unlinked_records=db.execute(
            "SELECT COUNT(*) FROM notes WHERE mrn IS NULL OR mrn=''"
        ).fetchone()[0],
    )
    save_json(directory / "export_summary.json", summary)
    return summary


def read_outputs(paths, fields):
    for path in paths:
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Expected a JSON object per record")
                if "case_id" in value and "output" in value:
                    record = dict(value)
                    output = record.get("output")
                else:
                    output = value
                    record = {"case_id": f"{path.name}:{number}", "output": value}
                safe_output = output if isinstance(output, dict) else {}
                record.setdefault(
                    "metadata",
                    {
                        key: safe_output.get(fields[key])
                        for key in ("patient_id", "source_id", "document_date")
                    },
                )
                record.setdefault("source_file", path.name)
                record.setdefault("source_row", number)
                yield record


def aggregate_outputs(study, module, paths, directory):
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    from .validation import build_validator

    validate = build_validator(study, module)
    fields = study.module(module)["output_fields"]
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        db = sqlite3.connect(Path(temporary) / "records.sqlite3")
        try:
            setup(db)
            for position, record in enumerate(read_outputs(paths, fields), 1):
                output = record.get("output")
                metadata = record["metadata"]
                original = output
                if isinstance(output, dict):
                    output = dict(
                        output,
                        **{
                            fields[key]: metadata[key]
                            for key in ("patient_id", "source_id", "document_date")
                        },
                    )
                case = dict(
                    record,
                    input={"metadata": {}},
                    ledger_metadata={
                        "pat_mrn_id": metadata["patient_id"],
                        "note_id": metadata["source_id"],
                        "note_signed_time": metadata["document_date"],
                    },
                )
                status = "invalid_schema" if validate(output) else "ok"
                with db:
                    add(db, position, case, status)
                    finish(
                        db,
                        position,
                        status,
                        {"output": output, "original_output": original},
                    )
            summary = export_run(db, directory, study, module, batches=False)
        finally:
            db.close()
    save_json(
        directory / "manifest.json",
        dict(
            summary,
            study_sha256=study.fingerprint(),
            state="complete",
            api_calls=0,
            input_files=[str(path.resolve()) for path in paths],
        ),
    )
    return summary


def read_patients(path, study=None, module=None):
    if study is not None:
        directory = path.parent.parent
        manifest = json.loads((directory / "manifest.json").read_text())
        contract_path = directory / "contract.json"
        contract = (
            json.loads(contract_path.read_text())
            if contract_path.exists()
            else manifest
        )
        if (
            manifest.get("state") != "complete"
            or contract.get("study_sha256") != study.fingerprint()
            or contract.get("module") != module
        ):
            raise ValueError(
                "Patient evidence must be complete and belong to this study and module"
            )
    with path.open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not set(PATIENT_COLUMNS) <= set(reader.fieldnames or []):
            raise ValueError("Expected a standard patient-level module CSV")
        rows = []
        for row in reader:
            for key in ("source_ids", "all_source_ids", "categories", "reasons"):
                row[key] = json.loads(row[key])
                if not isinstance(row[key], list) or not all(
                    isinstance(value, str) and value for value in row[key]
                ):
                    raise ValueError("Invalid patient evidence list")
            if row["target_malignancy_documented"] not in (
                "True",
                "False",
                "true",
                "false",
            ):
                raise ValueError("Invalid target evidence flag")
            row["target_malignancy_documented"] = (
                row["target_malignancy_documented"].lower() == "true"
            )
            row["positive_date"] = row["positive_date"] or None
            if row["positive_date"]:
                from .dates import date_bounds

                date_bounds(row["positive_date"])
            rows.append(row)
        return rows
