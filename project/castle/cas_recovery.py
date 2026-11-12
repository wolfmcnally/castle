"""Operation-owned CAS publication; callers retain the domain admission fence.

Only the caller's independently reserved recipe and immutable staged bytes can
authorize recovery. This is neither general corruption repair nor garbage
collection. No writer subprocess is started; media/posture probes are read-only.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, NoReturn

from castle import cas, journal
from castle import namespace as n
from castle.store_state import StoreIdentity, _fsync_directory, _rename_new, read_store_identity


@dataclass(frozen=True)
class PublicationRequest:
    namespace_id: str
    operation_id: str
    publication_id: str
    cas_binding: StoreIdentity
    cas_id: str
    size: int
    staged_file_id: str


@dataclass(frozen=True)
class PublicationReceipt:
    publication_id: str
    operation_id: str
    cas_binding: StoreIdentity
    cas_id: str
    size: int
    event_ids: tuple[str, ...]
    event_offset: int
    event_length: int
    event_sha256: str


@dataclass(frozen=True)
class TouchedPublication:
    """A namespace caller's proof of one publication's touched CAS authority.

    `position` is the namespace's verified `cas_index_position` row after
    catch-up; `records` are the identity's located journal records, re-proven
    in that admission; `row` is their folded objects row, or None.
    """

    position: tuple[Any, ...]
    records: tuple[dict[str, Any], ...]
    row: tuple[Any, ...] | None


_FIELDS = {
    "schema",
    "namespace_id",
    "operation_id",
    "publication_id",
    "cas_binding",
    "cas_id",
    "size",
    "staged_file_id",
    "cas_temporary_relpath",
    "prior_object_present",
    "journal_prefix_length",
    "prefix_tail",
    "events",
    "state",
}


def _blocked() -> NoReturn:
    n._refuse("namespace-storage-recovery-required")


def _regular(file: Path) -> os.stat_result:
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _blocked()
    return info


def _sync_file(file: Path) -> None:
    _regular(file)
    fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes, *, offset: int | None = None) -> None:
    view = memoryview(data)
    done = 0
    while done < len(view):
        count = (
            os.write(fd, view[done:])
            if offset is None
            else os.pwrite(fd, view[done:], offset + done)
        )
        if count <= 0:
            raise OSError("publication write did not advance")
        done += count


def _encoded(value: object) -> bytes:
    return n.canonical_bytes(value, limit=n.MAX_INTEGER)


def _record_file(operation: Path, request: PublicationRequest) -> Path:
    return operation / f"publication.{request.publication_id}.json"


def _save(
    operation: Path, request: PublicationRequest, value: dict[str, Any], *, new: bool
) -> None:
    target = _record_file(operation, request)
    temporary = operation / f"publication.{request.publication_id}.tmp"
    raw = _encoded(value)
    destination = target if new else temporary
    if not new and temporary.exists():
        _regular(temporary)
        prior = temporary.read_bytes()
        alternate = _encoded(
            dict(value, state="prepared" if value["state"] == "complete" else "complete")
        )
        if not raw.startswith(prior) and not alternate.startswith(prior):
            _blocked()
        temporary.unlink()
        _fsync_directory(operation)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        _write_all(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    if destination.read_bytes() != raw:
        _blocked()
    if not new:
        os.replace(temporary, target)
    _fsync_directory(operation)


def _request(request: PublicationRequest, expected: StoreIdentity) -> None:
    if type(request) is not PublicationRequest or type(expected) is not StoreIdentity:
        _blocked()
    for identity in (
        request.namespace_id,
        request.operation_id,
        request.publication_id,
        request.staged_file_id,
    ):
        n._uuid(identity)
    if (
        len(
            {
                request.namespace_id,
                request.operation_id,
                request.publication_id,
                request.staged_file_id,
            }
        )
        != 4
        or request.cas_binding != expected
    ):
        _blocked()
    if not isinstance(request.cas_id, str) or n._CAS.fullmatch(request.cas_id) is None:
        _blocked()
    n._integer(request.size)


def _roots(cas_root: Path, operation_root: Path, request: PublicationRequest) -> tuple[Path, Path]:
    store, operation = Path(cas_root).absolute(), Path(operation_root).absolute()
    if store.is_symlink() or operation.is_symlink():
        _blocked()
    store, operation = store.resolve(strict=True), operation.resolve(strict=True)
    if not store.is_dir() or not operation.is_dir() or operation.name != request.operation_id:
        _blocked()
    if store == operation or store in operation.parents or operation in store.parents:
        _blocked()
    return store, operation


def _stage(operation: Path, request: PublicationRequest) -> Path:
    stage = operation / request.staged_file_id
    info = _regular(stage)
    if (
        info.st_size != request.size
        or info.st_mode & 0o222
        or cas.sha256_file(stage) != request.cas_id
    ):
        _blocked()
    return stage


def _records(raw: bytes) -> list[dict[str, Any]]:
    if raw and not raw.endswith(b"\n"):
        _blocked()
    records = [
        journal.parse_journal_record(json.loads(line), "publication") for line in raw.splitlines()
    ]
    return journal.validate_journal_sequence(records, "publication")


def _stream(value: dict[str, Any]) -> bytes:
    return b"".join(
        (journal.canonical_journal_line(event) + "\n").encode() for event in value["events"]
    )


def _temporary(store: Path, request: PublicationRequest) -> Path:
    return cas.object_path(store, request.cas_id).with_name(
        f".cas-{request.publication_id.replace('-', '')}.tmp"
    )


def _load(
    store: Path, operation: Path, expected: StoreIdentity, request: PublicationRequest
) -> dict[str, Any]:
    target = _record_file(operation, request)
    _regular(target)
    value = n._decode(target.read_bytes(), limit=n.MAX_INTEGER)
    if not isinstance(value, dict) or set(value) != _FIELDS:
        _blocked()
    fixed = asdict(request)
    if any(value.get(key) != item for key, item in fixed.items()):
        _blocked()
    if (
        value["schema"] != "castle.namespace_publication.v1"
        or type(value["prior_object_present"]) is not bool
        or value["state"] not in {"prepared", "complete"}
        or value["cas_temporary_relpath"]
        != _temporary(store, request).relative_to(store).as_posix()
        or value["cas_binding"] != asdict(expected)
    ):
        _blocked()
    n._integer(value["journal_prefix_length"])
    tail = value["prefix_tail"]
    if (tail is None) != (value["journal_prefix_length"] == 0) or (
        tail is not None
        and (
            not isinstance(tail, list)
            or len(tail) != 3
            or type(tail[0]) is not int
            or type(tail[1]) is not int
            or tail[0] < 0
            or tail[1] <= 0
            or tail[0] + tail[1] != value["journal_prefix_length"]
            or not isinstance(tail[2], str)
            or n._CAS.fullmatch("sha256:" + tail[2]) is None
        )
    ):
        _blocked()
    events = value["events"]
    if not isinstance(events, list) or len(events) != 1:
        _blocked()
    event = journal.parse_journal_record(events[0], "publication")
    if any(
        event[key] != item
        for key, item in {
            "cas_id": request.cas_id,
            "size": request.size,
            "event": "ingest",
            "source_root_id": f"namespace:{request.namespace_id}",
            "original_relpath": f"{request.operation_id}/{request.publication_id}",
            "batch": request.operation_id,
            "outcome": "dedup" if value["prior_object_present"] else "stored",
        }.items()
    ):
        _blocked()
    return value


def _tail(raw: bytes) -> list[Any] | None:
    """[offset, length, SHA-256] of the final framed line, as the namespace locates it."""
    if not raw:
        return None
    line = raw.decode("utf-8").splitlines(keepends=True)[-1].encode("utf-8")
    return [len(raw) - len(line), len(line), hashlib.sha256(line).hexdigest()]


def _line(descriptor: int, size: int, offset: int, length: int, digest: str) -> dict[str, Any]:
    """One LF-terminated framed journal line at a recorded span, or refuse."""
    from castle import namespace_store as located

    try:
        record = located._span(descriptor, size, offset, length, digest)
    except located._Framing:
        _blocked()
    if os.pread(descriptor, 1, offset + length - 1) != b"\n":
        _blocked()
    return record


def _open_index(store: Path) -> sqlite3.Connection:
    """The existing derived index only: never created, never followed."""
    current = store / cas.INDEX_FILENAME
    _regular(current)
    return sqlite3.connect(f"{current.as_uri()}?mode=rw", uri=True, timeout=0)


def _pragmas(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
        _blocked()
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA fullfsync=ON")
    connection.execute("PRAGMA foreign_keys=ON")
    if (
        connection.execute("PRAGMA synchronous").fetchone()[0] != 2
        or connection.execute("PRAGMA fullfsync").fetchone()[0] != 1
        or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
    ):
        _blocked()


def _touched_rows(
    connection: sqlite3.Connection, cas_id: str
) -> tuple[Counter[tuple[Any, ...]], Counter[tuple[Any, ...]]]:
    objects = connection.execute(
        "SELECT cas_id, size, media_type, stored_relpath, first_seen_utc, batch "
        "FROM objects WHERE cas_id = ?",
        (cas_id,),
    ).fetchall()
    names = connection.execute(
        "SELECT cas_id, source_root_id, original_relpath, recorded_utc FROM names WHERE cas_id = ?",
        (cas_id,),
    ).fetchall()
    return Counter(objects), Counter(names)


def _expected_rows(
    records: list[dict[str, Any]], cas_id: str
) -> tuple[Counter[tuple[Any, ...]], Counter[tuple[Any, ...]]]:
    objects, names = cas.expected_index_rows(records)
    return Counter([objects[cas_id]] if cas_id in objects else []), Counter(names)


def _touched_state(
    store: Path, request: PublicationRequest, touched: TouchedPublication
) -> tuple[int, list[Any] | None, dict[str, Any] | None]:
    """Pre-evidence checks of exactly the touched authority (no CAS change).

    Returns the journal length, its tail span and the tail record. Whole-store
    agreement is left to rebuild, verify, merge, migration, excision, recovery
    and diagnosis.
    """
    position = touched.position
    _, length, count, last_offset, last_length, last_digest, last_at, last_event = position
    descriptor = os.open(store / journal.JOURNAL_FILENAME, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != length:
            _blocked()
        tail, previous = None, None
        if count:
            tail = [last_offset, last_length, last_digest]
            previous = _line(descriptor, length, last_offset, last_length, last_digest)
            if last_offset + last_length != length or (
                previous["at"],
                previous["event_id"],
            ) != (last_at, last_event):
                _blocked()
        elif length:
            _blocked()
    finally:
        os.close(descriptor)
    records = list(touched.records)
    if any(record["cas_id"] != request.cas_id for record in records):
        _blocked()
    objects, _ = cas.expected_index_rows(records)
    if objects.get(request.cas_id) != touched.row:
        _blocked()
    root = f"namespace:{request.namespace_id}"
    relpath = f"{request.operation_id}/{request.publication_id}"
    if any(
        record["source_root_id"] == root and record["original_relpath"] == relpath
        for record in records
    ):
        _blocked()
    connection = _open_index(store)
    try:
        if _touched_rows(connection, request.cas_id) != _expected_rows(records, request.cas_id):
            _blocked()
        # The derived names row answers the cross-object reuse question; this
        # object's own located records answered it authoritatively above.
        if connection.execute(
            "SELECT 1 FROM names WHERE original_relpath = ? AND source_root_id = ?",
            (relpath, root),
        ).fetchone():
            _blocked()
    finally:
        connection.close()
    destination = cas.object_path(store, request.cas_id)
    if os.path.lexists(_temporary(store, request)):
        _blocked()
    if touched.row is None:
        if os.path.lexists(destination):
            _blocked()
    else:
        if touched.row[1] != request.size:
            _blocked()
        _regular(destination)
        cas._validate_authority_objects(
            store, {request.cas_id: destination}, {request.cas_id: touched.row}
        )
    return length, tail, previous


def _validate_state(
    store: Path, request: PublicationRequest, value: dict[str, Any]
) -> tuple[bytes, bool]:
    _regular(store / journal.JOURNAL_FILENAME)
    raw = (store / journal.JOURNAL_FILENAME).read_bytes()
    boundary = value["journal_prefix_length"]
    prefix, suffix = raw[:boundary], raw[boundary:]
    if len(prefix) != boundary:
        _blocked()
    previous = _records(prefix)
    if _tail(prefix) != value["prefix_tail"]:
        _blocked()
    stream = _stream(value)
    complete = suffix.startswith(stream)
    if not complete and not stream.startswith(suffix):
        _blocked()
    records = _records(raw) if complete else previous
    journal.validate_journal_sequence(previous + value["events"], "publication")
    objects, _ = cas.expected_index_rows(records)
    prior, _ = cas.expected_index_rows(previous)
    if value["prior_object_present"] != (request.cas_id in prior):
        _blocked()
    if (
        value["prior_object_present"]
        and value["events"][0]["media_type"] != prior[request.cas_id][2]
    ):
        _blocked()
    temporary = None if value["prior_object_present"] else _temporary(store, request)
    paths = cas._authority_paths(store, owned_temporary=temporary)
    if not value["prior_object_present"]:
        objects.pop(request.cas_id, None)
        owned = paths.pop(request.cas_id, None)
        if owned is not None and (
            _regular(owned).st_size != request.size or cas.sha256_file(owned) != request.cas_id
        ):
            _blocked()
    cas._validate_authority_objects(store, paths, objects)
    current = store / cas.INDEX_FILENAME
    if not current.exists() and boundary:
        _blocked()
    return raw, complete


def _copy_object(
    store: Path, operation: Path, request: PublicationRequest, value: dict[str, Any]
) -> None:
    if value["prior_object_present"]:
        return
    stage = _stage(operation, request)
    destination = cas.object_path(store, request.cas_id)
    temporary = _temporary(store, request)
    parent = store / "objects" / "sha256"
    for component in destination.relative_to(parent).parts[:-1]:
        child = parent / component
        if not child.exists():
            child.mkdir()
            _fsync_directory(parent)
        if not stat.S_ISDIR(child.lstat().st_mode):
            _blocked()
        parent = child
    if temporary.exists():
        info = _regular(temporary)
        if info.st_size > request.size:
            _blocked()
        with temporary.open("rb") as left, stage.open("rb") as right:
            while chunk := left.read(1024 * 1024):
                if right.read(len(chunk)) != chunk:
                    _blocked()
        temporary.unlink()
        _fsync_directory(parent)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with stage.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                _write_all(fd, chunk)
        os.fchmod(fd, 0o444)
        os.fsync(fd)
    finally:
        os.close(fd)
    if _regular(temporary).st_size != request.size or cas.sha256_file(temporary) != request.cas_id:
        _blocked()
    if destination.exists():
        flags = getattr(_regular(destination), "st_flags", 0)
        mask = getattr(stat, "UF_IMMUTABLE", 0)
        if flags & mask:
            os.chflags(destination, flags & ~mask)
        os.replace(temporary, destination)
    else:
        _rename_new(temporary, destination)
    cas._seal_object(store, destination, request.cas_id)
    _fsync_directory(parent)


def _write_events(store: Path, value: dict[str, Any], complete: bool) -> None:
    fd = os.open(store / journal.JOURNAL_FILENAME, os.O_RDWR | os.O_NOFOLLOW)
    try:
        if not complete:
            os.ftruncate(fd, value["journal_prefix_length"])
            os.fsync(fd)
        _write_all(fd, _stream(value), offset=value["journal_prefix_length"])
        os.fsync(fd)
    finally:
        os.close(fd)


def _index_commit(connection: sqlite3.Connection) -> None:
    connection.commit()


def _index(store: Path) -> None:
    """Whole-index rebuild for the full path; remote rows are kept within this call."""
    records = journal._read_authority_journal(store)
    objects, names = cas.expected_index_rows(records)
    remotes = Counter(cas._snapshot_remotes(store))
    if any(row[0] not in objects for row in remotes):
        _blocked()
    connection = sqlite3.connect(store / cas.INDEX_FILENAME, timeout=0)
    try:
        _pragmas(connection)
        connection.executescript(cas._SCHEMA)
        remote_rows = connection.execute(f"SELECT {cas._REMOTES_COLUMNS} FROM remotes").fetchall()
        if Counter(remote_rows) != remotes:
            _blocked()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM names")
        connection.execute("DELETE FROM objects")
        connection.executemany("INSERT INTO objects VALUES (?,?,?,?,?,?)", objects.values())
        connection.executemany("INSERT INTO names VALUES (?,?,?,?)", names)
        _index_commit(connection)
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
            _blocked()
    finally:
        connection.close()
    _sync_file(store / cas.INDEX_FILENAME)
    _fsync_directory(store)
    if cas._index_errors(store, objects, names) or Counter(cas._snapshot_remotes(store)) != remotes:
        _blocked()


def _index_touched(store: Path, value: dict[str, Any], touched: TouchedPublication) -> None:
    """Apply only this publication's record to the derived index and read it back."""
    record = value["events"][0]
    expected = _expected_rows([*touched.records, record], record["cas_id"])
    connection = _open_index(store)
    try:
        _pragmas(connection)
        connection.execute("BEGIN IMMEDIATE")
        cas._index_record(connection, record, media_refinement=None)
        _index_commit(connection)
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
            _blocked()
        if _touched_rows(connection, record["cas_id"]) != expected:
            _blocked()
    finally:
        connection.close()
    _sync_file(store / cas.INDEX_FILENAME)
    _fsync_directory(store)


