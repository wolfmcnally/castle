"""Atomic namespace tree changes and deterministic durable retry outcomes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from castle import cas
from castle import namespace as n
from castle import namespace_content as content_io
from castle import namespace_snapshots as snapshots
from castle import namespace_store as store

_ARGS = {
    "create-directory": {"entry_id", "parent_id", "name", "metadata"},
    "create-file": {"entry_id", "parent_id", "name", "metadata", "content"},
    "set-metadata": {"entry_id", "metadata"},
    "replace-content": {"entry_id", "content"},
    "relocate": {"entry_id", "parent_id", "name"},
    "duplicate": {"entry_id", "parent_id", "name", "identities"},
    "delete-entry": {"entry_id"},
    "delete-subtree": {"entry_id"},
    "expire-results": {"operation_ids"},
    "import-folder": {"parent_id", "entries"},
    "snapshot": {"snapshot_id"},
    "delete-snapshot": {"snapshot_id"},
    "excise": {"selection", "snapshot_actions"},
}


def _now() -> datetime:
    return datetime.now(UTC)


def normalize(request: n.MutationRequest) -> dict[str, Any]:
    if type(request) is not n.MutationRequest or type(request.expected_cas) is not n.StoreIdentity:
        n._refuse()
    try:
        value = n._decode(n.canonical_bytes(asdict(request)))
    except RecursionError:
        n._refuse("namespace-limit-exceeded")
    n._uuid(value["namespace_id"])
    n._uuid(value["operation_id"])
    n._integer(value["expected_revision"])
    kind, args = value["kind"], value["args"]
    if (
        not isinstance(kind, str)
        or kind not in _ARGS
        or not isinstance(args, dict)
        or set(args) != _ARGS[kind]
    ):
        n._refuse()
    for key in ("entry_id", "parent_id", "snapshot_id"):
        if key in args:
            n._uuid(args[key])
    if "name" in args:
        args["name"] = n._name(args["name"])
    if "metadata" in args:
        args["metadata"] = n._metadata(args["metadata"])
    if "content" in args:
        content = args["content"]
        if not isinstance(content, dict) or set(content) != {"cas_id", "size"}:
            n._refuse()
        if not isinstance(content["cas_id"], str) or n._CAS.fullmatch(content["cas_id"]) is None:
            n._refuse()
        n._integer(content["size"])
    if kind == "import-folder":
        incoming = args["entries"]
        if not isinstance(incoming, list) or len(incoming) > n.MAX_ENTRIES:
            n._refuse("namespace-limit-exceeded")
        for item in incoming:
            if not isinstance(item, dict) or set(item) != {
                "entry_id",
                "kind",
                "parent_id",
                "name",
                "metadata",
                "cas_id",
                "size",
            }:
                n._refuse()
            n._uuid(item["entry_id"])
            n._uuid(item["parent_id"])
            item["name"] = n._name(item["name"])
            item["metadata"] = n._metadata(item["metadata"])
            if item["kind"] == "directory":
                if item["cas_id"] is not None or item["size"] is not None:
                    n._refuse()
            elif item["kind"] == "file":
                if not isinstance(item["cas_id"], str) or n._CAS.fullmatch(item["cas_id"]) is None:
                    n._refuse()
                n._integer(item["size"])
            else:
                n._refuse()
        if len({item["entry_id"] for item in incoming}) != len(incoming):
            n._refuse()
        args["entries"] = sorted(incoming, key=lambda item: item["entry_id"])
        content_io.specs(value)
    if "identities" in args:
        identities = args["identities"]
        if not isinstance(identities, list) or not identities or len(identities) > n.MAX_ENTRIES:
            n._refuse("namespace-limit-exceeded")
        for pair in identities:
            if not isinstance(pair, dict) or set(pair) != {"source_id", "new_id"}:
                n._refuse()
            n._uuid(pair["source_id"])
            n._uuid(pair["new_id"])
        if len({p["source_id"] for p in identities}) != len(identities) or len(
            {p["new_id"] for p in identities}
        ) != len(identities):
            n._refuse()
        args["identities"] = sorted(identities, key=lambda p: p["source_id"])
    if "operation_ids" in args:
        values = args["operation_ids"]
        if not isinstance(values, list) or len(values) > n.MAX_ENTRIES:
            n._refuse("namespace-limit-exceeded")
        for identity in values:
            n._uuid(identity)
        if len(set(values)) != len(values) or value["operation_id"] in values:
            n._refuse()
        args["operation_ids"] = sorted(values)
    if kind == "excise":
        from castle import namespace_excision as cutover

        value["args"] = cutover.normalize(args)
    n.canonical_bytes(value)
    return value


def _row(handle: store.NamespaceHandle, operation_id: str) -> dict[str, Any] | None:
    cursor = handle.db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,))
    row = cursor.fetchone()
    return None if row is None else dict(zip([d[0] for d in cursor.description], row, strict=True))


def _raise_saved(handle: store.NamespaceHandle, row: dict[str, Any]) -> None:
    error = n._terminal_error(n._decode(row["terminal_error"]))
    n._authorize(handle.session, "acknowledge")
    raise error from None


def _replay(handle: store.NamespaceHandle, row: dict[str, Any]) -> n.MutationResult:
    if row["state"] == "retired":
        n._refuse("namespace-operation-retired")
    if row["state"] == "rejected":
        _raise_saved(handle, row)
    if row["state"] != "committed" or row["terminal_result"] is None:
        n._refuse("namespace-storage-recovery-required")
    result = n._result(n._decode(row["terminal_result"]))
    n._authorize(handle.session, "acknowledge")
    return result


def prepare(
    handle: store.NamespaceHandle,
    request: dict[str, Any],
    *,
    evidence_present: bool = False,
    publications=None,
    snapshot: n.SnapshotRecord | None = None,
    rejection: str | None = None,
) -> None:
    info = handle.load_info()
    if info.revision == n.MAX_INTEGER:
        n._refuse("namespace-revision-exhausted")
    identity = request["operation_id"]
    if (
        handle.db.execute(
            "SELECT 1 FROM entry_reservations WHERE entry_id=?", (identity,)
        ).fetchone()
        or handle.db.execute(
            "SELECT 1 FROM snapshot_reservations WHERE snapshot_id=?", (identity,)
        ).fetchone()
        or identity in handle.authority_ids()
    ):
        n._refuse()
    receipt: dict[str, Any] = {
        "receipt": handle._ownership(request, info),
        "expiry": {},
        "publication": [
            {"request": asdict(item), "receipt": None} for item in (publications or [])
        ],
        "snapshot": asdict(snapshot) if snapshot else None,
        "precondition_error": rejection,
    }
    if request["kind"] == "expire-results":
        for target in request["args"]["operation_ids"]:
            target_row = _row(handle, target)
            if target_row and target_row["state"] in {"committed", "rejected"}:
                directory = handle.root / "operations" / target
                receipt["expiry"][target] = {
                    "receipt": handle._receipt(target),
                    "request_sha256": hashlib.sha256(
                        (directory / "request.json").read_bytes()
                    ).hexdigest(),
                }
    content_io.check_capacity(receipt)
    encoded_receipt = content_io.encode_recipe(receipt)
    handle.db.execute("BEGIN IMMEDIATE")
    handle.operation_id = identity
    handle.write_started = True
    handle.effect = "recovery-required"
    handle.db.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity,
            n._fingerprint(request),
            "staging" if publications or rejection is not None else "prepared",
            info.revision,
            None,
            None,
            None,
            n.canonical_bytes(request).decode(),
            None,
            None,
            encoded_receipt,
            None,
        ),
    )
    store._commit(handle.db)
    if not evidence_present:
        handle._evidence(request, info)


def execute(
    handle: store.NamespaceHandle, request: n.MutationRequest, sources: Mapping[str, object]
) -> n.MutationResult:
    n._authorize(handle.session, request.kind if type(request) is n.MutationRequest else "execute")
    value = normalize(request)
    info = handle.load_info()
    if value["namespace_id"] != info.namespace_id:
        n._refuse("namespace-binding-mismatch")
    row = _row(handle, value["operation_id"])
    if row is not None:
        handle.operation_id = value["operation_id"]
        if row["state"] == "retired":
            n._refuse("namespace-operation-retired")
        if row["fingerprint"] != n._fingerprint(value):
            n._refuse("namespace-operation-conflict")
        return _replay(handle, row)
    rejection = (
        "namespace-binding-mismatch"
        if value["expected_cas"] != asdict(info.cas)
        else "namespace-revision-conflict"
        if value["expected_revision"] != info.revision
        else None
    )
    data = {}
    snapshot = None
    if rejection is None:
        data = content_io.source(handle, value, sources)
        try:
            _changes(handle, value, info, available={key: len(raw) for key, raw in data.items()})
            if value["kind"] == "snapshot":
                snapshot, raw = snapshots.capture(handle, value["args"]["snapshot_id"])
                data = {snapshot.cas_id: raw}
            elif value["kind"] == "delete-snapshot":
                snapshot = snapshots.catalog(handle, value["args"]["snapshot_id"])
        except n.NamespaceError as error:
            if error.code in {
                "namespace-storage-recovery-required",
                "namespace-protection-unsupported",
            }:
                raise
            rejection = error.code
    publications = content_io.new_requests(handle, value, data) if rejection is None else []
    prepare(handle, value, publications=publications, snapshot=snapshot, rejection=rejection)
    if snapshot is not None and value["kind"] == "snapshot":
        handle.pending_captures[(value["operation_id"], snapshot)] = data[snapshot.cas_id]
    if publications:
        for item in publications:
            content_io.write_stage(handle, item, data[item.cas_id])
        content_io.promote(handle, value["operation_id"])
    return finish(handle, value["operation_id"])


def _subtree(entries: dict[str, n.EntryRecord], identity: str) -> set[str]:
    children: dict[str, list[str]] = {}
    for entry in entries.values():
        if entry.parent_id is not None:
            children.setdefault(entry.parent_id, []).append(entry.entry_id)
    result: set[str] = set()
    pending = [identity]
    while pending:
        current = pending.pop()
        if current in result:
            n._refuse("namespace-cycle")
        result.add(current)
        pending.extend(children.get(current, []))
    return result


def _content(handle: store.NamespaceHandle, cas_id: str, size: int) -> None:
    row = handle.objects.get(cas_id)
    if row is None or row[1] != size:
        n._refuse("namespace-content-mismatch")
    file = cas.object_path(handle.cas_root, cas_id)
    if cas.sha256_file(file) != cas_id or not cas._write_protected(file.stat()):
        n._refuse("namespace-storage-recovery-required")


def _changes(
    handle: store.NamespaceHandle,
    request: dict[str, Any],
    info: n.NamespaceInfo,
    *,
    available: dict[str, int] | None = None,
) -> tuple[dict[str, n.EntryRecord], set[str]]:
    entries = handle.entries()
    kind, args = request["kind"], request["args"]
    revision = info.revision + 1
    affected: set[str] = set()
    verified_content = set()

    def verify_content(identity: str, size: int) -> None:
        if (identity, size) not in verified_content:
            _content(handle, identity, size)
            verified_content.add((identity, size))

    def entry(identity: str) -> n.EntryRecord:
        if identity not in entries:
            n._refuse("namespace-entry-missing")
        return entries[identity]

    def parent(identity: str) -> None:
        if entry(identity).kind != "directory":
            n._refuse("namespace-kind-mismatch")

    def fresh(identity: str) -> None:
        if (
            identity in handle.authority_ids() | {request["operation_id"]}
            or handle.db.execute(
                "SELECT 1 FROM entry_reservations WHERE entry_id=?", (identity,)
            ).fetchone()
            or handle.db.execute(
                "SELECT 1 FROM operations WHERE operation_id=?", (identity,)
            ).fetchone()
            or handle.db.execute(
                "SELECT 1 FROM snapshot_reservations WHERE snapshot_id=?", (identity,)
            ).fetchone()
        ):
            n._refuse("namespace-request-invalid")

    if kind in {"create-directory", "create-file"}:
        fresh(args["entry_id"])
        parent(args["parent_id"])
        content = args.get("content")
        if content is not None and (available or {}).get(content["cas_id"]) != content["size"]:
            verify_content(content["cas_id"], content["size"])
        added = n.EntryRecord(
            args["entry_id"],
            "file" if content is not None else "directory",
            args["parent_id"],
            args["name"],
            args["metadata"],
            content["cas_id"] if content else None,
            content["size"] if content else None,
            revision,
        )
        entries[added.entry_id] = added
        affected.add(added.entry_id)
    elif kind == "replace-content":
        source = entry(args["entry_id"])
        if source.entry_id == info.root_id:
            n._refuse("namespace-root-immutable")
        if source.kind != "file":
            n._refuse("namespace-kind-mismatch")
        content = args["content"]
        if (available or {}).get(content["cas_id"]) != content["size"]:
            verify_content(content["cas_id"], content["size"])
        entries[source.entry_id] = replace(
            source, cas_id=content["cas_id"], size=content["size"], changed_revision=revision
        )
        affected.add(source.entry_id)
    elif kind in {"relocate", "set-metadata", "delete-entry", "delete-subtree", "duplicate"}:
        source = entry(args["entry_id"])
        if source.entry_id == info.root_id and kind != "duplicate":
            n._refuse("namespace-root-immutable")
        membership = _subtree(entries, source.entry_id)
        if kind == "relocate":
            parent(args["parent_id"])
            if args["parent_id"] in membership:
                n._refuse("namespace-cycle")
            entries[source.entry_id] = replace(
                source, parent_id=args["parent_id"], name=args["name"], changed_revision=revision
            )
            affected.add(source.entry_id)
        elif kind == "set-metadata":
            entries[source.entry_id] = replace(
                source, metadata=args["metadata"], changed_revision=revision
            )
            affected.add(source.entry_id)
        elif kind in {"delete-entry", "delete-subtree"}:
            if kind == "delete-entry" and len(membership) != 1:
                n._refuse("namespace-directory-not-empty")
            if kind == "delete-subtree" and source.kind != "directory":
                n._refuse("namespace-kind-mismatch")
            affected = membership
            for identity in affected:
                del entries[identity]
        else:
            parent(args["parent_id"])
            mapping = {pair["source_id"]: pair["new_id"] for pair in args["identities"]}
            if set(mapping) != membership:
                n._refuse("namespace-request-invalid")
            frozen = {identity: entries[identity] for identity in membership}
            for old, new in mapping.items():
                fresh(new)
                current = frozen[old]
                if current.cas_id is not None:
                    _content(handle, current.cas_id, current.size)
                entries[new] = replace(
                    current,
                    entry_id=new,
                    parent_id=args["parent_id"]
                    if old == source.entry_id
                    else mapping[current.parent_id],
                    name=args["name"] if old == source.entry_id else current.name,
                    changed_revision=revision,
                )
                affected.add(new)
    elif kind == "import-folder":
        parent(args["parent_id"])
        incoming = {item["entry_id"]: item for item in args["entries"]}
        for item in incoming.values():
            fresh(item["entry_id"])
        for item in incoming.values():
            target = item["parent_id"]
            if target != args["parent_id"]:
                if target not in incoming:
                    n._refuse(
                        "namespace-request-invalid"
                        if target in entries
                        else "namespace-entry-missing"
                    )
                if incoming[target]["kind"] != "directory":
                    n._refuse("namespace-kind-mismatch")
            if item["cas_id"] is not None and (available or {}).get(item["cas_id"]) != item["size"]:
                verify_content(item["cas_id"], item["size"])
            entries[item["entry_id"]] = n.EntryRecord(**item, changed_revision=revision)
            affected.add(item["entry_id"])
    elif kind in {"snapshot", "delete-snapshot"}:
        snapshots.precondition(handle, request)
    elif kind != "expire-results":
        n._refuse()
    if len(affected) > n.MAX_ENTRIES:
        n._refuse("namespace-limit-exceeded")
    store._tree(replace(info, revision=revision), entries)
    return entries, affected


def _expire(handle: store.NamespaceHandle, request: dict[str, Any], current_time: datetime) -> None:
    for identity in request["args"]["operation_ids"]:
        row = _row(handle, identity)
        if row is None or row["state"] not in {"committed", "rejected"}:
            continue
        deadline = datetime.fromisoformat(row["retire_after"])
        if current_time < deadline:
            continue
        handle.db.execute(
            (
                "UPDATE operations SET "
                "fingerprint=NULL,state='retired',prepared_revision=0,terminal_rev"
                "ision=NULL,completed_at=NULL,retire_after=NULL,canonical_request="
                "NULL,terminal_result=NULL,terminal_error=NULL,ownership_receipt=N"
                "ULL,recovery_action=NULL WHERE operation_id=?"
            ),
            (identity,),
        )
        handle.db.execute(
            "DELETE FROM dependencies WHERE owner_kind='operation' AND owner_id=?", (identity,)
        )


def cleanup_expired(handle: store.NamespaceHandle, row: dict[str, Any]) -> None:
    if row["state"] not in {"committed", "rejected"} or row["canonical_request"] is None:
        return
    request = n._decode(row["canonical_request"])
    if request["kind"] != "expire-results":
        return
    recipe = content_io.decode_recipe(row["ownership_receipt"])
    if not recipe["expiry"]:
        return
    # A rejected expiry owns no disposal. Its copied proof is unnecessary,
    # including if a later, independent expiry retired the same target.
    for identity, expected in recipe["expiry"].items() if row["state"] == "committed" else []:
        target = _row(handle, identity)
        if target is None:
            n._refuse("namespace-storage-recovery-required")
        if target["state"] != "retired":
            continue
        directory = handle.root / "operations" / identity
        if not directory.exists():
            # A prior failed sync or process death can leave visible absence.
            # Establish its durability before discarding the ownership proof.
            store._sync_dir(directory.parent)
            continue
        for name in ("request.json", "receipt.json"):
            file = directory / name
            if not file.exists():
                continue
            store._regular(file)
            raw = file.read_bytes()
            if name == "request.json":
                valid = hashlib.sha256(raw).hexdigest() == expected["request_sha256"]
            else:
                valid = n._decode(raw) == expected["receipt"]
            if not valid:
                n._refuse("namespace-storage-recovery-required")
            file.unlink()
        directory.rmdir()
        store._sync_dir(directory.parent)
    # Keep the complete recipe through every disposal/sync failure. Only a
    # durable SQL commit may forget it; an uncertain commit blocks admission
    # and explicit recovery reconciles whichever version actually survived.
    recipe["expiry"] = {}
    handle.write_started = True
    handle.db.execute("BEGIN IMMEDIATE")
    handle.db.execute(
        "UPDATE operations SET ownership_receipt=? WHERE operation_id=?",
        (content_io.encode_recipe(recipe), row["operation_id"]),
    )
    store._commit(handle.db)


def finish(handle: store.NamespaceHandle, operation_id: str) -> n.MutationResult:
    row = _row(handle, operation_id)
    if row is None:
        n._refuse("namespace-storage-recovery-required")
    if row["state"] not in {"staging", "prepared"}:
        return _replay(handle, row)
    recipe, publications = content_io.recipe(handle, row)
    request = n._decode(row["canonical_request"])
    reconstructed = n.MutationRequest(
        request["namespace_id"],
        n.StoreIdentity(**request["expected_cas"]),
        request["expected_revision"],
        request["operation_id"],
        request["kind"],
        request["args"],
    )
    if normalize(reconstructed) != request or n._fingerprint(request) != row["fingerprint"]:
        n._refuse("namespace-storage-recovery-required")
    info = handle.load_info()
    rejection = None
    publication_receipt = handle.publication_receipts.get(operation_id, [])
    try:
        if recipe["precondition_error"] is not None:
            n._refuse(recipe["precondition_error"])
        if row["state"] == "staging":
            n._refuse("namespace-input-unfrozen")
        if request["namespace_id"] != info.namespace_id or request["expected_cas"] != asdict(
            info.cas
        ):
            n._refuse("namespace-binding-mismatch")
        if request["expected_revision"] != info.revision:
            n._refuse("namespace-revision-conflict")
        _changes(handle, request, info, available={item.cas_id: item.size for item in publications})
        if publications:
            publication_receipt = handle.publication_receipts.get(operation_id)
            if publication_receipt is None:
                publication_receipt = content_io.publish(handle, row)
            handle.publication_receipts[operation_id] = publication_receipt
            handle._cas_check(full=False)
        entries, affected = _changes(handle, request, info)
    except n.NamespaceError as error:
        if error.code in {
            "namespace-storage-recovery-required",
            "namespace-protection-unsupported",
        }:
            raise
        if publication_receipt:
            # A published command cannot become a durable domain rejection.
            n._refuse("namespace-storage-recovery-required")
        rejection = n.NamespaceError(
            error.code, info.namespace_id, operation_id, info.revision, "none"
        )
        entries, affected = handle.entries(), set()
    completed = _now()
    handle.write_started = True
    handle.effect = "recovery-required"
    handle.db.execute("BEGIN IMMEDIATE")
    if rejection is None:
        content_io.save_receipt(handle, row, publication_receipt)
    if rejection is None:
        old_entries = handle.entries()
        # Vacate affected live names first; no observer sees intermediate rows.
        for identity in affected:
            if identity in old_entries:
                handle.db.execute("UPDATE entries SET live=0 WHERE entry_id=?", (identity,))
        for identity in sorted(affected):
            if identity in entries:
                handle.db.execute(
                    "INSERT OR IGNORE INTO entry_reservations VALUES (?)", (identity,)
                )
                handle.write_entry(entries[identity])
            else:
                handle.write_entry(old_entries[identity], live=False)
        if request["kind"] == "expire-results":
            _expire(handle, request, completed)
        snapshot_result = snapshots.commit(handle, request, recipe["snapshot"])
        result = n.MutationResult(
            info.namespace_id,
            operation_id,
            row["fingerprint"],
            request["kind"],
            info.revision,
            info.revision + 1,
            sorted(affected),
            snapshot_result,
            info.cas,
        )
        handle.db.execute(
            "UPDATE namespace SET revision=?,service_state='ready'", (result.revision_after,)
        )
        payload = n.canonical_bytes(asdict(result)).decode()
        terminal_error = None
    else:
        result = None
        payload = None
        terminal_error = n.canonical_bytes(
            {
                "code": rejection.code,
                "namespace_id": rejection.namespace_id,
                "operation_id": operation_id,
                "actual_revision": info.revision,
                "effect": "none",
            }
        ).decode()
        handle.db.execute("UPDATE namespace SET service_state='ready'")
    handle.db.execute(
        (
            "UPDATE operations SET "
            "state=?,terminal_revision=?,completed_at=?,retire_after=?,termina"
            "l_result=?,terminal_error=? WHERE operation_id=?"
        ),
        (
            "rejected" if rejection else "committed",
            info.revision if rejection else result.revision_after,
            completed.isoformat(),
            (completed + timedelta(days=7)).isoformat(),
            payload,
            terminal_error,
            operation_id,
        ),
    )
    handle.db.execute("DELETE FROM dependencies WHERE owner_kind='operation'")
    handle.db.executemany(
        "INSERT INTO dependencies VALUES (?,?,?,?)", sorted(operation_dependencies(handle))
    )
    handle.db.execute("DELETE FROM dependencies WHERE owner_kind='snapshot'")
    handle.db.executemany(
        "INSERT INTO dependencies VALUES (?,?,?,?)", sorted(snapshots.dependencies(handle))
    )
    n._authorize(handle.session, request["kind"])
    store._commit(handle.db)
    handle.publication_receipts.pop(operation_id, None)
    for key in [key for key in handle.pending_captures if key[0] == operation_id]:
        del handle.pending_captures[key]
    handle.effect = "none" if rejection else "namespace-committed"
    cleanup_expired(handle, _row(handle, operation_id))
    content_io.cleanup(handle, _row(handle, operation_id))
    handle._receipt_state(operation_id, "durable")
    n._authorize(handle.session, "acknowledge")
    if rejection is not None:
        raise rejection from None
    return result


def read(
    handle: store.NamespaceHandle,
    request: n.ReadRequest,
    *,
    destination: n.SnapshotDestination | None = None,
) -> n.ReadResult:
    n._authorize(handle.session, "read")
    if type(request) is not n.ReadRequest or type(request.expected_cas) is not n.StoreIdentity:
        n._refuse()
    value = n._decode(n.canonical_bytes(asdict(request)))
    n._uuid(value["namespace_id"])
    if value["at_revision"] is not None:
        n._integer(value["at_revision"])
    args, kind = value["args"], value["kind"]
    expected = {
        "info": set(),
        "get-entry": {"entry_id"},
        "list-directory": {"entry_id"},
        "resolve": {"components"},
        "inspect-snapshot": {"snapshot_id"},
        "export-snapshot": {"snapshot_id"},
    }
    if (
        not isinstance(kind, str)
        or kind not in expected
        or not isinstance(args, dict)
        or set(args) != expected[kind]
    ):
        n._refuse()
    if (kind == "export-snapshot") != (destination is not None):
        n._refuse()
    if "snapshot_id" in args:
        n._uuid(args["snapshot_id"])
    if "entry_id" in args:
        n._uuid(args["entry_id"])
    if kind == "resolve":
        if not isinstance(args["components"], list) or len(args["components"]) > n.MAX_ENTRIES:
            n._refuse()
        args["components"] = [n._name(v) for v in args["components"]]
    handle.db.execute("BEGIN")
    try:
        info = handle.load_info()
        if value["namespace_id"] != info.namespace_id or value["expected_cas"] != asdict(info.cas):
            n._refuse("namespace-binding-mismatch")
        if value["at_revision"] is not None and value["at_revision"] != info.revision:
            raise n.NamespaceError(
                "namespace-revision-conflict", info.namespace_id, actual_revision=info.revision
            )
        entries = handle.entries()
        if kind in {"inspect-snapshot", "export-snapshot"}:
            record = snapshots.catalog(handle, args["snapshot_id"])
            result = n.SnapshotView(record, snapshots.catalog_view(handle, record))
        elif kind == "info":
            result = info
        elif kind == "resolve":
            result = entries[info.root_id]
            for component in args["components"]:
                if result.kind != "directory":
                    n._refuse("namespace-kind-mismatch")
                found = [
                    e
                    for e in entries.values()
                    if e.parent_id == result.entry_id and n._key(e.name) == n._key(component)
                ]
                if len(found) != 1:
                    n._refuse("namespace-entry-missing")
                result = found[0]
        else:
            if args["entry_id"] not in entries:
                n._refuse("namespace-entry-missing")
            result = entries[args["entry_id"]]
            if kind == "list-directory":
                if result.kind != "directory":
                    n._refuse("namespace-kind-mismatch")
                result = sorted(
                    (e for e in entries.values() if e.parent_id == result.entry_id),
                    key=lambda e: (n._key(e.name), e.entry_id),
                )
        n.canonical_bytes(
            result.manifest
            if isinstance(result, n.SnapshotView)
            else n._plain(result)
            if not isinstance(result, list)
            else [asdict(e) for e in result],
            limit=n.MAX_MANIFEST_BYTES,
        )
        handle.db.execute("COMMIT")
    except BaseException:
        if handle.db.in_transaction:
            handle.db.execute("ROLLBACK")
        raise
    n._authorize(handle.session, "read-result")
    return n.ReadResult(info.namespace_id, info.revision, info.cas, result)


def operation(handle: store.NamespaceHandle, operation_id: str) -> n.OperationStatus:
    n._authorize(handle.session, "status")
    n._uuid(operation_id)
    handle.db.execute("BEGIN")
    try:
        info = handle.load_info()
        row = _row(handle, operation_id)
        if row is None:
            result = n.OperationStatus(
                info.namespace_id, operation_id, "unknown", None, None, "none"
            )
        else:
            state = row["state"]
            completed = None
            if (
                state == "retired"
                and handle.db.execute(
                    "SELECT 1 FROM replacement_completion WHERE operation_id=? AND completed=1",
                    (operation_id,),
                ).fetchone()
            ):
                from castle import namespace_excision as cutover

                completed = cutover.completed_result(handle, operation_id)
            if state == "staging":
                state = "recovery-required"
            if state not in {"prepared", "committed", "rejected", "retired", "recovery-required"}:
                n._refuse("namespace-storage-recovery-required")
            error = n._decode(row["terminal_error"])["code"] if row["terminal_error"] else None
            result = n.OperationStatus(
                info.namespace_id,
                operation_id,
                state,
                n._result(n._decode(row["terminal_result"]))
                if row["terminal_result"]
                else completed,
                error,
                "recover"
                if state == "recovery-required"
                else "retry-same"
                if state == "prepared"
                else "refresh-new-token"
                if state == "rejected"
                else "none",
            )
        handle.db.execute("COMMIT")
    except BaseException:
        if handle.db.in_transaction:
            handle.db.execute("ROLLBACK")
        raise
    n._authorize(handle.session, "status-result")
    return result


def operation_dependencies(handle: store.NamespaceHandle) -> set[tuple[str, str, str, str]]:
    entries = handle.entries(live=False)
    expected = set()
    for (identity,) in handle.db.execute("SELECT operation_id FROM operations").fetchall():
        row = _row(handle, identity)
        if row["state"] not in {"committed", "rejected"}:
            continue
        request = n._decode(row["canonical_request"])
        content_ids = set(content_io.specs(request))
        value = content_io.recipe(handle, row)[0]
        if request["kind"] == "excise":
            content_ids.update(request["args"]["selection"]["content_ids"])
        content_ids.update(item["request"]["cas_id"] for item in value["publication"])
        if row["state"] == "committed":
            result = n._result(n._decode(row["terminal_result"]))
            content_ids.update(
                entries[key].cas_id
                for key in result.affected_entry_ids
                if key in entries and entries[key].cas_id is not None
            )
            if result.snapshot:
                content_ids.add(result.snapshot.cas_id)
            if request["kind"] == "delete-snapshot":
                content_ids.add(n._snapshot(value["snapshot"]).cas_id)
        expected.update(("operation", identity, cas_id, "request-result") for cas_id in content_ids)
    return expected
