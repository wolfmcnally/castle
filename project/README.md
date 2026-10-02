# castle

A standalone content-addressable snapshot store: a Python library plus a
`castle` CLI.

## Library

Import the module that owns the operation:

- `castle.journal` owns strict journal v2 records, canonical bytes,
  deterministic ordering, journal reads, and the stable `journal.lock` sidecar.
- `castle.cas` owns content identities, object layout, byte-based media
  detection, immutable publication, verification, and the rebuildable SQLite
  index. The journal is authority; index rows are derived, while target-local
  `remotes` rows survive a rebuild.
- `castle.store_merge` owns `clone_store` and `merge_into`. These are
  library-only operations: Castle deliberately has no generic merge, copy, or
  compare CLI command. Store cloning and merge publication require Darwin and
  APFS copy-on-write support; they never fall back to a byte copy.
- `castle.journal_migrate_v1` is the sole reader of the superseded v1 journal.
  It exists only for the explicit, once-only migration operation and imports
  the journal and CAS layers. Ordinary runtime remains v2-only and never loads
  this module.

- `castle.store_state` owns `StoreIdentity`, `StoreStateError`, `read_store_identity` and offline `prepare_store`.
- `castle.excision` owns `ExcisionError`, `ExcisionReceipt`, `excise_store` and `recover_excision`. It reconstructs retained authority into a fresh successor, retires the predecessor and completes recovery forward. These are library-only operations.

The package root exports only `__version__`; import the module that owns the
operation you need.

## Platform capabilities

The core library and CLI cover ingest, deduplication, receipts, journal locking, lookup, materialization, statistics, verification, index rebuild and explicit once-only v1 migration on macOS and Linux. Linux ingest uses byte copying with destination digest verification, including when source and store share a volume; it never invokes Darwin's `cp -c`. Darwin retains opportunistic same-volume cloning and verified copying when that attempt is unavailable.

`clone_store` and `merge_into` remain Darwin-only. On other platforms they raise `MergeError` with `store-merge-platform-unsupported` before reading journal or object content, opening the index, entering the stable lock or publishing anything. Root canonicalization and metadata safety checks retain their earlier refusal precedence. Store cloning and merge have no byte-copy fallback or Linux reflink backend.

## CLI

Every command takes `--root <store-root>`:

- `init` creates a new store and is the sole CLI store-creation path.
- `ingest` snapshots a source tree.
- `get` resolves, streams, or rematerializes an object.
- `stat` reports object metadata or store totals.
- `names` lists sightings for an object identity.
- `find` searches journaled relative names.
- `verify` checks object bytes, sealing, journal structure, and index agreement.
- `rebuild-index` reconstructs the SQLite cache while preserving `remotes`.
- `journal-migrate-v1` performs the explicitly authorized once-only migration.
- `sync-s3` is an explicit not-implemented refusal reserved for a later
  encrypted-replication surface.

`init` is the only command that brings a root into existence. The exact guarded
set is `ingest`, `rebuild-index`, and `journal-migrate-v1`: each refuses a root
that is not already a directory before it reaches create-on-write library code.

## Install and quickstart

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). From the public repository root:

```bash
./bin/setup
```

```bash
cd project
```

```bash
uv run --locked --managed-python castle --help
```

Use disposable input for a first smoke test:

```bash
CASTLE_DEMO=$(mktemp -d)
```

```bash
mkdir "$CASTLE_DEMO/source"
```

```bash
printf 'disposable example\n' > "$CASTLE_DEMO/source/example.txt"
```

```bash
uv run --locked --managed-python castle init --root "$CASTLE_DEMO/store"
```

```bash
uv run --locked --managed-python castle ingest --root "$CASTLE_DEMO/store" --source "$CASTLE_DEMO/source" --source-id demo
```

```bash
uv run --locked --managed-python castle verify --root "$CASTLE_DEMO/store"
```

## Once-only v1 migration

The migration entry point accepts only an existing metadata-free v1 store. It refuses retired, prepared or already identified stores before reading old authority. The converted v2 store deliberately remains metadata-free; call `prepare_store` offline before ordinary reads, verification or index repair, as described below.

