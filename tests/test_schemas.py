"""Tests for schemas + rule registry + lint_schemas tool.

Each test maps to one bead acceptance criterion. Negative tests construct an
invalid schema/fixture in a temp directory and assert the linter rejects it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, doc: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)


def _copy_repo_skeleton(dest: Path) -> None:
    for sub in ("harness/schemas", "harness/rules", "tools", "cli"):
        shutil.copytree(REPO_ROOT / sub, dest / sub)


def _run_linter(repo_root: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(repo_root / "tools" / "lint_schemas.py")],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
    )


def _run_explain(repo_root: Path, rule_id: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "cli", "explain", rule_id],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
    )


class TestExplainCli(unittest.TestCase):
    """Acceptance: `mcp-ax explain AX0001` prints the rule sheet."""

    def test_explain_prints_known_rule_sheet(self) -> None:
        result = _run_explain(REPO_ROOT, "AX0001")
        self.assertEqual(result.returncode, 0, result.stderr)
        for required_key in (
            "id: AX0001",
            "title:",
            "rationale:",
            "rule_version_hash:",
            "one_line_summary:",
        ):
            self.assertIn(required_key, result.stdout)

    def test_explain_rejects_unregistered_id(self) -> None:
        result = _run_explain(REPO_ROOT, "AX9999")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AX9999", result.stderr)

    def test_explain_rejects_malformed_id(self) -> None:
        result = _run_explain(REPO_ROOT, "not-a-rule")
        self.assertNotEqual(result.returncode, 0)


class TestRepoLintsClean(unittest.TestCase):
    """Baseline: the in-tree state must lint clean before any negative test."""

    def test_repo_lint_clean(self) -> None:
        result = _run_linter(REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)


class _IsolatedRepoTestCase(unittest.TestCase):
    """Spawn a temp copy of the repo so schema/rule mutations don't pollute it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-ax-test-"))
        _copy_repo_skeleton(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestDuplicateRuleIds(_IsolatedRepoTestCase):
    """Acceptance: CI fails when two rule files share an ID."""

    def test_duplicate_id_rejected(self) -> None:
        clone_path = self.tmp / "harness" / "rules" / "AX0099.yaml"
        original = (self.tmp / "harness" / "rules" / "AX0001.yaml").read_text()
        clone_path.write_text(original.replace("id: AX0001", "id: AX0001"))
        result = _run_linter(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C2", result.stderr)


class TestOrphanRuleReferenceDetection(_IsolatedRepoTestCase):
    """Acceptance: CI fails when findings.json references a rule missing from the registry."""

    def test_orphan_rule_in_findings_fixture_rejected(self) -> None:
        fixture = {
            "schema_version": "1",
            "findings": [
                {
                    "id": "AX9001",
                    "severity": "high",
                    "evidence_refs": ["traces/x.jsonl"],
                    "current_text": "x",
                    "proposed_text": "y",
                    "auto_fixable": False,
                    "source": "static",
                    "claim_class": "AX-CC-fix-correct",
                    "scenario_ids": ["sc01"],
                    "oracle_backed": True,
                }
            ],
        }
        _write_json(self.tmp / "harness" / "fixtures" / "findings.orphan.json", fixture)
        result = _run_linter(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C3", result.stderr)
        self.assertIn("AX9001", result.stderr)


class TestFreeTextInDefaultSurfaceRejected(_IsolatedRepoTestCase):
    """Acceptance: CI fails when any default-surface schema introduces a free-text field."""

    def test_notes_property_in_findings_schema_rejected(self) -> None:
        schema_path = self.tmp / "harness" / "schemas" / "findings.json"
        schema = _read_json(schema_path)
        schema["definitions"]["finding"]["properties"]["notes"] = {"type": "string"}
        _write_json(schema_path, schema)
        result = _run_linter(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C4", result.stderr)
        self.assertIn("notes", result.stderr)

    def test_description_property_in_metrics_schema_rejected(self) -> None:
        schema_path = self.tmp / "harness" / "schemas" / "metrics.json"
        schema = _read_json(schema_path)
        scenario_props = schema["definitions"]["scenario_metrics"]["properties"]
        scenario_props["description"] = {"type": "string"}
        _write_json(schema_path, schema)
        result = _run_linter(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C4", result.stderr)
        self.assertIn("description", result.stderr)


class TestClaimsAggregationBan(_IsolatedRepoTestCase):
    """Acceptance: claims.json must not include score/weight/threshold/coverage."""

    def _add_property_and_lint(self, prop_name: str) -> subprocess.CompletedProcess:
        schema_path = self.tmp / "harness" / "schemas" / "claims.json"
        schema = _read_json(schema_path)
        schema["definitions"]["claim"]["properties"][prop_name] = {"type": "number"}
        _write_json(schema_path, schema)
        return _run_linter(self.tmp)

    def test_score_property_rejected(self) -> None:
        result = self._add_property_and_lint("score")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C5", result.stderr)

    def test_weight_property_rejected(self) -> None:
        result = self._add_property_and_lint("weight")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C5", result.stderr)

    def test_threshold_property_rejected(self) -> None:
        result = self._add_property_and_lint("threshold")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C5", result.stderr)

    def test_coverage_property_rejected(self) -> None:
        result = self._add_property_and_lint("coverage")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C5", result.stderr)


class TestReportRequiresRegressionDelta(_IsolatedRepoTestCase):
    """Acceptance: report.json must mark regression_delta required (R2)."""

    def test_remove_regression_delta_required_rejected(self) -> None:
        schema_path = self.tmp / "harness" / "schemas" / "report.json"
        schema = _read_json(schema_path)
        schema["required"] = [r for r in schema["required"] if r != "regression_delta"]
        _write_json(schema_path, schema)
        result = _run_linter(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C6", result.stderr)


class TestTraceRequiresMode(_IsolatedRepoTestCase):
    """Acceptance: trace.jsonl.schema.json must mark mode required (R26)."""

    def test_remove_mode_required_rejected(self) -> None:
        schema_path = self.tmp / "harness" / "schemas" / "trace.jsonl.schema.json"
        schema = _read_json(schema_path)
        schema["required"] = [r for r in schema["required"] if r != "mode"]
        _write_json(schema_path, schema)
        result = _run_linter(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C7", result.stderr)


class TestRunUsesCapabilityProfile(_IsolatedRepoTestCase):
    """Acceptance: run.json must reference models by capability_profile, never model_id (P1)."""

    def test_model_id_property_rejected(self) -> None:
        schema_path = self.tmp / "harness" / "schemas" / "run.json"
        schema = _read_json(schema_path)
        schema["properties"]["model_id"] = {"type": "string"}
        _write_json(schema_path, schema)
        result = _run_linter(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C8", result.stderr)


class TestClaimClassOpenRegistry(_IsolatedRepoTestCase):
    """Acceptance: claim_class must be open AX-CC-* registry, not enum (Theme D)."""

    def test_enum_restriction_rejected(self) -> None:
        schema_path = self.tmp / "harness" / "schemas" / "claims.json"
        schema = _read_json(schema_path)
        schema["definitions"]["claim"]["properties"]["claim_class"] = {
            "type": "string",
            "enum": ["fix-correct", "regression-introduced"],
        }
        _write_json(schema_path, schema)
        result = _run_linter(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C9", result.stderr)


class TestFixtureValidationFires(_IsolatedRepoTestCase):
    """Acceptance: jsonschema validation fires on bad fixtures."""

    def test_report_missing_regression_delta_rejected(self) -> None:
        bad = _read_json(self.tmp / "harness" / "fixtures" / "report.valid.json") if False else None
        bad = {
            "schema_version": "1",
            "run_id": "run-x",
            "findings": [],
            "patches": [],
        }
        _write_json(self.tmp / "harness" / "fixtures" / "report.bad.json", bad)
        result = _run_linter(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C1", result.stderr)
        self.assertIn("regression_delta", result.stderr)

    def test_trace_record_missing_mode_rejected(self) -> None:
        line = {
            "ts": 0,
            "scenario_id": "sc01",
            "kind": "tool_call",
            "payload": {},
        }
        bad_path = self.tmp / "harness" / "fixtures" / "trace.bad.jsonl"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text(json.dumps(line) + "\n")
        result = _run_linter(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C1", result.stderr)
        self.assertIn("mode", result.stderr)


class TestIndexLists(_IsolatedRepoTestCase):
    """Acceptance: _index.yaml must list every AX####.yaml on disk."""

    def test_missing_index_entry_rejected(self) -> None:
        new_rule = """\
id: AX0500
title: A test-only rule
rationale: |
  This rule exists only to test the index check.
citation: []
auto_fix: false
severity_default: low
evidence_template: ""
one_line_summary: Test-only.
rule_version_hash: deadbeef
"""
        (self.tmp / "harness" / "rules" / "AX0500.yaml").write_text(new_rule)
        result = _run_linter(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("C10", result.stderr)
        self.assertIn("AX0500", result.stderr)


if __name__ == "__main__":
    unittest.main()
