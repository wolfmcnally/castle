---
slug: jumping-tench
title: Test response failure through interpreter finalization after commit
status: candidate
scope: local
proposed_surface: test
filed: 2026-10-22
source: kickoff
occurrences:
  - date: 2026-10-22
    ref: "Phase 7 real closed-pipe response probe and CODE-F004"
---

Test a promised CLI response-failure status with an actual subprocess and closed pipe, then independently query the original durable operation token. Catching a buffered print/flush exception is not the last observable boundary: interpreter finalization may flush again and replace the intended exit code. Transport failure must remain an unknown response, never a false rollback assertion or permission to submit a new operation UUID.

The original synthetic namespace mutation committed revision13 and status reported committed, but a closed stdout caused CPython finalization to retry its buffered write, emit BrokenPipeError and replace the intended74 with120. The preserved original probe and independent advisory observation identified the same boundary. The correction closes the failed terminal stream and uses ASCII JSON escapes for configured ASCII output; actual closed stdout/stderr and response SIGINT probes now preserve the documented74/130 unknown-response codes and durable status. The existing CLI proof family executes those cases and every printed persistent-demo command; no new proof family or runtime storage contract is added. This is candidate engineering learning, not a hardware-durability claim or a graduated policy.
