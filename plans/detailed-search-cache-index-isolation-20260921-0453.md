---
title: Search cache correctness and scorer-independent vector identity
type: detailed
status: draft
owner_skill: codex-plan-detailed
created_at: "2026-09-21T04:53:34Z"
baseline_commit: 7f3d32ccdda1ad61d3efceee905c234e6ceddfda
roadmap_ref: specs/phase-plans-v13.md
execution_authorized: false
panel_approved: false
automation:
  suite_command: >-
    env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest
    tests/test_hybrid_search_cache.py tests/test_rerank_consumer_migration.py
    tests/test_semantic_indexer_registry.py tests/test_semantic_profiles.py
    tests/test_semantic_profile_settings.py tests/test_repository_readiness.py
    tests/test_profile_aware_semantic_indexer.py tests/test_inference_contracts.py
    -m 'not requires_network and not benchmark' -q --no-cov -o log_cli=false
---

# Detailed Plan: Search Cache And Index Isolation

## Task

Fix result-cache underfilling and prevent scorer-only changes from relocating
compatible vector resources. Preserve repository, registration, generation,
provider, endpoint, and embedding identity. Do not rebuild, rename, copy, or
delete vectors as a side effect.

This second bounded repair follows
`plans/detailed-reranker-correctness-20260921-0453.md`.
It does not depend on Jev. Authored outside Plan Mode as planning only;
no product changes or verification commands ran.

## Research Summary

At the baseline, one hybrid-search instance queried with limit 1 then limit 3
returned one result both times; a fresh instance returned three. The cache
stores already-truncated results under a query/filter key that omits limit
and effective ranking configuration.

Changing only `reranker_defaults.model` preserves the semantic compatibility
fingerprint but changes `SemanticIndexerRegistry._binding` and
`generation_root`, because the whole profile configuration is hashed.
Merely removing that field also changes the first upgraded process's namespace.
Upgrade continuity therefore needs a metadata-only resolution policy.

Prior evidence:
`/home/viperjuice/.local/state/code-index-mcp/research/jev-20260921/assessment.md`.
No fleet index is assumed to exist, be compatible, or be disposable.

## Contracts

At `mcp_server/interfaces/rerank_contracts.py:18-23`, scores are meaningful
"within a single provider response", not across providers or responses.
Lines 49-51 define candidate ID as the correlation key. No new rerank outcome,
embedding contract version, readiness enum, or public MCP error vocabulary.
Non-ready query paths retain `index_unavailable` with
`safe_fallback: "native_search"`.

Vector compatibility and runtime resource ownership are different identities.
Do not replace full resource identity with the compatibility fingerprint alone.
A new local locator is internal metadata, not an artifact-manifest or
embedding-wire schema change.

## Implementation Decisions

### Correct Result-Cache Scope

Keep the bounded, truncated-result cache and include effective limit in its
key. Build canonical JSON from query, effective query type, typed filters,
limit, effective weights/RRF/candidate limits/enabled sources, reranker
identity/policy, and repository/generation identity. Sort mapping keys,
preserve meaningful list order, and do not use delimiter concatenation.

Snapshot effective settings at call entry. If the backend cannot supply a
trustworthy generation token, bypass caching rather than using object identity
or an invented constant. Never include secrets/full provider settings.
Store and return defensive copies of mutable results and nested metadata.
Keep TTL/capacity limits and explicit invalidation.

Scorer changes invalidate result reuse, not vectors. This plan does not add a
provider judgment cache; such a cache would also need model, rubric, full
candidate contents/order, and generation identity.

### Scorer-Independent Resource Identity

Project the existing validated raw semantic configuration by excluding only
the known per-profile `reranker_defaults` block. Preserve every other field:
default profile, providers/models/revisions, dimensions, normalization,
enrichment endpoint/model/prompt, chunker/schema versions, collections,
resource endpoints, and Qdrant URL/mode.

Retain `StoreRegistry.binding`, registration/generation, commit/branch,
and index profile in their existing ownership roles. Keep borrower draining
and staged-versus-active isolation. Use the projection for resource-cache
binding and canonical namespace derivation, independently of scorer settings.

Add one small shared identity/locator helper used by registry construction
and read-only readiness. It is not a new storage service or global cache.

### Existing-Index Continuity

1. A version-1 local locator under the exact semantic generation directory
   maps a stable resource-key digest to a physical namespace basename. It
   records repo/registration/generation/index-profile identity, branch/commit
   binding, resource-key digest, compatibility fingerprint, and collection
   identity. Never record secrets or raw provider configuration.
2. Resolve only a basename inside that generation directory. Reject traversal,
   symlink escapes, malformed/unknown versions, conflicting targets, or stale
   ownership. Never choose a directory by mtime or partial match.
3. A valid existing locator selects its physical root, subject to normal
   metadata/evidence checks. An invalid locator fails closed; never overwrite
   it silently or create an empty replacement collection.
4. With no locator, consider only the canonical root and the exact legacy root
   computed by the old algorithm from the current configuration. Reuse legacy
   material only with matching indexed-profile attestation, SQLite vector-link
   evidence, collection identity, and ownership. Conflicting populated roots
   require explicit recovery; do not scan arbitrary directories/generations.
