---
slug: favorite-mongoose
title: Qualify complete control progress within the bounded observation
status: candidate
scope: local
proposed_surface: test
filed: 2026-10-28
source: kickoff
occurrences:
  - date: 2026-10-28
    ref: "Phase 9 hosted macOS lock-proof control calibration"
---

When an exclusion proof requires a live control to complete multiple cycles, syscall acquisition latency alone is an incomplete calibration proxy. The complete cycle also includes setup, event writes, cleanup and scheduling. Keep continuous exclusion/aliveness checks and the original evidence minima; observe that actual progress within a fixed cap, refusing if the controls cannot qualify rather than reducing counts, skipping or blindly rerunning.

Hosted macOS CI observed nine free-lock control cycles in a half-second window against a required ten. Its refusal was truthful but the window estimated only acquisition latency. A deliberately delayed actual control reproduced the same problem locally with six cycles. The bounded correction preserves the half-second minimum, ten cycles, five heartbeats and two-second cap, and qualifies progress in one continuously watched interval. The existing proof family additionally demonstrates immediate refusal of an unlocked waiter, dead control and insufficient progress at the unchanged cap. Native and Linux affected journal families pass; distribution and final qualification remain separate. The observations do not identify scheduling versus filesystem overhead as the hosted contributor. No product runtime, concurrency guarantee, assertion threshold or proof-family inventory changes.
