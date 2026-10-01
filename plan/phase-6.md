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

- Add the proposed `project/castle/namespace_excision.py` coordinator and `project/tests/test_namespace_excision.py`. Integrate with existing `project/castle/excision.py` and `project/castle/store_state.py` through their contract-defined lifecycle boundaries; do not reimplement retirement or invent rollback after retirement.
- Stop namespace and direct CAS admission, drain readers/handles, inventory source/shared-owner dependencies, deleted metadata, snapshots, requests/results, staging, journal sightings, remote records, derived indexes, and consumer obligations. Refuse conflicting live ownership or forbidden metadata attached to retained shared content unless explicit caller selection/custody boundaries resolve it.
- Implement all five durable stages: sanitized snapshot recipes before retirement; completed Castle forward cutover; successor-bound manifest ingestion while blocked; atomic retained reference/binding/dependency publication; fresh namespace database and owned-residue cleanup with the Phase 3 marker protocol. Source CAS identities remain stable; catalog/epoch bindings advance.
- Publish distinct reference-publication, cleanup, and completion outcomes. Preserve only permitted identity reservations and selection-free recovery evidence. Report external backup/replica/key/plaintext obligations separately from the enforced local boundary.
- Fence prepared/retired/foreign epochs on ordinary opens and admission; old replicas cannot merge into the successor, but explicit re-ingestion is a separately authorized intake decision. Derived retrieval reconciliation is required before it reports current completion.

## Acceptance

- Independent dependency closure includes manifests and metadata, not only a current file reference. Shared bytes and retained journal/remotes cannot hide a broken owner or an unsupported confidentiality claim.
- Kill before/after every durable stage, including successor manifest ingestion and database replacement. Resume forward without reading removed predecessor snapshots, ingesting into a prepared successor, repeating completed excision, or reactivating retirement.
- A references-published result leaves service blocked until independent selected-sentinel scans pass for retained owned authority/sidecars/staging/recipes. Row deletion and CAS retirement alone cannot pass purge completion.
- Verify coherent retained namespace/snapshots on the active successor, old-epoch refusal, and unchanged ordinary CAS/excision laws. No physical erasure or global offline-copy purge is claimed.

## Validation and handoff

Iteration: `./bin/test project/tests/test_namespace_excision.py project/tests/test_namespace_store.py project/tests/test_namespace_snapshots.py project/tests/test_merge.py`. Apply the shared two-gate close protocol in [namespace-implementation.md](namespace-implementation.md). All destructive exercises use disposable synthetic stores; no real-store excision is authorized by this phase.

User Demo: N/A — destructive cutover is demonstrated by disposable behavioral proofs, not a request for the owner to purge real content interactively.

## Brief refs

- [Namespace architecture](../briefs/coordinated-namespace-crud.md), §§8–9, 11–12.
- [Excision](../briefs/content-excision.md), quiescence, prepared/active successor, retirement, and forward recovery.
- [CRDT semantics](../briefs/crdt-semantics.md), epoch fencing and unchanged union.
- [CAS contract](../briefs/cas-api.md), inventory and custody metadata.
