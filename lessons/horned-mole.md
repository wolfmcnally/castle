---
slug: horned-mole
title: Commit allocation ownership before creating recoverable operation files
status: candidate
scope: local
proposed_surface: test
filed: 2026-10-21
source: kickoff
occurrences:
  - date: 2026-10-21
    ref: "Phase 2 code advice CODE-F002 and interrupted evidence-directory proof"
---

Persist the independent ownership recipe before allocating a recoverable operation directory or writing its files. A final UUID-shaped name and a partial receipt do not establish ownership after a crash. Recovery needs an authoritative record outside the allocation being repaired; only then can it distinguish an interrupted owned write from unrelated residue without guessing or deleting by filename.

The first namespace implementation created the operation directory, request and receipt before its prepared SQL intent. Source review identified the missing-directory/file windows; real child termination now exercises both. The correction commits the exact request and closed ownership recipe in SQL first, then allocates its slots. Explicit recovery validates namespace/generation/CAS identity and repairs only missing files or exact canonical prefixes, while different bytes and unknown inventory refuse. The existing restart/fault proof family carries these cases; no new test family, budget increase, corruption-repair promise or rule graduation follows.
