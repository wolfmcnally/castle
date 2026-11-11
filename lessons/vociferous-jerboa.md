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
---

A correction made to a design file while a background reviewer was reading it left that review's input ambiguous: its findings may reflect either version. Before any reviewer dispatch, copy the artifact to an immutable captured file and point the reviewer at that copy (capture-plan already does this for registered plan review). Make all later edits on a separate revision file, so every report binds to exactly one artifact hash.
