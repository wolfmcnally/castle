# Coordinated namespace implementation roadmap

## Authorization and boundary

The owner requested an implementation plan on 2026-10-01 and restricted delivery: “Local commits only for now.” This roadmap is planning work, not a kickoff, implementation authorization, release, migration, or permission to operate on real stores. Preserve the existing release row and its status; do not infer release completion from a tag or change historical execution records. All new phases are pending in [INDEX.md](INDEX.md). A subsequent explicit kickoff selects one phase; the local-only restriction applies to this namespace initiative until the owner changes it.

The [coordinated namespace brief](../briefs/coordinated-namespace-crud.md) defines the selected architecture. The current [CAS contract](../briefs/cas-api.md), [CRDT semantics](../briefs/crdt-semantics.md), and [excision contract](../briefs/content-excision.md) remain the delivered baseline. Phase 1 refines and records the proposed extension before runtime implementation. Ordinary phase-entry planning supplies exact public types, command flags, definition evidence, file changes, and a current candidate-bound implementation plan; this roadmap does not pre-author future helper internals or children.

## Sequence and independently accepted outcomes

| Phase | Outcome | Why this is a separate boundary |
|---|---|---|
| [1](phase-1.md) | Namespace, recovery, replacement, and protected-I/O contracts | Consequential persisted/public/security contracts must be settled before code. |
| [2](phase-2.md) | Bounded CAS interruption recovery | Namespace recovery cannot repair an unjournaled object or torn CAS append by itself. |
| [3](phase-3.md) | Namespace authority and crash-safe database replacement | A durable tree and forward replacement protocol precede commands and confidential cleanup. |
| [4](phase-4.md) | Coordinated transactional CRUD | One ordered authority, validated trees, durable intents, and safe retries form one coherent capability. |
| [5](phase-5.md) | Frozen folder admission, snapshots, and optional onboarding | Explicit historic views and uncertainty-preserving reconstruction precede purge dependencies. |
| [6](phase-6.md) | Namespace-aware excision and epoch fencing | Irreversible cutover has a distinct quiescence, cleanup, and custody acceptance seam. |
| [7](phase-7.md) | Consumer API/CLI, package, and platform qualification | Consumer usability and platform/artifact claims require their own end-to-end evidence. |

Each phase depends on its predecessor as recorded in frontmatter. These are major outcomes, not pre-drafted child phases. Contract changes ripple only into pending phases under [phase-ripple.md](../policies/phase-ripple.md). A newly discovered consequential decision is surfaced at its owning contract; ordinary helper choices stay with phase-entry implementation planning.

## Cross-cutting constraints

- Namespace authority is a separate disjoint root; the CAS inventory and closed journal schema do not gain arbitrary namespace files or event fields. Snapshots are ordinary immutable CAS payloads. CAS union does not merge live namespaces.
- One coordinator serializes mutation. Stable entry identities, normalized names, a rooted acyclic tree, coarse expected revisions, and reserved idempotency tokens are required throughout. Direct CAS callers participate in the caller-owned admission protocol; a namespace lock cannot fence an uncooperative caller.
- Publish content before its live reference. Recover only operation-owned, identity-bound residue. Inconsistent CAS blocks service; SQL commit does not establish CAS repair or confidential purge completion.
- Protect names, manifests, database pages/sidecars, staging, intents/results, journals, indexes, logs, and backups. SQLite on synthetic local files is a reference execution environment, not proof of encryption or enclave custody. No generic SQL plugin framework, crypto implementation, or attestation service is introduced by these phases.
- The protected execution/I/O contract is settled in Phase 1. Runtime phases may qualify local behavior on synthetic fixtures. Before claiming or using encrypted deployment, its owner supplies a concrete adapter meeting that contract and independent environment-bound evidence; unsupported capabilities refuse that deployment rather than weaken the contract. Choosing/deploying that adapter is outside this initiative.
- Keep Castle consumer-blind and `project/` self-contained. Intake freezes uploads; retrieval reconciles admitted revisions; policy selects authorized stores before access. Cross-domain relocation is explicit reclassification, not an ordinary move. No consuming repository is edited by this plan.
- Native filesystem reconstruction, mounts, offline edits, namespace CRDTs, snapshot-to-current-state restore, automatic per-edit archives/GC, cloud replication, physical erasure, live migrations, and publication are excluded.

## Proof ownership and acceptance inventory

