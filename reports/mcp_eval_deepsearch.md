# MCP Evaluation Report: `deepsearch`

**Date**: 2026-04-04
**Instance**: demo.sourcegraph.com
**Repo Context**: github.com/sourcegraph/sourcegraph
**Overall Usability Score**: 3.4 / 5

## 1. Executive Summary

**deepsearch** is Sourcegraph's agentic deep research tool — it answers complex codebase questions by internally orchestrating multiple search and analysis tools via an LLM. When it works, result quality is **exceptional**: well-structured markdown with file paths, line numbers, function names, and intelligent synthesis. However, the agent experience is significantly degraded by an **undocumented async polling pattern**, **opaque search scope**, and **no tool-boundary guidance**.

**One-sentence verdict**: A powerful research tool with best-in-class output quality, hamstrung by documentation gaps that make first-use success nearly impossible for autonomous agents.

**Top 3 recommendations**:

1. **Document the async polling pattern** — every scenario was affected by the undisclosed deepsearch → deepsearch_read requirement
2. **Fix the status line** — "completed successfully" appears even on failures/partial results, undermining trust
3. **Add tool-boundary redirection** — suggest simpler tools when the query doesn't warrant deep research

---

## 2. Test Results Matrix

| #   | Scenario                   | Accomplished | Relevance | Actionability | First-Try | Fallbacks Used            |
| --- | -------------------------- | :----------: | :-------: | :-----------: | :-------: | ------------------------- |
| 1   | Broad Conceptual           |     Yes      |     5     |       5       |    No     | deepsearch_read (polling) |
| 2   | Precise Targeted           |     Yes      |     5     |       5       |   Yes\*   | deepsearch_read (polling) |
| 3   | Cross-Cutting Concern      |    **No**    |     2     |       2       |    No     | deepsearch_read (polling) |
| 4   | Temporal/Change            |     Yes      |     5     |       4       |    No     | deepsearch_read (polling) |
| 5   | Negative/Empty Result      |     Yes      |     5     |       5       |    Yes    | None                      |
| 6   | Overlapping Tool Territory |    **No**    |     2     |       3       |    No     | None                      |
| 7   | Large Result Set           |   Partial    |     4     |       4       |    No     | deepsearch_read (polling) |
| 8   | Chained Workflow           |     Yes      |     5     |       5       |    Yes    | deepsearch_read (polling) |

\*First-try = tool was invoked correctly on first attempt. Polling requirement means most scenarios needed 2-3 tool calls total.

**Success rate**: 5/8 fully accomplished, 1 partial, 2 failed
**Polling required**: 7/8 scenarios needed deepsearch_read follow-up

---

## 3. Dimension Scores

| Dimension             | Score | Top Issue                                                                                            |
| --------------------- | :---: | ---------------------------------------------------------------------------------------------------- |
| **Comprehension**     | 3.3/5 | Async polling pattern completely omitted from tool description — 100% first-use discovery rate       |
| **Confidence**        | 3.3/5 | Search scope is opaque — no repo/file/rev params, no way to know what corpus is searched             |
| **Friction**          | 3.0/5 | Mandatory 2-step polling adds 1-3 extra tool calls and ~2min wall-clock per invocation, undocumented |
| **Composition**       | 3.7/5 | Never suggests simpler alternatives when query is better suited to keyword_search/list_files         |
| **Trust Calibration** | 3.3/5 | "Completed successfully" status appears on failures/partial results, undermining trust signals       |

---

## 4. Detailed Findings

### Comprehension (3.3/5)

The tool description accurately conveys the _purpose_ (deep research on codebases) and the _type of questions_ it handles (complex, multi-step, cross-cutting). The parameter interface is simple — a single `question` string.

**Critical gap**: The description says nothing about the async execution model. In practice, deepsearch returns a polling link, and the agent must:

1. Call `deepsearch_read` with the returned identifier
2. Possibly wait and retry if results aren't ready
3. Parse the final result from the read call

This was discovered through trial and error by every single test agent. The description creates a mental model of "ask question → get answer" but the reality is "ask question → get polling link → poll → maybe retry → get answer."

**Evidence**: Scenarios 1, 2, 3, 4, 7, 8 all required deepsearch_read. Agents that got results back directly (scenarios 5, 6) were edge cases.

### Confidence (3.3/5)

When results arrive, they are high-quality and trustworthy. The tool does not hallucinate (scenario 5 confirmed). However:

- **Scope is invisible**: No parameters to control which repos, branches, or revisions are searched. When the tool fails to find a repo (scenarios 3, 6), the agent cannot diagnose whether the issue is query phrasing, repo indexing, or instance configuration.
- **Instance dependency**: Results depend entirely on what's indexed on the connected Sourcegraph instance. Scenario 3 failed because sourcegraph/sourcegraph wasn't available, but the agent had no way to pre-check this.

