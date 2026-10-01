---
id: 4
title: Coordinated transactional namespace CRUD
depends_on: [phase-3.md]
informs: []
---

# Phase 4 — Coordinated transactional namespace CRUD

## Goal

Deliver the complete library capability for ordered current-state document and directory CRUD with validated trees, durable content publication, and safe retry.

## Deliverables and scope

- Add the proposed `project/castle/namespace.py` public library API and `project/castle/namespace_transactions.py` command/intent owner using the Phase 1 contract and Phase 3 authority. Keep `project/castle/__init__.py`'s lightweight import posture.
- Implement create file/directory, read/list/resolve, replace file content, whole-object metadata replacement, atomic rename-plus-move, ordinary file/empty-directory deletion, and explicit atomic subtree deletion. Preserve stable IDs, identity reservation, root invariants, and shared blob independence.
- Centralize NFC/casefold comparison, pinned Unicode policy, component/metadata limits, live sibling uniqueness, parent validity, reachability/cycle checks, and path refusal. Reject complete commands atomically rather than silently overwriting or renaming.
- Serialize commands under the coordinator; validate expected namespace revision, canonical request fingerprint, reserved idempotency UUID, durable outcome, seven-day retry retention, and expired-token refusal. Stage/prepare, publish CAS through Phase 2, then commit live references/results/dependencies atomically. Recover pending intents before new writes; committed outcomes win over leftover staging.
- Add `project/tests/test_namespace.py` and `project/tests/test_namespace_transactions.py`; include real concurrent clients/subprocess kills and independent before/after trees. Use opaque CAS provenance labels for namespace-owned writes.

## Acceptance

- Renames/moves preserve IDs and descendants; replacements change content references; deleting one of two shared-content entries leaves the other intact. Root, live-parent, uniqueness, acyclicity, path and metadata refusals change no live tree/revision.
- Two commands with the same expected revision have one ordered winner. Identical retry returns one durable outcome, changed request under the same UUID refuses, and expired tokens never replay commands.
- Interrupt staging, prepared intent, CAS publication, SQL commit, and response delivery; readers see a complete before/after tree, with no dangling live reference or duplicate namespace effect. Consistent unreferenced CAS residue is reported separately from inconsistent blocked storage.
- Logical deletion preserves reserved IDs and explicitly retained history; no CAS excision or confidential purge is claimed. Current authority remains separate from CAS union.

## Validation and handoff

Iteration: `./bin/test project/tests/test_namespace.py project/tests/test_namespace_transactions.py project/tests/test_namespace_store.py project/tests/test_cas_recovery.py`. Apply the shared two-gate close protocol in [namespace-implementation.md](namespace-implementation.md).

User Demo: N/A — this phase delivers the library; interactive command workflows are qualified in Phase 7.

## Brief refs

- [Namespace architecture](../briefs/coordinated-namespace-crud.md), §§3–6, 8, and 12.
- [CAS contract](../briefs/cas-api.md), immutable content and write path.
- [CRDT semantics](../briefs/crdt-semantics.md), the unchanged CAS authority boundary.