def _appended(store: Path, value: dict[str, Any]) -> None:
    """The publication's own line is exactly the journal's new final line."""
    stream = _stream(value)
    offset = value["journal_prefix_length"]
    descriptor = os.open(store / journal.JOURNAL_FILENAME, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            _blocked()
        if info.st_size != offset + len(stream):
            _blocked()
        record = _line(
            descriptor, info.st_size, offset, len(stream), hashlib.sha256(stream).hexdigest()
        )
    finally:
        os.close(descriptor)
    if record != value["events"][0]:
        _blocked()


def _receipt(request: PublicationRequest, value: dict[str, Any]) -> PublicationReceipt:
    stream = _stream(value)
    return PublicationReceipt(
        request.publication_id,
        request.operation_id,
        request.cas_binding,
        request.cas_id,
        request.size,
        tuple(event["event_id"] for event in value["events"]),
        value["journal_prefix_length"],
        len(stream),
        hashlib.sha256(stream).hexdigest(),
    )


def _run_locked(
    store: Path,
    operation: Path,
    expected: StoreIdentity,
    request: PublicationRequest,
    *,
    recover: bool,
    touched: TouchedPublication | None = None,
) -> PublicationReceipt:
    """For a coordinator already holding domain admission then the CAS lock.

    `touched` selects the namespace's touched path for a new publication; None
    keeps the full path (public entry points, excision, resumed evidence).
    """
    _request(request, expected)
    if read_store_identity(store) != expected:
        _blocked()
    stage = _stage(operation, request)
    target = _record_file(operation, request)
    if target.exists():
        value = _load(store, operation, expected, request)
        _, complete = _validate_state(store, request, value)
        if value["state"] == "complete":
            if not complete:
                _blocked()
            cas._validated_authority(store, derived=True)
            # Fresh evidence write establishes this retry's acknowledgment;
            # the complete record was reachable only after successful CAS sync.
            _save(operation, request, value, new=False)
            return _receipt(request, value)
        # Reestablish possibly sync-uncertain saved evidence before CAS effects.
        _save(operation, request, value, new=False)
    else:
        if recover:
            _blocked()
        if touched is None:
            records, paths, _ = cas._validated_authority(store, derived=True)
            raw = (store / journal.JOURNAL_FILENAME).read_bytes()
            _records(raw)
            if any(
                record["source_root_id"] == f"namespace:{request.namespace_id}"
                and record["original_relpath"] == f"{request.operation_id}/{request.publication_id}"
                for record in records
            ):
                _blocked()
            objects, _ = cas.expected_index_rows(records)
            row = objects.get(request.cas_id) if request.cas_id in paths else None
            length, tail, previous = len(raw), _tail(raw), records[-1] if records else None
        else:
            row = touched.row
            length, tail, previous = _touched_state(store, request, touched)
        prior = row is not None
        event = {
            "schema": journal.CAS_JOURNAL_EVENT_SCHEMA,
            "event_id": journal.new_event_id(),
            "at": journal.monotonic_journal_timestamp(previous),
            "event": "ingest",
            "cas_id": request.cas_id,
            "size": request.size,
            "media_type": row[2] if row is not None else cas.classify_media_type(stage),
            "source_root_id": f"namespace:{request.namespace_id}",
            "original_relpath": f"{request.operation_id}/{request.publication_id}",
            "batch": request.operation_id,
            "outcome": "dedup" if prior else "stored",
        }
        if row is not None and row[1] != request.size:
            _blocked()
        value = dict(
            asdict(request),
            schema="castle.namespace_publication.v1",
            cas_temporary_relpath=_temporary(store, request).relative_to(store).as_posix(),
            prior_object_present=prior,
            journal_prefix_length=length,
            prefix_tail=tail,
            events=[event],
            state="prepared",
        )
        _sync_file(stage)
        _fsync_directory(operation)
        _save(operation, request, value, new=True)
        # The same held lock covers the initial authority validation; saving
        # caller-owned evidence has made no CAS change to revalidate.
        complete = False
    _copy_object(store, operation, request, value)
    _write_events(store, value, complete)
    if touched is None:
        _index(store)
        cas._validated_authority(store, derived=True)
    else:
        _index_touched(store, value, touched)
        _appended(store, value)
    value["state"] = "complete"
    _save(operation, request, value, new=False)
    return _receipt(request, value)


def _call(
    cas_root: Path,
    operation_root: Path,
    expected_identity: StoreIdentity,
    publication: PublicationRequest,
    *,
    recover: bool,
) -> PublicationReceipt:
    try:
        _request(publication, expected_identity)
        store, operation = _roots(cas_root, operation_root, publication)
        with journal._stable_lock(store):
            return _run_locked(store, operation, expected_identity, publication, recover=recover)
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
            "namespace-storage-recovery-required", effect="recovery-required"
        ) from None


def publish_namespace_content(
    cas_root: Path,
    operation_root: Path,
    expected_identity: StoreIdentity,
    publication: PublicationRequest,
) -> PublicationReceipt:
    """Publish an independently owned immutable stage under caller admission."""
    return _call(cas_root, operation_root, expected_identity, publication, recover=False)


def recover_namespace_publication(
    cas_root: Path,
    operation_root: Path,
    expected_identity: StoreIdentity,
    publication: PublicationRequest,
) -> PublicationReceipt:
    """Finish only this exact saved publication, returning the identical receipt."""
    return _call(cas_root, operation_root, expected_identity, publication, recover=True)
