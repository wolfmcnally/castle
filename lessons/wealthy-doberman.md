---
slug: wealthy-doberman
title: Remove hook-local Git context before a gate creates foreign repositories
status: candidate
scope: methodology
proposed_surface: bin
filed: 2026-11-04
source: kickoff
occurrences:
  - date: 2026-11-04
    ref: "Read-only release annotated-tag pre-push fallback"
---

A pre-push receipt miss ran the complete test gate under Git's exported repository-local environment. Nested fixture Git commands then redirected into the publishing repository: its branch/index/config changed even though every qualified source file remained intact. The failed fallback and exact metadata preimages were preserved, all five synthetic commits retained under a private forensic ref, and only verified test-induced metadata restored. Remote source stayed qualified and no release tag was published by that attempt.

Bind the publishing root before validation, retain receipt verification, and clear Git's own local-environment-variable list only for the fallback gate child. A real nested-repository regression must verify that the parent HEAD, logical index and config remain unchanged. The old hook fails that proof; the repaired hook passes it on macOS and isolated Linux. Preserve full fallback enforcement rather than bypassing the hook. This is a candidate for template tooling; no policy or permission change is proposed.
