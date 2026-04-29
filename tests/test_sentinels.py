"""Tests for the R17 sentinel pack — bead mcp-ax-ph3.7 acceptance.

Covers:
  - At least 6 sentinels exist covering the required categories
  - Each sentinel has a manifest, scenario YAML, and expected.json entry
  - sentinel-check passes against current lint engine output
  - Variance across N=5 reruns is 0 (static lint is deterministic)
  - Bucket-mismatch breaks the gate (negative test)
  - sentinel-gate.yml exists and references make sentinel-check
  - rebless-procedure.md names a single approver
  - CODEOWNERS lists ownership for sentinels/
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SENTINELS_DIR = REPO_ROOT / "sentinels"
EXPECTED_PATH = SENTINELS_DIR / "expected.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "sentinel-gate.yml"
CODEOWNERS_PATH = REPO_ROOT / ".github" / "CODEOWNERS"
REBLESS_PATH = REPO_ROOT / "docs" / "sentinels" / "rebless-procedure.md"
MAKEFILE_PATH = REPO_ROOT / "Makefile"

REQUIRED_CATEGORIES = {
    "good-baseline",
    "ambiguous-name",
    "missing-required",
    "redundant-with-sibling",
    "oversized-description",
    "undocumented-coupling",
}


def _load_expected() -> dict:
    with EXPECTED_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _run_sentinel_check(repo_root: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(repo_root / "tools" / "sentinel_check.py")],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        env=env,
    )


class TestSentinelCoverage(unittest.TestCase):
    """Acceptance: at least 6 sentinels covering the required categories."""

    def test_six_required_categories_present(self) -> None:
        expected = _load_expected()
        sentinels = expected["sentinels"]
        self.assertGreaterEqual(len(sentinels), 6)
        categories = {entry["category"] for entry in sentinels.values()}
        missing = REQUIRED_CATEGORIES - categories
        self.assertEqual(missing, set(), f"missing categories: {missing}")

    def test_each_sentinel_has_manifest_and_scenario(self) -> None:
        expected = _load_expected()
        for name in expected["sentinels"]:
            manifest = SENTINELS_DIR / "tools" / f"{name}.json"
            scenario = SENTINELS_DIR / "scenarios" / f"{name}.yaml"
            self.assertTrue(manifest.is_file(), f"missing {manifest}")
            self.assertTrue(scenario.is_file(), f"missing {scenario}")

    def test_manifests_are_valid_mcp_tools_list_shape(self) -> None:
        expected = _load_expected()
        for name in expected["sentinels"]:
            manifest_path = SENTINELS_DIR / "tools" / f"{name}.json"
            with manifest_path.open("r", encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertIn("tools", doc, f"{manifest_path} missing tools[]")
            self.assertIsInstance(doc["tools"], list)
            self.assertGreaterEqual(len(doc["tools"]), 1)
            for tool in doc["tools"]:
                for required in ("name", "description", "inputSchema"):
                    self.assertIn(required, tool)


class TestSentinelCheckPasses(unittest.TestCase):
    """Acceptance: make sentinel-check passes on the in-tree state."""

    def test_sentinel_check_exit_zero(self) -> None:
        result = _run_sentinel_check(REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("sentinels match expected", result.stdout)


class TestVarianceUnderTarget(unittest.TestCase):
    """Acceptance: variance across N=5 reruns is < 0.5 per metric (R17)."""

    def test_static_lint_zero_variance(self) -> None:
        result = _run_sentinel_check(REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("variance breach", result.stderr)


class TestBucketMismatchBreaksGate(unittest.TestCase):
    """Negative: deliberately bad expected.json must break the gate."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mcp-ax-sentinels-test-"))
        for sub in ("harness", "cli", "lints", "tools", "sentinels"):
            src = REPO_ROOT / sub
            if src.is_dir():
                shutil.copytree(src, self.tmp / sub)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_wrong_expected_breaks_gate(self) -> None:
        expected_path = self.tmp / "sentinels" / "expected.json"
        with expected_path.open("r", encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["sentinels"]["good-baseline"]["expected_findings"] = ["AX9999"]
        with expected_path.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        result = _run_sentinel_check(self.tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("good-baseline", result.stderr)
        self.assertIn("bucket mismatch", result.stderr)


class TestWorkflowWiring(unittest.TestCase):
    """Acceptance: sentinel-gate.yml exists and references make sentinel-check."""

    def test_workflow_exists(self) -> None:
        self.assertTrue(WORKFLOW_PATH.is_file(), str(WORKFLOW_PATH))

    def test_workflow_runs_sentinel_check(self) -> None:
        body = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("make sentinel-check", body)

    def test_workflow_runs_on_main_pr_and_release_tag(self) -> None:
        with WORKFLOW_PATH.open("r", encoding="utf-8") as fh:
            wf = yaml.safe_load(fh)
        # PyYAML parses the bareword `on:` as Python `True`.
        triggers = wf.get("on") or wf.get(True)
        self.assertIsNotNone(triggers, f"no `on:` block in {WORKFLOW_PATH}")
        self.assertIn("push", triggers)
        self.assertIn("pull_request", triggers)
        push = triggers["push"]
        self.assertIn("main", push.get("branches", []))
        self.assertTrue(any("v" in t for t in push.get("tags", [])))


class TestRebless(unittest.TestCase):
    """Acceptance: rebless docs name a single approver and document SLA."""

    def test_rebless_doc_exists(self) -> None:
        self.assertTrue(REBLESS_PATH.is_file())

    def test_rebless_names_single_approver(self) -> None:
        body = REBLESS_PATH.read_text(encoding="utf-8")
        self.assertIn("@sjarmak", body)
        self.assertIn("single approver", body)

    def test_rebless_documents_sla(self) -> None:
        body = REBLESS_PATH.read_text(encoding="utf-8")
        self.assertRegex(body, r"\b24 hours\b")
        self.assertRegex(body, r"\b48 hours\b")

    def test_rebless_forbids_quiet_disablement(self) -> None:
        body = REBLESS_PATH.read_text(encoding="utf-8")
        self.assertIn("Quietly disabling", body)


class TestCodeOwners(unittest.TestCase):
    """Acceptance: bus-factor-2 maintainers listed for sentinels/."""

    def test_codeowners_exists(self) -> None:
        self.assertTrue(CODEOWNERS_PATH.is_file())

    def test_codeowners_assigns_sentinels(self) -> None:
        body = CODEOWNERS_PATH.read_text(encoding="utf-8")
        # Each meaningful line: <pattern> <space> <@owner> [<@owner>...]
        sentinel_line = next(
            (
                ln for ln in body.splitlines()
                if ln.strip().startswith("sentinels/") and "@" in ln
            ),
            None,
        )
        self.assertIsNotNone(sentinel_line)
        self.assertIn("@sjarmak", sentinel_line)

    def test_bus_factor_2_two_owners_on_sentinels(self) -> None:
        body = CODEOWNERS_PATH.read_text(encoding="utf-8")
        for ln in body.splitlines():
            stripped = ln.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if not stripped.startswith("sentinels/"):
                continue
            owners = re.findall(r"@[\w./-]+", stripped)
            self.assertGreaterEqual(
                len(owners), 2,
                f"bus-factor-2 violation on `{stripped}`: only {owners}",
            )


class TestMakefileTargets(unittest.TestCase):
    """Acceptance: make sentinel-check target wired."""

    def test_makefile_has_sentinel_check_target(self) -> None:
        body = MAKEFILE_PATH.read_text(encoding="utf-8")
        self.assertIn("sentinel-check:", body)
        self.assertIn("tools/sentinel_check.py", body)


class TestPendingRulesRegistered(unittest.TestCase):
    """Pending rules listed in expected.json must exist in the rule registry."""

    def test_pending_rules_have_sheets(self) -> None:
        expected = _load_expected()
        rules_dir = REPO_ROOT / "harness" / "rules"
        for name, entry in expected["sentinels"].items():
            for rule_id in entry.get("pending_rules", []):
                sheet = rules_dir / f"{rule_id}.yaml"
                self.assertTrue(
                    sheet.is_file(),
                    f"sentinel {name!r} pending rule {rule_id} has no sheet at {sheet}",
                )


if __name__ == "__main__":
    unittest.main()