5. Readiness performs resolution and evidence checking read-only. It must not
   construct a SemanticIndexer, open Qdrant, write a locator, or run inference.
   Preserve this side-effect-free property of `generation_root`; clarify that
   inspecting local metadata is allowed, creating resources is not.
6. Before semantic writes, the current owning runtime persists the validated
   mapping atomically under exclusive writer ownership, then rechecks current
   registration/generation. Use compare-existing behavior; never overwrite a
   different owner's locator. Keep the existing physical root and collection.
   A crash leaves either no locator or a complete valid locator.
7. New empty generations use the canonical root and the same ownership rules.
   Existing but unprovable legacy material fails closed instead of triggering
   indexing. A caller unable to supply SQLite ownership/evidence cannot migrate.
8. Scorer settings changed before upgrade may make the old root unprovable.
   Report that limitation and require a separate explicit recovery decision.
   No automatic paid reindex, broad discovery, or fabricated readiness.

No vector files/collections move. The locator is the only upgrade write.
This is a narrow compatibility bridge, not a general migration framework.

## Changes

Four production files, including one new helper:

| File | Entity | Action And Reason |
| --- | --- | --- |
| `mcp_server/indexer/hybrid_search.py` | `search`, `_get_cache_key`, cache insertion/result formatting | Modify keys and copy boundaries to stop underfill and cache poisoning. |
| `mcp_server/utils/semantic_indexer_registry.py` | `_binding`, `generation_root`, `_construct`, lease validation | Modify resource projection and physical-root resolution without weakening ownership. |
| `mcp_server/utils/semantic_generation_identity.py` | projection, derivation, locator validation/persistence | Create a shared bounded helper so runtime and readiness use identical rules. |
| `mcp_server/health/repository_readiness.py` | `classify_semantic_registered` | Modify root resolution while retaining read-only fail-closed evidence checks. |

Create `tests/test_hybrid_search_cache.py` with fake backends and no optional
model imports. Extend semantic-registry, readiness, profile, and settings test
files named in the suite. Preserve existing generation replacement, borrower
draining, staging, server namespace, and inference-contract coverage.

## Documentation Impact

Modify `docs/guides/inference-profiles.md` for scorer/vector identity, cache
scope, supported legacy continuity, and recovery limits.
Modify `docs/operations/v13-pmcp-pilot.md` to prohibit scorer-only reindex
spending and name the readiness evidence. No new PMCP provisioning feature or
automated fleet migration is required.

## Dependencies And Order

1. Apply the preceding plan's baseline/ownership and v13 PREP scope-amendment
   gate. Do not disturb pending fourth-review repairs.
2. Add cache regressions, then fix key/copy behavior.
3. Add identity differential tests before changing hashes. Build old-root
   fixtures using the baseline algorithm, not the new implementation.
4. Implement locator/root resolution and registry/readiness together. Prove
   continuity across restart, not only within an already-open registry.
5. Run C1 then C2; reconcile the shared hybrid-search consumer.
6. Supply exact-head evidence to EC-PREP-1/2/3 of `specs/phase-plans-v13.md`.
   Combined fixes require the existing four-seat review/reconciliation;
   previous approvals do not transfer to changed bytes.

## Verification

Future commands only. Use temporary SQLite/metadata, injected providers,
network-denying fixtures, and existing local-Qdrant coverage where available.
Never call live attestation or download a model as a test side effect.

**C1, narrow:**

```sh
env SEMANTIC_SEARCH_ENABLED=false MCP_TEST_MODE=1 uv run --locked --extra dev pytest tests/test_hybrid_search_cache.py tests/test_semantic_indexer_registry.py tests/test_repository_readiness.py -m 'not requires_network and not benchmark' -q --no-cov -o log_cli=false
```

**C2:** Run the exact frontmatter `automation.suite_command`.

Cover limits 1->3/3->1, nested filter types/key order, weights/scorer changes,
caller mutation, missing generation identity, scorer-only profile edits,
vector/enrichment/endpoint/Qdrant edits, registration/generation replacement,
restart after upgrade, ambiguous/tampered locators, symlink/path escape,
interrupted writes, missing attestation, and mismatched SQLite evidence.
Instrument constructor/provider/vector-write operations so zero reindex work
is an assertion, not an inference from short runtime.

## Acceptance Criteria

- [ ] C1 proves cache hits satisfy their own limits and cannot cross materially
  different filters, ranking settings, or generations.
- [ ] C1 proves caller/postprocessing mutation cannot poison later results.
- [ ] C2 proves scorer-only edits preserve resource binding and physical
  collection, while vector-affecting or ownership edits do not.
- [ ] C2 proves supported baseline-format indexes survive metadata-only upgrade
  and restart, with zero vector rebuild/copy/rename operations.
- [ ] C2 proves ambiguous, stale, unowned, or malformed evidence fails closed
  without inference or silent replacement collection creation.
- [ ] C2 proves readiness remains read-only and lease/draining, fingerprints,
  and frozen inference contracts retain their guarantees.

## Release Boundary

No fleet indexing is authorized. These repair plans do not themselves close a
roadmap criterion or make the release publishable. Resume the existing roadmap
only after implementation, verification, scope reconciliation, and required
exact-candidate reviews.
