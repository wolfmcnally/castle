"""Touched-record admission proofs: framing parity, index catch-up and error provenance."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from castle import cas, journal
from castle import namespace as n
from castle import namespace_store as store


def _reference(directory: Path, data: bytes) -> object:
    """0.3.0's strict authority reader on these exact bytes: records, or the refusal type."""
    (directory / journal.JOURNAL_FILENAME).write_bytes(data)
    try:
        return journal._read_authority_journal(directory)
    except journal.JournalError:
        return journal.JournalError


def _framed(data: bytes) -> object:
    try:
        return [span[3] for span in store._frame(data)]
    except journal.JournalError:
        return journal.JournalError


def framing_proof(tmp_path: Path) -> None:
    source = tmp_path / "framing-cas"
    for index in range(3):
        file = tmp_path / f"framing-{index}"
        file.write_bytes(f"framing parity payload {index}\n".encode())
        cas.ingest_file(source, file, "framing-proof")
    original = (source / journal.JOURNAL_FILENAME).read_bytes()
    lines = original.splitlines(keepends=True)
    assert len(lines) == 3 and all(line.endswith(b"\n") for line in lines)
    spaced = b"".join(
        json.dumps(json.loads(line), separators=(" , ", " : ")).encode() + b" \n" for line in lines
    )
    variants = {
        "lf": original,
        "crlf": original.replace(b"\n", b"\r\n"),
        "cr": original.replace(b"\n", b"\r"),
        "vt-ff-fs": lines[0][:-1] + b"\x0b" + lines[1][:-1] + b"\x0c" + lines[2][:-1] + b"\x1c",
        "nel": original.replace(b"\n", "\x85".encode()),
        "u2028": original.replace(b"\n", " ".encode()),
        "u2029-mixed": lines[0][:-1] + " ".encode() + lines[1] + lines[2][:-1] + b"\r\n",
        "whitespace": spaced,
        "unterminated": original[:-1],
        "blank": lines[0] + b"\n" + lines[1] + lines[2],
        "blank-crlf": lines[0][:-1] + b"\r\n\r\n" + lines[1] + lines[2],
        "invalid-utf8": lines[0] + b"\xff\n" + lines[1],
        "bom": b"\xef\xbb\xbf" + original,
        "empty": b"",
    }
    reference_dir = tmp_path / "framing-reference"
    reference_dir.mkdir()
    accepted = set()
    for label, data in variants.items():
        expected = _reference(reference_dir, data)
        assert _framed(data) == expected, label
        if expected is journal.JournalError:
            continue
        accepted.add(label)
        file = tmp_path / f"framing-{label}.jsonl"
        file.write_bytes(data)
        descriptor = store.os.open(file, store.os.O_RDONLY)
        try:
            for offset, length, digest, record in store._frame(data):
                assert store._span(descriptor, len(data), offset, length, digest) == record, label
        finally:
            store.os.close(descriptor)
    # Every separator 0.3.0 accepts stays accepted: no input contraction.
    assert accepted == {
        "lf",
        "crlf",
        "cr",
        "vt-ff-fs",
        "nel",
        "u2028",
        "u2029-mixed",
        "whitespace",
        "unterminated",
        "empty",
    }

    # Start boundary: a span is one line only when the byte before it ends a line.
    spans = store._frame(original)
    damaged = bytearray(original)
    damaged[spans[1][0] - 1] = ord(" ")
    file = tmp_path / "framing-boundary.jsonl"
    file.write_bytes(bytes(damaged))
    descriptor = store.os.open(file, store.os.O_RDONLY)
    try:
        offset, length, digest, _ = spans[2]
        assert store._span(descriptor, len(damaged), offset, length, digest)
        offset, length, _, _ = spans[1]
        with pytest.raises(store._Framing):
            store._span(
                descriptor,
                len(damaged),
                offset,
                length,
                store.hashlib.sha256(bytes(damaged[offset : offset + length])).hexdigest(),
            )
    finally:
        store.os.close(descriptor)
    # A CR ending one span and an LF starting the next are one CRLF terminator.
    crlf = original.replace(b"\n", b"\r\n")
    split = store._frame(crlf)
    file.write_bytes(crlf)
    descriptor = store.os.open(file, store.os.O_RDONLY)
    try:
        offset, length, _, _ = split[0]
        short = crlf[offset : offset + length - 1]
        with pytest.raises(store._Framing):
            store._span(
                descriptor, len(crlf), offset, length - 1, store.hashlib.sha256(short).hexdigest()
            )
    finally:
        store.os.close(descriptor)


def catch_up_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type) -> None:
    case = Case(tmp_path / "incremental-catch-up")
    with case.open() as handle:
        before = handle.db.execute(
            "SELECT journal_length,event_count FROM cas_index_position"
        ).fetchone()
        extra = case.directory / "outside-ingest"
        extra.write_bytes(b"appended by an independent CAS writer\n")
        stored = cas.ingest_file(case.cas_root, extra, "outside-writer")
        calls = []
        revalidate = store.NamespaceHandle._cas_revalidate

        def counted(self):
            calls.append(1)
            return revalidate(self)

        monkeypatch.setattr(store.NamespaceHandle, "_cas_revalidate", counted)
        handle.info()
        after = handle.db.execute(
            "SELECT journal_length,event_count FROM cas_index_position"
        ).fetchone()
        size = (case.cas_root / journal.JOURNAL_FILENAME).stat().st_size
        # An append costs only the appended bytes: no full revalidation.
        assert calls == [] and after == (size, before[1] + 1)
        with handle.admitted():
            assert handle.object(stored.cas_id)[1] == extra.stat().st_size
        assert calls == []
        # A same-length rewrite of the indexed tail triggers one full revalidation.
        journal_file = case.cas_root / journal.JOURNAL_FILENAME
        raw = journal_file.read_bytes()
        journal_file.chmod(0o644)
        journal_file.write_bytes(raw[:-1] + b"\r")
        handle.info()
        assert calls == [1]
        monkeypatch.undo()


def provenance_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type) -> None:
    """Typed public errors keep their codes; the chained cause locates the failing layer."""
    case = Case(tmp_path / "incremental-provenance")
    injected = journal.JournalError("injected journal fault")

    def fail_position(self):
        raise injected

    with monkeypatch.context() as patch:
        patch.setattr(store.NamespaceHandle, "_cas_position", fail_position)
        with pytest.raises(
            n.NamespaceError, match="^namespace-storage-recovery-required$"
        ) as caught:
            case.open()
    assert caught.value.__cause__ is injected
    with case.open() as handle:
        fault = sqlite3.OperationalError("injected admission fault")

        def fail_checks(self, **kwargs):
            raise fault

        with monkeypatch.context() as patch:
            patch.setattr(store.NamespaceHandle, "_admission_checks", fail_checks)
            with pytest.raises(n.NamespaceError, match="^namespace-io-failed$") as caught:
                handle.info()
        assert caught.value.__cause__ is fault
        assert caught.value.code == "namespace-io-failed" and str(caught.value) == caught.value.code


def _totals(handle) -> tuple[int, int, int]:
    return handle.db.execute(
        "SELECT live_entries,entry_bytes,live_files FROM tree_totals"
    ).fetchone()


def _independent(handle) -> tuple[int, int, int]:
    live = handle.entries()
    return (
        len(live),
        sum(
            len(n.canonical_bytes(store.asdict(entry), limit=n.MAX_MANIFEST_BYTES)) - 1
            for entry in live.values()
        ),
        sum(1 for entry in live.values() if entry.kind == "file"),
    )


def totals_proof(tmp_path: Path, Case: type, identity) -> None:
    """tree_totals stays exact across mixed file/directory mutations; damage refuses."""
    case = Case(tmp_path / "incremental-totals")
    content = {"cas_id": case.stored.cas_id, "size": case.source.stat().st_size}
    root = case.initial.root_id
    with case.open() as h:
        assert _totals(h) == _independent(h) and _totals(h)[2] == 0

        def check(expected_files):
            assert _totals(h) == _independent(h)
            assert _totals(h)[2] == expected_files

        folder, inner, first, second = identity(), identity(), identity(), identity()
        case.apply(
            h, "create-directory", dict(entry_id=folder, parent_id=root, name="F", metadata={})
        )
        case.apply(
            h, "create-directory", dict(entry_id=inner, parent_id=folder, name="I", metadata={})
        )
        case.apply(
            h,
            "create-file",
            dict(entry_id=first, parent_id=folder, name="a", metadata={}, content=content),
        )
        case.apply(
            h,
            "create-file",
            dict(entry_id=second, parent_id=inner, name="b", metadata={}, content=content),
        )
        check(2)
        mapping = sorted(
            ({"source_id": old, "new_id": identity()} for old in (folder, inner, first, second)),
            key=lambda pair: pair["source_id"],
        )
        case.apply(
            h, "duplicate", dict(entry_id=folder, parent_id=root, name="Copy", identities=mapping)
        )
        check(4)
        case.apply(h, "replace-content", dict(entry_id=first, content=content))
        case.apply(h, "relocate", dict(entry_id=second, parent_id=root, name="moved"))
        case.apply(h, "set-metadata", dict(entry_id=first, metadata={"k": "v"}))
        check(4)
        imported_dir, imported_file = identity(), identity()
        case.apply(
            h,
            "import-folder",
            dict(
                parent_id=root,
                entries=sorted(
                    [
                        dict(
                            entry_id=imported_dir,
                            kind="directory",
                            parent_id=root,
                            name="Imported",
                            metadata={},
                            cas_id=None,
                            size=None,
                        ),
                        dict(
                            entry_id=imported_file,
                            kind="file",
                            parent_id=imported_dir,
                            name="c",
                            metadata={},
                            cas_id=content["cas_id"],
                            size=content["size"],
                        ),
                    ],
                    key=lambda item: item["entry_id"],
                ),
            ),
        )
        check(5)
        case.apply(h, "delete-entry", dict(entry_id=second))
        check(4)
        copy_root = next(pair["new_id"] for pair in mapping if pair["source_id"] == folder)
        case.apply(h, "delete-subtree", dict(entry_id=copy_root))
        check(2)
        assert h._diagnose()["live_files"] == 2
    import sqlite3 as sql

    for column in ("live_files", "live_entries", "entry_bytes"):
        with sql.connect(case.root / "namespace.sqlite") as db:
            db.execute(f"UPDATE tree_totals SET {column}={column}+1")
        with (
            case.open() as h,
            pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"),
        ):
            h._diagnose()
        # Recovery re-derives the trusted summary from the validated entries.
        n.recover_namespace(case.root, case.cas_root, case.session, None)
        with case.open() as h:
            assert _totals(h) == _independent(h) and _totals(h)[2] == 2
    # Summary cardinality: a missing or repeated single-row summary refuses
    # when a mutation touches it (the marker at every admission), and
    # recovery restores exactly one row of each (CODE-F014).
    for statement in (
        "DELETE FROM tree_totals",
        "INSERT INTO tree_totals SELECT * FROM tree_totals",
        "DELETE FROM coordinator_state",
        "INSERT INTO coordinator_state SELECT * FROM coordinator_state",
    ):
        with sql.connect(case.root / "namespace.sqlite") as db:
            db.execute(statement)
        with pytest.raises(n.NamespaceError) as refused:
            with case.open() as h:
                case.apply(
                    h,
                    "create-directory",
                    dict(entry_id=identity(), parent_id=root, name="later", metadata={}),
                )
        assert refused.value.code == "namespace-storage-recovery-required", statement
        n.recover_namespace(case.root, case.cas_root, case.session, None)
        with closing(sql.connect(case.root / "namespace.sqlite")) as db:
            counts = [
                db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("tree_totals", "coordinator_state")
            ]
        assert counts == [1, 1], statement
        with case.open() as h:
            assert _totals(h) == _independent(h) and _totals(h)[2] == 2


def _read(h, kind, args):
    info = h.info()
    return n.read_namespace(h, n.ReadRequest(info.namespace_id, info.cas, None, kind, args)).value


