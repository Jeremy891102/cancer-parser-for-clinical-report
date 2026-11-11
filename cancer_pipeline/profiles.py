"""Study-owned clinical definitions and source/output mappings."""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re


@dataclass
class Study:
    path: Path
    settings: dict

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        settings = json.loads(path.read_text())
        if (
            not isinstance(settings.get("study_id"), str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]*", settings["study_id"])
            or settings.get("status") not in ("draft", "reviewed")
            or not isinstance(settings.get("target_sites"), list)
            or not settings["target_sites"]
            or not all(
                isinstance(site, str) and site for site in settings["target_sites"]
            )
        ):
            raise ValueError("Invalid study identity, status, or target sites")
        if set(settings.get("modules", {})) != {"note", "path"}:
            raise ValueError("Study must define NOTE and PATH module settings")
        if (
            not isinstance(settings.get("positive_classification"), str)
            or not settings["positive_classification"]
        ):
            raise ValueError("Define the study-positive classification")
        for module in settings["modules"].values():
            if module.get("validator") not in (
                "note",
                "path",
                "json_schema",
            ) or module.get("aggregation") not in ("note", "path", "classification"):
                raise ValueError("Invalid module validator or aggregation policy")
            for key, roles in (
                ("input_columns", {"patient_id", "source_id", "document_date", "text"}),
                ("input_metadata", {"patient_id", "source_id", "document_date"}),
                (
                    "output_fields",
                    {
                        "patient_id",
                        "source_id",
                        "document_date",
                        "classification",
                        "diagnosis_date",
                    },
                ),
            ):
                mapping = module.get(key, {})
                if (
                    set(mapping) != roles
                    or not all(
                        isinstance(value, str) and value for value in mapping.values()
                    )
                    or len(set(mapping.values())) != len(mapping)
                ):
                    raise ValueError("Invalid module field mappings")
            if (
                type(module.get("max_completion_tokens")) is not int
                or module["max_completion_tokens"] <= 0
                or not isinstance(module.get("model"), str)
                or not isinstance(module.get("text_key"), str)
            ):
                raise ValueError("Invalid module model or token settings")
        return cls(path, settings)

    def artifact(self, name):
        if not isinstance(name, str) or not name:
            raise ValueError("A required study artifact has not been supplied")
        return (self.path.parent / name).resolve()

    def module(self, name):
        if name not in ("note", "path"):
            raise ValueError("Unknown document module")
        return self.settings["modules"][name]

    def subtypes(self):
        reference = self.settings.get("subtype_reference")
        if reference is None:
            return {}
        rows = json.loads(self.artifact(reference).read_text())
        return {row["name"]: row["reference_group"] for row in rows}

    def fingerprint(self):
        digest = hashlib.sha256(self.path.read_bytes())
        paths = [self.settings.get("subtype_reference")]
        for module in self.settings["modules"].values():
            paths.extend(
                module.get(field)
                for field in ("prompt", "output_format", "output_schema")
            )
        for name in sorted({p for p in paths if p}):
            digest.update(name.encode())
            digest.update(self.artifact(name).read_bytes())
        return digest.hexdigest()

    def require_inference_ready(self, module):
        settings = self.module(module)
        if self.settings["status"] != "reviewed":
            raise ValueError(
                "Finish and review this study profile before executing inference"
            )
        for field in (
            "prompt",
            (
                "output_format"
                if settings["validator"] in ("note", "path")
                else "output_schema"
            ),
        ):
            if not self.artifact(settings.get(field)).is_file():
                raise ValueError("Study inference artifact is missing")
