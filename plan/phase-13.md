---
id: 13
title: Incremental namespace content publication
depends_on: [phase-12.md]
informs: []
review_lane: full
---

# Phase 13 — Incremental namespace content publication

Sketch. Authorized as a bounded design row on 2026-11-12 within the operator's approval of incremental uploads as Phase 13. The planner refines it at kickoff; every planned brief or contract amendment is made before evidence initialization.

## Goal

Make namespace content publication (the namespace's owned upload path in `cas_recovery`, not ordinary CAS ingest) cost what it touches instead of the whole content store, without weakening any guarantee publication gives today except the one the operator already approved: damage in untouched records is found when they are touched, by whole-sequence operations or by an explicit diagnosis. Operator principle (2026-11-11): "O(N) operations like 'validate everything' should be for deep tests and diagnostic/repair tools. Normal business should as much as possible amortize to O(1) or O(n). N = all elements in set, n = directly affected elements in set." Operator ruling (Phase 11 R3), selecting touched-record validation: "Yes, absolutely."

Operator ruling (2026-11-12), recorded after the phase's plan review: "In general we are not implementing Byzantine fault tolerance anywhere in our stack. So no, we should not scan the whole journal except as part of a doctor diagnosis/recovery procedure/tool." Publication therefore defends against crash and interruption faults and honest Castle writers, without any whole-journal scan. Adversarial edits of the journal, the index or the namespace database are outside its threat model and are stated as exclusions, not as defects fixed. Repeated event ids from honest faults stay refused on the appended range: every Castle append sorts strictly after the journal tail, so a re-appended earlier record breaks canonical order.

## Today's cost (the boundary Phase 12 left)

Each publication in `cas_recovery` validates the whole content store up to three times under the stable lock: it parses the whole journal, hashes every stored object, compares every index row, hashes the whole journal prefix, rebuilds the whole index (delete then reinsert every object and name row), and records a SHA-256 of the whole journal through its append in the receipt. Ordinary CAS ingest keeps its documented whole-store validation unchanged; this phase does not lighten it.

## Deliverables

- **Touched-object publication.** Publication validates the object it publishes (size, byte hash, sealing posture), the journal bytes it appends after, and the index rows it changes. It no longer hashes untouched objects, re-parses the whole journal or rebuilds the whole index on the ordinary path.
- **Journal position without trusting stamps.** Publication appends after a verified position: an exact length the namespace has already parsed, extended by parsing only the bytes appended since. A verified position says where parsing resumes; it is not proof that the untouched prefix bytes are unchanged, and no resumable whole-prefix hash is claimed. Two kinds of check are kept apart: the touched and appended authority (the published object's own journal records at their recorded offsets, and every appended byte) is checked against current bytes on every publication; damage inside the untouched prefix is the operator-approved delayed detection, found on touch, by a whole-sequence operation or by diagnosis. No stamp, chain or tail witness is trusted. A framing break, a record mismatch or an unknown position forces full revalidation.
- **Duplicate-event guarantee for honest faults.** Amended by the 2026-11-12 ruling. Publication no longer refuses a repeated id anywhere in the journal, because that needs a whole-journal scan. It refuses honest repeated ids on the appended range, through the namespace catch-up's ordering and known-id checks; these hold even when the namespace's own row was lost. A forged later-timestamp duplicate is a stated threat-model exclusion. No derived table, in either database, is treated as authority. No new content-store table is added.
- **Incremental index commit.** The index changes only the rows the publication adds. Rebuild, verify and diagnosis still fold the whole journal and compare every row.
- **Truthful contract text.** The namespace brief and the CAS contract state which publication checks moved, to where, and when each kind of damage is detected.

## Decisions

Resolved 2026-11-12. Decision 1: neither route; the operator's no-Byzantine ruling removes the whole-journal scan, and honest repeated ids are refused by appended-range ordering, so no content-store schema change is needed. Decision 2: no new field; `cas_index_position` already holds the verified length, event count and tail line. Decision 3: the plan lists every moved check. Decision 4: the receipt binds the publication's own journal line instead of a whole-journal SHA-256; excision keeps its own whole-journal witness. The original wording follows.

1. **Content-store event membership (operator).** Route A: a derived `events` table in `cas.sqlite` (event id, object identity, journal offset), written by every Castle CAS writer in the same commit as its other index rows (ingest, every rebuild including merge, v1 migration and excision, and publication recovery) and compared row for row by verify. It is a disposable cache rebuilt from the journal, like the rest of `cas.sqlite`, and is never authority: a row is trusted only after its offset is checked against the journal bytes it names. It changes no journal or object bytes, but it amends the index schema that `briefs/cas-api.md` owns, so it needs the operator's ruling. Route B, needing no contract change: keep a bounded check that refuses an appended event id already present anywhere in the journal, using the namespace's own per-event rows plus a full fold whenever those rows cannot vouch for the appended range.
2. **Verified position field.** Recording "verified through event count k" adds a field to the namespace's index position, a second namespace format change. Version 2 is unreleased, so it is changed in place under the greenfield rule; no second migration is added.
3. **Which publication checks move.** Byte hashing of the published object, duplication, materialization and export stays. The plan lists every other check publication runs today and names where each now runs.
4. **Receipt journal digest.** The receipt's whole-journal SHA-256 is itself whole-store work. The plan states whether the receipt keeps it, binds a different exact value, or drops it, and what each consumer of the receipt relies on.

## Carried obligations

- Both Phase 12 residuals stay open with no waiver and must not be widened: a coherent edit of both a located row and its witness is not detected on touch; a repeated event id carrying another object's identity, appended after its original located row was lost, is accepted until that object is touched or diagnosis runs. Removing publication's full check must not lengthen the second window unless the operator rules otherwise. The operator so ruled on 2026-11-12 (see Goal): honest repeated ids stay refused on the appended range, and only a forged later-timestamp duplicate falls outside, as a stated exclusion.
- Phase 11 F006: content publication cost belongs here.
- The Phase 12 final-critique correction: the membership assessment's offset column is inconsistent between its sections 4 and 6, and the mechanism that closes the duplicate case is "an appended event with no CAS-written row forces full revalidation".
- The withdrawn Phase 11 claims stay withdrawn: no resumable whole-prefix hash and no classification of changes by modification time.

## Acceptance

Executable:
- **Cost tests.** Publication cost is proved against store size by counting object hashes, parsed journal records and index rows written, at two store sizes.
- **Damage tests.** Damage in the published object, its own journal records, the appended range or the changed index rows refuses. Untouched damage is found on touch, at a whole-sequence operation or by diagnosis.
- **Honest repeated-id falsifiers.** Amended by the 2026-11-12 ruling. A saved event re-appended after a crash, a record re-appended after the namespace's own row was lost, and publication against a restored older journal copy are each refused with no evidence record and no journal change. Each test must fail on a mutant that drops the before-tail ordering check or the known-id check. Forged later-timestamp duplicates are a stated exclusion and are not asserted.
- **Interruption tests.** Publication recovery after each kill point still completes or removes only its own proven suffix.
- **Gates.** The phase checks, then `./bin/check all`, on macOS and Linux.

Manual: architectural acceptance of the moved-check statement remains the operator's.

## Bounds

- **Not authorized:** live stores, live migration, release, tag, push or public publication.
- **Out of scope:** ordinary CAS ingest admission (it stays fully validating), and journal or object byte formats.
- **Open, unwaived, no release claim:** two obligations stay open: the touched-integrity gap (a coherent edit of a located row and its witness is not detected on touch) and the upload-admission gap (a repeated event id is admitted after its original located row was lost). This sketch makes no release claim while either is open. Each closes only by a reviewed solution, never by a waiver. This phase aims to close the upload-admission gap through the duplicate guarantee above; the touched-integrity gap is not addressed here, and leaving it out does not accept it. Amended 2026-11-12: under the no-Byzantine ruling both gaps are adversarial-class. They are proposed as threat-model exclusions, not as defects fixed, and their final recorded status belongs to the operator.
- **Review route:** kickoff preflight, then a plan that presents decision 1 with both routes and a recommendation. Its one Fable High plan review is the architectural review. The operator rules decision 1 through the planning input-park route, informed by that review; the ruling is recorded in this phase file. The contract and namespace brief amendments are phase deliverables reviewed in the code critique, so the run does not declare them as fixed authorities (lesson strict-kudu). A second plan pass is used only for a recorded cause. Corrected 2026-11-12: kickoff requires cited briefs to be declared authorities, so the governing-prose route applies instead. The ruling and the brief amendments are written as preparation, that run parks, and a fresh run captures them before implementation. The one code critique still reviews the implementation against them.
- **Greenfield rule:** applies; v2 is unreleased.
- **Review allowance:** one registered Fable High plan review and one registered Fable High code critique under this phase's own preflight. Phase 12's spent passes and history are not reset or reused.
- **Public records:** generic terminology only.
