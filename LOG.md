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

## 2026-10-02 15:18 — METHODOLOGY SCOPE
Rule One after an unauthorized review substitution.

Approved outcome: apply the user's Rule One correction to the omitted configured subscription review. Record the causal diagnosis on the shared lesson ledger and strengthen the existing workflow proof that rejects native and unselected-model reviewers.

Surfaces in scope: `lessons/jasmine-lyrebird.md`, the retained complete synthetic kickoff proof family in `tests/test_kickoff_evidence.py`, and append-only close records. The proof extension was already written and its focused check passed before this scope record; this record does not backdate that work.

Explicit exclusions: no new routing policy, role defaults, provider fallback, proof family, permission weakening, phase-status changes, or push. The separate source-review correction retains its product scope.

## 2026-10-02 15:18 — PARK
Publication repair review: configured source review complete; contract clarification requires fresh authority capture.

Execution trace: 566aa007306747d3b8f45569b7749760

The configured subscription reviewer completed a read-only review of the exact local repair. It reported no evidenced break in the approved preservation behavior. Its findings exposed missing failure-boundary proof and recovery/durability guidance that must be clarified in the captured CAS contract. This preparation run does not claim accepted implementation or final gates.

Preserved evidence: the registered independent review, its source manifest, immutable dispatch identities and full report remain in the run evidence. No credential, private record, live store, or unrelated source was in the packet. The publication proof passed after the localized recovery-message correction; the retained workflow proof also passed.

Resume condition: capture the clarified contract in a fresh primary follow-up run, carry the exact original advice and pass budget, reassess the final delta, and run the required close gates. The user already authorized in-scope review corrections and local commits; no further user decision is required for this continuation. Phase statuses remain unchanged.

Lessons:
- Filed `jasmine-lyrebird`: the substitution bypassed an adequate guarded workflow and misread standing authorization. The retained workflow proof rejects both substitutions without adding a new proof family. No policy graduation is applied.

## 2026-10-02 15:29 — PARK
Publication repair review correction: acceptance stopped at lint.

Execution trace: 45d53b39f41b47c7be24407a1f9c773f

The full candidate gate stopped at one line-length error in the new recovery message before the test suite ran. The source review and focused behavioral proofs remain preserved; this run claims no accepted close or successful full gate.

Diagnosis: formatting preserves long string literals, so the formatted diagnostic still exceeded the declared lint width. The localized correction splits adjacent string literals with identical emitted bytes. Resume is permitted under the existing novel, diagnosed self-resume budget; one of three automatic corrective continuations is consumed. No provider retry or user approval is needed.

Lessons:
- No new reusable lesson: this is an ordinary lint defect caught by the required gate before expensive work. The existing shared review-routing lesson remains `jasmine-lyrebird`; its successful workflow proof is preserved.

Resume condition: apply the byte-equivalent string wrapping, pass focused lint, capture the unchanged authorities in a fresh run, carry original configured advice, reassess the delta and repeat the full acceptance gate. Local commits only; no phase-status or store changes.

## 2026-10-02 15:34 — PARK
Publication repair review correction: full gate exposed a caller-environment error.

Execution trace: 54210b68f76b470eb3ef5d02972e674d

Lint and formatting passed; 195 tests passed and the retained workflow fixture failed when its stub watcher could not find its trace. The gate inherited the task launcher's engine-root override, so a child running in a deliberately isolated fixture repository still selected the real repository. The same focused family previously passed without that override. This is an execution-setup defect; it is not a failed independent review, a provider fallback or a product-code finding.

Correction: remove the unnecessary override from the gate process, run the pinned tool from the actual repository, and keep explicit repo-root arguments for telemetry. No product, fixture, permission or validation code changes are required. The novel signature is diagnosed; two of three automatic corrective continuations are now consumed, with one remaining.

Lessons:
- Filed `bronze-tench`: scope explicit tool-root overrides to the process that needs them; do not leak parent authority into isolated fixture children.
- Existing `jasmine-lyrebird` retains the unauthorized review substitution diagnosis and its verified workflow-proof correction.

