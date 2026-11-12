"""SQL-owned immutable payload inventory, publication and exact cleanup."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from castle import cas, journal
from castle import cas_recovery as publication
from castle import namespace as n

if TYPE_CHECKING:
    from castle.namespace_store import NamespaceHandle

# Internal recipes repeat fixed-size publication/receipt bindings. This bound
# covers every product-admitted inventory; it does not narrow request/content limits.
OWNERSHIP_LIMIT = n.MAX_REQUEST_BYTES + 4096 * n.MAX_ENTRIES


def decode_recipe(raw: str) -> dict[str, Any]:
    return n._decode(raw, limit=OWNERSHIP_LIMIT)


def encode_recipe(value: dict[str, Any]) -> str:
    return n.canonical_bytes(value, limit=OWNERSHIP_LIMIT).decode()


def specs(request: dict[str, Any]) -> dict[str, int]:
    args = request["args"]
    values = []
    if request["kind"] in {"create-file", "replace-content"}:
        values = [(args["content"]["cas_id"], args["content"]["size"])]
    elif request["kind"] == "import-folder":
        values = [(e["cas_id"], e["size"]) for e in args["entries"] if e["kind"] == "file"]
    result = {}
    for identity, size in values:
        if identity in result and result[identity] != size:
            n._refuse()
        result[identity] = size
    return result


def source(
    handle: NamespaceHandle, request: dict[str, Any], sources: Mapping[str, object]
) -> dict[str, bytes]:
    if not isinstance(sources, Mapping):
        n._refuse()
    try:
        frozen = dict(sources)
    except Exception:
        # Mapping callbacks are caller code; ordinary transport exceptions are
        # an unreserved input refusal, never a backing-store recovery claim.
        n._refuse()
    declared = specs(request)
    if set(frozen) - set(declared):
        n._refuse()
    for identity, data in frozen.items():
        if type(data) is not bytes:
            n._refuse("namespace-input-unfrozen")
        if (
            len(data) != declared[identity]
            or "sha256:" + hashlib.sha256(data).hexdigest() != identity
        ):
            n._refuse("namespace-content-mismatch")
    if request["kind"] == "import-folder" and {
        i for i in set(declared) - set(frozen) if handle.content_row(i) is None
    }:
        n._refuse("namespace-input-unfrozen")
    return frozen


def new_requests(
    handle: NamespaceHandle, request: dict[str, Any], payloads: dict[str, bytes]
) -> list[publication.PublicationRequest]:
    used = {
        request["namespace_id"],
        request["operation_id"],
        handle.load_info().database_generation,
    }
    used |= handle.authority_ids()

    def taken(value: str) -> bool:
        # Primary-key lookups: a fresh uuid never needs every reserved id loaded.
        return value in used or any(
            handle.db.execute(f"SELECT 1 FROM {table} WHERE {field}=?", (value,)).fetchone()
            for table, field in (
                ("entry_reservations", "entry_id"),
                ("snapshot_reservations", "snapshot_id"),
                ("operations", "operation_id"),
            )
        )

    if request["kind"] == "import-folder":
        used.update(e["entry_id"] for e in request["args"]["entries"])
    used.update(v for k, v in request["args"].items() if k in {"entry_id", "snapshot_id"})
    result = []
    for identity in sorted(i for i in payloads if handle.content_row(i) is None):
        allocated = []
        while len(allocated) < 2:
            value = str(uuid.uuid4())
            if not taken(value):
                used.add(value)
                allocated.append(value)
        result.append(
            publication.PublicationRequest(
                request["namespace_id"],
                request["operation_id"],
                allocated[0],
                handle.binding,
                identity,
                len(payloads[identity]),
                allocated[1],
            )
        )
    return result


def check_capacity(value: dict[str, Any]) -> None:
    future = {**value, "publication": []}
    for item in value["publication"]:
        request = item["request"]
        future["publication"].append(
            {
                "request": request,
                "receipt": {
                    "publication_id": request["publication_id"],
                    "operation_id": request["operation_id"],
                    "cas_binding": request["cas_binding"],
                    "cas_id": request["cas_id"],
                    "size": request["size"],
                    "event_ids": ["v1:" + "9" * 16 + ":sha256:" + "f" * 64],
                    "event_offset": n.MAX_INTEGER,
                    "event_length": n.MAX_INTEGER,
                    "event_sha256": "f" * 64,
                },
            }
        )
    encode_recipe(future)


def recipe(
    handle: NamespaceHandle, row: dict[str, Any]
) -> tuple[dict[str, Any], list[publication.PublicationRequest]]:
    value = decode_recipe(row["ownership_receipt"])
    request = n._decode(row["canonical_request"])
    if request["kind"] == "excise" and "cutover" in value:
        from castle import namespace_excision as cutover

        return cutover.aborted_recipe(handle, row, value), []
    if (
        not isinstance(value, dict)
        or set(value) != {"receipt", "expiry", "publication", "precondition_error", "snapshot"}
        or value["receipt"] != handle._ownership(request, handle.ownership_info(row))
        or not isinstance(value["expiry"], dict)
        or not isinstance(value["publication"], list)
        or (value["precondition_error"] is not None and value["precondition_error"] not in n._CODES)
        or len(value["publication"]) > n.MAX_ENTRIES
    ):
        n._refuse("namespace-storage-recovery-required")
    declared = specs(request)
    if value["snapshot"] is not None:
        snapshot = n._snapshot(value["snapshot"])
        if (
            request["kind"] not in {"snapshot", "delete-snapshot"}
            or snapshot.snapshot_id != request["args"]["snapshot_id"]
            or (
                request["kind"] == "snapshot"
                and snapshot.captured_revision != row["prepared_revision"]
            )
            or snapshot.captured_revision > row["prepared_revision"]
        ):
            n._refuse("namespace-storage-recovery-required")
        declared = {snapshot.cas_id: snapshot.size}
        if request["kind"] == "snapshot" and row["state"] in {"staging", "prepared"}:
            from castle import namespace_snapshots as snapshots

            key = (row["operation_id"], snapshot)
            if key not in handle.pending_captures:
                expected, raw = snapshots.capture(handle, snapshot.snapshot_id, check_content=False)
                if expected != snapshot:
                    n._refuse("namespace-storage-recovery-required")
                handle.pending_captures[key] = raw
    elif request["kind"] in {"snapshot", "delete-snapshot"} and value["precondition_error"] is None:
        n._refuse("namespace-storage-recovery-required")
    result = []
    used = {request["namespace_id"], request["operation_id"]}
    ids = []
    for saved in value["publication"]:
        if not isinstance(saved, dict) or set(saved) != {"request", "receipt"}:
            n._refuse("namespace-storage-recovery-required")
        fields = dict(saved["request"])
        fields["cas_binding"] = n.StoreIdentity(**fields["cas_binding"])
        item = publication.PublicationRequest(**fields)
        publication._request(item, handle.ownership_info(row).cas)
        if (
            item.namespace_id != handle.anchor.namespace_id
            or item.operation_id != row["operation_id"]
            or declared.get(item.cas_id) != item.size
            or value["precondition_error"] is not None
            or (row["state"] == "committed") != (saved["receipt"] is not None)
            or item.publication_id in used
            or item.staged_file_id in used
            or item.publication_id == item.staged_file_id
        ):
            n._refuse("namespace-storage-recovery-required")
        used.update((item.publication_id, item.staged_file_id))
        ids.append(item.cas_id)
        if saved["receipt"] is not None:
            _receipt(saved["receipt"], item)
        result.append(item)
    if ids != sorted(set(ids)) or (
        row["state"] == "staging" and not result and value["precondition_error"] is None
    ):
        n._refuse("namespace-storage-recovery-required")
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
            "event_offset",
            "event_length",
            "event_sha256",
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
        n._integer(result.event_offset)
        n._integer(result.event_length)
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
            or not result.event_length
            or not isinstance(result.event_sha256, str)
            or n._CAS.fullmatch("sha256:" + result.event_sha256) is None
        ):
            raise ValueError("receipt binding")
        return result
    except (ValueError, TypeError, KeyError, journal.CastleError):
        n._refuse("namespace-storage-recovery-required")


def names(requests: list[publication.PublicationRequest]) -> set[str]:
    return {
        name
        for request in requests
        for name in (
            request.staged_file_id,
            f"publication.{request.publication_id}.json",
            f"publication.{request.publication_id}.tmp",
        )
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
    requests = recipe(handle, row)[1]
    directory = handle.root / "operations" / row["operation_id"]
    return bool(
        names(requests) & ({p.name for p in directory.iterdir()} if directory.exists() else set())
    )


def write_stage(
    handle: NamespaceHandle,
    request: publication.PublicationRequest,
    data: bytes,
    *,
    repair_prefix: bool = False,
) -> None:
    directory = handle.root / "operations" / request.operation_id
    stage = directory / request.staged_file_id
    existing = stage.exists()
    if existing:
        publication._regular(stage)
        raw = stage.read_bytes()
        if raw != data and not (repair_prefix and data.startswith(raw)):
            n._refuse("namespace-storage-recovery-required")
        stage.chmod(0o600)
    flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if not existing:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(stage, flags, 0o600)
    try:
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


def publish(handle: NamespaceHandle, row: dict[str, Any]) -> list[publication.PublicationReceipt]:
    requests = recipe(handle, row)[1]
    directory = handle.root / "operations" / row["operation_id"]
    try:
        started = [
            i
            for i, item in enumerate(requests)
            if publication._record_file(directory, item).exists()
        ]
        if started != list(range(len(started))):
            n._refuse("namespace-storage-recovery-required")
        for i in started[:-1]:
            if (
                publication._load(handle.cas_root, directory, handle.binding, requests[i])["state"]
                != "complete"
            ):
                n._refuse("namespace-storage-recovery-required")
        order = ([started[-1]] if started else []) + [
            i for i in range(len(requests)) if not started or i != started[-1]
        ]
        receipts = {}
        for i in order:
            request = requests[i]
            store, operation = publication._roots(handle.cas_root, directory, request)
            resume = publication._record_file(operation, request).exists()
            receipts[i] = publication._run_locked(
                store,
                operation,
                handle.binding,
                request,
                recover=resume,
                touched=None if resume else _touched(handle, request),
            )
        return [receipts[i] for i in range(len(requests))]
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
        n._refuse("namespace-storage-recovery-required")


def _touched(
    handle: NamespaceHandle, request: publication.PublicationRequest
) -> publication.TouchedPublication:
    """Catch the located index up to the journal, then prove this identity's records."""
    handle._cas_position()
    row = handle.object(request.cas_id)
    records = tuple(handle._object_records(request.cas_id))
    position = handle._position()
    if position is None:
        n._refuse("namespace-storage-recovery-required")
    return publication.TouchedPublication(tuple(position), records, row)