All listed runtime/test paths below are proposed new files unless marked existing. The exact phase-entry plan may consolidate ordinary private modules while retaining every behavior and proof owner. Fixture expectations are independently authored trees, byte identities, and dependency sets; do not derive the oracle with the implementation under test.

| Brief property | Owner and planned proof | Falsifier / distinguishing evidence |
|---|---|---|
| Public/persisted contract and protected boundary | Phase 1; contract tables in `briefs/coordinated-namespace-crud.md` | An operation, stage, field, error, or supported deployment cannot be interpreted without guessing. |
| Interrupted CAS publication | Phase 2; `project/tests/test_cas_recovery.py` | Real subprocess termination inside publication/append leaves an unsafe store accepted or silently loses custody metadata. |
| Namespace authority and database replacement | Phase 3; `project/tests/test_namespace_store.py` | Missing/mismatched authority is silently rebuilt, a living lock is stolen, or a replacement crash exposes unvalidated/old selected pages. |
| Stable IDs, names, parents, cycles, metadata | Phase 4; `project/tests/test_namespace.py` | Rename changes identity, a collision/cycle is admitted, or a failed precondition changes the live tree. |
| Atomicity, revisions, idempotence, intents | Phase 4; `project/tests/test_namespace_transactions.py` | Two stale writers both win, retry repeats an effect, or interruption publishes a partial tree/dangling reference. |
| Folder membership, snapshot bytes, and history | Phase 5; `project/tests/test_namespace_snapshots.py` | Empty directories vanish, live changing intake passes, or captured revision/canonical bytes disagree with independent traversal. |
| Optional onboarding and version refusal | Phase 5; `project/tests/test_namespace_onboarding.py` | Missing namespace becomes invented authority, ambiguous sightings select a winner, or unsupported formats open. |
| Excision dependencies and cleanup stages | Phase 6; `project/tests/test_namespace_excision.py` | A shared owner breaks, selected metadata survives in owned retained artifacts, or service opens before cleanup. |
| Unchanged CAS laws and lifecycle | Phases 2–6; existing `project/tests/test_merge.py`, `project/tests/test_journal.py`, `project/tests/test_cas.py` | Core union/strict-format/posture/retirement behavior changes outside the authorized extension. |
| Consumer isolation, CLI, and qualification | Phase 7; `project/tests/test_namespace_consumers.py`, existing `project/tests/test_cli.py` and `project/tests/test_hermeticity.py` | CAS-only workflows gain implicit namespace state, a mutable path grants access, or a package reads governance/consumer files. |

New/materially changed proofs receive named falsifiers and enter the existing governed proof inventory in the same implementation phase. Follow [test-suite-governance.md](../policies/test-suite-governance.md) and [acceptance-empirical.md](../policies/acceptance-empirical.md); preserve prior measured evidence instead of relabeling it as a new run. No runtime proof in this inventory has been executed by this planning task.

## Shared future close protocol

Each phase's focused command names its changed suite; those planned test files exist only after that phase implements them. Iteration uses the repository wrappers. After independent product critique and primary disposition, the orchestrator runs the complete phase acceptance sequence ending with a bare `./bin/check all`. Distinct Linux, artifact, fault-injection, or adapter evidence is additional to that gate; a skip is not a pass.

After accepted close, phase status, ripple, truthful LOG/lessons, and report writes, run the second bare `./bin/check all`. No tracked write follows its success. Stage only attributable paths, inspect the staged diff and resulting commit, and commit locally without push. Existing [review](../policies/review-lanes.md), [staging](../policies/commit-staging.md), [log](../policies/log-discipline.md), and [human acceptance](../policies/human-in-the-loop.md) rules remain binding. The planning update starts/closes no product phase and invents no execution trace, lessons, proof results, or LOG block.

## Owner inputs and stop conditions

No owner answer is needed to finish this roadmap. Phase 1 must settle consequential protocol questions before runtime work proceeds. The actual protected adapter/environment and its access/key lifecycle must be selected and qualified before encrypted use; synthetic local proof cannot satisfy that custody decision. Existing-store seed ambiguity and shared-content/metadata purge selections require explicit caller choices when those operations are requested. Real-data conversion, external-copy disposition, destructive tests, deployment, and publication require their own authorization. Unmet storage/replacement guarantees stop the affected implementation/deployment; they do not justify a weaker success claim.
