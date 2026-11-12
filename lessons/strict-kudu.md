---
slug: strict-kudu
title: A phase that amends one of its own declared authorities must capture that authority after the last amendment
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Phase 12 acceptance validation refusal at 00:27Z (declared authority changed)"
---

Observed once. Phase 12's plan includes amendments to a brief that is also one of the run's declared authorities. The run captured that brief at initialization, then the owner applied the planned amendments and later the final-critique corrections to it inside the same run. Every gate passed, but acceptance validation refused with "declared authority changed; re-review in a fresh evidence run", because the captured bytes no longer matched. The refusal was correct and could not have been avoided within that run once the planned amendment landed.

Containment: the refusal, the captured, reviewed and current hashes and both diffs were preserved; the run was parked truthfully and a fresh run captured the final authority, carrying the advice with its original identities and spent pass budget. The post-critique amendment is recorded as unreviewed.

Prevention candidate: when a phase's deliverables include edits to a declared authority, the kickoff resource should either sequence those edits before evidence initialization or make the run declare the file as a phase deliverable rather than a fixed authority, and command zero should warn when a tracked declared authority differs from its capture before any gate runs, rather than letting the drift surface only at acceptance.
