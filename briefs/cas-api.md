---
title: "Castle CAS API"
date: 2026-09-06
status: implemented
scope: |
  The ratified extraction contract for Castle's standalone content-addressable
  snapshot store: module ownership, strict journal-v2 semantics, object and
  index behavior, copy-on-write merge-back, once-only v1 migration, CLI
  boundary, test obligations, and independent consumer integration.
---

# Castle CAS API

## 1. Authority and extraction boundary

Castle owns the generic content-addressable store. Consumers own workspace composition: manifest lookup, declared store/member selection, lifecycle orchestration and their own authorization/custody policy. This contract describes the standalone delivered storage boundary.

Nothing under `project/` imports the consumer, reads a the consumer workspace manifest,
names a book, or reaches outside Castle’s self-contained deliverable. Castle
accepts a resolved CAS member root. A consuming workspace decides which root is
authorized before calling Castle.

The extraction is behavior-preserving at the storage boundary. It does not add a
daemon, deletion semantics, a generic copy command, or a generic merge command.
A resident service remains deferred until a concrete consumer requires
cross-process arbitration.

## 1a. Reviewed successor-epoch extension

The original extraction boundary above records the pre-excision implementation. The independently reviewed [content-excision contract](content-excision.md) now defines the authorized successor-epoch extension. Its API, metadata, transition, error and receipt tables are the single definitions; this section does not duplicate those schemas. The package implements this extension in `castle.store_state` and `castle.excision`. Implementation status identifies the code that owns the contract; release qualification and custody acceptance require their own evidence.

Within an active epoch, objects and journal events remain grow-only and merge remains union. Excision reconstructs a new epoch from explicitly retained authority, publishes an irreversible local retirement fence, removes the predecessor's content-bearing files, and enables selection-free forward recovery. It never deletes within the old merge domain. Ordinary root-touching operations refuse retired or non-active stores; existing metadata-free v2 stores require deliberate offline preparation rather than implicit enrollment. The isolated once-only v1 migration boundary remains explicit.

The extension adds `castle.store_state` for identity and lifecycle, and `castle.excision` for reconstruction, retirement and recovery. The module-initialization boundary in the owning contract permits delayed lifecycle calls from the journal without loading the v1 reader. Clone preserves lineage and epoch while minting a fresh local replica identity. Merge requires active, distinct replicas in the same epoch; its existing root/platform refusal precedence and Darwin-only no-object-read clone contract remain. Excision instead uses portable verified byte reconstruction on macOS and Linux, with a fresh index and retained remotes multiset.

Callers supply resolved roots, explicit content identities and operation identity, stop admission, and drain access through cutover. Consumer records, retained originals, remote copies and backups remain outside this library's local receipt. No additional CLI command is introduced.

## 2. Package map and import direction

The package has six storage implementation modules:

- `castle.journal` owns strict v2 events, canonical ordering and serialization, journal reads, the root `CastleError`, and stable locks.
- `castle.store_state` owns validated store identities, lifecycle metadata, offline preparation, bootstrap and epoch checks. At module initialization it imports only the root error from `castle.journal` among package modules.
- `castle.cas` owns content identity, object layout, media detection, ingest, receipts, lookup, materialization, statistics, verification and the rebuildable SQLite index. It uses journal and lifecycle validation.
- `castle.store_merge` owns APFS copy-on-write cloning, exclusion instrumentation, object/journal union, exclusive publication and resumable merge. It uses CAS, journal and state.
- `castle.excision` owns successor reconstruction, predecessor retirement and forward recovery. It uses CAS, journal and state.
- `castle.journal_migrate_v1` remains the sole reader of the superseded v1 journal shape. It uses CAS, journal and state only when explicitly invoked.

Journal entry points consult lifecycle validation through function-local imports after journal initialization. State's offline preparation likewise loads its CAS validation dependency at call time. These delayed calls preserve module initialization without duplicating the root error or importing the isolated v1 reader into ordinary runtime. The package root exports the version only; callers import each owning module directly. Every module-owned error descends from `CastleError`.

## 3. Persisted contracts

Schema discriminators name their local record family and structural version. The owning parser and complete field validation supply context:

