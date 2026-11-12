"""Local durable namespace authority and its lifetime single coordinator."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import struct
import threading
import uuid
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from castle import cas, journal, store_state
from castle import namespace as n
from castle import namespace_content as content_io

_TABLES = {
    ("namespace"): (
        "CREATE TABLE namespace (namespace_id TEXT PRIMARY KEY, root_id "
        "TEXT NOT NULL, policy_version INTEGER NOT NULL, "
        "unicode_data_version TEXT NOT NULL, revision INTEGER NOT NULL, "
        "cas_binding TEXT NOT NULL, database_generation TEXT NOT NULL, "
        "protection TEXT NOT NULL, service_state TEXT NOT NULL)"
    ),
    "entry_reservations": "CREATE TABLE entry_reservations (entry_id TEXT PRIMARY KEY)",
    ("entries"): (
        "CREATE TABLE entries (entry_id TEXT PRIMARY KEY REFERENCES "
        "entry_reservations(entry_id), kind TEXT NOT NULL, parent_id TEXT "
        "REFERENCES entries(entry_id) DEFERRABLE INITIALLY DEFERRED, name "
        "TEXT, metadata TEXT NOT NULL, cas_id TEXT, size INTEGER, "
        "changed_revision INTEGER NOT NULL, live INTEGER NOT NULL "
        "CHECK(live IN (0,1)), comparison_key TEXT, "
        "touched_revision INTEGER NOT NULL DEFAULT 0)"
    ),
    ("operations"): (
        "CREATE TABLE operations (operation_id TEXT PRIMARY KEY, "
        "fingerprint TEXT, state TEXT NOT NULL, prepared_revision INTEGER "
        "NOT NULL, terminal_revision INTEGER, completed_at TEXT, "
        "retire_after TEXT, canonical_request TEXT, terminal_result TEXT, "
        "terminal_error TEXT, ownership_receipt TEXT, recovery_action "
        "TEXT)"
    ),
    ("snapshots"): (
        "CREATE TABLE snapshots (snapshot_id TEXT PRIMARY KEY, cas_id "
        "TEXT NOT NULL, size INTEGER NOT NULL, captured_revision INTEGER "
        "NOT NULL, live INTEGER NOT NULL)"
    ),
    "snapshot_reservations": "CREATE TABLE snapshot_reservations (snapshot_id TEXT PRIMARY KEY)",
    ("dependencies"): (
        "CREATE TABLE dependencies (owner_kind TEXT NOT NULL, owner_id "
        "TEXT NOT NULL, cas_id TEXT NOT NULL, dependency_kind TEXT NOT "
        "NULL, PRIMARY KEY(owner_kind,owner_id,cas_id,dependency_kind))"
    ),
    ("replacement_completion"): (
        "CREATE TABLE replacement_completion (operation_id TEXT PRIMARY "
        "KEY, target_generation TEXT NOT NULL, cas_binding TEXT NOT NULL, "
        "revision_before INTEGER NOT NULL, revision_after INTEGER NOT "
        "NULL, completed INTEGER NOT NULL, predecessor_root_commitment TEXT NOT NULL, "
        "successor_root_commitment TEXT NOT NULL)"
    ),
    "tree_totals": (
        "CREATE TABLE tree_totals (live_entries INTEGER NOT NULL, entry_bytes INTEGER NOT NULL, "
        "live_files INTEGER NOT NULL)"
    ),
    "coordinator_state": "CREATE TABLE coordinator_state (unsettled_operation TEXT)",
    "cas_index_position": (
        "CREATE TABLE cas_index_position (store_identity TEXT NOT NULL, journal_length "
        "INTEGER NOT NULL, event_count INTEGER NOT NULL, last_offset INTEGER, last_length "
        "INTEGER, last_digest TEXT, last_at TEXT, last_event_id TEXT)"
    ),
    "cas_records": (
        "CREATE TABLE cas_records (event_id TEXT PRIMARY KEY, cas_id TEXT NOT NULL, "
        "offset INTEGER NOT NULL, length INTEGER NOT NULL, digest TEXT NOT NULL)"
    ),
    # Per-object completeness witness over that object's located rows (R1).
    "cas_objects": (
        "CREATE TABLE cas_objects (cas_id TEXT PRIMARY KEY, records INTEGER NOT NULL, "
        "rows_digest TEXT NOT NULL)"
    ),
}
# Derived tables: trusted only to locate bytes that every use re-verifies.
_DERIVED = ("cas_index_position", "cas_records", "cas_objects")
_INDEXES = {
    "live_names": (
        "CREATE UNIQUE INDEX live_names ON entries(parent_id,comparison_key) WHERE live=1"
    ),
    "entries_touched": "CREATE INDEX entries_touched ON entries(touched_revision,entry_id)",
    "operations_state": "CREATE INDEX operations_state ON operations(state)",
    "cas_records_object": "CREATE INDEX cas_records_object ON cas_records(cas_id,offset)",
}
_USER_VERSION = 2


def create_schema(connection: sqlite3.Connection) -> None:
    """Create the exact v2 schema inside the caller's open transaction."""
    for statement in _TABLES.values():
        connection.execute(statement)
    for statement in _INDEXES.values():
        connection.execute(statement)
    connection.execute(f"PRAGMA user_version={_USER_VERSION}")


def entry_bytes(entry: n.EntryRecord) -> int:
    """Canonical byte length of one manifest entry, without the record LF."""
    return len(n.canonical_bytes(asdict(entry), limit=n.MAX_MANIFEST_BYTES)) - 1


_HANDLES: weakref.WeakSet[NamespaceHandle] = weakref.WeakSet()


def _after_fork() -> None:
    for handle in list(_HANDLES):
        for descriptor in handle.descriptors:
            os.close(descriptor)
        handle.descriptors.clear()
        handle._release_journal()
        # CPython's inherited SQLite object must never run sqlite3_close in the
        # child: that could roll back the parent's active journal. Intentionally
        # leak one reference in the child; the OS reclaims it at process exit.
        # This callback is for the qualified CPython runtime, not a fork-safe DB.
        if handle.connection is not None:
            ctypes.pythonapi.Py_IncRef(ctypes.py_object(handle.connection))
            handle.connection = None
        handle.closed = True


os.register_at_fork(after_in_child=_after_fork)


def _sync_dir(root: Path) -> None:
    store_state._fsync_directory(root)


def _commit(connection: sqlite3.Connection) -> None:
    connection.execute("COMMIT")


def _regular(file: Path) -> None:
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        n._refuse("namespace-root-unsafe")


def _descriptor(
    session: n.AuthorizedSession, namespace_root: Path, cas_root: Path
) -> n.CapabilityDescriptor:
    descriptor = session.describe_capabilities()
    if type(descriptor) is not n.CapabilityDescriptor:
        n._refuse("namespace-protection-unsupported")
    properties = descriptor.properties
    if (
        not isinstance(descriptor.protection_domain_id, str)
        or not descriptor.protection_domain_id
        or descriptor.capability_id is not None
        or not isinstance(properties, tuple)
        or len(set(properties)) != len(properties)
        or not n.REQUIRED_PROPERTIES <= set(properties)
        or set(properties) != n.REQUIRED_PROPERTIES
        or descriptor.namespace_root != namespace_root
        or descriptor.cas_root != cas_root
    ):
        n._refuse("namespace-protection-unsupported")
    if (
        namespace_root == cas_root
        or namespace_root in cas_root.parents
        or cas_root in namespace_root.parents
    ):
        n._refuse("namespace-root-unsafe")
    return descriptor


def _policy(policy: object) -> None:
    if policy != {"policy_version": 1, "unicode_data_version": n.unicodedata.unidata_version}:
        n._refuse("namespace-format-unsupported")
    if type(policy["policy_version"]) is not int:
        n._refuse("namespace-format-unsupported")


def _validate_entry(entry: n.EntryRecord, revision: int) -> None:
    n._uuid(entry.entry_id)
    n._integer(entry.changed_revision)
    if entry.changed_revision > revision or entry.kind not in {"file", "directory"}:
        n._refuse("namespace-format-unsupported")
    n._metadata(entry.metadata)
    if entry.parent_id is not None:
        n._uuid(entry.parent_id)
        if n._name(entry.name) != entry.name:
            n._refuse("namespace-format-unsupported")
    if entry.kind == "file":
        if not isinstance(entry.cas_id, str) or n._CAS.fullmatch(entry.cas_id) is None:
            n._refuse("namespace-format-unsupported")
        n._integer(entry.size)
    elif entry.cas_id is not None or entry.size is not None:
        n._refuse("namespace-format-unsupported")


def _tree(info: n.NamespaceInfo, entries: dict[str, n.EntryRecord]) -> None:
    if len(entries) > n.MAX_ENTRIES:
        n._refuse("namespace-limit-exceeded")
    root = entries.get(info.root_id)
    if root != n.EntryRecord(info.root_id, "directory", None, None, {}, None, None, 0):
        n._refuse("namespace-format-unsupported")
    children: dict[str, list[str]] = {}
    names: set[tuple[str, str]] = set()
    for entry in entries.values():
        _validate_entry(entry, info.revision)
        if entry.entry_id == info.root_id:
            continue
        parent = entries.get(entry.parent_id)
        if parent is None or parent.kind != "directory":
            n._refuse("namespace-format-unsupported")
        key = (entry.parent_id, n._key(entry.name))
        if key in names:
            n._refuse("namespace-name-conflict")
        names.add(key)
        children.setdefault(entry.parent_id, []).append(entry.entry_id)
    pending = [info.root_id]
    visited: set[str] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            n._refuse("namespace-cycle")
        visited.add(current)
        pending.extend(children.get(current, []))
    if visited != set(entries):
        n._refuse("namespace-cycle")
    from castle.namespace_snapshots import manifest

    n.canonical_bytes(manifest(info, entries), limit=n.MAX_MANIFEST_BYTES)


# Every boundary str.splitlines recognizes, as UTF-8 bytes. 0.3.0 reads the
# journal with read_text(encoding="utf-8").splitlines(); framing must equal it.
_TERMINATORS = (
    b"\n",
    b"\r",
    b"\x0b",
    b"\x0c",
    b"\x1c",
    b"\x1d",
    b"\x1e",
    b"\xc2\x85",
    b"\xe2\x80\xa8",
    b"\xe2\x80\xa9",
)


_DIAGNOSE_PASSTHROUGH = frozenset(
    {
        "namespace-access-denied",
        "namespace-session-expired",
        "namespace-storage-recovery-required",
    }
)


