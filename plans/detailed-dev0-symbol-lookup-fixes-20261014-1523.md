---
automation:
  suite_command: "SEMANTIC_SEARCH_ENABLED=false MCP_SKIP_PLUGIN_PREINDEX=true PYTHON_DOTENV_DISABLED=1 uv run --locked --extra dev pytest -q --no-cov tests/test_python_plugin.py tests/test_dispatcher.py tests/test_dispatcher_extension_gating.py tests/smoke/test_mcpbase_stdio_smoke.py tests/test_tool_handlers_readiness.py tests/test_handler_path_sandbox.py tests/test_tool_schema_handler_parity.py"
---

# Detailed plan: correct dev0 pilot symbol lookup and gateway JSON handling

## Task

Investigate every reported automated-test failure and recommend the smallest corrective changes. This is a planning-only artifact; no implementation, publication, client reconfiguration, or additional rollout is performed. Keep the lexical-only dev0 pilot scoped to its existing repository until corrective acceptance succeeds.

## Research summary

The validated suite has 207 checks: 192 passed and 15 failed. All 15 failures reproduce through native STDIO and the active PMCP gateway. An independent AST inventory found 1,078 public top-level Python declarations: all 752 undecorated declarations were indexed, while all 326 decorated declarations were absent. This inventory does not cover every nested or conditional declaration.

The installed v1.4.1 Python plugin, dispatcher, index engine and Git manager are byte-identical to their release and current-main counterparts. Current main is `6b29c4a880141bcd4cfb187ec6e30d99859ba11d`; the live indexed checkout remains `dc5aca176a049b558d449346d88c4e92be475afd`. Current main only changes documentation and release-metadata tests after v1.4.1, so no runtime fix is already available there.

`Plugin.indexFile` rejects `decorated_definition` at both module and immediate class-body scopes. Its independently generated text chunks explain why text retrieval succeeds while definitions disappear. `EnhancedDispatcher.lookup` uses `s.name = ? OR s.name LIKE '%query%'`, sorting only exact names before partial names and taking the first row. That permits unrelated names and makes duplicate exact matches depend on database ordering. Markdown intentionally extracts code-block symbols with the same names and kinds as source definitions; language/provenance must distinguish them during selection. These are extraction and lookup defects, not missing dependencies, insufficient memory, or unavailable providers.

PMCP has a separate confirmed response-corruption bug. A redacted `Person` search returns `ok=true`, `truncated=false`, raw size 15,559 bytes, but `result` becomes a string that fails JSON parsing at position 7634. PMCP's base URL regex consumes the backslash before an escaped closing quote; `redact_auth_url` percent-encodes that backslash as part of the query parameter. `PolicyManager.process_output` then fails to parse its modified serialization and silently retains the malformed text. The same defect remains observable with the current installed PMCP 3.0.0; the local PMCP checkout's metadata is 2.7.3, so implementation must start from that repository's current release/main, not assume the local version is deployed.

## Every failed query and disposition

| Failure family | Queries | Root cause and correction |
| --- | --- | --- |
| Nine real definitions return `not_found` | `ArtifactRef`, `BenchmarkResult`, `CacheMetrics`, `GraphEdge`, `RuntimeSnapshot`, `SemanticWriteBlocker`, `get_search_config`, `ingest_history`, `invalidate_semantic_cache` | Decorator wrappers hide declarations from the Python extractor. Unwrap the definition node and persist its actual declaration location. |
| Two requests return a different name | `FrozenEval` → `FrozenEvalError`; `RepositoryReadiness` → `RepositoryReadinessState` | Requested decorated definitions are absent; arbitrary substring fallback accepts longer names. Fix extraction and reject unrelated partial matches independently. |
| Four requests return Markdown examples | `EnhancedDispatcher`, `IndexDiscovery`, `PluginFactory`, `SQLiteStore` | Both source and documentation rows exist; the query has no source preference or stable tie-break. Prefer actual source definitions. |
| Separate gateway representation defect | `search_code("Person")` | Valid downstream JSON becomes malformed during PMCP redaction. Fix escaped URL boundaries while retaining redaction. This is not a sixteenth Code-Index-MCP retrieval failure. |

Earlier C-line, qualified JavaScript-name and plain-text validation-error mismatches were test-oracle mistakes. Corrected receipts show they pass; do not change product behavior to satisfy those original expectations. Semantic unavailability and `local_only` rollout status are intentional pilot constraints. The allowlist-blocked sibling-worktree query does not prove the unsupported-worktree classifier.

## Code-Index-MCP changes: one bounded patch

