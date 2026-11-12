"""Configurable CSV inference using the shared durable engine."""

from contextlib import closing
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import threading
import zlib
import fcntl

from .engine import infer, setup
from .gateway import (
    check_live_config,
    decode_response,
    estimate_cost,
    model_base_url,
    perform_request,
    request_completion,
    resolve_key,
)
from .storage import save_json
from .metadata import normalize_metadata
from .validation import build_validator, response_schema


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(2**20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_cases(paths, settings, fingerprints=None):
    csv.field_size_limit(100_000_000)
    columns = settings["input_columns"]
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not set(columns.values()) <= set(reader.fieldnames or []):
                raise ValueError("Source CSV is missing mapped columns")
            for number, row in enumerate(reader, 1):
                if fingerprints:
                    expected = fingerprints[str(path.resolve())]
                    stat = path.stat()
                    if (stat.st_size, stat.st_mtime_ns) != (
                        expected["bytes"],
                        expected["mtime_ns"],
                    ):
                        raise ValueError("Source file changed during inference")
                values = {key: row[column] for key, column in columns.items()}
                if (
                    any(value is None for value in values.values())
                    or not values["source_id"]
                ):
                    raise ValueError(
                        "Source CSV row is malformed or missing its record ID"
                    )
                metadata = {
                    key: values[key] or None
                    for key in ("patient_id", "source_id", "document_date")
                }
                metadata = normalize_metadata(metadata)
                if metadata["source_id"] is None:
                    raise ValueError("Source record ID is missing")
                provider_metadata = {
                    settings["input_metadata"][key]: value
                    for key, value in metadata.items()
                }
                yield {
                    "case_id": f"{path.name}:{number}",
                    "source_file": path.name,
                    "source_row": number,
                    "metadata": metadata,
                    "ledger_metadata": dict(
                        pat_mrn_id=metadata["patient_id"],
                        note_id=metadata["source_id"],
                        note_signed_time=metadata["document_date"],
                    ),
                    "input": {
                        "metadata": provider_metadata,
                        settings["text_key"]: values["text"],
                    },
                }


def preflight(study, module, paths, model):
    if not paths or len({p.name for p in paths}) != len(paths):
        raise ValueError("Select source files with unique file names")
    settings = study.module(module)
    study.require_inference_ready(module)
    build_validator(study, module)
    files = []
    for path in paths:
        before = path.stat()
        digest = file_digest(path)
        count = sum(1 for _ in read_cases([path], settings))
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Source changed during preflight")
        files.append(
            {
                "path": str(path.resolve()),
                "sha256": digest,
                "records": count,
                "bytes": after.st_size,
                "mtime_ns": after.st_mtime_ns,
            }
        )
    if not sum(file["records"] for file in files):
        raise ValueError("No source records")
    return {
        "study_id": study.settings["study_id"],
        "study_sha256": study.fingerprint(),
        "module": module,
        "files": files,
        "model": {
            key: model.get(key)
            for key in ("id", "model_version", "reasoning_effort", "rates_per_million")
        },
        "completion_cap": settings["max_completion_tokens"],
    }


def validate_result(result, case, payload, limit, rates, validate, settings):
    cost = estimate_cost(result["usage"], rates)
    result["estimated_usd"] = cost
    if cost is None:
        return "invalid_usage"
    if (
        result["usage"]["prompt_tokens"] > limit
        or result["usage"]["completion_tokens"] > payload["max_completion_tokens"]
    ):
        return "usage_over_reserve"
    status, prediction = decode_response(result["raw_response"])
    if status != "parsed":
        return status
    errors = validate(prediction)
    result["validation_errors"] = errors
    if errors:
        return "invalid_schema"
    fields = settings["output_fields"]
    output = dict(prediction)
    restored = []
    for key in ("patient_id", "source_id", "document_date"):
        field = fields[key]
        if output.get(field) != case["metadata"][key]:
            restored.append(field)
        output[field] = case["metadata"][key]
    if validate(output):
        return "invalid_source_metadata"
    result.update(output=output, metadata_restorations=restored)
    return "ok"


def render_prompt(study, module):
    settings = study.module(module)
    prompt = study.artifact(settings["prompt"]).read_text()
    if settings["validator"] == "json_schema":
        if settings.get("response_mode", "json") == "json":
            schema = json.loads(study.artifact(settings["output_schema"]).read_text())
            prompt += "\n\nReturn one JSON object matching this study-defined output schema.\n"
            prompt += json.dumps(schema, ensure_ascii=False, indent=2)
        reference = study.settings.get("subtype_reference")
        if reference:
            prompt += "\n\nStudy-defined subtype reference.\n"
            prompt += study.artifact(reference).read_text()
    return prompt


def resume_contract(contract):
    return dict(
        contract,
        files=[
            {key: value for key, value in source.items() if key != "mtime_ns"}
            for source in contract["files"]
        ],
    )


def run(
    study,
    module,
    paths,
    directory,
    config,
    workers=8,
    budget=None,
    execute=False,
    send=None,
):
    settings = study.module(module)
    model = next(
        model for model in config["models"] if model["id"] == settings["model"]
    )
    contract = preflight(study, module, paths, model)
    base_url = model_base_url(config, model)
    contract.update(endpoint=base_url, provider=config.get("provider", "openai"))
    if not execute:
        return dict(
            mode="dry_run",
            records=sum(file["records"] for file in contract["files"]),
            module=module,
            study=contract["study_id"],
        )
    rates = model.get("rates_per_million")
    if (
        type(workers) is not int
        or not 1 <= workers <= 16
        or type(budget) not in (int, float)
        or not math.isfinite(budget)
        or budget <= 0
        or not isinstance(rates, dict)
        or any(
            type(rates.get(key)) not in (int, float)
            or not math.isfinite(rates[key])
            or rates[key] <= 0
            for key in ("input", "cached_input", "output")
        )
        or rates["cached_input"] > rates["input"]
    ):
        raise ValueError(
            "Specify worker count, positive reference rates and a finite budget"
        )
    if config.get("provider") == "nyu_gateway":
        override = os.environ.get("LLM_API_BASE_URL")
        if override and override.rstrip("/") != config["base_url"].rstrip("/"):
            raise ValueError(
                "Endpoint override differs from the approved configuration"
            )
    key = resolve_key(config)
    check_live_config(dict(config, base_url=base_url), key)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if (
        directory.is_symlink()
        or directory.stat().st_uid != os.getuid()
        or directory.stat().st_mode & 0o077
    ):
        raise ValueError("Run directory must be private and owned by the current user")
    with (directory / "runner.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        contract_path = directory / "contract.json"
        if contract_path.exists() and resume_contract(
            json.loads(contract_path.read_text())
        ) != resume_contract(contract):
            raise ValueError("Study, input files, endpoint or model changed on resume")
        save_json(contract_path, contract)
        prompt = render_prompt(study, module)
        (directory / "prompt.txt").write_text(prompt)
        stop = threading.Event()
        old_handlers = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                old_handlers[signum] = signal.signal(signum, lambda *_: stop.set())
        validate = build_validator(study, module)
        sender = send or (
            lambda payload: request_completion(
                base_url, payload, key, gateway=config.get("provider") == "nyu_gateway"
            )
        )

        def worker(case, payload, limit):
            return perform_request(
                payload,
                limit,
                rates,
                sender,
                lambda result: validate_result(
                    result, case, payload, limit, rates, validate, settings
                ),
            )

        def checkpoint(state, count, cost):
            save_json(
                directory / "manifest.json",
                {
                    "study": contract["study_id"],
                    "module": module,
                    "state": state,
                    "records": count,
                    "expected_records": sum(
                        file["records"] for file in contract["files"]
                    ),
                    "estimated_usd_known_usage": cost,
                    "reference_budget_usd": budget,
                    "rates_verified_with_gateway": False,
                },
            )

        try:
            with closing(sqlite3.connect(directory / "ledger.sqlite3")) as db:
                setup(db)
                state = infer(
                    db,
                    read_cases(
                        paths, settings, {f["path"]: f for f in contract["files"]}
                    ),
                    prompt,
                    rates,
                    budget,
                    workers,
                    worker,
                    checkpoint,
                    model_version=model.get("model_version", model["id"]),
                    completion_cap=settings["max_completion_tokens"],
                    reasoning_effort=model.get("reasoning_effort"),
                    stop_event=stop,
                    text_key=settings["text_key"],
                    output_schema=response_schema(study, module),
                    schema_name=(study.settings["study_id"] + "_" + module)[:64],
                )
                count, cost = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(usd),0) FROM notes"
                ).fetchone()
                checkpoint("exporting" if state == "complete" else state, count, cost)
                if state == "complete":
                    from .outputs import export_run

                    export_run(db, directory, study, module)
                    checkpoint("complete", count, cost)
                return json.loads((directory / "manifest.json").read_text())
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)


def ledger_records(db, fields, ordering="position"):
    if ordering not in ("position", "mrn,position"):
        raise ValueError("Invalid record ordering")
    for case_id, file, row, mrn, source_id, date, status, blob in db.execute(
        "SELECT case_id,source_file,source_row,mrn,note_id,signed,status,result FROM notes ORDER BY "
        + ordering
    ):
        result = json.loads(zlib.decompress(blob)) if blob else {}
        metadata = dict(patient_id=mrn, source_id=source_id, document_date=date)
        output = result.get("output")
        if not output:
            output = {
                fields[key]: metadata[key]
                for key in ("patient_id", "source_id", "document_date")
            }
        yield dict(
            case_id=case_id,
            source_file=file,
            source_row=row,
            status=status,
            metadata=metadata,
            output=output,
        )
