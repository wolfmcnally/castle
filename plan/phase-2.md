---
id: 2
title: Reliable existing-content namespace operations
depends_on: [phase-1.md]
informs: []
---

# Phase 2 — Reliable existing-content namespace operations

## Goal

Deliver usable, concurrent ACID file/directory namespace operations over already durably admitted CAS content, without waiting for publication or confidential-excision mechanisms those operations do not exercise.

## Authorization and scope reconciliation

The operator approved the operations-first sequence on 2026-10-22 UTC (2026-10-21 local). The approved slice adopts only the existing contract defaults it needs: typed UUID/name/metadata requests, pinned Unicode, coarse revisions, explicit duplicate maps, bounded trees/requests, seven-day result retention, one in-process coordinator and unprotected synthetic qualification. Historical Phase 1 evidence remains unchanged. Original pending phase definitions remain in Git history; later requirements are deferred, never dropped. No new-content CAS effects, snapshots, epoch excision, fresh-database replacement, encrypted deployment, cross-domain atomicity, real-store operations or publication are authorized.

## Deliverables and scope

- Add typed namespace initialization/open, request/result/error and session interfaces in `project/castle/namespace.py`, backed by `project/castle/namespace_store.py` and `project/castle/namespace_transactions.py` as implementation coherence requires. Maintain the lightweight package import posture.
- Implement explicit durable SQL authority with namespace/root/member/epoch/generation/policy identity, foreign keys, DELETE rollback journaling and EXTRA durability; lifetime domain and namespace ownership, serialized admission and single-revision reads. Missing/unsafe/unsupported authority, changed binding, replacement markers and unowned residue refuse; no implicit reconstruction or lock theft.
- Implement directory creation, file binding to independently verified existing durable CAS content without ingest or extra sightings, browsing/read/list/resolve/status, file/directory rename-plus-move, frozen-subtree duplication with fresh IDs, nonrecursive deletion and explicit atomic subtree deletion. Preserve IDs, empty directories, shared content and dependency/identity reservations.
- Persist canonical prepared requests, fingerprints and durable success/rejection results; commit complete tree/dependency/result/revision changes atomically. Recover prepared intents and uncertain SQL outcomes before admitting requests; same-token retries replay one retained result, changed payload conflicts and retired tokens never execute. Unknown storage outcomes block rather than fabricate a rollback or success.
- Provide a thin executable synthetic workflow using public APIs only, with explicit roots and existing CAS enrollment before coordinator ownership. Do not seed SQL or mutate CAS under an active coordinator through an independent caller.
- Qualify concurrency, equal-revision and identical-token races, owner death, rollback/reopen/retry, whole-subtree atomicity, copying/deletion independence, response loss, fsync/uncertain-commit failures and CAS integrity/lifecycle refusals. Admit proofs under existing estate governance without resetting baselines or weakening old coverage.

## Acceptance

- Public calls demonstrate actual create/browse/rename/move/duplicate/delete behavior over independently expected trees and already stored file bytes. CAS object/journal/index/remotes bytes remain unchanged during namespace operations.
- All accepted concurrent operations serialize. Reads observe whole before/after trees; equal revisions have one winner, identical tokens have one outcome, and retries after a lost response preserve the original effect and revision.
- Stable IDs, rooted acyclicity, sibling uniqueness, parent kinds, metadata/name/size limits and complete duplicate identity maps are enforced before any partial effect. Logical deletion never removes CAS bytes or claims confidential cleanup.
- Real subprocess termination before/after prepared intent, within SQL mutation/commit and before response yields an unchanged or complete recovered tree. Live ownership is never stolen; a dead owner's lock becomes available. Uncertain I/O blocks service; qualified SQLite recovery resolves only proven outcomes.
- A matching object hash alone never authorizes a reference: verify sealed content plus journal/index agreement and active exact member/epoch. Torn journal, unjournaled objects, unknown residue, changed epoch and unsupported publication/excision/protection paths fail closed.
- Process-kill evidence is reported separately from filesystem/VFS/hardware power-loss guarantees. No encryption, forensic erasure, arbitrary corruption repair or distributed transaction claim follows.

## Validation and handoff

Iteration uses `./bin/test project/tests/test_namespace.py` and affected retained CAS/journal suites; run format, lint, policy and proof-estate validation before critique. Shared close requires the complete implementation-candidate gate ending in `./bin/check all`, then the separate bare full handoff gate after bookkeeping. Local commits only; no push.

User Demo: Disposable namespace workflow through `./bin/python project/examples/namespace_demo.py` after implementation. Browse an empty directory and existing-content file; rename and move their directory, duplicate its subtree and logically delete one copy. Check stable original IDs, new duplicate IDs, preserved empty directories and unchanged shared content. Retry a supplied operation token and inspect its status. Interactive usability remains operator acceptance, not an automated checkoff.

## Brief refs

- [Namespace architecture](../briefs/coordinated-namespace-crud.md), §§2–6, 14–16, 19–21, especially the operations-first delivery boundary.
- [CAS contract](../briefs/cas-api.md), strict inventory, read verification and lifecycle.
- [CRDT semantics](../briefs/crdt-semantics.md), unchanged CAS and namespace authority boundary.
- [SQLite synchronous pin](../docs/sqlite-synchronous.txt), EXTRA directory durability.