Resume condition: fresh capture against unchanged governing prose, carry the exact original configured source advice, remove the gate environment override, and pass the complete acceptance and handoff gates. Local commits only; no phase-status or store changes. No accepted close or successful full gate is claimed here.

## 2026-10-02 15:40 — END (correction)
Publication repair: configured subscription review completed and its scoped findings corrected.

Execution trace: 53a74144f3ee414b84420bb2ba9f1f13

The missing configured independent review is now complete. The reviewer inspected the exact local repair commit `c2f1f484228cb1b684b3c0d4f31e5762360240cd` through the authorized subscription using an allowlisted source packet. Its report found no evidenced breach of the approved preservation requirements. The primary adopted the three material findings: a complete-record/no-newline proof, limits on later synchronization success, and recovery guidance that distinguishes index drift from object-inventory states that rebuilding cannot resolve. All nine advice items have explicit dispositions; automatic recovery redesign remains outside this correction.

Follow-up route:
- Configured source review retained primary coding authority and one independent advisory pass. Its governing-prose preparation was truthfully parked before fresh capture.
- Direct fix for the localized diagnostic, documentation and retained-proof corrections. The final primary decision assesses the delta against the carried original advice; no second review of the changed candidate is claimed.

Role model/venue:
- Planner and coder: primary inline; no fabricated delegated production roles.
- Critic: requested model=opus effort=high venue=claude, subscription-authenticated preflight and registered read-only dispatch passed. Observed model=claude-opus-5-5, harness_version=2.1.287, observed effort=unreported. Terminal stream succeeded, fresh structured result present, child exit 0; no fallback or protocol recovery.
- Source packet hashes were verified before and after review. Observed source reads stayed within the allowlisted packet; filename-only discovery preceded the reads. No credential, private record, live store, unrelated personal data, or consumer source was included.

Files changed:
- `project/castle/cas.py` — truthful inventory recovery message, remove redundant staging conditional, clarify current-inode synchronization comment.
- `project/tests/test_cas.py` — strengthen the retained publication family with complete JSON/newline failure, specific refusal messages, preserved cache bytes, missing index, staging residue, actual staging-lock exclusion and current-journal-inode sync; restore host spies before additional scenarios.
- `briefs/cas-api.md` — clarify operator recovery boundaries, visible receipt versus synchronization evidence, Linux write-back limits, concurrent reader checkpoint refusal and scan/staging cost.
- `tests/test_kickoff_evidence.py` — extend the existing complete synthetic workflow family with native/unselected-model primary-review refusals, no registration side effects, and subsequent valid provider path.
- `lessons/jasmine-lyrebird.md` — durable Rule One diagnosis of the unauthorized review substitution.
- `lessons/bronze-tench.md` — scope task-launch engine-root overrides to the process that needs them.
- `EXECUTION_LOG.jsonl` and `LOG.md` — privacy-projected real execution evidence and append-only correction records.

Build status:
- Complete publication proof passed; an isolated in-process removal of rebuild's appendability check failed at the complete-record/no-newline assertion with DID NOT RAISE. No repository mutation was used for that falsifier.
- Complete synthetic workflow proof passed directly and in the full gate. Both forbidden registrations refuse before handoff or ledger mutation; the configured independent path remains valid.
- Real last-reader checkpoint probe on a temporary store reproduced a transient admission refusal, preserved journal authority, verified healthy afterward, and succeeded on retry.
- Candidate-bound `./bin/check all`: PASS, 196 tests in 133.99 seconds, lint/format/all policy checks passed, zero warnings; full-tree and product identities unchanged across the gate.
- `kickoff-evidence validate --level acceptance --required-final-command "./bin/check all"`: PASS. Exact execution timing finalized and validated; final-run makespan 241.125248042 seconds, managed full gate 136.702882666 seconds. The final continuation contains no new intelligence span because its source advice is carried with original identity.
- Prior lint and gate-environment failures are preserved as truthful PARK records. The environment correction removed an unnecessary task-launch override; product and fixture code were not changed for it.
- Handoff gate: pending after these tracked close records; completion and local delivery depend on the ignored receipt from the final bare `./bin/check all`.

