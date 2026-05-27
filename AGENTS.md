# mcp-ax

MCP tool agentic experience (AX) evaluation framework.

## Purpose

Evaluate MCP tools from the agent's perspective — not just "does it work?" but "is it usable by an agent autonomously?" Combines structured behavioral testing with meta-reflection to produce actionable improvement recommendations.

The framework has two surfaces:

1. **Eval skill** (`/mcp-eval`) — spawns parallel test agents + a reflection pass to score a tool's agentic usability (1-5).
2. **CLI harness** (`mcp-ax`) — a deterministic, cassette-backed pipeline (lint → trace → claim → report) that produces typed JSON artifacts and checks them against the `AX####` rule registry.

## Project Structure

```
mcp-ax/
├── bin/mcp-ax        # CLI entry wrapper → `python3 -m cli`
├── cli/              # mcp-ax CLI subcommands (explain, lint, fix, trace, claim, baseline, report, try)
├── harness/          # Core eval infrastructure
│   ├── rules/        #   AX#### rule registry (_index.yaml + AX0001.yaml …)
│   ├── schemas/      #   Typed-artifact JSON schemas (run, trace, metrics, claims, findings, report)
│   ├── scenarios/    #   Scenario definitions (sc01/sc02/sc07) + _tiers.yaml
│   ├── runtime/      #   Trace runtime: cassette replay, live agent/MCP session, budget, run writer
│   ├── assertions/   #   Cross-cutting assertions (e.g. no_self_score)
│   └── fixtures/     #   Valid golden artifacts for schema tests
├── lints/            # Static AX lint rule implementations (ax0002, ax0007, _manifest, _finding)
├── claim_extract/    # Per-scenario claim extractors (sc01/sc02/sc07 + _base)
├── extract/          # Metrics extraction from traces
├── proxy/            # Description-override MCP proxy middleware (rewrites tools/list)
├── sentinels/        # R17 sentinel pack: scenarios/ + tool manifests/ + expected.json
├── prompts/          # Agent prompt templates (test-agent.md)
├── templates/        # Output templates (scorecard, pr-comment, description-override example)
├── tools/            # Lint/check CLIs (sentinel_check, lint_schemas, lint_prompts, cassette_freshness)
├── fixtures/         # Recorded cassettes for deterministic replay
├── tests/            # unittest suite (test_lint, test_trace, test_claim, test_schemas, …)
├── skills/           # Reusable evaluation skills (slash commands)
│   └── mcp-eval.md   #   Main evaluation skill
├── reports/          # Generated evaluation reports (mcp_eval_{tool}.md)
├── docs/             # PRD (docs/prd/) + sentinel rebless procedure (docs/sentinels/)
└── AGENTS.md         # This file (CLAUDE.md symlinks here)
```

## Running Evaluations

### Skill path (parallel test agents + reflection)

```
/mcp-eval <tool_name> [repo_context]
```

Example:

```
/mcp-eval deepsearch github.com/sourcegraph/sourcegraph
```

### CLI path (`mcp-ax`)

The `bin/mcp-ax` wrapper resolves the repo root and runs `python3 -m cli`. Subcommands:

| Command | Purpose |
|---------|---------|
| `mcp-ax explain AX0007` | Print the rule sheet for a registered `AX####` finding ID. |
| `mcp-ax lint <manifest>` | Run static AX checks against a tool manifest (`--format json\|text`). |
| `mcp-ax fix <manifest>` | Emit a unified diff for an auto-fixable rule (`--apply AX####`, `--all`, `--write`). |
| `mcp-ax trace <target>` | Run the trace stage: cassette replay → metrics → `run.json`. `--record` captures a live cassette; `--no-cassette`, `--tier`, `--modes`, `--max-usd`, `--rng-seed`. |
| `mcp-ax claim --from-trace <run_id>` | Generate `claims.json` from cached traces ($0 default; `--judge` opts into a model pass). |
| `mcp-ax try <manifest> --description-override <md>` | R9 description-override try cycle (lint + smoke trace + diff). |
| `mcp-ax baseline …` | Freeze, inspect, or update the reference baseline (interactive subcommand tree; no `--force`). |
| `mcp-ax report …` | Report stage (renderer). |

