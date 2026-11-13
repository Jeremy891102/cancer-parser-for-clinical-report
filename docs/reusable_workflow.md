# Reusable NOTE, PATH and ICD workflow

The shared entry point is python -m cancer_pipeline. Study profiles own prompts, output formats, classification meaning, target organs, subtype references and ICD lists. Reusable modules handle scheduling, validation, checkpointing, token accounting, patient grouping and exports.

## Setup

From the repository root:

    python3 -m venv .venv
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -e .
    .venv/bin/python -m cancer_pipeline --help

Supported environments are macOS and Linux, with Python 3.9 or newer. Custom formats use jsonschema. Existing ovarian commands remain available.

## Define and check a study

Use profiles/ovarian.json for the existing definitions. Copy profiles/breast.example.json for a new study. Supply both prompts, output schemas, classification rules and ICD lists. The example reports missing artifacts and cannot execute inference while it is a draft.

    .venv/bin/python -m cancer_pipeline check-study --study profiles/ovarian.json
    .venv/bin/python -m cancer_pipeline check-study --study profiles/breast.example.json

See [the profile guide](../profiles/README.md) for field mappings. Additional extracted fields remain in your full JSON output.

## NOTE and PATH

Both modules accept one or more raw CSV files. Each row represents a source document. The study profile maps column names.

    .venv/bin/python -m cancer_pipeline run \
      --study profiles/ovarian.json --module note \
      --source /path/to/notes.csv \
      --config config.example.json \
      --output-dir runs/ovarian-note

This command performs local preflight. For an authorized live run, select a private gateway configuration and add --execute --budget-usd AMOUNT. Select --module path for pathology.

The same command resumes its run directory. The immutable contract covers the study, prompt, output contract, source content, model and endpoint. A timestamp change with identical file content is allowed on restart. A source change during execution stops processing. Use a new directory for changed clinical definitions.

The scheduler refills a slot as soon as a response arrives and checkpoints before submission and after receipt. Unknown outcomes and held server errors retain their state and cost reservation. HTTP 429 delays subsequent submissions according to Retry-After. Authentication errors and inconsistent usage stop the run. The local reference budget includes outstanding requests. Actual charges require gateway billing.

Each completed source module produces:

    runs/ovarian-note/
      contract.json
      manifest.json
      prompt.txt
      ledger.sqlite3
      note/
        batch_0001.jsonl.gz
      csv/
        note_record_level.csv
        note_patient_level.csv
      export_summary.json

PATH uses path for its folder and CSV names. The compressed batches contain valid JSON in your defined format. SQLite retains raw responses, usage, source locators and errors.

The record CSV retains every source row. Its columns are MRN, source_id, document_date, derived_classification, diagnosis_date, source_file, source_row and status. Invalid outputs retain processing status.

The common patient CSV columns are MRN, positive_date, source_ids, all_source_ids, categories, reasons, target_malignancy_documented and status. Lists are JSON strings in CSV cells.

## Aggregate existing JSON offline

Existing JSONL and compressed JSONL can be aggregated without model requests. Inputs can contain output objects or wrappers with case_id and output.

    .venv/bin/python -m cancer_pipeline aggregate \
      --study profiles/ovarian.json --module note \
      --input /path/to/predictions.jsonl.gz \
      --output-dir runs/ovarian-note-aggregation

The classifier and diagnosis date come from each output. The program selects supported dates, retains IDs and flags invalid or conflicting evidence. An undated record does not erase a supported date in another record. Overlapping partial dates remain reviewable.

The ovarian NOTE adapter preserves reviewed target-origin and unresolved-lesion checks. The PATH adapter preserves its deterministic aggregation. Historical manual longitudinal adjudications remain part of the legacy ovarian run. Migrating those adjudications is separate from re-aggregating raw model outputs.

## ICD

ICD processing uses the supplied exact code lists, normalizing decimal dots and letter case. It retains each source row with a stable filename:row ID. Auxiliary and family-history rows cannot independently supply target-positive dates. Invalid dates and placeholders retain a reason.

    .venv/bin/python -m cancer_pipeline icd \
      --study profiles/ovarian.json --source /path/to/icd.csv \
      --output-dir runs/ovarian-icd

Outputs are csv/icd_record_level.csv, csv/icd_patient_level.csv and a study manifest.

## Merge patient evidence

