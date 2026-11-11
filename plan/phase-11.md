---
id: 11
title: Incremental admission design review
depends_on: [phase-10.md]
informs: []
review_lane: full
---

# Phase 11 — Incremental admission design review

## Goal

Produce an independently reviewed, non-destructive design that lets ordinary namespace open, admission, reads and commits cost O(1) or O(n) in the directly affected records, while full validation stays an explicit diagnosis and recovery path. Operator principle (2026-11-11): "O(N) operations like 'validate everything' should be for deep tests and diagnostic/repair tools. Normal business should as much as possible amortize to O(1) or O(n). N = all elements in set, n = directly affected elements in set."

## Deliverables

- A design, captured as this phase's plan artifact, covering: admission that keeps today's constant-cost identity, lock, epoch, protection, service-state and pending-operation checks; validation of touched records and their ancestry; restart and admission when earlier observations are absent or stale; concurrent same-size modification; cross-platform timestamp semantics; replacement and excision interaction; incremental commit, including tree-wide bounds and dependency maintenance; and the minimal public read surface (revision probe with store position, targeted entry read, directory page, change discovery including deletions, explicit diagnosis).
- An exact statement of what 0.3.0 validation actually protects, and of what the design preserves, defers to touch or diagnosis, or does not provide. No cryptographic guarantee that the library does not have may be claimed.
- Non-destructive format compatibility and migration options under the release posture in `policies/greenfield-until-released.md`.
- Recorded dispositions of independent Fable High advisory review findings, and a minimal scoped implementation proposal for a later, separately authorized phase.

## Acceptance

The design states, for each ordinary path, its cost in terms of touched records n, total records N and CAS events, with source-backed evidence, and identifies every remaining whole-set path as explicit diagnosis, recovery or an explicit O(N) operation. Independent advice is ingested with dispositions; adviser agreement alone is not architectural acceptance, which stays with the operator. The phase ends parked for an operator implementation decision: no runtime implementation is produced or claimed.

## Bounds

Design and review records only. No runtime source, test, schema, migration, storage-format, release, tag, push or publication changes. No live stores. Owner-led, root-authorized on 2026-11-11, with Opus as primary and Fable High as the independent reviewer, under the operator's explicit exception for large architectural changes. Review allowance: the non-qualifying native review launched at 2026-11-11T18:16:25Z counts as the first pass; at most one registered qualifying pass follows, with recorded cause; there is no third attempt. Avoid heavy test gates during other projects' bounded qualification slots. Public records use generic terminology only.

User Demo: N/A for a design-only phase; implementation and architectural acceptance remain operator decisions.

## Brief refs

- [Coordinated namespace contract](../briefs/coordinated-namespace-crud.md), sections 2, 4, 6, 15–18, 22 and 23: authority, admission, tree validation, recovery, transactions, replacement and read-only access.
- [Binding CAS contract](../briefs/cas-api.md): journal, index and inventory validation, and the bounded publication extension.