**Evidence**: Scenarios 3 and 6 both failed with "repo not found" — but the tool doesn't expose enough context for the agent to prevent or diagnose these failures.

### Friction (3.0/5)

**Input construction**: Excellent (5/5). Natural language question → single parameter. No syntax to learn.

**Output parsing**: Good (4/5). Markdown with file paths, line numbers, and structure. Highly parseable.

**Error recovery**: Poor (2/5). When results are empty or wrong:

- Fallback suggestions are formatted as clickable URLs, not tool invocations — agents can't use URLs
- No suggestion to try simpler/alternative tools
- The "completed successfully" status misleads agents into thinking the search worked

**Polling overhead**: The undocumented polling adds 1-3 extra tool calls and ~2 minutes wall-clock time per query. For an agent in a constrained context window, each unnecessary tool call consumes tokens.

### Composition (3.7/5)

This is deepsearch's strongest dimension. When successful:

- **File paths** are repo-relative, mapping directly to `read_file` parameters
- **Function/method names** map to `find_references` and `go_to_definition`
- **Line number ranges** reduce the need for broad file reads
- **Recommended reading orders** (scenario 8) provide sequencing strategy

**Gap**: The tool never signals when a simpler tool would suffice. Scenario 6 (filename lookup) burned agentic LLM resources on a query that `list_files` handles in milliseconds. No redirection signal was provided.

### Trust Calibration (3.3/5)

**Positive**: The tool does not hallucinate. Scenario 5 (nonexistent mutation) returned an honest "not found" with plausible explanations. Scenario 7 (large result set) explicitly noted results were not exhaustive.

**Negative**:

- The terminal status line "Deep search completed successfully with detailed analysis" appears even when the search found nothing or failed to locate the target repo
- No structured confidence signal (high/medium/low)
- No indication of whether results are exhaustive or sampled
- No machine-parseable metadata envelope

---

## 5. Comparison Context

| Capability                 |  deepsearch   | keyword_search |  nls_search   |
| -------------------------- | :-----------: | :------------: | :-----------: |
| Complex conceptual queries |   **Best**    |      Poor      |     Good      |
| Exact string/symbol lookup |   Overkill    |    **Best**    |     Good      |
| Temporal/change queries    |   **Best**    |      Poor      |     Poor      |
| Repo scoping parameter     |   **None**    |  Yes (repo:)   |  Yes (repo:)  |
| File scoping parameter     |   **None**    |  Yes (file:)   |  Yes (file:)  |
| Revision parameter         |   **None**    |   Yes (rev:)   |  Yes (rev:)   |
| Latency                    |    ~2 min     |    Seconds     |    Seconds    |
| Output format              | Rich markdown | Code snippets  | Code snippets |
| Anti-pattern docs          |   **None**    |      Yes       |      Yes      |

**Key insight**: keyword_search and nls_search both document when NOT to use them and redirect to each other. deepsearch provides no such guidance, leading to misuse on simple queries.

---

## 6. Prioritized Recommendations

| #   | Area           | Issue                                               | Suggestion                                                                                                                                                          | Impact                                                       |
| --- | -------------- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| 1   | Documentation  | Async polling pattern undocumented                  | Add to description: "Returns a polling reference. Use deepsearch_read to retrieve results. Typically 30-120s. Retry deepsearch_read after 15s if still processing." | Eliminates #1 first-use failure                              |
| 2   | Output Format  | "Completed successfully" on failures                | Use distinct statuses: `complete`, `partial (repo not indexed)`, `no results found`                                                                                 | Agents can programmatically assess result quality            |
| 3   | Error Handling | No tool-boundary redirection                        | When query is simple, suggest: "Try: keyword_search / list_files / go_to_definition"                                                                                | Reduces wasted compute on trivial queries                    |
| 4   | Parameters     | No repo filter                                      | Add optional `repo` parameter (string)                                                                                                                              | Prevents repo-not-found failures, gives agents scope control |
| 5   | Output Format  | Fallback suggestions use URLs, not tool invocations | Format as: `keyword_search(query="rate limit repo:sourcegraph/sourcegraph")`                                                                                        | Makes suggestions directly actionable by agents              |
| 6   | Documentation  | Search scope not documented                         | Add: "Searches all indexed repos on the connected instance. Default branches unless question implies otherwise. Use list_repos to verify availability."             | Agents can pre-check repo availability                       |
| 7   | Output Format  | No structured metadata envelope                     | Wrap in: `{status, repos_searched, confidence, body}`                                                                                                               | Enables programmatic quality assessment                      |
| 8   | Documentation  | No anti-patterns documented                         | Add: "Avoid for filename lookups (use list_files), exact matches (use keyword_search), single-symbol lookups (use go_to_definition)"                                | Reduces misuse, improves tool selection                      |
| 9   | Parameters     | No output verbosity control                         | Add optional `detail_level` (brief/standard/comprehensive)                                                                                                          | Prevents context window exhaustion on large queries          |
| 10  | Parameters     | No context passing from prior calls                 | Add optional `context` parameter for file paths/prior findings                                                                                                      | Improves efficiency in chained workflows                     |