- journal event schema: `journal.v2`;
- migration-result schema: `migration.v1`;
- clone-order witness schema: `clone-order.v1`;
- journal filename: `journal.jsonl`;
- stable lock filename: `journal.lock`;
- index filename: `cas.sqlite`;
- object identity: `sha256:<64 lowercase hex>`;
- object path: `objects/sha256/ab/cd/<full-64-hex>`.

Castle has one runtime journal format, v2, and rejects every other shape or discriminator. Schema names do not establish publisher identity, authenticity or authorization. Updating a discriminator requires an explicitly authorized offline conversion of existing journals and coordinated consumer upgrades; ordinary runtime has no alternate-schema reader.

The journal is authority. SQLite is a disposable query cache reconstructed from
the journal plus the immutable object tree. Rebuild preserves the non-journaled
`remotes` table or refuses before replacement if it cannot preserve it.

## 4. Journal-v2 contract

Every runtime event has exactly these fields:

- `schema`
- `event_id`
- `at`
- `event`
- `cas_id`
- `size`
- `media_type`
- `source_root_id`
- `original_relpath`
- `batch`
- `outcome`

Runtime-minted identities use `uuid:<UUIDv4>`. Migrated identities use
`v1:<16-digit ordinal>:sha256:<64 lowercase hex>`. No other identity grammar is
accepted.

Events order by `(parsed UTC timestamp, event_id)`. Canonical journal bytes are
UTF-8 JSON Lines with sorted keys, compact separators, and one trailing newline
per event. Duplicate identities, conflicting rows for one identity, malformed
timestamps, unsafe relative paths, unknown fields, missing fields, v1 records,
and cross-record fold inconsistencies refuse.

The lock is held on the stable `journal.lock` inode, never on
`journal.jsonl`, because merge and migration publish a replacement journal by
atomic rename. The sidecar contains no authority and must be a safe regular
file.

## 5. CAS runtime contract

`castle.cas` preserves the Phase-37 object-store behavior:

- SHA-256 is streamed and emitted in prefixed form.
- Stored object paths are pure functions of identity.
- Media type is classified from bytes, never from a filename suffix.
- The only admitted detector refinements are:
  - `application/zip` → WordprocessingML;
  - `application/zip` → SpreadsheetML.
- Same-volume ingest may use APFS clonefile behavior.
- The library retains Phase-37 create-on-write for a store tree. Through the
  CLI that path is reachable only via `init`; the commands §8 names as guarded
  refuse an absent root before the library can create one. §8 is the single
  statement of which commands those are -- this bullet does not widen it.
- Cross-volume ingest copies and re-hashes the destination before publication.
- Publication is atomic and write-if-absent.
- Existing bytes under one identity must hash to that identity or the operation
  refuses.
- Stored objects are sealed read-only; stores carrying either declared sync
  exclusion attribute additionally require the supported user-immutable
  posture.
- Ingest returns strict typed receipts carrying object and name-sighting
  provenance.
- `verify` distinguishes corrupt object, missing object, and index drift with
  exit codes `10`, `11`, and `12`; clean verification exits `0`.
- `rebuild_index`, `names_for`, `find_names`, `materialize`,
  `object_stat`, and `store_stat` preserve their Phase-37 behavior and JSON
  shapes.

Before each ingest publication, Castle holds the stable journal lock, validates the complete appendable journal, compares its folded object and name rows with the index, checks the object inventory for unresolved residue and missing journaled files, and synchronizes the current journal descriptor. Only an empty journal may bootstrap a missing index. Staging occurs inside this admitted locked interval. This admission scans journal, index and inventory for every file and serializes staging; a growing batch can therefore take quadratic work and large-file staging holds the writer lock. It adds no persisted recovery cursor or state. The index inspection reads a quiescent database image; a concurrent index reader's last close can checkpoint/remove its WAL between the reads and cause a transient admission refusal. This refusal preserves authority; inspect consistency and retry after readers drain rather than assume it demonstrates persistent index drift.

A failure before the journal append attempt still cleans up that operation's new object before releasing the stable lock; on macOS this clears only its deletion-prohibiting user-immutable flag when present. Once append is attempted, write, flush or synchronization failure retains the published object and all journal evidence, propagates the failure, and returns no receipt. Visibility is not proof of successful synchronization. An identical repeated sighting can leave the folded index unchanged despite an uncertain append, so subsequent ingest still requires the journal checkpoint.

