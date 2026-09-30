---
title: "Castle — Product Brief"
date: 2026-08-21
status: implemented
scope: Entry-point brief for this repository. Describes what castle is, the sibling-workspace context it is born into, the binding CAS contract delivered as `cas-api.md`, the scope boundary that holds implementation to that contract, and the completed Castle-owned CAS surface.
---

# Castle — Product Brief

*A standalone content-addressable snapshot store: a Python library plus a `castle` CLI.*

## Thesis

Castle is a content-addressable snapshot store extracted into its own repository so that it can be developed, tested, versioned, and consumed independently of the projects that use it. It ships as a Python library (`castle`) with a thin CLI (`castle`) over the same API. The repository exists with full engine discipline (plan ledger, policies, lessons ledger, briefs, atomic `bin/setup` / `bin/test` / `bin/check` toolchain), so implementation proceeds under the methodology from day one instead of retrofitting structure later.

## Scope boundary (load-bearing)

**The CAS contract has been delivered. [`cas-api.md`](cas-api.md) is binding, and implementation may not exceed it.**

- **[`cas-api.md`](cas-api.md) is the authority.** It defines the standalone delivered storage behavior. It fixes module ownership, strict journal-v2 semantics, object and index behavior, copy-on-write merge-back, once-only v1 migration, the CLI boundary, and the test obligations.
- **Castle does not invent beyond that contract.** No phase here may add an API surface, a schema, a command, or a concurrency semantic the contract does not specify. Where the contract is silent, the answer is to ask upstream, not to improvise: an invented-here surface either constrains the ratified design or gets thrown away, and both outcomes are waste.
- **Castle stays self-contained.** Nothing under `project/` imports consumer code or reads a consumer workspace manifest, or reaches outside this repository's deliverable. Castle accepts a resolved CAS member root; the consuming workspace decides which root is authorized before calling in.

This boundary is restated in `CLAUDE.md` § Project Context so every session sees it before acting.

## Independent consumer boundary

Castle supplies generic content-addressable storage. Consumers retain workspace composition, manifest resolution, declared store selection and lifecycle orchestration. Changes to their dependencies and live data remain separately authorized consumer work.

## Current state: Castle-owned CAS contract implemented

- **The contract is here and is being held to.** [`cas-api.md`](cas-api.md) is landed and binding.
- **The Castle-owned surface is complete.** Phase 1 delivered `castle.journal`, `castle.cas`, and the core CLI. Phase 2 delivered APFS copy-on-write cloning, CRDT-union merge, the isolated once-only `castle.journal_migrate_v1` reader, and the guarded `journal-migrate-v1` command. Phase 2.1's operator walkthrough was accepted on 2026-08-22; Phase 2.2's recorded migration success and refusal behavior was accepted on 2026-08-23. See [`crdt-semantics.md`](crdt-semantics.md) for the merge guarantees and limits.
- **Engine discipline.** The full methodology contract: `plan/`, `policies/`, `briefs/`, `lessons/`, `user-actions/`, `LOG.md`, the four canonical agents, the universal skills (`kickoff`, `methodology`, `learn`, `teach`, `roles`, `sweep`), cross-harness mirrors, and the orchestration-evidence and execution-telemetry machinery.
- **An atomic toolchain contract.** `./bin/setup`, `./bin/test`, `./bin/check all`, candidate-bound full-gate receipts through `./bin/check-receipt`, and `./bin/python` over a uv-managed Python 3.12 environment (`project/.python-version`, `project/pyproject.toml`, `project/uv.lock`).

## Acceptance criteria

- `./bin/setup` provisions the locked Python 3.12 environment from a fresh clone.
- `./bin/check all` passes: lint, format, deliverable tests, root methodology tests, and policy gates.
- `uv run --locked --managed-python castle --help` exits 0 (run inside `project/`).
- [`cas-api.md`](cas-api.md) is present and cataloged in `CLAUDE.md`.
- The scope boundary above appears in both this brief and `CLAUDE.md` § Project Context.

The Castle-owned contract in [`cas-api.md`](cas-api.md) is fully delivered. Later consumer cutover remains the consumer maintainer’s work and does not reopen this brief.

## Catalog

See [`CLAUDE.md`](../CLAUDE.md) for the index of sibling briefs and the policies catalog.
