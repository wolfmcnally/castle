# Sourced proof-estate evidence

The baseline, disposition ledger and effectiveness reports were carried forward from the same Castle retained test estate. They contain synthetic proof identifiers, technical mutation results and hashes, not new observations of this public checkout. They preserve the existing count ceilings and evidence contract; no historical disposition was invented. Run `./bin/test-governance validate` to verify the sourced records, and assay/reassess for changes that require fresh evidence.

## Public-tree runtime reassessment

After standalone runtime and copied-assay environment adaptation, the unchanged frozen corpus was executed again against disposable copies of this public tree. All controls passed; 11 of 12 historical defects and all 12 held-out mutants were detected. The existing `required-gate-inventory` miss remains visible. These are new executions of the same frozen corpus, not a new unseen holdout. `castle-effectiveness.jsonl` contains the completed manager output unchanged. The baseline and historical disposition ledger remain sourced evidence.

## Ledger mechanics

This directory records the recipient's own proof-estate reset. The frozen
pre-reset baseline is immutable. The append-only ledger records baseline dispositions, admissions, repairs, modifications and retirements. Bounded adoption keeps undispositioned baseline proofs active and reports them as pending review; completed resets remain complete. Modification events link one-based fresh witness receipt numbers; independent review checks their assertion-level relevance. Replaying the
ledger must reproduce the live inventory exactly; a missing event, repeated
retirement, or shadow proof fails validation. The witness ledger (`castle-witnesses.jsonl`) holds one receipt per
observed red witness: the proofs, the named defect, the command, the text its failure
had to contain, the files the defect was planted in, digests of the planted bytes and of
the failing output, and the date. Receipts are appended only by
`./bin/test-governance witness finish` and `./bin/test-governance admit`; no planted defect is stored here (the defect each proof was last witnessed with is kept in `tests/proof-plants.json`). Three historic gate admissions that predate receipts and assert this estate's own validity are named in the manifest's digest-bound `witness_history` and reported as unwitnessed history. Per-test timings are machine-local and live in the
ignored `.kickoff/test-timing/` record, never here.

These files are evidence, not portable judgments. A stamped, taught, or learning
recipient regenerates them from its own estate and never copies survivors,
selectors, witness receipts, timings, risk applicability, or dispositions.

The executable authority is:

```bash
./bin/test-governance validate
./bin/test-governance report
./bin/test-governance reassess
./bin/test-governance assay --class historical_defect
./bin/test-governance assay --class holdout_mutant
```

Routine vital and changed lanes never replace the full handoff gate. The frozen reset summary remains a historical snapshot; use `reassess` for current totals and for the count of proofs that still have no receipt.

A witness plants its defect in the live tree. While one is pending, `validate` refuses, so neither the commit hook nor the full gate can pass a planted defect; `./bin/test-governance witness abort` restores an interrupted run. To witness a proof of the governance manager itself, run the command from an untouched copy of the manager and its test_runner module with `--root` pointing here, so the planted defect cannot change how the observation is made. When challenging test_runner itself, run pytest through the repository interpreter under the untouched observer rather than through the runner being challenged; otherwise a planted exit-status defect can mask the expected failure.
