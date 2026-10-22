# Changelog

## 0.2.0

- Add an explicitly initialized, serialized ACID namespace with typed requests/results, durable original-token retry/status and fail-closed recovery. Support browsing, directories, file/directory rename, move, whole-subtree duplication and logical deletion with stable identities and empty-directory preservation.
- Create and replace actual new immutable file bytes through owned CAS publication and interruption recovery before atomic namespace/reference/result commitment. Replacement preserves entry identity; copies share immutable content and remain independent namespace entries.
- Admit complete frozen folders atomically, capture deterministic explicit snapshots and inspect/export them to an absent caller-authorized directory. Preserve the distinction between a snapshot control manifest and file members, including empty folders and a file named snapshot.json.
- Coordinate explicit library-only epoch excision, retained-reference/snapshot redaction, fresh database replacement and original-root-bound forward recovery. Logical deletion, snapshot-catalog deletion and result expiry never imply garbage collection or confidential erasure.
- Expose the same typed contract through castle namespace, with saved requests, explicit roots/domain ownership, strict UTF-8 input, safe errors and unknown-outcome handling after failed output/interruption. Provide a runnable synthetic workflow and trusted unprotected Unix reference environment.
- Support the namespace on macOS/APFS and Linux, with deliberate Darwin-only clone/merge refusals on Linux and exact wheel/source-rebuilt installation checks. Release qualification results identify the actual platforms and limits. No new platform backend or dependency is introduced.

Privacy and qualification limits: names, metadata, history and stable identifiers remain in user-controlled stores. Content hashes and unsalted recovery-path commitments permit guessing/linkage. Explicit retry-result expiry after seven days removes logical payloads, not forensic residue. The reference session provides neither authentication nor encryption. Process loss and injected storage faults do not certify native VFS/device hardware power loss, physical erasure, protected custody or external copies. Publication, consumer adoption and live-store operations require separate authority; this entry records capability scope, not publication status.

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
