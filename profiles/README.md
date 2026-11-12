# Study definitions

A study profile defines the clinical task for NOTE, PATH, ICD, patient aggregation and cohort merging. Copy breast.example.json for a new study. Supply and review its clinical definitions, then set status to reviewed.

Each study supplies the following.

- target_sites lists the normalized primary origins included in the study.
- positive_classification names the category eligible for the target cohort.
- modules.note.prompt and modules.path.prompt point to your extraction instructions. Define classification priorities, history/recurrence rules, dates and subtype rules here.
- modules.*.output_schema points to your JSON Schema when validator is json_schema. Define fields, types, required keys and allowed classifications. With response_mode structured, the pipeline sends this schema as a strict API constraint; with json, it appends the schema to the prompt. Both modes validate returned JSON.
- modules.*.output_fields maps five top-level fields from your JSON into the common workflow. They represent the patient ID, source ID, document date, classification and diagnosis date. Additional fields and nested content follow your schema.
- modules.*.input_columns, input_metadata and text_key describe the raw CSV columns and the model input. Identifiers remain strings.
- modules.*.model and max_completion_tokens select the configured model and response allowance.
- modules.*.aggregation selects classification for a new custom format. The note and path adapters preserve the reviewed ovarian contracts and checks.
- subtype_reference optionally supplies your study vocabulary. It is included with a custom prompt. Your schema can constrain subtype names with an enum.
- icd supplies positive codes, auxiliary codes, source columns, excluded categories and placeholder dates.
- merge_blocking_reasons defines which review flags prevent automatic entry into the dated target cohort.

The ovarian profile references the existing NOTE v1.3, PATH v1.5, fixed output templates and 56-item vocabulary. Its legacy field names remain supported.

## Custom output example

This example demonstrates the mapped fields. Extend it with the diagnoses and details needed for your study.

    {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "mrn": {"type": ["string", "null"]},
        "record_id": {"type": "string"},
        "document_date": {"type": ["string", "null"]},
        "classification": {
          "enum": ["Malignant", "Borderline", "Benign", "Other", "Uncertain"]
        },
        "diagnosis_date": {"type": ["string", "null"]}
      },
      "required": [
        "mrn", "record_id", "document_date", "classification", "diagnosis_date"
      ]
    }

Its output_fields mapping would be:

    {
      "patient_id": "mrn",
      "source_id": "record_id",
      "document_date": "document_date",
      "classification": "classification",
      "diagnosis_date": "diagnosis_date"
    }

positive_classification must appear in the classification enum. Dates preserve YYYY, YYYY-MM or YYYY-MM-DD and must be valid calendar dates. Source IDs and document timestamps are restored from the input CSV. The raw model response remains in the audit ledger.

Use a self-contained JSON Schema. The validator uses an empty external-reference registry. JSON Schema validates structure, while your prompt defines clinical classification meaning. Set modules.*.response_mode to structured for strict API Structured Outputs, or json for legacy JSON mode. Missing response_mode defaults to json. Structured mode requires output_schema even when using an existing ovarian validator. [Schema validation documentation](https://python-jsonschema.readthedocs.io/en/stable/validate/)

See the [README configuration walkthrough](../README.md#define-a-different-output-format-with-structured-outputs) for exact file paths and strict-schema requirements. Clinical rules remain in the prompt; the schema defines structure.
