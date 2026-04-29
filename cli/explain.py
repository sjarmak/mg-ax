"""mcp-ax explain <AX####> — print the rule sheet from harness/rules/<AX####>.yaml.

R3 acceptance: any registered ID prints the full rule sheet. IDs that aren't
registered exit non-zero with a clear message.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from ._paths import find_repo_root, rules_dir

RULE_ID_RE = re.compile(r"^AX[0-9]{4}$")


class RuleNotFoundError(RuntimeError):
    pass


def load_rule(repo_root: Path, rule_id: str) -> dict:
    if not RULE_ID_RE.match(rule_id):
        raise ValueError(
            f"Rule id must match AX#### (got {rule_id!r})."
        )
    sheet_path = rules_dir(repo_root) / f"{rule_id}.yaml"
    if not sheet_path.is_file():
        raise RuleNotFoundError(
            f"No rule sheet for {rule_id} at {sheet_path.relative_to(repo_root)}. "
            f"Check harness/rules/_index.yaml for registered IDs."
        )
    with sheet_path.open("r", encoding="utf-8") as fh:
        rule = yaml.safe_load(fh)
    if not isinstance(rule, dict):
        raise RuleNotFoundError(
            f"{sheet_path.relative_to(repo_root)} is not a YAML mapping."
        )
    return rule


def render_sheet(rule: dict) -> str:
    keys_in_order = [
        "id",
        "title",
        "severity_default",
        "auto_fix",
        "one_line_summary",
        "rule_version_hash",
        "rationale",
        "evidence_template",
        "citation",
    ]
    lines: list[str] = []
    for key in keys_in_order:
        if key not in rule:
            continue
        value = rule[key]
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        elif isinstance(value, str) and "\n" in value:
            lines.append(f"{key}: |")
            for piece in value.rstrip("\n").split("\n"):
                lines.append(f"  {piece}")
        else:
            lines.append(f"{key}: {value}")
    extras = [k for k in rule.keys() if k not in keys_in_order]
    for key in extras:
        lines.append(f"{key}: {rule[key]!r}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-ax explain",
        description="Print the rule sheet for a registered AX#### finding ID.",
    )
    parser.add_argument("rule_id", help="Rule ID (e.g. AX0007)")
    args = parser.parse_args(argv)

    try:
        repo_root = find_repo_root()
        rule = load_rule(repo_root, args.rule_id)
    except (RuleNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    sys.stdout.write(render_sheet(rule))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