Migration is an irreversible authority change. First compute and retain the
SHA-256 of the exact source `journal.jsonl`, and take your own backup or
filesystem snapshot when the store is valuable. Castle creates no backup,
rollback file, marker, or mixed-format reader.

```bash
shasum -a 256 ~/castle-demo/store/journal.jsonl
```

Pass the resulting lowercase digest as explicit authorization:

```bash
uv run --locked --managed-python castle journal-migrate-v1 \
    --root ~/castle-demo/store \
    --expect-journal-sha256 <64-lowercase-hex-digest>
```

The command accepts only the exact ten-field v1 ingest grammar. It derives each
v2 event identity from the one-based physical source-line ordinal plus the
canonical complete-row digest, orders the complete result canonically, and
discards v1 `mtime`. It plans and validates the full conversion before taking
the stable lock, repeats that plan under the same `journal.lock` used by
ingest, stages and fsyncs the complete target, reads it back, and publishes the
journal with one atomic replacement. It then rebuilds the index under that
same lock, preserving `remotes`.

Success emits one JSON object with this exact shape:

```json
{
  "event_count": 2,
  "from": "v1",
  "journal_sha256_after": "<64-lowercase-hex-digest>",
  "journal_sha256_before": "<64-lowercase-hex-digest>",
  "object_count": 2,
  "root": "<canonical-store-root>",
  "schema": "migration.v1",
  "to": "v2"
}
```

Before atomic replacement, any refusal preserves v1 authority. After
replacement, v2 remains authoritative even if directory durability or the
derived-index rebuild reports a failure; Castle never rolls back to v1. If the
journal is already migrated but index rebuilding failed, first prepare the v2 store offline. Then repair the cache with:

```bash
uv run --locked --managed-python castle rebuild-index --root ~/castle-demo/store
```

A second `journal-migrate-v1` invocation refuses because ordinary Castle reads
strict v2 only.

## Store identity and offline preparation

New stores receive an active generation-zero identity. Ordinary operations refuse retired stores with `store-retired`, building/prepared successors with `store-not-active`, and populated metadata-free stores with `store-identity-required`. Invalid metadata refuses with `store-state-invalid`. Even dry-run and empty requests respect lifecycle. `init` cannot revive a predecessor.

For an existing valid metadata-free v2 store, stop all users and drain open handles before deliberate enrollment. From this standalone package directory, start the locked interpreter:

```bash
uv run --locked --managed-python python
```

```python
from pathlib import Path
from castle.store_state import prepare_store
identity = prepare_store(Path("store-to-prepare"))
identity.to_dict()
```

Use the intended offline store path in this preparation example. Preparation validates authority without rewriting journal, objects, index or remote rows, then durably publishes identity. A matching active retry returns the same identity. Missing index is allowed; unreadable remotes, inconsistent authority or unbound metadata residue refuse. Ordinary reads never perform enrollment. A successful v1 conversion uses this same explicit boundary before ordinary `rebuild-index` repair.

Cloning preserves lineage and epoch but creates a fresh local store identity. Merge requires compatible active identities in the same epoch under both locks. Excision preserves lineage, advances generation once and mints a new epoch, so older replicas cannot merge selected content back. Journal and object growth remain monotonic within each compatible epoch.

## Disposable successor cutover and retry

This walkthrough uses the implemented APIs; automated tests and your operational acceptance remain separate. After installing the locked package, start Python from this package directory:

```bash
uv run --locked --managed-python python
```

Use only a fresh temporary directory and the following disposable inputs:

```python
from pathlib import Path
from tempfile import mkdtemp
from uuid import uuid4
from castle import cas, journal
from castle.store_state import read_store_identity
from castle.excision import excise_store, recover_excision
scratch = Path(mkdtemp(prefix="castle-excision-demo-"))
old, new = scratch / "old", scratch / "new"
remove_input, keep_input = scratch / "remove.txt", scratch / "keep.txt"
remove_input.write_text("disposable removed example\n")
keep_input.write_text("disposable retained example\n")
removed = cas.ingest_file(old, remove_input, "demo")
retained = cas.ingest_file(old, keep_input, "demo")
identity, operation = read_store_identity(old), str(uuid4())
```

