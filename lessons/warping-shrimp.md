---
slug: warping-shrimp
title: Distinguish clock zones before synthesizing append-only log timestamps
status: candidate
scope: methodology
proposed_surface: bin
filed: 2026-10-28
source: learn
occurrences:
  - date: 2026-10-28
    ref: "Delivery cleanup adoption LEARN — automatic local header refused before mutation"
---

The deterministic log writer synthesized the actual local time, 03:19, while the existing unzoned final anchor was 08:18. It correctly refused the append before writing. Current UTC was later than both. An unzoned historical header alone cannot establish its original timezone; treating either ordering as a wall-clock regression would overstate the evidence.

For this entry, the owner preserved the historical prefix and rejected draft/diagnostic, generated an explicit current UTC header, and labeled that entry's timezone. No time was fabricated and no chronology check was changed. A future writer improvement should define and preserve a consistent timestamp-zone contract before automatic header synthesis, with behavioral coverage for mixed-zone histories. Do not rewrite historical timestamps or disable monotonic validation to conceal this mismatch.
