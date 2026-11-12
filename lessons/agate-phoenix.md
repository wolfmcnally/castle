---
slug: agate-phoenix
title: A fixed-seed randomized proof must seed every source of variation, including generated identifiers
status: candidate
scope: local
proposed_surface: test
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Phase 13 focused run; Phase 12 differential_proof"
---

Observed once. The Phase 12 differential proof seeds its operation sequence with a fixed `random.Random`, but its entry ids come from `uuid4`, and import entries are sorted by entry id. Which refusal classes the sequence reaches therefore varied between runs. A copy of the last commit failed its refusal-coverage assertion in 1 of 40 repetitions, and it surfaced as an unexplained red in an unrelated focused run.

Containment: the proof now draws entry ids from its own seeded generator. Prevention candidate: when a proof claims fixed-seed determinism, route every identifier, timestamp and ordering input through the seed, and repeat the proof enough times to show it before relying on it.
