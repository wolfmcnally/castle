---
slug: imported-manatee
title: A design-only phase configured with full review and evidence lanes in primary mode has no reachable accepted close
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-11
source: kickoff
occurrences:
  - date: 2026-11-11
    ref: "Phase 11 close"
---

Observed once, for one configuration: a product phase whose deliverable is a reviewed design, with no product code, configured with the full review and evidence lanes in product primary mode, could not close as accepted. This is not a claim that every design phase is unclosable; other lane configurations were not examined. `kickoff-evidence close --outcome accepted` requires a registered `role.code-review` attempt, an active gate manifest and a final candidate gate, and the phase has nothing to critique and no review allowance left. The only truthful terminal record is a non-accepted park, and the phase row stays 🚧 indefinitely. When a design-only phase is configured this way, decide at planning time how it terminates: a declared design lane whose acceptance is the ingested plan review plus the two full gates, or a rule that design work is the planning stage of the implementation phase it informs rather than a separate phase.