### `mcp_server/plugins/python_plugin/plugin.py` (modify)

- `Plugin.indexFile` — modify the module-child and immediate class-method loops to unwrap a `decorated_definition` through its `definition` field before applying the existing function/class extraction logic. The rejection sites are at lines 157–159 and 203–205 in the researched file.
- Use the inner function/class node's declaration line and end line, preserving existing signatures and kinds. Handle stacked decorators, decorated async functions, decorated classes, and decorated methods in both decorated and undecorated classes. Apply decorators statically; never import or execute the indexed module.
- Each declaration must appear once per actual declaration site. Distinct property getter/setter sites may legitimately share a name. Preserve existing SQLite and shard paths, parent-class metadata, chunk identity and chunking behavior. Do not expand support to arbitrary nested definitions, variables, imports, decorator execution, or enriched signatures.

### `mcp_server/dispatcher/dispatcher_enhanced.py` (modify)

- `EnhancedDispatcher.lookup` — replace arbitrary substring matches with literal exact-name candidates and the already exercised unqualified-method alias case: an unqualified query may match a `kind=method` symbol whose final dot-separated component equals the query. A qualified query must match its full stored name. Do not treat SQL `%` or `_` in a query as wildcards.
- Rank acceptable source definitions before document rows, using file language and existing document provenance rather than symbol kind alone: Markdown examples are also `kind=class`. Within source/document tiers, rank literal exact names before qualified-method aliases, then use stable repository-relative path, declaration line, and kind ordering. Do not prefer the earliest inserted row or hard-code these four failed names.
- Retain exact document symbols when no acceptable source definition exists. Keep text-search behavior and document discovery unchanged.
- Remove the BM25 snippet path that fabricates a definition at line 1. BM25 may supply an extension hint to the existing bounded plugin fallback; it must not itself certify a declaration. Preserve fallback timeout and extension gating. Accept a fallback result only if its name satisfies the same exact/explicit-method-alias rule. Otherwise return the existing ready-index miss.
- Preserve the v1.4.1 location fields, language, and public tool shapes. Do not refactor the storage layer or introduce a new ranking framework.

### `tests/test_python_plugin.py` (modify)

- `TestSymbolExtraction` — add active assertions for dataclasses, decorated routes/Click-style stacked decorators, decorated async functions, decorated classes, properties, class methods and static methods. Assert declaration lines, kinds, parent metadata where applicable, no duplicate declaration sites, and parity between returned shards and SQLite persistence.
- Existing decorator coverage at lines 422–459 is skipped because it asks for decorator text in signatures. Replace/split that expectation so declaration presence is tested actively without requiring richer signatures. The class test at lines 309–353 contains decorated methods but only checks class names; add method assertions. Correct stale comments about methods only where touched.

### `tests/test_dispatcher.py` (modify)

- Add real-SQLite `EnhancedDispatcher.lookup` regressions, rather than relying on mocked `_symbol_route` tests for a different search path.
- Seed documentation and source candidates in opposite insertion orders and prove source wins consistently. Seed multiple real definitions and prove stable selection. Confirm exact `FrozenEval`/`RepositoryReadiness` beat their longer names and those longer names alone produce `not_found` for the shorter request.
- Cover `%` and `_` as literal characters; preserve JavaScript `addUser` → `UserManager.addUser` and qualified lookup, rejecting unrelated suffix/substring names. Confirm a document-only exact definition remains discoverable.

### `tests/test_dispatcher_extension_gating.py` (modify)

- Extend the existing extension-hint and timeout coverage to assert that a BM25 text mention with no valid plugin definition yields a miss, and that an unrelated plugin result is rejected. Retain existing C/Python extension-selection checks.

### `tests/smoke/test_mcpbase_stdio_smoke.py` (modify)

- Extend the existing committed fixture and official-SDK test with decorated class/function declarations, a longer-name collision, and a Markdown code example colliding with source. Check correct names and source lines through both text and structured MCP results. Keep the test offline, sandboxed and lexical-only.

## Documentation impact

- Modify `mcp_server/plugins/python_plugin/AGENTS.md` only for the extraction behavior now supported; remove the obsolete blanket decorator limitation without claiming runtime decorator evaluation or arbitrary nested-definition support.
- Modify `docs/operations/v13-pmcp-pilot.md` with a separate corrective-upgrade subsection: missing definitions require a full lexical rebuild, normal restart/incremental sync is insufficient, and wider rollout remains gated on real-repository acceptance. Preserve historical release acceptance and provider allowances. Do not rewrite v1.4.1's accepted receipts.
- No schema, dependency, chunk-identity, provider, authentication, path-allowlist or new public protocol vocabulary changes are required.

