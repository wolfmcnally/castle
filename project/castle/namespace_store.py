"""Local durable namespace authority and its lifetime single coordinator."""

from __future__ import annotations

import ctypes
import fcntl
import hashlib
import os
import sqlite3
import stat
import threading
import uuid
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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
        "CHECK(live IN (0,1)), comparison_key TEXT)"
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
}
_HANDLES: weakref.WeakSet[NamespaceHandle] = weakref.WeakSet()


def _after_fork() -> None:
    for handle in list(_HANDLES):
        for descriptor in handle.descriptors:
            os.close(descriptor)
        handle.descriptors.clear()
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
    ) -> None:
        self.session = session
        n._authorize(
            session, "initialize" if initialize is not None else "recover" if recover else "open"
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
        self.objects = {}
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
            with journal._stable_lock(self.cas_root):
                self._inventory(initializing=initialize is not None)
                if initialize is not None:
                    self._cas_check()
                    self._initialize(initialize)
                else:
                    self._read_anchor()
                    self._cas_check(identity_only=True)
                    self._open()
                    pending = self.db.execute(
                        "SELECT canonical_request FROM operations "
                        "WHERE state IN ('staging','prepared')"
                    ).fetchall()
                    if any(n._decode(raw)["kind"] == "excise" for (raw,) in pending):
                        n._refuse("namespace-storage-recovery-required")
                    self._validate(check_content=False)
                    self._inventory()
                    content_io.before_cas(self)
                    self._cas_check()
                self._validate()
                self._recover_receipts()
                from castle import namespace_transactions as transactions

                prepared = self.db.execute(
                    "SELECT operation_id FROM operations WHERE state IN ('staging','prepared')"
                ).fetchall()
                if len(prepared) > 1:
                    n._refuse("namespace-storage-recovery-required")
                for (operation_id,) in prepared:
                    transactions.finish(self, operation_id)
                    self.recovered.append(operation_id)
                self._validate()
                n._authorize(self.session, "ready")
        except n.NamespaceError:
            self.close()
            raise
        except (OSError, sqlite3.Error, journal.CastleError, ValueError, TypeError, KeyError):
            self.close()
            raise n.NamespaceError(
                "namespace-storage-recovery-required", effect="recovery-required"
            ) from None
        except BaseException:
            self.close()
            raise

    @property
    def db(self) -> sqlite3.Connection:
        if self.connection is None:
            n._refuse("namespace-storage-recovery-required")
        return self.connection

    def _lock(self, file: Path, *, create: bool) -> None:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        descriptor = os.open(file, flags | (os.O_CREAT if create else 0), 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size:
                n._refuse("namespace-root-unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
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

    def _inventory(self, *, initializing: bool = False) -> None:
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
                for directory in file.iterdir():
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
            file.as_uri() + "?mode=rw",
            uri=True,
            isolation_level=None,
            check_same_thread=False,
            timeout=0,
        )
        self.db.execute("PRAGMA foreign_keys=ON")
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
        for statement in _TABLES.values():
            self.db.execute(statement)
        self.db.execute(
            "CREATE UNIQUE INDEX live_names ON entries(parent_id,comparison_key) WHERE live=1"
        )
        self.db.execute("PRAGMA user_version=1")
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

    def _open(self) -> None:
        self._connect(create=False)
        if self.db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            n._refuse("namespace-storage-recovery-required")
        definitions = dict(self.db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        if definitions != _TABLES:
            n._refuse("namespace-format-unsupported")
        if self.db.execute("SELECT sql FROM sqlite_master WHERE name='live_names'").fetchone() != (
            "CREATE UNIQUE INDEX live_names ON entries(parent_id,comparison_key) WHERE live=1",
        ):
            n._refuse("namespace-format-unsupported")
        if self.db.execute("PRAGMA user_version").fetchone() != (1,):
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
        query = "SELECT * FROM entries" + (" WHERE live=1" if live else "")
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

    def write_entry(self, entry: n.EntryRecord, *, live: bool = True) -> None:
        self.db.execute(
            (
                "INSERT INTO entries VALUES (?,?,?,?,?,?,?,?,?,?) ON "
                "CONFLICT(entry_id) DO UPDATE SET "
                "kind=excluded.kind,parent_id=excluded.parent_id,name=excluded.nam"
                "e,metadata=excluded.metadata,cas_id=excluded.cas_id,size=excluded"
                ".size,changed_revision=excluded.changed_revision,live=excluded.li"
                "ve,comparison_key=excluded.comparison_key"
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
    ) -> None:
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
        _tree(info, self.entries())
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
                continue
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
                row["state"] in {"staging", "prepared"}
                and row["prepared_revision"] != info.revision
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

                    saved = n._snapshot(
                        content_io.decode_recipe(row["ownership_receipt"])["snapshot"]
                    )
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

        if set(
            self.db.execute("SELECT * FROM dependencies WHERE owner_kind='operation'")
        ) != transactions.operation_dependencies(self):
            n._refuse("namespace-storage-recovery-required")
        if self.db.execute(
            "SELECT 1 FROM dependencies WHERE owner_kind "
            "NOT IN ('operation','entry','snapshot') LIMIT 1"
        ).fetchone():
            n._refuse("namespace-storage-recovery-required")

        if self.db.execute("PRAGMA foreign_key_check").fetchall():
            n._refuse("namespace-storage-recovery-required")
        from castle import namespace_snapshots as snapshots

        if set(
            self.db.execute("SELECT * FROM dependencies WHERE owner_kind='snapshot'")
        ) != snapshots.dependencies(
            self, check_content=check_content, allow_pending=replacement_pending
        ):
            n._refuse("namespace-storage-recovery-required")
        for (operation,) in self.db.execute("SELECT operation_id FROM replacement_completion"):
            if self.db.execute(
                "SELECT state FROM operations WHERE operation_id=?", (operation,)
            ).fetchone() != ("retired",):
                n._refuse("namespace-replacement-invalid")

    def ownership_info(self, row: dict[str, Any]) -> n.NamespaceInfo:
        from castle import namespace_replacement as replacement

        info = self.load_info()
        intervals = getattr(self, "epoch_intervals", [(0, info.cas, info.database_generation)])
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
            try:
                self._inventory()
                with journal._stable_lock(self.cas_root):
                    if self.session.describe_capabilities() != self.descriptor:
                        n._refuse("namespace-protection-unsupported")
                    self._cas_check(full=False)
                    self._validate()
                    if (
                        self.db.execute("SELECT service_state FROM namespace").fetchone()
                        != ("ready",)
                        or self.db.execute(
                            "SELECT 1 FROM operations WHERE state IN ('staging','prepared')"
                        ).fetchone()
                    ):
                        n._refuse("namespace-storage-recovery-required")
                    yield
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
                ) from None
            except BaseException as error:
                if self.connection is not None and self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                    self.failed = True
                if isinstance(error, (journal.CastleError, ValueError, KeyError, TypeError)):
                    raise n.NamespaceError(
                        "namespace-storage-recovery-required",
                        self.anchor.namespace_id,
                        self.operation_id,
                        effect=self.effect,
                    ) from None
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

    def close(self) -> None:
        with self.mutex:
            if self.exporting:
                n._refuse("namespace-coordinator-busy")
            if self.pid != os.getpid():
                self.closed = True
                return
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
