---
slug: brilliant-mantis
title: register-role-attempt drops --cause on attempt 1
status: candidate
scope: methodology
proposed_surface: bin
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 11 role.plan-review attempt 1 registration"
---

`kickoff-evidence register-role-attempt --attempt 1 --reason initial --cause <text>` exits successfully ("registered") but writes no cause to either the registration file or `role-attempts.jsonl`. When the operator counts an earlier unregistered pass toward the two-pass bound, the first registered attempt is in fact the second pass and its cause must be durable. Today the cause survives only in a hand-written run file and the END block. Either persist `--cause` whenever it is supplied, or refuse it on attempt 1 so the omission is visible rather than silent.
