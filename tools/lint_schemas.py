"""Schema + rule-registry linter — runs in CI to enforce R3, R6-lite, and R11.

Checks (each maps to a bead acceptance criterion):

  C1 — All harness/schemas/*.json validate sample fixtures via jsonschema.
  C2 — Rule-id uniqueness across harness/rules/AX####.yaml.
  C3 — Every rule referenced in fixtures/findings*.json or fixtures/claims*.json
       exists in the registry (no orphan references).
  C4 — No free-text field (`notes`, `description`) on any default-surface
       schema (everything except harness/rules/* and the rule-registry sheet).
  C5 — claims.json must NOT permit `score`, `weight`, `threshold`, or
       `coverage` properties (Tension-1 default-output rule).
  C6 — report.json schema marks `regression_delta` required (R2).
  C7 — trace.jsonl.schema.json marks `mode` required (R26).
  C8 — run.json uses *_capability_profile fields, never `model_id`
       (P1 mitigation against Theme C).
  C9 — claim_class is pattern-validated as `^AX-CC-...`, never an enum
       (Theme D mitigation).
  C10 — _index.yaml lists every AX####.yaml on disk.

Exit code 0 = pass, 1 = lint failures, 2 = internal error.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import yaml
from jsonschema import Draft7Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT7

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "harness" / "schemas"
RULES_DIR = REPO_ROOT / "harness" / "rules"
FIXTURES_DIR = REPO_ROOT / "harness" / "fixtures"

DEFAULT_SURFACE_SCHEMAS = {
    "findings.json",
    "claims.json",
    "trace.jsonl.schema.json",
    "metrics.json",
    "run.json",
    "report.json",
}
FORBIDDEN_FREETEXT_PROPS = {"notes", "description"}
FORBIDDEN_AGGREGATION_PROPS = {"score", "weight", "threshold", "coverage"}
RULE_ID_PATTERN = "^AX[0-9]{4}$"
CLAIM_CLASS_PATTERN_PREFIX = "^AX-CC-"


class LintError(Exception):
    pass


def _walk_property_names(schema: dict) -> Iterable[tuple[str, dict]]:
    """Yield (property_name, sub-schema) for every nested `properties` block."""
    if not isinstance(schema, dict):
        return
    if "properties" in schema and isinstance(schema["properties"], dict):
        for name, sub in schema["properties"].items():
            yield name, sub
            yield from _walk_property_names(sub)
    for key in ("definitions", "items", "patternProperties"):
        node = schema.get(key)
        if isinstance(node, dict):
            for name, sub in node.items():
                if isinstance(sub, dict):
                    yield from _walk_property_names(sub)
        elif isinstance(node, list):
            for sub in node:
                if isinstance(sub, dict):
                    yield from _walk_property_names(sub)
    for key in ("allOf", "anyOf", "oneOf"):
        nodes = schema.get(key)
        if isinstance(nodes, list):
            for sub in nodes:
                if isinstance(sub, dict):
                    yield from _walk_property_names(sub)


def load_schemas() -> dict[str, dict]:
    schemas: dict[str, dict] = {}
    for path in sorted(SCHEMAS_DIR.glob("*.json")):
        with path.open("r", encoding="utf-8") as fh:
            schemas[path.name] = json.load(fh)
    return schemas


def check_default_surface_no_freetext(
    schemas: dict[str, dict], errors: list[str]
) -> None:
    for name, schema in schemas.items():
        if name not in DEFAULT_SURFACE_SCHEMAS:
            continue
        for prop_name, _sub in _walk_property_names(schema):
            if prop_name in FORBIDDEN_FREETEXT_PROPS:
                errors.append(
                    f"C4 free-text property '{prop_name}' is forbidden in "
                    f"default-surface schema {name}"
                )


def check_claims_aggregation_ban(
    schemas: dict[str, dict], errors: list[str]
) -> None:
    claims = schemas.get("claims.json")
    if claims is None:
        errors.append("C5 claims.json schema is missing")
        return
    for prop_name, _sub in _walk_property_names(claims):
        if prop_name in FORBIDDEN_AGGREGATION_PROPS:
            errors.append(
                f"C5 aggregation property '{prop_name}' must not appear in "
                f"claims.json (Tension-1 default-output rule)"
            )


def check_report_regression_delta_required(
    schemas: dict[str, dict], errors: list[str]
) -> None:
    report = schemas.get("report.json")
    if report is None:
        errors.append("C6 report.json schema is missing")
        return
    required = report.get("required", [])
    if "regression_delta" not in required:
        errors.append(
            "C6 report.json must mark `regression_delta` as required (R2)"
        )


def check_trace_mode_required(
    schemas: dict[str, dict], errors: list[str]
) -> None:
    trace = schemas.get("trace.jsonl.schema.json")
    if trace is None:
        errors.append("C7 trace.jsonl.schema.json is missing")
        return
    required = trace.get("required", [])
    if "mode" not in required:
        errors.append(
            "C7 trace.jsonl.schema.json must mark `mode` as required (R26)"
        )


def check_run_uses_capability_profile(
    schemas: dict[str, dict], errors: list[str]
) -> None:
    run = schemas.get("run.json")
    if run is None:
        errors.append("C8 run.json schema is missing")
        return
    for prop_name, _sub in _walk_property_names(run):
        if prop_name == "model_id":
            errors.append(
                "C8 run.json must reference models by capability_profile, "
                "never by model_id (P1 mitigation, Theme C)"
            )
    required = run.get("required", [])
    needed = {
        "orchestrator_capability_profile",
        "test_capability_profile",
        "judge_capability_profile",
    }
    missing = needed - set(required)
    if missing:
        errors.append(
            "C8 run.json must require capability-profile fields: "
            f"{sorted(missing)}"
        )


def check_claim_class_open_registry(
    schemas: dict[str, dict], errors: list[str]
) -> None:
    """`claim_class` must be pattern-validated, never enum-restricted."""
    for name in ("claims.json", "findings.json"):
        schema = schemas.get(name)
        if schema is None:
            continue
        for prop_name, sub in _walk_property_names(schema):
            if prop_name != "claim_class":
                continue
            if "enum" in sub:
                errors.append(
                    f"C9 claim_class in {name} must not be enum-restricted "
                    "(Theme D — open AX-CC-* registry)"
                )
            pattern = sub.get("pattern", "")
            if not pattern.startswith(CLAIM_CLASS_PATTERN_PREFIX):
                errors.append(
                    f"C9 claim_class in {name} must be regex-validated "
                    f"against `{CLAIM_CLASS_PATTERN_PREFIX}...`"
                )


def load_rules(errors: list[str]) -> dict[str, Path]:
    by_id: dict[str, list[Path]] = defaultdict(list)
    for path in sorted(RULES_DIR.glob("AX*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            rule = yaml.safe_load(fh)
        if not isinstance(rule, dict):
            errors.append(f"C2 {path.name} is not a YAML mapping")
            continue
        rule_id = rule.get("id")
        if not isinstance(rule_id, str):
            errors.append(f"C2 {path.name} missing string `id` field")
            continue
        if rule_id != path.stem:
            errors.append(
                f"C2 {path.name}: id `{rule_id}` does not match file stem"
            )
        by_id[rule_id].append(path)
    flat: dict[str, Path] = {}
    for rule_id, paths in by_id.items():
        if len(paths) > 1:
            errors.append(
                f"C2 duplicate rule id `{rule_id}` in: "
                + ", ".join(p.name for p in paths)
            )
        flat[rule_id] = paths[0]
    return flat


def check_index_lists_all_rules(
    rules: dict[str, Path], errors: list[str]
) -> None:
    index_path = RULES_DIR / "_index.yaml"
    if not index_path.is_file():
        errors.append("C10 harness/rules/_index.yaml is missing")
        return
    with index_path.open("r", encoding="utf-8") as fh:
        index = yaml.safe_load(fh)
    listed_ids = {entry["id"] for entry in (index.get("rules") or []) if "id" in entry}
    on_disk = set(rules.keys())
    missing = on_disk - listed_ids
    extra = listed_ids - on_disk
    if missing:
        errors.append(
            f"C10 _index.yaml is missing rules present on disk: {sorted(missing)}"
        )
    if extra:
        errors.append(
            f"C10 _index.yaml lists rules with no on-disk sheet: {sorted(extra)}"
        )


def check_no_orphan_rule_refs(
    rules: dict[str, Path], errors: list[str]
) -> None:
    """Every AX#### referenced in fixtures must exist in the registry."""
    if not FIXTURES_DIR.is_dir():
        return
    referenced: set[str] = set()
    for path in FIXTURES_DIR.glob("findings*.json"):
        if "invalid" in path.stem:
            continue
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        for finding in doc.get("findings", []):
            for key in ("id", "rule_id"):
                value = finding.get(key)
                if isinstance(value, str) and value.startswith("AX"):
                    referenced.add(value)
    for path in FIXTURES_DIR.glob("report*.json"):
        if "invalid" in path.stem:
            continue
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        for entry in doc.get("regression_delta", []):
            value = entry.get("rule_id")
            if isinstance(value, str) and value.startswith("AX"):
                referenced.add(value)
        for finding in doc.get("findings", []):
            value = finding.get("id")
            if isinstance(value, str) and value.startswith("AX"):
                referenced.add(value)
    orphans = referenced - set(rules.keys())
    if orphans:
        errors.append(
            f"C3 fixtures reference rules missing from registry: {sorted(orphans)}"
        )