Acceptance:
- Objective: exact configured source review completed; approved preservation requirements checked; strengthened retained proofs and scoped corrections passed the full candidate gate; Rule One diagnosis persisted and ledger schema validated.
- Parked for the user: none for this bounded correction. A persistent uncertain-sync recovery protocol, receipt gating, and automatic SQLite snapshot retry remain future design scope rather than delivered behavior.

Delivery:
- restricted: local commits only; no push.

Lessons:
- `jasmine-lyrebird` and `bronze-tench` filed as methodology candidates, one occurrence each; no policy graduation or routing/configuration weakening. `bin/lessons validate` passed; no graduation-ready lesson.
- `kickoff-config recommend-timeouts` reported no target with the required 30 successful samples and ignored older malformed telemetry rows. No timeout setting was changed.

Remaining:
- Final handoff gate and local commit remain pending at this record; their outcomes are reported after execution without a later tracked write.
- The later successful fsync, rebuilt counts, verification and reconstructed receipts do not prove earlier failed write-back became durable. Original-error callers keep that ingest unconfirmed; no new recovery lifecycle was introduced.
- This standalone correction does not change a roadmap phase or generate a numeric phase dashboard. Structured execution evidence and this append-only record carry its outcome; no phase is invented for presentation.
- Test protocol: run `./bin/test project/tests/test_cas.py -k publication_is_atomic` to exercise the preservation and recovery boundaries on temporary stores. No live-store operation is required.

## 2026-10-02 15:40 — METHODOLOGY
Rule One applied to the unauthorized review substitution.

The exact Rule One question is: "What can I remember to do differently next time so this doesn't happen again?" The visible omission was a symptom; the earliest supported divergence was my failure to resolve and follow the primary workflow before choosing reviewers, compounded by misreading the user's standing source-sharing authorization. Real subscription preflight and the completed configured review contradict provider unavailability as the cause. The existing rules and guarded registration already reject native and unselected-model substitutions, so no duplicate routing policy was added.

Persisted learning: `lessons/jasmine-lyrebird.md` records the diagnosis and the actionable change: load the frozen primary selection, honor standing authorization, sanitize relevant source, and enter registered guarded dispatch. The retained complete synthetic proof in `tests/test_kickoff_evidence.py` now rejects both substitutions without creating a handoff or modifying the attempt ledger, then proves the configured independent path. This is primary one-shot methodology work; it has no separately commissioned independent review or new proof family. The independent configured product source review is preserved separately.

Verification: the workflow family passed directly and in the full candidate gate. `bin/lessons validate` passed. `bin/test-governance validate` remains valid at 163 families and 218 executable leaves, with zero unspent retirement budget; no family or leaf growth. No graduation, permission change, fallback, phase-status edit, or push occurred. The diagnosed launcher-environment failure also yielded `lessons/bronze-tench.md`; its correction was verified by the successful 196-test full run.

Remaining human criteria: none for this authorized correction. Candidate lessons remain provisional. Final handoff and local commit follow this record; no later tracked write will claim their outcomes.

## 2026-10-02 16:13 — END (correction)
Castle 0.1.1 patch release candidate prepared and locally qualified.

Execution trace: fd5e4ca48c854ecc8f0ab74ca45c837e

The operator explicitly authorized publishing patch release 0.1.1: "Do the patch release." This supersedes the preservation repair's prior local-only hold for this release. Normal fast-forward delivery, a new patch tag and GitHub Release are authorized; no historical tag or history rewrite, registry publication, live-store operation, consumer pin change or namespace implementation is included.

Follow-up route:
- Direct fix for mechanical release metadata, lockfile and CI version expectations, with accurate patch and durability notes. The independently reviewed runtime repair and tests remain byte-identical. Original configured Claude subscription advice and all nine dispositions are carried with their original provenance; no new advisory invocation is claimed.
- Full candidate-bound evidence and both full close gates remain mandatory. No historical roadmap status or numeric dashboard is changed by this standalone release correction.

