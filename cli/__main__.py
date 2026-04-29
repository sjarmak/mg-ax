"""mcp-ax — top-level CLI dispatcher.

Only `explain` is wired in this bead (mcp-ax-ph3.6). Future beads add
`lint`, `trace`, `claim`, `report`, `try`, `baseline`, `fix`, `pack`.
"""

from __future__ import annotations

import argparse
import sys


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-ax",
        description="MCP tool agentic-experience evaluation harness.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    explain = sub.add_parser(
        "explain",
        help="Print the rule sheet for a registered AX#### finding ID.",
    )
    explain.add_argument("rule_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "explain":
        from . import explain as explain_mod
        return explain_mod.main([args.rule_id])

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
