---
slug: sincere-jaguar
title: Accept Phase 12's moved-detection statement and decide the Phase 13 CAS membership question
status: pending
category: decision
urgency: medium
blocks:
  - Planning Phase 13 (incremental content publication); does not block Phase 12's local delivery
filed: 2026-11-12
needed_at: Before Phase 13 planning
source: kickoff
refs:
  - briefs/coordinated-namespace-crud.md
  - plan/phase-12.md
---

Phase 12 makes ordinary namespace open, admission, reads and metadata mutations cost what they touch. Touched damage fails closed and latches; damage in untouched records is found on touch, at whole-sequence operations or by `diagnose`. Brief section 24 states exactly which checks moved and where they now run. Accepting that statement is the phase's one manual criterion and is yours.

Two residuals stay open with no waiver: a coherent edit of both a located row and its witness inside namespace.sqlite is not detectable on touch, and a repeated journal event id carrying another object's identity, appended after its original located row was lost, is accepted by incremental location until that original object is next touched or `diagnose` runs. Phase 13 removes publication's full CAS check, which today also stops the second case at the next publication.

The decision for Phase 13: whether a derived `events(event_id, cas_id, offset)` table in cas.sqlite, maintained by every Castle CAS writer and compared by verify, is acceptable. It changes no journal or object bytes, but it amends the CAS index schema that cas-api.md owns, and recording "verified through event count k" would add a field to the namespace's index position, a second namespace format change that is cheapest before v2 ships. If it is excluded, Phase 13 must keep a bounded check that refuses an appended event id already present anywhere in the journal before lightening publication, or state the longer detection window. The mechanism that actually closes the case is "an appended event with no CAS-written row forces full revalidation".

No live store, migration, release, tag or push is requested here.

Reconciliation (2026-11-12, appended; the text above is unchanged). The principle in the first paragraph is already the operator's ruling and is not asked again: in Phase 11 the question "For Castle, should normal operations validate only the records they touch, with damage elsewhere detected when accessed or during a full diagnostic, rather than blocking unrelated work immediately? The owner recommends this approach to meet your scaling requirement." was answered "Yes, absolutely." What remains for the operator in the first paragraph is only whether brief section 24's list of moved checks faithfully implements that ruling; its last amendment has had no independent review. That ruling does not cover the two residuals in the second paragraph: the first leaves a touched record undetected, and the second admits a new append built on undetected damage rather than only delaying detection. Both stay open with no waiver. On 2026-11-12 the Phase 13 design row was authorized; that is design authority, not a ruling on the CAS index schema question in the third paragraph, which is now decision 1 in plan/phase-13.md.
