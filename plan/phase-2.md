---
id: 2
title: Bounded CAS interruption recovery
depends_on: [phase-1.md]
informs: []
---

# Phase 2 — Bounded CAS interruption recovery

## Goal

Implement and qualify the contract-defined recovery of interrupted namespace-owned CAS publication before the namespace can publish live references.

## Deliverables and scope

- Add the proposed `project/castle/cas_recovery.py` public recovery owner and its types; integrate the needed evidence/publication hooks into existing `project/castle/cas.py` and `project/castle/journal.py` without changing ordinary strict journal interpretation or inventing new event fields.
- Persist operation-owned recovery evidence outside the CAS root before the corresponding unsafe publication boundary. Validate exact bytes, journal prefix/event identity, active store identity/epoch, ownership, and lock order before completing an interrupted operation. Handle object-published/journal-missing, incomplete append, complete append/index-stale, and post-completion response loss as distinct states.
- Preserve target-local remote custody rows when rebuilding derived index state. Unknown residue, unrelated corruption, unreadable remotes, missing required evidence, or lifecycle mismatch stays blocked; ordinary rebuild-index is not broadened into guess-based repair.
- Add `project/tests/test_cas_recovery.py` with real subprocess kills and independently expected journal/object/index results. Extend existing CAS/journal/merge regression proofs and record proof admission under the shared roadmap.

## Acceptance

- Kill at object publication, within journal append, before/after index commit, and after durable completion; the bounded operation completes once or refuses with storage recovery required. No accepted store has a dangling/misdescribed object or silently lost remote row.
- Replaying the same evidence is safe; changing identity/epoch/request or using unrelated temporary files refuses without deleting user/unknown files.
- Existing strict-format, immutable object, CAS-only command, same-epoch union, and excision/retirement behaviors still pass their relevant regression suites.
- A consistent CAS can be handed to namespace publication only after complete validated storage recovery. No automatic repair claim extends to corruption without operation evidence.

## Validation and handoff

Iteration: `./bin/test project/tests/test_cas_recovery.py project/tests/test_cas.py project/tests/test_journal.py project/tests/test_merge.py`. Apply the shared two-gate close protocol in [namespace-implementation.md](namespace-implementation.md); synthetic subprocess evidence is required, not mocked exceptions alone.

User Demo: N/A — this is a library recovery prerequisite; consumer-facing recovery presentation lands in Phase 7.

## Brief refs

- [Namespace architecture](../briefs/coordinated-namespace-crud.md), §6 and §12.
- [CAS contract](../briefs/cas-api.md), write ordering, strict journal, verification, and remotes.
- [CRDT semantics](../briefs/crdt-semantics.md).
- [Excision](../briefs/content-excision.md), lifecycle fencing.