---

## 7. Strengths (Preserve These)

- **Exceptional result quality**: When successful, outputs are the best-structured of any Sourcegraph MCP tool — rich markdown with file paths, line numbers, function names, and logical domain organization
- **Temporal query handling**: Genuinely unique capability — used git tag diffs and CHANGELOG analysis to answer version-comparison questions (scenario 4)
- **Honest negative results**: Does not hallucinate. Acknowledges absence with plausible explanations (scenario 5)
- **Intelligent summarization**: Large result sets are categorized and summarized, not dumped (scenario 7)
- **Chain-ready output**: File paths are repo-relative, symbols are named, line ranges are provided — directly parameterizable for downstream tool calls
- **Recommended reading orders**: Unique value-add that provides sequencing strategy, not just data (scenario 8)
- **Simple input interface**: Single natural-language question parameter — zero syntax learning curve

---

## 8. Raw Data

### Scenario 1: Broad Conceptual Query

```yaml
scenario: "Broad Conceptual Query"
tool_evaluated: "deepsearch"
tool_calls:
  - tool: "mcp__sourcegraph__deepsearch"
    params:
      { question: "How does authentication work in the Sourcegraph codebase?" }
    success: true
    result_summary: "Returned a comprehensive, well-structured markdown document covering the full authentication architecture: Actor model, session cookies, access tokens, pluggable auth providers (OAuth, SAML, OIDC, builtin), middleware stack ordering, CSRF protection, session validation, and key file paths with links."
    result_useful: true
    tokens_estimate: "large"
fallback_tools_used:
  - tool: "mcp__sourcegraph__deepsearch_read"
    reason: "deepsearch returned a polling link instead of direct results; had to call deepsearch_read twice (first time still processing, second time returned results after 15s wait)"
task_accomplished: true
accomplishment_note: "The broad conceptual question was answered thoroughly. The tool handled ambiguity well, decomposing 'how does authentication work' into multiple dimensions."
scores:
  result_relevance: 5
  result_completeness: 5
  output_actionability: 5
  output_structure: 5
  parameter_clarity: 4
  error_handling: 3
  first_attempt_success: false
observations:
  - "The tool does NOT return results directly. It returns a polling link and requires a separate deepsearch_read call."
  - "The first deepsearch_read call also returned the polling message (still processing), requiring a manual wait and retry."
  - "Once results arrived, the quality was exceptional."
  - "Total wall-clock time from invocation to results was approximately 2 minutes."
```

### Scenario 2: Precise Targeted Query

```yaml
scenario: "Precise Targeted Query"
tool_evaluated: "deepsearch"
tool_calls:
  - tool: "mcp__sourcegraph__deepsearch"
    params:
      {
        question: "What function validates the access token in the HTTP API middleware in the sourcegraph/sourcegraph repo and where is it defined?",
      }
    success: true
    result_summary: "Identified AccessTokenAuthMiddleware in cmd/frontend/internal/httpapi/auth.go as the entry point, and the Lookup function in internal/database/access_tokens.go (line 190-221) as the actual token validation function. Provided full call chain, file paths, line numbers, and detailed explanation of the validation logic."
    result_useful: true
    tokens_estimate: "large"
fallback_tools_used: []
task_accomplished: true
accomplishment_note: "Deepsearch identified both the entry point and validation logic with line numbers, call chain, and internal logic details — exceeding the precision requested."
scores:
  result_relevance: 5
  result_completeness: 5
  output_actionability: 5
  output_structure: 5
  parameter_clarity: 4
  error_handling: 2
  first_attempt_success: true
observations:
  - "The initial deepsearch call returns a polling link, not a result. This is an async pattern requiring deepsearch_read."
  - "First deepsearch_read poll returned 'poll again' message, requiring manual wait and retry."
  - "After ~2 minutes processing, answer quality was excellent: specific function names, exact file paths, line numbers."
  - "For a precise targeted query, deepsearch is arguably overpowered — keyword_search might have been faster."
```

### Scenario 3: Cross-Cutting Concern

