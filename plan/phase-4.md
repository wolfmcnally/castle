---
id: 4
title: Coordinated transactional namespace CRUD
depends_on: [phase-3.md]
informs: []
---

# Phase 4 — Coordinated transactional namespace CRUD

The operations-first Phase 2 delivers the existing-content authority and CRUD subset. This pending phase completes new-content create/replace and integrates Phase 3 publication recovery with that same coordinator; retain all earlier operation/regression guarantees without redelivering them as new work.

## Goal

Deliver the complete library capability for ordered current-state document and directory CRUD with validated trees, durable content publication, and safe retry.

## Deliverables and scope

- Extend the delivered `project/castle/namespace.py`, `project/castle/namespace_store.py` and `project/castle/namespace_transactions.py` owners using the Phase 1 contract, Phase 2 existing-content authority and Phase 3 bounded CAS recovery. Keep `project/castle/__init__.py`'s lightweight import posture.
- Implement create file/directory, read/list/resolve, replace file content, whole-object metadata replacement, atomic rename-plus-move, ordinary file/empty-directory deletion, and explicit atomic subtree deletion. Preserve stable IDs, identity reservation, root invariants, and shared blob independence.
- Centralize NFC/casefold comparison, pinned Unicode policy, component/metadata limits, live sibling uniqueness, parent validity, reachability/cycle checks, and path refusal. Reject complete commands atomically rather than silently overwriting or renaming.
- Serialize commands under the coordinator; validate expected namespace revision, canonical request fingerprint, reserved idempotency UUID, durable outcome, seven-day retry retention, and expired-token refusal. Stage/prepare, publish CAS through Phase 3, then commit live references/results/dependencies atomically. Recover pending intents before new writes; committed outcomes win over leftover staging.
- Strengthen delivered `project/tests/test_namespace.py` and add `project/tests/test_namespace_transactions.py` where distinct publication proof needs it; include real concurrent clients/subprocess kills and independent before/after trees. Use opaque CAS provenance labels for namespace-owned writes.

## Acceptance

- Renames/moves preserve IDs and descendants; replacements change content references; deleting one of two shared-content entries leaves the other intact. Root, live-parent, uniqueness, acyclicity, path and metadata refusals change no live tree/revision.
- Two commands with the same expected revision have one ordered winner. Identical retry returns one durable outcome, changed request under the same UUID refuses, and expired tokens never replay commands.
- Interrupt staging, prepared intent, CAS publication, SQL commit, and response delivery; readers see a complete before/after tree, with no dangling live reference or duplicate namespace effect. Consistent unreferenced CAS residue is reported separately from inconsistent blocked storage.
- Logical deletion preserves reserved IDs and explicitly retained history; no CAS excision or confidential purge is claimed. Current authority remains separate from CAS union.

- Atomic file/directory duplication preserves the independently frozen membership and exact fresh-ID map under kills. Concurrent threads/tasks through the same owner have one expected-revision winner and a revision-conflict loser; identical retry races have one durable outcome. Result expiry removes only logically eligible outcomes, never claims forensic purge.

## Validation and handoff

Iteration: `./bin/test project/tests/test_namespace.py project/tests/test_namespace_transactions.py project/tests/test_cas_recovery.py`. Apply the shared two-gate close protocol in [namespace-implementation.md](namespace-implementation.md).

User Demo: N/A — this phase delivers the library; interactive command workflows are qualified in Phase 7.

## Pinned Phase 1 contracts

Use namespace §§14–16 and 20 for typed operations, recursive file/directory duplication with explicit fresh-ID maps, serializable mutation and linearizable reads, bounded integer metadata, request/effect limits, durable terminal/pending/retired outcomes and explicit result expiry. Logical recursive deletion is one SQL transaction and does not move the CAS epoch or imply GC.

## Brief refs

- [Namespace architecture](../briefs/coordinated-namespace-crud.md), §§3–6, 8, and 12; exact contracts §§14–20.
- [CAS contract](../briefs/cas-api.md), immutable content and write path.
- [CRDT semantics](../briefs/crdt-semantics.md), the unchanged CAS authority boundary.

## Delivered publication prerequisite

Use `castle.cas_recovery.PublicationRequest` and `PublicationReceipt` with the exact public signatures `publish_namespace_content(cas_root, operation_root, expected_identity, publication)` and `recover_namespace_publication` with the same arguments. The namespace coordinator already holds the domain admission and CAS stable lock; its integration uses the private `_run_locked` entry while retaining that lock order rather than reacquiring a nonreentrant lock. Persist the immutable recipe in independent SQL ownership before allocating its operation stage/publication slots. Complete publication evidence derives the receipt; no additional receipt file is required. Preserve stage/evidence until the namespace outcome is durable and its owned cleanup is proved.

The low-level publisher uses one conservative storage-recovery refusal. Validate typed requests, immutable input hashes/sizes and effect-none admission before invoking it; evidence-file presence alone does not establish an effect boundary. The completed prerequisite proves process-loss and injected storage errors on synthetic stores, not hardware power-loss or protected deployment. Unreferenced durable CAS content remains retained; no GC follows create/replace recovery.
