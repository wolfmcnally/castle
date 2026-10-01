---
id: 3
title: Namespace authority and database replacement
depends_on: [phase-2.md]
informs: []
---

# Phase 3 — Namespace authority and database replacement

## Goal

Provide the versioned durable namespace authority and a qualified forward database replacement protocol that later CRUD and purge can share.

## Deliverables and scope

- Add the proposed `project/castle/namespace_store.py` owning schema, explicit initialization/open, namespace/root identity, CAS binding, policy/Unicode version, revision, durable intent/result and snapshot/dependency tables. Disjoint roots and unsupported/missing/inconsistent authority refuse; never rebuild live state from CAS sightings.
- Implement local SQLite transactions, foreign keys, full durability, stable process/thread coordinator locking, owned staging, and consistent reads. Require one coordinator; do not steal a live lock on timeout or introduce a multi-host database.
- Implement the Phase 1 replacement-marker protocol, fresh database validation, checkpoint/close, directory durability, generation agreement, exact owned-residue cleanup, and recovery after every replacement boundary. Keep the lock and selection-free marker outside the database being replaced. Do not add namespace files to CAS inventory.
- Add `project/tests/test_namespace_store.py` with independent authority fixtures, subprocess lock/restart cases, and selected sentinel metadata in old database pages, sidecars, and staging. The fixture tests storage mechanics; it supplies no encryption guarantee.

## Acceptance

- Explicit init is the only way namespace authority is created. A missing database, unsupported version, changed CAS binding, overlapping roots, wrong generation, or unresolved marker refuses ordinary service.
- Multiple clients observe one durable revision; a second coordinator cannot mutate while the first holds the lock. Abrupt death releases the process lock without discarding durable authority.
- Interrupt fresh-image publication, replacement, sidecar disposal, marker advancement, and completion; resume forward with validated authority, or stay unavailable. Unknown residue is not removed.
- Independent scan of the owned fresh database/sidecars/staging confirms selected sentinel absence only after cleanup; reference publication or row deletion alone cannot pass that check. Host remanence and external copies remain outside the claim.

## Validation and handoff

Iteration: `./bin/test project/tests/test_namespace_store.py project/tests/test_cas_recovery.py`. Apply the shared two-gate close protocol in [namespace-implementation.md](namespace-implementation.md). Qualify locking, directory durability, and replacement on supported local filesystems; any unavailable platform capability is reported honestly and cannot establish its qualification.

User Demo: N/A — durable authority and replacement are library foundations; live namespace interaction lands in Phase 7.

## Brief refs

- [Namespace architecture](../briefs/coordinated-namespace-crud.md), §§2–3, 6, 9, and 11–12.
- [CAS contract](../briefs/cas-api.md), strict inventory and explicit roots.
- [Excision](../briefs/content-excision.md), durability and irreversible recovery boundary.