The existing protocol contract in `mcp_server/cli/stdio_runner.py:334` says: “Ready misses return not_found with readiness metadata.” `mcp_server/cli/tool_handlers.py:434–440` constructs `"result": "not_found"` and adds readiness. Keep this contract and the existing `index_unavailable` / `safe_fallback=native_search` behavior; do not introduce new error states for corrected selection.

## PMCP changes: a separate bounded patch

- In `src/pmcp/auth.py::_sanitize_base`, make URL extraction preserve JSON escape/quote boundaries. Do not allow trailing backslashes belonging to an escaped string delimiter to enter `redact_auth_url`. Preserve credentials removal, secret-query-key redaction and escaped URL content. Implement the URL-token boundary correction instead of disabling redaction or increasing the output budget.
- In `tests/test_auth.py` and `tests/test_policy.py`, add nested MCP payloads containing HTML URL attributes, escaped quotes, multiple backslashes and URL query credentials. `process_output` must return an object when the original payload is an object and is not truncated; both outer serialization and JSON text content must still parse, and test secrets must remain absent. A legitimate truncated response may retain the documented text form.
- In `tests/test_redaction_additive.py`, add an unconditional preservation regression for this input. Existing `test_json_documents_stay_documents` at lines 600–623 skips inputs whose base redactor already broke JSON, which explains why the observed case escaped that check.
- `src/pmcp/policy/policy.py::PolicyManager.process_output` is the affected caller, not a reason to suppress redaction or restore unredacted data after parsing fails. Keep its envelope and output-budget semantics. A generalized redactor rewrite is outside this patch.
- Run PMCP's existing auth/redaction suites before installing its candidate. This patch can proceed independently, but end-to-end gateway acceptance must pass before calling the pilot fully corrected.

## Dependencies and order

1. Implement the Code-Index-MCP regressions and corrections in an isolated worktree based on current main. Run the focused suite first; then the repository's required broader offline checks. Recheck language fixtures, readiness fences and contract parity because lookup behavior changes.
2. Implement and verify the independent PMCP URL-boundary patch in that repository from its current upstream baseline. Do not change that checkout during this planning task.
3. Build an isolated corrective candidate wheel and verify the installed package with Python 3.12. Retain the current v1.4.1 wheel, configuration backups, original reports and index generations for rollback. Publication, merge, release numbering and fleet rollout are later owner decisions; never republish v1.4.1.
4. At corrective pilot deployment, stop/restart only the owned client/server processes as required, install the pinned candidate, and force a full lexical rebuild for repo `74f50742925c9613`. Keep inference, summarization, semantic indexing and artifact publication disabled. The current Git-manager fast path at installed lines 334–347 can accept the unchanged commit as up to date; file-hash checks also skip unchanged files. A restart therefore cannot repair extraction omissions.
5. Wait for the rebuilt generation to report ready, then repeat native STDIO and active-gateway acceptance, declaration inventory and restart checks. Preserve source bytes and commit. Gate wider rollout on passing evidence followed by the previously agreed day of normal dev0 use.

No automatic parser-version migration is added in this bounded patch. The full rebuild is a mandatory corrective-upgrade step for every affected repository, documented as such. General automatic extractor-version invalidation is separate work if later required.

## Verification to execute during implementation

These commands are prescribed, not executed during this planning turn. Run from the corrective implementation worktree:

```bash
uv sync --locked --python 3.12 --extra dev
SEMANTIC_SEARCH_ENABLED=false MCP_SKIP_PLUGIN_PREINDEX=true PYTHON_DOTENV_DISABLED=1 uv run --locked --extra dev pytest -q --no-cov tests/test_python_plugin.py tests/test_dispatcher.py tests/test_dispatcher_extension_gating.py tests/smoke/test_mcpbase_stdio_smoke.py tests/test_tool_handlers_readiness.py tests/test_handler_path_sandbox.py tests/test_tool_schema_handler_parity.py
SEMANTIC_SEARCH_ENABLED=false MCP_SKIP_PLUGIN_PREINDEX=true PYTHON_DOTENV_DISABLED=1 uv run --locked --extra dev make test-all
uv build --wheel
```

Run separately from the PMCP implementation worktree after its locked development environment is ready:

```bash
uv run --locked --extra dev pytest -q tests/test_auth.py tests/test_policy.py tests/test_redaction_additive.py
```

After a corrective candidate is installed in the existing dev0 pilot, the concrete rebuild and replay commands are:

