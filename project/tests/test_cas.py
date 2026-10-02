"""Behavioral tests for ``castle.cas`` — the object layer, index, and verify.

Conventions that hold module-wide, each guarding a criterion rather than a
preference:

* **Every filesystem path derives from ``tmp_path``.** Nothing here creates a
  store, object, index, or materialized file anywhere else. A stray write
  would both leak state and move the candidate identity this phase's evidence
  is bound to.
* **No ``pytest.raises`` is bare.** Every one carries a ``match=`` predicate, so
  a control cannot be silently reassigned to an unrelated refusal that raises
  the same type.
* **The one test that sets the OS immutable flag clears it in teardown.** An
  immutable file left inside a temporary directory defeats cleanup and leaves
  undeletable residue.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from castle import cas, journal

# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------

_MTIME = "2025-12-31T23:59:59Z"

_WORD_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_SHEET_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_ZIP_TYPE = "application/zip"

_IMMUTABLE_SUPPORTED = bool(hasattr(os, "chflags") and getattr(stat, "UF_IMMUTABLE", 0))


@pytest.fixture(autouse=True)
def _clear_immutable_probe_cache() -> Any:
    """The sync-exclusion probe is memoized per store path; isolate each test."""
    cas._store_requires_user_immutable.cache_clear()
    yield
    cas._store_requires_user_immutable.cache_clear()


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _place_object(store: Path, content: bytes, *, mode: int = 0o444) -> str:
    """Hand-place one object at its true path and return its identity."""
    journal.ensure_store(store)
    cas_id = _digest(content)
    path = cas.object_path(store, cas_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(mode)
    return cas_id


def _record(
    cas_id: str,
    at: str,
    *,
    size: int,
    media_type: str = "text/plain",
    source_root_id: str = "root-a",
    original_relpath: str = "docs/a.txt",
    batch: str | None = None,
    outcome: str = "stored",
    event_id: str | None = None,
) -> dict[str, Any]:
    """One valid v2 record, in the contract's field order."""
    return {
        "schema": journal.CAS_JOURNAL_EVENT_SCHEMA,
        "event_id": event_id or journal.new_event_id(),
        "at": at,
        "event": "ingest",
        "cas_id": cas_id,
        "size": size,
        "media_type": media_type,
        "source_root_id": source_root_id,
        "original_relpath": original_relpath,
        "batch": batch,
        "outcome": outcome,
    }


def _write_journal(store: Path, records: list[dict[str, Any]]) -> None:
    """Write the journal through the only sanctioned canonical serializer."""
    store.mkdir(parents=True, exist_ok=True)
    (store / journal.JOURNAL_FILENAME).write_bytes(journal.canonical_journal_bytes(records))


def _two_object_store(store: Path) -> tuple[str, str]:
    """Build a clean two-object store: journal bytes plus sealed object files."""
    content_a = b"alpha payload\n"
    content_b = b"bravo payload\n"
    cas_a = _place_object(store, content_a)
    cas_b = _place_object(store, content_b)
    _write_journal(
        store,
        [
            _record(
                cas_a,
                "2026-01-01T00:00:00Z",
                size=len(content_a),
                original_relpath="docs/alpha.txt",
            ),
            _record(
                cas_b,
                "2026-01-01T00:00:01Z",
                size=len(content_b),
                original_relpath="docs/bravo.txt",
                batch="b1",
            ),
        ],
    )
    return cas_a, cas_b


def _make_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def _child_env() -> dict[str, str]:
    """Let a child import ``castle`` from this deliverable's own location."""
    return {**os.environ, "PYTHONPATH": str(Path(cas.__file__).parents[1])}


# --------------------------------------------------------------------------
# object paths are a pure function of identity
# --------------------------------------------------------------------------

_FIXED_DIGEST = "abcdef0123456789" * 4  # 64 lowercase hex digits


def test_object_path_is_pure_function_of_identity(tmp_path: Path) -> None:
    cas_id = f"sha256:{_FIXED_DIGEST}"
    assert cas.object_relpath(cas_id) == Path("objects") / "sha256" / "ab" / "cd" / _FIXED_DIGEST
    assert cas.object_relpath(cas_id).as_posix() == f"objects/sha256/ab/cd/{_FIXED_DIGEST}"

    store_one = tmp_path / "one"
    store_two = tmp_path / "elsewhere" / "two"
    assert cas.object_path(store_one, cas_id) == store_one / cas.object_relpath(cas_id)
    # The store root is prefixed but never changes the identity-derived tail.
    assert cas.object_path(store_one, cas_id).relative_to(store_one) == cas.object_path(
        store_two, cas_id
    ).relative_to(store_two)


# --------------------------------------------------------------------------
# the file hash is streamed, not slurped, and is emitted prefixed
# --------------------------------------------------------------------------


def _peak_bytes(work: Any) -> int:
    import tracemalloc

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        work()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


# --------------------------------------------------------------------------
# media type classified from bytes; the refinement set is closed
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# the media-detection binding, both halves
# --------------------------------------------------------------------------


def test_media_detection_refuses_forged_proof(tmp_path: Path) -> None:
    store = tmp_path / "store"
    content = b"detectable text payload\n"
    cas_id = _place_object(store, content)
    detection = cas.detect_stored_media(store, cas_id, len(content))
    assert cas._validate_media_detection(detection, cas_id, len(content)) == detection.media_type
    # A proof not produced under the process-local key must not validate. The
    # key is unrecoverable by design, so the bad proof is built directly.
    forged = cas.MediaDetection(cas_id, len(content), detection.media_type, "de" * 32)
    with pytest.raises(cas.CasError, match="not bound to expected CAS content"):
        cas._validate_media_detection(forged, cas_id, len(content))


# --------------------------------------------------------------------------
# the sealing posture, both branches observed
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# sealing is verified, not assumed
# --------------------------------------------------------------------------


def test_seal_refuses_object_that_does_not_match_identity(tmp_path: Path) -> None:
    store = tmp_path / "store"
    journal.ensure_store(store)
    cas_id = _digest(b"the identity these bytes should have")
    wrong = cas.object_path(store, cas_id)
    wrong.parent.mkdir(parents=True, exist_ok=True)
    wrong.write_bytes(b"entirely different bytes")
    wrong.chmod(0o444)
    with pytest.raises(cas.CasError, match=f"existing object is corrupt: {cas_id}"):
        cas.seal_objects(store, [cas_id])


# --------------------------------------------------------------------------
# the journal-to-index fold: admitted refinement vs. incompatible metadata
# --------------------------------------------------------------------------

_FOLD_CAS_ID = f"sha256:{'ab' * 32}"  # a well-formed identity; no object on disk


# --------------------------------------------------------------------------
# the index is reconstructible and survives the file swap
# --------------------------------------------------------------------------

_READBACK_SCRIPT = r"""
import json
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1])
objects = connection.execute(
    "SELECT cas_id, size, media_type, stored_relpath, first_seen_utc, batch "
    "FROM objects ORDER BY cas_id"
).fetchall()
names = connection.execute(
    "SELECT cas_id, source_root_id, original_relpath, recorded_utc "
    "FROM names ORDER BY rowid"
).fetchall()
print(json.dumps({"objects": objects, "names": names}))
"""