Files changed:
- `project/pyproject.toml`, `project/uv.lock`, `project/castle/__init__.py` — consistent 0.1.1 version; dependency pins retained and locked check passed.
- `.github/workflows/ci.yml`, `.github/scripts/package-smoke.py` — exact 0.1.1 wheel and installed-version expectations.
- `project/CHANGELOG.md`, `project/RELEASE.md`, `project/README.md`, `README.md`, `SECURITY.md` — patch scope, publication-status guidance and accurate uncertainty/recovery limits.
- `EXECUTION_LOG.jsonl`, `LOG.md` — sanitized real acceptance evidence and append-only release authorization/qualification record.

Build status:
- Candidate-bound `./bin/check all`: PASS, 196 tests in 142.97 seconds; lint, formatting and every policy check passed with zero warnings. Full-tree identity b0f9fa6582791beb6dae79aaace046cf98be5effa16a0dc93cc92e85f80f3ec0 and product identity 7424e3afcf5934bb60e5d04a7239873a9ee513789db9fdd9ef47a0a4883b3900 stayed unchanged across the gate.
- Gate 34a666afcb8c452709762b22e9b9a78255d0a5fe28a37b70950bb89af5a94e47: complete diagnostics reviewed. Acceptance validation and finalized timing-summary validation passed.
- Wheel and source archive built with uv 0.9.26, pinned hatchling 1.32.0 and managed Python 3.12.12. Both include MIT license; metadata is 0.1.1 and the wheel declares no runtime dependencies. Archive source bytes match the candidate. Wheel inventory contains only package modules, metadata and license; source archive contains standalone package material and synthetic tests, with no repository planning, execution evidence, stores or private inputs.
- Exact wheel installed without dependencies outside the checkout on native macOS arm64 and Linux arm64. Import containment, version, CLI help/init/ingest/stat/verify/rebuild-index/verify and journal.v2 smoke passed on both. Linux uv copied files after a cross-filesystem hardlink warning; this did not affect installed contents or smoke.
- Inspected wheel SHA-256: 61c980285dd8e067bcb99d40addd08c9297c0da37a9316cacf83460532d9f3b6.
- Inspected source archive SHA-256: be7c24b234162a6674c702fc742b5f8628d887b17ec2187c78e1ea3dd9982259.
- Public-scope audit covered the five unpublished commits and their 28 changed blobs/additions, tracked path inventory and release delta: no private tracked paths, credential-pattern findings or private-source references in additions. Manual scope review confirms proposed namespace documents and sanitized methodology records are public checkout material; package archives exclude them.
- Installed repository hooks verified. Handoff gate remains pending after this tracked record; delivery requires the final bare `./bin/check all`. The exact committed candidate must also pass hosted macOS/Linux full gates and clean-wheel smoke before tag/publication.

Acceptance:
- Objective: prepared version consistency, accurate patch scope, preserved API/format/platform scope, inspected artifact identities/license/source bytes, disposable exact-wheel platform smoke and complete candidate gate.
- Parked for the user: none for this explicitly authorized patch publication. Power-loss recovery and proof that an earlier failed write-back became durable remain outside delivered claims.

Delivery:
- Authorized by "Do the patch release." Commit and normal fast-forward push after handoff, then require exact-commit hosted CI before new v0.1.1 tag and GitHub Release. Publish only the inspected wheel, source archive and SHA256SUMS, then download and verify the published bytes and installation. No PyPI publication or unrelated repository mutation.

Lessons:
- None newly filed; the existing configured-review and environment-scope lessons remain applicable and were followed. Lesson schema passed. Timeout recommendation had no target with the required 30 successful samples and reported ignored historical malformed rows; no timeout configuration was changed.

Remaining:
- Final bare handoff gate, exact commit, push, hosted CI, new tag, publication and download/install verification occur after this record and are reported from observed results without later tracked writes.
- An original ingest failure remains unconfirmed after later successful synchronization, verification or receipt reconstruction. No uncertain-write protocol, automatic partial-tail/orphan recovery, schema/API change or namespace feature is delivered.
- Test protocol: the release wheel's `castle --version` must report `castle 0.1.1`; the existing isolated package smoke verifies the CLI and journal format on disposable data. No live store is required.
