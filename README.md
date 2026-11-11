# Cancer Report Parser

Turn **progress notes, pathology reports, and ICD records** into traceable patient-level cancer data.

Use the same workflow across cancer types. You define the **prompts, classification rules, output formats, and ICD code lists**. The pipeline handles inference, validation, resuming runs, aggregation, and exports.

An ovarian study profile is included. A breast study template is available for you to complete.

## How the workflow runs

```mermaid
flowchart TD
    S["Your study definitions<br/>Prompts, output formats, classification and ICD rules"]

    N["Progress NOTE CSV"] --> NP["LLM extraction and classification"]
    P["Pathology CSV"] --> PP["LLM extraction and classification"]
    I["ICD CSV"] --> IP["Apply ICD code rules"]

    S --> NP
    S --> PP
    S --> IP

    NP --> NO["Full JSON outputs<br/>NOTE record-level CSV"]
    PP --> PO["Full JSON outputs<br/>PATH record-level CSV"]
    IP --> IO["ICD record-level CSV"]

    NO --> NM["Group by MRN<br/>NOTE patient-level CSV"]
    PO --> PM["Group by MRN<br/>PATH patient-level CSV"]
    IO --> IM["Group by MRN<br/>ICD patient-level CSV"]

    NM --> M["Merge the three sources by MRN"]
    PM --> M
    IM --> M

    M --> D{"Does NOTE or PATH provide<br/>an eligible target-cancer date?"}
    D -->|Yes| CD["Select the earliest eligible NOTE or PATH date"]
    D -->|No| ID["Check for an eligible ICD date"]
    CD --> O["Retain onset date, source-specific dates<br/>and supporting record IDs"]
    ID --> O
    O --> E{"Meets target criteria,<br/>has a date and passes required checks?"}
    E -->|Yes| T["target_cohort.csv"]
    E -->|No| X["exclusion_cohort.csv<br/>With exclusion or review reasons"]
```

**Parse each document first, then aggregate by MRN.** Multiple documents from the same patient can contribute date evidence. Individual outputs and source IDs remain available for review.

## What you define for each cancer type

Each study has a configuration file, such as [ovarian.json](profiles/ovarian.json). Start a breast study from [breast.example.json](profiles/breast.example.json).

- **Target scope** identifies the primary origins included in your study.
- **NOTE prompt** defines what to extract, how to classify it, and how to select diagnosis dates.
- **PATH prompt** defines specimen, diagnosis, primary-origin, and classification rules.
- **Output format** uses JSON Schema to define fields, types, and allowed values. Add the fields your study needs.
- **ICD rules** identify positive codes, auxiliary codes, and the source date fields.

Map five fields from your output into the shared workflow.

```text
Patient ID      → MRN
Source ID       → Note ID or report ID
Document date   → Encounter or report date
Classification  → The record's assigned category
Diagnosis date  → The date supported by that record
```

You can choose your own field names and map them through `output_fields`. For custom formats, the pipeline adds your JSON Schema to the prompt and validates the returned output.

The ovarian profile uses Malignant, Borderline, Benign, Other, and Uncertain. Complete and review the breast prompts, schemas, and ICD rules before executing that study.

[Read the study configuration guide and output-format example →](profiles/README.md)

## What you get

- **Full extraction outputs** in compressed JSONL, preserving all fields in your format, with one report per line.
- **Record-level CSVs** containing a summary for each document or ICD record, including classification, dates, source, and processing status.
- **Patient-level CSVs** for each source, containing the selected date, supporting source IDs, and review reasons.
- **Two final CSVs** containing the target cohort and the exclusion/review cohort.

For example, a completed NOTE run produces:

```text
runs/ovarian-note/
├── note/
│   ├── batch_0001.jsonl.gz
│   └── batch_0002.jsonl.gz
├── csv/
│   ├── note_record_level.csv
│   └── note_patient_level.csv
├── ledger.sqlite3       # Raw model responses, usage, and errors
└── manifest.json        # Progress and completion status
```

The final `target_cohort.csv` uses these columns in order:

```text
MRN, onset_date, source,
pathology_positive_date, progress_note_positive_date, icd10_positive_date,
in_pathology, in_progress_note, in_icd10,
pathology_source_id, progress_note_source_id, icd10_source_id
```

The exclusion CSV adds source classifications, review reasons, and `exclusion_reason`. It can include patients whose evidence or dates require further review.

## How dates are selected

**Use eligible NOTE/PATH dates first. Use an ICD date only when neither clinical source provides an eligible date.**

For example, if NOTE provides an initial diagnosis in 2001, PATH provides 2003, and ICD provides 1998, select **2001**.

If a report explicitly documents diagnosis in 2001 and recurrence in 2021, retain **2001**. If only the recurrence date is known, leave the initial diagnosis date missing. Preserve partial precision, such as `2001` or `2001-06`.

Only dates meeting the study's target-positive rules contribute to target onset. Dates associated with other classifications remain in the record-level outputs for review.

## Getting started

### 1. Install and inspect the study profile

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .

.venv/bin/python -m cancer_pipeline check-study --study profiles/ovarian.json
```

Requires Python 3.9+ on macOS or Linux. Model and gateway settings are shown in [config.example.json](config.example.json). Store credentials in local `config.local.json` or environment variables.

### 2. Check a source CSV before inference

```bash
.venv/bin/python -m cancer_pipeline run \
  --study profiles/ovarian.json --module note \
  --source data/notes.csv \
  --config config.example.json \
  --output-dir runs/ovarian-note
```

This performs local preflight. For live inference, use `config.local.json` with credentials and an approved endpoint, then add `--execute --budget-usd 20`. Choose your own budget amount. It controls a stopping threshold based on configured reference rates. Reconcile actual charges with the gateway.

For PATH, select `--module path`, the corresponding source CSV, and a separate output directory. Repeating the same command resumes the saved run.

### 3. Process ICD records

```bash
.venv/bin/python -m cancer_pipeline icd \
  --study profiles/ovarian.json --source data/icd.csv \
  --output-dir runs/ovarian-icd
```

### 4. Merge the completed sources

```bash
.venv/bin/python -m cancer_pipeline merge \
  --study profiles/ovarian.json \
  --note runs/ovarian-note/csv/note_patient_level.csv \
  --path runs/ovarian-path/csv/path_patient_level.csv \
  --icd runs/ovarian-icd/csv/icd_patient_level.csv \
  --output-dir runs/ovarian-cohorts
```

Supply your own input files under `data/`. Raw data, credentials, and run outputs are Git-ignored.

## More details

- [Full workflow guide](docs/reusable_workflow.md) covers resuming runs, error handling, offline JSON aggregation, and validation.
- [Study configuration guide](profiles/README.md) explains prompts, schemas, and field mappings.
- [Legacy ovarian commands and pilot notes](docs/legacy_ovarian_commands.md) preserves historical operations and experiments. Existing manual PATH adjudications are currently read by that workflow.

Run the local tests with:

```bash
.venv/bin/python -m unittest discover -s tests
```
