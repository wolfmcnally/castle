"""Behavioral contract for the isolated, once-only v1 journal migrator."""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import hashlib
import inspect
import json
import os
import sqlite3
import stat
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from castle import cas, journal, journal_migrate_v1, store_state

_EXPECTED_V1_FIELDS = frozenset(
    {
        "at",
        "event",
        "cas_id",
        "size",
        "media_type",
        "source_root_id",
        "original_relpath",
        "mtime",
        "batch",
        "outcome",
    }
)

_EXPECTED_V2_FIELDS = frozenset(
    {
        "schema",
        "event_id",
        "at",
        "event",
        "cas_id",
        "size",
        "media_type",
        "source_root_id",
        "original_relpath",
        "batch",
        "outcome",
    }
)


def _cas_id(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _v1_row(
    payload: bytes = b"alpha\n",
    *,
    at: str = "2031-03-04T00:01:00Z",
    relpath: str = "alpha.txt",
    source_root_id: str = "seed",
    batch: str | None = "batch-a",
    outcome: str = "stored",
    media_type: str = "text/plain",
) -> dict[str, Any]:
    return {
        "at": at,
        "event": "ingest",
        "cas_id": _cas_id(payload),
        "size": len(payload),
        "media_type": media_type,
        "source_root_id": source_root_id,
        "original_relpath": relpath,
        "mtime": at,
        "batch": batch,
        "outcome": outcome,
    }


def _v1_journal_bytes(rows: Sequence[dict[str, Any]], *, terminal_newline: bool = True) -> bytes:
    body = b"\n".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") for row in rows
    )
    return body + (b"\n" if terminal_newline else b"")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_index(
    root: Path,
    rows: Sequence[dict[str, Any]],
    *,
    remotes: Sequence[tuple[str, str, str, str, str]] = (),
) -> None:
    objects, names = cas.expected_index_rows(rows)
    with sqlite3.connect(root / cas.INDEX_FILENAME) as connection:
        connection.executescript(cas._SCHEMA)
        connection.execute("DELETE FROM objects")
        connection.execute("DELETE FROM names")
        connection.execute("DELETE FROM remotes")
        connection.executemany(
            """
            INSERT INTO objects
                (cas_id, size, media_type, stored_relpath, first_seen_utc, batch)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            objects.values(),
        )
        connection.executemany(
            """
            INSERT INTO names
                (cas_id, source_root_id, original_relpath, recorded_utc)
            VALUES (?, ?, ?, ?)
            """,
            names,
        )
        connection.executemany(
            f"INSERT INTO remotes ({cas._REMOTES_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            remotes,
        )


def _write_v1_store(
    root: Path,
    rows: Sequence[dict[str, Any]],
    payloads: Sequence[bytes],
    *,
    journal_bytes: bytes | None = None,
    remotes: Sequence[tuple[str, str, str, str, str]] = (),
) -> Path:
    (root / "objects" / "sha256").mkdir(parents=True)
    (root / journal.JOURNAL_LOCK_FILENAME).touch()
    for payload in payloads:
        target = cas.object_path(root, _cas_id(payload))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        target.chmod(0o444)
    journal_path = root / journal.JOURNAL_FILENAME
    journal_path.write_bytes(
        journal_bytes if journal_bytes is not None else _v1_journal_bytes(rows)
    )
    _write_index(root, rows, remotes=remotes)
    return journal_path


def _index_rows(root: Path) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    with sqlite3.connect(root / cas.INDEX_FILENAME) as connection:
        objects = connection.execute(
            """
            SELECT cas_id, size, media_type, stored_relpath, first_seen_utc, batch
            FROM objects
            """
        ).fetchall()
        names = connection.execute(
            """
            SELECT cas_id, source_root_id, original_relpath, recorded_utc
            FROM names
            """
        ).fetchall()
    return objects, names


def _remote_rows(root: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(root / cas.INDEX_FILENAME) as connection:
        return connection.execute(f"SELECT {cas._REMOTES_COLUMNS} FROM remotes").fetchall()


def _whole_store_snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    snapshot: dict[str, tuple[Any, ...]] = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(root).as_posix()
            info = entry.stat(follow_symlinks=False)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                snapshot[relative] = ("dir", mode, info.st_ino)
                pending.append(path)
            elif stat.S_ISREG(info.st_mode):
                snapshot[relative] = (
                    "file",
                    mode,
                    info.st_ino,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            elif stat.S_ISLNK(info.st_mode):
                snapshot[relative] = ("symlink", mode, info.st_ino, os.readlink(path))
            else:
                snapshot[relative] = (f"type-{stat.S_IFMT(info.st_mode):#o}", mode, info.st_ino)
    return snapshot


def _v2_record(row: dict[str, Any], *, ordinal: int = 1) -> dict[str, Any]:
    return {
        "schema": journal.CAS_JOURNAL_EVENT_SCHEMA,
        "event_id": journal_migrate_v1._v1_event_id(ordinal, row),
        **{name: value for name, value in row.items() if name != "mtime"},
    }


def _ordinary_rows() -> tuple[list[dict[str, Any]], list[bytes]]:
    payloads = [b"alpha\n", b"beta\n"]
    rows = [
        _v1_row(payloads[0], at="2031-03-04T00:01:00Z", relpath="alpha.txt"),
        _v1_row(payloads[1], at="2031-03-04T00:02:00Z", relpath="beta.txt"),
    ]
    return rows, payloads


def _child_environment() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(Path(cas.__file__).parents[1])}


def test_migration_public_surface_is_exact(tmp_path: Path) -> None:
    assert journal_migrate_v1.__all__ == [
        "MIGRATION_RESULT_SCHEMA",
        "JournalMigrationError",
        "MigrationResult",
        "migrate_v1_to_v2",
    ]
    assert journal_migrate_v1.MIGRATION_RESULT_SCHEMA == "migration.v1"
    assert inspect.signature(
        journal_migrate_v1.migrate_v1_to_v2, eval_str=True
    ) == inspect.Signature(
        parameters=[
            inspect.Parameter("root", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Path),
            inspect.Parameter(
                "expect_journal_sha256",
                inspect.Parameter.KEYWORD_ONLY,
                annotation=str,
            ),
        ],
        return_annotation=journal_migrate_v1.MigrationResult,
    )
    assert journal_migrate_v1.JournalMigrationError.__bases__ == (journal.CastleError,)
    assert [field.name for field in dataclasses.fields(journal_migrate_v1.MigrationResult)] == [
        "root",
        "source_sha256",
        "target_sha256",
        "event_count",
        "object_count",
    ]

    with pytest.raises(
        journal_migrate_v1.JournalMigrationError, match="cas-migration-ordinal"
    ) as caught:
        journal_migrate_v1._v1_event_id(0, _v1_row())
    assert type(caught.value) is journal_migrate_v1.JournalMigrationError
    assert caught.value.code == "cas-migration-ordinal"
    assert caught.value.detail
    assert not isinstance(caught.value, journal.JournalError | cas.CasError)
    result = journal_migrate_v1.MigrationResult(tmp_path, "a" * 64, "b" * 64, 3, 2)
    assert result.as_mapping() == {
        "schema": "migration.v1",
        "root": str(tmp_path),
        "from": "v1",
        "to": "v2",
        "journal_sha256_before": "a" * 64,
        "journal_sha256_after": "b" * 64,
        "event_count": 3,
        "object_count": 2,
    }
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(result, "event_count", 4)


def test_migration_digest_mismatch_precedes_v1_parsing(tmp_path: Path) -> None:
    root = tmp_path / "store"
    (root / "objects" / "sha256").mkdir(parents=True)
    (root / journal.JOURNAL_LOCK_FILENAME).touch()
    source = b"\xff not utf8\n"
    journal_path = root / journal.JOURNAL_FILENAME
    journal_path.write_bytes(source)
    with pytest.raises(journal_migrate_v1.JournalMigrationError) as mismatch:
        journal_migrate_v1.migrate_v1_to_v2(root, expect_journal_sha256="0" * 64)
    assert mismatch.value.code == "cas-migration-digest-mismatch"
    with pytest.raises(journal_migrate_v1.JournalMigrationError) as encoding:
        journal_migrate_v1.migrate_v1_to_v2(
            root,
            expect_journal_sha256=_digest_bytes(source),
        )
    assert encoding.value.code == "cas-migration-encoding"


def test_v1_migration_is_byte_deterministic(tmp_path: Path) -> None:
    rows, payloads = _ordinary_rows()
    outputs: list[bytes] = []
    for name in ("first", "second"):
        root = tmp_path / name
        journal_path = _write_v1_store(root, rows, payloads)
        journal_migrate_v1.migrate_v1_to_v2(
            root,
            expect_journal_sha256=_digest_bytes(journal_path.read_bytes()),
        )
        outputs.append(journal_path.read_bytes())
    assert outputs[0] == outputs[1]


def test_migration_validates_complete_v2_sequence_and_index_fold_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, payloads = _ordinary_rows()
    root = tmp_path / "store"
    journal_path = _write_v1_store(root, rows, payloads)
    before = _whole_store_snapshot(root)
    reached: list[str] = []

    def invalid(_records: Sequence[dict[str, Any]]) -> Any:
        reached.append("fold")
        raise cas.CasError("forced invalid fold")

    monkeypatch.setattr(cas, "expected_index_rows", invalid)
    with pytest.raises(journal_migrate_v1.JournalMigrationError) as caught:
        journal_migrate_v1.migrate_v1_to_v2(
            root,
            expect_journal_sha256=_digest_bytes(journal_path.read_bytes()),
        )
    assert caught.value.code == "cas-migration-invalid-fold"
    assert reached == ["fold"]
    assert _whole_store_snapshot(root) == before


def test_migration_accepts_symlinked_ancestor_and_resolves_root_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    link_target = real_parent / "link-target"
    link_target.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(link_target, target_is_directory=True)
    rows, payloads = _ordinary_rows()
    real_root = real_parent / "store"
    journal_path = _write_v1_store(real_root, rows, payloads)
    lexical_root = tmp_path / "store"
    lexical_rows = [_v1_row(b"lexical decoy\n", relpath="lexical.txt")]
    lexical_journal = _write_v1_store(lexical_root, lexical_rows, [b"lexical decoy\n"])
    lexical_before = _whole_store_snapshot(lexical_root)
    requested_root = linked_parent / ".." / "store"
    assert Path(os.path.normpath(requested_root)) == lexical_root
    assert requested_root.resolve(strict=True) == real_root
    real_resolve = Path.resolve
    calls: list[Path] = []

    def recording_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        calls.append(self)
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", recording_resolve)
    result = journal_migrate_v1.migrate_v1_to_v2(
        requested_root,
        expect_journal_sha256=_digest_bytes(journal_path.read_bytes()),
    )
    assert result.root == real_root
    assert calls == [requested_root]
    store_state.prepare_store(real_root)
    assert len(journal.read_journal(real_root)) == 2
    assert _whole_store_snapshot(lexical_root) == lexical_before
    assert lexical_journal.read_bytes() == _v1_journal_bytes(lexical_rows)


def test_migration_replans_under_the_stable_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, payloads = _ordinary_rows()
    root = tmp_path / "store"
    journal_path = _write_v1_store(root, rows, payloads)
    real_plan = journal_migrate_v1._plan_v1_to_v2
    real_lock = journal._stable_lock
    inside = False
    observations: list[bool] = []

    def plan(root: Path, *, expect_journal_sha256: str) -> Any:
        observations.append(inside)
        return real_plan(root, expect_journal_sha256=expect_journal_sha256)

    @contextlib.contextmanager
    def lock(root: Path) -> Iterator[Any]:
        nonlocal inside
        with real_lock(root) as handle:
            inside = True
            try:
                yield handle
            finally:
                inside = False

    monkeypatch.setattr(journal_migrate_v1, "_plan_v1_to_v2", plan)
    monkeypatch.setattr(journal, "_stable_lock", lock)
    journal_migrate_v1.migrate_v1_to_v2(
        root,
        expect_journal_sha256=_digest_bytes(journal_path.read_bytes()),
    )
    assert observations == [False, True]


@pytest.mark.parametrize("cut", ["stage-fsync", "readback", "replace"])
def test_migration_prepublication_faults_preserve_authority_and_clean_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    _assert_migration_lifecycle(tmp_path / "lifecycle", monkeypatch)
    rows, payloads = _ordinary_rows()
    root = tmp_path / "store"
    journal_path = _write_v1_store(root, rows, payloads)
    before = _whole_store_snapshot(root)
    source_digest = _digest_bytes(journal_path.read_bytes())
    reached: list[str] = []
    real_fsync = journal_migrate_v1.os.fsync
    real_read = Path.read_bytes

    if cut == "stage-fsync":

        def fsync(descriptor: int) -> None:
            real_fsync(descriptor)
            reached.append(cut)
            raise OSError("injected stage fsync failure")

        monkeypatch.setattr(journal_migrate_v1.os, "fsync", fsync)
    elif cut == "readback":

        def read_bytes(self: Path) -> bytes:
            value = real_read(self)
            if self.name.startswith(".cas-journal-migrate."):
                reached.append(cut)
                return value + b"corrupt"
            return value

        monkeypatch.setattr(Path, "read_bytes", read_bytes)
    else:

        def replace(*_args: object, **_kwargs: object) -> None:
            reached.append(cut)
            raise OSError("injected replace failure")

        monkeypatch.setattr(journal_migrate_v1.os, "replace", replace)

    with pytest.raises((OSError, journal_migrate_v1.JournalMigrationError)):
        journal_migrate_v1.migrate_v1_to_v2(
            root,
            expect_journal_sha256=source_digest,
        )
    assert reached == [cut]
    after = _whole_store_snapshot(root)
    before_without_lock = {k: v for k, v in before.items() if k != journal.JOURNAL_LOCK_FILENAME}
    after_without_lock = {k: v for k, v in after.items() if k != journal.JOURNAL_LOCK_FILENAME}
    assert after_without_lock == before_without_lock
    assert not list(root.glob(".cas-journal-migrate.*"))


def test_migration_is_once_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_migration_lifecycle(tmp_path / "lifecycle", monkeypatch)
    rows, payloads = _ordinary_rows()
    root = tmp_path / "store"
    journal_path = _write_v1_store(root, rows, payloads)
    first = journal_migrate_v1.migrate_v1_to_v2(
        root,
        expect_journal_sha256=_digest_bytes(journal_path.read_bytes()),
    )
    migrated = journal_path.read_bytes()
    with pytest.raises(journal_migrate_v1.JournalMigrationError) as second:
        journal_migrate_v1.migrate_v1_to_v2(
            root,
            expect_journal_sha256=_digest_bytes(migrated),
        )
    assert first.event_count == 2
    assert second.value.code == "cas-migration-already-v2"
    assert journal_path.read_bytes() == migrated
    assert not (root / "store.json").exists()
    with pytest.raises(store_state.StoreStateError, match="^store-identity-required$"):
        journal.read_journal(root)
    store_state.prepare_store(root)
    assert len(journal.read_journal(root)) == 2
    with pytest.raises(journal_migrate_v1.JournalMigrationError, match="cas-migration-already-v2"):
        journal_migrate_v1.migrate_v1_to_v2(root, expect_journal_sha256=_digest_bytes(migrated))


def test_postpublication_rebuild_failure_reports_repair_without_rolling_back_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, payloads = _ordinary_rows()
    root = tmp_path / "store"
    journal_path = _write_v1_store(root, rows, payloads)
    (root / cas.INDEX_FILENAME).unlink()
    real_rebuild = cas._rebuild_authority_index
    source = journal_path.read_bytes()
    reached: list[bool] = []

    def refuse(_root: Path, _records: Any) -> dict[str, int]:
        reached.append(bool(journal._read_authority_journal(root)))
        raise cas.CasError("forced rebuild failure")

    monkeypatch.setattr(cas, "_rebuild_authority_index", refuse)
    with pytest.raises(journal_migrate_v1.JournalMigrationError) as caught:
        journal_migrate_v1.migrate_v1_to_v2(
            root,
            expect_journal_sha256=_digest_bytes(source),
        )
    assert caught.value.code == "cas-migration-index-rebuild"
    assert "journal is already migrated" in caught.value.detail
    assert f"castle rebuild-index --root {root}" in caught.value.detail
    assert reached == [True]
    assert journal_path.read_bytes() != source
    with pytest.raises(store_state.StoreStateError, match="^store-identity-required$"):
        journal.read_journal(root)
    store_state.prepare_store(root)
    assert not list(root.glob(".cas-journal-migrate.*"))

    published = journal_path.read_bytes()
    object_bytes = {
        path: path.read_bytes() for path in (root / "objects").rglob("*") if path.is_file()
    }
    assert object_bytes
    drift = cas.verify(root)
    assert drift.exit_code == 12
    assert "index missing" in drift.errors
    monkeypatch.setattr(cas, "_rebuild_authority_index", real_rebuild)
    cas.rebuild_index(root)
    repaired = cas.verify(root)
    assert repaired.exit_code == 0, repaired.errors
    assert repaired.errors == ()
    assert journal_path.read_bytes() == published
    assert {
        path: path.read_bytes() for path in (root / "objects").rglob("*") if path.is_file()
    } == object_bytes


_DYNAMIC_IMPORT_PRIMITIVES = frozenset({"__import__", "import_module", "eval", "exec"})
_EXPECTED_MIGRATION_IMPORT_ROOTS = {
    "__future__",
    "castle",
    "collections",
    "dataclasses",
    "hashlib",
    "json",
    "os",
    "pathlib",
    "stat",
    "tempfile",
    "typing",
}


def _analyze_migration_import_boundary(
    source: str,
) -> tuple[set[str], set[str], set[str]]:
    tree = ast.parse(source)
    import_nodes = [
        node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)
    ]
    top_level_import_ids = {
        id(node) for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)
    }
    roots: set[str] = set()
    castle_modules: set[str] = set()
    dynamic_aliases: set[str] = set()
    violations: set[str] = set()
    for node in import_nodes:
        if id(node) not in top_level_import_ids:
            violations.add(f"deferred-import:{node.lineno}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                roots.add(root)
                if root == "importlib":
                    violations.add(f"dynamic-import-provider:{alias.asname or root}")
            continue

        if node.level:
            violations.add(f"relative-import:{node.lineno}")
        if node.module is None:
            continue

        root = node.module.split(".")[0]
        roots.add(root)
        if node.module == "castle":
            castle_modules.update(alias.name for alias in node.names)
        elif node.module.startswith("castle."):
            castle_modules.add(node.module.removeprefix("castle."))
        for alias in node.names:
            local_name = alias.asname or alias.name
            if (node.module == "builtins" and alias.name in {"__import__", "eval", "exec"}) or (
                node.module == "importlib" and alias.name == "import_module"
            ):
                dynamic_aliases.add(local_name)
                violations.add(f"dynamic-import-alias:{local_name}")

    dynamic_names = _DYNAMIC_IMPORT_PRIMITIVES | dynamic_aliases
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in dynamic_names:
            violations.add(f"dynamic-import-reference:{node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _DYNAMIC_IMPORT_PRIMITIVES:
            violations.add(f"dynamic-import-reference:{node.attr}")
        elif isinstance(node, ast.Constant) and node.value in _DYNAMIC_IMPORT_PRIMITIVES:
            violations.add(f"dynamic-import-reference:{node.value}")
    return roots, castle_modules, violations


def _assert_migration_lifecycle(parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uuid

    for phase, code in (("retired", "store-retired"), ("prepared", "store-not-active")):
        rows, payloads = _ordinary_rows()
        root = parent / phase
        journal_path = _write_v1_store(root, rows, payloads)
        digest = _digest_bytes(journal_path.read_bytes())
        if phase == "retired":
            (root / "retired.json").write_bytes(b"malformed fence")
        else:
            previous = store_state.StoreIdentity(
                str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), 0, None
            )
            successor = store_state.StoreIdentity(
                str(uuid.uuid4()), previous.lineage_id, str(uuid.uuid4()), 1, previous.epoch_id
            )
            binding = {
                "operation_id": str(uuid.uuid4()),
                "predecessor": previous.to_dict(),
                "successor": successor.to_dict(),
            }
            (root / "store.json").write_bytes(
                store_state._canonical(
                    store_state._store_metadata(
                        successor, "prepared", binding, "sha256:" + "a" * 64
                    )
                )
            )
        before = _whole_store_snapshot(root)
        reached = []

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            reached.append("legacy authority")
            pytest.fail("migration read legacy authority before lifecycle refusal")

        with monkeypatch.context() as controlled:
            controlled.setattr(journal_migrate_v1, "_read_v1_journal", forbidden)
            with pytest.raises(store_state.StoreStateError, match=f"^{code}$"):
                journal_migrate_v1.migrate_v1_to_v2(root, expect_journal_sha256=digest)
        assert reached == []
        assert _whole_store_snapshot(root) == before