The merge command requires complete module patient CSVs and checks their study fingerprint and module manifest.

    .venv/bin/python -m cancer_pipeline merge \
      --study profiles/ovarian.json \
      --note runs/ovarian-note/csv/note_patient_level.csv \
      --path runs/ovarian-path/csv/path_patient_level.csv \
      --icd runs/ovarian-icd/csv/icd_patient_level.csv \
      --output-dir runs/ovarian-cohorts

If NOTE or PATH has an eligible target-positive date, choose the earliest eligible clinical date. ICD dates stay visible but do not compete with that date. ICD can supply the onset when both clinical sources lack a usable date. Unresolved ordering and the profile's blocking reasons go to the exclusion/review output.

The target CSV has these columns in order:

    MRN
    onset_date
    source
    pathology_positive_date
    progress_note_positive_date
    icd10_positive_date
    in_pathology
    in_progress_note
    in_icd10
    pathology_source_id
    progress_note_source_id
    icd10_source_id

The exclusion/review CSV adds source classifications, review reasons and exclusion_reason. Apply study eligibility rules before using its patients as controls.

## Verification and migration

    .venv/bin/python -m unittest discover -s tests

Tests cover custom NOTE/PATH fields, resume without replay, prompt-change rejection, ICD rules, clinical-date priority, partial-date conflicts and cross-study merge rejection. They use synthetic transport responses.

Legacy behavior was compared with a saved pre-refactor snapshot using 20 reviewed NOTE outputs, 56 saved NOTE predictions and all 4,435 saved PATH outputs. Validation and deterministic patient aggregation were identical. This checks software behavior separately from clinical accuracy.

The new entry point has been verified offline and with simulated gateway responses. New cancer definitions require their own reviewed examples before production inference.

## Structured Outputs

Each NOTE/PATH module can set `response_mode: "structured"` and supply `output_schema`. The runner sends that schema with `strict: true`; it does not duplicate custom schemas in the prompt. Clinical extraction/classification/date rules stay in the study prompt. Local validation and metadata restoration still apply. Missing response_mode preserves legacy JSON mode. Changed prompt/schema artifacts require a separate run directory. See the [configuration walkthrough](../README.md#define-a-different-output-format-with-structured-outputs).

## Continuing a legacy NOTE shard on another host

`run_note_partition.py --partition <private-spec.json>` adapts the existing NOTE production runner to a frozen, zero-based, stop-exclusive range of its original local six-file shard. It verifies a checksum-protected SQLite snapshot with a settled contiguous prefix before skipping those submitted rows. Each partition needs a separate private output root and a frozen partition contract. Dry-run is the default; `--execute` enables requests. Preserve original source case IDs. Retain the prefix snapshot, including held/error results, for reconciliation and the final merge; do not resume the old local shard after handing off its remaining range. The adapter does not change the production prompt, model, or response mode. Operational partition specifications and job IDs belong in ignored audit files.

## Offline NOTE format repair (existing ovarian contract)

`repair_note_outputs.py` repairs saved NOTE exceptions without inference or network access. It is an explicit postprocessing step, separate from the production validator. Input is gzip JSONL containing `case_id`, `source_file`, `source_row`, authoritative `metadata` (`mrn`, `note_id`, `note_signed_time`), and the original `prediction`.

```bash
.venv/bin/python repair_note_outputs.py \
  --input data/note_exceptions.jsonl.gz \
  --output-dir runs/note-offline-repairs
```

The output directory must be new. Accepted repairs include literal `"null"` to JSON null, documented key typos, reviewed enum aliases, case-only vocabulary corrections, an explicit allowlist of unlisted tumor names preserved under `other:`, and unambiguous US/English dates with four-digit years. Early/mid/late within a named year retain year precision. Decades, ages, relative times, approximate years, ranges, and two-digit years remain pending. Missing classifications are not generated; existing classifications remain unchanged.

Outputs are `repaired_predictions.jsonl.gz` (only validated records), `pending_review.jsonl.gz` (original and candidate, not accepted), `changes.jsonl.gz` (field-level audit), `repair_records.csv`, and a checksum-bearing `summary.json`. Every accepted record passes the existing NOTE validator and an idempotence check. Original ledgers are never edited. These repair files are an explicit overlay for a later reviewed merge; creating them does not update formal cohort CSVs or establish clinical accuracy.
