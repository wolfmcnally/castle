"""Canonical explicit historic views and retained catalog dependencies."""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

from castle import cas
from castle import namespace as n

if TYPE_CHECKING:
    from castle.namespace_store import NamespaceHandle


def manifest(info: n.NamespaceInfo, entries: dict[str, n.EntryRecord]) -> dict[str, Any]:
    return {
        "schema": "castle.namespace_snapshot.v1",
        "namespace_id": info.namespace_id,
        "policy": info.policy,
        "captured_revision": info.revision,
        "root_id": info.root_id,
        "cas_binding": asdict(info.cas),
        "origin": {"kind": "capture"},
        "entries": [asdict(entries[key]) for key in sorted(entries)],
    }


def capture(
    handle: NamespaceHandle, identity: str, *, check_content: bool = True
) -> tuple[n.SnapshotRecord, bytes]:
    from castle.namespace_store import _tree

    info, entries = handle.load_info(), handle.entries()
    _tree(info, entries)
    for entry in entries.values():
        if (
            check_content
            and entry.cas_id
            and handle.objects.get(entry.cas_id, (None, None))[1] != entry.size
        ):
            n._refuse("namespace-content-mismatch")
    raw = n.canonical_bytes(manifest(info, entries), limit=n.MAX_MANIFEST_BYTES)
    return n.SnapshotRecord(
        identity, "sha256:" + hashlib.sha256(raw).hexdigest(), len(raw), info.revision
    ), raw


def catalog(handle: NamespaceHandle, identity: str, *, live: bool = True) -> n.SnapshotRecord:
    n._uuid(identity)
    row = handle.db.execute(
        "SELECT snapshot_id,cas_id,size,captured_revision,live FROM snapshots WHERE snapshot_id=?",
        (identity,),
    ).fetchone()
    if row is None or (live and not row[4]):
        n._refuse("namespace-entry-missing")
    return n.SnapshotRecord(*row[:4])


def decode(
    handle: NamespaceHandle,
    record: n.SnapshotRecord,
    *,
    check_content: bool = True,
    expected_origin: dict[str, str] | None = None,
    expected_binding: n.StoreIdentity | None = None,
) -> dict[str, Any]:
    try:
        return _decode_manifest(
            handle,
            record,
            check_content=check_content,
            expected_origin=expected_origin,
            expected_binding=expected_binding,
        )
    except (n.NamespaceError, OSError, TypeError, KeyError, ValueError):
        n._refuse("namespace-storage-recovery-required")