def save_receipt(
    handle: NamespaceHandle, row: dict[str, Any], receipts: list[publication.PublicationReceipt]
) -> None:
    value, requests = recipe(handle, row)
    if len(receipts) != len(requests):
        n._refuse("namespace-storage-recovery-required")
    for saved, request, receipt in zip(value["publication"], requests, receipts, strict=True):
        encoded = asdict(receipt)
        encoded["event_ids"] = list(receipt.event_ids)
        _receipt(encoded, request)
        saved["receipt"] = encoded
    handle.db.execute(
        "UPDATE operations SET ownership_receipt=? WHERE operation_id=?",
        (encode_recipe(value), row["operation_id"]),
    )


@contextmanager
def journal_descriptor(cas_root: Path) -> Iterator[tuple[int, int]]:
    """The CAS journal opened once: no link, a regular file, one link; with its size."""
    descriptor = os.open(cas_root / journal.JOURNAL_FILENAME, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            n._refuse("namespace-storage-recovery-required")
        yield descriptor, info.st_size
    finally:
        os.close(descriptor)


def validate_receipt(
    handle: NamespaceHandle,
    row: dict[str, Any],
    request: publication.PublicationRequest,
    encoded: dict[str, Any],
    descriptor: int,
    size: int,
) -> publication.PublicationReceipt:
    """Prove the receipt's own journal line where it says it is, and its object."""
    if row["state"] != "committed":
        n._refuse("namespace-storage-recovery-required")
    receipt = _receipt(encoded, request)
    event = publication._line(
        descriptor, size, receipt.event_offset, receipt.event_length, receipt.event_sha256
    )
    if receipt.event_ids != (event["event_id"],):
        n._refuse("namespace-storage-recovery-required")
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
    return receipt


def cleanup(handle: NamespaceHandle, row: dict[str, Any]) -> None:
    value, requests = recipe(handle, row)
    directory = handle.root / "operations" / row["operation_id"]
    if handle.ownership_info(row).cas != handle.binding:
        if names(requests) & (
            {item.name for item in directory.iterdir()} if directory.exists() else set()
        ):
            n._refuse("namespace-storage-recovery-required")
        return  # Historical terminal evidence stays byte-identical; no old CAS replay.
    if not requests:
        return
    with journal_descriptor(handle.cas_root) as (descriptor, size):
        _cleanup(handle, row, value, requests, directory, descriptor, size)


def _cleanup(
    handle: NamespaceHandle,
    row: dict[str, Any],
    value: dict[str, Any],
    requests: list[publication.PublicationRequest],
    directory: Path,
    descriptor: int,
    size: int,
) -> None:
    for request, saved in zip(requests, value["publication"], strict=True):
        stage = directory / request.staged_file_id
        record = publication._record_file(directory, request)
        temporary = directory / f"publication.{request.publication_id}.tmp"
        if row["state"] == "committed":
            validate_receipt(handle, row, request, saved["receipt"], descriptor, size)
            if stage.exists():
                publication._stage(directory, request)
                if not record.exists():
                    n._refuse("namespace-storage-recovery-required")
            if record.exists():
                evidence = publication._load(handle.cas_root, directory, handle.binding, request)
                boundary = evidence["journal_prefix_length"]
                stream = publication._stream(evidence)
                if (
                    evidence["state"] != "complete"
                    or os.pread(descriptor, len(stream), boundary) != stream
                ):
                    n._refuse("namespace-storage-recovery-required")
                if evidence["prefix_tail"] is not None:
                    publication._line(descriptor, size, *evidence["prefix_tail"])
                if temporary.exists():
                    publication._regular(temporary)
                    raw_temporary = temporary.read_bytes()
                    if not any(
                        encoded.startswith(raw_temporary)
                        for encoded in (
                            publication._encoded(evidence),
                            publication._encoded(dict(evidence, state="prepared")),
                        )
                    ):
                        n._refuse("namespace-storage-recovery-required")
            elif temporary.exists():
                n._refuse("namespace-storage-recovery-required")
        elif row["state"] == "rejected":
            error = n._decode(row["terminal_error"])
            if error["code"] != "namespace-input-unfrozen" or record.exists() or temporary.exists():
                n._refuse("namespace-storage-recovery-required")
            if stage.exists():
                stage_size = publication._regular(stage).st_size
                if stage_size > request.size or (
                    stage_size == request.size and cas.sha256_file(stage) != request.cas_id
                ):
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
    from castle import namespace_transactions as tx

    rows = [
        tx._row(handle, identity)
        for (identity,) in handle.db.execute(
            "SELECT operation_id FROM operations WHERE state!='retired'"
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
        value, requests = recipe(handle, row)
        if not requests:
            continue
        if row["state"] == "staging":
            generated = None
            if value["snapshot"] is not None:
                snap = n._snapshot(value["snapshot"])
                generated = handle.pending_captures[(row["operation_id"], snap)]
            incomplete = False
            stages = []
            for request in requests:
                stage = handle.root / "operations" / request.operation_id / request.staged_file_id
                if generated is not None:
                    if stage.exists():
                        publication._regular(stage)
                        if not generated.startswith(stage.read_bytes()):
                            n._refuse("namespace-storage-recovery-required")
                    stages.append((request, generated))
                elif not stage.exists() or publication._regular(stage).st_size < request.size:
                    incomplete = True
                elif (
                    stage.stat().st_size != request.size or cas.sha256_file(stage) != request.cas_id
                ):
                    n._refuse("namespace-storage-recovery-required")
                else:
                    stages.append((request, stage.read_bytes()))
            if incomplete:
                continue
            for request, data in stages:
                if generated is None:
                    write_stage(handle, request, data)
                else:
                    write_stage(handle, request, data, repair_prefix=True)
            promote(handle, row["operation_id"])
            row = tx._row(handle, row["operation_id"])
        handle.publication_receipts[row["operation_id"]] = publish(handle, row)