def _registry(schemas: dict[str, dict]) -> Registry:
    resources = [
        (
            f"https://mcp-ax/schemas/{name}",
            Resource(contents=schema, specification=DRAFT7),
        )
        for name, schema in schemas.items()
    ]
    return Registry().with_resources(resources)


def _validate_doc(
    schema_name: str,
    schemas: dict[str, dict],
    doc: object,
    errors: list[str],
    label: str,
) -> bool:
    schema = schemas[schema_name]
    registry = _registry(schemas)
    validator = Draft7Validator(schema, registry=registry)
    found = False
    for err in validator.iter_errors(doc):
        found = True
        errors.append(f"C1 {label} fails {schema_name}: {err.message} at {list(err.path)}")
    return not found


def check_fixtures_validate(
    schemas: dict[str, dict], errors: list[str]
) -> None:
    if not FIXTURES_DIR.is_dir():
        return
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        if "invalid" in path.stem:
            continue
        schema_name = _infer_schema(path.name)
        if schema_name is None:
            continue
        with path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        _validate_doc(schema_name, schemas, doc, errors, path.name)
    trace_schema = "trace.jsonl.schema.json"
    if trace_schema in schemas:
        for path in sorted(FIXTURES_DIR.glob("*.jsonl")):
            if "invalid" in path.stem:
                continue
            with path.open("r", encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    _validate_doc(
                        trace_schema, schemas, rec, errors,
                        f"{path.name}:L{lineno}",
                    )


def _infer_schema(filename: str) -> str | None:
    stem = filename.split(".")[0]
    mapping = {
        "findings": "findings.json",
        "claims": "claims.json",
        "metrics": "metrics.json",
        "run": "run.json",
        "report": "report.json",
    }
    return mapping.get(stem)


def main(argv: list[str] | None = None) -> int:
    errors: list[str] = []
    try:
        schemas = load_schemas()
    except (OSError, json.JSONDecodeError) as exc:
        print(f"internal error loading schemas: {exc}", file=sys.stderr)
        return 2

    check_default_surface_no_freetext(schemas, errors)
    check_claims_aggregation_ban(schemas, errors)
    check_report_regression_delta_required(schemas, errors)
    check_trace_mode_required(schemas, errors)
    check_run_uses_capability_profile(schemas, errors)
    check_claim_class_open_registry(schemas, errors)

    rules = load_rules(errors)
    check_index_lists_all_rules(rules, errors)
    check_no_orphan_rule_refs(rules, errors)
    check_fixtures_validate(schemas, errors)

    if errors:
        print("FAIL — schema lint detected issues:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    print(
        f"OK — {len(schemas)} schemas, {len(rules)} rules, "
        f"all linter checks passed."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
