---
slug: devious-cheetah
title: Bind safety-check comparisons to the exact baseline commit
status: candidate
scope: local
proposed_surface: test
filed: 2026-10-28
source: kickoff
occurrences:
  - date: 2026-10-28
    ref: "Phase 9 final exact-baseline authorization audit"
---

Before removing or relocating a safety check on the premise that it was newly introduced, compare the complete function against the exact baseline commit and preserve a negative public-workflow oracle for that check. A cached build with the same package version is not proof of those source bytes. Check both admission and return boundaries rather than only the beginning of the function.

The primary accepted independent advice that the shared excision planner's result-permission check was new. The follow-up adviser cited an older cached build; the primary checked the exact baseline's initial admission call but omitted its final authorization call. The resulting candidate removed an existing check and added tests that incorrectly expected execution and staging recovery to proceed without it. Complete final diff inspection against the exact baseline exposed the error before any commit or publication. A disposable public-workflow probe then committed a content-free excision without ever observing the denied permission kind. Prior reports, accepted qualification, passing handoff receipt, original test/source bytes and the failed audit remain preserved and explicitly withdrawn for delivery. The correction restores the baseline check, pins refusal before intent and staging progress, and rebuilds/revalidates the actual release. This is one observed engineering failure; no policy or review allowance is changed.
