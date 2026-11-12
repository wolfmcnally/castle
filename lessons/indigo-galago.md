---
slug: indigo-galago
title: Check who owns operator communication before raising a user-visible decision surface from a coordinated session
status: candidate
scope: methodology
proposed_surface: skill
filed: 2026-11-12
source: kickoff
occurrences:
  - date: 2026-11-12
    ref: "Phase 13 planning input-park, Fenric decision dialog at 01:47Z"
---

Observed once. At the planning input-park the owner launched a 60-minute operator decision dialog and sent its link to the coordinator for relay, following the documented park route. The operator was under a communication mute owned by the coordinator's root at the time. The dialog then had to stay open untouched until it expired, unanswered, because closing a window early was itself not allowed. The decision was later obtained through the coordinator in the operator's own words.

The documented route assumes the session may address the operator directly. In a coordinated session, a separate party owns that channel and its holds.

Containment: the dialog was left to its bound, its expiry was recorded as not consent, and nothing was regenerated. Prevention candidate: before opening any operator-visible surface from a coordinated session, ask the coordinator whether the operator may be addressed now. Otherwise send it the exact question wording and options and let it present them.