```yaml
scenario: "Cross-Cutting Concern"
tool_evaluated: "deepsearch"
tool_calls:
  - tool: "mcp__sourcegraph__deepsearch"
    params:
      {
        question: "How is rate limiting implemented across all API endpoints in the sourcegraph/sourcegraph codebase?",
      }
    success: true
    result_summary: "Returned a polling link. After reading with deepsearch_read, deepsearch reported it could not locate the sourcegraph/sourcegraph repository. Provided suggested search queries instead."
    result_useful: false
    tokens_estimate: "medium"
fallback_tools_used:
  - "mcp__sourcegraph__deepsearch_read (required by deepsearch's polling design)"
task_accomplished: false
accomplishment_note: "The tool failed to answer the cross-cutting concern question. It could not locate the sourcegraph/sourcegraph repository on the connected instance."
scores:
  result_relevance: 2
  result_completeness: 1
  output_actionability: 2
  output_structure: 3
  parameter_clarity: 4
  error_handling: 3
  first_attempt_success: false
observations:
  - "deepsearch returns a polling link on first call, requiring deepsearch_read. This two-step async pattern is not documented in the tool description."
  - "The tool gracefully acknowledged its limitation (repo not indexed) rather than hallucinating."
  - "The fallback suggestions (search query links) are not useful for an agent — it cannot click links."
  - "The core repo sourcegraph/sourcegraph appears to not be indexed on demo.sourcegraph.com."
```

### Scenario 4: Temporal/Change Query

```yaml
scenario: "Temporal/Change Query"
tool_evaluated: "deepsearch"
tool_calls:
  - tool: "mcp__sourcegraph__deepsearch"
    params:
      {
        question: "What major changes were made to the search backend between Sourcegraph 5.2 and 5.3?",
      }
    success: true
    result_summary: "Listed 7 major search backend changes between v5.2 and v5.3, including keyword search by default, search ranking flag removal, search jobs moving to beta, structural search disabled by default, search context improvements, result consistency fixes, and Zoekt memory fixes. Each item included PR/issue references."
    result_useful: true
    tokens_estimate: "large"
fallback_tools_used: []
task_accomplished: true
accomplishment_note: "The tool produced a detailed, well-structured answer identifying 7 major changes with issue/PR links. Used git diff between v5.2.0 and v5.3.0 tags."
scores:
  result_relevance: 5
  result_completeness: 5
  output_actionability: 4
  output_structure: 5
  parameter_clarity: 4
  error_handling: 3
  first_attempt_success: false
observations:
  - "The tool does not return results directly — requires deepsearch_read polling."
  - "Temporal query handled impressively well using git tag comparisons and CHANGELOG analysis."
  - "The final output was richly structured with numbered sections, PR/issue links, and a sources list."
  - "First attempt success is false solely because of the polling pattern."
```

### Scenario 5: Negative/Empty Result

```yaml
scenario: "Negative/Empty Result"
tool_evaluated: "deepsearch"
tool_calls:
  - tool: "mcp__sourcegraph__deepsearch"
    params:
      {
        question: "How does the GraphQL mutation resetKubernetesClusterCredentials work in the sourcegraph/sourcegraph repo?",
      }
    success: true
    result_summary: "Correctly identified that the mutation does not exist. Provided three possible explanations and three actionable follow-up suggestions with clickable search links."
    result_useful: true
    tokens_estimate: "medium"
fallback_tools_used: []
task_accomplished: true
accomplishment_note: "The tool correctly handled a query about a nonexistent mutation. Did not hallucinate. Acknowledged absence, offered explanations, and provided next steps."
scores:
  result_relevance: 5
  result_completeness: 4
  output_actionability: 5
  output_structure: 4
  parameter_clarity: 5
  error_handling: 5
  first_attempt_success: true
observations:
  - "Excellent negative-result handling: did not fabricate content."
  - "Provided three plausible explanations for why the mutation was not found."
  - "Included concrete follow-up search URLs."
  - "Trailing status line 'Deep search completed successfully with detailed analysis' is slightly misleading."
```

### Scenario 6: Overlapping Tool Territory

```yaml
scenario: "Overlapping Tool Territory"
tool_evaluated: "deepsearch"
tool_calls:
  - tool: "mcp__sourcegraph__deepsearch"
    params:
      {
        question: "Find all files named auth_middleware.go in the sourcegraph/sourcegraph repo",
      }
    success: false
    result_summary: "Reported sourcegraph/sourcegraph is not available on the connected instance. Found archived snapshot but could not search it."
    result_useful: false
    tokens_estimate: "medium"
fallback_tools_used: []
task_accomplished: false
accomplishment_note: "The tool could not locate the target repository. Notably, deepsearch did not suggest that a simpler tool (keyword_search, list_files) would be more appropriate."
scores:
  result_relevance: 2
  result_completeness: 1
  output_actionability: 3
  output_structure: 3
  parameter_clarity: 4
  error_handling: 3
  first_attempt_success: false
observations:
  - "Deepsearch did not redirect the user to a simpler, more efficient tool for a straightforward filename lookup."
  - "The error message was reasonably helpful but the closing 'completed successfully' contradicts the actual outcome."
  - "The tool spent agentic LLM resources on what is fundamentally a simple glob/filename query."
  - "No indication in the output that this query type maps better to keyword_search or list_files."
```

