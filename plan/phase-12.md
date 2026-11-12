---
id: 12
title: Touched-record namespace admission
depends_on: [phase-11.md]
informs: []
review_lane: full
---

# Phase 12 — Touched-record namespace admission

## Goal

Make ordinary namespace open, admission, reads and metadata commits cost O(1) or O(n) in the directly affected records instead of O(N) in the whole namespace and CAS. Every touched record is still validated against current bytes, and detected corruption fails closed. Operator principle (2026-11-11): "O(N) operations like 'validate everything' should be for deep tests and diagnostic/repair tools. Normal business should as much as possible amortize to O(1) or O(n). N = all elements in set, n = directly affected elements in set." The operator ruled on 2026-11-11 that normal operations validate only the records they touch, and that damage elsewhere is detected when it is accessed, at whole-sequence operations, or by full diagnosis, rather than blocking unrelated work immediately.

## Deliverables

- **Admission without whole-set rework.** Admission keeps every constant-cost check of 0.3.0: store identity, locks, generation and CAS epoch, replacement epoch intervals and marker state, protection, service state and pending-operation state. It no longer re-validates every entry, operation and dependency, or folds the whole CAS journal.
- **Validation of touched records.**
  - Each entry that is read or mutated is validated on access. Its ancestry is validated where the tree contract requires it: live and non-live rules differ.
  - Each touched content object's authority records are verified against the current journal bytes, using a derived per-object location table (offset, length and digest of each record line). The table is bound atomically to the CAS position it reflects.
  - An outside append is indexed by parsing only the appended bytes. A record that does not start on a line boundary, a digest mismatch or an unknown position triggers full revalidation.
  - Touched corruption fails closed.
- **Indexed reads and incremental commit.**
  - Reads and commits replace whole-namespace scans with indexed lookups and tracked changes.
  - Tree-wide limits and dependency rows are maintained incrementally.
  - Whole-set work stays only in explicit diagnosis, recovery and the operations that already require the whole sequence: publication, snapshot capture, excision, merge and index rebuild.
- **Minimal read surface:**
  - a revision probe with a logical store position, which is not a claim about prefix bytes;
  - a targeted entry read;
  - a directory page;
  - change discovery that includes deletions;
  - an explicit full diagnosis that reports the exact whole-journal length and SHA-256.
- **Format change and migration.** One namespace format change with a single non-destructive migration, tested on synthetic stores, that keeps an operator-held backup. No live data is migrated.
- **Truthful contract text.** The briefs state what moved from per-admission validation to on-touch, whole-sequence or diagnosis detection, and when each kind of damage is detected.

## Carried obligations

These come from Phase 11's two reviews and its analysis. They are unresolved until each is dispositioned here with a test or a named residual. Closing Phase 11 resolved none of them.

Native design review, high findings:
- **F001, F013:** stamp-based detection of outside writers and in-place changes is unsound. No trust may rest on file stamps.
- **F002:** timestamp granularity cannot be queried. No correctness may depend on it.
- **F003:** checkpoint self-reference: a fast path must be reachable after its own writes.
- **F004, F005:** CAS root, index sidecars and object fan-out cannot be stamped, cheaply or soundly. Per-admission object-inventory cost must not be O(objects).
- **F006:** content publication cost belongs to Phase 13. It is stated here only as the boundary.
- **F007:** legitimate outside ingest and merge must not latch a live handle or force whole-set work at the next open.
- **F008:** interrupted-completion and recovery routing must be kept on the fast open path.
- **F009:** dependency rows feeding an excision closure must be revalidated wherever excision relies on them.
- **F010:** no new root file may make replacement inventories or recovery refuse.
- **F011:** the migration must work with the delivered replacement marker and generation rules, and must not discard stored values.

Qualifying plan review:
- **PLAN-F001:** keep NORMAL locking with DELETE journal semantics. The reader and cutover journal-presence checks are unchanged.
- **PLAN-F003:** read-only handles must obtain the touched-record benefit without writing.
- **PLAN-F004:** the namespace-row checks and replacement epoch computation run on every open and admission. A missing epoch interval is a refusal, never a default.
- **PLAN-F005:** state exactly which open paths hash object bytes, and list every moved check.

Also carried:
- The withdrawn Phase 11 claims stay withdrawn: no resumable whole-prefix hash, and no classification of changes by modification time.
- No stamp, chain or tail witness may be treated as proof of an unchanged mutable prefix.

## Acceptance

Executable:
- **Cost tests.** Focused tests prove each ordinary path's cost against N by counting validations and record reads, not by wall time.
- **Damage tests.** Corruption-injection tests show that touched damage fails closed at access. Untouched damage is found on touch, at a whole-sequence operation, or by diagnosis.
- **Prefix and fallback tests.** Tests cover outside appends, same-length prefix rewrites, framing breaks, outside ingest and merge, and fallback to full revalidation.
- **Migration test.** A synthetic migration test covers the round trip, backup preservation and refusal on an existing, differing backup.
- **Carried obligations.** Every obligation above has a recorded disposition.
- **Gates.** The phase-specific checks run, then the full `./bin/check all` gate.

Manual: architectural acceptance of the moved-detection statement remains the operator's.

## Bounds

- **Out of scope:** CAS format changes. Content publication stays at today's cost; it is Phase 13.
- **Not authorized:** live stores, release, tag, push or public publication.
- **Greenfield rule:** applies, except for the single migration boundary named above.
- **Review allowance:** exactly one registered Fable High plan review and one registered Fable High code critique, under this phase's preflight. There is no automatic extra pass, and Phase 11's review history is not reset.
- **Public records:** generic terminology only.

User Demo: to be written in the plan. It is an operator-runnable comparison of validation counts against 0.3.0 on a synthetic namespace.

## Brief refs

- [Coordinated namespace contract](../briefs/coordinated-namespace-crud.md): authority, admission, tree validation, recovery, transactions, replacement, excision and read-only access.
- [Binding CAS contract](../briefs/cas-api.md): journal, index and inventory validation.