First submit an empty selection as one visible action:

```python
excise_store(old, new, cas_ids=[], operation_id=operation, expected_source=identity)
```

Expect `ExcisionError: excision-request-invalid`. Inspect the unchanged identity and absent destination, then judge whether the correction is clear:

```python
read_store_identity(old) == identity
new.exists()
```

These yield `True` and `False`. Now submit the same selected identity twice. Selection is normalized by identity, so this removes one identity and all of its sightings:

```python
receipt = excise_store(old, new, cas_ids=[removed.cas_id, removed.cas_id], operation_id=operation, expected_source=identity)
receipt.to_dict()
list(old.iterdir())
journal.read_journal(new)
cas.require_object(new, retained.cas_id).read_text()
```

Observe one generation advance, a new successor identity and epoch, only `retired.json` and the original `journal.lock` at the predecessor, no removed sightings, and `disposable retained example\n` in the successor. The closed receipt contains exactly `schema`, `operation_id`, `predecessor`, `successor`, `outcome` and `local_result`. Its schema is `castle.excision_receipt.v1`, outcome is `completed`, and local result is `successor-active-predecessor-retired`. It contains no selected identity, path, filename, remote key or count. Judge whether that local claim is understandable. Both original input files deliberately remain outside the store.

Recover without supplying selection and compare the historical receipt:

```python
again = recover_excision(old, new, operation_id=operation, expected_source=identity)
again.to_dict() == receipt.to_dict()
```

Expect `True`. Try each of these separately:

```python
journal.ensure_store(old)
```

```python
cas.require_object(new, removed.cas_id)
```

The first raises `StoreStateError: store-retired`; the second reports the selected object missing. Recovery does not recreate predecessor authority. Completed retries preserve the same receipt even after valid successor growth or a later successor retirement; that receipt describes the completed transition, not permanent equality of current payload.

For variations, create another fresh fixture using the setup above and select both identities to inspect a valid empty successor. Try a different operation UUID in recovery to observe `excision-operation-mismatch`. Discuss original inputs, backups and external reference updates. Keep the disposable artifacts available during discussion; their later cleanup is a separate explicit action.

## Cutover and recovery limits

The caller supplies the operation UUID and expected source identity, records their private selection association, stops admission, drains users and open handles, and serializes the entire cutover. Selecting an identity removes every occurrence of its identical bytes and all its sightings in this store; resolve shared-content ownership before selecting it. Excision validates the whole source and whole selection before allocation, constructs retained objects and journal plus a fresh SQLite database, and preserves the exact multiset of retained target-local remote rows. Missing indexes are reconstructible; unreadable remotes are never silently dropped.

Before retirement, the source remains authoritative. Retry `excise_store` with the original request; an owned preparation is revalidated against the newly locked source. Selection-free recovery reports `excision-preparation-incomplete` at that stage. An unbound destination is not adopted. Once retirement is visible, use `recover_excision`; never reactivate or roll back the source. Recovery validates ownership and the prepared successor before cleanup, keeps the successor unavailable until cleanup is durable, then activates it and marks completion. A damaged or mismatched recovery state refuses without deleting unrelated bytes.

After local completion the caller updates every retained live reference and resumes service on the successor. A reference-update failure remains incomplete caller work. Stable locks cannot revoke external descriptors, and Castle does not erase original inputs, disconnected replicas, backups, filesystem snapshots, SSD residue or caller records. This API makes no physical-erasure claim, performs no cloud deletion and supplies no hostile-administrator guarantee.

## Build gates

From the repository root:

```bash
./bin/check all
```

This runs lint, format validation and every retained package test. See [CONTRIBUTING.md](../CONTRIBUTING.md) for contribution guidance and [SECURITY.md](../SECURITY.md) for private vulnerability reporting.

## Release candidate

Version 0.1.1 is a patch release under the [MIT license](LICENSE). GitHub Releases records publication status. The [changelog](CHANGELOG.md) states the release scope, and the [release procedure](RELEASE.md) covers local artifact creation and a disposable clean-install smoke check. Tagging and publication require the maintainer’s approval of the exact artifacts and supported capability scope.
