---
slug: namespace-contract
title: Review the refined namespace contract before runtime adoption
status: pending
category: decision
urgency: medium
blocks:
  - Runtime adoption of the refined proposed namespace contracts in Phases 2–7; does not block Phase 1 documentation delivery
filed: 2026-10-21
needed_at: Before Phase 2
source: kickoff
refs:
  - briefs/coordinated-namespace-crud.md
  - plan/phase-1.md
---

The refinement specifies the operator-requested concurrent ACID namespace operations, including atomic file/directory duplication, relocation and recursive deletion. Review the proposed contract in sections 14–20 before downstream runtime implementation adopts the newly pinned details. Phase 1 proves documentation and repository consistency; runtime ACID, recovery and encryption remain unimplemented and unproved.

The choices requiring product judgment are the integer-only metadata policy (decimal quantities can be strings), fixed name/Unicode policy, whole-subtree UUID mapping, coarse revisions and seven-day retry retention, explicit protected versus unprotected execution mode, and bounded requests (100000 affected entries, 16 MiB canonical requests, 64 MiB manifests). Logical deletion retains CAS bytes and explicit snapshots; confidential excision is a separate fenced, forward-only operation. Cross-domain relocation remains caller orchestration without distributed atomicity.

Accept these refinements or identify the desired changes in a follow-up. Actual protected-environment selection/qualification, real-store seed conversion or purge, external-copy custody, and publication need their own authorization/evidence and are not checkoffs for this contract-documentation phase. No live data or deployment action is requested here.

The coordinator choice is one lifetime owner per protection domain, concurrent in-process threads/tasks and no new IPC service; competing processes receive coordinator-busy. The caller routes external requests to that owner if needed. The live tree must remain within 100000 entries and 64 MiB canonical manifest bytes, so admission can refuse growth; an oversized excision selection cannot be split implicitly and may require dropping affected snapshots or a separately approved bound change. Name-policy Unicode data is coupled to the recorded runtime: an upgrade with different Unicode data refuses open; keep the qualified runtime until a separately authorized explicit conversion is specified. No conversion command is introduced here. Sanitized snapshots keep their catalog UUID but are visibly redacted historical subsets, rather than faithful captures.