def api_proof(tmp_path: Path, Case: type, identity) -> None:
    """Probe, paging, change feed, diagnosis and operation targets."""
    case = Case(tmp_path / "incremental-api")
    content = {"cas_id": case.stored.cas_id, "size": case.source.stat().st_size}
    root = case.initial.root_id
    with case.open() as h:
        names = ["b", "A", "c", "d", "E"]
        ids = {}
        targets = {}
        for name in names:
            ids[name] = identity()
            result = case.apply(
                h,
                "create-directory",
                dict(entry_id=ids[name], parent_id=root, name=name, metadata={}),
            )
            targets[result.operation_id] = ids[name]
        file_id = identity()
        made = case.apply(
            h,
            "create-file",
            dict(entry_id=file_id, parent_id=ids["b"], name="f", metadata={}, content=content),
        )
        targets[made.operation_id] = file_id
        probe = _read(h, "probe", {})
        assert (probe.live_entries, probe.live_files, probe.revision) == (7, 1, 6)
        assert probe.journal_length == (case.cas_root / journal.JOURNAL_FILENAME).stat().st_size
        assert probe.event_count == len(journal.read_journal(case.cas_root))

        # Pages concatenate to list-directory, in the same order.
        whole = _read(h, "list-directory", {"entry_id": root})
        pages, after = [], None
        while True:
            page = _read(h, "list-directory-page", {"entry_id": root, "after": after, "limit": 2})
            pages.extend(page.entries)
            after = page.next_after
            if after is None:
                break
        assert pages == whole and len(whole) == 5
        for bad in (
            {"entry_id": root, "after": None, "limit": 0},
            {"entry_id": file_id, "after": None, "limit": 1},
        ):
            with pytest.raises(n.NamespaceError):
                _read(h, "list-directory-page", bad)

        # The change feed includes tombstones, ordered by (touched_revision, entry_id).
        deleted = case.apply(h, "delete-entry", dict(entry_id=ids["c"]))
        targets[deleted.operation_id] = ids["c"]
        mapping = sorted(
            ({"source_id": old, "new_id": identity()} for old in (ids["b"], file_id)),
            key=lambda pair: pair["source_id"],
        )
        duplicated = case.apply(
            h, "duplicate", dict(entry_id=ids["b"], parent_id=root, name="copy", identities=mapping)
        )
        targets[duplicated.operation_id] = next(
            p["new_id"] for p in mapping if p["source_id"] == ids["b"]
        )
        generation = h.info().database_generation
        feed, cursor = [], (0, None)
        while True:
            page = _read(
                h,
                "changes-since",
                {
                    "database_generation": generation,
                    "after_revision": cursor[0],
                    "after_entry_id": cursor[1],
                    "limit": 3,
                },
            )
            feed.extend(page.changes)
            if page.next_revision is None:
                break
            cursor = (page.next_revision, page.next_entry_id)
        keys = [(c.touched_revision, c.entry.entry_id) for c in feed]
        assert keys == sorted(keys)
        every = h.entries(live=False)
        assert {c.entry.entry_id for c in feed} == set(every)  # root included: (0, root) > (0, "")
        assert [c for c in feed if c.entry.entry_id == ids["c"]][0].live is False
        recent = _read(
            h,
            "changes-since",
            {
                "database_generation": generation,
                "after_revision": 7,
                "after_entry_id": None,
                "limit": 100,
            },
        )
        assert {c.entry.entry_id for c in recent.changes} == {ids["c"]} | {
            p["new_id"] for p in mapping
        }
        with pytest.raises(n.NamespaceError, match="namespace-revision-conflict"):
            _read(
                h,
                "changes-since",
                {
                    "database_generation": identity(),
                    "after_revision": 0,
                    "after_entry_id": None,
                    "limit": 1,
                },
            )

        diagnosis = _read(h, "diagnose", {})
        assert (diagnosis.live_entries, diagnosis.live_files, diagnosis.revision) == (8, 2, 8)

        # Targets: the primary entry of each committed kind, None otherwise.
        for operation_id, expected in targets.items():
            target = n.namespace_operation_target(h, operation_id)
            status = n.namespace_operation(h, operation_id)
            assert target.primary_entry_id == expected
            assert (target.state, target.result, target.error_code, target.next_action) == (
                status.state,
                status.result,
                status.error_code,
                status.next_action,
            )
        snapshot = case.apply(h, "snapshot", {"snapshot_id": identity()})
        assert n.namespace_operation_target(h, snapshot.operation_id).primary_entry_id is None
        rejected = case.directory_request(h, "A")
        with pytest.raises(n.NamespaceError, match="namespace-name-conflict"):
            n.execute_namespace(h, rejected, {})
        target = n.namespace_operation_target(h, rejected.operation_id)
        assert (target.state, target.primary_entry_id) == ("rejected", None)
        unknown = n.namespace_operation_target(h, identity())
        assert (unknown.state, unknown.primary_entry_id) == ("unknown", None)


# The v1 schema exactly as released Castle 0.3.0 wrote it: captured from a
# namespace created by the v0.3.0 tag's own code (sqlite_master), not from the
# migration's constants, so a wrong constant fails here (CODE-F017).
RELEASED_V1_TABLES = {
    "dependencies": (
        "CREATE TABLE dependencies (owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, cas"
        "_id TEXT NOT NULL, dependency_kind TEXT NOT NULL, PRIMARY KEY(owner_kind,owner_i"
        "d,cas_id,dependency_kind))"
    ),
    "entries": (
        "CREATE TABLE entries (entry_id TEXT PRIMARY KEY REFERENCES entry_reservations(en"
        "try_id), kind TEXT NOT NULL, parent_id TEXT REFERENCES entries(entry_id) DEFERRA"
        "BLE INITIALLY DEFERRED, name TEXT, metadata TEXT NOT NULL, cas_id TEXT, size INT"
        "EGER, changed_revision INTEGER NOT NULL, live INTEGER NOT NULL CHECK(live IN (0,"
        "1)), comparison_key TEXT)"
    ),
    "entry_reservations": ("CREATE TABLE entry_reservations (entry_id TEXT PRIMARY KEY)"),
    "namespace": (
        "CREATE TABLE namespace (namespace_id TEXT PRIMARY KEY, root_id TEXT NOT NULL, po"
        "licy_version INTEGER NOT NULL, unicode_data_version TEXT NOT NULL, revision INTE"
        "GER NOT NULL, cas_binding TEXT NOT NULL, database_generation TEXT NOT NULL, prot"
        "ection TEXT NOT NULL, service_state TEXT NOT NULL)"
    ),
    "operations": (
        "CREATE TABLE operations (operation_id TEXT PRIMARY KEY, fingerprint TEXT, state "
        "TEXT NOT NULL, prepared_revision INTEGER NOT NULL, terminal_revision INTEGER, co"
        "mpleted_at TEXT, retire_after TEXT, canonical_request TEXT, terminal_result TEXT"
        ", terminal_error TEXT, ownership_receipt TEXT, recovery_action TEXT)"
    ),
    "replacement_completion": (
        "CREATE TABLE replacement_completion (operation_id TEXT PRIMARY KEY, target_gener"
        "ation TEXT NOT NULL, cas_binding TEXT NOT NULL, revision_before INTEGER NOT NULL"
        ", revision_after INTEGER NOT NULL, completed INTEGER NOT NULL, predecessor_root_"
        "commitment TEXT NOT NULL, successor_root_commitment TEXT NOT NULL)"
    ),
    "snapshot_reservations": ("CREATE TABLE snapshot_reservations (snapshot_id TEXT PRIMARY KEY)"),
    "snapshots": (
        "CREATE TABLE snapshots (snapshot_id TEXT PRIMARY KEY, cas_id TEXT NOT NULL, size"
        " INTEGER NOT NULL, captured_revision INTEGER NOT NULL, live INTEGER NOT NULL)"
    ),
}
RELEASED_V1_INDEXES = {
    "live_names": (
        "CREATE UNIQUE INDEX live_names ON entries(parent_id,comparison_key) WHERE live=1"
    ),
}


def _receipts_as_released(db, cas_root: Path, *, tamper: bool = False) -> int:
    """Rewrite publication receipts into 0.3.0's whole-prefix shape; count them.

    Independent of the migration: 0.3.0 bound journal_length and the SHA-256 of
    the journal prefix ending with the publication's own line.
    """
    import hashlib

    from castle import namespace_content as content

    raw = (cas_root / journal.JOURNAL_FILENAME).read_bytes()
    converted = 0
    rows = db.execute(
        "SELECT operation_id, ownership_receipt FROM operations WHERE ownership_receipt IS NOT NULL"
    ).fetchall()
    for operation_id, text in rows:
        value = content.decode_recipe(text)
        if not isinstance(value, dict) or not isinstance(value.get("publication"), list):
            continue
        for saved in value["publication"]:
            receipt = saved["receipt"]
            if receipt is None:
                continue
            end = receipt.pop("event_offset") + receipt.pop("event_length")
            receipt.pop("event_sha256")
            digest = hashlib.sha256(raw[:end]).hexdigest()
            receipt.update(journal_length=end, journal_sha256=("0" * 64 if tamper else digest))
            converted += 1
        db.execute(
            "UPDATE operations SET ownership_receipt=? WHERE operation_id=?",
            (content.encode_recipe(value), operation_id),
        )
    return converted


