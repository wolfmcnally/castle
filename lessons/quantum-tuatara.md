---
slug: quantum-tuatara
title: A damage-injection proof must target its intended structure and prove the intended path was reached
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Phase 12 final-critique corrections (F006 replay cases, F021 page corruption)"
---

Observed twice in one correction pass. First, a replay proof damaged an operation row that was also the open-time settlement marker, so ordinary open refused before the replay path ran; both revert mutants of the fix under test survived, which exposed it. Second, a page-corruption proof overwrote every database page containing the target id's bytes; with random identifiers one of those pages could belong to an index or table that open reads, so the proof passed in focused runs and failed in the full suite, refusing at open instead of at the touched read.

Containment: each proof was rebuilt before acceptance. The replay cases damage an operation that does not own the marker and first assert that ordinary open does not touch it. The page case walks the table's b-tree from its root page and corrupts only the one leaf holding the target row.

Correction: every new falsifying check in the pass was red-checked against the pre-fix product or a scratch mutant, and the order was to strengthen the proof first and then confirm that the mutant fails at the intended assertion, not merely that something fails.

Prevention candidate: in the implementation resource, require that a damage-injection proof (a) selects its target by structure (schema, b-tree, row identity), never by a byte or string pattern, (b) asserts the earlier layers that could intercept it (open, marker, admission) did not, and (c) is shown red by a mutant of the specific guard, with the failing assertion named.
