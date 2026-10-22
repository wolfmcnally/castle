---
id: 6
title: Namespace excision coordination and epoch fencing
depends_on: [phase-5.md]
informs: []
---

# Phase 6 — Namespace excision coordination and epoch fencing

## Goal

Coordinate namespace dependency cleanup with Castle's irreversible successor-epoch excision, reopening only after both retained references and protected cleanup are complete.

## Deliverables and scope

- Implement the deferred fresh namespace database generation/replacement protocol from namespace §18: exact selection-free marker, fresh-image validation, directory durability, sidecar disposal, cleanup-scanned/verified stages and interrupted forward recovery. Original Phase 3 requirements are retained here; ordinary Phase 2 SQLite recovery is not this confidential cleanup mechanism.

- Add the proposed `project/castle/namespace_excision.py` coordinator and `project/tests/test_namespace_excision.py`. Integrate with existing `project/castle/excision.py` and `project/castle/store_state.py` through their contract-defined lifecycle boundaries; do not reimplement retirement or invent rollback after retirement.
- Stop namespace and direct CAS admission, drain readers/handles, inventory source/shared-owner dependencies, deleted metadata, snapshots, requests/results, staging, journal sightings, remote records, derived indexes, and consumer obligations. Refuse conflicting live ownership or forbidden metadata attached to retained shared content unless explicit caller selection/custody boundaries resolve it.
- Implement all five durable stages: sanitized snapshot recipes before retirement; completed Castle forward cutover; successor-bound manifest ingestion while blocked; atomic retained reference/binding/dependency publication; fresh namespace database and owned-residue cleanup with the replacement-marker protocol. Source CAS identities remain stable; catalog/epoch bindings advance.
- Publish distinct reference-publication, cleanup, and completion outcomes. Preserve only permitted identity reservations and selection-free recovery evidence. Report external backup/replica/key/plaintext obligations separately from the enforced local boundary.
- Fence prepared/retired/foreign epochs on ordinary opens and admission; old replicas cannot merge into the successor, but explicit re-ingestion is a separately authorized intake decision. Derived retrieval reconciliation is required before it reports current completion.

## Acceptance

- Independent dependency closure includes manifests and metadata, not only a current file reference. Shared bytes and retained journal/remotes cannot hide a broken owner or an unsupported confidentiality claim.
- Kill before/after every durable stage, including successor manifest ingestion and database replacement. Resume forward without reading removed predecessor snapshots, ingesting into a prepared successor, repeating completed excision, or reactivating retirement.
- A references-published result leaves service blocked until independent selected-sentinel scans pass for retained owned authority/sidecars/staging/recipes. Row deletion and CAS retirement alone cannot pass purge completion.
- Verify coherent retained namespace/snapshots on the active successor, old-epoch refusal, and unchanged ordinary CAS/excision laws. No physical erasure or global offline-copy purge is claimed.

- Unlisted disclosing operation/deleted-entry/dependency rows refuse before retirement. Explicit predecessor/successor root arguments bind initial excise and recovery; abort-before-retirement restores the prior marker only with proof that retirement and uncertain writes are absent. Completed status and RecoveryResult expose their distinct safe typed shapes.

## Validation and handoff

Iteration: `./bin/test project/tests/test_namespace_excision.py project/tests/test_namespace.py project/tests/test_namespace_snapshots.py project/tests/test_merge.py`. Apply the shared two-gate close protocol in [namespace-implementation.md](namespace-implementation.md). All destructive exercises use disposable synthetic stores; no real-store excision is authorized by this phase.

User Demo: N/A — destructive cutover is demonstrated by disposable behavioral proofs, not a request for the owner to purge real content interactively.

## Pinned Phase 1 contracts

Use namespace §§14, 16–20 for exhaustive excision selectors, shared-owner refusal, snapshot replacement/drop decisions, safe selection-free completion, UUID-only retired reservations, explicit-root recovery and fresh database cleanup. References-published is internal and blocked; neither completion nor a later fsync proves an earlier failed write was durable.

## Brief refs

- [Namespace architecture](../briefs/coordinated-namespace-crud.md), §§8–9, 11–12; exact contracts §§14–20.
- [Excision](../briefs/content-excision.md), quiescence, prepared/active successor, retirement, and forward recovery.
- [CRDT semantics](../briefs/crdt-semantics.md), epoch fencing and unchanged union.
- [CAS contract](../briefs/cas-api.md), inventory and custody metadata.

## Retained publication evidence inventory pin

Namespace operation ownership now retains publication request/receipt and precondition-error fields in its SQL recipe until explicit result expiry. Its closed outer operation receipt remains prepared through payload cleanup and becomes durable afterward; committed SQL plus the saved validated publication receipt permits stage-absent cleanup recovery. Excision must inventory every retained request, receipt, recipe, deleted entry and dependency disclosure, including publication CasIDs and journal commitments, before retirement. Ordinary staging disposal and logical deletion remain distinct from fresh-database replacement, confidential cleanup and CAS excision. Preserve the existing explicit root/member fencing and never infer purge from absent stage files.

## Delivered folder and snapshot disclosure pin

The closed private SQL ownership recipe now contains an ordered publication request/nullable-receipt array and nullable snapshot record, with one pre-reservation internal bound covering the eventual complete receipts. Snapshot dependencies include the exact manifest and member CasIDs; logical catalog deletion retains its UUID, payload, metadata and member dependencies. Excision closure must also include every imported descriptor, retained request/result/error, publication recipe, deleted entry and journal commitment. Use the actual semantic/restart families in `project/tests/test_namespace.py`; snapshot/onboarding modules are helpers invoked by those families, not independently collected proofs. Rebuild derived snapshot and pending-capture caches from the new generation after database replacement. Generic manifest validation admits an explicitly expected redacted origin; current ordinary catalog admission requires capture until this phase binds replacement manifests to actual excision ownership. Preserve last-started-first multi-publication recovery when publishing sanitized successor manifests.
