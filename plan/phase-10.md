---
id: 10
title: Read-only namespace access
depends_on: [phase-9.md]
informs: []
---

# Phase 10 — Read-only namespace access

## Goal

Let an authorized read/search process open an existing namespace and its CAS on read-only mounts through the same validated public read interface.

## Deliverables

- Add `castle.namespace.open_namespace_readonly(namespace_root, cas_root, session)` and `LocalSession(..., read_only=True)`.
- Support existing info, get-entry, list-directory and snapshot inspection with shared reader locks, exclusive writer exclusion, read-only SQLite, unchanged validation and authorization, and no writes or recovery to namespace/CAS/domain-lock authority.
- Refuse mutation, hot-journal/sidecar residue and interrupted writer state without repair. Preserve writer defaults, formats, safe roots, binding and session lifetime checks.
- Extend existing proofs and owning consumer documentation. Preserve all other repositories and running previews.

## Acceptance

Real synthetic public-API fixtures demonstrate current reads and immutable snapshot inspection, two readers together, writer/reader exclusion across processes, and writer reopening after readers close. Namespace/CAS/domain-lock images remain unchanged. Writable opens to those roots, direct SQL mutation and every public mutation route refuse. Hot-journal/sidecar or interrupted writer state refuses without modification; existing recovery and refusal proofs remain valid.

Use independent direct Opus/high PLAN and CODE advice under the configured primary workflow, focused tests, implementation full gate, evidence close and separate bare handoff gate. Linux and artifact qualification claims require actual matching evidence; missing venues stay unclaimed.

## Bounds

Synthetic source/test work only. No live stores, downstream repository edits, worker recovery, policy/settings/permission changes, migration or new storage representation. No commit, push, tag, release or publication under this task; return a qualified source diff for review. The downstream official qualified tag dependency remains a separate release decision.

User Demo: N/A for this source API qualification; human integration acceptance and official release remain open.

## Brief refs

- [Coordinated namespace contract](../briefs/coordinated-namespace-crud.md), sections 2, 14–20 and 23: explicit authority, admission, validated reads, snapshots, concurrency and recovery ownership.
