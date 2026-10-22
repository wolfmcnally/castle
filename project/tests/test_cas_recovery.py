"""One protocol proof: independent authority, real death, owned fault recovery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import uuid
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from castle import cas, journal
from castle import cas_recovery as recovery
from castle import namespace as n
from castle.store_state import read_store_identity


def _id() -> str:
    return str(uuid.uuid4())


def _fixture(root: Path, payload: bytes = b"new publication bytes\n"):
    root.mkdir()
    store = root / "cas"
    seed = root / "seed"
    seed.write_bytes(b"retained seed\n")
    cas.ingest_file(store, seed, "seed")
    identity = read_store_identity(store)
    request = recovery.PublicationRequest(
        _id(),
        _id(),
        _id(),
        identity,
        "sha256:" + hashlib.sha256(payload).hexdigest(),
        len(payload),
        _id(),
    )
    operation = root / request.operation_id
    operation.mkdir()
    stage = operation / request.staged_file_id
    stage.write_bytes(payload)
    stage.chmod(0o444)
    remote = (journal.read_journal(store)[0]["cas_id"], "archive", "key", "uploaded", "checked")
    with sqlite3.connect(store / cas.INDEX_FILENAME) as connection:
        connection.executemany("INSERT INTO remotes VALUES (?,?,?,?,?)", [remote, remote])
    return store, operation, identity, request, payload, remote


_CHILD = r"""
import json, os, sys
from pathlib import Path
from castle import cas_recovery as r
from castle.store_state import StoreIdentity
store, operation, raw, point = sys.argv[1:]
value = json.loads(raw)
value['cas_binding'] = StoreIdentity(**value['cas_binding'])
request = r.PublicationRequest(**value)
store, operation = Path(store), Path(operation)
if point in {'prepared','complete'}:
    original = r._save
    def save(*a, **kw):
        original(*a, **kw)
        if ((point == 'prepared' and kw['new'])
                or (point == 'complete' and a[2]['state'] == 'complete')):
            os._exit(97)
    r._save = save
elif point == 'object':
    original = r._copy_object
    def copy(*a):
        original(*a)
        os._exit(97)
    r._copy_object = copy