class _Overlay:
    """Reader-side or in-transaction CAS index state, keyed for bounded lookups.

    `replace` means the rows are the complete index (a full revalidation);
    otherwise they extend the stored rows.
    """

    def __init__(
        self,
        position: tuple[Any, ...],
        rows: list[tuple[Any, ...]],
        replace: bool,
        witnesses: dict[str, tuple[int, str]],
    ) -> None:
        self.position, self.rows, self.replace, self.witnesses = position, rows, replace, witnesses
        self.by_object: dict[str, list[tuple[Any, ...]]] = {}
        for row in rows:
            self.by_object.setdefault(row[1], []).append(row)
        self.events = {row[0] for row in rows}


class _Framing(Exception):
    """A located journal span no longer proves its record; revalidate in full."""


@contextmanager
def _touched_storage() -> Iterator[None]:
    """Touched storage that is gone, misplaced or a link is damage, not transient I/O."""
    try:
        yield
    except (FileNotFoundError, NotADirectoryError) as error:
        raise n.NamespaceError(
            "namespace-storage-recovery-required", effect="recovery-required"
        ) from error
    except OSError as error:
        if error.errno != errno.ELOOP:
            raise
        raise n.NamespaceError(
            "namespace-storage-recovery-required", effect="recovery-required"
        ) from error


def _frame(data: bytes) -> list[tuple[int, int, str, dict[str, Any]]]:
    """Frame journal bytes exactly as 0.3.0's strict authority reader does.

    Returns (offset, length, digest, record) per line, where each span covers
    one splitlines line plus its own terminator. Acceptance and refusal match
    journal._read_authority_journal on the same bytes.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise journal.JournalError("journal is not valid UTF-8") from exc
    spans: list[tuple[int, int, str, dict[str, Any]]] = []
    offset = 0
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        raw = line.encode("utf-8")
        where = f"journal line {number}"
        try:
            value = json.loads(line.splitlines()[0])
        except json.JSONDecodeError as exc:
            raise journal.JournalError(f"{where} is invalid JSON") from exc
        record = journal.parse_journal_record(value, where)
        spans.append((offset, len(raw), hashlib.sha256(raw).hexdigest(), record))
        offset += len(raw)
    journal.validate_journal_sequence([span[3] for span in spans], "journal line")
    return spans


def _witness(rows: list[tuple[Any, ...]]) -> tuple[int, str]:
    """(count, SHA-256 over the ordered located rows) of one object's records."""
    ordered = sorted(rows, key=lambda row: row[2])
    text = "".join(f"{row[0]}\t{row[2]}\t{row[3]}\t{row[4]}\n" for row in ordered)
    return len(ordered), hashlib.sha256(text.encode()).hexdigest()


def _witnesses_of(rows: list[tuple[Any, ...]]) -> dict[str, tuple[int, str]]:
    groups: dict[str, list[tuple[Any, ...]]] = {}
    for row in rows:
        groups.setdefault(row[1], []).append(row)
    return {key: _witness(group) for key, group in groups.items()}


def _span(descriptor: int, size: int, offset: int, length: int, digest: str) -> dict[str, Any]:
    """Prove one indexed span is still exactly one framed journal line."""
    if offset < 0 or length <= 0 or offset + length > size:
        raise _Framing("span outside the journal")
    raw = os.pread(descriptor, length, offset)
    if len(raw) != length or hashlib.sha256(raw).hexdigest() != digest:
        raise _Framing("span digest differs")
    try:
        line = raw.decode("utf-8")
    except UnicodeError as exc:
        raise _Framing("span is not UTF-8") from exc
    if line.splitlines(keepends=True) != [line]:
        raise _Framing("span is not exactly one line")
    body = line.splitlines()[0]
    if body == line and offset + length != size:
        raise _Framing("unterminated span before the journal end")
    if offset:
        before = os.pread(descriptor, min(offset, 3), offset - min(offset, 3))
        if not before.endswith(_TERMINATORS) or (before.endswith(b"\r") and raw[:1] == b"\n"):
            raise _Framing("span does not start at a line boundary")  # CR+LF is one terminator
    if raw.endswith(b"\r") and offset + length < size:
        if os.pread(descriptor, 1, offset + length) == b"\n":
            raise _Framing("CR+LF straddles the span")
    try:
        return journal.parse_journal_record(json.loads(body), "journal line")
    except (json.JSONDecodeError, journal.JournalError) as exc:
        raise _Framing("span record does not parse") from exc


def summarize(live: Mapping[str, n.EntryRecord]) -> tuple[int, int, int]:
    """(live entries, manifest entry bytes, live files) over a set of live entries."""
    return (
        len(live),
        sum(map(entry_bytes, live.values())),
        sum(entry.kind == "file" for entry in live.values()),
    )


def astuple_status(status: n.OperationStatus) -> tuple[Any, ...]:
    return (
        status.namespace_id,
        status.operation_id,
        status.state,
        status.result,
        status.error_code,
        status.next_action,
    )


def tree_totals(handle: NamespaceHandle) -> tuple[int, int, int]:
    """The single trusted summary row; a missing or repeated row is damage (CODE-F014)."""
    rows = handle.db.execute(
        "SELECT live_entries,entry_bytes,live_files FROM tree_totals"
    ).fetchall()
    if len(rows) != 1:
        n._refuse("namespace-storage-recovery-required")
    return rows[0]


def recompute_totals(handle: NamespaceHandle) -> None:
    """Replace tree_totals from every live entry: whole-sequence paths only."""
    handle.db.execute("DELETE FROM tree_totals")
    handle.db.execute("INSERT INTO tree_totals VALUES (?,?,?)", summarize(handle.entries()))


def dependency_rows(handle: NamespaceHandle) -> set[tuple[str, str, str, str]]:
    """All content dependencies: stored entry rows plus computed operation and snapshot rows."""
    from castle import namespace_snapshots as snapshots
    from castle import namespace_transactions as transactions

    rows = set(handle.db.execute("SELECT * FROM dependencies WHERE owner_kind='entry'"))
    return rows | transactions.operation_dependencies(handle) | snapshots.dependencies(handle)


