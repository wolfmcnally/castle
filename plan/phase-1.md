---
id: 1
title: Namespace and recovery contract refinement
depends_on: []
informs: []
---

# Phase 1 — Namespace and recovery contract refinement

## Goal

Settle the consequential public, persisted, recovery, and protected-execution contracts needed by the coordinated namespace before runtime implementation.

## Deliverables and scope

- Refine `briefs/coordinated-namespace-crud.md` with exact typed library command/request/result/error shapes, namespace schema/version, canonical manifest encoding, root/entry/operation identity rules, name/metadata limits, revision/idempotency outcomes, retry retention, and cleanup ownership. Pin a deterministic numeric encoding for snapshot/fingerprint JSON, rather than assuming key sorting defines float serialization.
- Specify a bounded CAS publication/recovery interface: durable operation evidence, known journal boundary/event identities, staged bytes, root/epoch binding, lock order, deduplication, append interruption, index/remotes preservation, completion versus blocked-storage outcomes. No guessed repair of unrelated corruption; preserve the strict journal format and root inventory. Recovery state resides in the disjoint owned operation area.
- Specify the exact namespace replacement-marker schema and publication/fsync/recovery order, fresh database generations, sidecar/recipe ownership, confidentiality checks, selection-free durable completion evidence, and resumability after replacing the authority database.
- Specify the caller-owned protected execution/I/O contract for SQLite, CAS, staging, locks, atomic replacement, restart, admission, authorized reads, and key/session lifetime. Define required capabilities and refusal/qualification evidence without choosing a crypto/backend product or inventing a host-encryption guarantee.
- Record API/CLI ownership and the contract-to-proof matrix; amend the owning CAS/excision briefs only where the proposed extension genuinely adds an API. Preserve the distinction between specified/proposed behavior and delivered behavior. Ripple pinned contracts into pending phases.

## Acceptance

- Contract tables cover every command, authority field, recovery/cutover stage, and terminal/refusal outcome in the roadmap; independent design advice is disposed against the exact candidate.
- The append-interruption and database-replacement sequences identify durable authority at every crash boundary and never require predecessor-selected payloads after retirement.
- The encrypted deployment prerequisites state what Castle can prove and what the caller's adapter must prove; no plaintext fixture substitutes for protected environment evidence.
- Documentation links/catalogs and both full close gates pass. Manual: the owner judges any consequential change to the selected architecture or deployment boundary; unaccepted extensions do not silently authorize downstream runtime work.

## Validation and handoff

Iteration: `./bin/check policy`. Apply the shared close protocol in [namespace-implementation.md](namespace-implementation.md); this phase proves contract/document consistency, not runtime recovery.

User Demo: N/A — this phase refines contracts and exposes no executable user surface.

## Brief refs

- [Namespace architecture](../briefs/coordinated-namespace-crud.md), §§2–11 and 13.
- [CAS contract](../briefs/cas-api.md), journal, write path, verification, and explicit-root ownership.
- [CRDT semantics](../briefs/crdt-semantics.md), authority and within-epoch union.
- [Excision](../briefs/content-excision.md), retirement and forward recovery.