def _downgrade_to_v1(case, *, tamper: bool = False) -> int:
    """Rewrite a v2 database into the exact 0.3.0 v1 shape, as 0.3.0 stored it."""
    from castle import namespace_migrate_v1 as migration

    with case.open() as h:
        dependencies = sorted(store.dependency_rows(h))
    db = sqlite3.connect(case.root / "namespace.sqlite", isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        for index in ("entries_touched", "operations_state", "cas_records_object"):
            db.execute(f"DROP INDEX {index}")
        for table in store._DERIVED + ("tree_totals", "coordinator_state"):
            db.execute(f"DROP TABLE {table}")
        db.execute("ALTER TABLE entries DROP COLUMN touched_revision")
        db.execute("DELETE FROM dependencies")
        db.executemany("INSERT INTO dependencies VALUES (?,?,?,?)", dependencies)
        db.execute("PRAGMA user_version=1")
        converted = _receipts_as_released(db, case.cas_root, tamper=tamper)
        db.execute("COMMIT")
        assert migration._schema(db) == (RELEASED_V1_TABLES, RELEASED_V1_INDEXES, 1, [])
    finally:
        db.close()
    return converted


def _state(case):
    with case.open() as h:
        tables = {
            table: sorted(h.db.execute(f"SELECT * FROM {table}").fetchall())
            for table in (
                "namespace",
                "entry_reservations",
                "operations",
                "snapshots",
                "snapshot_reservations",
                "replacement_completion",
            )
        }
        entries = h.entries(live=False)
        dependencies = store.dependency_rows(h)
        diagnosis = h._diagnose()
    receipts = {
        p.relative_to(case.root).as_posix(): p.read_bytes()
        for p in (case.root / "operations").rglob("*")
        if p.is_file()
    }
    return tables, entries, dependencies, receipts, diagnosis


def migration_proof(tmp_path: Path, Case: type, identity, child) -> None:
    """Explicit operator migration of v1 data: exact, backed up, validated before commit."""
    from castle import namespace_migrate_v1 as migration

    def populated(label):
        case = Case(tmp_path / label)
        content = {"cas_id": case.stored.cas_id, "size": case.source.stat().st_size}
        root = case.initial.root_id
        with case.open() as h:
            folder, gone, kept = identity(), identity(), identity()
            case.apply(
                h, "create-directory", dict(entry_id=folder, parent_id=root, name="F", metadata={})
            )
            for entry_id, name in ((gone, "gone"), (kept, "kept")):
                case.apply(
                    h,
                    "create-file",
                    dict(
                        entry_id=entry_id,
                        parent_id=folder,
                        name=name,
                        metadata={"m": 1},
                        content=content,
                    ),
                )
            case.apply(h, "snapshot", {"snapshot_id": identity()})
            case.apply(h, "delete-entry", dict(entry_id=gone))
            rejected = case.directory_request(h, "F")
            with pytest.raises(n.NamespaceError, match="namespace-name-conflict"):
                n.execute_namespace(h, rejected, {})
        return case

    assert (migration._V1_TABLES, migration._V1_INDEXES) == (
        RELEASED_V1_TABLES,
        RELEASED_V1_INDEXES,
    )
    case = populated("migration-exact")
    before = _state(case)
    # Committed publications keep 0.3.0 receipts; the migration rebinds each
    # to its own line after verifying the released whole-prefix SHA-256.
    assert _downgrade_to_v1(case) > 0
    source_sha = store.hashlib.sha256((case.root / "namespace.sqlite").read_bytes()).hexdigest()
    with pytest.raises(n.NamespaceError, match="^namespace-format-unsupported$"):
        case.open()  # no silent migration at open
    backup = case.directory / "namespace-v1.backup"
    with pytest.raises(n.NamespaceError, match="^namespace-root-unsafe$"):
        migration.migrate_namespace_v1(case.root, case.cas_root, case.session, case.root / "inside")
    result = migration.migrate_namespace_v1(case.root, case.cas_root, case.session, backup)
    assert result.migrated and result.backup_sha256 == source_sha
    assert store.hashlib.sha256(backup.read_bytes()).hexdigest() == source_sha
    after = _state(case)
    assert after[:4] == before[:4]
    assert {k: v for k, v in after[4].items() if k != "journal_sha256"} == {
        k: v for k, v in before[4].items() if k != "journal_sha256"
    }
    with case.open() as h:
        assert h.db.execute(
            "SELECT COUNT(*) FROM entries WHERE touched_revision!=changed_revision"
        ).fetchone() == (0,)
        assert h.db.execute(
            "SELECT COUNT(*) FROM dependencies WHERE owner_kind!='entry'"
        ).fetchone() == (0,)
        assert _totals(h) == _independent(h)
    rerun = migration.migrate_namespace_v1(case.root, case.cas_root, case.session, backup)
    assert not rerun.migrated and rerun.backup_sha256 == source_sha

    # A released receipt whose whole-prefix SHA-256 does not verify refuses
    # before commit, leaving the v1 database byte-identical.
    case = populated("migration-receipt-mismatch")
    assert _downgrade_to_v1(case, tamper=True) > 0
    image = (case.root / "namespace.sqlite").read_bytes()
    with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
        migration.migrate_namespace_v1(
            case.root, case.cas_root, case.session, case.directory / "mismatch.backup"
        )
    assert (case.root / "namespace.sqlite").read_bytes() == image

    # A different existing backup refuses before any change.
    case = populated("migration-wrong-backup")
    _downgrade_to_v1(case)
    image = (case.root / "namespace.sqlite").read_bytes()
    wrong = case.directory / "wrong.backup"
    wrong.write_bytes(b"not this database")
    with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
        migration.migrate_namespace_v1(case.root, case.cas_root, case.session, wrong)
    assert (case.root / "namespace.sqlite").read_bytes() == image

    # Process death at either side of the migration COMMIT: a rerun completes.
    for boundary in ("before-commit", "after-commit"):
        case = populated("migration-kill-" + boundary)
        before = _state(case)
        _downgrade_to_v1(case)
        backup = case.directory / "namespace-v1.backup"
        result = child(case, backup, boundary)
        assert result.returncode == 62, result.stderr
        assert backup.exists()
        rerun = migration.migrate_namespace_v1(case.root, case.cas_root, case.session, backup)
        assert rerun.migrated == (boundary == "before-commit")
        assert _state(case)[:4] == before[:4]

    # A partial copy left by a run killed during the backup copy is never the
    # backup: the next run replaces it and publishes a complete backup by link.
    case = populated("migration-partial-copy")
    _downgrade_to_v1(case)
    source_sha = store.hashlib.sha256((case.root / "namespace.sqlite").read_bytes()).hexdigest()
    backup = case.directory / "namespace-v1.backup"
    partial = backup.with_name(backup.name + ".partial")
    partial.write_bytes((case.root / "namespace.sqlite").read_bytes()[:100])
    result = migration.migrate_namespace_v1(case.root, case.cas_root, case.session, backup)
    assert result.migrated and result.backup_sha256 == source_sha
    assert store.hashlib.sha256(backup.read_bytes()).hexdigest() == source_sha
    assert not partial.exists() and backup.stat().st_nlink == 1

    # A run killed after publishing by link but before removing the partial
    # name leaves one complete file under both names: the next run drops the
    # partial name and continues (CODE-F023).
    case = populated("migration-killed-after-link")
    _downgrade_to_v1(case)
    source_sha = store.hashlib.sha256((case.root / "namespace.sqlite").read_bytes()).hexdigest()
    backup = case.directory / "namespace-v1.backup"
    partial = backup.with_name(backup.name + ".partial")
    partial.write_bytes((case.root / "namespace.sqlite").read_bytes())
    os.link(partial, backup)
    assert backup.stat().st_nlink == 2
    result = migration.migrate_namespace_v1(case.root, case.cas_root, case.session, backup)
    assert result.migrated and result.backup_sha256 == source_sha
    assert not partial.exists() and backup.stat().st_nlink == 1
    # A second name that is not the partial still refuses.
    case = populated("migration-backup-extra-link")
    _downgrade_to_v1(case)
    backup = case.directory / "namespace-v1.backup"
    backup.write_bytes((case.root / "namespace.sqlite").read_bytes())
    os.link(backup, case.directory / "elsewhere")
    with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
        migration.migrate_namespace_v1(case.root, case.cas_root, case.session, backup)

    # Containment is judged on the resolved parent: a linked directory that
    # leads into a root is refused; one that leads outside both is accepted.
    case = populated("migration-linked-parent")
    _downgrade_to_v1(case)
    image = (case.root / "namespace.sqlite").read_bytes()
    for root in (case.root, case.cas_root):
        linked = case.directory / f"linked-{root.name}"
        linked.symlink_to(root, target_is_directory=True)
        with pytest.raises(n.NamespaceError, match="^namespace-root-unsafe$"):
            migration.migrate_namespace_v1(
                case.root, case.cas_root, case.session, linked / "backup"
            )
        assert not (root / "backup").exists()
    assert (case.root / "namespace.sqlite").read_bytes() == image
    outside = case.directory / "outside"
    outside.mkdir()
    (case.directory / "linked-outside").symlink_to(outside, target_is_directory=True)
    result = migration.migrate_namespace_v1(
        case.root, case.cas_root, case.session, case.directory / "linked-outside" / "backup"
    )
    assert result.migrated and (outside / "backup").is_file()

    # A refused in-transaction validation leaves the v1 database bytes intact.
    case = populated("migration-refused-validation")
    _downgrade_to_v1(case)
    image = (case.root / "namespace.sqlite").read_bytes()
    with pytest.MonkeyPatch.context() as patch:

        def refused(self, **kwargs):
            n._refuse("namespace-format-unsupported")

        patch.setattr(store.NamespaceHandle, "_validate", refused)
        with pytest.raises(n.NamespaceError, match="^namespace-format-unsupported$"):
            migration.migrate_namespace_v1(
                case.root, case.cas_root, case.session, case.directory / "refused.backup"
            )
    assert (case.root / "namespace.sqlite").read_bytes() == image

    # A rerun after a committed migration repairs nothing: damage in a derived
    # summary is refused by its diagnosis and is still there afterwards.
    case = populated("migration-rerun-diagnoses")
    _downgrade_to_v1(case)
    backup = case.directory / "namespace-v1.backup"
    migration.migrate_namespace_v1(case.root, case.cas_root, case.session, backup)
    with sqlite3.connect(case.root / "namespace.sqlite") as db:
        db.execute("UPDATE tree_totals SET live_files=live_files+1")
        damaged = db.execute("SELECT * FROM tree_totals").fetchall()
    with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
        migration.migrate_namespace_v1(case.root, case.cas_root, case.session, backup)
    with sqlite3.connect(case.root / "namespace.sqlite") as db:
        assert db.execute("SELECT * FROM tree_totals").fetchall() == damaged


# Phase 12 finding R1 (cas_records completeness on touch). The per-object
# witness (cas_objects) makes a touched lookup prove its located row set: a
# lost, reassigned or added row disagrees with the witness, triggers one full
# revalidation against the journal, and the lookup then answers from current
# bytes. STILL OPEN: a coherent edit of both a row and its witness inside
# namespace.sqlite is not detectable on touch (no SQL-resident witness can be);
# diagnosis refuses it. This set names that open case; it is not a contract.
OPEN_R1_TOUCH_ACCEPTS = {"coherent-later-missing"}


def completeness_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity
) -> None:
    """Row-set faults on touch: detected and revalidated, except the named coherent edit."""
    import sqlite3 as sql

    case = Case(tmp_path / "incremental-completeness")
    content = {"cas_id": case.stored.cas_id, "size": case.source.stat().st_size}
    file_id = identity()
    with case.open() as h:
        case.apply(
            h,
            "create-file",
            dict(
                entry_id=file_id,
                parent_id=case.initial.root_id,
                name="f",
                metadata={},
                content=content,
            ),
        )
    again = case.directory / "again"
    again.write_bytes(case.source.read_bytes())
    cas.ingest_file(case.cas_root, again, "second-sighting")  # a later record of the same object
    with case.open() as h:
        h.info()  # catch-up locates the appended record
        rows = h.db.execute(
            "SELECT event_id,cas_id,offset,length,digest FROM cas_records WHERE cas_id=? "
            "ORDER BY offset",
            (content["cas_id"],),
        ).fetchall()
        located = sorted(h.db.execute("SELECT * FROM cas_records").fetchall())
    assert len(rows) == 2

    def read(h):
        info = h.info()
        return n.read_namespace(
            h, n.ReadRequest(info.namespace_id, info.cas, None, "get-entry", {"entry_id": file_id})
        )

    calls = []
    revalidate = store.NamespaceHandle._cas_revalidate

    def counted(self):
        calls.append(1)
        return revalidate(self)

    # Undamaged outputs: the public entry read and the touched object's fold
    # row. Every namespace consumer of that row reads only presence and size.
    with case.open() as h:
        baseline = read(h).value
        with h.admitted():
            baseline_row = h.object(content["cas_id"])

    # A failed end-of-admission persist of a rebuilt index never changes the
    # admitted result: the read answers, the overlay is dropped, the stored
    # rows stay as they were and the next admission revalidates again.
    with sql.connect(case.root / "namespace.sqlite") as db:
        db.execute("DELETE FROM cas_records WHERE event_id=?", (rows[1][0],))
    flushes = []
    flush, persist = store.NamespaceHandle._flush_overlay, store.NamespaceHandle._cas_store

    def failing_flush(self):
        flushes.append(self._cas_overlay is not None)
        self._flushing = self._cas_overlay is not None
        try:
            return flush(self)
        finally:
            self._flushing = False

    def failing_store(self, *args, **kwargs):
        if getattr(self, "_flushing", False):
            raise sql.OperationalError("disk I/O error")
        return persist(self, *args, **kwargs)

    calls.clear()
    with monkeypatch.context() as patch:
        patch.setattr(store.NamespaceHandle, "_cas_revalidate", counted)
        patch.setattr(store.NamespaceHandle, "_flush_overlay", failing_flush)
        patch.setattr(store.NamespaceHandle, "_cas_store", failing_store)
        with case.open() as h:
            assert read(h).value == baseline
            assert calls and any(flushes) and h._cas_overlay is None and not h.failed
    with closing(sql.connect(case.root / "namespace.sqlite")) as db:
        assert sorted(db.execute("SELECT * FROM cas_records").fetchall()) != located
    calls.clear()
    with monkeypatch.context() as patch:
        patch.setattr(store.NamespaceHandle, "_cas_revalidate", counted)
        with case.open() as h:
            assert read(h).value == baseline and len(calls) == 1
    with closing(sql.connect(case.root / "namespace.sqlite")) as db:
        assert sorted(db.execute("SELECT * FROM cas_records").fetchall()) == located

    observed = {}
    for fault in (
        "later-missing",
        "reassigned",
        "sole-missing",
        "added",
        "coherent-later-missing",
    ):
        with sql.connect(case.root / "namespace.sqlite") as db:
            if fault == "later-missing":
                db.execute("DELETE FROM cas_records WHERE event_id=?", (rows[1][0],))
            elif fault == "reassigned":
                db.execute(
                    "UPDATE cas_records SET cas_id=? WHERE event_id=?",
                    ("sha256:" + "0" * 64, rows[1][0]),
                )
            elif fault == "sole-missing":
                db.execute("DELETE FROM cas_records WHERE cas_id=?", (content["cas_id"],))
            elif fault == "added":
                db.execute(
                    "INSERT INTO cas_records VALUES (?,?,?,?,?)",
                    (identity(), content["cas_id"], rows[0][2], rows[0][3], rows[0][4]),
                )
            else:
                db.execute("DELETE FROM cas_records WHERE event_id=?", (rows[1][0],))
                db.execute(
                    "UPDATE cas_objects SET records=?,rows_digest=? WHERE cas_id=?",
                    (*store._witness([rows[0]]), content["cas_id"]),
                )
        calls.clear()
        with monkeypatch.context() as patch:
            patch.setattr(store.NamespaceHandle, "_cas_revalidate", counted)
            with case.open() as h:
                # Actual outputs, including for the accepted coherent edit: the
                # public read and the fold row equal the undamaged ones exactly.
                assert read(h).value == baseline, fault
                with h.admitted():
                    assert h.object(content["cas_id"]) == baseline_row, fault
                detected = bool(calls)
            # The rebuild is durable once the handle is gone (F012), read
            # through an independent connection rather than the handle's own.
            with closing(sql.connect(case.root / "namespace.sqlite")) as db:
                repaired = sorted(db.execute("SELECT * FROM cas_records").fetchall()) == located
        observed[fault] = not detected
        if detected:
            # One full revalidation rebuilt the located rows from journal bytes.
            assert repaired and len(calls) == 1, (fault, repaired, len(calls))
            continue
        with (
            case.open() as h,
            pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"),
        ):
            h._diagnose()
        n.recover_namespace(case.root, case.cas_root, case.session, None)
        with case.open() as h:
            assert h._diagnose()["live_files"] == 1
    assert {fault for fault, accepted in observed.items() if accepted} == OPEN_R1_TOUCH_ACCEPTS


# UNRESOLVED R1, duplicate-event part. 0.3.0 refuses any repeated event_id
# because it validates the whole journal. v2 catch-up refuses a repeat whose
# original row is located (primary key), and, through the per-object witness,
# a repeat of the SAME object whose original row was lost. STILL OPEN: a repeat
# carrying ANOTHER object's cas_id after the original row was lost passes
# catch-up; diagnosis refuses it. Named for the code review; not a contract.
OPEN_R1_DUPLICATE_ACCEPTS = {"other-object-lost"}


