---
slug: vociferous-jerboa
title: Never edit an artifact while an independent reviewer is reading it
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 11 design.md edited during the non-qualifying native review"
  - date: 2026-11-11
    ref: "Phase 12 tracked test files edited during the registered read-only code critique; ingest refused on dispatch/return candidate drift, review 1 nonqualifying"
---

A correction made to a design file while a background reviewer was reading it left that review's input ambiguous: its findings may reflect either version. Before any reviewer dispatch, copy the artifact to an immutable captured file and point the reviewer at that copy (capture-plan already does this for registered plan review). Make all later edits on a separate revision file, so every report binds to exactly one artifact hash.

Second occurrence, for product code: the registered code critique binds both its dispatch and return candidates, so any tracked-tree write while it runs, including small inventory or test fixes, makes the critique nonqualifying and spends its allowance. The safe action is a full tree freeze from dispatch until the watcher reports terminal; queue every pending edit in a scratch note outside the tree and apply it only after ingestion.
