# Changelog

## 0.1.0

- Compact context-bound discriminators: `journal.v2`, `migration.v1` and `clone-order.v1`; runtime accepts only its current journal discriminator.
- Strict journal-v2 authority, SHA-256 content identities, sealed immutable objects, typed ingest receipts, deterministic journal union and a rebuildable SQLite query index.
- Portable macOS and Linux ingest, lookup, materialization, statistics, verification, index rebuild and CLI commands. Query connections close deterministically, including on failure.
- Darwin/APFS store cloning and merge with explicit unsupported-platform refusals and no byte-copy fallback for those operations.
- Explicit once-only v1 journal migration; ordinary runtime reads only v2.
- Offline store identity preparation, successor-epoch excision, predecessor retirement and forward recovery. Local receipts do not establish removal of original inputs, remote copies, backups or physical residue.
- A standalone Python 3.12+ package and CLI with a standard-library-only runtime and the included MIT license.

This entry defines the tagged release scope. GitHub Releases records the tested commit and inspected package hashes; package-registry publication is separate.