def duplicate_event_proof(tmp_path: Path, Case: type) -> None:
    import sqlite3 as sql

    case = Case(tmp_path / "incremental-duplicate-event")
    other = case.directory / "other-object"
    other.write_bytes(b"a different object for the duplicate-event proof\n")
    cas.ingest_file(case.cas_root, other, "duplicate-proof")
    with case.open() as h:
        h.info()
    journal_file = case.cas_root / journal.JOURNAL_FILENAME
    records = journal.read_journal(case.cas_root)
    first, second = records[0], records[1]
    assert first["cas_id"] != second["cas_id"]
    observed = {}
    for variant in ("present", "same-object-lost", "other-object-lost"):
        repeated = dict(first if variant != "other-object-lost" else second)
        repeated["event_id"] = first["event_id"]
        repeated["at"] = "2999-01-01T00:00:00Z"
        image = (case.root / "namespace.sqlite").read_bytes()
        if variant != "present":
            with sql.connect(case.root / "namespace.sqlite") as db:
                db.execute("DELETE FROM cas_records WHERE event_id=?", (first["event_id"],))
        original = journal_file.read_bytes()
        journal_file.chmod(0o644)
        journal_file.write_bytes(
            original + (journal.canonical_journal_line(repeated) + "\n").encode()
        )
        with pytest.raises(journal.JournalError):
            journal._read_authority_journal(case.cas_root)  # 0.3.0 refuses every repeat
        accepted = True
        try:
            with case.open() as h:
                h.info()
        except n.NamespaceError as error:
            assert error.code == "namespace-storage-recovery-required"
            accepted = False
        observed[variant] = accepted
        if accepted:
            # The detection window ends at the original object's next touch:
            # its witness no longer matches, so the lookup revalidates the
            # whole journal and refuses the repeat (CODE-F027).
            with case.open() as h, pytest.raises(n.NamespaceError) as touched:
                with h.admitted():
                    h.object(first["cas_id"])
            assert touched.value.code == "namespace-storage-recovery-required"
            with (
                case.open() as h,
                pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"),
            ):
                h._diagnose()
        journal_file.write_bytes(original)
        journal_file.chmod(0o444)
        (case.root / "namespace.sqlite").write_bytes(image)
    assert {variant for variant, accepted in observed.items() if accepted} == (
        OPEN_R1_DUPLICATE_ACCEPTS
    )


def tripwire_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity) -> None:
    """Ordinary paths never reach whole-namespace or whole-CAS work (PLAN-F011)."""
    from castle import namespace_snapshots as snapshots
    from castle import namespace_transactions as transactions

    case = Case(tmp_path / "incremental-tripwire")
    content = {"cas_id": case.stored.cas_id, "size": case.source.stat().st_size}
    root = case.initial.root_id
    folder, file_id = identity(), identity()
    with case.open() as h:
        case.apply(
            h, "create-directory", dict(entry_id=folder, parent_id=root, name="F", metadata={})
        )
        case.apply(
            h,
            "create-file",
            dict(entry_id=file_id, parent_id=folder, name="f", metadata={}, content=content),
        )
        last = case.apply(h, "set-metadata", dict(entry_id=folder, metadata={"x": 1}))
    operations = case.root / "operations"
    tripped: list[str] = []

    def wire(name):
        def tripwire(*args, **kwargs):
            tripped.append(name)
            raise AssertionError(f"whole-set work reached: {name}")

        return tripwire

    iterdir = Path.iterdir

    def guarded_iterdir(self):
        if self == operations:
            tripped.append("operations-iteration")
            raise AssertionError("operations/ iterated on an ordinary path")
        return iterdir(self)

    with monkeypatch.context() as patch:
        patch.setattr(cas, "_authority_paths", wire("cas._authority_paths"))
        patch.setattr(journal, "_read_authority_journal", wire("journal._read_authority_journal"))
        patch.setattr(store.NamespaceHandle, "entries", wire("NamespaceHandle.entries"))
        patch.setattr(store.NamespaceHandle, "_validate", wire("NamespaceHandle._validate"))
        patch.setattr(transactions, "operation_dependencies", wire("operation_dependencies"))
        patch.setattr(snapshots, "dependencies", wire("snapshots.dependencies"))
        patch.setattr(Path, "iterdir", guarded_iterdir)
        with case.open() as h:
            info = h.info()

            def read(kind, args):
                return n.read_namespace(
                    h, n.ReadRequest(info.namespace_id, info.cas, None, kind, args)
                )

            assert read("get-entry", {"entry_id": file_id}).value.entry_id == file_id
            assert [e.entry_id for e in read("list-directory", {"entry_id": folder}).value] == [
                file_id
            ]
            assert read("resolve", {"components": ["F", "f"]}).value.entry_id == file_id
            assert read("probe", {}).value.live_files == 1
            assert read(
                "list-directory-page", {"entry_id": root, "after": None, "limit": 5}
            ).value.entries
            assert n.namespace_operation(h, last.operation_id).state == "committed"
            assert n.namespace_operation_target(h, last.operation_id).primary_entry_id == folder
            changed = case.apply(h, "set-metadata", dict(entry_id=file_id, metadata={"y": 2}))
            assert changed.affected_entry_ids == [file_id]
            renamed = case.apply(h, "relocate", dict(entry_id=file_id, parent_id=root, name="g"))
            assert renamed.revision_after == changed.revision_after + 1
            made = case.apply(
                h,
                "create-directory",
                dict(entry_id=identity(), parent_id=root, name="N", metadata={}),
            )
            assert made.revision_after == renamed.revision_after + 1
        with n.open_namespace(case.root, case.cas_root, case.session) as again:
            assert again.info().revision == made.revision_after
    assert tripped == []
    # The wires are live: a whole-sequence diagnosis trips them.
    with case.open() as h, monkeypatch.context() as patch:
        patch.setattr(store.NamespaceHandle, "_validate", wire("NamespaceHandle._validate"))
        with pytest.raises(AssertionError, match="whole-set work reached"):
            h._diagnose()
    assert tripped == ["NamespaceHandle._validate"]


def differential_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity
) -> None:
    """Run the differential with its lowered entry limit scoped to this proof only."""
    with monkeypatch.context() as patch:
        _differential(tmp_path, patch, Case, identity)


def _differential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity) -> None:
    """Fixed-seed: the touched tree check agrees with store._tree on the whole next state."""
    import random

    from castle import namespace_transactions as transactions

    compared = []
    original = transactions._TreeView.check

    def checked(self, info):
        try:
            original(self, info)
            touched = None
        except n.NamespaceError as error:
            touched = error.code
        whole = {
            key: value
            for key, value in {**self.handle.entries(), **self.overlay}.items()
            if value is not None
        }
        try:
            store._tree(info, whole)
            reference = None
        except n.NamespaceError as error:
            reference = error.code
        compared.append((touched, reference))
        assert touched == reference, (touched, reference)
        if touched is not None:
            n._refuse(touched)

    monkeypatch.setattr(transactions._TreeView, "check", checked)
    monkeypatch.setattr(n, "MAX_ENTRIES", 14)
    case = Case(tmp_path / "incremental-differential")
    content = {"cas_id": case.stored.cas_id, "size": case.source.stat().st_size}
    root = case.initial.root_id
    rng = random.Random(20261111)
    # Entry ids order import entries: random uuid4 ids made this fixed-seed
    # sequence's refusal classes vary between runs (about 1 in 40 at 2a26514).
    id_source = random.Random(20261112)

    def identity() -> str:
        import uuid

        return str(uuid.UUID(int=id_source.getrandbits(128), version=4))

    names = ["a", "A", "b", "B", "c", "é", "é"]
    outcomes: dict[str | None, int] = {}
    with case.open() as h:
        for _ in range(160):
            live = h.entries()
            ids = sorted(live)
            directories = [key for key in ids if live[key].kind == "directory"]
            others = [key for key in ids if key != root]
            choice = rng.choice(
                ["dir", "file", "relocate", "duplicate", "delete-subtree", "delete", "import"]
            )
            if choice == "dir":
                kind, args = (
                    "create-directory",
                    dict(
                        entry_id=identity(),
                        parent_id=rng.choice(directories),
                        name=rng.choice(names),
                        metadata={},
                    ),
                )
            elif choice == "file":
                kind, args = (
                    "create-file",
                    dict(
                        entry_id=identity(),
                        parent_id=rng.choice(directories),
                        name=rng.choice(names),
                        metadata={},
                        content=content,
                    ),
                )
            elif choice == "relocate" and others:
                kind, args = (
                    "relocate",
                    dict(
                        entry_id=rng.choice(others),
                        parent_id=rng.choice(directories),
                        name=rng.choice(names),
                    ),
                )
            elif choice == "duplicate" and others:
                source = rng.choice(others)
                members = sorted(
                    key
                    for key in ids
                    if key == source
                    or (lambda k: any(p == source for p in _ancestors(live, k)))(key)
                )
                kind, args = (
                    "duplicate",
                    dict(
                        entry_id=source,
                        parent_id=rng.choice(directories),
                        name=rng.choice(names),
                        identities=[{"source_id": key, "new_id": identity()} for key in members],
                    ),
                )
            elif choice in {"delete-subtree", "delete"} and others:
                target = rng.choice(others)
                kind, args = (
                    (
                        "delete-subtree"
                        if live[target].kind == "directory" and choice == "delete-subtree"
                        else "delete-entry"
                    ),
                    dict(entry_id=target),
                )
            else:
                top, child = identity(), identity()
                parent = rng.choice(directories)
                entries = [
                    dict(
                        entry_id=top,
                        kind="directory",
                        parent_id=parent,
                        name=rng.choice(names),
                        metadata={},
                        cas_id=None,
                        size=None,
                    ),
                    dict(
                        entry_id=child,
                        kind="file",
                        parent_id=top,
                        name=rng.choice(names),
                        metadata={},
                        cas_id=content["cas_id"],
                        size=content["size"],
                    ),
                ]
                kind, args = (
                    "import-folder",
                    dict(parent_id=parent, entries=sorted(entries, key=lambda e: e["entry_id"])),
                )
            try:
                case.apply(h, kind, args)
                code = None
            except n.NamespaceError as error:
                code = error.code
                assert error.code != "namespace-storage-recovery-required", (kind, args)
            outcomes[code] = outcomes.get(code, 0) + 1
            assert _totals(h) == _independent(h)
        assert h._diagnose()["revision"] == h.info().revision
    # The sequence exercised commits and each tree-level refusal class.
    assert outcomes.get(None, 0) > 10
    assert {"namespace-name-conflict", "namespace-cycle", "namespace-limit-exceeded"} <= set(
        outcomes
    )
    assert any(t is not None for t, _ in compared) and any(t is None for t, _ in compared)


def _ancestors(live, key):
    seen = []
    current = live[key].parent_id
    while current is not None and current not in seen:
        seen.append(current)
        current = live[current].parent_id
    return seen


def _damage_case(directory: Path, Case: type, identity):
    """Good/g.txt and Bad/b.txt with distinct objects; b's journal line is not the tail."""
    case = Case(directory)
    root = case.initial.root_id
    bad_source = case.directory / "bad-source"
    bad_source.write_bytes(b"bytes of the record that will be damaged\n")
    bad = cas.ingest_file(case.cas_root, bad_source, "damage-proof")
    tail = case.directory / "tail-source"
    tail.write_bytes(b"a later journal line, so the damaged line is not the tail\n")
    cas.ingest_file(case.cas_root, tail, "damage-proof")
    ids = {name: identity() for name in ("Good", "g", "Bad", "b")}
    with case.open() as h:
        for name in ("Good", "Bad"):
            case.apply(
                h,
                "create-directory",
                dict(entry_id=ids[name], parent_id=root, name=name, metadata={}),
            )
        for name, parent, stored, source in (
            ("g", "Good", case.stored, case.source),
            ("b", "Bad", bad, bad_source),
        ):
            case.apply(
                h,
                "create-file",
                dict(
                    entry_id=ids[name],
                    parent_id=ids[parent],
                    name=f"{name}.txt",
                    metadata={},
                    content={"cas_id": stored.cas_id, "size": source.stat().st_size},
                ),
            )
    return case, ids, bad.cas_id


def _resolve(h, *components):
    return _read(h, "resolve", {"components": list(components)})