```bash
/home/viperjuice/workspace/pilots/code-index-mcp-v1.4.1-dev0-20261014/launch.py repository sync --repo-id 74f50742925c9613 --force-full
/home/viperjuice/workspace/pilots/code-index-mcp-v1.4.1-dev0-20261014/tools/index-it-mcp/bin/python /home/viperjuice/workspace/pilots/code-index-mcp-v1.4.1-dev0-20261014/automated_query_suite.py
```

The runner checks Git cleanliness, creates a fresh timestamped evidence directory, performs controlled tracked-file edits, restores exact bytes and polls readiness. Start clean and preserve its evidence. Recompute the independent AST-versus-read-only-SQLite declaration inventory from the current registry's `index_path`; never derive expected declarations from the index. Repeat the 38 gateway requests, plus `Person`, with `redact_secrets=true`. The original runner does not itself execute the full declaration inventory or gateway crosschecks: those must be captured as separate acceptance artifacts, not inferred from its exit code. Preserve the existing inventory and crosscheck receipt schemas; save the additional full redacted gateway envelope as `gateway-person.json`. Runner-stamped operational evidence must name and hash these files in the corrective execution handoff; no old receipt substitutes for a new candidate run.

After producing those fresh receipts, set `PILOT_CHECK_DIR` to that new run directory and run the acceptance assertions:

```bash
/home/viperjuice/workspace/pilots/code-index-mcp-v1.4.1-dev0-20261014/tools/index-it-mcp/bin/python - "$PILOT_CHECK_DIR" <<'PY'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
load = lambda name: json.loads((root / name).read_text())
summary = load('summary.json')
assert summary['total'] == 207 and summary['failed'] == 0
assert summary['readiness_restored'] and summary['source_checkout_clean']
assert summary['semantic_enabled'] is False
inventory = load('python-declaration-inventory.json')['summary']
assert inventory['public_top_level_declarations'] == 1078
assert inventory['missing'] == 0 and inventory['missing_decorated'] == 0
crosschecks = load('gateway-crosschecks.json')['results']
assert len(crosschecks) == 38 and all(row['passed'] for row in crosschecks)
person = load('gateway-person.json')
assert person['ok'] and person['truncated'] is False
assert isinstance(person['result'], dict)
json.loads(person['result']['content'][0]['text'])
PY
```

## Acceptance criteria

- [ ] The focused pytest command passes active decorated class/function/method extraction and persistence checks, with exact declaration lines and no duplicate declaration sites.
- [ ] Real-SQLite lookup regressions pass both insertion orders, source preference, stable duplicate-source ordering and document-only lookup.
- [ ] Real-SQLite and plugin-fallback tests reject longer-name/substring substitutes and preserve supported JavaScript qualified-method aliases and literal query characters.
- [ ] Official-SDK smoke tests prove corrected definitions in both MCP text and structured content; existing readiness, allowlist and schema-parity tests remain green.
- [ ] Required broader offline checks pass for the candidate; semantic/provider requests remain disabled and no consumed live allowance is reused.
- [ ] Installed corrective candidate plus full rebuild yields 207/207 equivalent original automated checks and resolves all 15 original failures through both transports; retain a fresh results manifest and summary.
- [ ] The independent inventory on the unchanged original corpus contains all 1,078 public top-level declarations, including all 326 previously missing decorated declarations. Any corpus change requires regenerated AST expectations and an explicit amendment in the execution handoff.
- [ ] PMCP regression suites pass and the redacted, untruncated `Person` gateway response remains an object with valid JSON text content and unchanged expected Dart retrieval. Test credentials remain redacted.
- [ ] Source restoration, ready-state recovery, clean Git state, lexical-only configuration, representative restart queries and current pilot resource bounds are confirmed in fresh evidence; only then begin the one-day dev0 observation gate.

## Evidence and limits

Original evidence: `/home/viperjuice/workspace/pilots/code-index-mcp-v1.4.1-dev0-20261014/automated-tests-20261014T071607Z/REPORT.md`, `validated-results.json`, `validated-summary.json`, `python-declaration-inventory.json`, `gateway-crosschecks.json`, and `gateway-readiness-fence.json`.

Fresh representation diagnostic: `/home/viperjuice/workspace/pilots/code-index-mcp-v1.4.1-dev0-20261014/failure-investigation-20261014/gateway-json-diagnostic.json`.

This planning branch starts clean at current main. The primary live checkout also starts clean. Planning files are isolated here so they cannot trigger dirty-checkout refusals in the pilot. There are no pre-existing untracked files to attribute. No tests/builds, implementation edits, deletion, commit, push, merge or publication occur during this planning turn.
