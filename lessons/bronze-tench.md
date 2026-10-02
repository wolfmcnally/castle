---
slug: bronze-tench
title: Scope a tool-root override to the process that needs it
status: candidate
scope: methodology
proposed_surface: bin
filed: 2026-10-02
source: kickoff
occurrences:
  - date: 2026-10-02
    ref: "Publication review correction PARK: gate environment root override"
---

When launching a repository gate from pinned orchestration tools, scope any explicit engine-root override to the tool invocation that requires it. A gate's child tests can intentionally create another repository and use that child's cwd as authority; carrying the parent's root override into those children changes the meaning of their cwd. Invoke the pinned gate tool from the real repository with the unnecessary override removed, rather than changing fixture code or weakening telemetry validation.

A full acceptance run passed 195 tests and failed the retained complete synthetic kickoff family at its stub watcher because that child looked for its fixture trace in the real repository. The same focused family had already passed directly without the override. The runner had deliberately set `KICKOFF_ENGINE_ROOT` for pinned tool ownership and propagated it into `run-gate`; `kickoff-config` resolves that variable before cwd, so the test fixture's correct cwd could not select its trace. This is a caller-environment contribution, not evidence that the product, the independent subscription reviewer, or the test oracle is broken. Correction belongs in this task's launcher: remove the override for gate execution while retaining explicit repo-root arguments for telemetry operations. The required full gate checks the corrected execution boundary before acceptance.

## Evidence

`bin/kickoff-config` resolves `KICKOFF_ENGINE_ROOT` ahead of cwd for pinned tools. `tests/test_kickoff_evidence.py::test_complete_synthetic_kickoff_cross_validates_roles_revision_and_gates` passed in the direct focused invocation and failed only under the incorrectly scoped run environment. The truthful PARK and exact failing gate diagnostics are preserved; no fixture or runtime policy change is made because of this lesson.