def damage_proof(tmp_path: Path, Case: type, identity) -> None:
    """Touched damage fails closed at access; untouched damage does not block other reads.

    For each fault an unrelated read succeeds, the damaged record's read refuses
    with the code the whole-tree validation gives for the same damage, and a
    recovery-required refusal latches the handle (CODE-F002, CODE-F005).
    """

    def sql(case, statement, *values):
        with sqlite3.connect(case.root / "namespace.sqlite") as db:
            db.execute(statement, values)

    def object_file(case, cas_id):
        return cas.object_path(case.cas_root, cas_id)

    def flip_journal_line(case, cas_id):
        file = case.cas_root / journal.JOURNAL_FILENAME
        data = file.read_bytes()
        lines = data.splitlines(keepends=True)
        index = next(i for i, line in enumerate(lines) if cas_id.encode() in line)
        assert index < len(lines) - 1, "the damaged line must not be the tail"
        # Same length, invalid identity: the parse of the located line fails.
        lines[index] = lines[index].replace(cas_id.encode(), b"g" + cas_id[1:].encode(), 1)
        file.chmod(0o644)
        file.write_bytes(b"".join(lines))
        assert file.stat().st_size == len(data)

    def grow(case, cas_id):
        file = object_file(case, cas_id)
        mode = file.stat().st_mode
        file.chmod(0o644)
        with file.open("ab") as output:
            output.write(b"x")
        file.chmod(mode & 0o777)

    faults = {
        "entry-metadata": (
            lambda c, ids, o: sql(c, "UPDATE entries SET metadata='[' WHERE entry_id=?", ids["b"]),
            "namespace-format-unsupported",
        ),
        "entry-reservation": (
            lambda c, ids, o: sql(c, "DELETE FROM entry_reservations WHERE entry_id=?", ids["b"]),
            "namespace-format-unsupported",
        ),
        "ancestor-metadata": (
            lambda c, ids, o: sql(
                c, "UPDATE entries SET metadata='[' WHERE entry_id=?", ids["Bad"]
            ),
            "namespace-format-unsupported",
        ),
        "entry-touched-revision": (
            lambda c, ids, o: sql(
                c,
                "UPDATE entries SET touched_revision=touched_revision+1 WHERE entry_id=?",
                ids["b"],
            ),
            "namespace-storage-recovery-required",
        ),
        "entry-dependency": (
            lambda c, ids, o: sql(c, "DELETE FROM dependencies WHERE owner_id=?", ids["b"]),
            "namespace-storage-recovery-required",
        ),
        "ancestor-touched-revision": (
            lambda c, ids, o: sql(
                c,
                "UPDATE entries SET touched_revision=touched_revision+1 WHERE entry_id=?",
                ids["Bad"],
            ),
            "namespace-storage-recovery-required",
        ),
        "journal-line": (
            lambda c, ids, o: flip_journal_line(c, o),
            "namespace-storage-recovery-required",
        ),
        "object-mode": (
            lambda c, ids, o: object_file(c, o).chmod(0o644),
            "namespace-storage-recovery-required",
        ),
        "object-size": (lambda c, ids, o: grow(c, o), "namespace-storage-recovery-required"),
        "object-missing": (
            lambda c, ids, o: object_file(c, o).unlink(),
            "namespace-storage-recovery-required",
        ),
    }
    for label, (inject, code) in faults.items():
        case, ids, damaged = _damage_case(tmp_path / f"damage-{label}", Case, identity)
        inject(case, ids, damaged)
        with case.open() as h:
            if label != "journal-line":
                # The journal is whole-sequence state: a damaged line anywhere
                # makes the full revalidation of any lookup miss refuse.
                assert _resolve(h, "Good", "g.txt").entry_id == ids["g"], label
            with pytest.raises(n.NamespaceError, match=f"^{code}$"):
                _resolve(h, "Bad", "b.txt")
            if code == "namespace-storage-recovery-required":
                # The refusal latched the handle: nothing more is admitted.
                with pytest.raises(n.NamespaceError, match=f"^{code}$"):
                    _resolve(h, "Good", "g.txt")
            else:
                assert _resolve(h, "Good", "g.txt").entry_id == ids["g"], label
                with pytest.raises(n.NamespaceError, match=f"^{code}$"):
                    _resolve(h, "Bad", "b.txt")
        if code == "namespace-format-unsupported":
            # A mutation resting on a damaged touched row (the entry, an
            # ancestor or a colliding sibling) is damage, never a durable
            # domain rejection (CODE-F018).
            for kind, args in (
                ("set-metadata", {"entry_id": ids["b"], "metadata": {"k": "v"}}),
                (
                    "create-directory",
                    dict(entry_id=identity(), parent_id=ids["Bad"], name="b.txt", metadata={}),
                ),
                (
                    "create-directory",
                    dict(entry_id=identity(), parent_id=ids["Bad"], name="new", metadata={}),
                ),
            )[: 3 if label.startswith("ancestor") else 2]:
                with case.open() as h:
                    request = case.request(h, kind, args)
                    with pytest.raises(n.NamespaceError) as refused:
                        n.execute_namespace(h, request, {})
                with closing(sqlite3.connect(case.root / "namespace.sqlite")) as db:
                    rows = db.execute(
                        "SELECT state FROM operations WHERE operation_id=?",
                        (request.operation_id,),
                    ).fetchall()
                outcome = (refused.value.code, rows)
                assert outcome == ("namespace-storage-recovery-required", []), (
                    label,
                    kind,
                    outcome,
                )
        # Explicit diagnosis finds the same damage without touching it first,
        # reports it as damage whatever the specific cause, and latches.
        with case.open() as h:
            for attempt in (h._diagnose, lambda: _resolve(h, "Good", "g.txt")):
                with pytest.raises(n.NamespaceError) as refused:
                    attempt()
                assert refused.value.code == "namespace-storage-recovery-required", (
                    label,
                    refused.value.code,
                )

    # A replay touches its stored operation row: a tampered fingerprint or a
    # forged retired state is damage, never a conflict or a retirement
    # (CODE-F006).
    forged = (
        "UPDATE operations SET fingerprint=NULL,state='retired',prepared_revision=0,"
        "terminal_revision=NULL,completed_at=NULL,retire_after=NULL,"
        "canonical_request=NULL,terminal_result=NULL,terminal_error=NULL,"
        "ownership_receipt=NULL,recovery_action=NULL WHERE operation_id=?"
    )
    for label, statement in (
        ("fingerprint", "UPDATE operations SET fingerprint=? WHERE operation_id=?"),
        ("retired-partial", "UPDATE operations SET state='retired' WHERE operation_id=?"),
        ("retired-forged", forged),
    ):
        case = Case(tmp_path / f"damage-replay-{label}")
        with case.open() as h:
            request = case.request(
                h,
                "create-directory",
                dict(entry_id=identity(), parent_id=case.initial.root_id, name="D", metadata={}),
            )
            n.execute_namespace(h, request, {})
            # A later operation owns the open-time marker, so only the replay
            # touches the damaged row.
            case.apply(
                h,
                "create-directory",
                dict(entry_id=identity(), parent_id=case.initial.root_id, name="E", metadata={}),
            )
        values = ("0" * 64,) if label == "fingerprint" else ()
        sql(case, statement, *values, request.operation_id)
        with case.open() as h:
            assert h.info().revision == 2, label  # ordinary open does not touch it
            with pytest.raises(n.NamespaceError) as refused:
                n.execute_namespace(h, request, {})
        assert refused.value.code in {
            "namespace-storage-recovery-required",
            "namespace-format-unsupported",
        }, (label, refused.value.code)

    # A name conflict rests on its stored sibling: a sibling whose
    # comparison_key no longer matches its name is damage, and the colliding
    # create leaves no durable rejection (CODE-F013).
    case, ids, _ = _damage_case(tmp_path / "damage-sibling-key", Case, identity)
    sql(case, "UPDATE entries SET comparison_key=? WHERE entry_id=?", n._key("c.txt"), ids["b"])
    with case.open() as h:
        request = case.request(
            h,
            "create-directory",
            dict(entry_id=identity(), parent_id=ids["Bad"], name="c.txt", metadata={}),
        )
        with pytest.raises(n.NamespaceError) as refused:
            n.execute_namespace(h, request, {})
    with closing(sqlite3.connect(case.root / "namespace.sqlite")) as db:
        rows = db.execute(
            "SELECT state FROM operations WHERE operation_id=?", (request.operation_id,)
        ).fetchall()
    assert (refused.value.code, rows) == ("namespace-storage-recovery-required", [])

    # A malformed database page under a touched row is damage, not transient
    # I/O: ordinary open no longer runs integrity_check (CODE-F021).
    case = Case(tmp_path / "damage-page")
    target = identity()
    with case.open() as h:
        for index in range(300):
            case.apply(
                h,
                "create-directory",
                dict(
                    entry_id=target if index == 150 else identity(),
                    parent_id=case.initial.root_id,
                    name=f"d{index:04}" + "x" * 100,
                    metadata={},
                ),
            )
    database = case.root / "namespace.sqlite"
    with closing(sqlite3.connect(database)) as db:
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        (root_page,) = db.execute(
            "SELECT rootpage FROM sqlite_master WHERE type='table' AND name='entries'"
        ).fetchone()
    data = bytearray(database.read_bytes())

    def leaves(number):
        # Walk the entries table b-tree (rowid order is insertion order), so
        # only its own leaf holding the target is damaged, never a page that
        # ordinary open reads.
        start = (number - 1) * page_size
        header = start + (100 if number == 1 else 0)
        if data[header] == 0x0D:
            return [number]
        assert data[header] == 0x05
        cells = int.from_bytes(data[header + 3 : header + 5], "big")
        children = [
            int.from_bytes(data[start + pointer : start + pointer + 4], "big")
            for pointer in (
                int.from_bytes(data[header + 12 + 2 * i : header + 14 + 2 * i], "big")
                for i in range(cells)
            )
        ] + [int.from_bytes(data[header + 8 : header + 12], "big")]
        return [leaf for child in children for leaf in leaves(child)]

    pages = [
        leaf
        for leaf in leaves(root_page)
        if target.encode() in data[(leaf - 1) * page_size : leaf * page_size]
    ]
    assert len(pages) == 1 and pages[0] != 1
    start = (pages[0] - 1) * page_size
    data[start : start + page_size] = b"\xff" * page_size
    database.write_bytes(bytes(data))
    with case.open() as h:
        for attempt in (
            lambda: _read(h, "get-entry", {"entry_id": target}),
            lambda: _read(h, "get-entry", {"entry_id": case.initial.root_id}),
        ):
            with pytest.raises(n.NamespaceError) as refused:
                attempt()
            assert refused.value.code == "namespace-storage-recovery-required"
    with case.open() as h, pytest.raises(n.NamespaceError) as refused:
        h._diagnose()
    assert refused.value.code == "namespace-storage-recovery-required"


def settled_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity) -> None:
    """The unsettled-operation marker carries what 0.3.0 proved by scanning every receipt.

    Each unsettled state refuses ordinary open and clears under explicit
    recovery (CODE-F004, PLAN-F001).
    """
    import runpy

    from castle import namespace_excision as cutover

    excision = runpy.run_path(str(Path(__file__).with_name("test_namespace_excision.py")))
    recovery_code = "^namespace-storage-recovery-required$"

    # Kill point: after the excision abort commit, before its receipt is durable.
    case, request, roots, recovery = excision["_small_case"](tmp_path, Case, "settled-abort")
    with case.open() as handle, monkeypatch.context() as patch:

        def before_cas(*args):
            raise OSError("injected before CAS cutover")

        patch.setattr(cutover, "_cas_transition", before_cas)
        with pytest.raises(n.NamespaceError, match=recovery_code):
            n.execute_namespace(handle, request, {}, roots)
    recovery["action"] = "abort-before-retirement"
    receipt_state = store.NamespaceHandle._receipt_state

    def killed_before_durable(self, operation, state):
        if operation == request.operation_id and state == "durable":
            raise OSError("process death after the abort commit")
        return receipt_state(self, operation, state)

    with monkeypatch.context() as patch:
        patch.setattr(store.NamespaceHandle, "_receipt_state", killed_before_durable)
        with pytest.raises(n.NamespaceError):
            n.recover_namespace(case.root, case.cas_root, case.session, recovery)
    with sqlite3.connect(case.root / "namespace.sqlite") as db:
        assert db.execute("SELECT unsettled_operation FROM coordinator_state").fetchall() == [
            (request.operation_id,)
        ]
    with pytest.raises(n.NamespaceError, match=recovery_code):
        case.open()
    n.recover_namespace(case.root, case.cas_root, case.session, None)
    with case.open() as handle:
        assert n.namespace_operation(handle, request.operation_id).state == "rejected"
        assert handle.db.execute(
            "SELECT unsettled_operation FROM coordinator_state"
        ).fetchall() == [(None,)]

    # Excise on a live handle whose last operation became unsettled refuses
    # before any intent is written (the execute precheck).
    case, request, roots, _ = excision["_small_case"](tmp_path, Case, "settled-excise")
    with case.open() as handle:
        last = case.apply(
            handle,
            "create-directory",
            dict(entry_id=identity(), parent_id=case.initial.root_id, name="Late", metadata={}),
        )
        (case.root / "operations" / last.operation_id / "receipt.tmp").write_bytes(b"residue")
        with sqlite3.connect(case.root / "namespace.sqlite") as db:
            db.execute("UPDATE coordinator_state SET unsettled_operation=?", (last.operation_id,))
        excise = case.request(handle, "excise", request.args)
        with pytest.raises(n.NamespaceError, match=recovery_code):
            n.execute_namespace(handle, excise, {}, roots)
    assert not roots["successor_root"].exists()
    assert not (case.root / "operations" / excise.operation_id).exists()

    # Direct _settled cases, each against a fresh store at ordinary open.
    def unknown_row(case, operation):
        return identity()

    def receipt_temporary(case, operation):
        (case.root / "operations" / operation / "receipt.tmp").write_bytes(b"residue")
        return operation

    def receipt_prepared(case, operation):
        with case.open() as handle:
            handle._receipt_state(operation, "prepared")
        return operation

    def retired_row(case, operation):
        with sqlite3.connect(case.root / "namespace.sqlite") as db:
            db.execute(
                "UPDATE operations SET fingerprint=NULL,state='retired',prepared_revision=0,"
                "terminal_revision=NULL,completed_at=NULL,retire_after=NULL,"
                "canonical_request=NULL,terminal_result=NULL,terminal_error=NULL,"
                "ownership_receipt=NULL,recovery_action=NULL WHERE operation_id=?",
                (operation,),
            )
        return operation

    outcomes = {}
    for label, fault in (
        ("missing-row", unknown_row),
        ("receipt-temporary", receipt_temporary),
        ("receipt-not-durable", receipt_prepared),
        ("retired-row", retired_row),
    ):
        case = Case(tmp_path / f"settled-{label}")
        with case.open() as handle:
            operation = case.apply(
                handle,
                "create-directory",
                dict(entry_id=identity(), parent_id=case.initial.root_id, name="D", metadata={}),
            ).operation_id
        marked = fault(case, operation)
        with sqlite3.connect(case.root / "namespace.sqlite") as db:
            db.execute("UPDATE coordinator_state SET unsettled_operation=?", (marked,))
        with pytest.raises(n.NamespaceError, match=recovery_code):
            case.open()
        try:
            n.recover_namespace(case.root, case.cas_root, case.session, None)
        except n.NamespaceError as error:
            outcomes[label] = error.code
            continue
        with case.open() as handle:
            assert handle.db.execute(
                "SELECT unsettled_operation FROM coordinator_state"
            ).fetchall() == [(None,)], label
        outcomes[label] = "cleared"
    # Two states stay refused under recovery, by the receipt scan unchanged
    # from 0.3.0: a surviving receipt.tmp (never inferred durable), and a
    # retired row whose directory survives with no expiry owning its disposal.
    assert outcomes == {
        "missing-row": "cleared",
        "receipt-temporary": "namespace-storage-recovery-required",
        "receipt-not-durable": "cleared",
        "retired-row": "namespace-storage-recovery-required",
    }, outcomes