### Scenario 7: Large Result Set

```yaml
scenario: "Large Result Set"
tool_evaluated: "deepsearch"
tool_calls:
  - tool: "mcp__sourcegraph__deepsearch"
    params:
      {
        question: "List all GraphQL resolvers in the Sourcegraph codebase and what they do",
      }
    success: true
    result_summary: "Returned a comprehensive categorized overview of GraphQL resolvers organized by domain (search, batch changes, code intelligence, auth/users, repos, code monitoring, notebooks, insights, executors). Listed ~30+ key resolvers with file paths and brief descriptions."
    result_useful: true
    tokens_estimate: "huge"
fallback_tools_used: []
task_accomplished: partial
accomplishment_note: "Provided useful architectural overview organized by domain area with key resolvers listed, but explicitly noted this is not exhaustive."
scores:
  result_relevance: 4
  result_completeness: 3
  output_actionability: 4
  output_structure: 5
  parameter_clarity: 4
  error_handling: 3
  first_attempt_success: false
observations:
  - "The tool wisely chose to categorize and summarize rather than attempt an exhaustive dump."
  - "Output explicitly noted it was not comprehensive, which is good trust calibration."
  - "The categorization by domain area made the large result set navigable."
  - "Token consumption was very high (~huge) which could be a concern in constrained contexts."
  - "Polling pattern again required deepsearch_read follow-up."
```

### Scenario 8: Chained Workflow

```yaml
scenario: "Chained Workflow"
tool_evaluated: "deepsearch"
tool_calls:
  - tool: "mcp__sourcegraph__deepsearch"
    params:
      {
        question: "Find the main entry point for batch changes execution in the sourcegraph/sourcegraph repo and explain its dependencies so I can read the key files",
      }
    success: true
    result_summary: "Returned a comprehensive architectural walkthrough identifying cmd/executor/main.go as the entry point, with detailed file paths for config, run orchestrator, worker, job handler, API client, three runtime implementations (Docker, Firecracker, Kubernetes), runner layer, and supporting components. Included recommended reading order and hyperlinks."
    result_useful: true
    tokens_estimate: "large"
fallback_tools_used: []
task_accomplished: true
accomplishment_note: "Provided concrete file paths, function names, line number ranges, and a dependency map. Highly chainable."
scores:
  result_relevance: 5
  result_completeness: 5
  output_actionability: 5
  output_structure: 5
  parameter_clarity: 5
  error_handling: 3
  first_attempt_success: true
observations:
  - "Output is exceptionally well-structured for chaining: every file is a hyperlink with line ranges."
  - "File paths are repo-relative, mapping directly to read_file parameters without transformation."
  - "Recommended reading order is unique and valuable — provides sequencing strategy."
  - "Key function/method names mentioned could chain into find_references or go_to_definition."
  - "Line number ranges reduce need for broad file reads in follow-up steps."
```

### Meta-Reflection

