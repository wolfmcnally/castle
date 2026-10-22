---
slug: namespace-contract
title: Review the refined namespace contract before runtime adoption
status: pending
category: decision
urgency: medium
blocks:
  - Actual protected deployment, real-store seed/purge and external-copy custody; does not block the approved synthetic Phases 5–7 sequence
filed: 2026-10-21
needed_at: Before actual protected deployment, real-store seed or purge, or an expansion of adopted product limits
source: kickoff
refs:
  - briefs/coordinated-namespace-crud.md
  - plan/phase-1.md
---

The refinement specifies the operator-requested concurrent ACID namespace operations, including atomic file/directory duplication, relocation and recursive deletion. Review the proposed contract in sections 14–20 before downstream runtime implementation adopts the newly pinned details. Phase 1 proves documentation and repository consistency; runtime ACID, recovery and encryption remain unimplemented and unproved.

The choices requiring product judgment are the integer-only metadata policy (decimal quantities can be strings), fixed name/Unicode policy, whole-subtree UUID mapping, coarse revisions and seven-day retry retention, explicit protected versus unprotected execution mode, and bounded requests (100000 affected entries, 16 MiB canonical requests, 64 MiB manifests). Logical deletion retains CAS bytes and explicit snapshots; confidential excision is a separate fenced, forward-only operation. Cross-domain relocation remains caller orchestration without distributed atomicity.

Accept these refinements or identify the desired changes in a follow-up. Actual protected-environment selection/qualification, real-store seed conversion or purge, external-copy custody, and publication need their own authorization/evidence and are not checkoffs for this contract-documentation phase. No live data or deployment action is requested here.

The coordinator choice is one lifetime owner per protection domain, concurrent in-process threads/tasks and no new IPC service; competing processes receive coordinator-busy. The caller routes external requests to that owner if needed. The live tree must remain within 100000 entries and 64 MiB canonical manifest bytes, so admission can refuse growth; an oversized excision selection cannot be split implicitly and may require dropping affected snapshots or a separately approved bound change. Name-policy Unicode data is coupled to the recorded runtime: an upgrade with different Unicode data refuses open; keep the qualified runtime until a separately authorized explicit conversion is specified. No conversion command is introduced here. Sanitized snapshots keep their catalog UUID but are visibly redacted historical subsets, rather than faithful captures.

## Scoped adoption disposition

The operator approved operations-first implementation on 2026-10-22 UTC (2026-10-21 local). Included operation defaults are adopted only for that slice: integer metadata, fixed name/Unicode policy, explicit duplicate maps, coarse revisions, seven-day retry results, bounded admission, single in-process owner and synthetic unprotected execution. The prior blanket “Before Phase 2” dependency was broader than the actual recovery dependency and is superseded by this scoped authorization. Historical text and Phase 1 evidence remain in Git history. This item stays pending for genuinely deferred snapshot redaction semantics, excision selection capacities and actual protected-environment qualification; no blanket ratification or deployment approval is claimed.

## Continued synthetic adoption and remaining owner inputs

On 2026-10-22 UTC the operator approved goal-complete new-byte creation/replacement and then sequential snapshot/folder-admission, excision/recovery and consumer qualification using synthetic stores with ordinary reviews/gates and local commits only. The snapshot/folder slice adopts its deterministic encoding, explicit empty-directory membership, bounded complete inventory, capture versus explicitly redacted origin schema, typed authorized export callback and ambiguity-preserving seed defaults only as needed for that approved local sequence. Runtime ACID, publication, snapshots and optional seed behavior now have disposable concurrency/interruption/refusal proofs; actual excision ownership of redacted manifests remains the next phase. This item does not impose another gate on that already approved work.

Remaining owner inputs are actual protected-environment selection/qualification, real-store seed or purge and external-copy custody, release/publication and any future change to the adopted product limits or name/runtime policy. Synthetic process-loss tests do not establish hardware power-loss behavior or encrypted deployment. No future choice or real-data operation is marked ratified by local completion.

## Synthetic excision correction and retained owner boundary

The approved local sequence now qualifies coordinated namespace excision and fresh database replacement on disposable stores. Scoped contract reconciliation retains only original predecessor/successor root equality hashes in SQL completion after private-intent disposal; it preserves the existing hash formula, CAS/public API/marker shapes and missing-proof refusal. Predictable paths remain guessable/linkable; no path secrecy, authentication, protected deployment, real-store purge or external-copy erasure is implied. Concurrent/cyclic recovery and owned artifact proofs do not replace actual filesystem/VFS/device or protected-adapter qualification. Consumer/platform/artifact qualification follows under the existing approval. The remaining owner inputs above stay pending, without adding a new gate to synthetic work or ratifying future scope.


## Qualified local consumer candidate

The approved synthetic/local sequence now includes namespace CLI/API agreement, a caller-owned unprotected session and explicit directory-handle export, real request/retry/concurrency/refusal and response-loss proofs, a runnable retained-workspace demo, actual macOS/APFS and standing Linux execution and exact isolated wheel/sdist-chain installation. Namespace additions remain an unpublished candidate under the existing package version. The local code outcome needs no renewed implementation approval; it does not close manual naming/errors/retry-window judgment, the existing release version/scope/disclosure/destination decision, protected custody or real-data adoption. No new blanket contract ratification, encrypted environment, live seed/purge, external-copy deletion or consumer repository change is inferred.