def test_index_rebuilds_from_journal_and_object_tree(tmp_path: Path) -> None:
    store = tmp_path / "store"
    content_a = b"alpha payload\n"
    content_b = b"bravo payload\n"
    cas_a = _place_object(store, content_a)
    cas_b = _place_object(store, content_b)
    at_a = "2026-01-01T00:00:00Z"
    at_b = "2026-01-01T00:00:01Z"
    _write_journal(
        store,
        [
            _record(cas_a, at_a, size=len(content_a), original_relpath="docs/alpha.txt"),
            _record(
                cas_b, at_b, size=len(content_b), original_relpath="docs/bravo.txt", batch="b1"
            ),
        ],
    )

    counts = cas.rebuild_index(store)
    assert counts == {"objects": 2, "names": 2, "remotes": 0}

    expected_objects = sorted(
        [
            (cas_a, len(content_a), "text/plain", cas.object_relpath(cas_a).as_posix(), at_a, None),
            (cas_b, len(content_b), "text/plain", cas.object_relpath(cas_b).as_posix(), at_b, "b1"),
        ]
    )
    expected_names = [
        [cas_a, "root-a", "docs/alpha.txt", at_a],
        [cas_b, "root-a", "docs/bravo.txt", at_b],
    ]

    db = store / "cas.sqlite"
    with sqlite3.connect(db) as connection:
        objects = connection.execute(
            "SELECT cas_id, size, media_type, stored_relpath, first_seen_utc, batch "
            "FROM objects ORDER BY cas_id"
        ).fetchall()
        names = connection.execute(
            "SELECT cas_id, source_root_id, original_relpath, recorded_utc "
            "FROM names ORDER BY rowid"
        ).fetchall()
    assert len(objects) == 2
    assert objects == expected_objects
    assert len(names) == 2
    assert [list(row) for row in names] == expected_names

    # Read the rows back in a fresh process — the rename-replace is sound only
    # if a second interpreter sees the same rows.
    result = subprocess.run(
        [sys.executable, "-c", _READBACK_SCRIPT, str(db)],
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, f"readback failed:\n{result.stderr}"
    import json

    seen = json.loads(result.stdout)
    assert seen["objects"] == [list(row) for row in expected_objects]
    assert seen["names"] == expected_names

    # No non-empty stale write-ahead sidecar may survive beside the db.
    wal = store / "cas.sqlite-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    remote = (cas_a, "local-archive", "key", "uploaded", "verified")
    _seed_remotes(store, [remote, remote])
    assert cas.rebuild_index(store) == {"objects": 2, "names": 2, "remotes": 2}
    assert Counter(cas._snapshot_remotes(store)) == Counter([remote, remote])
    assert sorted(_index_object_rows(store)) == expected_objects


# --------------------------------------------------------------------------
# verify distinguishes its four modes
# --------------------------------------------------------------------------


def test_verify_clean_store_exits_zero(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _two_object_store(store)
    cas.rebuild_index(store)
    result = cas.verify(store)
    assert result.exit_code == cas.VERIFY_OK
    assert result.errors == ()
    assert result.ok

    store = tmp_path / "missing-store"
    cas_a, _cas_b = _two_object_store(store)
    cas.rebuild_index(store)
    cas.object_path(store, cas_a).unlink()
    result = cas.verify(store)
    assert result.exit_code == cas.VERIFY_MISSING_OBJECT
    assert any(f"missing object: {cas_a}" in message for message in result.errors)

    store = tmp_path / "drift-store"
    cas_a, _cas_b = _two_object_store(store)
    cas.rebuild_index(store)
    with sqlite3.connect(store / "cas.sqlite") as connection:
        connection.execute("DELETE FROM objects WHERE cas_id = ?", (cas_a,))
        connection.commit()
    result = cas.verify(store)
    assert result.exit_code == cas.VERIFY_INDEX_DRIFT
    assert any("objects table differs from journal" in message for message in result.errors)


# --------------------------------------------------------------------------
# create-on-write retained at the library layer; reads create nothing
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# the under-lock rebuild does not re-enter the lock
# --------------------------------------------------------------------------

_POSITIVE_DEADLINE_S = 30.0


# --------------------------------------------------------------------------
# remotes survive rebuild; an unpreservable index refuses before replace
# --------------------------------------------------------------------------


def _seed_remotes(store: Path, rows: list[tuple[str, str, str, str, str]]) -> None:
    with sqlite3.connect(store / "cas.sqlite") as connection:
        connection.executemany(
            f"INSERT INTO remotes ({cas._REMOTES_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()


# --------------------------------------------------------------------------
# the read surface returns declared shapes and fails closed
# --------------------------------------------------------------------------


def _single_object_store(store: Path, *, extra_names: bool = False) -> str:
    content = b"materializable content\n"
    cas_id = _place_object(store, content)
    records = [_record(cas_id, "2026-01-01T00:00:00Z", size=len(content))]
    if extra_names:
        records = [
            _record(
                cas_id,
                "2026-01-01T00:00:00Z",
                size=len(content),
                original_relpath="one/x.txt",
            ),
            _record(
                cas_id,
                "2026-01-01T00:00:01Z",
                size=len(content),
                original_relpath="two/x.txt",
            ),
        ]
    _write_journal(store, records)
    return cas_id


# --------------------------------------------------------------------------
# import discipline, proven where it can fail
# --------------------------------------------------------------------------


def _cas_module_tree() -> ast.Module:
    return ast.parse(Path(cas.__file__).read_text(encoding="utf-8"))


_MODULE_LOAD_SCRIPT = r"""
import sys

import castle

assert "castle.cas" not in sys.modules, "importing castle eagerly loaded castle.cas"

import castle.cas  # noqa: E402

assert "castle.journal" in sys.modules, "importing castle.cas did not load castle.journal"
sys.exit(0)
"""


# --------------------------------------------------------------------------
# the write path: ingest, dedup, publication, refusals, and receipts
# --------------------------------------------------------------------------


def _write_source(root: Path, relative: str, content: bytes) -> Path:
    """Write one source file under *root*, creating parents, and return it."""
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def _index_object_rows(store: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(store / cas.INDEX_FILENAME) as connection:
        return connection.execute(
            "SELECT cas_id, size, media_type, stored_relpath, first_seen_utc, batch FROM objects"
        ).fetchall()


def _index_name_rows(store: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(store / cas.INDEX_FILENAME) as connection:
        return connection.execute(
            "SELECT cas_id, source_root_id, original_relpath, recorded_utc FROM names"
        ).fetchall()


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Capture every path under *root* with its size and mode, for equality."""
    return {
        str(path.relative_to(root)): (path.lstat().st_size, path.lstat().st_mode)
        for path in sorted(root.rglob("*"))
    }


def _word_package(path: Path) -> Path:
    """A suffixless OOXML word package: a ZIP the byte classifier refines to word."""
    _make_zip(path, {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<w/>"})
    return path


def _rewrite_object_media(store: Path, cas_id: str, media_type: str) -> None:
    """Rewrite one object's media type in both journal and index.

    Simulates a detached archive-typed ingest so that a later media refinement
    has an archive row to advance, the posture the merge caller produces.
    """
    journal_path = store / journal.JOURNAL_FILENAME
    rows = [json.loads(line) for line in journal_path.read_text().splitlines()]
    for row in rows:
        if row["cas_id"] == cas_id:
            row["media_type"] = media_type
    journal_path.write_bytes(journal.canonical_journal_bytes(rows))
    with sqlite3.connect(store / cas.INDEX_FILENAME) as connection:
        connection.execute(
            "UPDATE objects SET media_type = ? WHERE cas_id = ?", (media_type, cas_id)
        )
        connection.commit()


def _seed_nonappendable_journal(store: Path) -> None:
    """Seed a syntactically valid but non-v2 journal tail via a raw write.

    The canonical serializer would refuse this line, so it is written directly.
    The appendability check then raises a journal-content refusal for it.
    """
    journal.ensure_store(store)
    (store / journal.JOURNAL_FILENAME).write_text('{"not": "a v2 record"}\n', encoding="utf-8")


def _seed_malformed_digest_v2_tail(store: Path) -> None:
    """Seed a v2-shaped journal tail whose CAS id is a well-formed but non-hex string.

    The appendability check parses this tail and reaches the shared identity
    validator, which raises the package-root error — neither the object-store
    nor the journal-content child — for a value that is not a real digest.
    """
    journal.ensure_store(store)
    record = {
        "schema": journal.CAS_JOURNAL_EVENT_SCHEMA,
        "event_id": journal.new_event_id(),
        "at": "2026-01-01T00:00:00Z",
        "event": "ingest",
        "cas_id": "sha256:" + "z" * 64,
        "size": 3,
        "media_type": "text/plain",
        "source_root_id": "root",
        "original_relpath": "a.txt",
        "batch": None,
        "outcome": "stored",
    }
    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
    (store / journal.JOURNAL_FILENAME).write_text(line + "\n", encoding="utf-8")


def test_write_path_roundtrips_and_incremental_index_equals_the_fold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    source = tmp_path / "source"
    payloads = {
        "top.txt": b"top level bytes\n",
        "nested/one.txt": b"nested one bytes\n",
        "nested/deep/two.dat": b"\x00\x01\x02 deep binary payload\n",
    }
    for relpath, content in payloads.items():
        _write_source(source, relpath, content)

    import gc
    import shutil as _shutil

    gc.disable()  # an index connection must be closed by the writer, not by garbage collection
    try:
        summary = cas.ingest(store, source, "root-a", batch="wave-1")
        lsof = _shutil.which("lsof")
        held = (
            subprocess.run(
                [lsof, "-p", str(os.getpid())], capture_output=True, text=True, check=False
            ).stdout
            if lsof
            else ""
        )
    finally:
        gc.enable()
    assert str((store / cas.INDEX_FILENAME).resolve()) not in held

    # Exact positive population: a no-op walk fails these before anything else.
    assert summary.files_seen == 3
    assert summary.new_objects == 3
    assert summary.dedup_hits == 0
    assert summary.logical_bytes == sum(len(c) for c in payloads.values())
    assert cas.verify(store).exit_code == cas.VERIFY_OK

    # Rows the incremental writer produced, captured before any rebuild.
    incremental_objects = Counter(_index_object_rows(store))
    incremental_names = Counter(_index_name_rows(store))
    assert incremental_objects and incremental_names  # not vacuously empty

    # A clean verify already proved index/fold agreement; rebuild proves the two
    # writers emit byte-identical rows, not merely mutually consistent ones.
    assert cas.rebuild_index(store) == {"objects": 3, "names": 3, "remotes": 0}
    assert Counter(_index_object_rows(store)) == incremental_objects
    assert Counter(_index_name_rows(store)) == incremental_names

    # Each stored object materializes byte-identical to exactly one source file.
    out = tmp_path / "out"
    out.mkdir()
    materialized = set()
    for receipt in summary.receipts:
        target = cas.materialize(
            store, receipt.cas_id, out / receipt.cas_id.removeprefix("sha256:")
        )
        materialized.add(target.read_bytes())
    assert materialized == set(payloads.values())

    # Returned query results must not leave handles open until garbage collection.
    # Retain strong references so the oracle cannot pass through finalization.
    real_connect = sqlite3.connect
    connections = []

    def track_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    with monkeypatch.context() as controlled:
        controlled.setattr(cas.sqlite3, "connect", track_connection)
        receipt = summary.receipts[0]
        assert cas.names_for(store, receipt.cas_id)
        assert cas.find_names(store, "top.txt")
        assert cas.object_stat(store, receipt.cas_id)["cas_id"] == receipt.cas_id
        assert cas.store_stat(store)["objects"] == 3
        assert cas.rebuild_index(store)["objects"] == 3
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")

    failed_connection = real_connect(":memory:")
    with monkeypatch.context() as controlled:
        controlled.setattr(cas.sqlite3, "connect", lambda *_a, **_kw: failed_connection)
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            cas.names_for(store, receipt.cas_id)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        failed_connection.execute("SELECT 1")

    # Same-volume Linux ingest must copy verified bytes without trying BSD cp.
    real_run = cas.subprocess.run
    commands: list[str] = []

    def observe_command(argv: Any, **kwargs: Any) -> Any:
        commands.append(str(argv[0]))
        assert Path(argv[0]).name != "cp", "non-Darwin ingest launched cp"
        return real_run(argv, **kwargs)

    linux_store = tmp_path / "linux-store"
    with monkeypatch.context() as controlled:
        controlled.setattr(cas.sys, "platform", "linux")
        controlled.setattr(cas, "_same_volume", lambda *_args: True)
        controlled.setattr(cas.subprocess, "run", observe_command)
        assert cas._clone_file(source / "top.txt", tmp_path / "unavailable") is False
        assert commands == []
        copied = cas.ingest(linux_store, source, "linux-root")
    assert copied.new_objects == 3 and not copied.refusals
    assert {receipt.cas_id for receipt in copied.receipts} == {
        _digest(content) for content in payloads.values()
    }
    for content in payloads.values():
        expected_id = _digest(content)
        assert cas.object_path(linux_store, expected_id).read_bytes() == content
        assert (
            cas.materialize(linux_store, expected_id, out / expected_id[7:]).read_bytes() == content
        )
    assert cas.verify(linux_store).exit_code == 0

    # Controlled Darwin success and native failure preserve both helper outcomes.
    clone_commands: list[list[str]] = []

    def clone_command(argv: list[str], **kwargs: Any) -> None:
        clone_commands.append(argv)
        assert kwargs == {"check": True, "capture_output": True, "timeout": 120}
        Path(argv[3]).write_bytes(Path(argv[2]).read_bytes())

    cloned = tmp_path / "controlled-clone"
    with monkeypatch.context() as controlled:
        controlled.setattr(cas.sys, "platform", "darwin")
        controlled.setattr(cas.subprocess, "run", clone_command)
        assert cas._clone_file(source / "top.txt", cloned) is True
        assert cloned.read_bytes() == payloads["top.txt"]
        assert clone_commands == [["cp", "-c", str(source / "top.txt"), str(cloned)]]

        def refuse_clone(*_args: Any, **_kwargs: Any) -> None:
            raise subprocess.CalledProcessError(1, "cp")

        controlled.setattr(cas.subprocess, "run", refuse_clone)
        assert cas._clone_file(source / "top.txt", cloned) is False


def _journal_sync_failure_preserves_content(parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real post-write journal fsync failure must not delete referenced bytes."""
    import errno

    for code in (errno.ENOSPC, errno.EIO):
        case = parent / str(code)
        store = case / "store"
        baseline = _write_source(case / "source", "base.txt", b"baseline bytes\n")
        base_receipt = cas.ingest_file(store, baseline, "base")
        remote = (base_receipt.cas_id, "archive", "key", "uploaded", "verified")
        _seed_remotes(store, [remote, remote])
        source = _write_source(case / "source", "new.txt", b"preserve uncertain append\n")
        expected = _digest(source.read_bytes())
        log = store / journal.JOURNAL_FILENAME
        before = log.read_bytes()
        identity = (log.stat().st_dev, log.stat().st_ino)
        real_sync = os.fsync
        calls = []

        def fail_journal_sync(descriptor: int) -> None:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) == identity and info.st_size > len(before):
                calls.append(log.read_bytes())
                raise OSError(code, "injected uncertain journal sync")
            real_sync(descriptor)

        with monkeypatch.context() as controlled:
            controlled.setattr(os, "fsync", fail_journal_sync)
            with pytest.raises(OSError, match="injected uncertain journal sync") as failure:
                cas.ingest_file(store, source, "new")
        assert failure.value.errno == code
        assert len(calls) == 1
        records = journal.read_journal(store)
        assert len(records) == 2 and records[-1]["cas_id"] == expected
        assert log.read_bytes().startswith(before)
        assert cas.object_path(store, expected).read_bytes() == source.read_bytes()
        assert cas.verify(store).exit_code == cas.VERIFY_INDEX_DRIFT
        with pytest.raises(cas.CasError, match="required before ingest"):
            cas.ingest_file(store, source, "new")
        assert len(journal.read_journal(store)) == 2
        with monkeypatch.context() as controlled:
            controlled.setattr(os, "fsync", fail_journal_sync)
            with pytest.raises(OSError, match="injected uncertain journal sync"):
                cas.rebuild_index(store)
        assert cas.verify(store).exit_code == cas.VERIFY_INDEX_DRIFT
        assert cas.rebuild_index(store) == {"objects": 2, "names": 2, "remotes": 2}
        assert Counter(cas._snapshot_remotes(store)) == Counter([remote, remote])
        assert cas.verify(store).ok
        retry = cas.ingest_file(store, source, "new")
        assert retry.outcome == "dedup"
        assert retry.object_first_seen_utc == records[-1]["at"]
        assert retry.first_sighting.recorded_utc == records[-1]["at"]
        assert len(journal.read_journal(store)) == 3 and cas.verify(store).ok
        # Cache reconstruction cannot restore authoritative bytes. Admission
        # refuses even after a rebuild whose row-count result is not health.
        cas.object_path(store, expected).unlink()
        cas.rebuild_index(store)
        assert cas.verify(store).exit_code == cas.VERIFY_MISSING_OBJECT
        with pytest.raises(cas.CasError, match="journaled object missing"):
            cas.ingest_file(store, source, "new")

    # A failed duplicate sighting can leave the folded rows unchanged. A clean
    # verify result then says nothing about whether those visible bytes synced.
    case = parent / "duplicate"
    store = case / "store"
    source = _write_source(case / "source", "same.txt", b"same sighting\n")
    baseline = cas.ingest_file(store, source, "same")
    log = store / journal.JOURNAL_FILENAME
    before = log.read_bytes()
    identity = (log.stat().st_dev, log.stat().st_ino)
    real_sync = os.fsync
    hits = []

    def fail_duplicate_sync(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == identity and info.st_size > len(before):
            hits.append(True)
            raise OSError(errno.EIO, "injected duplicate journal sync")
        real_sync(descriptor)

    with monkeypatch.context() as controlled:
        controlled.setattr(os, "fsync", fail_duplicate_sync)
        with pytest.raises(OSError, match="injected duplicate journal sync"):
            cas.ingest_file(store, source, "same")
        assert len(journal.read_journal(store)) == 2
        assert cas.verify(store).ok
        uncertain = log.read_bytes()
        with pytest.raises(OSError, match="injected duplicate journal sync"):
            cas.ingest_file(store, source, "same")
        assert log.read_bytes() == uncertain
    assert hits == [True, True]
    retry = cas.ingest_file(store, source, "same")
    assert retry.outcome == "dedup"
    assert retry.object_first_seen_utc == baseline.object_first_seen_utc
    assert retry.first_sighting == baseline.first_sighting
    assert len(journal.read_journal(store)) == 3 and cas.verify(store).ok


def _journal_stream_failure_preserves_evidence(
    parent: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise real buffered writes/close; do not replace append_record."""
    import errno

    for mode in ("zero", "partial", "flush-before", "flush-after"):
        case = parent / mode
        store = case / "store"
        baseline = _write_source(case / "source", "base.txt", b"base\n")
        cas.ingest_file(store, baseline, "base")
        source = _write_source(case / "source", "new.txt", b"uncertain stream\n")
        expected = _digest(source.read_bytes())
        log = store / journal.JOURNAL_FILENAME
        before = log.read_bytes()
        real_open = Path.open
        injections = []

        class Stream:
            def __init__(self, handle: Any) -> None:
                self.handle = handle
                self.touched = False

            def __getattr__(self, name: str) -> Any:
                return getattr(self.handle, name)

            def __enter__(self) -> Any:
                self.handle.__enter__()
                return self

            def __exit__(self, *args: Any) -> Any:
                return self.handle.__exit__(*args)

            def write(self, value: str) -> int:
                self.touched = True
                if mode in ("zero", "partial"):
                    if mode == "partial":
                        self.handle.write(value[: len(value) // 2])
                        self.handle.flush()
                    injections.append(mode)
                    raise OSError(errno.EIO, "injected append stream")
                return self.handle.write(value)

            def flush(self) -> None:
                if self.touched and mode.startswith("flush"):
                    if mode == "flush-after":
                        self.handle.flush()
                    injections.append(mode)
                    raise OSError(errno.EIO, "injected append stream")
                self.handle.flush()

        def open_stream(path: Path, *args: Any, **kwargs: Any) -> Any:
            handle = real_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            return Stream(handle) if path == log and mode == "a+" else handle

        with monkeypatch.context() as controlled:
            controlled.setattr(Path, "open", open_stream)
            with pytest.raises(OSError, match="injected append stream"):
                cas.ingest_file(store, source, "new")
        assert injections == [mode]
        assert cas.object_path(store, expected).read_bytes() == source.read_bytes()
        after = log.read_bytes()
        assert after.startswith(before)
        assert after == before if mode == "zero" else len(after) > len(before)
        with pytest.raises(journal.CastleError, match="journal|ingest"):
            cas.ingest_file(store, source, "new")
        assert log.read_bytes() == after
        if mode in ("zero", "partial"):
            with pytest.raises(journal.CastleError, match="journal|paths"):
                cas.rebuild_index(store)
            assert log.read_bytes() == after
            assert cas.object_path(store, expected).read_bytes() == source.read_bytes()
        else:
            assert len(journal.read_journal(store)) == 2
            cas.rebuild_index(store)
            assert cas.verify(store).ok
            assert cas.ingest_file(store, source, "new").outcome == "dedup"

    case = parent / "preappend"
    store = case / "store"
    baseline = _write_source(case / "source", "base.txt", b"base\n")
    cas.ingest_file(store, baseline, "base")
    source = _write_source(case / "source", "new.txt", b"preappend cleanup\n")
    expected = _digest(source.read_bytes())
    before = (store / journal.JOURNAL_FILENAME).read_bytes()
    real_sync = os.fsync
    hit = []

    def fail_object_directory(descriptor: int) -> None:
        info = os.fstat(descriptor)
        if stat.S_ISDIR(info.st_mode) and cas.object_path(store, expected).exists():
            hit.append(True)
            raise OSError(errno.EIO, "injected preappend directory sync")
        real_sync(descriptor)

    real_unlink = Path.unlink
    cleanup_checks = []

    def check_cleanup_lock(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == cas.object_path(store, expected):
            # Independent process and actual flock: ownership cannot change
            # through a cooperating merge before preappend deletion completes.
            script = """import fcntl, sys
with open(sys.argv[1], "r+") as handle:
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
sys.exit(1)
"""
            checked = subprocess.run(
                [sys.executable, "-c", script, str(store / journal.JOURNAL_LOCK_FILENAME)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert checked.returncode == 0, checked.stderr or "cleanup released journal lock"
            cleanup_checks.append(True)
        real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as controlled:
        controlled.setattr(os, "fsync", fail_object_directory)
        controlled.setattr(Path, "unlink", check_cleanup_lock)
        with pytest.raises(OSError, match="injected preappend directory sync"):
            cas.ingest_file(store, source, "new")
    assert hit == cleanup_checks == [True]
    assert not cas.object_path(store, expected).exists()
    assert not list(store.rglob(".cas-*.tmp"))
    assert (store / journal.JOURNAL_FILENAME).read_bytes() == before
    assert cas.verify(store).ok

    if _IMMUTABLE_SUPPORTED:
        for boundary in ("object-sync", "directory-sync", "journal-sync"):
            case = parent / ("immutable-" + boundary)
            store = case / "store"
            journal.ensure_store(store)
            cas._run_xattr(["-w", "com.dropbox.ignored", "1", str(store)])
            baseline = _write_source(case / "source", "base.txt", b"sealed base\n")
            source = _write_source(case / "source", "new.txt", b"sealed new\n")
            target = cas.object_path(store, _digest(source.read_bytes()))
            log = store / journal.JOURNAL_FILENAME
            try:
                base_receipt = cas.ingest_file(store, baseline, "base")
                assert (
                    cas.object_path(store, base_receipt.cas_id).stat().st_flags & stat.UF_IMMUTABLE
                )
                before = log.read_bytes()
                identity = (log.stat().st_dev, log.stat().st_ino)
                real_sync = os.fsync
                hits = []

                def fail_immutable_sync(descriptor: int) -> None:
                    info = os.fstat(descriptor)
                    target_info = target.stat() if target.exists() else None
                    is_target = target_info is not None and (info.st_dev, info.st_ino) == (
                        target_info.st_dev,
                        target_info.st_ino,
                    )
                    fail = (
                        (boundary == "object-sync" and is_target)
                        or (
                            boundary == "directory-sync"
                            and stat.S_ISDIR(info.st_mode)
                            and target.exists()
                        )
                        or (
                            boundary == "journal-sync"
                            and (info.st_dev, info.st_ino) == identity
                            and info.st_size > len(before)
                        )
                    )
                    if fail:
                        hits.append(True)
                        assert target.stat().st_flags & stat.UF_IMMUTABLE
                        raise OSError(errno.EIO, "injected immutable " + boundary)
                    real_sync(descriptor)

                with monkeypatch.context() as controlled:
                    controlled.setattr(os, "fsync", fail_immutable_sync)
                    with pytest.raises(OSError, match="injected immutable " + boundary) as failure:
                        cas.ingest_file(store, source, "new")
                assert failure.value.errno == errno.EIO and hits == [True]
                if boundary == "journal-sync":
                    assert target.read_bytes() == source.read_bytes()
                    assert target.stat().st_flags & stat.UF_IMMUTABLE
                    assert len(journal.read_journal(store)) == 2
                    cas.rebuild_index(store)
                    assert cas.ingest_file(store, source, "new").outcome == "dedup"
                else:
                    assert not target.exists()
                    assert log.read_bytes() == before
                    assert cas.verify(store).ok
                    assert cas.ingest_file(store, source, "new").outcome == "stored"
                assert cas.verify(store).ok
            finally:
                # Only flags on this test's disposable store are cleared.
                for path in store.rglob("*"):
                    if path.is_file():
                        flags = path.stat().st_flags
                        if flags & stat.UF_IMMUTABLE:
                            os.chflags(path, flags & ~stat.UF_IMMUTABLE)


def _journal_interrupted_child_requires_reconciliation(parent: Path) -> None:
    """Process exit after real append is not a power-loss durability proof."""
    source = _write_source(parent / "source", "new.txt", b"child uncertain append\n")
    store = parent / "store"
    journal.ensure_store(store)
    script = r"""import os, sys
from pathlib import Path
from castle import cas, journal
store, source = map(Path, sys.argv[1:])
path = store / journal.JOURNAL_FILENAME
identity = (path.stat().st_dev, path.stat().st_ino)
real = os.fsync
def die(fd):
    info = os.fstat(fd)
    if (info.st_dev, info.st_ino) == identity and info.st_size:
        os._exit(77)
    real(fd)
os.fsync = die
cas.ingest_file(store, source, "child")
raise AssertionError("receipt returned")
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(store), str(source)],
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 77, child.stderr
    expected = _digest(source.read_bytes())
    assert cas.object_path(store, expected).read_bytes() == source.read_bytes()
    assert len(journal.read_journal(store)) == 1
    with pytest.raises(cas.CasError, match="required before ingest"):
        cas.ingest_file(store, source, "child")
    cas.rebuild_index(store)
    assert cas.verify(store).ok
    assert cas.ingest_file(store, source, "child").outcome == "dedup"


def test_publication_is_atomic_write_if_absent_resealed_no_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path / "store"
    source = tmp_path / "source"
    _write_source(source, "doc.txt", b"atomic publication bytes\n")

    # Behavior (1) — write-if-absent by rename. Spy the publish primitive: an
    # atomic write-if-absent publishes by renaming a staging temp into place,
    # never by writing the destination directly. A direct-to-destination
    # reduction never calls os.rename and fails here.
    renames: list[tuple[str, str]] = []
    real_rename = cas.os.rename

    def spy_rename(src: Any, dst: Any) -> None:
        renames.append((str(src), str(dst)))
        real_rename(src, dst)

    monkeypatch.setattr(cas.os, "rename", spy_rename)

    # Behavior (3) — immutable publication by post-rename reseal. Hand the write
    # path a temp left writable, so the published object's read-only posture can
    # come ONLY from the post-rename reseal, never from staging's own chmod.
    # Remove the reseal and the object stays 0o644, the write path refuses, and
    # new_objects falls to zero — this test then goes red. This proves the reseal
    # behavior, not merely the final mode a bare staging chmod would also produce.
    real_stage = cas._stage_object

    def writable_stage(src: Any, dst: Any, cid: str) -> Path:
        temp = real_stage(src, dst, cid)
        temp.chmod(0o644)
        return temp

    monkeypatch.setattr(cas, "_stage_object", writable_stage)

    summary = cas.ingest(store, source, "root-a")
    assert not summary.refusals
    assert summary.new_objects == 1
    cas_id = summary.receipts[0].cas_id
    stored = cas.object_path(store, cas_id)
    # Exactly one publish into the object's final path, and it renamed a staging
    # temp — not a direct write — into place.
    publish_renames = [r for r in renames if r[1] == str(stored)]
    assert len(publish_renames) == 1
    staged_src = Path(publish_renames[0][0])
    assert staged_src.name.startswith(".cas-") and staged_src.suffix == ".tmp"
    # The reseal restored the sealed posture the writable stage deliberately removed.
    assert stat.S_IMODE(stored.lstat().st_mode) == 0o444
    assert cas._write_protected(stored.lstat())
    first_seen = summary.receipts[0].object_first_seen_utc
    before = stored.lstat()
    before_bytes = stored.read_bytes()

    # Behavior (2) — overwrite-on-present is excluded. Re-ingesting identical
    # bytes dedups without republishing. An in-place overwrite would truncate and
    # rewrite the existing inode (moving mtime) after first clearing its
    # read-only protection (moving ctime), then reseal — adding no rename and
    # changing no inode, so st_ino alone cannot see it. mtime, ctime, and the
    # byte content each can, and each fails such a reduction independently.
    again = cas.ingest(store, source, "root-b")
    assert again.new_objects == 0 and again.dedup_hits == 1
    assert again.receipts[0].object_first_seen_utc == first_seen
    assert [r for r in renames if r[1] == str(stored)] == publish_renames
    after = stored.lstat()
    assert after.st_ino == before.st_ino
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ctime_ns == before.st_ctime_ns
    assert stored.read_bytes() == before_bytes
    assert stat.S_IMODE(after.st_mode) == 0o444
    assert cas._write_protected(after)

    # No staging temporary survived; the tree inventory in rebuild does not trip.
    assert list((store / "objects").rglob(".cas-*.tmp")) == []
    assert cas.rebuild_index(store) == {"objects": 1, "names": 2, "remotes": 0}

    corrupt_source = _write_source(source, "corrupt.txt", b"expected copy bytes\n")
    expected_id = _digest(b"expected copy bytes\n")
    corrupt_store = tmp_path / "corrupt-store"
    copies: list[Path] = []

    def corrupt_copy(_source: Path, destination: Path) -> None:
        copies.append(destination)
        destination.write_bytes(b"wrong copy bytes\n")

    with monkeypatch.context() as controlled:
        controlled.setattr(cas.sys, "platform", "linux")
        controlled.setattr(cas, "_same_volume", lambda *_args: True)
        controlled.setattr(cas.shutil, "copyfile", corrupt_copy)
        with pytest.raises(cas.CasError, match="^copy verification failed for corrupt.txt$"):
            cas.ingest_file(corrupt_store, corrupt_source, "corrupt-root")
    assert len(copies) == 1
    assert not cas.object_path(corrupt_store, expected_id).exists()
    assert not copies[0].exists()
    assert list(corrupt_store.rglob(".cas-*.tmp")) == []

    _journal_sync_failure_preserves_content(tmp_path / "journal-errors", monkeypatch)
    _journal_stream_failure_preserves_evidence(tmp_path / "stream-errors", monkeypatch)
    _journal_interrupted_child_requires_reconciliation(tmp_path / "child-error")


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("empty_root_id", "opaque label"),
        ("absolute_root_id", "opaque label"),
        ("newline_root_id", "opaque label"),
        ("source_symlink", "source must be a directory or a regular file"),
        ("non_disjoint", "disjoint"),
        ("file_not_regular", "not a regular file"),
        ("file_broken_symlink", "not a regular file"),
        ("relpath_absolute", "safe relative path"),
        ("relpath_dotdot", "safe relative path"),
    ],
)
def test_ingest_refusal_matrix(tmp_path: Path, case: str, match: str) -> None:
    store = tmp_path / "store"
    source = tmp_path / "source"
    regular = _write_source(source, "a.txt", b"content\n")

    if case == "empty_root_id":
        with pytest.raises(cas.CasError, match=match):
            cas.ingest(store, source, "   ")
    elif case == "absolute_root_id":
        with pytest.raises(cas.CasError, match=match):
            cas.ingest(store, source, "/abs/root")
    elif case == "newline_root_id":
        with pytest.raises(cas.CasError, match=match):
            cas.ingest(store, source, "root\nid")
    elif case == "source_symlink":
        # A symlink is refused on the path as given. The check must precede
        # resolution, which would follow the link and leave nothing to detect.
        link = tmp_path / "link-to-source"
        link.symlink_to(source)
        with pytest.raises(cas.CasError, match=match):
            cas.ingest(store, link, "root")
    elif case == "non_disjoint":
        with pytest.raises(cas.CasError, match=match):
            cas.ingest(source / "inner-store", source, "root")
    elif case == "file_not_regular":
        with pytest.raises(cas.CasError, match=match):
            cas.ingest_file(store, source, "root")
    elif case == "file_broken_symlink":
        link = tmp_path / "dangling.txt"
        link.symlink_to(tmp_path / "nonexistent-target")
        with pytest.raises(cas.CasError, match=match):
            cas.ingest_file(store, link, "root")
    elif case == "relpath_absolute":
        with pytest.raises(cas.CasError, match=match):
            cas.ingest_file(store, regular, "root", original_relpath="/abs/name")
    elif case == "relpath_dotdot":
        with pytest.raises(cas.CasError, match=match):
            cas.ingest_file(store, regular, "root", original_relpath="../escape")


def _excision_fixture(parent: Path) -> tuple[Path, Path, str, str, Any, str]:
    import gc
    import uuid

    from castle import store_state

    parent.mkdir(parents=True, exist_ok=True)
    source, destination = parent / "old", parent / "new"
    removed, retained = _two_object_store(source)
    cas.rebuild_index(source)
    gc.collect()
    return (
        source,
        destination,
        removed,
        retained,
        store_state.read_store_identity(source),
        str(uuid.uuid4()),
    )


def _full_snapshot(root: Path) -> dict[str, tuple[int, bytes | str]]:
    import gc

    gc.collect()
    if not root.exists():
        return {}
    result = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        payload = (
            os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else b""
        )
        result[path.relative_to(root).as_posix()] = (info.st_mode, payload)
    return result


def _stop_publication(monkeypatch: pytest.MonkeyPatch, label: str, when: str) -> list[str]:
    from castle import store_state

    real = store_state._publish_metadata
    reached = []

    def publish(path: Path, value: Any, **kwargs: Any) -> None:
        current = path.name + ":" + value.get("state", "selection")
        if current == label and when == "before":
            reached.append(current)
            raise OSError("injected publication fault")
        real(path, value, **kwargs)
        if current == label and when == "after":
            reached.append(current)
            raise OSError("injected publication fault")

    monkeypatch.setattr(store_state, "_publish_metadata", publish)
    return reached


def test_excision_selection_refuses_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from castle import excision

    # Reproduce removal after directory enumeration, before lstat. Only
    # SQLite's root-level volatile files may disappear without invalidating
    # authority; present unsafe entries and missing durable files still refuse.
    for site, name in (
        ("inventory", "cas.sqlite-wal"),
        ("inventory", "cas.sqlite-shm"),
        ("inventory", "cas.sqlite-journal"),
        ("inventory", "journal.jsonl"),
        ("inventory", "object"),
        ("removal", "cas.sqlite-wal"),
        ("removal", "cas.sqlite-shm"),
        ("removal", "cas.sqlite-journal"),
    ):
        source, destination, removed, retained, identity, operation = _excision_fixture(
            tmp_path / f"vanishing-{site}-{name}"
        )
        disappearing = cas.object_path(source, retained) if name == "object" else source / name
        if name.startswith("cas.sqlite-"):
            disappearing.write_bytes(b"")
        real_lstat = Path.lstat
        real_inventory = cas._authority_paths
        real_remove = excision._remove_file
        enumerating = []
        vanished = []

        def vanish(path: Path, *args: Any, **kwargs: Any) -> Any:
            if site == "inventory" and enumerating and path == disappearing and not vanished:
                disappearing.unlink()
                vanished.append(path)
            return real_lstat(path, *args, **kwargs)

        def inventory(root: Path) -> Any:
            enumerating.append(root)
            try:
                return real_inventory(root)
            finally:
                enumerating.pop()

        def remove(path: Path, **kwargs: Any) -> None:
            if site == "removal" and path == disappearing and not vanished:
                disappearing.unlink()
                vanished.append(path)
            real_remove(path, **kwargs)

        with monkeypatch.context() as local:
            local.setattr(Path, "lstat", vanish)
            local.setattr(cas, "_authority_paths", inventory)
            local.setattr(excision, "_remove_file", remove)
            if name.startswith("cas.sqlite-"):
                excision.excise_store(
                    source,
                    destination,
                    cas_ids=[removed],
                    operation_id=operation,
                    expected_source=identity,
                )
                assert cas.require_object(destination, retained).read_bytes()
            else:
                with pytest.raises(excision.ExcisionError, match="excision-root-unsafe"):
                    excision.excise_store(
                        source,
                        destination,
                        cas_ids=[removed],
                        operation_id=operation,
                        expected_source=identity,
                    )
                assert not destination.exists()
        assert vanished == [disappearing]

    cases = [
        ("empty", "excision-request-invalid"),
        ("string", "excision-request-invalid"),
        ("bytes", "excision-request-invalid"),
        ("member", "excision-request-invalid"),
        ("uppercase", "excision-request-invalid"),
        ("bare", "excision-request-invalid"),
        ("unknown", "excision-selection-unknown"),
        ("mixed", "excision-selection-unknown"),
        ("object-only", "excision-source-inconsistent"),
        ("journal-only", "excision-source-inconsistent"),
        ("size", "excision-source-inconsistent"),
        ("outcome-list", "excision-source-inconsistent"),
        ("outcome-dict", "excision-source-inconsistent"),
        ("corrupt", "excision-source-inconsistent"),
        ("remotes", "excision-remotes-unreadable"),
        ("dangling", "excision-source-inconsistent"),
        ("remote-type", "excision-source-inconsistent"),
        ("operation", "excision-request-invalid"),
        ("expected", "excision-request-invalid"),
        ("identity", "excision-operation-mismatch"),
    ]
    for kind, code in cases:
        source, destination, removed, retained, identity, operation = _excision_fixture(
            tmp_path / kind
        )
        selected: Any = [removed]
        expected = identity
        if kind == "empty":
            selected = []
        elif kind == "string":
            selected = removed
        elif kind == "bytes":
            selected = removed.encode()
        elif kind == "member":
            selected = [42]
        elif kind == "uppercase":
            selected = [removed.upper()]
        elif kind == "bare":
            selected = [removed[7:]]
        elif kind == "unknown":
            selected = ["sha256:" + "f" * 64]
        elif kind == "mixed":
            selected = [removed, "sha256:" + "f" * 64]
        elif kind == "object-only":
            _place_object(source, b"unjournaled")
            selected = ["sha256:" + "f" * 64]
        elif kind == "journal-only":
            cas.object_path(source, retained).unlink()
            selected = ["sha256:" + "f" * 64]
        elif kind == "size":
            records = journal.read_journal(source)
            records[0]["size"] += 1
            _write_journal(source, records)
        elif kind in ("outcome-list", "outcome-dict"):
            records = journal.read_journal(source)
            records[0]["outcome"] = [] if kind == "outcome-list" else {}
            _write_journal(source, records)
        elif kind == "corrupt":
            target = cas.object_path(source, removed)
            target.chmod(0o644)
            target.write_bytes(b"corrupt bytes\n")
            target.chmod(0o444)
        elif kind == "remotes":
            (source / "cas.sqlite").write_bytes(b"unreadable private SQL")
        elif kind in ("dangling", "remote-type"):
            from contextlib import closing

            with closing(sqlite3.connect(source / "cas.sqlite")) as db:
                db.execute(
                    "INSERT INTO remotes VALUES (?, ?, ?, ?, ?)",
                    (
                        "sha256:" + "f" * 64 if kind == "dangling" else retained,
                        "remote",
                        b"not text" if kind == "remote-type" else "key",
                        "at",
                        "checked",
                    ),
                )
                db.commit()
        elif kind == "operation":
            operation = "not a UUID"
        elif kind == "expected":
            expected = identity.to_dict()
        elif kind == "identity":
            from dataclasses import replace

            expected = replace(identity, store_id="77777777-7777-4777-8777-777777777777")
        before = _full_snapshot(source)
        with pytest.raises(excision.ExcisionError, match=f"^{code}$") as caught:
            excision.excise_store(
                source,
                destination,
                cas_ids=selected,
                operation_id=operation,
                expected_source=expected,
            )
        assert str(caught.value) == code, kind
        assert caught.value.__cause__ is None, kind
        assert caught.value.__context__ is None or caught.value.__suppress_context__, kind
        assert _full_snapshot(source) == before, kind
        assert not destination.exists(), kind
    source, destination, removed, _retained, identity, operation = _excision_fixture(
        tmp_path / "duplicates"
    )
    result = excision.excise_store(
        source,
        destination,
        cas_ids=[removed, removed],
        operation_id=operation,
        expected_source=identity,
    )
    assert result.successor.generation == 1
    for dangling in (False, True):
        source, destination, removed, retained, identity, operation = _excision_fixture(
            tmp_path / f"persisted-wal-{dangling}"
        )
        remote_id = "sha256:" + "f" * 64 if dangling else retained
        row = (remote_id, "wal-only", "key", "uploaded", "verified")
        script = """
import json
import os
import sqlite3
import sys
db = sqlite3.connect(sys.argv[1])
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA wal_autocheckpoint=0")
db.executemany("INSERT INTO remotes VALUES (?, ?, ?, ?, ?)", [json.loads(sys.argv[2])] * 2)
db.commit()
os._exit(0)
"""
        completed = subprocess.run(
            [sys.executable, "-c", script, str(source / "cas.sqlite"), json.dumps(row)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        wal = source / "cas.sqlite-wal"
        assert b"wal-only" in wal.read_bytes()
        assert b"wal-only" not in (source / "cas.sqlite").read_bytes()
        # The process exited without closing SQLite. Reconstruct the volatile
        # index from committed WAL, including a torn trailing frame, without SHM.
        (source / "cas.sqlite-shm").unlink()
        with wal.open("ab") as handle:
            handle.write(b"\0" * 24)
        before = _full_snapshot(source)
        assert Counter(cas._snapshot_remotes(source)) == Counter([row, row])
        objects, names = cas.expected_index_rows(journal.read_journal(source))
        assert cas._index_errors(source, objects, names) == []
        assert _full_snapshot(source) == before
        if dangling:
            with pytest.raises(excision.ExcisionError, match="^excision-source-inconsistent$"):
                excision.excise_store(
                    source,
                    destination,
                    cas_ids=[removed],
                    operation_id=operation,
                    expected_source=identity,
                )
            assert _full_snapshot(source) == before
            assert not destination.exists()
        else:
            excision.excise_store(
                source,
                destination,
                cas_ids=[removed],
                operation_id=operation,
                expected_source=identity,
            )
            assert Counter(cas._snapshot_remotes(destination)) == Counter([row, row])
    # Root safety wins even over an invalid request.
    linked = tmp_path / "linked"
    linked.symlink_to(destination, target_is_directory=True)
    with pytest.raises(excision.ExcisionError, match="^excision-root-unsafe$"):
        excision.excise_store(
            linked, tmp_path / "other", cas_ids=[], operation_id="bad", expected_source=None
        )


def test_excision_reconstructs_exact_retained_authority(tmp_path: Path) -> None:
    import uuid
    from contextlib import closing

    from castle import excision, store_state

    for indexed, all_selected in ((True, False), (False, False), (True, True)):
        root = tmp_path / f"{indexed}-{all_selected}"
        source, destination, removed, retained, identity, operation = _excision_fixture(root)
        removed_bytes = cas.object_path(source, removed).read_bytes()
        original = journal.read_journal(source)
        cas.object_path(source, retained).unlink()
        retained_bytes = b"retained text embeds " + removed_bytes + b"as an innocent quotation\n"
        retained = _place_object(source, retained_bytes)
        original[1].update(cas_id=retained, size=len(retained_bytes))
        assert removed_bytes in retained_bytes
        original[1]["media_type"] = _ZIP_TYPE
        original.extend(
            [
                {
                    **original[0],
                    "event_id": "uuid:" + str(uuid.uuid4()),
                    "at": "2026-01-01T00:00:02Z",
                    "outcome": "dedup",
                },
                {
                    **original[1],
                    "event_id": "uuid:" + str(uuid.uuid4()),
                    "at": "2026-01-01T00:00:03Z",
                    "media_type": _WORD_TYPE,
                    "outcome": "dedup",
                },
                {
                    **original[1],
                    "event_id": "uuid:" + str(uuid.uuid4()),
                    "at": "2026-01-01T00:00:04Z",
                    "media_type": _WORD_TYPE,
                    "original_relpath": "retained/alpha.txt",
                    "outcome": "dedup",
                },
            ]
        )
        _write_journal(source, original)
        cas.rebuild_index(source)
        retained_remote = (retained, "remote", "retained-key", "uploaded", "verified")
        removed_remote = (removed, "remote", "removed-only-remote-token", "uploaded", "verified")
        with closing(sqlite3.connect(source / "cas.sqlite")) as db:
            db.executemany(
                "INSERT INTO remotes VALUES (?, ?, ?, ?, ?)",
                [retained_remote] * 2 + [removed_remote],
            )
            db.execute("PRAGMA secure_delete=OFF")
            db.execute("CREATE TABLE residual(marker TEXT)")
            db.execute("INSERT INTO residual VALUES ('removed-only-database-residue-token')")
            db.commit()
            db.execute("DELETE FROM residual")
            db.commit()
        assert b"removed-only-database-residue-token" in (source / "cas.sqlite").read_bytes()
        if not indexed:
            (source / "cas.sqlite").unlink()
        selected = [removed, retained] if all_selected else [removed]
        expected_records = (
            [] if all_selected else [row for row in original if row["cas_id"] == retained]
        )
        result = excision.excise_store(
            source, destination, cas_ids=selected, operation_id=operation, expected_source=identity
        )
        assert result.predecessor == identity
        assert result.successor.generation == identity.generation + 1
        assert result.successor.lineage_id == identity.lineage_id
        assert result.successor.epoch_id != identity.epoch_id
        assert result.successor.store_id != identity.store_id
        assert result.successor.predecessor_epoch_id == identity.epoch_id
        assert {p.name for p in source.iterdir()} == {"retired.json", "journal.lock"}
        assert journal.read_journal(destination) == expected_records
        assert (destination / "journal.jsonl").read_bytes() == journal.canonical_journal_bytes(
            expected_records
        )
        actual_ids = {
            "sha256:" + p.name for p in (destination / "objects").rglob("*") if p.is_file()
        }
        assert actual_ids == (set() if all_selected else {retained})
        if not all_selected:
            assert cas.require_object(destination, retained).read_bytes() == retained_bytes
            # Same selected-looking text is an innocent retained name, never a selection rule.
            assert any(row["original_relpath"] == "retained/alpha.txt" for row in expected_records)
        assert not cas.object_path(destination, removed).exists()
        with closing(sqlite3.connect(destination / "cas.sqlite")) as db:
            object_rows = db.execute("SELECT * FROM objects").fetchall()
            name_rows = db.execute("SELECT * FROM names").fetchall()
            remote_rows = db.execute("SELECT * FROM remotes").fetchall()
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        assert tables == {"objects", "names", "remotes"}
        expected_objects = (
            []
            if all_selected
            else [
                (
                    retained,
                    len(retained_bytes),
                    _WORD_TYPE,
                    cas.object_relpath(retained).as_posix(),
                    original[1]["at"],
                    "b1",
                )
            ]
        )
        expected_names = (
            []
            if all_selected
            else [
                (retained, "root-a", "docs/bravo.txt", original[1]["at"]),
                (retained, "root-a", "retained/alpha.txt", "2026-01-01T00:00:04Z"),
            ]
        )
        assert object_rows == expected_objects
        assert name_rows == expected_names
        assert Counter(remote_rows) == Counter(
            [retained_remote] * 2 if indexed and not all_selected else []
        )
        for path in destination.rglob("*"):
            if path.is_file():
                data = path.read_bytes()
                assert b"removed-only-database-residue-token" not in data
                assert b"removed-only-remote-token" not in data
        assert store_state.read_store_identity(destination) == result.successor
        assert cas.verify(destination).ok


def test_excision_pre_retirement_restart_is_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from castle import excision, store_state

    _exercise_pre_retirement_faults(tmp_path / "durability", monkeypatch)
    for label in (
        "store.json:building",
        "excision-selection.json:selection",
        "store.json:prepared",
        "retired.json:retired",
    ):
        for when in ("before", "after"):
            if label == "retired.json:retired" and when == "after":
                continue
            parent = tmp_path / (label.replace(":", "-") + "-" + when)
            source, destination, removed, retained, identity, operation = _excision_fixture(parent)
            before = _full_snapshot(source)
            with monkeypatch.context() as controlled:
                reached = _stop_publication(controlled, label, when)
                with pytest.raises(excision.ExcisionError, match="^excision-preparation-failed$"):
                    excision.excise_store(
                        source,
                        destination,
                        cas_ids=[removed],
                        operation_id=operation,
                        expected_source=identity,
                    )
            assert reached == [label]
            assert _full_snapshot(source) == before
            if label == "store.json:building" and when == "before":
                unowned = _full_snapshot(destination)
                with pytest.raises(excision.ExcisionError, match="^excision-destination-unowned$"):
                    excision.excise_store(
                        source,
                        destination,
                        cas_ids=[removed],
                        operation_id=operation,
                        expected_source=identity,
                    )
                assert _full_snapshot(destination) == unowned
                continue
            saved = json.loads((destination / "store.json").read_text())
            with pytest.raises(excision.ExcisionError, match="^excision-preparation-incomplete$"):
                excision.recover_excision(
                    source, destination, operation_id=operation, expected_source=identity
                )
            if (destination / "excision-selection.json").exists():
                snapshot = _full_snapshot(destination)
                with pytest.raises(excision.ExcisionError, match="^excision-operation-mismatch$"):
                    excision.excise_store(
                        source,
                        destination,
                        cas_ids=[retained],
                        operation_id=operation,
                        expected_source=identity,
                    )
                assert _full_snapshot(destination) == snapshot
                selection = json.loads((destination / "excision-selection.json").read_text())
                assert selection["cas_ids"] == [removed]
                assert (destination / "excision-selection.json").stat().st_mode & 0o777 == 0o600
            added_file = parent / "growth.txt"
            added_file.write_bytes(b"growth after interrupted preparation\n")
            added = cas.ingest_file(source, added_file, "new")
            receipt = excision.excise_store(
                source,
                destination,
                cas_ids=[removed, removed],
                operation_id=operation,
                expected_source=identity,
            )
            assert receipt.successor.to_dict() == saved["identity"]
            assert (
                cas.require_object(destination, added.cas_id).read_bytes()
                == added_file.read_bytes()
            )
    for kind in ("missing", "torn"):
        source, destination, removed, _retained, identity, operation = _excision_fixture(
            tmp_path / kind
        )
        with monkeypatch.context() as controlled:
            reached = _stop_publication(controlled, "store.json:prepared", "after")
            with pytest.raises(excision.ExcisionError, match="^excision-preparation-failed$"):
                excision.excise_store(
                    source,
                    destination,
                    cas_ids=[removed],
                    operation_id=operation,
                    expected_source=identity,
                )
        assert reached
        selection = destination / "excision-selection.json"
        selection.unlink() if kind == "missing" else selection.write_bytes(b"torn")
        before = (_full_snapshot(source), _full_snapshot(destination))
        with pytest.raises(excision.ExcisionError, match="^excision-recovery-state$"):
            excision.excise_store(
                source,
                destination,
                cas_ids=[removed],
                operation_id=operation,
                expected_source=identity,
            )
        assert (_full_snapshot(source), _full_snapshot(destination)) == before
        assert store_state.read_store_identity(source) == identity


def _metadata_fault(
    monkeypatch: pytest.MonkeyPatch, label: str, boundary: str, when: str
) -> list[str]:
    from contextlib import contextmanager

    from castle import store_state

    publishing: list[str] = []
    reached: list[str] = []
    real_publish, real_read = store_state._publish_metadata, store_state._read_metadata
    real_rename, real_replace = store_state._rename_new, os.replace
    real_sync, real_fsync, real_fdopen = store_state._fsync_directory, os.fsync, os.fdopen

    def hit(site: str, side: str) -> None:
        if publishing == [label] and (site, side) == (boundary, when) and not reached:
            reached.append(f"{label}/{site}/{side}")
            raise OSError("injected private failure")

    def publish(path: Path, value: Any, **kwargs: Any) -> None:
        publishing.append(path.name + ":" + value.get("state", "selection"))
        try:
            real_publish(path, value, **kwargs)
        finally:
            publishing.pop()

    def read(path: Path, **kwargs: Any) -> Any:
        temporary = path.name.startswith(".")
        if temporary:
            hit("readback", "before")
        result = real_read(path, **kwargs)
        if temporary:
            hit("readback", "after")
        return result

    def rename(source: Path, destination: Path) -> None:
        hit("rename", "before")
        real_rename(source, destination)
        hit("rename", "after")

    def replace(source: Any, destination: Any, **kwargs: Any) -> None:
        hit("rename", "before")
        real_replace(source, destination, **kwargs)
        hit("rename", "after")

    def sync(root: Path) -> None:
        hit("directory-fsync", "before")
        real_sync(root)
        hit("directory-fsync", "after")

    def fsync(descriptor: int) -> None:
        regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
        if regular:
            hit("file-fsync", "before")
        real_fsync(descriptor)
        if regular:
            hit("file-fsync", "after")

    @contextmanager
    def fdopen(descriptor: int, mode: str) -> Any:
        with real_fdopen(descriptor, mode) as handle:

            class Writer:
                def write(self, data: bytes) -> int:
                    hit("write", "before")
                    count = handle.write(data)
                    hit("write", "after")
                    return count

                def flush(self) -> None:
                    handle.flush()

                def fileno(self) -> int:
                    return handle.fileno()

            yield Writer()

    monkeypatch.setattr(store_state, "_publish_metadata", publish)
    monkeypatch.setattr(store_state, "_read_metadata", read)
    monkeypatch.setattr(store_state, "_rename_new", rename)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(store_state, "_fsync_directory", sync)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "fdopen", fdopen)
    return reached


def _exercise_pre_retirement_faults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from castle import excision

    for label in (
        "store.json:building",
        "excision-selection.json:selection",
        "store.json:prepared",
        "retired.json:retired",
    ):
        for site in ("write", "file-fsync", "readback", "rename", "directory-fsync"):
            for when in ("before", "after"):
                if label == "retired.json:retired" and (
                    site == "directory-fsync" or (site == "rename" and when == "after")
                ):
                    continue
                parent = tmp_path / f"{label}-{site}-{when}"
                source, destination, removed, retained, identity, operation = _excision_fixture(
                    parent
                )
                original = (source / "journal.jsonl").read_bytes()
                with monkeypatch.context() as controlled:
                    reached = _metadata_fault(controlled, label, site, when)
                    with pytest.raises(
                        excision.ExcisionError, match="^excision-preparation-failed$"
                    ):
                        excision.excise_store(
                            source,
                            destination,
                            cas_ids=[removed],
                            operation_id=operation,
                            expected_source=identity,
                        )
                assert reached == [f"{label}/{site}/{when}"]
                assert not (source / "retired.json").exists()
                assert (source / "journal.jsonl").read_bytes() == original
                assert cas.object_path(source, retained).read_bytes() == b"bravo payload\n"
                if not (destination / "store.json").exists():
                    before = _full_snapshot(destination)
                    with pytest.raises(
                        excision.ExcisionError, match="^excision-destination-unowned$"
                    ):
                        excision.excise_store(
                            source,
                            destination,
                            cas_ids=[removed],
                            operation_id=operation,
                            expected_source=identity,
                        )
                    assert _full_snapshot(destination) == before
                elif (destination / ".excision-selection.json.tmp").exists() and not (
                    destination / "excision-selection.json"
                ).exists():
                    with pytest.raises(excision.ExcisionError, match="^excision-recovery-state$"):
                        excision.excise_store(
                            source,
                            destination,
                            cas_ids=[removed],
                            operation_id=operation,
                            expected_source=identity,
                        )
                else:
                    excision.excise_store(
                        source,
                        destination,
                        cas_ids=[removed],
                        operation_id=operation,
                        expected_source=identity,
                    )
                    assert journal.read_journal(destination)[0]["cas_id"] == retained


def test_excision_recovers_forward_after_each_durable_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from castle import excision, store_state

    cuts = []
    for label in ("retired.json:retired", "store.json:active", "retired.json:complete"):
        for site in ("write", "file-fsync", "readback", "rename", "directory-fsync"):
            for when in ("before", "after"):
                if label == "retired.json:retired" and not (
                    site == "directory-fsync" or (site == "rename" and when == "after")
                ):
                    continue
                cuts.append((label, site, when))
    for label, site, when in cuts:
        parent = tmp_path / f"{label}-{site}-{when}"
        source, destination, removed, retained, identity, operation = _excision_fixture(parent)
        inode = (source / "journal.lock").stat().st_ino
        with monkeypatch.context() as controlled:
            reached = _metadata_fault(controlled, label, site, when)
            with pytest.raises(excision.ExcisionError, match="^excision-retirement-incomplete$"):
                excision.excise_store(
                    source,
                    destination,
                    cas_ids=[removed],
                    operation_id=operation,
                    expected_source=identity,
                )
        assert reached == [f"{label}/{site}/{when}"]
        assert (source / "retired.json").exists()
        with pytest.raises(store_state.StoreStateError, match="^store-retired$"):
            journal.ensure_store(source)
        if (
            label in ("retired.json:retired", "retired.json:complete")
            and site == "directory-fsync"
            and when == "before"
        ):
            marker = json.loads((source / "retired.json").read_text())
            assert marker["state"] == label.split(":")[1]
            before = (_full_snapshot(source), _full_snapshot(destination))
            canonical_source = source.resolve(strict=True)
            retry_syncs = []
            real_sync = store_state._fsync_directory

            def refuse_source_sync(directory: Path) -> None:
                if directory.resolve(strict=True) == canonical_source:
                    assert directory == canonical_source
                    retry_syncs.append(directory)
                    raise OSError("injected continued source fsync failure")
                real_sync(directory)

            # Qualify the fault against the same canonical spelling the runtime
            # uses; an unfired /var versus /private/var fault proves nothing.
            with pytest.raises(OSError, match="continued source fsync failure"):
                refuse_source_sync(canonical_source)
            assert retry_syncs == [canonical_source]
            retry_syncs.clear()
            with monkeypatch.context() as controlled:
                controlled.setattr(store_state, "_fsync_directory", refuse_source_sync)
                with pytest.raises(
                    excision.ExcisionError, match="^excision-retirement-incomplete$"
                ) as caught:
                    excision.recover_excision(
                        source, destination, operation_id=operation, expected_source=identity
                    )
            assert retry_syncs == [canonical_source], label
            assert caught.value.__cause__ is None and caught.value.__suppress_context__
            assert (_full_snapshot(source), _full_snapshot(destination)) == before, label
            assert (source / "journal.lock").stat().st_ino == inode
        receipt = excision.recover_excision(
            source, destination, operation_id=operation, expected_source=identity
        )
        assert receipt.successor.generation == 1
        assert (source / "journal.lock").stat().st_ino == inode
        assert {path.name for path in source.iterdir()} == {"retired.json", "journal.lock"}
        assert cas.require_object(destination, retained).read_bytes() == b"bravo payload\n"
        assert not cas.object_path(destination, removed).exists()
        assert json.loads((destination / "store.json").read_text())["prepared_sha256"] is None
        assert (
            excision.recover_excision(
                source, destination, operation_id=operation, expected_source=identity
            )
            == receipt
        )
    for cut in (
        "objects",
        "journal.jsonl",
        "cas.sqlite",
        "store.json",
        "excision-selection.json",
        "cleanup-fsync",
    ):
        for when in ("before", "after"):
            source, destination, removed, retained, identity, operation = _excision_fixture(
                tmp_path / f"cleanup-{cut}-{when}"
            )
            reached = []
            real_remove, real_tree, real_sync = (
                excision._remove_file,
                excision._remove_tree,
                store_state._fsync_directory,
            )

            def failure() -> None:
                reached.append(cut)
                raise OSError("injected cleanup failure")

            def remove(path: Path, **kwargs: Any) -> None:
                selected = not reached and path.name == cut
                if selected and when == "before":
                    failure()
                real_remove(path, **kwargs)
                if selected and when == "after":
                    failure()

            def tree(path: Path) -> None:
                selected = not reached and path.name == cut
                if selected and when == "before":
                    failure()
                real_tree(path)
                if selected and when == "after":
                    failure()

            def sync(path: Path) -> None:
                selected = (
                    cut == "cleanup-fsync"
                    and not reached
                    and path == source.resolve()
                    and (source / "retired.json").exists()
                    and not (source / "cas.sqlite").exists()
                )
                if selected and when == "before":
                    failure()
                real_sync(path)
                if selected and when == "after":
                    failure()

            with monkeypatch.context() as controlled:
                controlled.setattr(excision, "_remove_file", remove)
                controlled.setattr(excision, "_remove_tree", tree)
                controlled.setattr(store_state, "_fsync_directory", sync)
                with pytest.raises(
                    excision.ExcisionError, match="^excision-retirement-incomplete$"
                ):
                    excision.excise_store(
                        source,
                        destination,
                        cas_ids=[removed],
                        operation_id=operation,
                        expected_source=identity,
                    )
            assert reached == [cut]
            assert json.loads((destination / "store.json").read_text())["state"] == "prepared"
            excision.recover_excision(
                source, destination, operation_id=operation, expected_source=identity
            )
            assert {p.name for p in source.iterdir()} == {"retired.json", "journal.lock"}
            assert cas.require_object(destination, retained).exists()
    for persisted_wal in (False, True):
        source, destination, removed, _retained, identity, operation = _excision_fixture(
            tmp_path / f"active-growth-{persisted_wal}"
        )
        with monkeypatch.context() as controlled:
            reached = _stop_publication(controlled, "retired.json:complete", "before")
            with pytest.raises(excision.ExcisionError, match="^excision-retirement-incomplete$"):
                excision.excise_store(
                    source,
                    destination,
                    cas_ids=[removed],
                    operation_id=operation,
                    expected_source=identity,
                )
        assert reached == ["retired.json:complete"]
        assert json.loads((destination / "store.json").read_text())["state"] == "active"
        added = tmp_path / f"active-growth-input-{persisted_wal}"
        added.write_bytes(b"growth after activation")
        growth_id = cas.sha256_file(added)
        expected_remotes = []
        if persisted_wal:
            script = """
import os
from pathlib import Path
import sqlite3
import sys
from castle import cas
root, added = map(Path, sys.argv[1:])
growth = cas.ingest_file(root, added, "growth")
db = sqlite3.connect(root / "cas.sqlite")
assert db.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
db.execute("PRAGMA wal_autocheckpoint=0")
row = (growth.cas_id, "active-wal-only", "key", "uploaded", "verified")
db.executemany("INSERT INTO remotes VALUES (?, ?, ?, ?, ?)", [row, row])
db.commit()
print(growth.cas_id, flush=True)
os._exit(0)
"""
            completed = subprocess.run(
                [sys.executable, "-c", script, str(destination), str(added)],
                env=_child_env(),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
            assert completed.stdout.strip() == growth_id
            wal = destination / "cas.sqlite-wal"
            assert b"active-wal-only" in wal.read_bytes()
            assert b"active-wal-only" not in (destination / "cas.sqlite").read_bytes()
            assert (destination / "cas.sqlite-shm").stat().st_size > 0
            expected_remotes = [(growth_id, "active-wal-only", "key", "uploaded", "verified")] * 2
        else:
            assert cas.ingest_file(destination, added, "growth").cas_id == growth_id
        # No parent connection owns the child's WAL. Snapshot collection cannot
        # checkpoint it; positively witness its committed rows before recovery.
        before = _full_snapshot(destination)
        assert Counter(cas._snapshot_remotes(destination)) == Counter(expected_remotes)
        if persisted_wal:
            assert b"active-wal-only" in before["cas.sqlite-wal"][1]
        receipt = excision.recover_excision(
            source, destination, operation_id=operation, expected_source=identity
        )
        assert _full_snapshot(destination) == before
        assert Counter(cas._snapshot_remotes(destination)) == Counter(expected_remotes)
        assert cas.require_object(destination, growth_id).read_bytes() == added.read_bytes()
        assert (
            excision.recover_excision(
                source, destination, operation_id=operation, expected_source=identity
            ).to_dict()
            == receipt.to_dict()
        )
        assert _full_snapshot(destination) == before


def test_excision_recovery_refuses_unbound_or_damaged_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uuid

    from castle import excision, store_state

    cases = [
        "object",
        "journal",
        "outcome-list",
        "outcome-dict",
        "remotes",
        "derived",
        "missing",
        "building",
        "binding",
        "identity",
        "retirement",
        "unknown-source",
        "symlink-source",
        "unknown-destination",
    ]
    for kind in cases:
        source, destination, removed, retained, identity, operation = _excision_fixture(
            tmp_path / kind
        )
        with monkeypatch.context() as controlled:
            reached = _stop_publication(controlled, "retired.json:retired", "after")
            with pytest.raises(excision.ExcisionError, match="^excision-retirement-incomplete$"):
                excision.excise_store(
                    source,
                    destination,
                    cas_ids=[removed],
                    operation_id=operation,
                    expected_source=identity,
                )
        assert reached
        code = "excision-recovery-state"
        if kind == "object":
            target = cas.object_path(destination, retained)
            target.chmod(0o644)
            target.write_bytes(b"damaged retained object")
            target.chmod(0o444)
        elif kind == "journal":
            (destination / "journal.jsonl").write_bytes(b"damaged retained journal")
        elif kind in ("outcome-list", "outcome-dict"):
            records = [
                json.loads(line)
                for line in (destination / "journal.jsonl").read_text().splitlines()
            ]
            assert journal.parse_journal_record(records[0], "control") == records[0]
            records[0]["outcome"] = [] if kind == "outcome-list" else {}
            _write_journal(destination, records)
        elif kind in ("remotes", "derived"):
            from contextlib import closing

            with closing(sqlite3.connect(destination / "cas.sqlite")) as db:
                if kind == "remotes":
                    db.execute(
                        "INSERT INTO remotes VALUES (?, 'changed', 'key', 'at', 'at')", (retained,)
                    )
                else:
                    db.execute("DELETE FROM names")
                db.commit()
        elif kind == "missing":
            destination.rename(destination.with_name("preserved-destination"))
        elif kind in ("building", "binding", "identity"):
            metadata = json.loads((destination / "store.json").read_text())
            if kind == "building":
                metadata["state"], metadata["prepared_sha256"] = "building", None
            elif kind == "binding":
                code = "excision-operation-mismatch"
                metadata["transition"]["operation_id"] = str(uuid.uuid4())
            else:
                code = "excision-operation-mismatch"
                local = str(uuid.uuid4())
                metadata["identity"]["store_id"] = local
                metadata["transition"]["successor"]["store_id"] = local
            (destination / "store.json").write_bytes(store_state._canonical(metadata))
        elif kind == "retirement":
            (source / "retired.json").write_bytes(b"malformed retirement")
        elif kind in ("unknown-source", "symlink-source"):
            code = "excision-retirement-incomplete"
            unrelated = source.parent / "unrelated"
            unrelated.write_bytes(b"unrelated sensitive bytes")
            if kind == "unknown-source":
                (source / "unrelated").write_bytes(unrelated.read_bytes())
            else:
                (source / "unrelated").symlink_to(unrelated)
        else:
            (destination / "unrelated").write_bytes(b"unrelated destination bytes")
        before = (_full_snapshot(source), _full_snapshot(destination))
        with pytest.raises(excision.ExcisionError, match=f"^{code}$") as caught:
            excision.recover_excision(
                source, destination, operation_id=operation, expected_source=identity
            )
        assert str(caught.value) == code, kind
        assert caught.value.__cause__ is None, kind
        assert caught.value.__context__ is None or caught.value.__suppress_context__, kind
        assert (_full_snapshot(source), _full_snapshot(destination)) == before, kind
        with pytest.raises(store_state.StoreStateError, match="^store-retired$"):
            journal.ensure_store(source)
    for kind in ("empty", "unrelated", "active-without-retirement"):
        source, destination, removed, _retained, identity, operation = _excision_fixture(
            tmp_path / kind
        )
        code = "excision-destination-unowned"
        if kind == "empty":
            destination.mkdir()
        elif kind == "unrelated":
            journal.ensure_store(destination)
        else:
            with monkeypatch.context() as controlled:
                reached = _stop_publication(controlled, "store.json:prepared", "after")
                with pytest.raises(excision.ExcisionError, match="^excision-preparation-failed$"):
                    excision.excise_store(
                        source,
                        destination,
                        cas_ids=[removed],
                        operation_id=operation,
                        expected_source=identity,
                    )
            assert reached
            metadata = json.loads((destination / "store.json").read_text())
            metadata.update(state="active", prepared_sha256=None)
            (destination / "store.json").write_bytes(store_state._canonical(metadata))
            code = "excision-recovery-state"
        before = (_full_snapshot(source), _full_snapshot(destination))
        with pytest.raises(excision.ExcisionError, match=f"^{code}$"):
            excision.excise_store(
                source,
                destination,
                cas_ids=[removed],
                operation_id=operation,
                expected_source=identity,
            )
        assert (_full_snapshot(source), _full_snapshot(destination)) == before


def test_excision_receipt_is_closed_private_and_stable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses
    import inspect
    import traceback
    import uuid
    from collections.abc import Sequence

    from castle import excision, store_state

    failed_source, failed_destination, selected, _kept, failed_identity, failed_operation = (
        _excision_fixture(tmp_path / "private-failure")
    )

    def unreadable(_path: Path) -> str:
        raise OSError(selected + " private SQL and path")

    with monkeypatch.context() as controlled:
        controlled.setattr(cas, "sha256_file", unreadable)
        with pytest.raises(
            excision.ExcisionError, match="^excision-source-inconsistent$"
        ) as caught:
            excision.excise_store(
                failed_source,
                failed_destination,
                cas_ids=[selected],
                operation_id=failed_operation,
                expected_source=failed_identity,
            )
    printable = "".join(traceback.format_exception(caught.value))
    assert selected not in printable and "private SQL and path" not in printable
    assert caught.value.__suppress_context__

    source, destination, removed, retained, identity, operation = _excision_fixture(
        tmp_path / "first"
    )
    receipt = excision.excise_store(
        source, destination, cas_ids=[removed], operation_id=operation, expected_source=identity
    )
    literal = {
        "schema": "castle.excision_receipt.v1",
        "operation_id": operation,
        "predecessor": identity.to_dict(),
        "successor": store_state.read_store_identity(destination).to_dict(),
        "outcome": "completed",
        "local_result": "successor-active-predecessor-retired",
    }
    assert receipt.to_dict() == literal
    assert [field.name for field in dataclasses.fields(receipt)] == list(literal)
    with pytest.raises(dataclasses.FrozenInstanceError, match="cannot assign"):
        receipt.outcome = "other"
    for field, value in (
        ("schema", "wrong"),
        ("outcome", "physically-erased"),
        ("local_result", None),
        ("operation_id", removed),
        ("predecessor", identity.to_dict()),
        ("successor", identity),
    ):
        with pytest.raises(excision.ExcisionError, match="^excision-request-invalid$"):
            dataclasses.replace(receipt, **{field: value})
    for field in ("cas_ids", "reason", "count"):
        with pytest.raises(TypeError, match="unexpected keyword"):
            excision.ExcisionReceipt(**{**dataclasses.asdict(receipt), field: "private"})
    for function, selected in ((excision.excise_store, True), (excision.recover_excision, False)):
        parameters = [
            inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Path)
            for name in ("source", "destination")
        ]
        if selected:
            parameters.append(
                inspect.Parameter(
                    "cas_ids", inspect.Parameter.KEYWORD_ONLY, annotation=Sequence[str]
                )
            )
        parameters.extend(
            [
                inspect.Parameter("operation_id", inspect.Parameter.KEYWORD_ONLY, annotation=str),
                inspect.Parameter(
                    "expected_source",
                    inspect.Parameter.KEYWORD_ONLY,
                    annotation=store_state.StoreIdentity,
                ),
            ]
        )
        assert inspect.signature(function, eval_str=True) == inspect.Signature(
            parameters, return_annotation=excision.ExcisionReceipt
        )
    assert list(inspect.signature(excision.ExcisionError).parameters) == ["code"]
    assert excision.ExcisionError.__bases__ == (journal.CastleError,)
    state_codes = {
        "store-retired",
        "store-identity-required",
        "store-state-invalid",
        "store-not-active",
        "store-epoch-mismatch",
        "store-identity-aliased",
        "store-preparation-failed",
    }
    excision_codes = {
        "excision-root-unsafe",
        "excision-request-invalid",
        "excision-destination-unowned",
        "excision-operation-mismatch",
        "excision-source-inconsistent",
        "excision-remotes-unreadable",
        "excision-selection-unknown",
        "excision-preparation-incomplete",
        "excision-recovery-state",
        "excision-retirement-committed",
        "excision-preparation-failed",
        "excision-retirement-incomplete",
    }
    for owner, codes in (
        (store_state.StoreStateError, state_codes),
        (excision.ExcisionError, excision_codes),
    ):
        for code in codes:
            error = owner(code)
            assert error.code == code and str(error) == code
            assert repr(error) == f"{owner.__name__}('{code}')"
        with pytest.raises(ValueError, match="invalid"):
            owner("private cause")
    for wrong_operation, wrong_identity in (
        (str(uuid.uuid4()), identity),
        (operation, receipt.successor),
    ):
        before = (_full_snapshot(source), _full_snapshot(destination))
        with pytest.raises(excision.ExcisionError, match="^excision-operation-mismatch$") as caught:
            excision.recover_excision(
                source, destination, operation_id=wrong_operation, expected_source=wrong_identity
            )
        printable = "".join(traceback.format_exception(caught.value))
        assert removed not in printable and "private cause" not in printable
        assert (_full_snapshot(source), _full_snapshot(destination)) == before
    canonical = json.dumps(literal, sort_keys=True, separators=(",", ":"))
    for stage in ("unchanged", "grown", "subsequently-retired"):
        if stage == "grown":
            input_file = tmp_path / "growth"
            input_file.write_bytes(b"later successor growth")
            cas.ingest_file(destination, input_file, "growth")
        elif stage == "subsequently-retired":
            excision.excise_store(
                destination,
                destination.with_name("third"),
                cas_ids=[retained],
                operation_id=str(uuid.uuid4()),
                expected_source=receipt.successor,
            )
        before = (_full_snapshot(source), _full_snapshot(destination))
        repeated = excision.recover_excision(
            source, destination, operation_id=operation, expected_source=identity
        )
        assert json.dumps(repeated.to_dict(), sort_keys=True, separators=(",", ":")) == canonical
        assert (_full_snapshot(source), _full_snapshot(destination)) == before
    for root in (source, destination):
        for path in root.iterdir():
            if path.is_file():
                payload = path.read_bytes()
                assert removed.encode() not in payload
                assert b"docs/alpha.txt" not in payload
    with pytest.raises(excision.ExcisionError, match="^excision-retirement-committed$"):
        excision.excise_store(
            source, destination, cas_ids=[removed], operation_id=operation, expected_source=identity
        )


def test_store_lifecycle_refuses_before_content_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inspect

    from castle import excision, store_state

    input_file = tmp_path / "input"
    input_file.write_bytes(b"input")
    empty_input = tmp_path / "empty-input"
    empty_input.mkdir()

    def take_lock(root: Path) -> None:
        with journal.journal_lock(root):
            pytest.fail("lifecycle admitted a journal handle")

    for phase, code in (
        ("retired", "store-retired"),
        ("malformed-retirement", "store-retired"),
        ("building", "store-not-active"),
        ("prepared", "store-not-active"),
        ("metadata-free", "store-identity-required"),
    ):
        source, destination, removed, _retained, identity, operation = _excision_fixture(
            tmp_path / phase
        )
        root = source
        if phase in ("building", "prepared"):
            with monkeypatch.context() as controlled:
                reached = _stop_publication(controlled, "store.json:" + phase, "after")
                with pytest.raises(excision.ExcisionError, match="^excision-preparation-failed$"):
                    excision.excise_store(
                        source,
                        destination,
                        cas_ids=[removed],
                        operation_id=operation,
                        expected_source=identity,
                    )
            assert reached
            root = destination
        elif phase == "metadata-free":
            (source / "store.json").unlink()
        elif phase == "retired":
            excision.excise_store(
                source,
                destination,
                cas_ids=[removed],
                operation_id=operation,
                expected_source=identity,
            )
        else:
            (source / "retired.json").write_bytes(b"malformed")
        calls = {
            "journal.read_journal": lambda: journal.read_journal(root),
            "journal.ensure_store": lambda: journal.ensure_store(root),
            "journal.journal_lock": lambda: take_lock(root),
            "cas.require_object": lambda: cas.require_object(root, removed),
            "cas.detect_stored_media": lambda: cas.detect_stored_media(root, removed, 14),
            "cas.seal_objects": lambda: cas.seal_objects(root, []),
            "cas.rebuild_index": lambda: cas.rebuild_index(root),
            "cas.rebuild_index_under_lock": lambda: cas.rebuild_index_under_lock(root),
            "cas.verify": lambda: cas.verify(root),
            "cas.names_for": lambda: cas.names_for(root, removed),
            "cas.find_names": lambda: cas.find_names(root, "alpha"),
            "cas.materialize": lambda: cas.materialize(root, removed, tmp_path / "output"),
            "cas.object_stat": lambda: cas.object_stat(root, removed),
            "cas.store_stat": lambda: cas.store_stat(root),
            "cas.ingest": lambda: cas.ingest(root, empty_input, "input", dry_run=True),
            "cas.ingest_file": lambda: cas.ingest_file(root, input_file, "input"),
            "cas.sightings_for": lambda: cas.sightings_for(root, removed),
            "cas.receipt_for": lambda: cas.receipt_for(root, removed),
        }
        inventory = {
            module.__name__.split(".")[-1] + "." + name
            for module in (journal, cas)
            for name, value in vars(module).items()
            if not name.startswith("_")
            and inspect.isfunction(value)
            and value.__module__ == module.__name__
            and "store" in inspect.signature(value).parameters
            and name != "object_path"
        }
        assert inventory == set(calls)
        before = _full_snapshot(root)
        reached = []

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            reached.append("content or bootstrap")
            pytest.fail("lifecycle checked after content I/O")

        with monkeypatch.context() as controlled:
            controlled.setattr(journal, "_read_authority_journal", forbidden)
            controlled.setattr(sqlite3, "connect", forbidden)
            controlled.setattr(cas, "sha256_file", forbidden)
            controlled.setattr(Path, "mkdir", forbidden)
            for name, call in calls.items():
                with pytest.raises(store_state.StoreStateError, match=f"^{code}$"):
                    call()
                assert reached == [], name
            with pytest.raises(store_state.StoreStateError, match=f"^{code}$"):
                cas.ingest(root, empty_input, "input", dry_run=False)
            with pytest.raises(store_state.StoreStateError, match=f"^{code}$"):
                cas.ingest(root, input_file, "input", dry_run=True)
        assert _full_snapshot(root) == before
        assert not (tmp_path / "output").exists()
    active = tmp_path / "collector"
    journal.ensure_store(active)
    for site in ("read_journal", "_ingest_one", "seal_objects"):
        reached = []

        def retired(*args: Any, **kwargs: Any) -> Any:
            reached.append(site)
            raise store_state.StoreStateError("store-retired")

        with monkeypatch.context() as controlled:
            controlled.setattr(cas, site, retired)
            with pytest.raises(store_state.StoreStateError, match="^store-retired$"):
                if site == "read_journal":
                    cas.verify(active)
                else:
                    cas.ingest(active, input_file, "input")
        assert reached == [site]
