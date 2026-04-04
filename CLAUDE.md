# mcp-ax

MCP tool agentic experience (AX) evaluation framework.

## Purpose

Evaluate MCP tools from the agent's perspective — not just "does it work?" but "is it usable by an agent autonomously?" Combines structured behavioral testing with meta-reflection to produce actionable improvement recommendations.

## Project Structure

```
mcp-ax/
├── skills/          # Reusable evaluation skills (slash commands)
│   └── mcp-eval.md  # Main evaluation skill
├── reports/         # Generated evaluation reports (mcp_eval_{tool}.md)
├── lib/             # Shared evaluation infrastructure
└── CLAUDE.md        # This file
```

## Running Evaluations

```
/mcp-eval <tool_name> [repo_context]
```

Example:

```
/mcp-eval deepsearch github.com/sourcegraph/sourcegraph
```

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
