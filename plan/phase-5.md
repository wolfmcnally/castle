---
id: 5
title: Folder admission snapshots and optional onboarding
depends_on: [phase-4.md]
informs: []
---

# Phase 5 — Folder admission, snapshots, and optional onboarding

## Goal

Add atomic frozen folder admission, explicit immutable historic views, and optional existing-store reconstruction without inventing current authority from ambiguous history.

## Deliverables and scope

- Add the proposed `project/castle/namespace_snapshots.py` and `project/castle/namespace_onboarding.py` owners; extend the public namespace API with the contract-defined frozen import and snapshot commands. Import explicit empty directories and complete accepted path-to-content membership atomically; reject source changes and destination/name collisions as a unit.
- Serialize canonical snapshot manifests with version/policy/Unicode identity, complete tree, protected CAS binding, and captured revision. Capture revision R under the mutation lock; publish the immutable payload and snapshot association/result at R+1 using durable intents. Track dependencies separately from ordinary searchable source documents.
- Provide snapshot inspection and authorized export selection, reference deletion, and retention discovery. Historic views exist only for explicitly retained snapshots. Snapshot-to-current-state restoration and automatic per-edit archives remain excluded.
- Onboard into a new disjoint authority only after an explicit accepted seed. Inventory all sightings and independently supplied consumer selections; report ambiguous versions, roots, collisions, and unknown membership. Refuse timestamp/first-sighting winner selection, invented historical empty folders, unsupported versions, and implicit bootstrap during reads.
- Add `project/tests/test_namespace_snapshots.py` and `project/tests/test_namespace_onboarding.py` with independent manifests, explicit folder inventories, ambiguous historical associations, and unsupported format fixtures.

## Acceptance

- Import interruption publishes no partial folder. Independent traversal agrees with exact membership and preserved empty directories. A changing/unfrozen source or normalized collision refuses completion.
- Repeated serialization of one view is byte-identical under the pinned encoding; the manifest captures R and its catalog publication advances once to R+1. Retries/failures obey Phase 4's publication contract.
- Snapshot deletion is reference deletion; retained payloads/dependencies remain accurately discoverable for excision. A historical path is never an implicit current-version pointer or access grant.
- CAS-only stores work without namespace state; missing namespace is feature absence. Reconstruction preserves uncertainty until explicit selection and changes no source CAS/corpus. Version/name-policy conversion is an explicit operation, not a silent library update.

## Validation and handoff

Iteration: `./bin/test project/tests/test_namespace_snapshots.py project/tests/test_namespace_onboarding.py project/tests/test_namespace_transactions.py`. Apply the shared two-gate close protocol in [namespace-implementation.md](namespace-implementation.md).

User Demo: N/A — library admission/history lands here; the command/export workflow is demonstrated in Phase 7.

## Brief refs

- [Namespace architecture](../briefs/coordinated-namespace-crud.md), §§6–7, 9–10, and 12.
- [CAS contract](../briefs/cas-api.md), folder ingest and historical sightings.
- [CRDT semantics](../briefs/crdt-semantics.md), no automatic current-state selection.
