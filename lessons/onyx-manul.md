---
slug: onyx-manul
title: Follow privacy payloads through copied recovery authority
status: candidate
scope: local
proposed_surface: test
filed: 2026-10-22
source: kickoff
occurrences:
  - date: 2026-10-22
    ref: "Phase 8 CODE-F002 independent expiry-privacy probe"
---

When promising logical expiry, inventory every surviving authority that copied the expired request's semantic evidence. NULL fields in the target row and absence of its directory prove only those locations; they do not prove absence from another operation's cleanup recipe. Retain recovery proof until owned cleanup and its parent-directory synchronization are durable, then durably prune copied proof before acknowledgment. A failed or uncertain pruning commit must preserve fail-closed admission and original-token recovery instead of guessing which SQL state survived.

An independent release-preparation probe found that retired request/result fields were NULL and their files gone, yet the expiry operation retained the target fingerprint and canonical-request digest inside ownership_receipt. The smallest correction preserves the existing privacy contract: complete exact owned disposal, resynchronize even already-absent targets, then clear the copied expiry map in SQL. Terminal pending proof, even with a durable own receipt, refuses ordinary reopen until explicit recovery. Rejected expiry owns no target disposal. The existing restart/storage-fault leaf independently scans all logical rows and surviving files and exercises actual process death, failed unlink/fsync, uncertain commits, repeated recovery and concurrent retries. SQLite free pages, physical residue and external copies remain outside logical expiry. This is one candidate engineering observation, not a policy graduation or new test-family budget.