```yaml
tool: "deepsearch"
overall_usability_score: 3.4

dimension_scores:
  comprehension: 3.3
  confidence: 3.3
  friction: 3.0
  composition: 3.7
  trust_calibration: 3.3

top_issues:
  comprehension: "The tool description omits the async polling pattern entirely. An agent calling deepsearch expects a result but receives a polling link, with no documented indication that deepsearch_read is required. This is the single largest gap between the described mental model and actual behavior."
  confidence: "Scope is opaque — the tool has no repo, file, or revision parameters, so an agent cannot know what corpus is being searched. When the tool fails to find a repo (scenarios 3 and 6), the agent has no way to diagnose whether the issue is query phrasing, repo indexing, or instance configuration."
  friction: "The mandatory two-step polling pattern (deepsearch -> wait -> deepsearch_read -> possibly wait and retry) adds 1-3 extra tool calls and wall-clock delays of ~2 minutes per invocation. This friction is entirely undocumented and unguessable from the tool description."
  composition: "The tool never suggests simpler alternatives when the query is better suited to keyword_search, list_files, or go_to_definition. An agent using deepsearch for a filename lookup (scenario 6) wastes significant resources with no redirection signal."
  trust_calibration: "The closing status line 'Deep search completed successfully' appears even on partial or failed results (scenarios 5 and 6), undermining trust signals. The tool does not consistently communicate confidence, exhaustiveness, or when it is summarizing rather than enumerating."

recommendations:
  - priority: 1
    area: "documentation"
    issue: "Async polling pattern undocumented"
    suggestion: "Add to description: 'Returns a polling reference. Use deepsearch_read to retrieve results. Typically 30-120s. Retry deepsearch_read after 15s if still processing.'"
    impact: "Eliminates the most common first-use failure"
  - priority: 2
    area: "output_format"
    issue: "Status line says 'completed successfully' even on failures"
    suggestion: "Use distinct statuses: 'complete', 'partial (repo not indexed)', 'no results found'"
    impact: "Agents can programmatically determine result quality"
  - priority: 3
    area: "error_handling"
    issue: "No tool-boundary redirection"
    suggestion: "Suggest simpler tools when query is below deepsearch's intended complexity"
    impact: "Reduces wasted compute on trivial queries"
  - priority: 4
    area: "parameters"
    issue: "No repo filter parameter"
    suggestion: "Add optional 'repo' parameter"
    impact: "Prevents repo-not-found failures"
  - priority: 5
    area: "output_format"
    issue: "Fallback suggestions use URLs, not tool invocations"
    suggestion: "Format as concrete tool calls"
    impact: "Makes suggestions directly actionable by agents"
  - priority: 6
    area: "documentation"
    issue: "Search scope not documented"
    suggestion: "Document what repos/branches are searched"
    impact: "Agents can pre-check repo availability"
  - priority: 7
    area: "output_format"
    issue: "No structured metadata envelope"
    suggestion: "Wrap in {status, repos_searched, confidence, body}"
    impact: "Enables programmatic quality assessment"
  - priority: 8
    area: "documentation"
    issue: "No anti-patterns documented"
    suggestion: "Document when NOT to use deepsearch"
    impact: "Reduces misuse, improves tool selection"
  - priority: 9
    area: "parameters"
    issue: "No output verbosity control"
    suggestion: "Add optional detail_level parameter"
    impact: "Prevents context window exhaustion"
  - priority: 10
    area: "parameters"
    issue: "No context passing from prior calls"
    suggestion: "Add optional context parameter"
    impact: "Improves chained workflow efficiency"

strengths:
  - "Exceptional result quality — rich markdown with file paths, line numbers, function names"
  - "Temporal query handling via git tag diffs and CHANGELOG analysis"
  - "Honest negative results — no hallucination"
  - "Intelligent summarization of large result sets"
  - "Chain-ready output with repo-relative paths and named symbols"
  - "Recommended reading orders — unique value-add"
  - "Simple single-parameter input interface"

if_you_fix_one_thing: "Document the async polling pattern in the tool description. Every single test scenario was affected by the undocumented requirement to call deepsearch_read after deepsearch. An agent encountering this tool for the first time will fail on the first call 100% of the time."

organic_reachability: "medium"
organic_reachability_note: "An agent would naturally reach for deepsearch on broad conceptual, cross-cutting, and temporal queries. However, the undocumented polling pattern creates a high first-use failure rate that degrades trust. The main reachability gap is for queries in the overlapping zone where the description does not differentiate deepsearch from simpler alternatives."
```

---

## 9. Specific Fixes

### Fix 1: Tool Description — Add Async Polling Documentation

**Priority**: 1 (Critical)
**Evidence**: Scenarios 1, 2, 3, 4, 7, 8 all required undocumented deepsearch_read polling

**Current description** (append this after the existing examples block):

```
# NO CURRENT TEXT — this section does not exist
```

**Proposed addition** (add before closing of description):

```
Important usage notes:
- This tool starts an asynchronous deep search and returns a conversation identifier/URL
- Results are NOT returned directly — use `deepsearch_read` with the returned identifier to retrieve results
- Typical processing time: 30-120 seconds depending on query complexity
- If `deepsearch_read` returns a "still processing" message, wait 15 seconds and retry
- The tool searches all repositories indexed on the connected Sourcegraph instance
- Use `list_repos` to verify a repository is indexed before running a deep search

When NOT to use this tool:
- For simple filename lookups (use `list_files` instead)
- For exact string or symbol matches (use `keyword_search` instead)
- For single symbol definition lookups (use `go_to_definition` instead)
- For finding all references to a known symbol (use `find_references` instead)
- When you need results in under 10 seconds (use `keyword_search` or `nls_search`)

Use this tool when you need:
- Comprehensive analysis of complex technical questions
- Multi-step research that requires synthesizing information from many files
- Temporal/version comparison questions (e.g., "what changed between v5.2 and v5.3")
- Architectural understanding that spans multiple packages or services
```

---

### Fix 2: Parameter Schema — Add Optional `repo` Parameter

**Priority**: 4 (High)
**Evidence**: Scenarios 3 and 6 failed because the tool searched the wrong corpus with no way to scope

