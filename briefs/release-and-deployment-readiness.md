---
title: "Release and deployment readiness: Linux-proven core, first tagged release, and the excision question"
date: 2026-09-06
status: draft
scope: "The near-term roadmap for deployed consumers: Linux qualification of the ingest/journal/verify core, a first tagged MIT release, and qualification of successor-epoch excision with a separate external-custody boundary."
---

# Release and deployment readiness

## Why now

Castle's contract is fully delivered and accepted: strict journal-v2 authority, objects, indexes, ingest, verification, copy-on-write cloning, CRDT-union merge, and the guarded v1 migration. Its consumers to date run on this machine. The next class of consumer is **deployed**: a service on cloud Linux that uses Castle as its content-addressable document store and audit journal — ingesting, sealing, verifying, and journaling, with the store's isolation guarantees load-bearing for the consumer's own security model. Consumer shapes are described here only as capability classes, never by name; a Castle that is about to become public carries no references to who uses it.

Three items follow, in increasing order of consequence. The operator authorized excision design and implementation as recorded in §3. The [CAS contract](cas-api.md) distinguishes the implemented successor-epoch extension from its historical extraction boundary; qualification and release claims require separate evidence.

## 1. Ingest/journal/verify proven green on Linux

`cross-platform-cloning.md` already establishes the posture: ingest degrades gracefully (clone when the platform can, byte-copy with digest re-check when it cannot), while store cloning and merge refuse off-Darwin — correctly, because a silent byte-copy wearing clone's name would betray its cost model. What is missing is proof, as a gate rather than a reading of the source:

- The test suite runs green on a Linux host (CI or a pinned container) for every surface that does not require Darwin: journal semantics, ingest with the byte-copy path, receipts, lookup, materialization, statistics, verification, index rebuild, and the CLI commands over all of these.
- The Linux gate runs Castle's complete retained proof estate, not a separate
  hand-picked smoke lane. Its recipient-local reset evidence remains valid:
  both family and leaf counts stay within their frozen ceilings, historical and
  held-out recall stay above their floors, and every applicable critical risk
  keeps a direct executable witness.
- The Darwin-only surfaces refuse *loudly and testably* on Linux — the refusal identifiers already exist (`store-merge-platform-unsupported` and kin); the Linux run must assert them rather than skip them.
- The known `cas._clone_file` defect recorded in `cross-platform-cloning.md` — BSD-only `cp -c` on whatever `cp` is on `PATH`, no platform test, silent fall-through on GNU systems — gets its platform test in the same pass, since the Linux CI run is precisely the environment that exposes it.

Out of scope, unchanged from the survey brief: a Linux reflink backend for clone/merge (Btrfs/XFS `FICLONE`). That is real future work with its own design sketch, but no deployed single-store consumer needs it — clone and merge matter when replicas exist, and a first deployment has one store per partition and no replica topology.

## 2. First tagged release

Consumers currently pin a commit hash. A deployed consumer needs a release: a tagged version whose promise is written down — suite green on macOS and Linux per item 1, docs current for released surfaces, the MIT `LICENSE` already in the repo governing — plus a changelog seed. The pre-release sweep verifies no committed file references any consumer by name; capability classes only. Tagging itself is the owner's act, per the destructive-git boundary.

## 3. Excision — successor-epoch implementation

Castle's object and event authority remains grow-only within each merge epoch. The implemented excision operation reconstructs a successor from retained authority, assigns a new epoch, fences ordinary access to the local predecessor, and removes its content-bearing files. Merge refuses across epochs, so an old replica cannot restore removed authority by union into the successor. Explicit ingestion remains a separate caller action; an epoch fence is not a content denylist.

As of 2026-09-05, the operator's ruling is: “You are authorized to implement excision.” It supersedes the earlier reservation requiring a second implementation approval. The independently reviewed [content-excision contract](content-excision.md) owns the exact API, metadata, errors, recovery and local receipt. Implementation is present; release readiness must be established against the candidate being delivered, including macOS and Linux execution and recovery proofs.

No real-store operation or custody acceptance follows from implementation status. Consumer withdrawal and reference updates, retained originals, remote object versions and backups remain external obligations. Local completion does not establish physical erasure or removal of every copy.

## Sequencing

Item 1 establishes a standing Linux gate. Item 2 follows item 1 — the tag's promise includes the Linux gate. Item 3 requires independent implementation review and executable qualification; no renewed implementation permission is required. Release preparation for the existing portable contract need not wait for excision. Tagging and publication remain owner actions, and custody acceptance remains separate from implementation delivery.
