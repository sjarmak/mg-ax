# R17 Sentinel Rebless Procedure

**Premortem mitigation:** the operational failure narrative described
sentinels over-triggering after a Sonnet 4.6 → 4.7 pin bump, with nobody
on the team holding rebless authority. Release was blocked for eleven
days until the gate was quietly disabled. This document exists so that
never happens again.

## When a rebless is needed

`make sentinel-check` (and the `sentinel-gate` GitHub Action) fail when
either:

1. **Bucket-assignment mismatch** — the set of `AX####` IDs the lint
   engine fires on a sentinel does not equal `expected_findings` in
   `sentinels/expected.json`.
2. **Variance breach** — across the configured `rerun_count` (default 5)
   reruns, the fired-set or the finding count drifts by more than
   `variance_target_per_metric` (default 0.5). Because the static lint
   stage is fully deterministic, any drift here is an outright bug, not
   a tuning issue. For future trace-tier sentinels, drift may be a
   stochastic effect of model/protocol movement and is the trigger for
   this procedure.

A rebless is the act of intentionally updating
`sentinels/expected.json` (or the underlying manifests) to reflect new
ground truth — typically because:

- The lint engine added or refined a rule (`AX####` newly fires on a
  sentinel that previously got past it).
- An upstream model or MCP-protocol change shifted trace-tier metrics
  enough that the prior expected set is no longer valid.
- A sentinel itself was rewritten to better isolate a category.

## Named approver

The single approver for sentinel reblesses is **@sjarmak** (project
owner of record per `git log` and the bead registry). All other
maintainers MAY review, but only the named approver may merge a PR
that touches `sentinels/expected.json` or `sentinels/tools/*.json`.
This is enforced via `.github/CODEOWNERS`.

If `@sjarmak` is unavailable for more than the SLA window below, the
escalation contact is the second code owner listed in
`.github/CODEOWNERS` (currently TBD — see "Bus factor" below).

### SLA

- **Within 24 hours** of a PR opening that requests a rebless, the
  named approver MUST either review the diff or hand off to the
  escalation contact in writing on the PR.
- **Within 48 hours** of the gate breaking on `main`, the named
  approver MUST land a rebless PR or open a tracking issue with a
  go/no-go date for disabling the gate temporarily. Quiet disablement
  without an issue is forbidden.

## Rebless procedure

1. **Reproduce locally**: run `make sentinel-check` on a clean checkout.
   Capture the actual fired-set per sentinel.
2. **Decide intent**: for each drifted sentinel, decide whether the
   drift represents (a) a lint-engine improvement (good — update
   `expected_findings`), (b) a sentinel that no longer reflects its
   category (rewrite the manifest), or (c) an upstream regression
   (file an issue, do not rebless).
3. **Open a PR** that touches only `sentinels/expected.json` and/or
   `sentinels/tools/*.json` and/or `sentinels/scenarios/*.yaml`, and
   includes:
   - A one-line per-sentinel justification in the PR body.
   - A link to the lint-rule change (if the drift was rule-driven) or
     to the upstream model/protocol announcement (if it was version
     drift).
   - A passing local `make sentinel-check` log.
4. **Get named-approver review** — the PR is blocked from merge until
   `@sjarmak` (or the escalated second owner) approves.
5. **After merge**, post a short note in the project status thread so
   later contributors can audit the rebless trail.

## Bus factor

The premortem flagged "no one on the team had authority" as a
release-blocking failure mode. The mitigation is **bus-factor-2**:
at least two people MUST be listed in `.github/CODEOWNERS` for
`sentinels/`. The current state is:

- `@sjarmak` — primary approver and project owner of record.
- `@mcp-ax-bus-factor-2-tbd` — placeholder second owner. **This must
  be replaced with a real human handle before the v3.1 MVP ships.**
  The placeholder is intentional — GitHub surfaces an unresolved-team
  warning on every PR, keeping the gap visible until GATE-3
  (bead **mcp-ax-ph3.5**: "Named owner + go/no-go date for 7 open
  questions") names the second owner. Do **not** delete the placeholder
  before then; deletion silently downgrades the project to bus-factor-1.

## Forbidden actions

- **Quietly disabling the gate.** If the gate must be temporarily
  disabled, an issue MUST be filed with a go/no-go date and the issue
  link MUST be in the disablement commit message. Disablement without
  an issue is a P0 incident.
- **Reblessing under deadline pressure.** Reblesses move a
  load-bearing oracle. They are never urgent enough to skip review.
- **Editing `sentinels/expected.json` from any PR that also changes
  the lint engine.** Rule changes and rebless commits MUST be in
  separate PRs so the diff is auditable.