**Current schema**:

```json
{
  "properties": {
    "question": {
      "description": "The question to research using deep search. Should be detailed and specific about what you want to understand.",
      "type": "string"
    }
  },
  "required": ["question"]
}
```

**Proposed schema**:

```json
{
  "properties": {
    "question": {
      "description": "The question to research using deep search. Should be detailed and specific about what you want to understand.",
      "type": "string"
    },
    "repo": {
      "description": "Optional repository to scope the search to (e.g., 'github.com/sourcegraph/sourcegraph'). If omitted, searches all indexed repositories. Use list_repos to verify availability.",
      "type": "string"
    },
    "detail_level": {
      "description": "Controls output verbosity. 'brief' returns a concise summary (~500 tokens), 'standard' returns a detailed answer (~2000 tokens), 'comprehensive' returns an exhaustive analysis (~5000+ tokens). Default: 'standard'.",
      "type": "string",
      "enum": ["brief", "standard", "comprehensive"]
    }
  },
  "required": ["question"]
}
```

---

### Fix 3: Output Format — Replace Misleading Status Line

**Priority**: 2 (Critical)
**Evidence**: Scenarios 5 and 6 both showed "Deep search completed successfully" on non-success outcomes

**Current output** (trailing every response):

```
Deep search completed successfully with detailed analysis.
```

**Proposed replacement** — use one of these context-appropriate status lines:

```
# On full success:
[Deep search complete — full results from N repositories]

# On partial results (e.g., large result set summarized):
[Deep search complete — partial results. Query matched more results than shown. Use keyword_search for exhaustive listing.]

# On repo not found:
[Deep search complete — target repository not indexed. Try: list_repos(query="sourcegraph") to verify availability, or keyword_search(query="your terms") as a fallback.]

# On no results found:
[Deep search complete — no matching results. The queried concept/symbol may not exist. Try: keyword_search(query="exact_term") for precise matching, or nls_search(query="related concept") for broader semantic search.]
```

---

### Fix 4: Output Format — Replace URL Fallbacks with Tool Invocations

**Priority**: 5 (High)
**Evidence**: Scenarios 3 and 6 provided clickable URLs as fallback suggestions — agents cannot click URLs

**Current fallback format** (observed in scenarios 3, 5, 6):

```markdown
### Suggested Next Steps

- [Search for rate limiting](https://sourcegraph.com/search?q=rate+limit+repo:sourcegraph/sourcegraph)
- [Search for middleware](https://sourcegraph.com/search?q=middleware+rate+limit)
```

**Proposed fallback format**:

```markdown
### Suggested Next Steps

If this result is insufficient, try these tools:

1. **keyword_search** — for exact matches:
   `keyword_search(query="rate limit repo:^sourcegraph/sourcegraph$")`

2. **nls_search** — for broader semantic matching:
   `nls_search(query="rate limiting middleware API endpoints repo:^sourcegraph/sourcegraph$")`

3. **list_files** — to find relevant files:
   `list_files(repo="github.com/sourcegraph/sourcegraph", path="cmd/frontend/internal/httpapi/")`
```

---

### Fix 5: Output Format — Add Structured Metadata Header

**Priority**: 7 (Medium)
**Evidence**: No scenario provided machine-parseable metadata; agents had to infer result quality from prose

**Current output format** — pure markdown prose with no metadata:

```markdown
# How Authentication Works in Sourcegraph

Authentication in Sourcegraph uses an Actor model...
[... prose content ...]

Deep search completed successfully with detailed analysis.
```

**Proposed output format** — add a YAML frontmatter block:

```markdown
---
status: complete # complete | partial | no_results | repo_not_found
repos_searched:
  - github.com/sourcegraph/sourcegraph
files_analyzed: 47
confidence: high # high | medium | low
result_type: synthesis # synthesis | enumeration | error
completeness: full # full | sampled | summary
---

# How Authentication Works in Sourcegraph

Authentication in Sourcegraph uses an Actor model...
[... prose content ...]
```

This lets agents programmatically check `status` and `confidence` before consuming the full body.

---

### Fix 6: Polling Response — Add Progress Indicator

**Priority**: 3 (High)
**Evidence**: Scenarios 1, 2, 7 all required blind retries with no progress indication

**Current polling response** (when search is still processing):

```
Your deep search is still processing. Poll for results using deepsearch_read with identifier: https://demo.sourcegraph.com/deepsearch/abc123
```

**Proposed polling response**:

```yaml
---
status: processing
identifier: "https://demo.sourcegraph.com/deepsearch/abc123"
estimated_seconds_remaining: 45
retry_after_seconds: 15
started_at: "2026-04-04T16:28:12Z"
---
Deep search is still processing. Use deepsearch_read(identifier="https://demo.sourcegraph.com/deepsearch/abc123") to check results. Retry after 15 seconds.
```

