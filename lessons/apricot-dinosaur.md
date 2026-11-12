---
slug: apricot-dinosaur
title: Before dropping a check under a narrowed threat model, enumerate the honest faults that check also catches
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Phase 13 planning, reconciliation of the no-Byzantine ruling (plan v3, superseded by v4)"
---

Observed once. The operator ruled that the stack does not defend against Byzantine faults. In applying the ruling, the owner reasoned that "a repeated event id requires a forger, because Castle mints random ids". On that basis it dismissed four review findings by label and dropped excision's whole-journal witness. The coordinator rejected the reasoning. Crash replay, a duplicate append, a recovery path re-sending saved events, and appending onto a restored older journal all repeat a previously minted id without any adversary. An honest merge or rebuild can re-sort earlier journal lines between excision's steps.

The proxy was "random identifiers imply uniqueness". It inverts the sign: it reclassifies honest faults as adversarial, then removes the checks that catch them, while the ruling meant to keep them. The corrected plan rests the honest repeated-id refusal on canonical ordering of the appended range and keeps excision's witness, with a stated non-Byzantine purpose.

Containment: the plan was revised before any implementation, and the withdrawn justification is kept in the dispositions with a superseding correction. Prevention candidate: when a ruling narrows the threat model, list for each check it would drop the non-adversarial failures that check also detects (replay, retry, restore, re-sort, crash between steps). Drop a check only when that list is empty, and record each dropped case as an exclusion, never as a fix.