class NamespaceHandle:
    """One process-lifetime owner; use as a context manager or close explicitly."""

    def __init__(
        self,
        namespace_root: Path,
        cas_root: Path,
        session: n.AuthorizedSession,
        *,
        initialize: n.InitializeRequest | None = None,
        recover: bool = False,
        cutover_recovery: object = None,
        read_only: bool = False,
        migrate_backup: Path | None = None,
    ) -> None:
        if read_only and (
            initialize is not None
            or recover
            or cutover_recovery is not None
            or migrate_backup is not None
        ):
            n._refuse("namespace-access-denied")
        self.read_only = read_only
        self.session = session
        n._authorize(
            session,
            "open-readonly"
            if read_only
            else "migrate"
            if migrate_backup is not None
            else "initialize"
            if initialize is not None
            else "recover"
            if recover
            else "open",
        )
        self.closed = False
        self.failed = False
        self.pid = os.getpid()
        self.mutex = threading.RLock()
        self.descriptors: list[int] = []
        self.connection: sqlite3.Connection | None = None
        self.recovered: list[str] = []
        self.root = Path(namespace_root).absolute()
        self.cas_root = Path(cas_root).absolute()
        self.domain_context = None
        self.recover_requested = recover
        self.operation_id: str | None = None
        self.write_started = False
        self.effect = "none"
        # Whole-sequence fold, set only by a full CAS check in this admission.
        self._objects: dict[str, tuple[Any, ...]] | None = None
        self._object_memo: dict[str, tuple[Any, ...] | None] = {}
        self._entry_memo: dict[str, tuple[n.EntryRecord, bool]] = {}
        self._ancestry_memo: set[str] = set()
        self._journal: tuple[int | None, int] | None = None
        # Reader-side (or in-transaction) CAS index state.
        self._cas_overlay: _Overlay | None = None
        self.swapped = False
        self.publication_receipts = {}
        self.snapshot_cache = {}
        self.pending_captures = {}
        self.exporting = False
        if initialize is not None and (
            type(initialize) is not n.InitializeRequest
            or initialize.protection != {"kind": "unprotected"}
        ):
            n._refuse("namespace-protection-unsupported")
        try:
            if self.root.is_symlink() or self.cas_root.is_symlink():
                n._refuse("namespace-root-unsafe")
            self.root = self.root.resolve()
            self.cas_root = self.cas_root.resolve(strict=True)
            cas_stat = self.cas_root.stat()
            self.cas_root_inode = (cas_stat.st_dev, cas_stat.st_ino)
            self.descriptor = _descriptor(session, self.root, self.cas_root)
            if initialize is not None:
                if self.root.exists():
                    if not self.root.is_dir() or any(self.root.iterdir()):
                        n._refuse("namespace-root-unsafe")
                else:
                    self.root.mkdir(mode=0o700)
                    _sync_dir(self.root.parent)
            elif not self.root.is_dir():
                n._refuse("namespace-authority-missing")
            domain_context = session.acquire_domain_ownership()
            domain_context.__enter__()
            self.domain_context = domain_context
            self._lock(self.root / "coordinator.lock", create=initialize is not None)
            _HANDLES.add(self)
            if cutover_recovery is not None:
                from castle import namespace_excision as cutover

                self.cutover_result = cutover.recover(self, cutover_recovery)
                return
            with journal._stable_lock(self.cas_root, shared=self.read_only):
                if migrate_backup is not None:
                    from castle import namespace_migrate_v1 as migration

                    migration.run(self, migrate_backup)
                    if self.migration[1]:
                        self._open_whole(None)
                    else:
                        # A rerun after a committed migration repairs nothing:
                        # ordinary open, then the caller's refuse-only diagnosis.
                        self._open_touched()
                elif initialize is not None or recover:
                    self._open_whole(initialize)
                else:
                    self._open_touched()
                n._authorize(self.session, "ready")
        except n.NamespaceError:
            self.close()
            raise
        except (
            OSError,
            sqlite3.Error,
            journal.CastleError,
            ValueError,
            TypeError,
            KeyError,
        ) as error:
            self.close()
            # Keep the typed public error; chain the cause so its traceback
            # locates the failing layer. The CLI emits only the typed fields.
            raise n.NamespaceError(
                "namespace-storage-recovery-required", effect="recovery-required"
            ) from error
        except BaseException:
            self.close()
            raise

    def _open_whole(self, initialize: n.InitializeRequest | None) -> None:
        """Initialization and explicit recovery: the complete whole-set sequence."""
        from castle import namespace_transactions as transactions

        self._inventory(initializing=initialize is not None, deep=True)
        if initialize is not None:
            self._cas_check()
            self._initialize(initialize)
        else:
            self._read_anchor()
            self._cas_check(identity_only=True)
            self._open(integrity=True)
            self._refuse_pending_excise()
            self._validate(check_content=False, totals=False)
            self._inventory(deep=True)
            content_io.before_cas(self)
            self._cas_check()
        self._validate(totals=False)
        self._recover_receipts()
        prepared = self.db.execute(
            "SELECT operation_id FROM operations WHERE state IN ('staging','prepared')"
        ).fetchall()
        if len(prepared) > 1:
            n._refuse("namespace-storage-recovery-required")
        for (operation_id,) in prepared:
            transactions.finish(self, operation_id)
            self.recovered.append(operation_id)
        self._validate(totals=False)
        self._settle_summaries()
        self._validate()

    def _open_touched(self) -> None:
        """Ordinary and read-only open: checks proportional to touched records.

        Anything incomplete refuses recovery-required exactly as 0.3.0 does;
        no file or row is repaired here. Untouched damage is found on touch,
        at a whole-sequence operation, or by explicit diagnosis.
        """
        self._inventory()
        self._open()
        self._load_anchor()
        self._cas_check(identity_only=True)
        self._refuse_pending_excise()
        self._admission_checks(require_ready=False)
        self._cas_position()
        self._recover_light()

    def _refuse_pending_excise(self) -> None:
        pending = self.db.execute(
            "SELECT canonical_request FROM operations WHERE state IN ('staging','prepared')"
        ).fetchall()
        if any(n._decode(raw)["kind"] == "excise" for (raw,) in pending):
            n._refuse("namespace-storage-recovery-required")

    def _load_anchor(self) -> None:
        """Read the initialize receipt named by the SQL row, not by a directory scan."""
        info = self.load_info()
        n._uuid(info.namespace_id)
        if not (self.root / "operations" / info.namespace_id / "receipt.json").exists():
            n._refuse("namespace-authority-missing")
        receipt = self._receipt(info.namespace_id)
        if receipt["kind"] != "initialize" or receipt["state"] != "durable":
            n._refuse("namespace-authority-missing")
        anchor = n._info(receipt["initial_info"])
        if anchor.namespace_id != info.namespace_id or anchor.root_id != info.root_id:
            n._refuse("namespace-authority-missing")
        if anchor.protection != {"kind": "unprotected"}:
            n._refuse("namespace-protection-unsupported")
        self.anchor = anchor

    def _admission_checks(self, *, require_ready: bool = True) -> n.NamespaceInfo:
        """The namespace-row, epoch and reservation checks of every admission."""
        from castle import namespace_replacement as replacement

        info = self.load_info()
        n._uuid(info.namespace_id)
        n._uuid(info.root_id)
        n._uuid(info.database_generation)
        n._integer(info.revision)
        _policy(info.policy)
        if info.protection != {"kind": "unprotected"}:
            n._refuse("namespace-protection-unsupported")
        if info.cas != self.binding:
            n._refuse("namespace-binding-mismatch")
        completions = self.db.execute("SELECT * FROM replacement_completion").fetchall()
        self.epoch_intervals = replacement.epochs(
            self.anchor, info, completions, replacement.read(self.root), allow_pending=False
        )
        reserved = "SELECT 1 FROM entry_reservations WHERE entry_id=?"
        if self.db.execute(reserved, (info.root_id,)).fetchone() is None or any(
            self.db.execute(reserved, (identity,)).fetchone() for identity in self.authority_ids()
        ):
            n._refuse("namespace-format-unsupported")
        for row in completions:
            if self.db.execute(
                "SELECT state FROM operations WHERE operation_id=?", (row[0],)
            ).fetchone() != ("retired",):
                n._refuse("namespace-replacement-invalid")
        if require_ready and (
            self.db.execute("SELECT service_state FROM namespace").fetchone() != ("ready",)
            or self.db.execute(
                "SELECT 1 FROM operations WHERE state IN ('staging','prepared')"
            ).fetchone()
        ):
            n._refuse("namespace-storage-recovery-required")
        return info

    def _recover_light(self) -> None:
        """Refuse an incomplete namespace without recovery, reading only the marked row."""
        markers = self.db.execute("SELECT unsettled_operation FROM coordinator_state").fetchall()
        if len(markers) != 1:
            n._refuse("namespace-storage-recovery-required")
        if (
            self.db.execute("SELECT service_state FROM namespace").fetchone() != ("ready",)
            or self.db.execute(
                "SELECT 1 FROM operations WHERE state IN ('staging','prepared')"
            ).fetchone()
            or (markers[0][0] is not None and not self._settled(markers[0][0]))
        ):
            n._refuse("namespace-storage-recovery-required")

    def _settled(self, identity: str) -> bool:
        """True when every post-commit step of one terminal operation is proven done."""
        from castle import namespace_transactions as transactions

        row = transactions._row(self, identity)
        if row is None or row["state"] not in {"committed", "rejected"}:
            return False
        self._check_operation(row, self.load_info())
        directory = self.root / "operations" / identity
        if not directory.is_dir() or directory.is_symlink() or (directory / "receipt.tmp").exists():
            return False
        receipt = self._receipt(identity)
        request = n._decode(row["canonical_request"])
        if {**receipt, "state": "prepared"} != self._ownership(request, self.ownership_info(row)):
            n._refuse("namespace-storage-recovery-required")
        if (
            receipt["state"] != "durable"
            or content_io.has_payload(self, row)
            or content_io.decode_recipe(row["ownership_receipt"]).get("expiry")
        ):
            return False
        if request["kind"] == "expire-results" and row["state"] == "committed":
            for target in request["args"]["operation_ids"]:
                state = self.db.execute(
                    "SELECT state FROM operations WHERE operation_id=?", (target,)
                ).fetchone()
                if state == ("retired",) and (self.root / "operations" / target).exists():
                    return False
        return True

    def _settle_summaries(self) -> None:
        """After a whole pass, re-derive the CAS index, tree totals and marker."""
        self._cas_revalidate()
        live = self.entries()
        totals = [summarize(live)]
        current = self.db.execute(
            "SELECT live_entries,entry_bytes,live_files FROM tree_totals"
        ).fetchall()
        markers = self.db.execute("SELECT unsettled_operation FROM coordinator_state").fetchall()
        if current == totals and markers == [(None,)]:
            return
        self.write_started = True
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("DELETE FROM tree_totals")
        self.db.execute("INSERT INTO tree_totals VALUES (?,?,?)", totals[0])
        self.db.execute("DELETE FROM coordinator_state")
        self.db.execute("INSERT INTO coordinator_state VALUES (NULL)")
        _commit(self.db)
        self.write_started = False

    @property
    def objects(self) -> dict[str, tuple[Any, ...]]:
        """The whole folded CAS index; only after a full check in this admission."""
        if self._objects is None:
            n._refuse("namespace-storage-recovery-required")
        return self._objects

    @objects.setter
    def objects(self, value: dict[str, tuple[Any, ...]] | None) -> None:
        self._objects = value

    def content_row(self, cas_id: str) -> tuple[Any, ...] | None:
        """One object's folded index row, or None when the journal never recorded it.

        Inside a whole-sequence context the fold just proven by a full CAS check
        answers; otherwise the touched-record proof of handle.object does.
        """
        if self._objects is not None:
            return self._objects.get(cas_id)
        return self.object(cas_id)

    def object(self, cas_id: str) -> tuple[Any, ...] | None:
        """Prove one object from its located journal lines and its file, memoized."""
        if self.swapped:
            n._refuse("namespace-storage-recovery-required")
        if cas_id in self._object_memo:
            return self._object_memo[cas_id]
        try:
            records = self._object_records(cas_id)
        except _Framing:
            self._cas_revalidate()
            try:
                records = self._object_records(cas_id)
            except _Framing as exc:
                raise n.NamespaceError(
                    "namespace-storage-recovery-required", effect="recovery-required"
                ) from exc
        row = cas.expected_index_rows(records)[0][cas_id] if records else None
        if row is not None:
            self._object_file(cas_id, row[1])
        self._object_memo[cas_id] = row
        return row

    def _object_records(self, cas_id: str) -> list[dict[str, Any]]:
        descriptor, size = self._journal_descriptor()
        located = self._cas_rows(cas_id)
        # Completeness of the located set: its witness must match (R1). This
        # catches lost, reassigned or added rows; a coherent edit of both rows
        # and witness inside namespace.sqlite is not detectable here.
        if (_witness(located) if located else None) != self._cas_witness(cas_id):
            raise _Framing("located rows disagree with their witness")
        records = []
        for event_id, identity, offset, length, digest in located:
            if descriptor is None:
                raise _Framing("journal absent with indexed spans")
            record = _span(descriptor, size, offset, length, digest)
            if record["event_id"] != event_id or record["cas_id"] != identity:
                raise _Framing("span names another record")
            records.append(record)
        return records

    def _object_file(self, cas_id: str, size: int) -> None:
        file = cas.object_path(self.cas_root, cas_id)
        with _touched_storage():
            for directory in (file.parent.parent.parent.parent, *file.parents[:3][::-1]):
                if not stat.S_ISDIR(directory.lstat().st_mode):
                    n._refuse("namespace-storage-recovery-required")
            info = file.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != size
            or not cas._write_protected(info)
        ):
            n._refuse("namespace-storage-recovery-required")

    def _journal_descriptor(self) -> tuple[int | None, int]:
        """The journal opened once per admission: no link, a regular file, one link."""
        if self._journal is None:
            try:
                descriptor = os.open(
                    self.cas_root / journal.JOURNAL_FILENAME, os.O_RDONLY | os.O_NOFOLLOW
                )
            except FileNotFoundError:
                self._journal = (None, 0)
                return self._journal
            except OSError as error:
                if error.errno != errno.ELOOP:
                    raise
                raise n.NamespaceError(
                    "namespace-storage-recovery-required", effect="recovery-required"
                ) from error
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                os.close(descriptor)
                n._refuse("namespace-storage-recovery-required")
            self._journal = (descriptor, info.st_size)
        return self._journal

    def _release_journal(self) -> None:
        if self._journal is not None and self._journal[0] is not None:
            os.close(self._journal[0])
        self._journal = None

    def _identity_text(self) -> str:
        return n.canonical_bytes(asdict(self.binding)).decode()

    def _position(self) -> tuple[Any, ...] | None:
        if self._cas_overlay is not None:
            return self._cas_overlay.position
        rows = self.db.execute("SELECT * FROM cas_index_position").fetchall()
        return rows[0] if len(rows) == 1 else None

    def _cas_rows(self, cas_id: str) -> list[tuple[Any, ...]]:
        rows = []
        if self._cas_overlay is None or not self._cas_overlay.replace:
            rows = self.db.execute(
                "SELECT event_id,cas_id,offset,length,digest FROM cas_records "
                "WHERE cas_id=? ORDER BY offset",
                (cas_id,),
            ).fetchall()
        if self._cas_overlay is not None:
            rows += self._cas_overlay.by_object.get(cas_id, [])
        return sorted(rows, key=lambda row: row[2])

    def _known_event(self, event_id: str) -> bool:
        if self._cas_overlay is not None and event_id in self._cas_overlay.events:
            return True
        if self._cas_overlay is not None and self._cas_overlay.replace:
            return False
        return (
            self.db.execute("SELECT 1 FROM cas_records WHERE event_id=?", (event_id,)).fetchone()
            is not None
        )

    def _cas_witness(self, cas_id: str) -> tuple[int, str] | None:
        if self._cas_overlay is not None:
            if cas_id in self._cas_overlay.witnesses:
                return self._cas_overlay.witnesses[cas_id]
            if self._cas_overlay.replace:
                return None
        row = self.db.execute(
            "SELECT records,rows_digest FROM cas_objects WHERE cas_id=?", (cas_id,)
        ).fetchone()
        return tuple(row) if row is not None else None

    def _cas_store(
        self,
        position: tuple[Any, ...],
        rows: list[tuple[Any, ...]],
        *,
        replace: bool,
        witnesses: dict[str, tuple[int, str]] | None = None,
    ) -> None:
        """Persist located spans and their witnesses, or hold them in the overlay.

        Each touched object's witness is recomputed from its complete row set
        in the same step as its rows, so the two never diverge legitimately.
        """
        if witnesses is None:
            groups: dict[str, list[tuple[Any, ...]]] = {}
            for row in rows:
                groups.setdefault(row[1], []).append(row)
            if not replace:
                groups = {key: self._cas_rows(key) + group for key, group in groups.items()}
            witnesses = {key: _witness(group) for key, group in groups.items()}
        if self.read_only or self.db.in_transaction:
            if self._cas_overlay is not None and not replace:
                rows = self._cas_overlay.rows + rows
                witnesses = {**self._cas_overlay.witnesses, **witnesses}
                replace = self._cas_overlay.replace
            self._cas_overlay = _Overlay(position, rows, replace, witnesses)
            return
        self.db.execute("BEGIN IMMEDIATE")
        if replace:
            self.db.execute("DELETE FROM cas_records")
            self.db.execute("DELETE FROM cas_objects")
        self.db.executemany("INSERT INTO cas_records VALUES (?,?,?,?,?)", rows)
        self.db.executemany(
            "INSERT OR REPLACE INTO cas_objects VALUES (?,?,?)",
            [(key, *value) for key, value in sorted(witnesses.items())],
        )
        self.db.execute("DELETE FROM cas_index_position")
        self.db.execute("INSERT INTO cas_index_position VALUES (?,?,?,?,?,?,?,?)", position)
        _commit(self.db)

    def _cas_sidecars(self) -> None:
        """O(1) refusal of an interrupted CAS index writer's residue, as 0.3.0 refuses it.

        The index's row-for-row agreement with the journal is whole-sequence
        work, kept in full revalidation and diagnosis.
        """
        index = self.cas_root / cas.INDEX_FILENAME
        rollback = index.with_name(index.name + "-journal")
        wal = index.with_name(index.name + "-wal")
        if rollback.is_symlink() or wal.is_symlink():
            n._refuse("namespace-storage-recovery-required")
        if rollback.exists():
            with rollback.open("rb") as file:
                if file.read(8) == b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7":
                    n._refuse("namespace-storage-recovery-required")
        if wal.exists() and wal.stat().st_size:
            with _touched_storage(), index.open("rb") as file:
                header = file.read(100)
            with _touched_storage(), wal.open("rb") as file:
                head = file.read(32)
            if len(header) < 100 or len(head) < 32 or header[18:20] != b"\x02\x02":
                n._refuse("namespace-storage-recovery-required")
            page_size = int.from_bytes(header[16:18], "big")
            page_size = 65536 if page_size == 1 else page_size
            magic, version, wal_page_size = struct.unpack_from(">III", head)
            order = "<" if magic == 0x377F0682 else ">"
            if (
                magic not in (0x377F0682, 0x377F0683)
                or version != 3007000
                or wal_page_size != page_size
                or cas._wal_checksum(head[:24], order, (0, 0))
                != struct.unpack_from(">II", head, 24)
            ):
                n._refuse("namespace-storage-recovery-required")

    def _cas_position(self) -> None:
        """Bring the located journal index up to the journal's current bytes.

        An append costs only the appended bytes; anything else costs one full
        revalidation, which refuses recovery-required when the CAS is damaged.
        """
        self._cas_sidecars()
        self._object_memo.clear()
        self._release_journal()
        self._flush_overlay()
        try:
            self._catch_up(self._position())
        except _Framing:
            self._cas_revalidate()

    def _flush_overlay(self) -> None:
        """Persist a writer's in-transaction index state once no transaction is open."""
        overlay = self._cas_overlay
        if overlay is not None and not self.read_only and not self.db.in_transaction:
            self._cas_overlay = None
            self._cas_store(
                overlay.position, overlay.rows, replace=overlay.replace, witnesses=overlay.witnesses
            )

    def _catch_up(self, position: tuple[Any, ...] | None) -> None:
        if position is None or position[0] != self._identity_text():
            raise _Framing("no index position for this binding")
        _, length, count, last_offset, last_length, last_digest, last_at, last_event = position
        descriptor, size = self._journal_descriptor()
        if count == 0:
            if length != 0 or any(value is not None for value in position[3:]):
                raise _Framing("empty position is not empty")
            if descriptor is None:
                return
        elif descriptor is None:
            raise _Framing("journal absent with indexed events")
        else:
            last = _span(descriptor, size, last_offset, last_length, last_digest)
            if last_offset + last_length != length or (last["at"], last["event_id"]) != (
                last_at,
                last_event,
            ):
                raise _Framing("indexed tail moved")
        if size == length:
            return
        if size < length:
            raise _Framing("journal shrank")
        try:
            spans = _frame(os.pread(descriptor, size - length, length))
        except journal.JournalError as exc:
            raise _Framing("appended bytes do not frame") from exc
        previous = (
            journal.journal_sort_key({"at": last_at, "event_id": last_event}) if count else None
        )
        additions: dict[str, list[dict[str, Any]]] = {}
        for _, _, _, record in spans:
            if (previous is not None and journal.journal_sort_key(record) < previous) or (
                self._known_event(record["event_id"])
            ):
                raise _Framing("appended record precedes or repeats indexed events")
            additions.setdefault(record["cas_id"], []).append(record)
        for cas_id, records in additions.items():
            try:
                cas.expected_index_rows(self._object_records(cas_id) + records)
            except journal.JournalError as exc:
                raise _Framing("appended record contradicts its object") from exc
        rows = [
            (record["event_id"], record["cas_id"], length + offset, span_length, digest)
            for offset, span_length, digest, record in spans
        ]
        tail = spans[-1]
        self._cas_store(
            (
                self._identity_text(),
                size,
                count + len(spans),
                length + tail[0],
                tail[1],
                tail[2],
                tail[3]["at"],
                tail[3]["event_id"],
            ),
            rows,
            replace=False,
        )

    def _cas_revalidate(self) -> None:
        """0.3.0's full ordinary CAS check, then re-locate every journal line."""
        self._object_memo.clear()
        self._release_journal()
        paths = cas._authority_paths(self.cas_root)
        descriptor, size = self._journal_descriptor()
        data = os.pread(descriptor, size, 0) if descriptor is not None else b""
        if len(data) != size:
            n._refuse("namespace-storage-recovery-required")
        spans = _frame(data)
        records = [span[3] for span in spans]
        if records != journal._read_authority_journal(self.cas_root):
            n._refuse("namespace-storage-recovery-required")
        self._cas_light(paths, records)
        rows = [
            (record["event_id"], record["cas_id"], offset, length, digest)
            for offset, length, digest, record in spans
        ]
        tail = spans[-1] if spans else None
        position = (
            self._identity_text(),
            size,
            len(spans),
            *(
                (tail[0], tail[1], tail[2], tail[3]["at"], tail[3]["event_id"])
                if tail
                else (None,) * 5
            ),
        )
        if (
            self._position() == position
            and self._cas_overlay is None
            and sorted(
                self.db.execute("SELECT event_id,cas_id,offset,length,digest FROM cas_records")
            )
            == sorted(rows)
            and self._stored_witnesses() == _witnesses_of(rows)
        ):
            return
        self._cas_store(position, rows, replace=True)

    def _stored_witnesses(self) -> dict[str, tuple[int, str]]:
        return {
            key: (count, digest)
            for key, count, digest in self.db.execute(
                "SELECT cas_id,records,rows_digest FROM cas_objects"
            )
        }

    def _cas_light(self, paths: dict[str, Path], records: list[dict[str, Any]]) -> dict[str, Any]:
        """0.3.0 `_cas_check(full=False)` over already-read authority records."""
        objects, names = cas.expected_index_rows(records)
        if cas._index_errors(self.cas_root, objects, names):
            n._refuse("namespace-storage-recovery-required")
        remotes = cas._snapshot_remotes(self.cas_root)
        if any(
            len(row) != 5 or any(not isinstance(v, str) for v in row) or row[0] not in objects
            for row in remotes
        ):
            n._refuse("namespace-storage-recovery-required")
        for identity, file in paths.items():
            info = file.lstat()
            if (
                identity not in objects
                or info.st_size != objects[identity][1]
                or not cas._write_protected(info)
            ):
                n._refuse("namespace-storage-recovery-required")
        if set(objects) != set(paths):
            n._refuse("namespace-storage-recovery-required")
        return objects

    def touch_entry(self, identity: str, revision: int) -> tuple[n.EntryRecord, bool] | None:
        """Validate one entry row against current bytes; None when it does not exist."""
        if identity in self._entry_memo:
            return self._entry_memo[identity]
        row = self.db.execute(
            "SELECT kind,parent_id,name,metadata,cas_id,size,changed_revision,live,"
            "comparison_key,touched_revision FROM entries WHERE entry_id=?",
            (identity,),
        ).fetchone()
        if row is None:
            return None
        kind, parent, name, metadata, cas_id, size, changed, live, comparison, touched = row
        entry = n.EntryRecord(
            identity, kind, parent, name, n._decode(metadata), cas_id, size, changed
        )
        if live not in (0, 1) or comparison != (n._key(name) if name is not None else None):
            n._refuse("namespace-format-unsupported")
        _validate_entry(entry, revision)
        if (
            self.db.execute(
                "SELECT 1 FROM entry_reservations WHERE entry_id=?", (identity,)
            ).fetchone()
            is None
        ):
            n._refuse("namespace-format-unsupported")
        if (
            type(touched) is not int
            or not changed <= touched <= revision
            or (live and touched != changed)
        ):
            n._refuse("namespace-storage-recovery-required")
        dependencies = set(
            self.db.execute(
                "SELECT * FROM dependencies WHERE owner_kind='entry' AND owner_id=?", (identity,)
            )
        )
        if dependencies != ({("entry", identity, cas_id, "content")} if cas_id else set()):
            n._refuse("namespace-storage-recovery-required")
        if cas_id:
            found = self.content_row(cas_id)
            if found is None or found[1] != size:
                n._refuse("namespace-storage-recovery-required")
        self._entry_memo[identity] = (entry, bool(live))
        return self._entry_memo[identity]

    def touch_ancestry(self, entry: n.EntryRecord, info: n.NamespaceInfo) -> None:
        """Validate a live entry's parent chain to the canonical root."""
        seen: list[str] = []
        current = entry
        while current.entry_id != info.root_id and current.entry_id not in self._ancestry_memo:
            if current.entry_id in seen:
                n._refuse("namespace-cycle")
            seen.append(current.entry_id)
            parent = (
                self.touch_entry(current.parent_id, info.revision)
                if current.parent_id is not None
                else None
            )
            if parent is None or not parent[1] or parent[0].kind != "directory":
                n._refuse("namespace-format-unsupported")
            current = parent[0]
        if current.entry_id == info.root_id and current != n.EntryRecord(
            info.root_id, "directory", None, None, {}, None, None, 0
        ):
            n._refuse("namespace-format-unsupported")
        self._ancestry_memo.update(seen)

    def _diagnose(self) -> dict[str, Any]:
        with self.admitted():
            return self.diagnose_admitted()

    def diagnose_admitted(self) -> dict[str, Any]:
        """Refuse-only whole-sequence check of every record; repairs nothing.

        Finds damage that touched-record admission leaves for touch time. The
        caller holds an admission. Any finding other than an access or session
        refusal is damage: it refuses recovery-required, chaining the specific
        cause, so the admission latches the handle.
        """
        try:
            return self._diagnose_whole()
        except n.NamespaceError as error:
            if error.code in _DIAGNOSE_PASSTHROUGH:
                raise
            raise n.NamespaceError(
                "namespace-storage-recovery-required", effect="recovery-required"
            ) from error

    def _diagnose_whole(self) -> dict[str, Any]:
        if self.db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            n._refuse("namespace-storage-recovery-required")
        self._inventory(deep=True)
        anchor = self.anchor
        self._read_anchor()
        if self.anchor != anchor:
            n._refuse("namespace-storage-recovery-required")
        self._cas_check()
        self._validate()
        requested, self.recover_requested = self.recover_requested, False
        try:
            self._recover_receipts()
        finally:
            self.recover_requested = requested
        descriptor, size = self._journal_descriptor()
        data = os.pread(descriptor, size, 0) if descriptor is not None else b""
        spans = _frame(data)
        located = sorted(
            (record["event_id"], record["cas_id"], offset, length, digest)
            for offset, length, digest, record in spans
        )
        overlay = self._cas_overlay
        if overlay is not None and overlay.replace:
            indexed = overlay.rows
        else:
            indexed = self.db.execute(
                "SELECT event_id,cas_id,offset,length,digest FROM cas_records"
            ).fetchall() + (overlay.rows if overlay else [])
        position = self._position()
        witnesses = (
            overlay.witnesses
            if overlay is not None and overlay.replace
            else {**self._stored_witnesses(), **(overlay.witnesses if overlay else {})}
        )
        if (
            sorted(indexed) != located
            or witnesses != _witnesses_of(located)
            or position is None
            or position[1:3] != (size, len(spans))
        ):
            n._refuse("namespace-storage-recovery-required")
        info = self.load_info()
        counts = dict(self.db.execute("SELECT live,COUNT(*) FROM entries GROUP BY live").fetchall())
        return {
            "namespace_id": info.namespace_id,
            "database_generation": info.database_generation,
            "revision": info.revision,
            "journal_length": size,
            "journal_sha256": hashlib.sha256(data).hexdigest(),
            "live_entries": counts.get(1, 0),
            "live_files": self.db.execute("SELECT live_files FROM tree_totals").fetchone()[0],
            "retained_entries": counts.get(0, 0),
            "operations": self.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0],
            "objects": len(self.objects),
        }

    def forget_touched(self) -> None:
        """Drop per-admission proofs after this handle commits new rows."""
        self._entry_memo.clear()
        self._ancestry_memo.clear()

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            n._refuse("namespace-storage-recovery-required")
        return self.connection

    def _lock(self, file: Path, *, create: bool) -> None:
        flags = (os.O_RDONLY if self.read_only else os.O_RDWR) | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(file, flags | (os.O_CREAT if create else 0), 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size:
                n._refuse("namespace-root-unsafe")
            try:
                mode = fcntl.LOCK_SH if self.read_only else fcntl.LOCK_EX
                fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            except BlockingIOError:
                n._refuse("namespace-coordinator-busy")
            self.descriptors.append(descriptor)
            if create:
                os.fsync(descriptor)
                _sync_dir(file.parent)
        except BaseException:
            if descriptor not in self.descriptors:
                os.close(descriptor)
            raise

    def _inventory(self, *, initializing: bool = False, deep: bool = False) -> None:
        """Check the namespace root; deep also walks every operation directory."""
        if self.read_only:
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = self.root / ("namespace.sqlite" + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    n._refuse("namespace-storage-recovery-required")
        allowed = (
            {"coordinator.lock"}
            if initializing
            else {"coordinator.lock", "operations", "namespace.sqlite", "namespace.sqlite-journal"}
        )
        names = {p.name for p in self.root.iterdir()}
        from castle import namespace_replacement as replacement

        marker = replacement.read(self.root)
        if marker is not None:
            if initializing or marker["stage"] != "complete":
                n._refuse("namespace-storage-recovery-required")
            allowed.add("replacement.json")
        if names - allowed:
            n._refuse("namespace-storage-recovery-required")
        for file in self.root.iterdir():
            if file.name == "operations":
                if file.is_symlink() or not file.is_dir():
                    n._refuse("namespace-root-unsafe")
                for directory in file.iterdir() if deep else ():
                    n._uuid(directory.name)
                    if directory.is_symlink() or not directory.is_dir():
                        n._refuse("namespace-root-unsafe")
                    content_io.inventory(self, directory, structural=self.connection is None)
                    for child in directory.iterdir():
                        _regular(child)
            else:
                _regular(file)
        if not initializing and not {"operations", "namespace.sqlite"} <= names:
            n._refuse("namespace-authority-missing")

    def _connect(self, *, create: bool) -> None:
        file = self.root / "namespace.sqlite"
        if create:
            descriptor = os.open(file, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
        self.connection = sqlite3.connect(
            file.as_uri() + ("?mode=ro" if self.read_only else "?mode=rw"),
            uri=True,
            isolation_level=None,
            check_same_thread=False,
            timeout=0,
        )
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.read_only:
            self.db.execute("PRAGMA query_only=ON")
            self.db.execute("PRAGMA temp_store=MEMORY")
            if self.db.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                n._refuse("namespace-storage-recovery-required")
            return
        if self.db.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            n._refuse("namespace-protection-unsupported")
        self.db.execute("PRAGMA synchronous=EXTRA")
        self.db.execute("PRAGMA fullfsync=ON")
        if (
            self.db.execute("PRAGMA foreign_keys").fetchone() != (1,)
            or self.db.execute("PRAGMA synchronous").fetchone() != (3,)
            or self.db.execute("PRAGMA fullfsync").fetchone() != (1,)
        ):
            n._refuse("namespace-protection-unsupported")

    def _initialize(self, request: n.InitializeRequest) -> None:
        if type(request) is not n.InitializeRequest:
            n._refuse()
        n._uuid(request.namespace_id)
        n._uuid(request.root_id)
        if request.namespace_id == request.root_id:
            n._refuse()
        _policy(request.policy)
        if request.protection != {"kind": "unprotected"}:
            n._refuse("namespace-protection-unsupported")
        if request.expected_cas != self.binding:
            n._refuse("namespace-binding-mismatch")
        info = n.NamespaceInfo(
            request.namespace_id,
            request.root_id,
            dict(request.policy),
            self.binding,
            0,
            str(uuid.uuid4()),
            {"kind": "unprotected"},
        )
        n.canonical_bytes(asdict(info))
        (self.root / "operations").mkdir(mode=0o700)
        _sync_dir(self.root)
        self._evidence(asdict(request), info, kind="initialize", state="prepared")
        self._connect(create=True)
        self.db.execute("BEGIN IMMEDIATE")
        create_schema(self.db)
        self.db.execute("INSERT INTO tree_totals VALUES (0,0,0)")
        self.db.execute("INSERT INTO coordinator_state VALUES (NULL)")
        self.db.execute(
            "INSERT INTO namespace VALUES (?,?,?,?,?,?,?,?,?)",
            (
                info.namespace_id,
                info.root_id,
                1,
                info.policy["unicode_data_version"],
                0,
                n.canonical_bytes(asdict(info.cas)).decode(),
                info.database_generation,
                n.canonical_bytes(info.protection).decode(),
                "ready",
            ),
        )
        self.db.execute("INSERT INTO entry_reservations VALUES (?)", (info.root_id,))
        self.write_entry(n.EntryRecord(info.root_id, "directory", None, None, {}, None, None, 0))
        _commit(self.db)
        with (self.root / "namespace.sqlite").open("rb") as source:
            os.fsync(source.fileno())
        _sync_dir(self.root)
        self._receipt_state(info.namespace_id, "durable")
        self.anchor = info

    def _read_anchor(self) -> None:
        initial = []
        # Read only closed control receipts here, before any CAS content or SQL
        # access. Mutation request files can be absent during owned expiry.
        for directory in (self.root / "operations").iterdir():
            file = directory / "receipt.json"
            if not file.exists():
                continue
            try:
                receipt = n._decode(file.read_bytes())
            except n.NamespaceError:
                continue  # A prepared SQL recipe must prove any partial mutation.
            if isinstance(receipt, dict) and receipt.get("kind") == "initialize":
                initial.append(self._receipt(directory.name))
        if len(initial) != 1 or initial[0]["state"] != "durable":
            n._refuse("namespace-authority-missing")
        self.anchor = n._info(initial[0]["initial_info"])
        if self.anchor.protection != {"kind": "unprotected"}:
            n._refuse("namespace-protection-unsupported")

    def _open(self, *, integrity: bool = False) -> None:
        self._connect(create=False)
        if integrity and self.db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            n._refuse("namespace-storage-recovery-required")
        definitions = dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        if not definitions:
            # Schema and rows commit together before the initialize receipt turns
            # durable, so an empty database is an interrupted initialize.
            n._refuse("namespace-authority-missing")
        if definitions != _TABLES:
            n._refuse("namespace-format-unsupported")
        # Automatic primary-key indexes carry NULL sql and are not part of the closed set.
        indexes = dict(
            self.db.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
            )
        )
        if indexes != _INDEXES:
            n._refuse("namespace-format-unsupported")
        if self.db.execute("PRAGMA user_version").fetchone() != (_USER_VERSION,):
            n._refuse("namespace-format-unsupported")
        if self.db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
            n._refuse("namespace-format-unsupported")

    def _cas_check(self, *, full: bool = True, identity_only: bool = False) -> None:
        current = self.cas_root.lstat()
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != self.cas_root_inode
        ):
            n._refuse("namespace-binding-mismatch")
        try:
            binding = store_state.read_store_identity(self.cas_root)
        except store_state.StoreStateError as error:
            if error.code in {"store-retired", "store-not-active", "store-epoch-mismatch"}:
                n._refuse("namespace-binding-mismatch")
            n._refuse("namespace-storage-recovery-required")
        if hasattr(self, "binding") and self.binding != binding:
            n._refuse("namespace-binding-mismatch")
        self.binding = binding
        if identity_only:
            return
        if full:
            records, paths, _ = cas._validated_authority(self.cas_root, derived=True)
        else:
            paths = cas._authority_paths(self.cas_root)
            records = journal._read_authority_journal(self.cas_root)
            self._cas_light(paths, records)
        self.objects, _ = cas.expected_index_rows(records)
        if set(self.objects) != set(paths):
            n._refuse("namespace-storage-recovery-required")

    def _receipt(self, identity: str) -> dict[str, Any]:
        n._uuid(identity)
        directory = self.root / "operations" / identity
        for name in ("receipt.json", "request.json"):
            _regular(directory / name)
        receipt = n._decode((directory / "receipt.json").read_bytes())
        if (
            not isinstance(receipt, dict)
            or set(receipt)
            != {
                "schema",
                "namespace_id",
                "operation_id",
                "database_generation",
                "cas_binding",
                "request_fingerprint",
                "state",
                "kind",
                "initial_info",
            }
            or receipt["schema"] != "castle.namespace_ownership.v1"
            or receipt["operation_id"] != identity
        ):
            n._refuse("namespace-storage-recovery-required")
        if receipt["state"] not in {"prepared", "durable", "storage-fault"} or receipt[
            "kind"
        ] not in {"initialize", "mutation"}:
            n._refuse("namespace-storage-recovery-required")
        raw = (directory / "request.json").read_bytes()
        request = n._decode(raw)
        fingerprint = (
            hashlib.sha256(raw).hexdigest()
            if receipt["kind"] == "initialize"
            else n._fingerprint(request)
        )
        if fingerprint != receipt["request_fingerprint"]:
            n._refuse("namespace-storage-recovery-required")
        return receipt

    def _ownership(
        self,
        request: dict[str, Any],
        info: n.NamespaceInfo,
        *,
        kind: str = "mutation",
        state: str = "prepared",
    ) -> dict[str, Any]:
        identity = info.namespace_id if kind == "initialize" else request["operation_id"]
        raw = n.canonical_bytes(request)
        return {
            "schema": "castle.namespace_ownership.v1",
            "namespace_id": info.namespace_id,
            "operation_id": identity,
            "database_generation": info.database_generation,
            "cas_binding": asdict(info.cas),
            "request_fingerprint": hashlib.sha256(raw).hexdigest()
            if kind == "initialize"
            else n._fingerprint(request),
            "state": state,
            "kind": kind,
            "initial_info": asdict(info) if kind == "initialize" else None,
        }

    def _evidence(
        self,
        request: dict[str, Any],
        info: n.NamespaceInfo,
        *,
        kind: str = "mutation",
        state: str = "prepared",
    ) -> None:
        receipt = self._ownership(request, info, kind=kind, state=state)
        directory = self.root / "operations" / receipt["operation_id"]
        directory.mkdir(mode=0o700)
        for name, payload in [
            ("request.json", n.canonical_bytes(request)),
            ("receipt.json", n.canonical_bytes(receipt)),
        ]:
            descriptor = os.open(
                directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        _sync_dir(directory)
        _sync_dir(directory.parent)

    def _restore_evidence(self, row: dict[str, Any]) -> None:
        """Repair only slots proven by the independently validated SQL recipe.

        The intent commits before allocation. A missing file or exact prefix of
        its canonical bytes is a supported interrupted write. Different bytes,
        unknown files, links or wrong authority remain a refusal.
        """
        request = n._decode(row["canonical_request"])
        recipe = content_io.recipe(self, row)[0]
        expected = self._ownership(request, self.ownership_info(row))
        if (
            not isinstance(recipe, dict)
            or set(recipe) != {"receipt", "expiry", "publication", "precondition_error", "snapshot"}
            or recipe["receipt"] != expected
        ):
            n._refuse("namespace-storage-recovery-required")
        directory = self.root / "operations" / row["operation_id"]
        if not directory.exists():
            directory.mkdir(mode=0o700)
        for name, payload in [
            ("request.json", n.canonical_bytes(request)),
            ("receipt.json", n.canonical_bytes(expected)),
        ]:
            file = directory / name
            if file.exists():
                _regular(file)
                raw = file.read_bytes()
                if name == "receipt.json":
                    variants = [
                        n.canonical_bytes({**expected, "state": value})
                        for value in ("prepared", "durable")
                    ]
                    if raw in variants:
                        continue
                if raw == payload:
                    continue
                if row["state"] not in {"staging", "prepared"} or not payload.startswith(raw):
                    n._refuse("namespace-storage-recovery-required")
            descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
        temporary = directory / "receipt.tmp"
        if temporary.exists():
            _regular(temporary)
            raw = temporary.read_bytes()
            if not any(
                n.canonical_bytes({**expected, "state": value}).startswith(raw)
                for value in ("prepared", "durable")
            ):
                n._refuse("namespace-storage-recovery-required")
            temporary.unlink()
        _sync_dir(directory)
        _sync_dir(directory.parent)

    def _receipt_state(self, identity: str, state: str) -> None:
        receipt = self._receipt(identity)
        receipt["state"] = state
        directory = self.root / "operations" / identity
        temporary = directory / "receipt.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(n.canonical_bytes(receipt))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / "receipt.json")
        _sync_dir(directory)

    def _reassert(self) -> None:
        """Establish a new durable logical SQL image, never a forensic replacement.

        Delete/reinsert forces every retained authority row through a fresh
        SQLite transaction. Mere visibility or syncing a fresh descriptor does
        not prove an earlier uncertain commit durable. This slow path is only
        for interrupted completion or explicitly requested storage recovery.
        """
        saved = {table: self.db.execute(f"SELECT * FROM {table}").fetchall() for table in _TABLES}
        self.db.execute("BEGIN IMMEDIATE")
        self.db.execute("PRAGMA defer_foreign_keys=ON")
        for table in reversed(_TABLES):
            self.db.execute(f"DELETE FROM {table}")
        for table, rows in saved.items():
            for row in rows:
                self.db.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)})", row)
        _commit(self.db)

    def _recover_receipts(self) -> None:
        from castle import namespace_transactions as transactions

        fault = self.db.execute("SELECT service_state FROM namespace").fetchone() != ("ready",)
        rows = [
            transactions._row(self, identity)
            for (identity,) in self.db.execute(
                "SELECT operation_id FROM operations WHERE state != 'retired'"
            ).fetchall()
        ]
        expected_ids = {self.anchor.namespace_id} | {row["operation_id"] for row in rows}
        retired = {
            identity
            for (identity,) in self.db.execute(
                "SELECT operation_id FROM operations WHERE state='retired'"
            )
        }
        directories = {p.name for p in (self.root / "operations").iterdir()}
        if directories - expected_ids - retired:
            n._refuse("namespace-storage-recovery-required")
        incomplete = fault or any(row["state"] in {"staging", "prepared"} for row in rows)
        incomplete |= bool(directories & retired)
        for row in rows:
            if row["state"] in {"staging", "prepared"}:
                continue
            directory = self.root / "operations" / row["operation_id"]
            if not directory.exists() or (directory / "receipt.tmp").exists():
                incomplete = True
                continue
            receipt = self._receipt(row["operation_id"])
            expected = self._ownership(
                n._decode(row["canonical_request"]), self.ownership_info(row)
            )
            if {**receipt, "state": "prepared"} != expected:
                n._refuse("namespace-storage-recovery-required")
            incomplete |= (
                receipt["state"] != "durable"
                or content_io.has_payload(self, row)
                or bool(content_io.decode_recipe(row["ownership_receipt"]).get("expiry"))
            )
        # Conservative restart rule: never infer an interrupted durability
        # boundary from visibility alone. Explicit recovery owns all repair and
        # cleanup; ordinary open has made no evidence-file changes at this point.
        if incomplete and not self.recover_requested:
            n._refuse("namespace-storage-recovery-required")
        if incomplete:
            for row in rows:
                self._restore_evidence(row)
        if incomplete:
            self.write_started = True
            self._reassert()
        for row in rows if incomplete else []:
            identity = row["operation_id"]
            if row["state"] in {"staging", "prepared"}:
                self.operation_id = identity
                self.effect = "recovery-required"
                try:
                    transactions.finish(self, identity)
                except n.NamespaceError:
                    if transactions._row(self, identity)["state"] != "rejected":
                        raise
                self.recovered.append(identity)
            else:
                transactions.cleanup_expired(self, row)
                content_io.cleanup(self, row)
                receipt = self._receipt(identity)
                if receipt["state"] != "durable":
                    self._receipt_state(identity, "durable")
                    self.recovered.append(identity)
        # Every surviving retired directory needs a committed cleanup recipe;
        # a UUID-shaped name alone never authorizes deletion.
        if any((self.root / "operations" / identity).exists() for identity in retired):
            n._refuse("namespace-storage-recovery-required")
        if incomplete:
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("UPDATE namespace SET service_state='ready'")
            _commit(self.db)
        self.operation_id = None
        self.effect = "none"
        self.write_started = False

    def load_info(self) -> n.NamespaceInfo:
        rows = self.db.execute("SELECT * FROM namespace").fetchall()
        if len(rows) != 1:
            n._refuse("namespace-format-unsupported")
        (
            identity,
            root,
            version,
            unicode_version,
            revision,
            binding,
            generation,
            protection,
            service,
        ) = rows[0]
        if service not in {"ready", "recovery-required"}:
            n._refuse("namespace-format-unsupported")
        return n.NamespaceInfo(
            identity,
            root,
            {"policy_version": version, "unicode_data_version": unicode_version},
            store_state._identity(n._decode(binding)),
            revision,
            generation,
            n._decode(protection),
        )

    def entries(self, *, live: bool = True) -> dict[str, n.EntryRecord]:
        query = (
            "SELECT entry_id,kind,parent_id,name,metadata,cas_id,size,changed_revision,"
            "live,comparison_key FROM entries" + (" WHERE live=1" if live else "")
        )
        result: dict[str, n.EntryRecord] = {}
        for row in self.db.execute(query):
            identity, kind, parent, name, metadata, cas_id, size, changed, live_flag, comparison = (
                row
            )
            entry = n.EntryRecord(
                identity, kind, parent, name, n._decode(metadata), cas_id, size, changed
            )
            if live_flag not in (0, 1) or comparison != (
                n._key(name) if name is not None else None
            ):
                n._refuse("namespace-format-unsupported")
            result[identity] = entry
        return result

    def vacate_entry(self, entry: n.EntryRecord) -> None:
        """Clear one live row's name slot before rewriting affected rows, keeping totals."""
        self.db.execute("UPDATE entries SET live=0 WHERE entry_id=?", (entry.entry_id,))
        self.db.execute(
            "UPDATE tree_totals SET live_entries=live_entries-1,entry_bytes=entry_bytes-?,"
            "live_files=live_files-?",
            (entry_bytes(entry), int(entry.kind == "file")),
        )

    def write_entry(
        self, entry: n.EntryRecord, *, live: bool = True, touched_revision: int | None = None
    ) -> None:
        """Upsert one entry row, its dependency row and the tree totals.

        A live row's touched revision is its changed revision; a tombstone's
        is the revision that deleted it, supplied by the caller.
        """
        if live:
            touched = entry.changed_revision
        elif touched_revision is None:
            raise ValueError("tombstone requires its deletion revision")
        else:
            touched = touched_revision
        previous = self.db.execute(
            "SELECT kind,parent_id,name,metadata,cas_id,size,changed_revision,live "
            "FROM entries WHERE entry_id=?",
            (entry.entry_id,),
        ).fetchone()
        count_delta, bytes_delta, files_delta = 0, 0, 0
        if previous is not None and previous[7] == 1:
            prior = n.EntryRecord(
                entry.entry_id, *previous[:3], n._decode(previous[3]), *previous[4:7]
            )
            count_delta, bytes_delta = -1, -entry_bytes(prior)
            files_delta = -int(prior.kind == "file")
        if live:
            count_delta, bytes_delta = count_delta + 1, bytes_delta + entry_bytes(entry)
            files_delta += int(entry.kind == "file")
        if count_delta or bytes_delta or files_delta:
            self.db.execute(
                "UPDATE tree_totals SET live_entries=live_entries+?,entry_bytes=entry_bytes+?,"
                "live_files=live_files+?",
                (count_delta, bytes_delta, files_delta),
            )
        self.db.execute(
            (
                "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?) ON "
                "CONFLICT(entry_id) DO UPDATE SET "
                "kind=excluded.kind,parent_id=excluded.parent_id,name=excluded.nam"
                "e,metadata=excluded.metadata,cas_id=excluded.cas_id,size=excluded"
                ".size,changed_revision=excluded.changed_revision,live=excluded.li"
                "ve,comparison_key=excluded.comparison_key,"
                "touched_revision=excluded.touched_revision"
            ),
            (
                entry.entry_id,
                entry.kind,
                entry.parent_id,
                entry.name,
                n.canonical_bytes(entry.metadata).decode(),
                entry.cas_id,
                entry.size,
                entry.changed_revision,
                int(live),
                n._key(entry.name) if entry.name is not None else None,
                touched,
            ),
        )
        self.db.execute(
            "DELETE FROM dependencies WHERE owner_kind='entry' AND owner_id=?", (entry.entry_id,)
        )
        if entry.cas_id:
            self.db.execute(
                "INSERT INTO dependencies VALUES ('entry',?,?,'content')",
                (entry.entry_id, entry.cas_id),
            )

    def _validate(
        self,
        *,
        check_content: bool = True,
        replacement_pending: bool = False,
        cutover_operation: str | None = None,
        totals: bool = True,
    ) -> None:
        """Whole-namespace validation: whole-sequence operations only.

        Recovery passes totals=False until it re-derives tree_totals and the
        coordinator marker; that mode skips both summary checks.
        """
        info = self.load_info()
        n._uuid(info.namespace_id)
        n._uuid(info.root_id)
        n._uuid(info.database_generation)
        n._integer(info.revision)
        _policy(info.policy)
        if info.protection != {"kind": "unprotected"}:
            n._refuse("namespace-protection-unsupported")
        if info.cas != self.binding:
            n._refuse("namespace-binding-mismatch")
        from castle import namespace_replacement as replacement

        marker = replacement.read(self.root)
        if cutover_operation is not None:
            from castle import namespace_excision as cutover

            _, intent = cutover._intent(self, cutover_operation)
            if intent["stage"] not in {"staging", "prepared"} or info != n._info(
                intent["source_info"]
            ):
                replacement.invalid()
            marker = intent["prior_marker"]
        self.epoch_intervals = replacement.epochs(
            self.anchor,
            info,
            self.db.execute("SELECT * FROM replacement_completion").fetchall(),
            marker,
            allow_pending=replacement_pending,
        )
        live_entries = self.entries()
        _tree(info, live_entries)
        # The two derived summaries are checked only when totals=True: recovery
        # re-derives both rows in _settle_summaries, so their damage, including
        # row cardinality, must not block the pass that repairs it.
        if totals:
            summary = self.db.execute(
                "SELECT live_entries,entry_bytes,live_files FROM tree_totals"
            ).fetchall()
            if summary != [summarize(live_entries)]:
                n._refuse("namespace-storage-recovery-required")
            marker_rows = self.db.execute(
                "SELECT unsettled_operation FROM coordinator_state"
            ).fetchall()
            if len(marker_rows) != 1 or (
                marker_rows[0][0] is not None
                and self.db.execute(
                    "SELECT 1 FROM operations WHERE operation_id=?", marker_rows[0]
                ).fetchone()
                is None
            ):
                n._refuse("namespace-storage-recovery-required")
        if self.db.execute(
            "SELECT 1 FROM entries WHERE touched_revision<changed_revision OR touched_revision>? "
            "OR (live=1 AND touched_revision!=changed_revision) LIMIT 1",
            (info.revision,),
        ).fetchone():
            n._refuse("namespace-storage-recovery-required")
        reservations = {
            row[0] for row in self.db.execute("SELECT entry_id FROM entry_reservations")
        }
        for identity in reservations:
            n._uuid(identity)
        if info.root_id not in reservations or bool(self.authority_ids() & reservations):
            n._refuse("namespace-format-unsupported")
        expected = set()
        for entry in self.entries(live=False).values():
            _validate_entry(entry, info.revision)
            if entry.entry_id not in reservations:
                n._refuse("namespace-format-unsupported")
            if entry.cas_id:
                if check_content and (
                    entry.cas_id not in self.objects or self.objects[entry.cas_id][1] != entry.size
                ):
                    n._refuse("namespace-storage-recovery-required")
                expected.add(("entry", entry.entry_id, entry.cas_id, "content"))
        actual_entries = set(self.db.execute("SELECT * FROM dependencies WHERE owner_kind='entry'"))
        if actual_entries != expected:
            n._refuse("namespace-storage-recovery-required")

        from castle import namespace_transactions as transactions

        for (identity,) in self.db.execute("SELECT operation_id FROM operations").fetchall():
            n._uuid(identity)
            if identity in reservations or identity in self.authority_ids():
                n._refuse("namespace-format-unsupported")
            row = transactions._row(self, identity)
            if identity == cutover_operation:
                continue
            self._check_operation(row, info, check_content=check_content)

        # Only entry dependency rows are stored; operation and snapshot rows are
        # computed where used. Computing them here validates every recipe,
        # result and snapshot view they read.
        transactions.operation_dependencies(self)
        if self.db.execute(
            "SELECT 1 FROM dependencies WHERE owner_kind != 'entry' LIMIT 1"
        ).fetchone():
            n._refuse("namespace-storage-recovery-required")

        if self.db.execute("PRAGMA foreign_key_check").fetchall():
            n._refuse("namespace-storage-recovery-required")
        from castle import namespace_snapshots as snapshots

        snapshots.dependencies(self, check_content=check_content, allow_pending=replacement_pending)
        for (operation,) in self.db.execute("SELECT operation_id FROM replacement_completion"):
            if self.db.execute(
                "SELECT state FROM operations WHERE operation_id=?", (operation,)
            ).fetchone() != ("retired",):
                n._refuse("namespace-replacement-invalid")

    def _check_operation(
        self, row: dict[str, Any], info: n.NamespaceInfo, *, check_content: bool = True
    ) -> None:
        """0.3.0's per-row operation validation: full loop, status, replay and expiry."""
        from castle import namespace_transactions as transactions

        identity = row["operation_id"]
        n._uuid(identity)
        if (
            self.db.execute(
                "SELECT 1 FROM entry_reservations WHERE entry_id=?", (identity,)
            ).fetchone()
            or identity in self.authority_ids()
        ):
            n._refuse("namespace-format-unsupported")
        if row["state"] == "retired":
            if (
                any(
                    row[key] is not None
                    for key in (
                        "fingerprint",
                        "canonical_request",
                        "terminal_result",
                        "terminal_error",
                        "completed_at",
                        "retire_after",
                        "ownership_receipt",
                        "recovery_action",
                        "terminal_revision",
                    )
                )
                or row["prepared_revision"] != 0
            ):
                n._refuse("namespace-format-unsupported")
            return
        if (
            row["state"] not in {"staging", "prepared", "committed", "rejected"}
            or row["canonical_request"] is None
        ):
            n._refuse("namespace-format-unsupported")
        request = n._decode(row["canonical_request"])
        try:
            if (
                not isinstance(request, dict)
                or request.get("schema") != "castle.namespace_request.v1"
            ):
                n._refuse()
            reconstructed = n.MutationRequest(
                **{key: value for key, value in request.items() if key != "schema"}
            )
            reconstructed = replace(
                reconstructed, expected_cas=store_state._identity(reconstructed.expected_cas)
            )
            normalized = transactions.normalize(reconstructed)
            if n.canonical_bytes(normalized).decode() != row["canonical_request"]:
                n._refuse()
        except (n.NamespaceError, store_state.StoreStateError, TypeError, ValueError):
            n._refuse("namespace-storage-recovery-required")
        content_io.recipe(self, row)
        if (
            request["operation_id"] != identity
            or request["namespace_id"] != info.namespace_id
            or n._fingerprint(request) != row["fingerprint"]
        ):
            n._refuse("namespace-storage-recovery-required")
        n._integer(row["prepared_revision"])
        if row["prepared_revision"] > info.revision or (
            row["state"] in {"staging", "prepared"} and row["prepared_revision"] != info.revision
        ):
            n._refuse("namespace-storage-recovery-required")
        if row["state"] == "committed":
            result = n._result(n._decode(row["terminal_result"]))
            if (
                result.namespace_id != info.namespace_id
                or result.operation_id != identity
                or result.request_fingerprint != row["fingerprint"]
                or result.kind != request["kind"]
                or result.cas_binding != self.ownership_info(row).cas
                or request["expected_cas"] != asdict(result.cas_binding)
                or request["expected_revision"] != result.revision_before
                or result.revision_after != result.revision_before + 1
                or result.revision_after > info.revision
                or result.revision_before != row["prepared_revision"]
                or result.revision_after != row["terminal_revision"]
                or result.affected_entry_ids != sorted(set(result.affected_entry_ids))
            ):
                n._refuse("namespace-storage-recovery-required")
            if request["kind"] == "snapshot":
                from castle import namespace_snapshots as snapshots

                saved = n._snapshot(content_io.decode_recipe(row["ownership_receipt"])["snapshot"])
                if (
                    result.snapshot != saved
                    or saved.snapshot_id != request["args"]["snapshot_id"]
                    or saved.captured_revision != result.revision_before
                    or (
                        result.cas_binding == info.cas
                        and snapshots.catalog(self, saved.snapshot_id, live=False) != saved
                    )
                ):
                    n._refuse("namespace-storage-recovery-required")
                if result.cas_binding != info.cas:
                    historical = snapshots.decode(
                        self,
                        saved,
                        check_content=check_content,
                        expected_binding=result.cas_binding,
                    )
                    snapshots.validate_provenance(self, historical, result.cas_binding)
            elif result.snapshot is not None:
                n._refuse("namespace-storage-recovery-required")
        if row["state"] == "rejected":
            error = n._terminal_error(n._decode(row["terminal_error"]))
            if (
                error.namespace_id != info.namespace_id
                or error.operation_id != identity
                or error.actual_revision != row["prepared_revision"]
                or row["terminal_revision"] != row["prepared_revision"]
            ):
                n._refuse("namespace-storage-recovery-required")

    def ownership_info(self, row: dict[str, Any]) -> n.NamespaceInfo:
        from castle import namespace_replacement as replacement

        info = self.load_info()
        intervals = getattr(self, "epoch_intervals", None)
        if intervals is None:
            # Epoch intervals come from the marker and completion rows checked at
            # every admission; never default them to the current binding.
            n._refuse("namespace-storage-recovery-required")
        binding, generation = replacement.epoch_at(intervals, row["prepared_revision"])
        return replace(info, cas=binding, database_generation=generation)

    def authority_ids(self) -> set[str]:
        info = self.load_info()
        return {info.namespace_id, info.database_generation, self.anchor.database_generation} | {
            row[0]
            for row in self.db.execute("SELECT target_generation FROM replacement_completion")
        }

    def _io_failed(self) -> None:
        self.failed = True
        try:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("UPDATE namespace SET service_state='recovery-required'")
            _commit(self.db)
        except (OSError, sqlite3.Error):
            # The original uncertain storage failure remains a refusal. Failure
            # to persist its latch is never evidence of durable completion.
            pass

    def _persist_derived_index(self) -> None:
        """Write a writer's in-transaction CAS index rebuild before the admission ends.

        The rows are derived and re-proven at every later admission, so a
        failure here never changes the admitted result: the overlay is dropped
        and the next admission revalidates (CODE-F012).
        """
        try:
            self._flush_overlay()
        except (OSError, sqlite3.Error):
            self._cas_overlay = None
            if self.connection is not None and self.db.in_transaction:
                self.db.execute("ROLLBACK")

    @contextmanager
    def admitted(self) -> Iterator[None]:
        n._authorize(self.session, "admit")
        if self.pid != os.getpid() or self.closed or self.failed:
            n._refuse("namespace-storage-recovery-required")
        with self.mutex:
            if self.exporting:
                n._refuse("namespace-coordinator-busy")
            if self.closed or self.failed:
                n._refuse("namespace-storage-recovery-required")
            self.operation_id = None
            self.write_started = False
            self.effect = "none"
            self._objects = None
            self.forget_touched()
            try:
                self._inventory()
                with journal._stable_lock(self.cas_root, shared=self.read_only):
                    if self.session.describe_capabilities() != self.descriptor:
                        n._refuse("namespace-protection-unsupported")
                    self._cas_check(identity_only=True)
                    self._admission_checks()
                    self._cas_position()
                    yield
                    self._persist_derived_index()
            except n.NamespaceError as error:
                if error.code == "namespace-storage-recovery-required":
                    self.failed = True
                if error.namespace_id is None:
                    error.namespace_id = self.anchor.namespace_id
                if self.connection is not None and self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                    self.failed = True
                if self.operation_id is not None:
                    error.namespace_id = self.anchor.namespace_id
                    error.operation_id = self.operation_id
                    # A durable domain rejection proves no namespace change.
                    row = self.db.execute(
                        "SELECT state FROM operations WHERE operation_id=?", (self.operation_id,)
                    ).fetchone()
                    error.effect = "none" if row == ("rejected",) else self.effect
                raise
            except (OSError, sqlite3.Error) as error:
                code = getattr(error, "sqlite_errorcode", None)
                recoverable_lock = code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                if code is not None and code & 0xFF in {
                    sqlite3.SQLITE_CORRUPT,
                    sqlite3.SQLITE_NOTADB,
                }:
                    # A malformed page is damage, not transient I/O: ordinary
                    # open no longer runs integrity_check, so the touch that
                    # finds it latches (CODE-F021).
                    self.failed = True
                    with suppress(sqlite3.Error):
                        if self.db.in_transaction:
                            self.db.execute("ROLLBACK")
                    raise n.NamespaceError(
                        "namespace-storage-recovery-required",
                        self.anchor.namespace_id,
                        self.operation_id,
                        effect=self.effect if self.write_started else "none",
                    ) from error
                if self.write_started:
                    if recoverable_lock:
                        self.failed = True
                        if self.db.in_transaction:
                            self.db.execute("ROLLBACK")
                    else:
                        self._io_failed()
                elif self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise n.NamespaceError(
                    "namespace-storage-recovery-required"
                    if self.write_started
                    else "namespace-io-failed",
                    self.anchor.namespace_id,
                    self.operation_id,
                    effect=self.effect if self.write_started else "none",
                ) from error
            except BaseException as error:
                if self.connection is not None and self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                    self.failed = True
                if isinstance(error, (journal.CastleError, ValueError, KeyError, TypeError)):
                    # Damage found by a CAS or decode check latches like any
                    # other recovery-required refusal: later admissions on
                    # this handle no longer re-prove the whole store.
                    self.failed = True
                    raise n.NamespaceError(
                        "namespace-storage-recovery-required",
                        self.anchor.namespace_id,
                        self.operation_id,
                        effect=self.effect,
                    ) from error
                raise

    def info(self) -> n.NamespaceInfo:
        with self.admitted():
            self.db.execute("BEGIN")
            result = self.load_info()
            self.db.execute("COMMIT")
            n._authorize(self.session, "info")
            return result

    def execute(
        self, request: n.MutationRequest, content_sources: Mapping[str, bytes]
    ) -> n.MutationResult:
        from castle import namespace_transactions as transactions

        if self.read_only:
            n._authorize(self.session, "admit")
            n._refuse("namespace-access-denied")
        with self.admitted():
            return transactions.execute(self, request, content_sources)

    def read(
        self, request: n.ReadRequest, *, destination: n.SnapshotDestination | None = None
    ) -> n.ReadResult:
        from castle import namespace_transactions as transactions

        with self.mutex:
            try:
                with self.admitted():
                    result = transactions.read(self, request, destination=destination)
                if request.kind == "export-snapshot":
                    from castle import namespace_snapshots as snapshots

                    exported = snapshots.export(self, result.value, destination)
                    result = n.ReadResult(
                        result.namespace_id, result.revision, result.cas_binding, exported
                    )
                    n._authorize(self.session, "read-result")
                return result
            except n.NamespaceError as error:
                if error.code == "namespace-storage-recovery-required":
                    self.failed = True
                if error.namespace_id is None:
                    error.namespace_id = self.anchor.namespace_id
                raise

    def operation(self, operation_id: str) -> n.OperationStatus:
        from castle import namespace_transactions as transactions

        n._authorize(self.session, "status")
        n._uuid(operation_id)
        if self.pid == os.getpid() and not self.closed and self.failed:
            n._authorize(self.session, "status")
            return n.OperationStatus(
                self.anchor.namespace_id,
                operation_id,
                "recovery-required",
                None,
                "namespace-storage-recovery-required",
                "recover",
            )
        with self.admitted():
            return transactions.operation(self, operation_id)

    def operation_target(self, operation_id: str) -> n.OperationTarget:
        from castle import namespace_transactions as transactions

        n._authorize(self.session, "status")
        n._uuid(operation_id)
        if self.pid == os.getpid() and not self.closed and self.failed:
            return n.OperationTarget(*astuple_status(self.operation(operation_id)), None)
        # One admission: the status and its target come from the same row state.
        with self.admitted():
            status = transactions.operation(self, operation_id)
            return transactions.operation_target(self, status)

    def close(self) -> None:
        with self.mutex:
            if self.exporting:
                n._refuse("namespace-coordinator-busy")
            if self.pid != os.getpid():
                self.closed = True
                return
            self._release_journal()
            if self.connection is not None:
                try:
                    self.connection.close()
                finally:
                    self.connection = None
            while self.descriptors:
                descriptor = self.descriptors.pop()
                os.close(descriptor)
            if self.domain_context is not None:
                self.domain_context.__exit__(None, None, None)
                self.domain_context = None
            self.publication_receipts.clear()
            self.pending_captures.clear()
            self.closed = True

    def __enter__(self) -> NamespaceHandle:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
