---
id: 7
title: Consumer surface and namespace qualification
depends_on: [phase-6.md]
informs: []
---

# Phase 7 — Consumer surface and namespace qualification

## Goal

Make the completed library usable through Castle's CLI and document consumer responsibilities, with truthful local-platform, artifact, and protected-deployment qualification boundaries.

## Deliverables and scope

- Extend existing `project/castle/cli.py` with the Phase 1 namespace command contract over the same library: explicit namespace/CAS roots, create/read/list/replace/rename/move/delete, frozen import, snapshot inspection/export, and explicit recovery presentation. No implicit authority creation, root selection, remote service, or global decrypted view. Destructive purge remains the coordinated library operation unless its public exposure was explicitly settled in Phase 1.
- Add proposed `project/tests/test_namespace_consumers.py`, extend existing `project/tests/test_cli.py` and `project/tests/test_hermeticity.py`, and add a disposable synthetic interactive workflow in `project/examples/namespace_demo.py`. Demonstrate intake freeze, policy-first addressing, retrieval revision lag/citation identity, manifest exclusion from generic indexing, CAS-only use, and authorized export destination checks without importing a consuming repository.
- Update existing `project/README.md`, top-level `README.md`, and owning package/API documentation with feature discovery, version refusal, current versus historical authority, lifecycle/recovery outcomes, backup requirements, consumer admission obligations, and key/access lifetime duties. Keep `project/` independent of governance paths.
- Qualify real subprocess transactions/locking/recovery on local macOS and Linux, and fresh wheel/sdist install workflows using the repository's existing artifact/CI gates. Do not claim Darwin-only CAS clone is supported on Linux. No package publication, live migration, deployment, or dependency on consumer repository changes.
- Provide a conformance checklist/harness specification for the Phase 1 protected adapter contract: names/pages/sidecars/temp/logs/indexes/backups, locking/durability, restart, key/session revoke, and unauthorized reads. Actual encrypted adapter selection/operation is out of scope; its absent/unrun evidence is explicit and blocks an encrypted deployment claim, not local synthetic library qualification.

## Acceptance

- CLI/library end-to-end runs match independent expected tree/content/snapshot outcomes; refused inputs are actionable and do not create unintended roots. Export refuses collisions and unsafe destination links and exposes plaintext only to caller-authorized destinations.
- Consumer examples distinguish committed namespace revision from search reconciliation, preserve immutable citation identity, and apply policy before store access. A mutable name/snapshot never grants authority; cross-domain move is refused as ordinary CRUD.
- Existing CAS-only commands, import isolation, and exact artifact smoke remain valid. Supported-platform claims have actual environment-bound evidence; missing Linux or adapter qualification is recorded as not run and cannot be advertised as passed.
- Manual: the owner judges CLI naming/errors, stale-revision retry, explicit subtree delete, snapshot/export usability, and retry-window behavior. Encrypted custody and real-store seed/purge decisions remain separate owner inputs.

## Validation and handoff

Iteration: `./bin/test project/tests/test_namespace_consumers.py project/tests/test_cli.py project/tests/test_hermeticity.py`. The phase-entry plan names the current repository-owned Linux and exact-artifact commands after reading existing CI/tooling; unavailable environments park their claims. Apply the shared two-gate close protocol in [namespace-implementation.md](namespace-implementation.md).

User Demo: Disposable namespace command workflow.

- Entry point after implementation: `./bin/python project/examples/namespace_demo.py`. The example creates only its own temporary synthetic roots and prints the contract-defined commands/identities for interactive use.
- Suggested inputs: rename/move a file and its containing folder, replace its bytes, capture a snapshot, then attempt a case-colliding name and an outdated-revision write. Include an empty directory in a frozen upload.
- What to look for: familiar current names, stable entry IDs, immutable historical views, preserved empty directories, and clear conflict/error messages. Inspect the chosen export directory and distinguish search lag from lost content.
- Variations: retry after a lost response; choose a different sibling name after conflict; inspect a snapshot after logical deletion. Do not run confidential purge or real-store conversion as this demo.

## Brief refs

- [Namespace architecture](../briefs/coordinated-namespace-crud.md), §§5–12 and scope exclusions.
- [CAS contract](../briefs/cas-api.md), explicit-root CLI and self-contained consumer API.
- [Platform capabilities](../briefs/cross-platform-cloning.md), platform-specific clone limits.
- [Release readiness](../briefs/release-and-deployment-readiness.md), exact-artifact and platform evidence.
