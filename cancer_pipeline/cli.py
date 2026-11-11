"""One command line for study-defined NOTE, PATH, ICD and cohort workflows."""

import argparse
import json
import os
from pathlib import Path
import sqlite3

from .cohorts import EXCLUSION_COLUMNS, FINAL_COLUMNS, merge_patients
from .icd import process_icd
from .outputs import PATIENT_COLUMNS, aggregate_outputs, read_patients
from .profiles import Study
from .runner import run
from .storage import save_csv, save_json


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check-study", help="Inspect study definitions and missing artifacts"
    )
    check.add_argument("--study", type=Path, required=True)
    inference = commands.add_parser(
        "run", help="Preflight NOTE or PATH; add --execute to send requests"
    )
    inference.add_argument("--study", type=Path, required=True)
    inference.add_argument("--module", choices=("note", "path"), required=True)
    inference.add_argument("--source", type=Path, nargs="+", required=True)
    inference.add_argument("--config", type=Path, default=Path("config.local.json"))
    inference.add_argument("--output-dir", type=Path, required=True)
    inference.add_argument("--execute", action="store_true")
    inference.add_argument("--workers", type=int, default=8)
    inference.add_argument("--budget-usd", type=float)
    aggregate = commands.add_parser(
        "aggregate", help="Offline JSON/JSONL.gz to patient evidence"
    )
    aggregate.add_argument("--study", type=Path, required=True)
    aggregate.add_argument("--module", choices=("note", "path"), required=True)
    aggregate.add_argument("--input", type=Path, nargs="+", required=True)
    aggregate.add_argument("--output-dir", type=Path, required=True)
    icd = commands.add_parser(
        "icd", help="Apply the study ICD code list without an LLM"
    )
    icd.add_argument("--study", type=Path, required=True)
    icd.add_argument("--source", type=Path, nargs="+", required=True)
    icd.add_argument("--output-dir", type=Path, required=True)
    merge = commands.add_parser("merge", help="Merge standard patient evidence CSVs")
    merge.add_argument("--study", type=Path, required=True)
    for source in ("note", "path", "icd"):
        merge.add_argument("--" + source, type=Path)
    merge.add_argument("--output-dir", type=Path, required=True)
    return root


def dispatch(args):
    study = Study.load(args.study)
    if args.command == "check-study":
        modules = {}
        for name, module in study.settings["modules"].items():
            fields = (
                ("prompt", "output_format")
                if module["validator"] in ("note", "path")
                else ("prompt", "output_schema")
            )
            modules[name] = {
                "missing": [
                    field
                    for field in fields
                    if not module.get(field)
                    or not study.artifact(module[field]).is_file()
                ],
                "validator": module["validator"],
            }
        return dict(
            study=study.settings["study_id"],
            status=study.settings["status"],
            target_sites=study.settings["target_sites"],
            modules=modules,
            icd_positive_codes=len(study.settings["icd"]["positive_codes"]),
        )
    if args.command == "run":
        config = json.loads(args.config.read_text())
        return run(
            study,
            args.module,
            args.source,
            args.output_dir,
            config,
            args.workers,
            args.budget_usd,
            args.execute,
        )
    if args.command == "aggregate":
        return aggregate_outputs(study, args.module, args.input, args.output_dir)
    if args.command == "icd":
        if not study.settings["icd"]["positive_codes"]:
            raise ValueError("Define the study ICD positive codes before processing")
        result = process_icd(args.source, study)
        args.output_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
        (args.output_dir / "csv").mkdir(mode=0o700)
        save_csv(
            args.output_dir / "csv/icd_record_level.csv",
            [
                "MRN",
                "icd10_source_id",
                "icd10_code",
                "icd10_date",
                "outcome",
                "date_reason",
                "source_file",
                "source_row",
            ],
            result["records"],
        )
        save_csv(
            args.output_dir / "csv/icd_patient_level.csv",
            PATIENT_COLUMNS,
            result["patients"],
        )
        summary = dict(
            study=study.settings["study_id"],
            module="icd",
            state="complete",
            study_sha256=study.fingerprint(),
            api_calls=0,
            records=len(result["records"]),
            patients=len(result["patients"]),
        )
        save_json(args.output_dir / "manifest.json", summary)
        return summary
    modules = {
        name: read_patients(getattr(args, name), study, name)
        for name in ("note", "path", "icd")
        if getattr(args, name) is not None
    }
    if not modules:
        raise ValueError("Supply at least one patient evidence source")
    result = merge_patients(modules, study)
    args.output_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    save_csv(args.output_dir / "target_cohort.csv", FINAL_COLUMNS, result["target"])
    save_csv(
        args.output_dir / "exclusion_cohort.csv", EXCLUSION_COLUMNS, result["exclusion"]
    )
    summary = dict(
        study=study.settings["study_id"],
        study_sha256=study.fingerprint(),
        state="complete",
        target_patients=len(result["target"]),
        excluded_or_review_patients=len(result["exclusion"]),
        api_calls=0,
    )
    save_json(args.output_dir / "manifest.json", summary)
    return summary


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    try:
        result = dispatch(args)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        StopIteration,
    ) as error:
        print(
            json.dumps(
                {
                    "state": "failed",
                    "error_type": type(error).__name__,
                    "action": "Check the study artifacts, source mappings and run directory.",
                }
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0