def cost_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity) -> None:
    """Ordinary paths cost the same at two namespace sizes (CODE-F003).

    Counts SQL statements (sqlite3 trace callback), entry validations and
    journal record parses for each ordinary operation at N=100 and N=2000
    entries, including set-metadata and relocate of the directory holding
    almost all of them. Any O(N) step makes the two measurements differ.
    """
    from castle import namespace_store as store_module
    from castle import namespace_transactions as transactions

    # Request-sized work (CODE-F020): name keys computed and overlay entries
    # visited while one request checks its own k entries must grow linearly.
    request_work = {"keys": 0, "visits": 0}

    class Visited(dict):
        def items(self):
            for item in super().items():
                request_work["visits"] += 1
                yield item

        def __iter__(self):
            for key in super().__iter__():
                request_work["visits"] += 1
                yield key

    def request_sized(action) -> tuple[int, int]:
        key, view_init = n._key, transactions._TreeView.__init__

        def counted_key(name):
            request_work["keys"] += 1
            return key(name)

        def visited_init(self, *args):
            view_init(self, *args)
            self.overlay = Visited()

        for name in request_work:
            request_work[name] = 0
        with monkeypatch.context() as patch:
            patch.setattr(n, "_key", counted_key)
            patch.setattr(transactions._TreeView, "__init__", visited_init)
            action()
        return request_work["keys"], request_work["visits"]

    def measure(size: int) -> tuple[dict[str, tuple[int, int, int]], dict[str, tuple[int, int]]]:
        case = Case(tmp_path / f"cost-{size}")
        root = case.initial.root_id
        content = {"cas_id": case.stored.cas_id, "size": case.source.stat().st_size}
        big, small, target, leaf = identity(), identity(), identity(), identity()
        with case.open() as h:
            for entry_id, name in ((big, "Big"), (small, "Small"), (target, "Target")):
                case.apply(
                    h,
                    "create-directory",
                    dict(entry_id=entry_id, parent_id=root, name=name, metadata={}),
                )
            case.apply(
                h,
                "create-file",
                dict(entry_id=leaf, parent_id=small, name="leaf", metadata={}, content=content),
            )
            members = [
                dict(
                    entry_id=identity(),
                    kind="file" if index % 2 else "directory",
                    parent_id=big,
                    name=f"member-{index:05}",
                    metadata={},
                    cas_id=content["cas_id"] if index % 2 else None,
                    size=content["size"] if index % 2 else None,
                )
                for index in range(size)
            ]
            sized = {
                "import-folder": request_sized(
                    lambda: case.apply(
                        h,
                        "import-folder",
                        dict(
                            parent_id=big,
                            entries=sorted(members, key=lambda item: item["entry_id"]),
                        ),
                    )
                )
            }
        counts = {"statements": 0, "entries": 0, "parses": 0, "vm": 0}
        connect = store_module.NamespaceHandle._connect
        validate_entry = store_module._validate_entry
        parse = journal.parse_journal_record

        def traced(self, *, create):
            connect(self, create=create)
            self.db.set_trace_callback(
                lambda _: counts.__setitem__("statements", counts["statements"] + 1)
            )
            # SQLite virtual-machine steps, in units of 100: a seek stays
            # nearly flat with N where a scan grows with it (CODE-F024).
            self.db.set_progress_handler(
                lambda: counts.__setitem__("vm", counts["vm"] + 1) or 0, 100
            )

        def counted_entry(*args):
            counts["entries"] += 1
            return validate_entry(*args)

        def counted_parse(*args, **kwargs):
            counts["parses"] += 1
            return parse(*args, **kwargs)

        result = {}
        with monkeypatch.context() as patch:
            patch.setattr(store_module.NamespaceHandle, "_connect", traced)
            patch.setattr(store_module, "_validate_entry", counted_entry)
            patch.setattr(journal, "parse_journal_record", counted_parse)

            def step(label, action):
                for key in counts:
                    counts[key] = 0
                action()
                result[label] = (counts["statements"], counts["entries"], counts["parses"])
                steps[label] = counts["vm"]

            handle = None
            steps = {}

            def open_handle():
                nonlocal handle
                handle = case.open()

            step("open", open_handle)
            try:
                step("resolve", lambda: _resolve(handle, "Small", "leaf"))
                step(
                    "list-small",
                    lambda: _read(
                        handle,
                        "list-directory-page",
                        {"entry_id": small, "after": None, "limit": 10},
                    ),
                )
                step(
                    "list-big-deep",
                    lambda: _read(
                        handle,
                        "list-directory-page",
                        {"entry_id": big, "after": f"member-{size // 2:05}", "limit": 10},
                    ),
                )
                step(
                    "set-metadata-big",
                    lambda: case.apply(
                        handle, "set-metadata", dict(entry_id=big, metadata={"k": 1})
                    ),
                )
                step(
                    "relocate-big",
                    lambda: case.apply(
                        handle, "relocate", dict(entry_id=big, parent_id=target, name="Big")
                    ),
                )
                step(
                    "delete-entry-refused",
                    lambda: pytest.raises(
                        n.NamespaceError, case.apply, handle, "delete-entry", dict(entry_id=big)
                    ),
                )
            finally:
                handle.close()
        with case.open() as h:
            sized["delete-subtree"] = request_sized(
                lambda: case.apply(h, "delete-subtree", dict(entry_id=big))
            )
        return result, {**sized, **{f"vm:{key}": (value,) for key, value in steps.items()}}

    (smaller, small_sized), (larger, large_sized) = measure(100), measure(2000)
    assert smaller == larger, {"N=100": smaller, "N=2000": larger}
    for label in ("vm:list-big-deep", "vm:delete-entry-refused"):
        # A seek into the large directory, never a scan of it.
        assert large_sized[label][0] <= 3 * max(small_sized[label][0], 1), (
            label,
            small_sized[label],
            large_sized[label],
        )
    for label, work in large_sized.items():
        if label.startswith("vm:"):
            continue
        # Linear: twenty times the entries costs at most about twenty times the
        # work, where a pairwise scan would cost about four hundred times.
        assert all(
            large <= 25 * small for large, small in zip(work, small_sized[label], strict=True)
        ), (label, small_sized, large_sized)
    # Journal parses are only the touched object's own located lines and the
    # index position's tail check: never the journal, at either size.
    assert all(parses <= 3 for _, _, parses in larger.values()), larger


def limits_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity) -> None:
    """The incremental tree check's limit, cycle and manifest-size branches (CODE-F008 a, b)."""
    from castle.namespace_snapshots import manifest

    def root_args(case, name):
        return dict(entry_id=identity(), parent_id=case.initial.root_id, name=name, metadata={})

    # (a) A flipped total that drives a limit rejection refuses recovery-required
    # instead of committing a rejection derived from it.
    case = Case(tmp_path / "limits-flip")
    with sqlite3.connect(case.root / "namespace.sqlite") as db:
        db.execute("UPDATE tree_totals SET live_entries=?", (n.MAX_ENTRIES,))
    with case.open() as h:
        request = case.request(h, "create-directory", root_args(case, "Over"))
        with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
            n.execute_namespace(h, request, {})
    with sqlite3.connect(case.root / "namespace.sqlite") as db:
        assert (
            db.execute(
                "SELECT 1 FROM operations WHERE operation_id=?", (request.operation_id,)
            ).fetchone()
            is None
        )

    # (b) An import whose two folders name each other as parent is a cycle.
    case = Case(tmp_path / "limits-cycle")
    first, second = identity(), identity()
    items = [
        dict(
            entry_id=first,
            kind="directory",
            parent_id=second,
            name="A",
            metadata={},
            cas_id=None,
            size=None,
        ),
        dict(
            entry_id=second,
            kind="directory",
            parent_id=first,
            name="B",
            metadata={},
            cas_id=None,
            size=None,
        ),
    ]
    with case.open() as h:
        with pytest.raises(n.NamespaceError, match="^namespace-cycle$"):
            case.apply(
                h,
                "import-folder",
                dict(
                    parent_id=case.initial.root_id,
                    entries=sorted(items, key=lambda i: i["entry_id"]),
                ),
            )

    # (c) The manifest-size identity agrees with the full encoding at the exact
    # boundary: a limit equal to the next state's encoded size admits it, one
    # byte less rejects it.
    def next_size() -> int:
        probe = Case(tmp_path / "limits-size-probe")
        with probe.open() as h:
            probe.apply(h, "create-directory", root_args(probe, "Sized"))
            info = h.info()
            return len(n.canonical_bytes(manifest(info, h.entries()), limit=10**9))

    size = next_size()
    for limit, expected in ((size, None), (size - 1, "namespace-limit-exceeded")):
        case = Case(tmp_path / f"limits-size-{limit}")
        with case.open() as h, monkeypatch.context() as patch:
            patch.setattr(n, "MAX_MANIFEST_BYTES", limit)
            if expected is None:
                case.apply(h, "create-directory", root_args(case, "Sized"))
            else:
                with pytest.raises(n.NamespaceError, match=f"^{expected}$"):
                    case.apply(h, "create-directory", root_args(case, "Sized"))


def reader_proof(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity) -> None:
    """Read-only handles adopt appends, revalidate once per handle, never write (CODE-F008 c, d)."""
    from castle.namespace_environment import LocalSession

    case = Case(tmp_path / "reader-overlay")
    with case.open() as h:
        file_id = identity()
        case.apply(
            h,
            "create-file",
            dict(
                entry_id=file_id,
                parent_id=case.initial.root_id,
                name="f",
                metadata={},
                content={"cas_id": case.stored.cas_id, "size": case.source.stat().st_size},
            ),
        )
    database = case.root / "namespace.sqlite"
    session = LocalSession(case.root, case.cas_root, case.directory / "domain.lock", read_only=True)

    def reader():
        return n.open_namespace_readonly(case.root, case.cas_root, session)

    calls = []
    revalidate = store.NamespaceHandle._cas_revalidate

    def counted(self):
        calls.append(self)
        return revalidate(self)

    journal_file = case.cas_root / journal.JOURNAL_FILENAME
    before = database.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(store.NamespaceHandle, "_cas_revalidate", counted)
        # An outside append is adopted by the reader without revalidation.
        extra = case.directory / "reader-extra"
        extra.write_bytes(b"appended while only readers are open\n")
        appended = cas.ingest_file(case.cas_root, extra, "reader-proof")
        with reader() as r:
            with r.admitted():
                assert r.object(appended.cas_id)[1] == extra.stat().st_size
            assert calls == []
        # A canonical rewrite (same records, other line endings) is adopted after
        # exactly one revalidation per reader handle, without latching.
        raw = journal_file.read_bytes()
        journal_file.chmod(0o644)
        journal_file.write_bytes(raw.replace(b"\n", b"\r\n"))
        for _ in range(2):
            with reader() as r:
                for _ in range(3):
                    assert _resolve(r, "f").entry_id == file_id
                assert [handle for handle in calls if handle is r] == [r]
        assert len(calls) == 2
    assert database.read_bytes() == before
    # A truncated journal no longer accounts for a stored object: the reader's
    # catch-up sees a shorter journal, revalidates in full and refuses at open.
    journal_file.write_bytes(journal_file.read_bytes().splitlines(keepends=True)[0])
    with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
        reader()


# --------------------------------------------------------------------------
# Phase 13: touched namespace content publication
# --------------------------------------------------------------------------


def _payload(tag: str) -> bytes:
    return f"phase 13 touched publication payload {tag:>8}\n".encode()


def _content(payload: bytes) -> dict:
    import hashlib

    return {"cas_id": "sha256:" + hashlib.sha256(payload).hexdigest(), "size": len(payload)}


def _create(case, h, payload: bytes, name: str, identity) -> n.MutationResult:
    content = _content(payload)
    request = case.request(
        h,
        "create-file",
        dict(
            entry_id=identity(),
            parent_id=case.initial.root_id,
            name=name,
            metadata={},
            content=content,
        ),
    )
    return n.execute_namespace(h, request, {content["cas_id"]: payload})