A complete valid append whose index is stale requires explicit `rebuild-index` before ingest can continue. Rebuild validates appendability and synchronizes the current journal inode under the stable lock before reconstructing the cache, preserving remote rows. Unterminated tails, unjournaled objects and staging residue refuse; Castle does not truncate, delete or silently adopt them. Those object-inventory states and missing objects have no generic in-product recovery: operator action is required, or the existing resumable merge procedure applies when a merge was interrupted. Rebuild cannot restore missing object bytes, and its returned row counts are not a store-health verdict; callers must verify the recovered store. Verification checks visible consistency and content, not power-loss durability. Receipt reconstruction likewise describes visible journal content; it is not synchronization evidence after a failed ingest.

A successful later checkpoint is not proof that an earlier failed synchronization became durable. On Linux, write-back errors are tracked relative to each open file's error cursor, so a newly opened descriptor need not report an already observed error ([Linux kernel error-sequence documentation](https://docs.kernel.org/core-api/errseq.html)). Operating systems can also lose buffered data after a write-back failure while a retry reports success ([PostgreSQL's write-back failure guidance](https://www.postgresql.org/docs/current/runtime-config-error-handling.html)). A caller that received the original synchronization error must keep that ingest unconfirmed even if rebuild and verification subsequently pass. The injected-error proofs establish preservation and control flow, not kernel write-back or power-loss recovery; this repair does not add uncertain-byte rewriting or a persistent recovery protocol.

Castle owns its streaming file-hash helper. `castle.store_merge` consumes that
implementation; Castle does not import a consumer-specific hashing module and does not duplicate
the algorithm in a second runtime module.

## 6. CoW clone and merge-back contract

`castle.store_merge` preserves the Phase-37 guarantees:

At operation entry, Castle canonicalizes each existing store root to its real path exactly once. A symlink in an ancestor outside the store subtree is permitted; a symlink at the store root or anywhere beneath it refuses. An absent clone destination is derived from its once-canonicalized existing parent. Canonical roots govern overlap checks, lock ordering, inventory, and native filesystem operations.

- There is no byte-copy fallback for store cloning.
- The destination root is created empty.
- Both exclusion attributes are applied and read back before the first
  clonefile operation:
  - `com.dropbox.ignored`
  - `com.apple.fileprovider.ignore#P`
- After the exclusions are established, `objects/` is cloned recursively by one `clonefileat` call. `journal.jsonl`, `cas.sqlite`, and any present `cas.sqlite-wal` are cloned individually while the source stable journal lock is held.
- The source `journal.lock` is not carried; the destination receives a fresh inert lock. `cas.sqlite-shm` is never carried.
- The source database is not checkpointed or otherwise mutated before cloning.
- Clone completion performs no index rebuild, byte comparison, digest verification, or `cas.verify` call. No object bytes are read by the clone operation; explicit fidelity verification remains a caller operation.
- Instrumentation uses schema `clone-order.v1` and records only fixed operation names, dense sequence numbers, and fixed attribute names. It records no content, paths, filenames, identities, byte counts, or per-file ordinals. The terminal `clone-complete` operation records successful completion and is not a fidelity-verification result.
- Source trees must be quiescent and structurally safe.
- Objects merge by identity union.
- Different bytes under one identity refuse before canonical mutation.
- Deletion in a clone has no merge meaning and never deletes canonical data.
- Journal events merge by `event_id`.
- Identical identities with different records refuse.
- The merged journal order is deterministic by `(at, event_id)`.
- The exact merged sequence is validated through the same fold used by index
  reconstruction before publication.
- Publication is exclusive, verifies bytes, restores the sealed posture, and
  remains resumable after interruption.
- Journal replacement and ordinary ingest synchronize through the stable
  sidecar lock.

Castle exposes these as library operations. The initial extraction does not add
generic `merge`, `copy`, or `compare` CLI commands. The consumer’s workspace command
continues to own clone lifecycle state and the user-facing `merge-back`
operation.

## 7. Once-only v1 migration

`castle.journal_migrate_v1` is a tool module, not a runtime reader.

It:

- accepts only the exact ten-field v1 ingest record;
- rejects mixed, v2, malformed, empty, or unsafe journals;
- derives deterministic migrated event identities from source ordinal and
  canonical row digest;
- validates the complete resulting v2 sequence and index fold before writing;
- requires the caller’s expected source-journal SHA-256;
- re-reads and replans under the stable journal lock;
- stages, fsyncs, read-backs, atomically replaces, and directory-fsyncs the
  journal;
- rebuilds the derived index under the same lock;
- refuses a second migration;
- preserves authoritative bytes on every pre-publication refusal.

The Castle CLI may expose
`journal-migrate-v1 --root <store-root> --expect-journal-sha256 <hex>`.
Its handler imports the migration module lazily. Ordinary runtime import and
ordinary CAS commands never import the v1 reader.

## 8. CLI contract

The installed entrypoint remains `castle = "castle.cli:main"`.

Core commands preserve the existing CAS operations:

- `ingest`
- `get`
- `stat`
- `names`
- `find`
- `verify`
- `rebuild-index`
- the existing explicit `sync-s3` not-implemented refusal

One additional command creates a store tree:

- `init` — the sole sanctioned way a store root comes into existence through
  Castle's CLI.

Each command accepts `--root <store-root>`. This path is one already-resolved
CAS store root, not a the consumer workspace identity. Castle has no `--manifest`,
`--repo-root`, book slug, store identity, or member vocabulary.

**These commands refuse a `--root` that does not already exist**, and refuse it
before the library is reached, so `castle.cas`'s create-on-write can never bring
a store into being as a side effect:

- `ingest`
- `rebuild-index`
- `journal-migrate-v1` (§7)

The set is enumerated rather than described as "every mutating command", because
which commands mutate is not self-evident from their names. `rebuild-index` reads
as a cache operation -- §3 calls the SQLite index a disposable query cache -- but
in the Phase-37 source being transferred it takes `journal_lock`, which calls
`_ensure_store` and creates the store tree, the lock and the journal; the
rebuild then writes the index. The end state is a store that did not exist.
Any command later found to reach `_ensure_store`, directly or transitively, joins
this list; the guard follows the behavior, not the name.

`init` is the sole exception and exists to make store creation an explicit act
rather than an implicit one.

The read commands -- `get`, `stat`, `names`, `find`, `verify` -- are unchanged
from Phase-37 behavior against an absent root, including `verify`'s exit codes.

The flag is spelled `--root`, deliberately not `--store`. The consumer's own
`bin/cas` retired `--store <filesystem path>` because a path-valued flag sharing
the vocabulary of a *declared store identity* let an absent custody root plus a
path argument become, in its own words, "a one-command refill of a store that was
deliberately not there." `--root` is unambiguously path-valued and cannot be
confused with the identity vocabulary the consumer resolves.

JSON output shapes, verification exit codes, binary `--cat` behavior, name
selection, and refusal-before-mutation semantics remain unchanged. User-facing
errors are prefixed `castle: error:`.

The consumer’s later cutover resolves a declared `(store, member)` through its own
workspace authority before invoking Castle or calling the library. An absent
declared outer store remains a the consumer refusal; Castle does not recreate that
policy from filesystem guesses.

## 9. Dependencies, isolation, and tests

The extracted runtime remains Python-standard-library-only. Phase 1 adds no
runtime dependency and therefore does not change the lockfile merely because
the modules arrive.

Tests are self-contained under `project/tests/` and never import the consumer or load
files outside `project/`:

- `test_journal.py` covers strict v2 parsing, identities, timestamps,
  deterministic bytes, sequence conflicts, v1 rejection, append refusal, and
  stable locking.
- `test_cas.py` covers identity, layout, ingest, deduplication, media
  classification and refinement, atomic publication, receipts, lookup,
  materialization, verification exit modes, index reconstruction, and remotes
  preservation.
- `test_merge.py` covers clone ordering, no-copy refusal, algebraic journal
  union, object conflicts, deletion non-semantics, deterministic order,
  exclusive publication, sealing, interruption, resumption, and shared locking.
- `test_journal_migrate_v1.py` covers determinism, once-only behavior,
  source-order identities, complete refusal atomicity, index rebuild, and proof
  that runtime imports do not load the v1 reader.
- `test_cli.py` covers every shipped CLI command and its misuse/refusal surface.

Castle’s hermetic network guard remains active and tested.

## 10. Independent consumer integration

Consumers select an authorized store root before invoking Castle and import the module that owns the operation. A dependency upgrade and any persisted-format conversion require explicit consumer validation and migration scope. Castle's gate proves only Castle's candidate; no upstream test result qualifies a consumer's live data or authorization boundary.
