import copy
import json
from pathlib import Path
import unittest

from cancer_pipeline.note_repairs import repair_note

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = json.loads((ROOT / "schemas/note_output_format.json").read_text())
SUBTYPES = {
    r["name"] for r in json.loads((ROOT / "schemas/subtypes_56.json").read_text())
}


class NoteRepairTests(unittest.TestCase):
    def setUp(self):
        self.note = copy.deepcopy(TEMPLATE)
        self.note.update(
            mrn="001",
            note_id="N1",
            note_signed_time="2025-01-01",
            derived_classification="Other",
        )
        self.note["diagnoses"] = [
            dict(
                TEMPLATE["diagnoses"][0],
                diagnosis_as_documented="Synthetic diagnosis",
                temporal_status="history_only",
            )
        ]

    def repair(self, value=None):
        return repair_note(
            value if value is not None else self.note, TEMPLATE, SUBTYPES
        )

    def test_null_and_enum_repairs_preserve_original_and_classification(self):
        d = self.note["diagnoses"][0]
        d.update(
            histologic_subtype="null",
            diagnostic_qualification="confirmed",
            primary_origin_certainty="null",
            temporal_status="current",
        )
        original = copy.deepcopy(self.note)
        result = self.repair()
        self.assertEqual(self.note, original)
        self.assertEqual(result["status"], "ok_offline")
        fixed = result["output"]["diagnoses"][0]
        self.assertIsNone(fixed["histologic_subtype"])
        self.assertIsNone(fixed["diagnostic_qualification"])
        self.assertIsNone(fixed["primary_origin_certainty"])
        self.assertEqual(fixed["temporal_status"], "current_disease")
        self.assertEqual(result["output"]["derived_classification"], "Other")
        self.assertTrue(result["changes"])

    def test_unlisted_names_are_not_promoted_to_malignant_reference_types(self):
        self.note["diagnoses"][0]["histologic_subtype"] = "Carcinoid tumor"
        self.assertEqual(
            self.repair()["output"]["diagnoses"][0]["histologic_subtype"],
            "other: Carcinoid tumor",
        )
        self.note["diagnoses"][0][
            "histologic_subtype"
        ] = "teratoma, behavior unspecified"
        self.assertEqual(
            self.repair()["output"]["diagnoses"][0]["histologic_subtype"],
            "Teratoma, behavior unspecified",
        )
        self.note["diagnoses"][0]["histologic_subtype"] = "unreviewed made-up subtype"
        self.assertEqual(self.repair()["status"], "needs_review")

    def test_dates_preserve_precision_and_ambiguous_dates_remain_pending(self):
        for value, expected in [
            ("09/2017", "2017-09"),
            ("August 2005", "2005-08"),
            ("09/04/2015", "2015-09-04"),
            ("early 2018", "2018"),
        ]:
            with self.subTest(value=value):
                self.note["diagnosis_date"] = value
                result = self.repair()
                self.assertEqual(result["status"], "ok_offline")
                self.assertEqual(result["output"]["diagnosis_date"], expected)
        for value in (
            "1980s",
            "age 20",
            "2014/2015",
            "1/09",
            "~1998",
            "About 29 years ago",
            "02/30/2015",
        ):
            with self.subTest(value=value):
                self.note["diagnosis_date"] = value
                result = self.repair()
                self.assertEqual(result["status"], "needs_review")
                self.assertEqual(result["candidate"]["diagnosis_date"], value)
                self.assertIsNone(result["output"])

    def test_key_typos_and_displaced_subtype_key_are_repaired_without_inventing_values(
        self,
    ):
        self.note["family_history_of_ovarian_cancer"] = self.note.pop(
            "family_history_of_ovarian_cancer_documented"
        )
        d = self.note["diagnoses"][0]
        d["diagnosis_as_document"] = d.pop("diagnosis_as_documented")
        result = self.repair()
        self.assertEqual(result["status"], "ok_offline")
        d = copy.deepcopy(result["output"]["diagnoses"][0])
        del d["histologic_subtype"]
        d["other: high-grade carcinoma of mullerian origin; subtype unspecified"] = None
        self.note = result["output"]
        self.note["diagnoses"] = [d]
        self.assertEqual(self.repair()["status"], "ok_offline")
        del self.note["derived_classification"]
        result = self.repair()
        self.assertEqual(result["status"], "needs_review")
        self.assertNotIn("derived_classification", result["candidate"])

    def test_displaced_key_preserves_an_existing_subtype_value(self):
        diagnosis = self.note["diagnoses"][0]
        del diagnosis["histologic_subtype"]
        diagnosis["other: descriptive text used as key"] = (
            "other: existing subtype value"
        )
        result = self.repair()
        self.assertEqual(result["status"], "ok_offline")
        self.assertEqual(
            result["output"]["diagnoses"][0]["histologic_subtype"],
            "other: existing subtype value",
        )

    def test_source_metadata_restoration_and_idempotence(self):
        metadata = {"mrn": "0000123", "note_id": "ID", "note_signed_time": "2025-01-02"}
        result = repair_note(self.note, TEMPLATE, SUBTYPES, metadata)
        self.assertEqual(result["output"]["mrn"], "0000123")
        again = repair_note(result["output"], TEMPLATE, SUBTYPES, metadata)
        self.assertEqual(again["output"], result["output"])
        self.assertEqual(again["changes"], [])