def _import(case, h, payloads: list[bytes], identity) -> n.MutationResult:
    entries = sorted(
        (
            dict(
                entry_id=identity(),
                kind="file",
                parent_id=case.initial.root_id,
                name=f"imported-{index}",
                metadata={},
                **_content(payload),
            )
            for index, payload in enumerate(payloads)
        ),
        key=lambda item: item["entry_id"],
    )
    request = case.request(
        h, "import-folder", dict(parent_id=case.initial.root_id, entries=entries)
    )
    return n.execute_namespace(h, request, {_content(p)["cas_id"]: p for p in payloads})


def _evidence(case) -> list[Path]:
    operations = case.root / "operations"
    return sorted(operations.rglob("publication.*")) if operations.exists() else []


def _rewrite(file: Path, data: bytes) -> None:
    mode = file.stat().st_mode & 0o777
    file.chmod(0o644)
    file.write_bytes(data)
    file.chmod(mode)


def _refuses(case, action, *, label: str) -> None:
    """Recovery-required, with no publication evidence and unchanged journal bytes."""
    journal_file = case.cas_root / journal.JOURNAL_FILENAME
    before = journal_file.read_bytes()
    with pytest.raises(n.NamespaceError) as refused:
        action()
    assert refused.value.code == "namespace-storage-recovery-required", label
    assert journal_file.read_bytes() == before, label
    assert not _evidence(case), label


def _lines(case) -> list[bytes]:
    return (case.cas_root / journal.JOURNAL_FILENAME).read_bytes().splitlines(keepends=True)


def _flip_relpath(line: bytes) -> bytes:
    """The same line, well formed and the same length, naming another relpath."""
    marker = b'"original_relpath":"'
    at = line.index(marker) + len(marker)
    return line[:at] + (b"_" if line[at : at + 1] == b"~" else b"~") + line[at + 1 :]


def _unseal(file: Path) -> None:
    import stat

    if getattr(file.stat(), "st_flags", 0) & getattr(stat, "UF_IMMUTABLE", 0):
        os.chflags(file, 0)
    file.chmod(0o644)


def _direct(directory: Path, payload: bytes, identity, *, prior: bool):
    """A store, an operation directory and one immutable stage, without a namespace.

    Returns the publication arguments and the touched proof a namespace's
    located index would supply, derived independently from the journal bytes.
    """
    from castle import cas_recovery as publication
    from castle import store_state

    directory.mkdir()
    cas_root = directory / "cas"
    seed = directory / "seed"
    seed.write_bytes(payload if prior else b"seed for the direct publication harness\n")
    cas.ingest_file(cas_root, seed, "seed")
    operation_id = str(identity())
    operation = directory / "operations" / operation_id
    operation.mkdir(parents=True)
    stage_id = str(identity())
    (operation / stage_id).write_bytes(payload)
    (operation / stage_id).chmod(0o444)
    binding = store_state.read_store_identity(cas_root)
    cas_id = _content(payload)["cas_id"]
    request = publication.PublicationRequest(
        str(identity()), operation_id, str(identity()), binding, cas_id, len(payload), stage_id
    )
    data = (cas_root / journal.JOURNAL_FILENAME).read_bytes()
    spans = store._frame(data)
    last = spans[-1]
    position = ("binding", len(data), len(spans), *last[:3], last[3]["at"], last[3]["event_id"])
    records = tuple(span[3] for span in spans if span[3]["cas_id"] == cas_id)
    row = cas.expected_index_rows(list(records))[0][cas_id] if records else None
    return (cas_root, operation, binding, request), publication.TouchedPublication(
        position, records, row
    )


def _run_direct(arguments, touched, *, recover: bool = False):
    from castle import cas_recovery as publication

    try:
        with journal._stable_lock(arguments[0]):
            return publication._run_locked(*arguments, recover=recover, touched=touched)
    except (OSError, sqlite3.Error, journal.CastleError, ValueError, TypeError, KeyError):
        # The namespace caller's own conversion (namespace_content.publish).
        n._refuse("namespace-storage-recovery-required")


def publication_cost_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity
) -> None:
    """One namespace publication costs the same at 20 and 400 prior objects.

    Counts every whole-store primitive (object hashes, tree walks, index
    images, whole-journal reads, whole-index rebuilds) and the journal bytes
    read positionally, through commit and cleanup of a two-payload import.
    """
    from castle import cas_recovery as publication

    def measure(size: int) -> tuple[dict[str, int], int, int]:
        case = Case(tmp_path / f"publication-cost-{size}")
        for index in range(size):
            source = case.directory / f"prior-{index:05}"
            source.write_bytes(f"prior object {index:05}\n".encode())
            cas.ingest_file(case.cas_root, source, "prior", original_relpath=f"prior/{index:05}")
        counts = {
            "sha256_file": 0,
            "authority_paths": 0,
            "tree_inventory": 0,
            "inspection_image": 0,
            "read_authority_journal": 0,
            "journal_read_bytes": 0,
            "full_index": 0,
            "index_record": 0,
        }
        pread = {"bytes": 0}

        def counted(name, function):
            def wrapper(*args, **kwargs):
                counts[name] += 1
                return function(*args, **kwargs)

            return wrapper

        read_bytes, real_pread = Path.read_bytes, os.pread

        def counted_read_bytes(self):
            if self.name == journal.JOURNAL_FILENAME:
                counts["journal_read_bytes"] += 1
            return read_bytes(self)

        def counted_pread(fd, length, offset):
            data = real_pread(fd, length, offset)
            pread["bytes"] += len(data)
            return data

        with case.open() as h:
            h.info()  # catch the located index up to the prior ingests first
            with monkeypatch.context() as patch:
                patch.setattr(cas, "sha256_file", counted("sha256_file", cas.sha256_file))
                patch.setattr(
                    cas, "_authority_paths", counted("authority_paths", cas._authority_paths)
                )
                patch.setattr(
                    cas, "_tree_inventory", counted("tree_inventory", cas._tree_inventory)
                )
                patch.setattr(
                    cas, "_inspection_image", counted("inspection_image", cas._inspection_image)
                )
                patch.setattr(
                    journal,
                    "_read_authority_journal",
                    counted("read_authority_journal", journal._read_authority_journal),
                )
                patch.setattr(publication, "_index", counted("full_index", publication._index))
                patch.setattr(cas, "_index_record", counted("index_record", cas._index_record))
                patch.setattr(Path, "read_bytes", counted_read_bytes)
                patch.setattr(os, "pread", counted_pread)
                _import(case, h, [_payload(f"a{size}"), _payload(f"b{size}")], identity)
        assert cas.verify(case.cas_root).ok
        journal_size = (case.cas_root / journal.JOURNAL_FILENAME).stat().st_size
        return counts, pread["bytes"], journal_size

    (small, small_read, _), (large, large_read, large_journal) = measure(20), measure(400)
    assert small == large, {"20": small, "400": large}
    for name in (
        "authority_paths",
        "tree_inventory",
        "inspection_image",
        "read_authority_journal",
        "journal_read_bytes",
        "full_index",
    ):
        assert large[name] == 0, (name, large)
    assert large["index_record"] == 2, large
    # Positional journal reads are the tail and the publications' own lines:
    # bounded by request size, never a share of the journal.
    assert large_read <= small_read + 1024, (small_read, large_read)
    assert large_read * 4 < large_journal, (large_read, large_journal)


