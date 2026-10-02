# Activity Log

This public repository begins with the reviewed Castle 0.1.0 source and methodology. Earlier private execution records are excluded.

## 2026-09-30 01:09 — Compact persisted identifiers

The maintainer authorized compact record discriminators: `journal.v2`, `migration.v1` and `clone-order.v1`. Runtime remains single-schema; immutable object identities and unrelated lifecycle schemas are unchanged. Existing stores require separately verified private offline conversion and coordinated dependency updates.

Validation: 56 focused package tests and the first complete 196-test repository gate passed. A private converter passed 14 synthetic preservation, refusal, interruption, repeat and rollback tests, including abrupt process termination around atomic publication. Independent critique identified staging and durability gaps; corrections keep staging outside authority and durably publish backup and resumed conversion directories. Live-data cutover and fresh artifact/Linux qualification remain separate evidence.

Lessons: source-bound representation conversion must preserve non-journaled remote metadata and verify crash durability on already-published retry paths. Private conversion evidence and protected copies are excluded from public artifacts. Human custody and publication judgment remain with the maintainer.

## 2026-09-30 01:13 — Public proof-label redaction

Public proof-label redaction initially left the baseline serialization digest stale; the full gate refused it. The corrected public inventory digest and structural ledger references now bind the redacted serialization. Historical oracle text and effectiveness observations retain their original meaning, and original input files are preserved privately. Eleven focused governance tests passed after correction. This is publication redaction, not a new historical proof run.

Lessons: changing even a display label inside a digest-bound inventory requires rebinding its structural references and recording the distinction from measured historical evidence.

## 2026-09-30 01:21 — METHODOLOGY — Public package qualification

Scope: Complete approved public0.1.0 package and contributor qualification after compact identifier handoff.
Changes: Preserve excluded operator files under ignored.private-development during normal checkout alignment; normalize three inherited extraEOF blank lines without changing license text; add isolated exactwheel CLI/journal.v2 smoke to both hosted platforms.
Verification: Migration handoff passed196tests andallpolicygates; public oldnamespace lexicalscan clean. Exactartifact validation and final systemBash/full/hosted gates remain required; no live store/client migration is performed here.

## 2026-10-01 22:10 — TAUGHT FROM TEMPLATE

Source: agentic starter template @ 147d5fc. Scope: one correction, taught on the operator's instruction of 2026-10-01 to carry it to every repository holding a hand-added usage mapping. Items applied: 1, by tier T1=0/T2=1/T3=0/T4=0. Parity heals applied: 0 (AUTO); 0 surfaced as DECIDE. Stale-in-light-of-teaching migrations: 1 (AUTO); 0 DECIDE; 0 DEFER. Unharvested methodology lessons surfaced for `learn`: not assessed in this narrow pass. Files touched: 5.

**What changed.** `lib/agentic_starter/workflow.py` now applies an additional usage group that names the model it meters to a target of that model and to no other; a group that names no model still refuses as ambiguous. `tests/test_kickoff_config.py` proves the three cases, and `policies/role-models.md` states the rule. The one provider group reported today is a fallback allowance for a single named model that is not a review target here (retrieved 2026-10-01).

**Stale migration.** `kickoff.yaml` carried a custom target that restated the built-in one only to add a usage mapping. That mapping excluded every additional group by an explicit mapping, which also silenced the refusal for any group a provider adds later. The custom target is removed, so the built-in target and the new rule apply. No model, effort, timeout or budget value changed.

**Checks.** Configuration validation, the configuration tests and the new rule against the live usage reading passed before this record. Independent review is not applicable because this is primary one-shot methodology work. The one full gate follows this record.

## 2026-10-02 08:38 — END (correction)

CAS publication — preserve bytes after uncertain journal append

An append that becomes visible before its synchronization fails no longer causes ingest cleanup to delete the new object. Failed ingest returns no receipt. Subsequent writes require consistent journal, index and inventory plus a successful journal checkpoint. Complete valid append evidence can be reconciled through synchronized index reconstruction; incomplete tails and orphan objects remain preserved and refused.

Follow-up route:
- Full cycle — this correction changes publication ordering, failure recovery and lock lifetime. Primary planning/coding with independent plan and code advice; no historical phase or pending namespace status changes.

Role model/venue:
- Primary: inline Codex implementation.
- Plan adviser and code critic: requested fresh native Astra instances, high effort. Native preflight: N/A. Private-source review stayed in the authorized native venue; cross-provider Claude advice was not run. Provider-reported model and effort: unreported. Exact orchestration timing: unavailable.
- Code advice used two passes. CODE-F001 identified immutable-file cleanup; native failure injection confirmed it, and cleanup now clears only the operation-owned file's user-immutable flag before deletion. CODE-F002 identified cleanup after lock release; a disposable cooperating-merge reproduction confirmed missing authoritative bytes, and cleanup now finishes under the original stable lock. Both findings were resolved through primary assessment and independent-process controls; no third advisory pass was requested.

Files changed:
- `project/castle/cas.py` — retain objects once append is attempted; admit and synchronize consistent authority before staging; synchronize the current journal inode before cache reconstruction; finish safe preappend cleanup under the lock.
- `project/tests/test_cas.py` — extend the existing publication proof with real journal-descriptor ENOSPC/EIO, zero/partial writes, buffered flush failures, duplicate sightings, abrupt process exit, remote metadata/provenance preservation, missing-byte negatives, native immutable-file failures and a competing-process lock check.
- `briefs/cas-api.md` — state uncertainty, recovery, cleanup and verification limits and the per-file admission cost.
- `LOG.md` — append this correction record.

Build status:
- Fail-first controls reproduced deleted bytes after journal fsync failure, immutable cleanup masking the original I/O error, and lock release before deletion. Each control passed after its corresponding correction.
- macOS implementation gate `./bin/check all`: OK — 196 tests, lint, format and every policy check; unchanged candidate `26e310ea18cfc7187253bf63af50a943ba43e8a79b1515ea1de8a6f06092e1c5`.
- Linux affected tests: 73 passed, 6 macOS native clone/merge cases skipped. The final publication proof, including the competing-process cleanup-lock check, passed separately on Linux using the repository's documented containment-checked runtime override.
- Handoff gate: runs after this tracked correction block; completion is contingent on the final bare `./bin/check all` receipt.

Acceptance:
- Objective: preservation, refusal without receipts, synchronized recovery, dedup provenance and metadata, safe cleanup and cooperating-lock lifetime. Reviewed implementation resolutions are bounded to the approved repair; complete gate evidence is required before local delivery.
- Parked for the user: None for this correction. Synthetic I/O injection and process exit do not qualify actual disk exhaustion, power loss, x86 Linux, or every storage medium.

Delivery:
- Restricted: the user answered "Go ahead" to "May I implement and test the preservation-first repair in Castle, with local commits only?" Local commit after the handoff gate; no push or release.

Lessons:
- None newly filed; the approved repair's failure and recovery semantics are recorded in their binding contract.

Remaining:
- Partial tails and orphan objects still require separately authorized recovery; no automatic truncation, deletion or adoption was added. Index reconstruction cannot restore missing source bytes. Full journal/index/inventory admission runs per file and serializes staging; this repair introduces no persisted recovery state or performance optimization.