The **`AX####` rule registry** lives in `harness/rules/` (`_index.yaml` maps each ID to its YAML sheet) and is the source of truth for both `lint` findings and `explain` output. The live `--record` recorder for `trace` landed in `f448e43`; the typed-artifact schemas + `AX####` registry + `explain` CLI landed in `b67a459`.

## Build & Test

No `pyproject.toml` — this is a plain `python3` source tree run from the repo root. Targets live in the `Makefile`:

```bash
make test            # python3 -m unittest discover -s tests -v
make sentinel-check  # python3 tools/sentinel_check.py  (R17 sentinel pack vs static lint engine)
make schema-lint     # python3 tools/lint_schemas.py    (schema + rule-registry linter)
```

CI gates: `sentinel-check` (sentinel pack) and `schema-lint` (schema/registry) — see `.github/`.

## Available MCP Tools for Evaluation

Sourcegraph MCP tools:

- `deepsearch` — agentic deep research across codebases
- `keyword_search` — exact keyword code search
- `nls_search` — semantic/NLP code search
- `commit_search` — search commit history
- `diff_search` — search code changes/diffs
- `find_references` — find symbol references
- `go_to_definition` — find symbol definitions
- `compare_revisions` — compare two revisions
- `list_files` — list files in a repo
- `list_repos` — find repositories
- `read_file` — read file contents
- `deepsearch_read` — read a previous deep search result
- `get_contributor_repos` — find repos by contributor

## Report Format

Reports are saved to `reports/` as `mcp_eval_{tool_name}.md` and include:

1. Executive summary with overall usability score (1-5)
2. Test results matrix (8 scenarios)
3. Dimension scores (comprehension, confidence, friction, composition, trust calibration)
4. Detailed findings with evidence
5. Prioritized recommendations
6. Strengths to preserve
7. Raw YAML data from all agents

## Conventions

- Reports use consistent 1-5 scoring for cross-tool comparison
- Raw agent YAML outputs are always preserved in reports
- Test agents are independent (no shared context)
- Reflection is always a separate pass from testing

## Standing Rules

- **Determinism first.** The CLI pipeline runs against recorded cassettes (`fixtures/cassettes/`) by default. Only pass `--record`/`--no-cassette` deliberately; live runs cost money and are bounded by `--max-usd`.
- **Rules are data.** Add or change an `AX####` finding by editing `harness/rules/*.yaml` and registering it in `harness/rules/_index.yaml` — not by hardcoding logic. Lint implementations in `lints/` consume the registry.
- **Schemas are the contract.** Every pipeline artifact (`run`, `trace`, `metrics`, `claims`, `findings`, `report`) validates against `harness/schemas/`. Update the schema and the golden fixture in `harness/fixtures/` together.
- **Sentinels guard the linter.** Changes to the static lint engine must keep `make sentinel-check` green; the expected verdicts live in `sentinels/expected.json`. Re-blessing requires `docs/sentinels/rebless-procedure.md`.
- **No self-scoring.** Agents never score their own output; the `no_self_score` assertion enforces the test/reflection separation.
- Run `make test && make sentinel-check && make schema-lint` before declaring CLI work done.

## Hands-Off Zones

Do not edit these without an explicit ticket — they are generated, golden, or workflow-owned:

- `fixtures/cassettes/`, `tests/fixtures/`, `harness/fixtures/` — recorded/golden inputs; regenerate via the recorder, don't hand-edit.
- `sentinels/expected.json` — sentinel verdict baseline; change only via the rebless procedure.
- `reports/` — generated eval output, not source.
- `.beads/` — beads issue tracker + formulas + hooks (Gas City workflow state); managed by `bd`/`gc`, never edited by hand.
- `.gc/`, `.codex/`, `.codegraph/` — agent/workflow tooling state.

## Memory Anchors

- **Persistent project memory:** `/home/ds/.claude-homes/account1/.claude/projects/-home-ds-projects-mcp-ax/memory/` (one fact per file + `MEMORY.md` index). Currently empty — write durable, non-obvious project facts here.
- **Beads memory:** `bd remember "<insight>"` for cross-session knowledge; recall with `bd memories <keyword>`.
- **Design source of truth:** `docs/prd/mcp_evaluation_harness.md` (harness PRD).
- **Sentinel re-bless playbook:** `docs/sentinels/rebless-procedure.md`.
