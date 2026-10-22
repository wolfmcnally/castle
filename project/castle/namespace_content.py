"""SQL-owned immutable input, publication and cleanup for one file operation."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from castle.namespace_store import NamespaceHandle

from castle import cas, journal
from castle import cas_recovery as publication
from castle import namespace as n


def source(request: dict[str, Any], sources: Mapping[str, object]) -> bytes | None:
    if not isinstance(sources, Mapping):
        n._refuse()
    try:
        frozen = dict(sources)
    except Exception:
        # Mapping callbacks are caller code, not a backing-store failure.
        # Translate ordinary callback failures; process interrupts still escape.
        n._refuse("namespace-request-invalid")
    content = request["args"].get("content")
    if frozen and (content is None or set(frozen) != {content["cas_id"]}):
        n._refuse()
    if not frozen:
        return None
    data = frozen[content["cas_id"]]
    if type(data) is not bytes:
        n._refuse("namespace-input-unfrozen")
    if (
        len(data) != content["size"]
        or "sha256:" + hashlib.sha256(data).hexdigest() != content["cas_id"]
    ):
        n._refuse("namespace-content-mismatch")
    return data


def new_request(handle: NamespaceHandle, request: dict[str, Any]) -> publication.PublicationRequest:
    content = request["args"]["content"]
    used = {request["namespace_id"], request["operation_id"]}
    used.update(row[0] for row in handle.db.execute("SELECT entry_id FROM entry_reservations"))
    used.update(row[0] for row in handle.db.execute("SELECT operation_id FROM operations"))
    allocated = []
    while len(allocated) < 2:
        value = str(uuid.uuid4())
        if value not in used:
            used.add(value)
            allocated.append(value)
    return publication.PublicationRequest(
        request["namespace_id"],
        request["operation_id"],
        allocated[0],
        handle.binding,
        content["cas_id"],
        content["size"],
        allocated[1],
    )


def recipe(
    handle: NamespaceHandle, row: dict[str, Any]
) -> tuple[dict[str, Any], publication.PublicationRequest | None]:
    value = n._decode(row["ownership_receipt"])
    request = n._decode(row["canonical_request"])
    if (
        not isinstance(value, dict)
        or set(value) != {"receipt", "expiry", "publication", "precondition_error"}
        or value["receipt"] != handle._ownership(request, handle.load_info())
        or not isinstance(value["expiry"], dict)
        or (value["precondition_error"] is not None and value["precondition_error"] not in n._CODES)
    ):
        n._refuse("namespace-storage-recovery-required")
    saved = value["publication"]
    if saved is None:
        if row["state"] == "staging" and value["precondition_error"] is None:
            n._refuse("namespace-storage-recovery-required")
        return value, None
    if not isinstance(saved, dict) or set(saved) != {"request", "receipt"}:
        n._refuse("namespace-storage-recovery-required")
    fields = dict(saved["request"])
    fields["cas_binding"] = n.StoreIdentity(**fields["cas_binding"])
    result = publication.PublicationRequest(**fields)
    publication._request(result, handle.binding)
    if (
        result.namespace_id != handle.anchor.namespace_id
        or result.operation_id != row["operation_id"]
        or request["kind"] not in {"create-file", "replace-content"}
        or request["args"]["content"] != {"cas_id": result.cas_id, "size": result.size}
        or value["precondition_error"] is not None
        or (row["state"] == "committed") != (saved["receipt"] is not None)
    ):
        n._refuse("namespace-storage-recovery-required")
    if saved["receipt"] is not None:
        _receipt(saved["receipt"], result)
    return value, result


def _receipt(
    value: object, request: publication.PublicationRequest
) -> publication.PublicationReceipt:
    try:
        if not isinstance(value, dict) or set(value) != {
            "publication_id",
            "operation_id",
            "cas_binding",
            "cas_id",
            "size",
            "event_ids",
            "journal_length",
            "journal_sha256",
        }:
            raise ValueError("closed receipt")
        fields = dict(value)
        events = fields["event_ids"]
        if not isinstance(events, list) or len(events) != 1:
            raise ValueError("event identity")
        journal.validate_event_id(events[0], "namespace publication")
        fields["cas_binding"] = n.StoreIdentity(**fields["cas_binding"])
        fields["event_ids"] = tuple(events)
        result = publication.PublicationReceipt(**fields)
        n._integer(result.size)
        n._integer(result.journal_length)
        if (
            (
                result.publication_id,
                result.operation_id,
                result.cas_binding,
                result.cas_id,
                result.size,
            )
            != (
                request.publication_id,
                request.operation_id,
                request.cas_binding,
                request.cas_id,
                request.size,
            )
            or not result.journal_length
            or not isinstance(result.journal_sha256, str)
            or n._CAS.fullmatch("sha256:" + result.journal_sha256) is None
        ):
            raise ValueError("receipt binding")
        return result
    except (ValueError, TypeError, KeyError, journal.CastleError):
        n._refuse("namespace-storage-recovery-required")


def names(request: publication.PublicationRequest | None) -> set[str]:
    if request is None:
        return set()
    return {
        request.staged_file_id,
        f"publication.{request.publication_id}.json",
        f"publication.{request.publication_id}.tmp",
    }


def inventory(handle: NamespaceHandle, directory: Path, *, structural: bool) -> None:
    base = {"request.json", "receipt.json", "receipt.tmp"}
    actual = {p.name for p in directory.iterdir()}
    if structural:
        for name in actual - base:
            candidate = name
            if name.startswith("publication.") and name.endswith((".json", ".tmp")):
                candidate = name.split(".")[1]
            if n._UUID.fullmatch(candidate) is None:
                n._refuse("namespace-storage-recovery-required")
    else:
        from castle import namespace_transactions as tx

        row = tx._row(handle, directory.name)
        allowed = (
            set() if row is None or row["state"] == "retired" else names(recipe(handle, row)[1])
        )
        if actual - base - allowed:
            n._refuse("namespace-storage-recovery-required")


def has_payload(handle: NamespaceHandle, row: dict[str, Any]) -> bool:
    _, request = recipe(handle, row)
    directory = handle.root / "operations" / row["operation_id"]
    return bool(
        names(request) & ({p.name for p in directory.iterdir()} if directory.exists() else set())
    )


def write_stage(
    handle: NamespaceHandle, request: publication.PublicationRequest, data: bytes
) -> None:
    directory = handle.root / "operations" / request.operation_id
    stage = directory / request.staged_file_id
    existing = stage.exists()
    if existing:
        publication._regular(stage)
        if stage.read_bytes() != data:
            n._refuse("namespace-storage-recovery-required")
        stage.chmod(0o600)
    flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if not existing:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(stage, flags, 0o600)
    try:
        # An in-place rewrite of identical bytes preserves the complete verified
        # copy even if this recovery write itself stops partway through.
        publication._write_all(fd, data, offset=0 if existing else None)
        os.fchmod(fd, 0o444)
        os.fsync(fd)
    finally:
        os.close(fd)
    from castle import namespace_store as store

    store._sync_dir(directory)
    store._sync_dir(directory.parent)


def promote(handle: NamespaceHandle, operation_id: str) -> None:
    from castle import namespace_store as store

    handle.db.execute("BEGIN IMMEDIATE")
    handle.db.execute(
        "UPDATE operations SET state='prepared' WHERE operation_id=? AND state='staging'",
        (operation_id,),
    )
    store._commit(handle.db)


def publish(handle: NamespaceHandle, row: dict[str, Any]) -> publication.PublicationReceipt | None:
    _, request = recipe(handle, row)
    if request is None:
        return None
    directory = handle.root / "operations" / request.operation_id
    try:
        store, operation = publication._roots(handle.cas_root, directory, request)
        return publication._run_locked(
            store,
            operation,
            handle.binding,
            request,
            recover=publication._record_file(operation, request).exists(),
        )
    except (
        OSError,
        sqlite3.Error,
        journal.CastleError,
        ValueError,
        TypeError,
        KeyError,
        UnicodeError,
        RecursionError,
    ):
        raise n.NamespaceError(
            "namespace-storage-recovery-required",
            request.namespace_id,
            request.operation_id,
            effect="recovery-required",
        ) from None


def save_receipt(
    handle: NamespaceHandle, row: dict[str, Any], receipt: publication.PublicationReceipt | None
) -> None:
    value, request = recipe(handle, row)
    if request is None:
        return
    if receipt is None or (
        receipt.publication_id,
        receipt.operation_id,
        receipt.cas_binding,
        receipt.cas_id,
        receipt.size,
    ) != (
        request.publication_id,
        request.operation_id,
        request.cas_binding,
        request.cas_id,
        request.size,
    ):
        n._refuse("namespace-storage-recovery-required")
    encoded_receipt = asdict(receipt)
    encoded_receipt["event_ids"] = list(receipt.event_ids)
    _receipt(encoded_receipt, request)
    value["publication"]["receipt"] = encoded_receipt
    handle.db.execute(
        "UPDATE operations SET ownership_receipt=? WHERE operation_id=?",
        (n.canonical_bytes(value).decode(), row["operation_id"]),
    )


def validate_receipt(handle: NamespaceHandle, row: dict[str, Any]) -> None:
    value, request = recipe(handle, row)
    if request is None or row["state"] != "committed":
        n._refuse("namespace-storage-recovery-required")
    receipt = _receipt(value["publication"]["receipt"], request)
    raw = (handle.cas_root / journal.JOURNAL_FILENAME).read_bytes()
    prefix = raw[: receipt.journal_length]
    if (
        len(prefix) != receipt.journal_length
        or hashlib.sha256(prefix).hexdigest() != receipt.journal_sha256
    ):
        n._refuse("namespace-storage-recovery-required")
    records = publication._records(prefix)
    if not records or receipt.event_ids != (records[-1]["event_id"],):
        n._refuse("namespace-storage-recovery-required")
    event = records[-1]
    if any(
        event[k] != v
        for k, v in {
            "cas_id": request.cas_id,
            "size": request.size,
            "source_root_id": "namespace:" + request.namespace_id,
            "original_relpath": request.operation_id + "/" + request.publication_id,
            "batch": request.operation_id,
        }.items()
    ):
        n._refuse("namespace-storage-recovery-required")
    file = cas.object_path(handle.cas_root, request.cas_id)
    if file.stat().st_size != request.size or cas.sha256_file(file) != request.cas_id:
        n._refuse("namespace-storage-recovery-required")


def cleanup(handle: NamespaceHandle, row: dict[str, Any]) -> None:
    _, request = recipe(handle, row)
    if request is None:
        return
    directory = handle.root / "operations" / request.operation_id
    stage = directory / request.staged_file_id
    record = publication._record_file(directory, request)
    temporary = directory / f"publication.{request.publication_id}.tmp"
    if row["state"] == "committed":
        validate_receipt(handle, row)
        if stage.exists():
            publication._stage(directory, request)
            if not record.exists():
                n._refuse("namespace-storage-recovery-required")
        if record.exists():
            value = publication._load(handle.cas_root, directory, handle.binding, request)
            raw = (handle.cas_root / journal.JOURNAL_FILENAME).read_bytes()
            boundary = value["journal_prefix_length"]
            if (
                value["state"] != "complete"
                or hashlib.sha256(raw[:boundary]).hexdigest() != value["journal_prefix_sha256"]
                or not raw[boundary:].startswith(publication._stream(value))
                or Counter(cas._snapshot_remotes(handle.cas_root))
                != Counter(tuple(v) for v in value["remotes"])
            ):
                n._refuse("namespace-storage-recovery-required")
            if temporary.exists():
                publication._regular(temporary)
                raw_temporary = temporary.read_bytes()
                if not any(
                    encoded.startswith(raw_temporary)
                    for encoded in (
                        publication._encoded(value),
                        publication._encoded(dict(value, state="prepared")),
                    )
                ):
                    n._refuse("namespace-storage-recovery-required")
        elif temporary.exists():
            # Without the complete record there are no expected temporary bytes.
            n._refuse("namespace-storage-recovery-required")
    elif row["state"] == "rejected":
        error = n._decode(row["terminal_error"])
        if error["code"] != "namespace-input-unfrozen" or record.exists() or temporary.exists():
            n._refuse("namespace-storage-recovery-required")
        if stage.exists():
            if publication._regular(stage).st_size >= request.size:
                n._refuse("namespace-storage-recovery-required")
    else:
        n._refuse("namespace-storage-recovery-required")
    for file in (stage, temporary, record):
        if file.exists():
            publication._regular(file)
            file.unlink()
            from castle import namespace_store as store

            store._sync_dir(directory)


def before_cas(handle: NamespaceHandle) -> None:
    """Repair only SQL-validated publication before requiring a clean CAS fold."""
    from castle import namespace_transactions as tx

    rows = [
        tx._row(handle, identity)
        for (identity,) in handle.db.execute(
            "SELECT operation_id FROM operations WHERE state != 'retired'"
        )
    ]
    pending = [row for row in rows if row["state"] in {"staging", "prepared"}]
    if len(pending) > 1:
        n._refuse("namespace-storage-recovery-required")
    if not handle.recover_requested:
        if pending or any(has_payload(handle, row) for row in rows):
            n._refuse("namespace-storage-recovery-required")
        return
    for row in pending:
        handle._restore_evidence(row)
        _, request = recipe(handle, row)
        if request is None:
            continue
        if row["state"] == "staging":
            stage = handle.root / "operations" / request.operation_id / request.staged_file_id
            if not stage.exists() or publication._regular(stage).st_size < request.size:
                continue  # no content intent/CAS effect; finish records input-unfrozen
            if stage.stat().st_size != request.size or cas.sha256_file(stage) != request.cas_id:
                n._refuse("namespace-storage-recovery-required")
            write_stage(handle, request, stage.read_bytes())
            promote(handle, request.operation_id)
            row = tx._row(handle, request.operation_id)
        handle.publication_receipts[request.operation_id] = publish(handle, row)
