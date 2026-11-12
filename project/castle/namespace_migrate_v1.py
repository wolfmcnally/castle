"""Isolated, operator-run migration of a v1 namespace database to v2.

Ordinary runtime accepts only the v2 schema and never migrates silently. This
module runs only when an operator calls migrate_namespace_v1 with a backup
path. It validates the exact v1 database, writes a byte-verified backup, and
converts the schema in one transaction that commits only after the complete
v2 validation passes. Generation, epoch, receipts and the replacement marker
are unchanged; only the derived non-entry dependency rows are dropped, since
v2 computes them.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from castle import namespace as n
from castle import namespace_store as store

__all__ = ["NamespaceMigrationResult", "migrate_namespace_v1"]

_V1_TABLES = {
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
_V1_INDEXES = {
    "live_names": (
        "CREATE UNIQUE INDEX live_names ON entries(parent_id,comparison_key) WHERE live=1"
    )
}


@dataclass(frozen=True)
class NamespaceMigrationResult:
    namespace_id: str
    revision: int
    backup_sha256: str
    migrated: bool
    diagnosis: n.NamespaceDiagnosis
    schema: str = field(default="castle.namespace_migration.v1", init=False)


def migrate_namespace_v1(
    namespace_root: Path, cas_root: Path, session: n.AuthorizedSession, backup: Path
) -> NamespaceMigrationResult:
    """Migrate one v1 namespace to v2 under writer ownership, keeping a backup."""
    n._authorize(session, "migrate")
    with store.NamespaceHandle(namespace_root, cas_root, session, migrate_backup=backup) as handle:
        diagnosis = n.NamespaceDiagnosis(**handle._diagnose())
        return NamespaceMigrationResult(
            diagnosis.namespace_id,
            diagnosis.revision,
            handle.migration[0],
            handle.migration[1],
            diagnosis,
        )


def _schema(connection: sqlite3.Connection) -> tuple[dict, dict, int, list]:
    tables = dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    indexes = dict(
        connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        )
    )
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    triggers = connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
    return tables, indexes, version, triggers


def _digest(file: Path) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as source:
        for block in iter(lambda: source.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _backup_matches_v1(backup: Path, namespace_id: str) -> bool:
    """A rerun after commit: the backup must be an exact v1 copy of this namespace."""
    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro&immutable=1", uri=True)) as copy:
        tables, indexes, version, triggers = _schema(copy)
        row = copy.execute("SELECT namespace_id FROM namespace").fetchall()
    return (tables, indexes, version, triggers) == (_V1_TABLES, _V1_INDEXES, 1, []) and row == [
        (namespace_id,)
    ]


def _upgrade_receipts(db: sqlite3.Connection, cas_root: Path) -> None:
    """Rebind 0.3.0 whole-prefix publication receipts to their own journal line.

    0.3.0 receipts carry journal_length and journal_sha256 over the journal
    prefix ending with the publication's line. Each is verified against the
    current journal before it is converted to event_offset, event_length and
    event_sha256 of that last line; a mismatch refuses before commit.
    """
    from castle import cas_recovery as publication
    from castle import journal
    from castle import namespace_content as content

    raw: bytes | None = None
    rows = db.execute(
        "SELECT operation_id, ownership_receipt FROM operations WHERE ownership_receipt IS NOT NULL"
    ).fetchall()
    for operation_id, text in rows:
        value = content.decode_recipe(text)
        publications = value.get("publication") if isinstance(value, dict) else None
        if not isinstance(publications, list):
            continue
        changed = False
        for saved in publications:
            receipt = saved.get("receipt") if isinstance(saved, dict) else None
            if not isinstance(receipt, dict) or "journal_sha256" not in receipt:
                continue
            if raw is None:
                raw = (cas_root / journal.JOURNAL_FILENAME).read_bytes()
            length, digest = receipt.pop("journal_length"), receipt.pop("journal_sha256")
            prefix = raw[:length] if type(length) is int and length > 0 else b""
            tail = publication._tail(prefix) if len(prefix) == length else None
            try:
                event = json.loads(prefix[tail[0] :]) if tail is not None else None
            except ValueError:
                event = None
            if (
                not isinstance(event, dict)
                or hashlib.sha256(prefix).hexdigest() != digest
                or not prefix.endswith(b"\n")
                or [event.get("event_id")] != receipt.get("event_ids")
            ):
                n._refuse("namespace-storage-recovery-required")
            receipt.update(event_offset=tail[0], event_length=tail[1], event_sha256=tail[2])
            changed = True
        if changed:
            db.execute(
                "UPDATE operations SET ownership_receipt=? WHERE operation_id=?",
                (content.encode_recipe(value), operation_id),
            )


def run(handle: store.NamespaceHandle, backup: Path) -> None:
    """Called by the handle under domain ownership, coordinator.lock and the stable CAS lock."""
    from castle import namespace_replacement as replacement

    backup = Path(backup).absolute()
    try:
        # Containment is decided on the resolved parent, so a linked parent
        # (for example a system temporary directory) cannot hide a root.
        backup = backup.parent.resolve(strict=True) / backup.name
    except OSError:
        n._refuse("namespace-root-unsafe")
    if (
        backup.is_relative_to(handle.root)
        or backup.is_relative_to(handle.cas_root)
        or backup.is_symlink()
    ):
        n._refuse("namespace-root-unsafe")
    handle._inventory(deep=True)
    handle._read_anchor()
    handle._cas_check(identity_only=True)
    handle._connect(create=False)  # a writer connection rolls back any hot journal first
    db = handle.db
    if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        n._refuse("namespace-storage-recovery-required")
    tables, indexes, version, triggers = _schema(db)
    info_row = db.execute("SELECT namespace_id FROM namespace").fetchall()
    if len(info_row) != 1:
        n._refuse("namespace-format-unsupported")
    if (tables, indexes, version, triggers) == (store._TABLES, store._INDEXES, 2, []):
        # Rerun after a committed migration: verify the backup, then diagnose only.
        if not backup.is_file() or not _backup_matches_v1(backup, info_row[0][0]):
            n._refuse("namespace-storage-recovery-required")
        handle.migration = (_digest(backup), False)
        handle.connection.close()
        handle.connection = None
        return
    if (tables, indexes, version, triggers) != (_V1_TABLES, _V1_INDEXES, 1, []):
        n._refuse("namespace-format-unsupported")
    if (
        db.execute("SELECT service_state FROM namespace").fetchone() != ("ready",)
        or db.execute("SELECT 1 FROM operations WHERE state IN ('staging','prepared')").fetchone()
    ):
        n._refuse("namespace-storage-recovery-required")
    marker = replacement.read(handle.root)
    if marker is not None and marker["stage"] != "complete":
        n._refuse("namespace-storage-recovery-required")
    source = handle.root / "namespace.sqlite"
    expected = _digest(source)
    if backup.exists():
        # A run killed between the publishing link and the partial's removal
        # leaves the backup's own second name; only that name is dropped.
        partial = backup.with_name(backup.name + ".partial")
        if (
            not partial.is_symlink()
            and partial.is_file()
            and not backup.is_symlink()
            and os.path.samestat(partial.lstat(), backup.lstat())
        ):
            partial.unlink()
            store._sync_dir(backup.parent)
        if not backup.is_file() or backup.lstat().st_nlink != 1 or _digest(backup) != expected:
            n._refuse("namespace-storage-recovery-required")
    else:
        # Copy beside the backup, then publish by link: a visible backup is
        # always complete. A partial copy left by a killed run is never the
        # backup, so it is replaced rather than refused (CODE-F009).
        partial = backup.with_name(backup.name + ".partial")
        if partial.is_symlink() or (partial.exists() and not partial.is_file()):
            n._refuse("namespace-root-unsafe")
        partial.unlink(missing_ok=True)
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as data:
            for block in iter(lambda: data.read(1 << 20), b""):
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if _digest(partial) != expected:
            n._refuse("namespace-storage-recovery-required")
        os.link(partial, backup, follow_symlinks=False)
        partial.unlink()
        store._sync_dir(backup.parent)
        if _digest(backup) != expected or not stat.S_ISREG(backup.lstat().st_mode):
            n._refuse("namespace-storage-recovery-required")
    handle.write_started = True
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("ALTER TABLE entries ADD COLUMN touched_revision INTEGER NOT NULL DEFAULT 0")
        # v1 kept no deletion revision: a tombstone keeps its last changed revision.
        db.execute("UPDATE entries SET touched_revision=changed_revision")
        for name in (
            "tree_totals",
            "coordinator_state",
            "cas_index_position",
            "cas_records",
            "cas_objects",
        ):
            db.execute(store._TABLES[name])
        for name, statement in store._INDEXES.items():
            if name not in _V1_INDEXES:
                db.execute(statement)
        db.execute(f"PRAGMA user_version={store._USER_VERSION}")
        db.execute("INSERT INTO tree_totals VALUES (?,?,?)", store.summarize(handle.entries()))
        db.execute("INSERT INTO coordinator_state VALUES (NULL)")
        db.execute("DELETE FROM dependencies WHERE owner_kind!='entry'")
        _upgrade_receipts(db, handle.cas_root)
        # Validate the complete v2 state before COMMIT (PLAN-F008).
        if _schema(db) != (store._TABLES, store._INDEXES, store._USER_VERSION, []):
            n._refuse("namespace-format-unsupported")
        handle._cas_check()
        handle._validate()
        handle._recover_receipts()
        store._commit(db)
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    handle.write_started = False
    with source.open("rb") as file:
        os.fsync(file.fileno())
    handle.migration = (expected, True)
    handle.connection.close()
    handle.connection = None
