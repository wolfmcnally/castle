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

First submit a malformed selection container as one visible action. An empty list is valid and would advance the epoch while retaining every object:

```python
excise_store(old, new, cas_ids="invalid-container", operation_id=operation, expected_source=identity)
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

## Release

Version 0.2.1 corrects Delete/Excise behavior on top of the namespace capabilities introduced in 0.2.0, under the [MIT license](LICENSE). GitHub Releases records publication status. The [changelog](CHANGELOG.md) states the release scope, and the [release procedure](RELEASE.md) covers local artifact creation and a disposable clean-install smoke check. Tagging and publication require the maintainer’s approval of the exact artifacts and supported capability scope.
# Namespaces

The Python namespace library provides an explicitly initialized, single-coordinator tree over immutable CAS content. It supports directory creation, existing-content binding, new-byte file creation and replacement, read/list/resolve/status, file and directory rename/move, atomic subtree duplication and logical deletion. Copies share immutable content; deleting a namespace entry does not delete CAS bytes, retained snapshots or history. No mounted filesystem is provided.

Run the disposable workflow with the repository's Python entry point:

```sh
./bin/python project/examples/namespace_demo.py
```

The workflow uses public APIs and temporary synthetic stores. Its caller fixture supplies the `AuthorizedSession` protocol and declares an unprotected local environment; that declaration is not authentication, encryption or proof of a protected deployment. Applications must supply trusted authorization/capability evidence and one lifetime ownership context for each protection domain. Concurrent calls through that owner serialize; competing coordinator processes refuse. Direct SQL access and independent CAS writers while the coordinator is active are unsupported. Fork children must close inherited domain-ownership references; `castle.namespace_environment.LocalSession` and Castle's coordinator close their own inherited descriptors.

Mutations include an expected revision and operation UUID. A successful mutation, including a no-op rename, advances one revision. Retry the identical token/request after a lost response; never infer permission to use a fresh token. Changed requests under the same token refuse. Domain rejections, including revision conflicts, are retained without changing the tree. Results remain available until explicit maintenance expires eligible outcomes after seven days; reservations never recycle. Expiration removes logical payloads, not forensic remnants.

Names use NFC and casefold comparison, 255 UTF-8-byte components and the recorded Unicode-data version. Metadata accepts bounded integers, not floating-point or Decimal values; decimal quantities can be strings. Requests are bounded to 16 MiB, complete trees to 100000 entries and 64 MiB canonical capture bytes. Oversized recursive operations refuse atomically. This bounded implementation traverses the current tree and retained outcomes for validation; it is not a claim of constant-time operations at maximum capacity.

Acknowledgment follows SQLite DELETE-journal/EXTRA commit and durable owned evidence, never merely visible rows. The runtime sets and reads back `fullfsync=ON`; actual power-loss guarantees still depend on the qualified filesystem, SQLite VFS and hardware. The tests distinguish process termination, a genuine spilled hot rollback journal, before/after commit, response loss and injected wrapper sync/SQLite error boundaries. Python fault injection does not intercept every native VFS call, and kills do not prove hardware power-loss durability.

Opening validates exact CAS member/epoch and content authority and refuses unknown residue. An interrupted prepared intent, incomplete receipt/expiry cleanup or observed storage fault makes ordinary open refuse before evidence cleanup; `recover_namespace` with a newly authorized environment must establish fresh durable SQL/evidence before readiness. Recovery uses the durable SQL ownership recipe committed before operation-file allocation; only missing or exact-prefix interrupted writes in its closed slots can be repaired. Different bytes and unowned residue refuse. The slow recovery path may rewrite retained logical rows in a single transaction; it does not replace the database file, purge old pages or claim confidential cleanup. Persistent errors remain unavailable. Interrupted initialization before its completion receipt refuses as incomplete authority; it is not silently re-created. Disposition of an incomplete real namespace root is an explicit owner/storage-repair responsibility, not an automatic library deletion.

New-byte file creation/replacement, atomic frozen folder admission, explicit canonical snapshots and namespace-aware epoch excision use the same single domain coordinator. Excision uses a separate blocked cutover/replacement protocol described below; ordinary logical deletion and result expiry do not invoke it. Protected execution, cross-domain atomicity, real-store conversion, publication and broad platform qualification are not established by these local synthetic proofs.

## Owned content publication prerequisite

`castle.cas_recovery` provides `publish_namespace_content(cas_root, operation_root, expected_identity, publication)` and `recover_namespace_publication` with the same arguments. `PublicationRequest` is a frozen runtime recipe containing namespace, operation, publication and staged-file UUIDs, the exact `StoreIdentity` binding, CasID and byte size. The caller must already own the protection-domain admission fence, durably reserve this recipe, and supply a regular single-link read-only stage named by its UUID inside the operation directory. A path or UUID grants no authorization. The publisher takes the existing CAS stable lock after that fence; it starts no writer/copy subprocess.

The publisher saves exact operation-owned prefix/event/remote evidence before CAS effects, copies and seals the new object, synchronizes its journal events and derived index, then atomically saves complete evidence. Only then does it return the closed `PublicationReceipt`. Retrying or recovering the same recipe preserves the event identities and original receipt, including when later valid journal events exist. Recovery rewrites only its saved event byte range and preserves subsequent bytes. It validates the complete store before and after publication, so this bounded implementation reads all stored object bytes while holding the writer lock. Recovery and replay retain their complete authority checks.

Missing or corrupt evidence, unrelated tails/residue, changed member/epoch, or unreadable/unexplained changed remote custody refuse with `namespace-storage-recovery-required`. Duplicate target-local remote rows are preserved. The low-level publisher conservatively reports the fixed recovery refusal even for invalid input before effects; a namespace caller must validate request shape, immutable source bytes and admission before entering publication. Evidence-file presence alone is not proof of whether effects occurred. Incomplete publication recovery establishes fresh writes from the verified stage and events; later visibility or a fresh descriptor's successful sync does not retroactively confirm a failed call. Persistent storage faults remain unavailable. A partial initial evidence record has no repair authority and refuses before CAS effects. Keep the stage and publication record until the owning namespace operation resolves; their later retirement does not authorize deleting CAS content.

This prerequisite is qualified on synthetic local stores with real process termination and injected I/O faults. It supplies neither hardware power-loss certification nor automatic garbage collection. The namespace coordinator integrates this publisher for new-byte file creation and replacement; calling the publisher alone does not create a namespace entry. Folder transactions, captures and excision's successor-bound replacement manifests reuse this publisher. Durable unreferenced objects do not authorize garbage collection.


The namespace handle is process-local. Fork children release inherited lock descriptors and cannot use the handle; the qualified CPython callback deliberately pins the inherited SQLite object so child finalization cannot close or roll back the parent’s connection. Children should exec or exit promptly. A known blocked live handle reports operation status `recovery-required` with next action `recover`, without reading uncertain rows or inventing a terminal outcome. Unknown/unopened authority returns only a fixed safe refusal.

## New-byte namespace files

Pass immutable bytes separately from a closed request: `execute_namespace(handle, request, {cas_id: payload})`. `create-file` supplies the declared content identity/size with its entry ID, parent, name and metadata; `replace-content` has exactly entry_id and content. Replacement preserves the file ID, name, parent and metadata. The source mapping is captured once, exact-type bytes are checked against SHA-256 and size, and source transport is excluded from the request fingerprint. Mutable buffers/path handles refuse input-unfrozen; mismatched bytes refuse content-mismatch; malformed/unused keys refuse request-invalid. Those transport refusals reserve no operation UUID, so corrected bytes may retry it. Expected binding/revision is checked before inspecting or hashing fresh source transport. Empty sources retain existing-content binding semantics: a missing verified object is a durable content-mismatch rejection under that UUID. A matching source for the declared content is used/validated even if the object is already durable, without a duplicate CAS event.

Retained results are resolved before source transport or stale-revision checks; identical committed retry needs no bytes. New input has an independently durable SQL staging reservation before any file allocation. Only complete sealed/synchronized bytes become a prepared intent; the validated publisher receipt precedes atomic references, dependencies, result and revision commit. Explicit recovery completes verified input once, or durably rejects a missing/short pre-intent stage as input-unfrozen. A torn initial publication record or missing/corrupt required prepared stage remains blocked and preserved. The runtime cannot recover an undecodable first publication record: its saved prefix/event identities are unavailable, and filename/absence of a temporary or sighting is not repair authority. This boundary requires explicit owner/storage intervention if encountered on real authority; none is performed by this synthetic qualification. Committed outcomes win over cleanup residue; a saved SQL publication receipt validates already-removed staging without fabricating it. Cleanup finishes before the outer durable completion receipt. Unknown files or changed bytes never become automatic cleanup authority.

Run `./bin/python project/examples/namespace_demo.py` for the synthetic public workflow: it creates genuinely new bytes, duplicates a subtree, replaces only the original while preserving its identity, and checks the copy retains the original bytes. It also exercises empty directories, relocation, logical deletion and identical-request retry/status. The extended folder/snapshot workflow finishes at revision 12. Source/header/provenance and the single protection-domain owner remain caller responsibilities; no live-store or protected deployment claim follows. Platform, consumer and release qualification remain separate.

## Frozen folders and explicit snapshots

`import-folder` accepts exactly `parent_id` and a complete immutable `entries` array. Every entry supplies `entry_id`, `kind`, `parent_id`, `name`, `metadata`, `cas_id` and `size`; directories explicitly carry null content fields, including empty folders. All incoming parents are the chosen destination or incoming directories. Input order is normalized by entry UUID. Pass matching exact bytes separately by CasID; existing verified content needs no new event. Duplicate-content sizes must agree. Missing new folder bytes refuse as input-unfrozen before reservation; malformed or changed bytes refuse without a partial forest. All required stages are durable before any publication, and every live entry/result/dependency appears in one commit. Multi-payload recovery repairs the last started publication before validating earlier complete ones. The retained per-payload publisher performs repeated full-store validation, so large imports can incur quadratic work; no smaller product limit or batch-performance qualification is claimed.

`snapshot` accepts exactly `snapshot_id`. It captures the complete live tree at revision R, publishes its canonical immutable manifest and catalog reference at R+1, and returns a `SnapshotRecord`. The manifest preserves explicit empty directories and distinguishes capture from a redacted historic subset; it is separate from ordinary source documents. Identical retry returns that original capture. `delete-snapshot` removes only the live catalog reference, retains the UUID and payload/member dependencies, and returns no snapshot value. Missing/deleted reference reads or deletion refuse entry-missing; logical deletion and result expiry imply neither garbage collection nor confidential removal. Authorized historical ownership is available through inventory-excision; ordinary listing remains live-only.

`read_namespace(handle, request, destination=adapter)` accepts a destination only for `export-snapshot`; `inspect-snapshot` returns a `SnapshotView` with the catalog record and verified manifest. A destination implements `publish(view, manifest_bytes, read_content)` and returns `ExportReceipt(snapshot_id, cas_id)`. Its byte reader permits only captured file CasIDs during that callback on its thread, checks authorization for every read, and expires on return. The SQL read finishes before destination I/O while admission remains fenced; reentrant handle calls refuse. The destination owns permitted filesystem paths, link/collision refusal, temporary publication and cleanup. An I/O failure may leave a partial external export; it does not mutate or roll back namespace state. The explicit `castle.namespace_environment.DirectoryDestination` reference adapter and `namespace export-snapshot --out` provide this unprotected local boundary; destination ancestors must be physical, symlink-free paths.

`namespace_onboarding.inspect_seed(cas_root, session)` inventories all authorized source sightings without initializing namespace state or selecting current versions. It reports version/source/normalized-path ambiguities and unknown historic directory membership. Reserved namespace provenance remains explicitly namespace-owned rather than original-upload evidence. `admit_seed(handle, request)` binds a caller-selected complete existing-content import into explicitly initialized revision-zero authority using the same public transaction; it rejects control manifests as source documents. Initialization and seed admission are separate outcomes. Replaying the original seed remains safe, while a namespace emptied after earlier mutations cannot be reseeded. Real-store conversion, unknown policy/version conversion and protected deployment still require their own authority and evidence.


Seed inventory observations are raw reconstructed associations, not recommended seed candidates or proof of original upload membership. The namespace-owned label uses the reserved source-root prefix as its observable proxy. The optional seed helper conservatively refuses a payload whose decoded JSON declares the known snapshot schema; a legitimately archived manifest document triggers that same rule. This is a narrow seed-admission choice, not a claim about author intent or a general content classification rule. No observation is automatically selected or discarded, and generic namespace file admission does not infer document roles from content.

Export destination code runs after the CAS stable lock is released while lifetime domain ownership and the admission mutex remain held. The byte reader is restricted to the owning process and thread and becomes unusable on return. A refused byte read, even if destination code catches it and returns a nominal receipt, prevents success; detected backing corruption also blocks the live handle. Destination exceptions can leave partial external output. Local cleanup reuses one locked journal/remote snapshot, verifies shared existing content once per unique binding in each planning pass, and discards completed pending publication/capture caches; full per-payload publisher validation and receipt-prefix checks still have scaling costs.

## Explicit namespace epoch excision

Use `execute_namespace(handle, request, {}, excision_roots)` with kind `excise` and exactly `{"predecessor_root": Path(...), "successor_root": Path(...)}`. The active handle/session binds the predecessor; the distinct successor path is explicit caller-authorized transport. The closed request args are `selection` and `snapshot_actions`. Selection contains unique lists `content_ids`, `entry_ids`, `operation_ids` and `metadata_entry_ids`; content_ids may be empty when another selection array is nonempty, and every selected content identity must be verified. Selecting a live or deleted directory expands its entire retained descendant closure atomically. Deleted entry payloads, shared content owners, retained operation requests/results/errors/recipes and snapshot dependencies must be covered explicitly; uncovered disclosure refuses before retirement without silently adding identities. Metadata selection clears only the selected owner's metadata, including deleted rows. It leaves that entry's name, identity and live/deleted state intact. Cleared live rows receive the transition revision; deleted rows retain their prior changed revision. The root cannot be removed or cleared.

Every current catalog row, including a reference-deleted snapshot, requires exactly one explicit action. A drop is `{"snapshot_id": UUID, "action": "drop"}`; replacement adds `"action": "replace"`, `remove_entry_ids` and `clear_metadata_entry_ids`. Replacement preserves the catalog UUID, original captured revision, permitted changed revisions and live/deleted flag; its manifest is a verified retained rooted subset with redacted origin bound to the completed excision. Empty remove/clear lists permit a lossless epoch rebind. Drop retains the UUID reservation and does not itself delete immutable CAS bytes. Select an original manifest's CasID when it discloses removed content or metadata. A retained snapshot-deletion token stores its original manifest association independently of the current catalog, so rebinding the catalog does not change that token's outcome or dependencies. Dropping a catalog payload requires explicit retirement of each token whose request/result/recipe retains that snapshot record, including its capture and deletion tokens.

Excision serializes with reads, mutations and admitted export callbacks under the lifetime domain/coordinator owner. It commits a private root/selection/recipe intent and blocks ordinary service before allocating operation files or retiring CAS. It uses the delivered CAS excision/recovery API with its own lock ordering, publishes successor-bound manifests durably, then commits all reference/binding/result effects at one new namespace revision. References-published is internal unavailable state. It constructs a fresh SQLite generation from retained logical rows, preserving page size and permanent UUID reservations; selected payloads never enter the new allocator. The complete serialized image has a privately durable size/digest before its exact-prefix file publication. File and directory synchronization, validated image installation, independent retained-authority and selected-CasID checks, owned witness disposal and completion all precede acknowledgment. No page-copy, row deletion, visible rename or later successful sync establishes confidential completion by itself.

The fixed selection-free replacement marker binds source/target database generations, predecessor/successor CAS identities, operation and image digest through blocked, image-ready, database-published, cleanup-scanned, cleanup-verified and complete stages. Recovery handles either rename gap; unknown files, links, generations, roots or changed bytes refuse. Source-generation SQL remains the private cleanup witness until selected operation files and recipes are durably gone, then is itself disposed. Historical permitted outcomes remain unchanged and are checked against their original validated epoch interval; recovery never replays their old CAS publication against the successor. Completed excision status is retired with a reconstructed result containing a null fingerprint, empty affected IDs, no snapshot and the saved transition binding/revisions. Re-executing its UUID refuses operation-retired without performing a second excision.

After any interrupted cutover, ordinary open refuses. Call `recover_namespace(namespace_root, predecessor_root, predecessor_session, recovery_roots)` with exactly predecessor_root, successor_root, operation_id, expected_predecessor and action. Both roots are Path values, expected_predecessor is the original StoreIdentity, and action is resume or abort-before-retirement. Resume uses the actual retirement binding, saved sanitized bytes and exact private root commitments; once retired it only moves forward. Successful cutover closes the old handle; ordinary use reopens explicitly on the active successor with a new matching session. Repeat completion recovery/status uses safe completion evidence after private selection is gone. Historical status remains available on current explicitly supplied authority; recovery refuses rather than guessing the location of a later successor.

The SQL completion preserves the original predecessor and successor root commitments after private intent disposal. Each uses the existing SHA-256 of the canonical resolved-root string, including its final LF; the role columns and validated namespace/operation/generation/epoch chain distinguish their use, without an added hash domain prefix. A different same-ID replica path, missing proof, malformed/NULL commitment or old schema refuses before CAS payload reads or cleanup. No proof is inferred from current arguments and no old schema is migrated. These unsalted equality witnesses permit offline guessing and linkage of predictable paths; they provide neither path secrecy nor authentication. Completion retains no removed-selection fingerprint, selected content IDs, names or private plaintext.

Abort requires a still-consistent predecessor and proof under the same ordered locks that no retirement or uncertain retirement temporary exists. It records its action before cleanup, restores the prior complete marker or durable absence, then records a durable rejected token at the unchanged revision. Interrupted abort resumes that action; neither an absent retirement file alone nor a readable source authorizes rollback. A bound building/prepared successor remains fenced caller-owned residue, never automatically deleted or adopted. Unknown unbound destinations, corrupted critical recipes or unexplained residue require caller/storage intervention. Persistent I/O or authorization failure remains unavailable.

Dependency classification follows semantic ownership. Equal innocent names/metadata on another retained owner and arbitrary substrings in retained document bytes do not authorize deleting that owner. Selected CasID literals in retained entry names, persisted casefolded comparison keys, metadata or sanitized snapshot recipes refuse before intent; callers must explicitly remove or clear those owners. Exact selected CasID literals or ambiguous removed ownership metadata (including keys and canonical scalar values) in retained journal/remotes can conservatively refuse even an innocent archived reference; the caller must resolve that refusal explicitly. Synthetic tests independently choose removed-only sentinels, inspect logical inventories and exercise actual child termination plus wrapper sync/commit failures. They do not prove native VFS coverage, hardware power-loss safety, physical erasure, protected deployment, external export cleanup, backups, disconnected replicas or key destruction. Direct independent CAS writers must remain excluded by the caller's common protection-domain admission contract; no distributed atomicity or external custody is implied.

## Namespace commands and local environment

Version 0.2.1 includes the namespace capabilities. The library and `castle namespace` share the same typed requests, transaction authority and refusal/recovery behavior. See [environment conformance](NAMESPACE_ENVIRONMENT.md) for admission, backup, plaintext/key lifetime, export, resource and qualification obligations.

The CLI takes `castle namespace --root CAS --namespace-root NS --domain-lock LOCK VERB`. `identity` requires only an existing active CAS root and returns its full StoreIdentity without initializing namespace state. All other verbs require the separate namespace root and external domain lock. For a new empty CAS, run ordinary `init` then `rebuild-index` before namespace initialization, while no namespace owner is active. `namespace init --request-file FILE` consumes the exact InitializeRequest JSON, including schema castle.namespace_initialize.v1, lowercase UUIDv4 namespace_id/root_id, expected_cas, the current policy_version/unicode_data_version and protection={kind:unprotected}. The reference environment provides trusted unprotected Unix custody, not authentication or encryption. Protected mode refuses instead of falling back.

`info` returns current state and `status UUID` returns a retained operation's state/result. `read`, `list`, `resolve`, `inspect-snapshot`, `export-snapshot`, `inventory-excision` and `preview-excision` consume an exact ReadRequest JSON in `--request-file` with schema castle.namespace_read_request.v1, namespace_id, expected_cas, at_revision, kind and args. Their kinds are get-entry, list-directory, resolve, inspect-snapshot, export-snapshot, inventory-excision and preview-excision respectively. Export additionally requires `--out` naming an absent caller-authorized directory, with control snapshot.json beside member-only tree/. There is no generic content-read request kind; immutable CAS reads still need the host's same admission policy/fence.

Mutation verbs are create-directory, create-file, replace-content, rename, move, duplicate, delete, delete-subtree, set-metadata, import-folder, snapshot, delete-snapshot and expire-results. Each takes `--request-file` containing exactly schema castle.namespace_request.v1, namespace_id, expected_cas, expected_revision, operation_id, kind and args. Rename/move require kind relocate; delete requires delete-entry. Other names equal their typed kind. Command/kind mismatch refuses; the CLI never rewrites intent or refreshes a saved revision/token. `--content CasID=FILE` may repeat for new file/folder bytes; alternatively `--content-file FILE` names a CasID-to-path JSON object. Both are lazy, so a retained successful token replays before checking missing source files. Folder admission needs a complete frozen entry inventory, including empty directories; this is not a live recursive source-tree snapshot command. Sources remain caller-owned and memory/resource policy is the consumer's responsibility.

`recover --request-file FILE` consumes JSON null for ordinary recovery, or the exact explicit cutover record: predecessor_root, successor_root, operation_id, expected_predecessor and action (resume or abort-before-retirement). The predecessor must equal the explicit --root after resolution, and completed recovery requires the original recorded root hashes. Recovery discovers no root from metadata, infers no missing proof and reactivates no retired predecessor. Excision stays library-only; logical namespace deletion never moves the CAS epoch or authorizes GC.

Namespace errors go to stderr as the exact five-field safe JSON code/namespace_id/operation_id/actual_revision/effect record, exit 2. No private path/selection/backend exception is printed. Output failure exits 74 and interruption exits 130 with an unknown response outcome; query the original operation token. Argument-parser usage errors remain separate. A partial authorized export can remain after an error and is never automatically purged.

The existing direct CAS writers/maintenance do not acquire the namespace domain lock. They are unsupported concurrently with an active namespace owner; CAS-only use remains valid. One command holds one lifetime admission, competing coordinators refuse busy, and all callers must agree on the same stable external lock. Cross-domain moves remain external orchestration with no implied distributed atomicity.

For an interactive synthetic workspace that persists after the example exits:

```sh
./bin/python project/examples/namespace_demo.py --workspace .runtime/namespace-demo
```

The workspace must be absent. Its COMMANDS.txt gives directly runnable commands and request files whose expected revisions match the printed sequence, including new-byte replacement, move and snapshot inspect/export. If changing that sequence, inspect current info and set the expected revision for each new intent, preserving the exact original request for a retry. Try folder rename/move, stable-ID replacement, empty-folder snapshot/export, case collision and stale revision. The default example still runs its complete automated workflow in temporary roots. Manual usability, protected deployment, physical erasure and hardware power-loss qualification remain separate from automated local delivery.

Destination-choice errors, including existing outputs or symlinked/non-directory ancestors, return namespace-root-unsafe; genuine destination I/O faults return namespace-io-failed and may leave partial output. On macOS use the physical path (for example the retained demo workspace), since /tmp and /var are normally symlink aliases. Authority overlap checks compare opened directory identities as well as lexical paths, including case-insensitive filesystem aliases. Request transport is strictly UTF-8. Responses use ASCII JSON escapes so configured ASCII streams preserve decoded Unicode names.


### Delete, Excise and historical closure

Delete (`delete-entry` or `delete-subtree`; CLI `delete` or `delete-subtree`) makes entries disappear from live listings atomically. It preserves CAS bytes, deleted entry payloads and operation logging. Excise is the explicit-root library mutation `excise`: it removes the selected managed content and entry/metadata payloads, reconstructs retained namespace authority and advances the existing CAS epoch. It unlinks owned files; it does not overwrite physical media, erase filesystem snapshots/backups/external copies, or revoke already-open external descriptors. Anonymous permanent UUID reservations and selection-free transition/root-equality evidence remain under the existing safety contract.

The closed selection has `content_ids`, `entry_ids`, `operation_ids` and `metadata_entry_ids`, each a unique sorted array after normalization. At least one array must be nonempty. Content IDs select global immutable bytes; selected entry IDs remove their complete live/deleted descendant closure; metadata IDs clear only the complete metadata objects at remaining entries. Content-free empty-directory or name/metadata cleanup still advances the epoch. To remove file bytes as well as file metadata, explicitly select their content IDs and every shared entry owner: entry removal alone does not authorize content removal. Missing shared owners, historic disclosures or snapshot actions refuse before intent/retirement; no implicit widening or automatic GC occurs.

The typed read kind `inventory-excision` takes empty args and returns `ExcisionInventory`: `entries` contains `HistoricalEntry(entry: EntryRecord, live: bool)` including deleted descendants; `snapshots` contains `HistoricalSnapshot(view: SnapshotView, live: bool)` including deleted catalogs and verified membership; `operations` contains payload-bearing `HistoricalOperation(operation_id, state)`; `dependencies` contains `ContentDependency(owner_kind, owner_id, cas_id, role)`; and `content_ids` lists verified stored identities. Payload-free retired reservations are not inventory owners. Each collection and the aggregate snapshot-member pairs are separately limited to 100000, and the whole canonical value to 64 MiB. Overflow refuses, never silently truncates; these finite read bounds do not promise inventory of unlimited history. Ordinary `get-entry`, `list-directory` and `resolve` remain live-only.

The read kind `preview-excision` requires an explicit current `at_revision` and args exactly `operation_id`, `selection`, `snapshot_actions`. The future operation UUID is caller-owned and unreserved. `ExcisionPreview` has `request`, `entry_ids`, `metadata_entry_ids`, `conflicting_content_ids`. When the explicit content/shared-entry/snapshot choices are closed, preview expands deleted descendants, completes disclosing operation owners, and returns the normalized typed `MutationRequest` for explicit library execution. It never adds content IDs, shared entry owners or snapshot actions. When conservative retained journal/remotes metadata matches or selected content-ID literals in retained journal/remotes fields or CAS objects conflict, `request` is null and the authorized result names only conflicting retained content IDs. Equal strings can be innocent: this diagnostic is not permission or advice to remove those bytes. Selected content-ID literals in retained entry names, casefolded comparison keys, metadata or sanitized recipes instead raise namespace-dependency-conflict before intent in both preview and execution; explicitly remove or clear those owners. Execution retains its fixed dependency refusal without exposing paths. Other incomplete/invalid input refuses and leaves a healthy handle usable; real backing corruption remains fail-closed.

Every retained catalog snapshot, including deleted and unaffected records, needs an explicit drop or replace action. A replace includes `remove_entry_ids` and `clear_metadata_entry_ids`, preserves retained membership and the original captured revision, rebinds to the successor, and labels the view redacted even when its entries are unchanged. All replacement members consume the existing excision owner-pair bound. Disclosing original manifest bytes must also be explicitly selected for removal. Inventory provides the actual catalog/member information; callers decide their intended snapshot policy.

Both queries require `excision-inventory` authorization before UUID lookup/history access and `excision-inventory-result` before return, in addition to ordinary read admission. The existing shared excision preflight also requires both kinds before execution or pre-retirement staging/abort recovery proceeds with private closure; the new reads reuse these permissions and do not remove that safety boundary. They use the same serialized coordinator/read transaction and do not reserve a token, change revision, write SQL/CAS, create files or grant execution. Inventory/preview outputs disclose historical names, metadata, IDs, relationships and hashes to that authorized caller; protect any saved output. Preview performs full-store validation while holding exclusive admission. Content-free preview skips the impossible literal-content-ID scan, not integrity or metadata-disclosure checks.

Execute only a nonnull returned request, with the explicit original predecessor and absent successor roots. Execution revalidates every closure input. A changed successful revision requires refreshing the request. Rejected/aborted operations can add disclosing history without advancing revision, so dependency-conflict also requires a fresh preview, possibly at the same revision. A preview is not a reservation or an execution guarantee. Completed excision retires its token; normal status on the successor returns the safe retired outcome. Re-execution refuses operation-retired. Explicit-root recovery supplies the original predecessor as its `cas_root` argument, even after completion, and both original roots/expected predecessor; repeated recovery never advances the epoch again.

CLI `namespace inventory-excision` and `namespace preview-excision` use the existing `--request-file` `ReadRequest` transport and produce the same `ReadResult` values as the library. No destructive excision CLI is added. The explicit-root library remains the execution boundary; the CLI already supports explicit-root recovery. Version 0.2.1 corrects empty saved-selection admission, deleted descendants, typed operation-link cleanup and preliminary historical-snapshot validation; later full CAS integrity validation remains mandatory.

Conservative journal/remotes matching includes metadata keys, string values and canonical scalar values, using whole-field or slash-component equality. Common keys such as docs or values such as 1 may match innocent source paths and block a metadata-only clear or entry purge. There is no in-scope override: only an independently authorized explicit content selection can remove those sources. A conflict is neither permission nor advice to destroy unrelated bytes.
