---
slug: merciful-lemur
title: Validate terminal log witnesses before irreversible append
status: candidate
scope: methodology
proposed_surface: bin
filed: 2026-10-21
source: kickoff
occurrences:
  - date: 2026-10-21
    ref: "Phase 1 policy-log failure after preparation PARK"
---

Validate the standalone terminal Lessons heading at the append boundary before accepting a new END or PARK block. The existing policy requires a literal standalone `Lessons:` line, but `bin/log-append` validates chronology and `kickoff-evidence close` can append a block whose heading and prose share one line. A later policy check then rejects bytes that the append-only rule forbids routinely rewriting. The operator-authorized one-byte formatting repair preserved the rejected block and closure and left the committed prefix unchanged; it did not make the original closure a successful validation run.

The immediate contributor was an agent formatting error: `Lessons: none filed...` was written on one physical line. The tooling contribution was admitting the malformed terminal block before the existing executable guard checked it. Next time, run the existing terminal-witness validation on the proposed block before append; a future ratified tool correction should enforce the same condition at the writer boundary and include a negative fixture. This candidate proposes no policy relaxation and authorizes no implementation in this phase.
