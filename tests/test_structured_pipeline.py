import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from cancer_pipeline.gateway import decode_response
from cancer_pipeline.profiles import Study
from cancer_pipeline.runner import run
from cancer_pipeline.validation import build_validator

ROOT = Path(__file__).resolve().parents[1]


class StructuredPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "mrn": {"type": ["string", "null"]},
                "record_id": {"type": ["string", "null"]},
                "document_date": {"type": ["string", "null"]},
                "classification": {"enum": ["Malignant", "Other", "Uncertain"]},
                "diagnosis_date": {"type": ["string", "null"]},
            },
            "required": [
                "mrn",
                "record_id",
                "document_date",
                "classification",
                "diagnosis_date",
            ],
        }
        self.schema_path = self.root / "output.schema.json"
        self.schema_path.write_text(json.dumps(self.schema))
        (self.root / "prompt.txt").write_text(
            "Apply the study classification and initial diagnosis date rules."
        )
        settings = json.loads((ROOT / "profiles/breast.example.json").read_text())
        settings.update(status="reviewed", study_id="synthetic")
        for name, module in settings["modules"].items():
            module.update(
                response_mode="structured",
                validator="json_schema",
                aggregation="classification",
                prompt="prompt.txt",
                output_schema="output.schema.json",
                model="fake",
                output_fields={
                    "patient_id": "mrn",
                    "source_id": "record_id",
                    "document_date": "document_date",
                    "classification": "classification",
                    "diagnosis_date": "diagnosis_date",
                },
            )
        self.profile_path = self.root / "study.json"
        self.profile_path.write_text(json.dumps(settings))
        self.study = Study.load(self.profile_path)
        self.config = {
            "provider": "openai",
            "base_url": "https://example.invalid",
            "api_key": "SYNTHETIC_TEST_ONLY",
            "data_approved_for_endpoint": True,
            "models": [
                {
                    "id": "fake",
                    "reasoning_effort": "low",
                    "rates_per_million": {
                        "input": 0.1,
                        "cached_input": 0.01,
                        "output": 0.5,
                    },
                }
            ],
        }
        self.prediction = {
            "mrn": "wrong",
            "record_id": "wrong",
            "document_date": "wrong",
            "classification": "Malignant",
            "diagnosis_date": "2001",
        }

    def source(self, module):
        settings = self.study.module(module)
        path = self.root / (module + ".csv")
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(
                stream, fieldnames=list(settings["input_columns"].values())
            )
            writer.writeheader()
            writer.writerow(
                {
                    settings["input_columns"][key]: value
                    for key, value in {
                        "patient_id": "001",
                        "source_id": "R1",
                        "document_date": "2025-01-01",
                        "text": "Synthetic study document.",
                    }.items()
                }
            )
        return path

    def response(self):
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(self.prediction)},
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        }

    def test_note_and_path_send_the_exact_strict_schema_and_resume(self):
        for module in ("note", "path"):
            with self.subTest(module=module):
                send = Mock(return_value=(self.response(), "synthetic-request"))
                source = self.source(module)
                directory = self.root / (module + "-run")
                run(
                    self.study,
                    module,
                    [source],
                    directory,
                    self.config,
                    execute=True,
                    budget=1,
                    send=send,
                )
                payload = send.call_args[0][0]
                self.assertEqual(payload["response_format"]["type"], "json_schema")
                self.assertTrue(payload["response_format"]["json_schema"]["strict"])
                self.assertEqual(
                    payload["response_format"]["json_schema"]["schema"], self.schema
                )
                self.assertEqual(
                    payload["response_format"]["json_schema"]["name"],
                    "synthetic_" + module,
                )
                self.assertNotIn('"required"', payload["messages"][0]["content"])
                with (directory / f"csv/{module}_patient_level.csv").open() as stream:
                    patient = next(csv.DictReader(stream))
                self.assertEqual(patient["MRN"], "001")
                self.assertEqual(patient["positive_date"], "2001")
                run(
                    self.study,
                    module,
                    [source],
                    directory,
                    self.config,
                    execute=True,
                    budget=1,
                    send=send,
                )
                send.assert_called_once()

    def test_bad_strict_schemas_are_rejected_before_requests(self):
        invalid = []
        schema = copy.deepcopy(self.schema)
        schema["required"].remove("record_id")
        invalid.append(schema)
        schema = copy.deepcopy(self.schema)
        schema["additionalProperties"] = True
        invalid.append(schema)
        schema = copy.deepcopy(self.schema)
        schema["if"] = {"properties": {"classification": {"const": "Malignant"}}}
        invalid.append(schema)
        for index, schema in enumerate(invalid):
            with self.subTest(index=index):
                self.schema_path.write_text(json.dumps(schema))
                send = Mock()
                with self.assertRaises(ValueError):
                    run(
                        self.study,
                        "note",
                        [self.source("note")],
                        self.root / str(index),
                        self.config,
                        execute=True,
                        budget=1,
                        send=send,
                    )
                send.assert_not_called()

    def test_schema_change_prevents_resume(self):
        source = self.source("note")
        directory = self.root / "resume"
        send = Mock(return_value=(self.response(), "request"))
        run(
            self.study,
            "note",
            [source],
            directory,
            self.config,
            execute=True,
            budget=1,
            send=send,
        )
        self.schema["properties"]["diagnosis_date"][
            "description"
        ] = "Changed definition"
        self.schema_path.write_text(json.dumps(self.schema))
        with self.assertRaises(ValueError):
            run(
                self.study,
                "note",
                [source],
                directory,
                self.config,
                execute=True,
                budget=1,
                send=send,
            )
        send.assert_called_once()

    def test_refusal_and_truncation_do_not_become_valid_output(self):
        response = self.response()
        response["choices"][0]["message"]["refusal"] = "Synthetic refusal"
        self.assertEqual(decode_response(response), ("refused", None))
        response["choices"][0].pop("message")
        response["choices"][0]["finish_reason"] = "length"
        self.assertEqual(decode_response(response), ("truncated", None))

    def test_extra_fields_and_invalid_enums_fail_local_validation(self):
        validate = build_validator(self.study, "note")
        self.assertTrue(validate(dict(self.prediction, extra_field="unexpected")))
        self.assertTrue(validate(dict(self.prediction, classification="unrecognized")))
        self.assertTrue(validate(dict(self.prediction, diagnosis_date="2023-02-29")))


if __name__ == "__main__":
    unittest.main()