def publication_damage_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity
) -> None:
    """Touched damage refuses before evidence; untouched damage waits for diagnosis."""
    import hashlib

    from castle import cas_recovery as publication
    from castle import namespace_content as content

    payload = _payload("damage")
    cas_id = _content(payload)["cas_id"]

    def tail(case):
        lines = _lines(case)
        return case.cas_root / journal.JOURNAL_FILENAME, b"".join(lines[:-1]), lines[-1]

    def damage(case, kind):
        file, head, last = tail(case)
        index = case.cas_root / cas.INDEX_FILENAME
        if kind == "tail":
            _rewrite(file, head + b"[" + last[1:])
        elif kind == "tail-without-lf":
            _rewrite(file, head + last[:-1])
        elif kind == "tail-ending-in-cr":
            _rewrite(file, head + last[:-1] + b"\r")
        elif kind == "unframed-append":
            _rewrite(file, head + last + b"not a journal record\n")
        elif kind == "index-missing":
            index.unlink()
        elif kind == "index-symlink":
            real = case.directory / "outside.sqlite"
            index.rename(real)
            index.symlink_to(real)
        elif kind == "index-hardlink":
            os.link(index, case.directory / "alias.sqlite")
        elif kind in {"objects-row", "names-row"}:
            with closing(sqlite3.connect(index)) as db, db:
                if kind == "objects-row":
                    db.execute(
                        "INSERT INTO objects VALUES (?,?,?,?,?,?)",
                        (
                            cas_id,
                            len(payload),
                            "text/plain",
                            cas.object_relpath(cas_id).as_posix(),
                            "2000-01-01T00:00:00Z",
                            None,
                        ),
                    )
                else:
                    db.execute(
                        "INSERT INTO names VALUES (?,?,?,?)",
                        (cas_id, "elsewhere", "stray", "2000-01-01T00:00:00Z"),
                    )
        elif kind == "unjournaled-object":
            destination = cas.object_path(case.cas_root, cas_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(payload)
            destination.chmod(0o444)
        else:
            raise AssertionError(kind)

    kinds = (
        "tail",
        "tail-without-lf",
        "tail-ending-in-cr",
        "unframed-append",
        "index-missing",
        "index-symlink",
        "index-hardlink",
        "objects-row",
        "names-row",
        "unjournaled-object",
    )
    for kind in kinds:
        case = Case(tmp_path / f"publication-damage-{kind}")
        with case.open() as h:
            h.info()
        damage(case, kind)

        def attempt(case=case):
            with case.open() as h:
                _create(case, h, payload, "damaged", identity)

        _refuses(case, attempt, label=kind)

    # The tail must end in LF: without the check the publication's line is
    # written onto the unterminated tail, two records on one physical line.
    case = Case(tmp_path / "publication-damage-mutant-lf")
    with case.open() as h:
        h.info()
    damage(case, "tail-without-lf")

    def without_lf(descriptor, size, offset, length, digest):
        try:
            return store._span(descriptor, size, offset, length, digest)
        except store._Framing:
            n._refuse("namespace-storage-recovery-required")

    with monkeypatch.context() as patch:
        patch.setattr(publication, "_line", without_lf)
        try:
            with case.open() as h:
                _create(case, h, payload, "damaged", identity)
        except n.NamespaceError:
            pass
    with pytest.raises(journal.JournalError):
        journal._read_authority_journal(case.cas_root)

    # Without the pre-evidence row comparison the drift is found only after
    # effects: evidence is saved and the journal has grown.
    case = Case(tmp_path / "publication-damage-mutant-rows")
    with case.open() as h:
        h.info()
    damage(case, "objects-row")
    before = (case.cas_root / journal.JOURNAL_FILENAME).read_bytes()
    from collections import Counter

    with monkeypatch.context() as patch:
        patch.setattr(
            publication, "_touched_rows", lambda connection, identity: (Counter(), Counter())
        )
        with pytest.raises(n.NamespaceError), case.open() as h:
            _create(case, h, payload, "damaged", identity)
    assert _evidence(case) and (case.cas_root / journal.JOURNAL_FILENAME).read_bytes() != before

    # A touched identity's own located lines are re-proven by the caller's
    # touched proof (handle.object), which falls to full revalidation.
    case = Case(tmp_path / "publication-damage-own-line")
    with case.open() as h:
        h.info()
    file, head, last = tail(case)
    assert json.loads(last)["cas_id"] == case.stored.cas_id
    with case.open() as h, h.admitted():
        request = publication.PublicationRequest(
            str(identity()),
            str(identity()),
            str(identity()),
            h.binding,
            case.stored.cas_id,
            case.source.stat().st_size,
            str(identity()),
        )
        content._touched(h, request)  # proven before the damage
        _rewrite(file, head + _flip_relpath(last))
        with pytest.raises(n.NamespaceError) as refused:
            content._touched(h, request)
    assert refused.value.code == "namespace-storage-recovery-required"

    # Untouched damage (another object's bytes) does not refuse publication;
    # diagnosis refuses that store.
    case = Case(tmp_path / "publication-damage-untouched")
    other = cas.object_path(case.cas_root, case.stored.cas_id)
    with case.open() as h:
        h.info()
    original = other.read_bytes()
    _unseal(other)
    other.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    other.chmod(0o444)
    with case.open() as h:
        _create(case, h, payload, "untouched", identity)
    with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
        with case.open() as h:
            h._diagnose()

    # Existing identity (the dedup branch): its object's bytes are hashed.
    arguments, touched = _direct(tmp_path / "publication-dedup", payload, identity, prior=True)
    existing = cas.object_path(arguments[0], cas_id)
    inode = existing.stat().st_ino
    receipt = _run_direct(arguments, touched)
    assert existing.stat().st_ino == inode
    assert journal.read_journal(arguments[0])[-1]["outcome"] == "dedup"
    raw = (arguments[0] / journal.JOURNAL_FILENAME).read_bytes()
    assert receipt.event_offset + receipt.event_length == len(raw)
    assert receipt.event_sha256 == hashlib.sha256(raw[receipt.event_offset :]).hexdigest()
    assert cas.verify(arguments[0]).ok

    def rewritten(label: str):
        arguments, touched = _direct(tmp_path / label, payload, identity, prior=True)
        file = cas.object_path(arguments[0], cas_id)
        _unseal(file)
        file.write_bytes(bytes([payload[0] ^ 1]) + payload[1:])
        file.chmod(0o444)
        return arguments, touched

    arguments, touched = rewritten("publication-dedup-rewritten")
    before = (arguments[0] / journal.JOURNAL_FILENAME).read_bytes()
    with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
        _run_direct(arguments, touched)
    assert (arguments[0] / journal.JOURNAL_FILENAME).read_bytes() == before
    assert not list(arguments[1].glob("publication.*"))
    arguments, touched = rewritten("publication-dedup-mutant")
    with monkeypatch.context() as patch:
        patch.setattr(cas, "_validate_authority_objects", lambda *args: None)
        _run_direct(arguments, touched)  # the mutant publishes over rewritten bytes


def touched_guard_proof(tmp_path: Path, Case: type, identity) -> None:
    """Each pre-evidence guard refuses a stale or wrong caller proof (CODE-F003).

    The touched path trusts the caller's located proof only as far as these
    checks re-establish it: every case refuses with no evidence and unchanged
    journal bytes.
    """
    from dataclasses import replace

    from castle import cas_recovery as publication

    payload = _payload("guards")

    def refused(label: str, mutate, *, prior: bool = False) -> None:
        arguments, touched = _direct(tmp_path / f"guard-{label}", payload, identity, prior=prior)
        touched = mutate(arguments, touched)
        journal_file = arguments[0] / journal.JOURNAL_FILENAME
        before = journal_file.read_bytes()
        with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
            _run_direct(arguments, touched)
        assert journal_file.read_bytes() == before, label
        assert not list(arguments[1].glob("publication.*")), label

    def position(touched, **changes):
        names = ("identity", "length", "count", "offset", "span", "digest", "at", "event")
        fields = dict(zip(names, touched.position, strict=True)) | changes
        return replace(touched, position=tuple(fields[name] for name in names))

    refused("length", lambda a, t: position(t, length=t.position[1] + 1))
    refused("tail-digest", lambda a, t: position(t, digest="0" * 64))
    refused("tail-event", lambda a, t: position(t, event="uuid:" + str(identity())))
    refused(
        "empty-count",
        lambda a, t: position(t, count=0, offset=None, span=None, digest=None, at=None, event=None),
    )

    def foreign(arguments, touched):
        seed = journal.read_journal(arguments[0])[0]
        return replace(touched, records=(seed,))

    refused("foreign-record", foreign)
    refused(
        "row",
        lambda a, t: replace(t, row=(*t.row[:2], "application/x-other", *t.row[3:])),
        prior=True,
    )

    def own_relpath(arguments, touched):
        request = arguments[3]
        record = dict(
            touched.records[0],
            source_root_id=f"namespace:{request.namespace_id}",
            original_relpath=f"{request.operation_id}/{request.publication_id}",
        )
        return replace(touched, records=(record,))

    refused("own-relpath", own_relpath, prior=True)

    def cross_object(arguments, touched):
        request = arguments[3]
        with closing(sqlite3.connect(arguments[0] / cas.INDEX_FILENAME)) as db, db:
            db.execute(
                "INSERT INTO names VALUES (?,?,?,?)",
                (
                    journal.read_journal(arguments[0])[0]["cas_id"],
                    f"namespace:{request.namespace_id}",
                    f"{request.operation_id}/{request.publication_id}",
                    "2000-01-01T00:00:00Z",
                ),
            )
        return touched

    refused("cross-object-relpath", cross_object)

    def leftover(arguments, touched):
        temporary = publication._temporary(arguments[0], arguments[3])
        temporary.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(b"left by a killed writer")
        return touched

    refused("leftover-temporary", leftover)


def publication_crash_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity
) -> None:
    """Honest faults: crash replay, duplicate append, state loss, restored journal."""
    import types

    def published(label: str):
        case = Case(tmp_path / f"publication-crash-{label}")
        with case.open() as h:
            _create(case, h, _payload("first"), "first", identity)
        return case, _lines(case)[-1]

    def append(case, data: bytes) -> None:
        file = case.cas_root / journal.JOURNAL_FILENAME
        _rewrite(file, file.read_bytes() + data)

    def lose_rows(case, line: bytes) -> None:
        record = json.loads(line)
        with closing(sqlite3.connect(case.root / "namespace.sqlite")) as db, db:
            db.execute("DELETE FROM cas_records WHERE event_id=?", (record["event_id"],))
            db.execute("DELETE FROM cas_objects WHERE cas_id=?", (record["cas_id"],))

    def second(case):
        def attempt():
            with case.open() as h:
                _create(case, h, _payload("second"), "second", identity)

        return attempt

    # A saved event appended again after a crash (the same bytes twice).
    case, line = published("replay")
    append(case, line)
    _refuses(case, second(case), label="replay")

    # The same replay after the namespace lost its own located row and
    # witness: canonical ordering alone refuses it.
    case, line = published("state-loss")
    append(case, line)
    lose_rows(case, line)
    _refuses(case, second(case), label="state-loss")

    # A rolled-back namespace image plus the replay: the appended range
    # repeats an id within itself.
    case = Case(tmp_path / "publication-crash-rollback")
    with case.open() as h:
        h.info()
    image = (case.root / "namespace.sqlite").read_bytes()
    with case.open() as h:
        _create(case, h, _payload("first"), "first", identity)
    append(case, _lines(case)[-1])
    (case.root / "namespace.sqlite").write_bytes(image)
    for sidecar in ("namespace.sqlite-wal", "namespace.sqlite-shm"):
        (case.root / sidecar).unlink(missing_ok=True)
    _refuses(case, second(case), label="rollback")

    # Appending onto a restored older journal copy: the indexed tail moved.
    case = Case(tmp_path / "publication-crash-restored")
    older = (case.cas_root / journal.JOURNAL_FILENAME).read_bytes()
    with case.open() as h:
        _create(case, h, _payload("first"), "first", identity)
    _rewrite(case.cas_root / journal.JOURNAL_FILENAME, older)
    _refuses(case, second(case), label="restored")

    # Either guard alone refuses the replay; dropping both accepts it.
    class Unordered(types.SimpleNamespace):
        counter = 0

        def journal_sort_key(self, record):
            Unordered.counter += 1
            return (
                journal.parse_journal_timestamp("9999-01-01T00:00:00Z", "mutant"),
                f"{Unordered.counter:012}",
            )

    unordered = Unordered()
    for name in dir(journal):
        if not name.startswith("__") and name != "journal_sort_key":
            setattr(unordered, name, getattr(journal, name))
    outcomes = {}
    for mutant in ("no-known-id", "no-ordering", "neither"):
        case, line = published(f"mutant-{mutant}")
        append(case, line)
        lose = mutant == "neither"
        if lose:
            lose_rows(case, line)
        with monkeypatch.context() as patch:
            if mutant in {"no-known-id", "neither"}:
                patch.setattr(store.NamespaceHandle, "_known_event", lambda self, event: False)
            if mutant in {"no-ordering", "neither"}:
                patch.setattr(store, "journal", unordered)
            try:
                second(case)()
                outcomes[mutant] = "accepted"
            except n.NamespaceError as error:
                outcomes[mutant] = error.code
    assert outcomes == {
        "no-known-id": "namespace-storage-recovery-required",
        "no-ordering": "namespace-storage-recovery-required",
        "neither": "accepted",
    }, outcomes

    # An ordinary ingest killed between its append and its index commit: the
    # appended line is located and an unrelated publication proceeds, as the
    # touched-only approval allows; verification reports the index gap.
    case = Case(tmp_path / "publication-crash-ingest")
    with case.open() as h:
        h.info()
    interrupted = case.directory / "interrupted"
    interrupted.write_bytes(b"ordinary ingest interrupted before its index commit\n")

    def killed(*args, **kwargs):
        raise OSError("killed before the index commit")

    with monkeypatch.context() as patch, pytest.raises(Exception):
        patch.setattr(cas, "_index_record", killed)
        cas.ingest_file(case.cas_root, interrupted, "interrupted")
    with case.open() as h:
        _create(case, h, _payload("second"), "second", identity)
    assert not cas.verify(case.cas_root).ok
    cas.rebuild_index(case.cas_root)
    assert cas.verify(case.cas_root).ok


def publication_recovery_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, Case: type, identity
) -> None:
    """Interrupted touched publications finish exactly once through recovery."""
    from castle import cas_recovery as publication
    from castle import namespace_content as content

    def killer(point: str, *, skip: int = 0):
        target = {
            "before-copy": (publication, "_copy_object"),
            "before-append": (publication, "_write_events"),
            "before-index": (publication, "_index_touched"),
            "before-completion": (publication, "_appended"),
            "before-receipt": (content, "save_receipt"),
        }[point]
        original = getattr(*target)
        calls = {"count": 0}

        def interrupted(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] > skip:
                raise OSError(f"killed {point}")
            return original(*args, **kwargs)

        return target, interrupted

    def exactly_once(case, payloads: list[bytes]) -> None:
        records = journal.read_journal(case.cas_root)
        for payload in payloads:
            ours = [r for r in records if r["cas_id"] == _content(payload)["cas_id"]]
            assert len(ours) == 1, (payload, ours)
        assert cas.verify(case.cas_root).ok
        assert not _evidence(case)

    for point in (
        "before-copy",
        "before-append",
        "before-index",
        "before-completion",
        "before-receipt",
    ):
        # Two payloads: the kill lands on the second publication (the receipt
        # save is once per operation, after both).
        second = 0 if point == "before-receipt" else 1
        for payloads, skip in (
            ([_payload(point)], 0),
            ([_payload("x" + point), _payload("y" + point)], second),
        ):
            case = Case(tmp_path / f"publication-recovery-{point}-{len(payloads)}")
            target, interrupted = killer(point, skip=skip)
            with monkeypatch.context() as patch, case.open() as h:
                patch.setattr(*target, interrupted)
                with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
                    if len(payloads) == 1:
                        _create(case, h, payloads[0], "recovered", identity)
                    else:
                        _import(case, h, payloads, identity)
            n.recover_namespace(case.root, case.cas_root, case.session, None)
            exactly_once(case, payloads)

    # Recovery refuses a moved tail (the evidence's prefix_tail no longer
    # names the line before its stream); a mutant that skips it accepts.
    for mutant in (False, True):
        arguments, _ = _direct(
            tmp_path / f"publication-recovery-moved-{mutant}",
            _payload("moved"),
            identity,
            prior=False,
        )
        target, interrupted = killer("before-append")
        with monkeypatch.context() as patch, pytest.raises(n.NamespaceError):
            patch.setattr(*target, interrupted)
            _run_direct(arguments, None)
        file = arguments[0] / journal.JOURNAL_FILENAME
        _rewrite(file, _flip_relpath(file.read_bytes()))
        record = publication._record_file(arguments[1], arguments[3])
        saved = record.read_bytes()
        with monkeypatch.context() as patch:
            if mutant:
                patch.setattr(publication, "_tail", lambda raw: json.loads(saved)["prefix_tail"])
                _run_direct(arguments, None, recover=True)
                continue
            with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
                _run_direct(arguments, None, recover=True)
        assert record.read_bytes() == saved


def receipt_proof(tmp_path: Path, Case: type, identity) -> None:
    """A receipt binds its own line where it says; unrelated earlier lines are diagnosis's."""
    from castle import namespace_content as content
    from castle import namespace_transactions as transactions

    def check(label: str, damage) -> None:
        case = Case(tmp_path / f"receipt-{label}")
        with case.open() as h:
            operation = _create(case, h, _payload("receipt"), "receipt", identity).operation_id
        file = case.cas_root / journal.JOURNAL_FILENAME
        with case.open() as h:
            row = transactions._row(h, operation)
            value, requests = content.recipe(h, row)
            lines = file.read_bytes().splitlines(keepends=True)
            if damage is not None:
                _rewrite(file, damage(lines))
            with content.journal_descriptor(case.cas_root) as (descriptor, size):
                content.validate_receipt(
                    h, row, requests[0], value["publication"][0]["receipt"], descriptor, size
                )
        return case

    check("valid", None)
    for label, damage in (
        ("own-line-changed", lambda lines: b"".join(lines[:-1]) + _flip_relpath(lines[-1])),
        ("own-line-moved", lambda lines: lines[0] + b"\n" + b"".join(lines[1:])),
        ("own-line-off-boundary", lambda lines: lines[0][:-1] + b" " + b"".join(lines[1:])),
        ("own-line-removed", lambda lines: b"".join(lines[:-1])),
    ):
        with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
            check(label, damage)

    # An unrelated earlier line rewritten in place stays well formed: the
    # receipt still proves its own line; diagnosis refuses the store.
    case = check("unrelated", lambda lines: _flip_relpath(lines[0]) + b"".join(lines[1:]))
    with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
        with case.open() as h:
            h._diagnose()