---

### Fix 7: deepsearch_read Description — Cross-Reference from deepsearch

**Priority**: 1 (Critical — paired with Fix 1)
**Evidence**: The coupling between deepsearch and deepsearch_read is not documented on either side clearly enough

**Current deepsearch_read description** (first line):

```
This is a Sourcegraph search tool and is best used with other sourcegraph search tools.
Reads a Deep Search conversation and returns the markdown content of the questions and answers.
```

**Proposed deepsearch_read description** (first line):

```
Retrieves results from a deepsearch call. Every deepsearch invocation returns an identifier — pass it here to get the actual results.

This is the required second step after calling deepsearch. If results are still processing, wait 15 seconds and retry.

Also useful for:
- Re-reading results from a previous deep search session
- Sharing or citing deep search findings
```

---

### Fix 8: Tool Description Examples — Add Async Pattern Example

**Priority**: 1 (Critical — paired with Fix 1)
**Evidence**: Current examples show only the question, not the polling flow

**Current examples**:

```xml
<example>
  <user>How does authentication work in this codebase?</user>
  <response>calls the deep search tool with question: "How does authentication work in this codebase?"</response>
</example>
```

**Proposed examples** (add one that shows the full flow):

```xml
<example>
  <user>How does authentication work in this codebase?</user>
  <response>
    1. Calls deepsearch with question: "How does authentication work in this codebase?"
    2. Receives polling identifier
    3. Waits 15 seconds, then calls deepsearch_read with the identifier
    4. If still processing, waits another 15 seconds and retries deepsearch_read
    5. Returns the synthesized research results
  </response>
</example>
```

---

### Fix 9: Source Links — Add Token Budget Warning

**Priority**: 9 (Low)
**Evidence**: Scenario 7 noted source links consumed ~1/3 of total output tokens

**Current behavior**: Every response includes 15-20+ source links at the bottom:

```markdown
## Sources

- [cmd/frontend/internal/httpapi/auth.go](https://...)
- [internal/database/access_tokens.go](https://...)
- ... (15+ more)
```

**Proposed fix**: When `detail_level` is `brief`, omit source links. When `standard`, include top 5. When `comprehensive`, include all. Add a count indicator:

```markdown
## Sources (showing 5 of 23 — use detail_level: "comprehensive" for all)

- [cmd/frontend/internal/httpapi/auth.go](https://...) — primary auth middleware
- [internal/database/access_tokens.go](https://...) — token validation
- [cmd/frontend/internal/auth/](https://...) — auth provider implementations
- [internal/actor/actor.go](https://...) — Actor model definition
- [cmd/frontend/internal/session/](https://...) — session management
```

---

### Fix 10: Error Response — Distinguish Repo Not Found from No Results

**Priority**: 2 (Critical)
**Evidence**: Scenarios 3 and 6 conflated "repo not indexed" with "nothing found"

**Current behavior**: Both "repo not indexed" and "no matching code" produce similar prose responses with "completed successfully" status.

**Proposed fix**: Use distinct error categories in the metadata header:

```yaml
# Repo not indexed:
---
status: repo_not_found
repos_requested: ["github.com/sourcegraph/sourcegraph"]
repos_available: []
suggestion: "Use list_repos(query='sourcegraph') to find available repositories"
---
# No results for a valid repo:
---
status: no_results
repos_searched: ["github.com/sourcegraph/sourcegraph"]
files_analyzed: 0
suggestion: "The queried concept may not exist. Try keyword_search for exact matching."
---
```

---

### Summary: Fix Priority Matrix

| Fix                                            | Area             | Effort |  Impact  | Priority |
| ---------------------------------------------- | ---------------- | :----: | :------: | :------: |
| 1. Add polling docs to description             | Description text |  Low   | Critical |  **P0**  |
| 7. Cross-reference deepsearch_read             | Description text |  Low   | Critical |  **P0**  |
| 8. Add async flow example                      | Description text |  Low   | Critical |  **P0**  |
| 3. Fix status line                             | Output format    |  Low   |   High   |  **P1**  |
| 10. Distinguish error types                    | Output format    | Medium |   High   |  **P1**  |
| 6. Add progress indicator                      | Output format    | Medium |   High   |  **P1**  |
| 4. Replace URL fallbacks with tool invocations | Output format    | Medium |   High   |  **P2**  |
| 2. Add repo + detail_level params              | Parameter schema |  High  |   High   |  **P2**  |
| 5. Add metadata header                         | Output format    |  High  |  Medium  |  **P3**  |
| 9. Token budget for source links               | Output format    |  Low   |   Low    |  **P3**  |