def _decode_manifest(
    handle: NamespaceHandle,
    record: n.SnapshotRecord,
    *,
    check_content: bool = True,
    expected_origin: dict[str, str] | None = None,
    expected_binding: n.StoreIdentity | None = None,
) -> dict[str, Any]:
    from castle.namespace_store import _policy, _regular, _tree

    n._snapshot(asdict(record))
    file = cas.object_path(handle.cas_root, record.cas_id)
    _regular(file)
    raw = file.read_bytes()
    if len(raw) != record.size or "sha256:" + hashlib.sha256(raw).hexdigest() != record.cas_id:
        n._refuse("namespace-storage-recovery-required")
    value = n._decode(raw, limit=n.MAX_MANIFEST_BYTES)
    info = handle.load_info()
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "namespace_id",
        "policy",
        "captured_revision",
        "root_id",
        "cas_binding",
        "origin",
        "entries",
    }:
        n._refuse("namespace-storage-recovery-required")
    _policy(value["policy"])
    n._integer(value["captured_revision"])
    parsed_binding = n.StoreIdentity(**value["cas_binding"])
    if (
        value["schema"] != "castle.namespace_snapshot.v1"
        or value["namespace_id"] != info.namespace_id
        or value["policy"] != info.policy
        or value["root_id"] != info.root_id
        or parsed_binding != (expected_binding or info.cas)
        or value["captured_revision"] != record.captured_revision
        or record.captured_revision > info.revision
    ):
        n._refuse("namespace-storage-recovery-required")
    origin = value["origin"]
    if expected_origin is not None and origin != expected_origin:
        n._refuse("namespace-storage-recovery-required")
    if origin != {"kind": "capture"}:
        if (
            not isinstance(origin, dict)
            or set(origin) != {"kind", "operation_id"}
            or origin["kind"] != "redacted"
        ):
            n._refuse("namespace-storage-recovery-required")
        n._uuid(origin["operation_id"])
    rows = value["entries"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= n.MAX_ENTRIES:
        n._refuse("namespace-storage-recovery-required")
    entries = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {
            "entry_id",
            "kind",
            "parent_id",
            "name",
            "metadata",
            "cas_id",
            "size",
            "changed_revision",
        }:
            n._refuse("namespace-storage-recovery-required")
        entry = n.EntryRecord(**row)
        if entry.entry_id in entries:
            n._refuse("namespace-storage-recovery-required")
        entries[entry.entry_id] = entry
    if list(entries) != sorted(entries):
        n._refuse("namespace-storage-recovery-required")
    _tree(replace(info, revision=record.captured_revision), entries)
    if check_content:
        for entry in entries.values():
            if entry.cas_id and handle.objects.get(entry.cas_id, (None, None))[1] != entry.size:
                n._refuse("namespace-storage-recovery-required")
        if handle.objects.get(record.cas_id, (None, None))[1] != record.size:
            n._refuse("namespace-storage-recovery-required")
    return value


def validate_provenance(
    handle: NamespaceHandle,
    value: dict[str, Any],
    binding: n.StoreIdentity,
    *,
    allow_pending: bool = False,
) -> None:
    origin = value["origin"]
    if origin == {"kind": "capture"}:
        return
    row = handle.db.execute(
        "SELECT cas_binding,revision_after,completed FROM replacement_completion "
        "WHERE operation_id=?",
        (origin["operation_id"],),
    ).fetchone()
    if (
        row is None
        or n._decode(row[0]) != binding.to_dict()
        or type(row[1]) is not int
        or row[1] <= value["captured_revision"]
        or type(row[2]) is not int
        or row[2] not in ({0, 1} if allow_pending else {1})
    ):
        n._refuse("namespace-storage-recovery-required")


def catalog_view(
    handle: NamespaceHandle,
    record: n.SnapshotRecord,
    *,
    check_content: bool = True,
    allow_pending: bool = False,
) -> dict[str, Any]:
    value = decode(handle, record, check_content=check_content)
    validate_provenance(handle, value, handle.load_info().cas, allow_pending=allow_pending)
    return value


def dependencies(
    handle: NamespaceHandle, *, check_content: bool = True, allow_pending: bool = False
) -> set[tuple[str, str, str, str]]:
    reserved = {
        row[0] for row in handle.db.execute("SELECT snapshot_id FROM snapshot_reservations")
    }
    others = {row[0] for row in handle.db.execute("SELECT entry_id FROM entry_reservations")}
    others.update(row[0] for row in handle.db.execute("SELECT operation_id FROM operations"))
    others.update(handle.authority_ids())
    for identity in reserved:
        n._uuid(identity)
        if identity in others:
            n._refuse("namespace-storage-recovery-required")
    result = set()
    for row in handle.db.execute(
        "SELECT snapshot_id,cas_id,size,captured_revision,live FROM snapshots"
    ):
        record = n.SnapshotRecord(*row[:4])
        if record.snapshot_id not in reserved or type(row[4]) is not int or row[4] not in {0, 1}:
            n._refuse("namespace-storage-recovery-required")
        cached = handle.snapshot_cache.get(record.snapshot_id)
        if cached is None or cached[0] != record:
            view = catalog_view(
                handle, record, check_content=check_content, allow_pending=allow_pending
            )
            members = {e["cas_id"]: e["size"] for e in view["entries"] if e["cas_id"] is not None}
            handle.snapshot_cache[record.snapshot_id] = (record, members)
        else:
            members = cached[1]
        if check_content:
            for identity, size in {**members, record.cas_id: record.size}.items():
                if handle.objects.get(identity, (None, None))[1] != size:
                    n._refuse("namespace-storage-recovery-required")
        result.add(("snapshot", record.snapshot_id, record.cas_id, "manifest"))
        result.update(("snapshot", record.snapshot_id, identity, "member") for identity in members)

    return result


@dataclass(frozen=True)
class _RetainedSnapshot:
    snapshot: n.SnapshotRecord
    live: bool
    dependencies: list[str]


def _retained(handle: NamespaceHandle) -> list[_RetainedSnapshot]:
    with handle.admitted():
        n._authorize(handle.session, "snapshot-retention")
        result = []
        for identity, live in handle.db.execute(
            "SELECT snapshot_id,live FROM snapshots ORDER BY snapshot_id"
        ):
            record = catalog(handle, identity, live=False)
            view = catalog_view(handle, record)
            result.append(
                _RetainedSnapshot(
                    record,
                    bool(live),
                    sorted(
                        {record.cas_id}
                        | {e["cas_id"] for e in view["entries"] if e["cas_id"] is not None}
                    ),
                )
            )
        n._authorize(handle.session, "snapshot-retention-result")
        return result


def precondition(handle: NamespaceHandle, request: dict[str, Any]) -> None:
    identity = request["args"]["snapshot_id"]
    if request["kind"] == "delete-snapshot":
        catalog(handle, identity)
        return
    info = handle.load_info()
    if identity in handle.authority_ids() | {info.root_id, request["operation_id"]} or any(
        handle.db.execute(f"SELECT 1 FROM {table} WHERE {field}=?", (identity,)).fetchone()
        for table, field in (
            ("entry_reservations", "entry_id"),
            ("operations", "operation_id"),
            ("snapshot_reservations", "snapshot_id"),
        )
    ):
        n._refuse()


def commit(
    handle: NamespaceHandle, request: dict[str, Any], saved: dict[str, Any] | None
) -> n.SnapshotRecord | None:
    if request["kind"] == "delete-snapshot":
        handle.db.execute(
            "UPDATE snapshots SET live=0 WHERE snapshot_id=?", (request["args"]["snapshot_id"],)
        )
        return None
    if request["kind"] != "snapshot":
        return None
    record = n._snapshot(saved)
    handle.db.execute("INSERT INTO snapshot_reservations VALUES (?)", (record.snapshot_id,))
    handle.db.execute(
        "INSERT INTO snapshots VALUES (?,?,?,?,1)",
        (record.snapshot_id, record.cas_id, record.size, record.captured_revision),
    )
    return record


def export(
    handle: NamespaceHandle, view: n.SnapshotView, destination: n.SnapshotDestination
) -> n.ExportReceipt:
    import threading

    from castle.namespace_store import _regular

    record = view.snapshot
    allowed = {e["cas_id"]: e["size"] for e in view.manifest["entries"] if e["cas_id"] is not None}
    manifest_bytes = n.canonical_bytes(view.manifest, limit=n.MAX_MANIFEST_BYTES)
    active = True
    owner = threading.get_ident()
    failures: list[n.NamespaceError] = []

    def record_failure(error: n.NamespaceError) -> None:
        if active and not failures:
            failures.append(error)

    def read_content(identity: str) -> bytes:
        if (
            not active
            or handle.closed
            or handle.pid != os.getpid()
            or threading.get_ident() != owner
            or not isinstance(identity, str)
            or identity not in allowed
        ):
            error = n.NamespaceError("namespace-access-denied")
            record_failure(error)
            raise error from None
        try:
            n._authorize(handle.session, "export-source")
            file = cas.object_path(handle.cas_root, identity)
            _regular(file)
            raw = file.read_bytes()
            if (
                len(raw) != allowed[identity]
                or "sha256:" + hashlib.sha256(raw).hexdigest() != identity
            ):
                n._refuse("namespace-storage-recovery-required")
            n._authorize(handle.session, "export-source-result")
            return raw
        except n.NamespaceError as error:
            record_failure(error)
            raise
        except OSError:
            error = n.NamespaceError(
                "namespace-storage-recovery-required", effect="recovery-required"
            )
            record_failure(error)
            raise error from None

    n._authorize(handle.session, "export")
    handle.exporting = True
    try:
        receipt = destination.publish(view, manifest_bytes, read_content)
        if failures:
            raise failures[0] from None
        if type(receipt) is not n.ExportReceipt or receipt != n.ExportReceipt(
            record.snapshot_id, record.cas_id
        ):
            n._refuse("namespace-io-failed")
    except n.NamespaceError:
        raise
    except Exception:
        # The caller's destination may raise any ordinary I/O/adapter error.
        # Report fixed failure; partial external output is never namespace commit.
        n._refuse("namespace-io-failed")
    finally:
        active = False
        handle.exporting = False
    n._authorize(handle.session, "export-result")
    return receipt
