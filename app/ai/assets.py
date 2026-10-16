"""Versioned asset loader with explicit consumption and integrity validation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator


def module_registry() -> dict[str, dict]:
    """Return stable types with both runtime and service dependency spellings."""
    return {m["module_type"]: {**m, "dependencies": m["depends_on"]}
            for m in AssetBundle().registry["modules"]}


class AssetBundle:
    def __init__(self, root: Path | None = None, *, verify: bool = True):
        self.root = root or Path(__file__).resolve().parents[2]
        self.consumed: set[str] = set()
        self.manifest = json.loads((self.root / "config/asset_manifest.json").read_text(encoding="utf-8"))
        self.registry = self.json("config/module_registry.json")
        self.policy = self.json("policies/runtime_policy.json")
        self.quality_patterns = self.json("config/lesson_quality_patterns.json")
        if verify:
            errors = self.validate()
            if errors:
                raise ValueError("Controlled asset bundle failed validation: " + "; ".join(errors[:5]))

    def read(self, relative: str) -> str:
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("Asset path must stay inside bundle")
        self.consumed.add(relative)
        return path.read_text(encoding="utf-8")

    def json(self, relative: str):
        return json.loads(self.read(relative))

    def schema(self, name: str, value) -> None:
        Draft202012Validator(self.json(f"schemas/{name}.schema.json")).validate(value)

    @property
    def version(self) -> str:
        return self.manifest["version"] + ":" + self.fingerprint()["bundle_hash"][:12]

    def fingerprint(self) -> dict:
        paths = sorted(entry["path"] for entry in self.manifest["assets"])
        hashes = {p: hashlib.sha256((self.root / p).read_bytes()).hexdigest() for p in paths}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
        return {"version": self.manifest["version"], "bundle_hash": digest,
                "consumed": sorted(self.consumed), "hashes": hashes}

    def prompt(self, workflow: str, inputs: dict, modules: list[dict]) -> str:
        from app.core.catalog import GRADES, LESSON_TYPES, STAGES, SUBJECTS
        inputs = dict(inputs)
        for key, options in [("subject", SUBJECTS), ("school_stage", STAGES), ("lesson_type", LESSON_TYPES), ("grade", GRADES)]:
            inputs[key] = next((entry["label"] for entry in options if entry["value"] == inputs.get(key)), inputs.get(key))
        parts = [self.read("prompts/system_prompt.md"), self.read("prompts/teacher_output_contract.md"),
                 self.read(f"prompts/{workflow}.md")]
        subject = inputs.get("subject", "")
        guide = {"数学": "math", "物理": "science", "化学": "science", "生物": "science",
                 "英语": "english", "语文": "chinese", "历史": "humanities", "地理": "humanities",
                 "道德与法治": "humanities", "道德与法治/思想政治": "humanities", "政治": "humanities"}.get(subject, "general")
        parts.append(self.read(f"prompts/subject_guides/{guide}.md"))
        requested = {m.get("module_type", "custom_module") for m in modules}
        known = {m["module_type"] for m in self.registry["modules"]}
        for module_type in sorted(requested):
            module_type = module_type if module_type in known else "custom_module"
            parts.append(self.read(f"prompts/module_generation/{module_type}.md"))
        patterns = [p["instruction"] for p in self.quality_patterns["patterns"]
                    if (not p.get("subjects") or subject in p["subjects"])
                    and (not p.get("stages") or inputs.get("school_stage") in p["stages"])
                    and (not p.get("lesson_types") or inputs.get("lesson_type") in p["lesson_types"])]
        parts.append("适用质量模式：\n" + "\n".join(patterns[:3]))
        parts.append("本次课题、学段和课型：" + json.dumps({k: inputs.get(k) for k in
                     ("topic", "school_stage", "grade", "subject", "lesson_type")}, ensure_ascii=False))
        return "\n\n".join(parts)

    def validate(self) -> list[str]:
        errors = []
        registered = {entry["path"] for entry in self.manifest["assets"]}
        for entry in self.manifest["assets"]:
            path = (self.root / entry["path"]).resolve()
            if not path.is_relative_to(self.root.resolve()) or not path.is_file():
                errors.append(f"missing asset: {entry['path']}")
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
                errors.append(f"hash mismatch: {entry['path']}")
            if entry.get("status") == "active" and not entry.get("consumer"):
                errors.append(f"active asset missing consumer: {entry['path']}")
            if path.suffix == ".json":
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if path.name.endswith(".schema.json"):
                        Draft202012Validator.check_schema(value)
                except Exception as exc:
                    errors.append(f"invalid JSON/schema: {entry['path']}: {type(exc).__name__}")
        for folder in ("config", "schemas", "prompts", "policies"):
            for path in (self.root / folder).rglob("*"):
                relative = path.relative_to(self.root).as_posix()
                if path.is_file() and relative != "config/asset_manifest.json" and relative not in registered:
                    errors.append(f"unregistered asset: {relative}")
        ids = {m["module_type"] for m in self.registry["modules"]}
        for module in self.registry["modules"]:
            if not set(module["depends_on"]) <= ids:
                errors.append(f"unknown dependency: {module['module_type']}")
        remaining = {m["module_type"]: set(m["depends_on"]) for m in self.registry["modules"]}
        while remaining:
            ready = {name for name, dependencies in remaining.items() if not dependencies & remaining.keys()}
            if not ready:
                errors.append("module dependency cycle")
                break
            remaining = {name: deps for name, deps in remaining.items() if name not in ready}
        return errors
