import contextlib
import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from cancer_pipeline.aggregation import aggregate_module
from cancer_pipeline.cli import main
from cancer_pipeline.cohorts import merge_patients
from cancer_pipeline.icd import process_icd
from cancer_pipeline.outputs import read_patients
from cancer_pipeline.profiles import Study
from cancer_pipeline.runner import run
from cancer_pipeline.validation import build_validator

ROOT = Path(__file__).resolve().parents[1]


class ReusableWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ovarian = Study.load(ROOT / "profiles/ovarian.json")
        self.settings = copy.deepcopy(self.ovarian.settings)
        self.settings.update(
            study_id="breast_test",
            status="reviewed",
            target_sites=["breast"],
            subtype_reference=None,
            positive_classification="target",
        )
        self.settings["icd"]["positive_codes"] = ["TEST.1"]
        self.settings["icd"]["auxiliary_codes"] = ["AUX.1"]
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "person": {"type": ["string", "null"]},
                "document": {"type": "string"},
                "event": {"type": ["string", "null"]},
                "outcome": {"enum": ["target", "unresolved", "outside"]},
                "diagnosed_on": {"type": ["string", "null"]},
                "custom_detail": {"type": "string"},
            },
            "required": [
                "person",
                "document",
                "event",
                "outcome",
                "diagnosed_on",
                "custom_detail",
            ],
        }
        (self.root / "schema.json").write_text(json.dumps(schema))
        (self.root / "prompt.txt").write_text(
            "Synthetic test instructions. Return the supplied JSON format."
        )
        for name, module in self.settings["modules"].items():
            module.update(
                prompt="prompt.txt",
                output_schema="schema.json",
                output_format=None,
                validator="json_schema",
                response_mode="json",
                aggregation="classification",
                model="fake",
                max_completion_tokens=3000,
                input_columns=dict(
                    patient_id="mrn",
                    source_id="document_id",
                    document_date="document_date",
                    text="text",
                ),
                output_fields=dict(
                    patient_id="person",
                    source_id="document",
                    document_date="event",
                    classification="outcome",
                    diagnosis_date="diagnosed_on",
                ),
            )
        self.profile = self.root / "study.json"
        self.profile.write_text(json.dumps(self.settings))
        self.study = Study.load(self.profile)
        self.prediction = dict(
            person="MODEL_WRONG",
            document="MODEL_WRONG",
            event="MODEL_WRONG",
            outcome="target",
            diagnosed_on="2001",
            custom_detail="user-defined field",
        )

    def source(self):
        path = self.root / "source.csv"
        path.write_text(
            "mrn,document_id,document_date,text\n001,A,2025-01-01,synthetic\n"
        )
        return path

    def config(self):
        return dict(
            provider="openai",
            base_url="https://example.invalid",
            api_key="SYNTHETIC_TEST_ONLY",
            data_approved_for_endpoint=True,
            models=[
                dict(
                    id="fake",
                    reasoning_effort="low",
                    rates_per_million=dict(input=0.1, cached_input=0.01, output=0.5),
                )
            ],
        )

    def response(self, value=None):
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(value or self.prediction)},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }

    def test_custom_note_and_path_schemas_execute_and_resume_with_mock_transport(self):
        for module in ("note", "path"):
            with self.subTest(module=module):
                directory = self.root / module
                send = Mock(return_value=(self.response(), "synthetic-request"))
                result = run(
                    self.study,
                    module,
                    [self.source()],
                    directory,
                    self.config(),
                    execute=True,
                    budget=1,
                    send=send,
                )
                self.assertEqual(result["state"], "complete")
                run(
                    self.study,
                    module,
                    [self.source()],
                    directory,
                    self.config(),
                    execute=True,
                    budget=1,
                    send=send,
                )
                send.assert_called_once()
                payload = send.call_args[0][0]
                self.assertIn("custom_detail", payload["messages"][0]["content"])
                self.assertEqual(payload["response_format"], {"type": "json_object"})
                patient = read_patients(directory / f"csv/{module}_patient_level.csv")[
                    0
                ]
                self.assertEqual(patient["MRN"], "001")
                self.assertEqual(patient["positive_date"], "2001")
                self.assertEqual(patient["source_ids"], ["A"])
                self.assertEqual(patient["status"], "target_positive")
                ledger_text = "".join(p.read_text() for p in directory.glob("*.json"))
                self.assertNotIn("SYNTHETIC_TEST_ONLY", ledger_text)

    def test_dry_run_makes_no_requests_or_run_directory(self):
        send = Mock()
        directory = self.root / "dry"
        result = run(
            self.study, "note", [self.source()], directory, self.config(), send=send
        )
        self.assertEqual(result["mode"], "dry_run")
        self.assertFalse(directory.exists())
        send.assert_not_called()

    def test_schema_and_calendar_errors_are_rejected_without_leaking_values(self):
        validate = build_validator(self.study, "note")
        value = dict(self.prediction, custom_detail=12)
        self.assertTrue(validate(value))
        self.assertTrue(validate(dict(self.prediction, diagnosed_on="2023-02-29")))
        self.assertTrue(validate(dict(self.prediction, outcome="not-user-defined")))

    def test_changed_prompt_cannot_resume_existing_run(self):
        directory = self.root / "resume"
        send = Mock(return_value=(self.response(), "request"))
        run(
            self.study,
            "note",
            [self.source()],
            directory,
            self.config(),
            execute=True,
            budget=1,
            send=send,
        )
        (self.root / "prompt.txt").write_text("changed")
        with self.assertRaises(ValueError):
            run(
                self.study,
                "note",
                [self.source()],
                directory,
                self.config(),
                execute=True,
                budget=1,
                send=send,
            )
        send.assert_called_once()

    def test_draft_breast_profile_cannot_send_inference(self):
        draft = Study.load(ROOT / "profiles/breast.example.json")
        with self.assertRaises(ValueError):
            draft.require_inference_ready("note")

    def test_clinical_date_wins_over_earlier_icd_and_icd_falls_back_when_needed(self):
        def patient(date, ids):
            return dict(
                MRN="001",
                positive_date=date,
                source_ids=ids,
                categories=["target"],
                reasons=[],
                target_malignancy_documented=True,
            )

        result = merge_patients(
            {
                "note": [patient("2001", ["N"])],
                "path": [patient("2005", ["P"])],
                "icd": [patient("1999", ["I"])],
            },
            self.study,
        )
        self.assertEqual(result["target"][0]["onset_date"], "2001")
        self.assertEqual(result["target"][0]["source"], "progress_note")
        self.assertEqual(result["target"][0]["icd10_positive_date"], "1999")
        result = merge_patients(
            {"note": [patient(None, [])], "icd": [patient("1999", ["I"])]}, self.study
        )
        self.assertEqual(result["target"][0]["source"], "icd10")

    def test_unknown_date_does_not_veto_dated_evidence_and_ambiguous_precision_is_review(
        self,
    ):
        value = dict(self.prediction, person="001", document="A", event=None)
        records = [
            {"output": value},
            {"output": dict(value, document="B", diagnosed_on=None)},
        ]
        result = aggregate_module(records, self.study, "note")
        self.assertEqual(result["patients"][0]["positive_date"], "2001")
        records[1]["output"]["diagnosed_on"] = "2001-06"
        result = aggregate_module(records, self.study, "note")
        self.assertIsNone(result["patients"][0]["positive_date"])
        self.assertIn("overlapping_date_precision", result["patients"][0]["reasons"])

    def test_icd_is_configured_and_family_history_and_auxiliary_are_not_positive(self):
        source = self.root / "icd.csv"
        source.write_text(
            "MRN,ICD10_CODE,DX_DATE,ICD_CODE_TYPE\n"
            "001,TEST.1,2001,Clinical Encounter\n"
            "002,TEST.1,1999,FamilyHistory\n"
            "003,AUX.1,1998,Clinical Encounter\n"
            "004,TEST.1,1899-12-30,Clinical Encounter\n"
        )
        result = process_icd([source], self.study)
        self.assertEqual(
            [r["outcome"] for r in result["records"]],
            ["positive", "family_history", "auxiliary", "positive"],
        )
        self.assertEqual(
            [r["positive_date"] for r in result["patients"]], ["2001", None, None, None]
        )
        self.assertEqual(result["records"][0]["icd10_source_id"], "icd.csv:1")

    def test_cli_icd_and_merge_emit_final_csvs_with_traceable_source_dates(self):
        source = self.root / "icd.csv"
        source.write_text(
            "MRN,ICD10_CODE,DX_DATE,ICD_CODE_TYPE\n001,TEST.1,2001,Clinical Encounter\n"
        )
        out = self.root / "icd_output"
        final = self.root / "final"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "icd",
                        "--study",
                        str(self.profile),
                        "--source",
                        str(source),
                        "--output-dir",
                        str(out),
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "merge",
                        "--study",
                        str(self.profile),
                        "--icd",
                        str(out / "csv/icd_patient_level.csv"),
                        "--output-dir",
                        str(final),
                    ]
                ),
                0,
            )
        with (final / "target_cohort.csv").open() as stream:
            row = next(csv.DictReader(stream))
        self.assertEqual(row["MRN"], "001")
        self.assertEqual(row["onset_date"], "2001")
        self.assertEqual(row["in_progress_note"], "False")
        self.assertEqual(json.loads(row["icd10_source_id"]), ["icd.csv:1"])

    def test_cli_offline_aggregate_accepts_user_output_and_preserves_precision(self):
        source = self.root / "predictions.jsonl"
        value = dict(self.prediction, person="001", document="A", event="2025-01-01")
        source.write_text(json.dumps(value) + "\n")
        out = self.root / "aggregated"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "aggregate",
                        "--study",
                        str(self.profile),
                        "--module",
                        "note",
                        "--input",
                        str(source),
                        "--output-dir",
                        str(out),
                    ]
                ),
                0,
            )
        rows = read_patients(out / "csv/note_patient_level.csv")
        self.assertEqual(rows[0]["positive_date"], "2001")
        self.assertEqual(
            json.loads((out / "manifest.json").read_text())["state"], "complete"
        )

    def test_merge_rejects_patient_evidence_from_a_different_study(self):
        source = self.root / "icd.csv"
        source.write_text(
            "MRN,ICD10_CODE,DX_DATE,ICD_CODE_TYPE\n001,TEST.1,2001,Clinical Encounter\n"
        )
        out = self.root / "icd_output"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "icd",
                        "--study",
                        str(self.profile),
                        "--source",
                        str(source),
                        "--output-dir",
                        str(out),
                    ]
                ),
                0,
            )
            self.assertEqual(
                main(
                    [
                        "merge",
                        "--study",
                        str(ROOT / "profiles/ovarian.json"),
                        "--icd",
                        str(out / "csv/icd_patient_level.csv"),
                        "--output-dir",
                        str(self.root / "wrong_merge"),
                    ]
                ),
                1,
            )
        self.assertFalse((self.root / "wrong_merge").exists())

    def test_export_failure_does_not_publish_complete_manifest(self):
        from unittest.mock import patch

        directory = self.root / "export-failure"
        send = Mock(return_value=(self.response(), "request"))
        with patch(
            "cancer_pipeline.outputs.export_run", side_effect=OSError("synthetic")
        ):
            with self.assertRaises(OSError):
                run(
                    self.study,
                    "note",
                    [self.source()],
                    directory,
                    self.config(),
                    execute=True,
                    budget=1,
                    send=send,
                )
        self.assertEqual(
            json.loads((directory / "manifest.json").read_text())["state"], "exporting"
        )

    def test_invalid_output_schema_is_rejected_before_any_request(self):
        schema = json.loads((self.root / "schema.json").read_text())
        del schema["properties"]["event"]
        (self.root / "schema.json").write_text(json.dumps(schema))
        send = Mock()
        with self.assertRaises(ValueError):
            run(
                self.study,
                "note",
                [self.source()],
                self.root / "bad-schema",
                self.config(),
                execute=True,
                budget=1,
                send=send,
            )
        send.assert_not_called()

    def test_missing_patient_sentinel_remains_unlinked(self):
        source = self.root / "unlinked.csv"
        source.write_text(
            "MRN,ICD10_CODE,DX_DATE,ICD_CODE_TYPE\nnull,TEST.1,2001,Clinical Encounter\n"
        )
        result = process_icd([source], self.study)
        self.assertIsNone(result["records"][0]["MRN"])
        self.assertEqual(result["patients"], [])

    def test_offline_aggregate_uses_authoritative_envelope_metadata(self):
        source = self.root / "wrapped.jsonl"
        source.write_text(
            json.dumps(
                {
                    "case_id": "case-1",
                    "output": self.prediction,
                    "metadata": {
                        "patient_id": "001",
                        "source_id": "A",
                        "document_date": "2025-01-01",
                    },
                }
            )
            + "\n"
        )
        directory = self.root / "authoritative"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "aggregate",
                        "--study",
                        str(self.profile),
                        "--module",
                        "note",
                        "--input",
                        str(source),
                        "--output-dir",
                        str(directory),
                    ]
                ),
                0,
            )
        patient = read_patients(directory / "csv/note_patient_level.csv")[0]
        self.assertEqual(patient["MRN"], "001")
        self.assertEqual(patient["source_ids"], ["A"])


if __name__ == "__main__":
    unittest.main()