elif point in {'temporary','partial-evidence'}:
    original = r.os.write
    temporary = (r._temporary(store, request) if point == 'temporary'
                 else r._record_file(operation, request))
    def write(fd, data):
        if temporary.exists() and os.fstat(fd).st_ino == temporary.stat().st_ino:
            original(fd, data[:max(1,len(data)//2)])
            os._exit(97)
        return original(fd, data)
    r.os.write = write
elif point == 'append':
    original = r.os.pwrite
    def pwrite(fd, data, offset):
        original(fd, data[:len(data)//2], offset)
        os._exit(97)
    r.os.pwrite = pwrite
elif point in {'before-index','after-index'}:
    original = r._index_commit
    def commit(connection):
        if point == 'after-index':
            original(connection)
        os._exit(97)
    r._index_commit = commit
r.publish_namespace_content(store, operation, request.cas_binding, request)
os._exit(97)
"""


def _kill(store: Path, operation: Path, request: recovery.PublicationRequest, point: str):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(cas.__file__).resolve().parent.parent)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(store),
            str(operation),
            json.dumps(asdict(request)),
            point,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert child.returncode == 97, (point, child.stdout, child.stderr)


def _assert_authority(store, operation, identity, request, payload, remote):
    receipt = recovery.recover_namespace_publication(store, operation, identity, request)
    assert recovery.recover_namespace_publication(store, operation, identity, request) == receipt
    assert recovery.publish_namespace_content(store, operation, identity, request) == receipt
    assert cas.object_path(store, request.cas_id).read_bytes() == payload
    assert cas.object_path(store, request.cas_id).stat().st_mode & 0o222 == 0
    events = [json.loads(line) for line in (store / "journal.jsonl").read_bytes().splitlines()]
    assert len(events) == 2
    event = events[1]
    assert event["source_root_id"] == "namespace:" + request.namespace_id
    assert event["original_relpath"] == request.operation_id + "/" + request.publication_id
    assert event["batch"] == request.operation_id
    assert event["outcome"] == "stored"
    assert event["cas_id"] == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert event["size"] == len(payload)
    assert receipt.event_ids == (event["event_id"],)
    assert receipt.journal_length == (store / "journal.jsonl").stat().st_size
    assert (
        receipt.journal_sha256 == hashlib.sha256((store / "journal.jsonl").read_bytes()).hexdigest()
    )
    assert set(asdict(receipt)) == {
        "publication_id",
        "operation_id",
        "cas_binding",
        "cas_id",
        "size",
        "event_ids",
        "journal_length",
        "journal_sha256",
    }
    assert Counter(cas._snapshot_remotes(store)) == Counter([remote, remote])
    with sqlite3.connect(store / cas.INDEX_FILENAME) as connection:
        rows = connection.execute("SELECT cas_id,size FROM objects ORDER BY cas_id").fetchall()
        assert sorted(rows) == sorted(
            [(events[0]["cas_id"], len(b"retained seed\n")), (request.cas_id, len(payload))]
        )
        assert connection.execute(
            "SELECT original_relpath FROM names WHERE cas_id=?", (request.cas_id,)
        ).fetchall() == [(request.operation_id + "/" + request.publication_id,)]
    assert cas.verify(store).ok
    assert not recovery._temporary(store, request).exists()
    return receipt


def test_owned_publication_recovery_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    values = _fixture(tmp_path / "partial-evidence")
    store, operation, identity, request, *_ = values
    original_journal = (store / "journal.jsonl").read_bytes()
    _kill(store, operation, request, "partial-evidence")
    partial = recovery._record_file(operation, request).read_bytes()
    with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
        recovery.recover_namespace_publication(store, operation, identity, request)
    assert recovery._record_file(operation, request).read_bytes() == partial
    assert (store / "journal.jsonl").read_bytes() == original_journal
    assert not cas.object_path(store, request.cas_id).exists()

    # Each point is real abrupt process death, independently followed by one
    # exact expected authority. No hardware power-loss claim follows.
    for point in (
        "prepared",
        "temporary",
        "object",
        "append",
        "before-index",
        "after-index",
        "complete",
        "response",
    ):
        values = _fixture(tmp_path / point)
        store, operation, identity, request, payload, remote = values
        _kill(store, operation, request, point)
        saved = json.loads(recovery._record_file(operation, request).read_bytes())
        event_ids = [event["event_id"] for event in saved["events"]]
        receipt = _assert_authority(*values)
        assert list(receipt.event_ids) == event_ids

    # A complete saved append can have later valid events. Actual positional
    # writes reestablish only the saved range and leave later bytes untouched.
    values = _fixture(tmp_path / "later")
    store, operation, identity, request, payload, remote = values
    _kill(store, operation, request, "after-index")
    later = tmp_path / "later-source"
    later.write_bytes(b"subsequent durable content\n")
    cas.ingest_file(store, later, "later")
    before = (store / "journal.jsonl").read_bytes()
    calls = []
    original_pwrite = os.pwrite

    def observed(fd, data, offset):
        calls.append((offset, bytes(data)))
        return original_pwrite(fd, data, offset)

    with monkeypatch.context() as patch:
        patch.setattr(recovery.os, "pwrite", observed)
        receipt = recovery.recover_namespace_publication(store, operation, identity, request)
    assert calls and (store / "journal.jsonl").read_bytes() == before
    assert len(journal.read_journal(store)) == 3
    assert receipt.journal_length < len(before)
    assert Counter(cas._snapshot_remotes(store)) == Counter([remote, remote])

    # Dedup does not rewrite a preexisting object and uses opaque provenance.
    store, operation, identity, request, payload, remote = _fixture(
        tmp_path / "dedup", b"retained seed\n"
    )
    existing = cas.object_path(store, request.cas_id)
    inode = existing.stat().st_ino
    first = recovery.publish_namespace_content(store, operation, identity, request)
    assert recovery.recover_namespace_publication(store, operation, identity, request) == first
    assert existing.stat().st_ino == inode
    assert journal.read_journal(store)[1]["outcome"] == "dedup"

    values = _fixture(tmp_path / "empty", b"")
    recovery.publish_namespace_content(*values[:4])
    _assert_authority(*values)

    # A dedup recipe never owns a CAS temporary, even at its deterministic name.
    store, operation, identity, request, *_ = _fixture(tmp_path / "dedup-temp", b"retained seed\n")
    _kill(store, operation, request, "prepared")
    temporary = recovery._temporary(store, request)
    temporary.write_bytes(b"unowned")
    before = (store / "journal.jsonl").read_bytes()
    with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
        recovery.recover_namespace_publication(store, operation, identity, request)
    assert temporary.read_bytes() == b"unowned"
    assert (store / "journal.jsonl").read_bytes() == before

    # Missing evidence after completion must not mint a second dedup sighting.
    store, operation, identity, request, *_ = _fixture(tmp_path / "lost-complete-evidence")
    _kill(store, operation, request, "response")
    recovery._record_file(operation, request).unlink()
    before = (store / "journal.jsonl").read_bytes()
    with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
        recovery.publish_namespace_content(store, operation, identity, request)
    assert (store / "journal.jsonl").read_bytes() == before
    assert not recovery._record_file(operation, request).exists()

    # Positive short writes finish the exact stream, zero progress refuses.
    values = _fixture(tmp_path / "short")
    original_write = os.write

    def short(fd, data):
        return original_write(fd, data[: max(1, len(data) // 2)])

    with monkeypatch.context() as patch:
        patch.setattr(recovery.os, "write", short)
        recovery.publish_namespace_content(*values[:4])
    _assert_authority(*values)

    for point in (
        "object-write",
        "journal-write",
        "file-sync",
        "directory-sync",
        "object-sync",
        "journal-sync",
        "index-sync",
        "complete-sync",
        "object-directory-sync",
        "index-before",
        "index-after",
        "complete-replace",
    ):
        values = _fixture(tmp_path / ("fault-" + point))
        store, operation, identity, request, payload, remote = values
        _kill(store, operation, request, "prepared")
        with monkeypatch.context() as patch:
            if point == "object-write":
                original = recovery.os.write
                failed = []

                def zero(fd, data):
                    temporary = recovery._temporary(store, request)
                    if (
                        not failed
                        and temporary.exists()
                        and os.fstat(fd).st_ino == temporary.stat().st_ino
                    ):
                        failed.append(True)
                        return 0
                    return original(fd, data)

                patch.setattr(recovery.os, "write", zero)
            elif point == "journal-write":
                patch.setattr(recovery.os, "pwrite", lambda *a: 0)
            elif point.endswith("sync"):
                original = recovery.os.fsync
                failed = []

                def sync(fd):
                    info = os.fstat(fd)
                    is_directory = stat.S_ISDIR(info.st_mode)
                    temporary = recovery._temporary(store, request)
                    selected = (
                        point == "file-sync"
                        and not is_directory
                        or point == "directory-sync"
                        and is_directory
                        or point == "object-sync"
                        and temporary.exists()
                        and info.st_ino == temporary.stat().st_ino
                        or point == "journal-sync"
                        and info.st_ino == (store / "journal.jsonl").stat().st_ino
                        and info.st_size
                        > json.loads(recovery._record_file(operation, request).read_bytes())[
                            "journal_prefix_length"
                        ]
                        or point == "index-sync"
                        and info.st_ino == (store / cas.INDEX_FILENAME).stat().st_ino
                        or point == "object-directory-sync"
                        and temporary.parent.exists()
                        and info.st_ino == temporary.parent.stat().st_ino
                        and cas.object_path(store, request.cas_id).exists()
                    )
                    completion = operation / f"publication.{request.publication_id}.tmp"
                    if point == "complete-sync" and completion.exists():
                        selected = (
                            info.st_ino == completion.stat().st_ino
                            and json.loads(completion.read_bytes())["state"] == "complete"
                        )
                    if not failed and selected:
                        failed.append(True)
                        raise OSError("injected sync")
                    return original(fd)

                patch.setattr(recovery.os, "fsync", sync)
            elif point in {"index-before", "index-after"}:
                original = recovery._index_commit

                def commit(connection):
                    if point == "index-after":
                        original(connection)
                    raise sqlite3.OperationalError("injected uncertain index commit")

                patch.setattr(recovery, "_index_commit", commit)
            else:
                original = recovery.os.replace

                def replace_file(source, destination):
                    if Path(destination) == recovery._record_file(operation, request):
                        value = json.loads(Path(source).read_bytes())
                        if value["state"] == "complete":
                            raise OSError("injected completion replacement")
                    return original(source, destination)

                patch.setattr(recovery.os, "replace", replace_file)
            with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
                recovery.recover_namespace_publication(store, operation, identity, request)
        saved = json.loads(recovery._record_file(operation, request).read_bytes())
        assert saved["state"] == "prepared"
        # Fixed visible residue per injected boundary is not an acknowledgment.
        # A successful later retry must separately establish fresh durability.
        object_present = point not in {"object-write", "file-sync", "directory-sync", "object-sync"}
        object_file = cas.object_path(store, request.cas_id)
        assert object_file.exists() == object_present, point
        if object_present:
            assert object_file.read_bytes() == payload
        temporary = recovery._temporary(store, request)
        if point in {"object-write", "object-sync"}:
            assert temporary.read_bytes() == (b"" if point == "object-write" else payload)
        else:
            assert not temporary.exists(), point
        current_journal = (store / "journal.jsonl").read_bytes()
        prefix = current_journal[: saved["journal_prefix_length"]]
        assert hashlib.sha256(prefix).hexdigest() == saved["journal_prefix_sha256"]
        appended = point in {
            "journal-sync",
            "index-sync",
            "complete-sync",
            "index-before",
            "index-after",
            "complete-replace",
        }
        expected_stream = b"".join(
            (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
            for event in saved["events"]
        )
        assert current_journal == prefix + (expected_stream if appended else b""), point
        with sqlite3.connect(store / cas.INDEX_FILENAME) as connection:
            rows = {row[0] for row in connection.execute("SELECT cas_id FROM objects")}
        index_complete = point in {"index-sync", "complete-sync", "index-after", "complete-replace"}
        assert rows == {remote[0]} | ({request.cas_id} if index_complete else set()), point
        writes = []
        object_writes = []

        def observe_fresh(fd, data, offset):
            writes.append(bytes(data))
            return original_pwrite(fd, data, offset)

        def observe_object(fd, data):
            temporary = recovery._temporary(store, request)
            if temporary.exists() and os.fstat(fd).st_ino == temporary.stat().st_ino:
                object_writes.append(bytes(data))
            return original_write(fd, data)

        with monkeypatch.context() as patch:
            patch.setattr(recovery.os, "pwrite", observe_fresh)
            patch.setattr(recovery.os, "write", observe_object)
            _assert_authority(*values)
        assert writes  # a newly established write boundary, not visibility
        assert b"".join(object_writes) == payload

    # Persistent I/O error remains unavailable; a later descriptor is not proof.
    values = _fixture(tmp_path / "persistent")
    store, operation, identity, request, *_ = values
    _kill(store, operation, request, "object")
    with monkeypatch.context() as patch:

        def fail_sync(fd):
            raise OSError("persistent storage fault")

        patch.setattr(recovery.os, "fsync", fail_sync)
        with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
            recovery.recover_namespace_publication(store, operation, identity, request)
    assert json.loads(recovery._record_file(operation, request).read_bytes())["state"] == "prepared"

    # Every negative preserves the independently chosen unknown/corrupt bytes.
    for fault in (
        "tail",
        "strict-foreign-tail",
        "request-namespace",
        "request-size",
        "request-cas",
        "request-stage",
        "store-identity",
        "prefix",
        "stage",
        "unknown-temp",
        "epoch",
        "remote-change",
        "missing-evidence",
    ):
        store, operation, identity, request, *_ = _fixture(tmp_path / ("refuse-" + fault))
        _kill(store, operation, request, "prepared")
        witness = store / "journal.jsonl"
        evidence = recovery._record_file(operation, request)
        saved_bytes = evidence.read_bytes()
        if fault == "tail":
            with witness.open("ab") as output:
                output.write(b"unrelated")
        elif fault == "strict-foreign-tail":
            seed = json.loads(witness.read_bytes().splitlines()[0])
            event = {
                "schema": "journal.v2",
                "event_id": "uuid:" + _id(),
                "at": "2099-01-01T00:00:00Z",
                "event": "ingest",
                "cas_id": seed["cas_id"],
                "size": len(b"retained seed\n"),
                "media_type": seed["media_type"],
                "source_root_id": "unrelated",
                "original_relpath": "foreign",
                "batch": "unrelated",
                "outcome": "dedup",
            }
            with witness.open("ab") as output:
                output.write(
                    (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
                )
            assert len(journal.read_journal(store)) == 2
        elif fault.startswith("request-"):
            if fault == "request-namespace":
                request = replace(request, namespace_id=_id())
            elif fault == "request-size":
                request = replace(request, size=request.size + 1)
            elif fault == "request-cas":
                request = replace(request, cas_id="sha256:" + "0" * 64)
            else:
                new_stage = _id()
                (operation / new_stage).write_bytes(
                    (operation / request.staged_file_id).read_bytes()
                )
                (operation / new_stage).chmod(0o444)
                request = replace(request, staged_file_id=new_stage)
        elif fault == "prefix":
            witness.write_bytes(b"x" + witness.read_bytes()[1:])
        elif fault == "stage":
            witness = operation / request.staged_file_id
            witness.chmod(0o600)
            witness.write_bytes(b"changed")
            witness.chmod(0o444)
        elif fault == "unknown-temp":
            witness = recovery._temporary(store, request).with_name(".cas-" + "a" * 32 + ".tmp")
            witness.parent.mkdir(parents=True, exist_ok=True)
            witness.write_bytes(b"unowned")
        elif fault in {"epoch", "store-identity"}:
            identity = replace(
                identity, **{("epoch_id" if fault == "epoch" else "store_id"): _id()}
            )
            request = replace(request, cas_binding=identity)
        elif fault == "remote-change":
            with sqlite3.connect(store / cas.INDEX_FILENAME) as connection:
                connection.execute("DELETE FROM remotes")
        else:
            recovery._record_file(operation, request).unlink()
        original_bytes = witness.read_bytes()
        with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
            recovery.recover_namespace_publication(store, operation, identity, request)
        assert witness.read_bytes() == original_bytes
        if fault != "missing-evidence":
            assert evidence.read_bytes() == saved_bytes

    # Ordinary CAS admission still refuses the deterministic transient name.
    store, operation, identity, request, *_ = _fixture(tmp_path / "strict-temp")
    _kill(store, operation, request, "temporary")
    temporary = recovery._temporary(store, request)
    raw = temporary.read_bytes()
    with pytest.raises(cas.CasError, match="invalid object paths"):
        cas.ingest_file(store, tmp_path / "strict-temp" / "seed", "seed", original_relpath="again")
    assert temporary.read_bytes() == raw
    recovery.recover_namespace_publication(store, operation, identity, request)
