# Changelog

## 0.1.1

- Preserve the object and journal once a journal append is attempted, including write, flush and synchronization failures. The ingest still fails and returns no success receipt; retained bytes allow diagnosis and recovery instead of being deleted after uncertain publication.
- Require a consistent strict journal, index and object inventory before ingest. Keep staging and operation-owned preappend cleanup under the stable journal lock, including immutable-file cleanup on macOS. Index rebuild synchronizes the current journal inode before reconstructing the derived cache and preserves remote metadata and provenance.
- Strengthen the existing failure-injection proofs and clarify recovery guidance. Partial journal tails, orphan objects, staging residue and missing bytes fail closed; index rebuild does not automatically adopt or remove these states or restore missing object bytes.
- Retain the 0.1.0 public API, CLI, persisted schema identifiers and platform capability scope. Namespace CRUD remains proposed documentation, not a delivered runtime feature.

Durability limits: a later successful synchronization, clean verification, rebuilt counts or reconstructed receipt does not prove bytes from an earlier failed write-back became durable. Keep the original failed ingest unconfirmed. This patch adds no persistent uncertain-write recovery protocol or power-loss qualification. Per-file admission scans the full journal, index and inventory and serializes staging; a concurrent SQLite reader checkpoint can cause a transient healthy-ingest refusal.

## 0.1.0

- Compact context-bound discriminators: `journal.v2`, `migration.v1` and `clone-order.v1`; runtime accepts only its current journal discriminator.
- Strict journal-v2 authority, SHA-256 content identities, sealed immutable objects, typed ingest receipts, deterministic journal union and a rebuildable SQLite query index.
- Portable macOS and Linux ingest, lookup, materialization, statistics, verification, index rebuild and CLI commands. Query connections close deterministically, including on failure.
- Darwin/APFS store cloning and merge with explicit unsupported-platform refusals and no byte-copy fallback for those operations.
- Explicit once-only v1 journal migration; ordinary runtime reads only v2.
- Offline store identity preparation, successor-epoch excision, predecessor retirement and forward recovery. Local receipts do not establish removal of original inputs, remote copies, backups or physical residue.
- A standalone Python 3.12+ package and CLI with a standard-library-only runtime and the included MIT license.

This entry defines the tagged release scope. GitHub Releases records the tested commit and inspected package hashes; package-registry publication is separate.
