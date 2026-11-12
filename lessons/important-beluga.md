---
slug: important-beluga
title: The watcher owns the advisory launch claim; a manual start-advice spends the pass with no child
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 12 final code critique, prelaunch refusal at 22:45:52Z"
---

Observed once. In a primary-mode run, the orchestrator ran `kickoff-evidence start-advice` by hand after `register-role-attempt`, then launched `kickoff-config watch --telemetry-role-registration`. The watcher claims the launch itself, so it refused with "advisory invocation already launched; resume is another pass". It exited before creating any child, but the phase's last allowed pass was recorded as spent. The watcher's own cancellation covered only claims it made in the same invocation, so no supported refund existed.

Containment: no review was transmitted. The refused receipt, the claim and the artifacts were preserved.

Correction: a tested `reconcile-unstarted-advice` control now refunds exactly such a claim once, after checking the rejected, not-dispatched receipt, the matching active claim and that the dispatch candidate is in the run's recorded lineage; the repair itself moves the product candidate, so both candidates are recorded rather than asserted equal. The executable catalog says that the watcher owns the claim.

Prevention candidate: make `start-advice` refuse when it is not called by the watcher (for example, with a watcher-issued nonce), so the mistake cannot recur. That is an executable guard, not a prose reminder.
