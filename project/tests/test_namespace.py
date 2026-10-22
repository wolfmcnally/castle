"""Independent namespace trees, concurrent outcomes and real restart/fault lanes."""

from __future__ import annotations

import json
import os
import runpy
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from castle import (
    cas,
    journal,
    store_state,
)
from castle import (
    namespace as n,
)
from castle import (
    namespace_store as store,
)
from castle import (
    namespace_transactions as tx,
)

_EXAMPLE = Path(__file__).parents[1] / "examples" / "namespace_demo.py"
_CALLER = runpy.run_path(str(_EXAMPLE))
LocalSession = _CALLER["LocalSession"]


def _id() -> str:
    return str(uuid.uuid4())


class Case:
    def __init__(self, directory: Path) -> None:
        directory.mkdir()
        self.directory = directory
        self.cas_root, self.root = directory / "cas", directory / "namespace"
        self.source = directory / "source"
        self.source.write_bytes(b"independently expected immutable bytes\n")
        self.stored = cas.ingest_file(self.cas_root, self.source, "namespace-test")
        self.session = LocalSession(self.root, self.cas_root, directory / "domain.lock")
        self.initial = n.InitializeRequest(
            _id(),
            _id(),
            store_state.read_store_identity(self.cas_root),
            {"policy_version": 1, "unicode_data_version": unicodedata.unidata_version},
            {"kind": "unprotected"},
        )
        n.create_namespace(self.root, self.cas_root, self.session, self.initial)

    def open(self) -> n.NamespaceHandle:
        return n.open_namespace(self.root, self.cas_root, self.session)

    def request(self, handle: n.NamespaceHandle, kind: str, args: dict) -> n.MutationRequest:
        info = handle.info()
        return n.MutationRequest(info.namespace_id, info.cas, info.revision, _id(), kind, args)

    def directory_request(
        self, handle: n.NamespaceHandle, name: str, *, parent: str | None = None
    ) -> n.MutationRequest:
        return self.request(
            handle,
            "create-directory",
            {
                "entry_id": _id(),
                "parent_id": parent or self.initial.root_id,
                "name": name,
                "metadata": {},
            },
        )

    def apply(self, handle: n.NamespaceHandle, kind: str, args: dict) -> n.MutationResult:
        return n.execute_namespace(handle, self.request(handle, kind, args), {})


def _cas_image(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mode)
        for p in root.rglob("*")
        if p.is_file()
    }


def _tree(handle: n.NamespaceHandle) -> dict[str, dict]:
    info = handle.info()
    result: dict[str, dict] = {}
    pending = [info.root_id]
    while pending:
        identity = pending.pop()
        assert identity not in result
        entry = n.read_namespace(
            handle,
            n.ReadRequest(info.namespace_id, info.cas, None, "get-entry", {"entry_id": identity}),
        ).value
        result[identity] = asdict(entry)
        if entry.kind == "directory":
            children = n.read_namespace(
                handle,
                n.ReadRequest(
                    info.namespace_id,
                    info.cas,
                    info.revision,
                    "list-directory",
                    {"entry_id": identity},
                ),
            ).value
            pending.extend(e.entry_id for e in children)
    return result


def test_namespace_transactions_and_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = Case(tmp_path / "semantic")
    before_cas = _cas_image(case.cas_root)
    assert n.canonical_bytes({"z": 2, "a": 1}) == b'{"a":1,"z":2}\n'
    assert (
        n.canonical_bytes({"\ue000": "\n", "\U00010000": "é"})
        == '{"\ue000":"\\u000a","\U00010000":"é"}\n'.encode()
    )
    for bad in (1.0, float("inf"), n.MAX_INTEGER + 1, "\ud800"):
        with pytest.raises(n.NamespaceError, match="namespace-request-invalid"):
            n.canonical_bytes(bad)
    for raw in (b"-0\n", b"1e0\n", b'{"a":1,"a":2}\n'):
        with pytest.raises(
            n.NamespaceError, match="namespace-(request-invalid|format-unsupported)"
        ):
            n._decode(raw)
    with case.open() as handle:
        root = case.initial.root_id
        folder, empty, file_id, target = _id(), _id(), _id(), _id()
        for identity, parent, name in [
            (folder, root, "Folder"),
            (empty, folder, "Empty"),
            (target, root, "Target"),
        ]:
            case.apply(
                handle,
                "create-directory",
                {"entry_id": identity, "parent_id": parent, "name": name, "metadata": {}},
            )
        case.apply(
            handle,
            "create-file",
            {
                "entry_id": file_id,
                "parent_id": folder,
                "name": "File.txt",
                "metadata": {"quantity": "1.25"},
                "content": {"cas_id": case.stored.cas_id, "size": len(case.source.read_bytes())},
            },
        )
        expected = {
            root: (None, None, "directory"),
            folder: (root, "Folder", "directory"),
            empty: (folder, "Empty", "directory"),
            target: (root, "Target", "directory"),
            file_id: (folder, "File.txt", "file"),
        }
        assert {
            k: (v["parent_id"], v["name"], v["kind"]) for k, v in _tree(handle).items()
        } == expected
        original_ids = set(expected)
        case.apply(handle, "relocate", {"entry_id": folder, "parent_id": target, "name": "Renamed"})
        current = _tree(handle)
        assert set(current) == original_ids and current[folder]["parent_id"] == target
        assert (
            current[empty]["parent_id"] == folder
            and current[file_id]["cas_id"] == case.stored.cas_id
        )
        resolved = n.read_namespace(
            handle,
            n.ReadRequest(
                handle.info().namespace_id,
                handle.info().cas,
                None,
                "resolve",
                {"components": ["target", "renamed", "file.TXT"]},
            ),
        ).value
        assert resolved.entry_id == file_id
        mapping = {old: _id() for old in (folder, empty, file_id)}
        request = case.request(
            handle,
            "duplicate",
            {
                "entry_id": folder,
                "parent_id": root,
                "name": "Copy",
                "identities": [
                    {"source_id": old, "new_id": new}
                    for old, new in reversed(list(mapping.items()))
                ],
            },
        )
        copied = n.execute_namespace(handle, request, {})
        assert (
            n.execute_namespace(
                handle,
                replace(
                    request,
                    args={**request.args, "identities": list(reversed(request.args["identities"]))},
                ),
                {},
            )
            == copied
        )
        assert set(copied.affected_entry_ids) == set(mapping.values())
        current = _tree(handle)
        assert current[mapping[empty]]["parent_id"] == mapping[folder]
        assert current[mapping[file_id]]["cas_id"] == current[file_id]["cas_id"]
        case.apply(handle, "delete-subtree", {"entry_id": mapping[folder]})
        current = _tree(handle)
        assert set(current) == original_ids and current[file_id]["cas_id"] == case.stored.cas_id
        prior = handle.info().revision
        case.apply(handle, "relocate", {"entry_id": folder, "parent_id": target, "name": "Renamed"})
        assert handle.info().revision == prior + 1
        for kind, args, code in [
            (
                "relocate",
                {"entry_id": target, "parent_id": empty, "name": "bad"},
                "namespace-cycle",
            ),
            ("delete-entry", {"entry_id": folder}, "namespace-directory-not-empty"),
            ("delete-subtree", {"entry_id": file_id}, "namespace-kind-mismatch"),
            ("delete-entry", {"entry_id": root}, "namespace-root-immutable"),
            (
                "create-directory",
                {"entry_id": _id(), "parent_id": root, "name": "TARGET", "metadata": {}},
                "namespace-name-conflict",
            ),
            (
                "create-file",
                {
                    "entry_id": _id(),
                    "parent_id": root,
                    "name": "absent",
                    "metadata": {},
                    "content": {"cas_id": "sha256:" + "a" * 64, "size": 1},
                },
                "namespace-content-mismatch",
            ),
        ]:
            request = case.request(handle, kind, args)
            old = _tree(handle)
            revision = handle.info().revision
            for _ in range(2):
                with pytest.raises(n.NamespaceError, match=code):
                    n.execute_namespace(handle, request, {})
            assert _tree(handle) == old and handle.info().revision == revision
            assert n.namespace_operation(handle, request.operation_id).state == "rejected"
        ahead = replace(
            case.directory_request(handle, "future"), expected_revision=handle.info().revision + 1
        )
        with pytest.raises(n.NamespaceError, match="namespace-revision-conflict"):
            n.execute_namespace(handle, ahead, {})
        case.apply(
            handle,
            "create-directory",
            {"entry_id": _id(), "parent_id": root, "name": "advance", "metadata": {}},
        )
        with pytest.raises(n.NamespaceError, match="namespace-revision-conflict"):
            n.execute_namespace(handle, ahead, {})
        with pytest.raises(n.NamespaceError, match="namespace-revision-conflict"):
            n.read_namespace(
                handle,
                n.ReadRequest(case.initial.namespace_id, case.initial.expected_cas, 0, "info", {}),
            )
        barrier = threading.Barrier(2)
        requests = [case.directory_request(handle, f"racer-{i}") for i in range(2)]

        def race(request: n.MutationRequest) -> object:
            barrier.wait(timeout=10)
            try:
                return n.execute_namespace(handle, request, {})
            except n.NamespaceError as error:
                return error.code

        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(race, requests))
        assert sum(isinstance(v, n.MutationResult) for v in outcomes) == 1
        assert outcomes.count("namespace-revision-conflict") == 1
        barrier = threading.Barrier(2)
        same = case.directory_request(handle, "same-token")
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(race, [same, same]))
        assert outcomes[0] == outcomes[1] and isinstance(outcomes[0], n.MutationResult)
        barrier = threading.Barrier(2)
        divergent = case.directory_request(handle, "token-winner")
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(
                pool.map(
                    race, [divergent, replace(divergent, args={**divergent.args, "name": "other"})]
                )
            )
        assert sum(isinstance(value, n.MutationResult) for value in outcomes) == 1
        assert outcomes.count("namespace-operation-conflict") == 1
        # Admission holds readers until the complete multi-row transaction ends.
        waiting = threading.Event()
        release = threading.Event()
        original_write = handle.write_entry
        observed_reads = []
        request_copy = case.request(
            handle,
            "duplicate",
            {
                "entry_id": folder,
                "parent_id": root,
                "name": "ReadRace",
                "identities": [
                    {"source_id": old, "new_id": _id()} for old in (folder, empty, file_id)
                ],
            },
        )
        before_ids = set(_tree(handle))

        def paused_write(entry, **kwargs):
            original_write(entry, **kwargs)
            waiting.set()
            assert release.wait(timeout=10)

        with monkeypatch.context() as patch:
            patch.setattr(handle, "write_entry", paused_write)
            with ThreadPoolExecutor(2) as pool:
                writer = pool.submit(n.execute_namespace, handle, request_copy, {})
                assert waiting.wait(timeout=10)
                reader = pool.submit(_tree, handle)
                release.set()
                after_copy = writer.result(timeout=10)
                observed_reads.append(set(reader.result(timeout=10)))
        assert observed_reads == [before_ids | set(after_copy.affected_entry_ids)]
        with pytest.raises(n.NamespaceError, match="namespace-operation-conflict"):
            n.execute_namespace(handle, replace(same, args={**same.args, "name": "changed"}), {})
        with pytest.raises(n.NamespaceError, match="namespace-request-invalid"):
            n.execute_namespace(
                handle,
                replace(same, operation_id=_id(), kind="snapshot", args={"snapshot_id": _id()}),
                {},
            )
        assert n.namespace_operation(handle, _id()).state == "unknown"
        with pytest.raises(n.NamespaceError, match="namespace-coordinator-busy"):
            case.open()
        with monkeypatch.context() as patch:
            patch.setattr(tx, "_now", lambda: datetime.now(UTC) + timedelta(days=8))
            case.apply(handle, "expire-results", {"operation_ids": [same.operation_id]})
        assert n.namespace_operation(handle, same.operation_id).state == "retired"
        with pytest.raises(n.NamespaceError, match="namespace-operation-retired"):
            n.execute_namespace(handle, same, {})
        assert not (case.root / "operations" / same.operation_id).exists()
        # The root may be duplicated from a frozen whole-tree membership,
        # including into a descendant, without recursively copying the copy.
        before_root_copy = _tree(handle)
        root_mapping = {identity: _id() for identity in before_root_copy}
        result = case.apply(
            handle,
            "duplicate",
            {
                "entry_id": root,
                "parent_id": empty,
                "name": "Whole",
                "identities": [
                    {"source_id": old, "new_id": new} for old, new in root_mapping.items()
                ],
            },
        )
        assert set(result.affected_entry_ids) == set(root_mapping.values())
        copied_tree = _tree(handle)
        assert len(copied_tree) == 2 * len(before_root_copy)
        assert copied_tree[root_mapping[root]]["parent_id"] == empty
        assert copied_tree[root_mapping[file_id]]["cas_id"] == case.stored.cas_id
        case.apply(handle, "delete-subtree", {"entry_id": root_mapping[root]})
        assert set(_tree(handle)) == set(before_root_copy)
        # Read-only failures do not create a persistent write-fault latch.
        with monkeypatch.context() as patch:

            def read_failure(*args, **kwargs):
                raise OSError("injected pre-write read failure")

            patch.setattr(handle, "_inventory", read_failure)
            with pytest.raises(n.NamespaceError, match="namespace-io-failed") as error:
                handle.info()
            assert error.value.effect == "none"
        assert handle.info().revision == result.revision_after + 1
        case.session.active = False
        with pytest.raises(n.NamespaceError, match="namespace-session-expired"):
            handle.info()
        case.session.active = True
    with case.open() as handle:
        assert n.namespace_operation(handle, same.operation_id).state == "retired"
    assert _cas_image(case.cas_root) == before_cas
    with monkeypatch.context() as patch:
        original = case.session.describe_capabilities
        patch.setattr(
            case.session,
            "describe_capabilities",
            lambda: replace(original(), capability_id="unqualified"),
        )
        with pytest.raises(n.NamespaceError, match="namespace-protection-unsupported"):
            case.open()

    case = Case(tmp_path / "individual-file")
    with case.open() as handle:
        root = case.initial.root_id
        folder = _id()
        file_id = _id()
        copied_id = _id()
        case.apply(
            handle,
            "create-directory",
            {"entry_id": folder, "parent_id": root, "name": "Folder", "metadata": {}},
        )
        case.apply(
            handle,
            "create-file",
            {
                "entry_id": file_id,
                "parent_id": root,
                "name": "file",
                "metadata": {},
                "content": {"cas_id": case.stored.cas_id, "size": case.source.stat().st_size},
            },
        )
        case.apply(
            handle, "relocate", {"entry_id": file_id, "parent_id": folder, "name": "renamed"}
        )
        result = case.apply(
            handle,
            "duplicate",
            {
                "entry_id": file_id,
                "parent_id": root,
                "name": "copy",
                "identities": [{"source_id": file_id, "new_id": copied_id}],
            },
        )
        tree = _tree(handle)
        assert tree[file_id]["parent_id"] == folder and tree[file_id]["name"] == "renamed"
        assert tree[file_id]["cas_id"] == tree[copied_id]["cas_id"] == case.stored.cas_id
        case.apply(handle, "delete-entry", {"entry_id": copied_id})
        assert file_id in _tree(handle) and copied_id not in _tree(handle)
        assert handle.info().revision == result.revision_after + 1
        rejected = case.request(handle, "delete-entry", {"entry_id": root})
        with pytest.raises(n.NamespaceError, match="namespace-root-immutable"):
            n.execute_namespace(handle, rejected, {})
        authorized = case.session.check_authorized

        def revoke_ack(kind):
            if kind == "acknowledge":
                raise n.NamespaceError("namespace-session-expired")
            authorized(kind)

        with monkeypatch.context() as patch:
            patch.setattr(case.session, "check_authorized", revoke_ack)
            with pytest.raises(n.NamespaceError, match="namespace-session-expired"):
                n.execute_namespace(handle, rejected, {})
        # A failed BEGIN acquired no transaction or durable token: retry is
        # ordinary I/O failure with no operation identity or write effect.
        request_before_lock = case.directory_request(handle, "before-lock")
        connection = handle.connection

        class BusyBeforePrepare:
            def execute(self, statement, *args):
                if statement == "BEGIN IMMEDIATE":
                    error = sqlite3.OperationalError("injected pre-write lock")
                    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error
                return connection.execute(statement, *args)

            def __getattr__(self, name):
                return getattr(connection, name)

        with monkeypatch.context() as patch:
            patch.setattr(handle, "connection", BusyBeforePrepare())
            with pytest.raises(n.NamespaceError, match="namespace-io-failed") as refusal:
                n.execute_namespace(handle, request_before_lock, {})
            assert refusal.value.operation_id is None and refusal.value.effect == "none"
        assert not handle.failed and not handle.db.in_transaction
        assert (
            handle.db.execute(
                "SELECT 1 FROM operations WHERE operation_id=?", (request_before_lock.operation_id,)
            ).fetchone()
            is None
        )
        # Fixture-only boundary: no library workflow is seeded through SQL.
        handle.db.execute("UPDATE namespace SET revision=?", (n.MAX_INTEGER,))
        request = case.directory_request(handle, "exhausted")
        operations = handle.db.execute("SELECT count(*) FROM operations").fetchone()
        with pytest.raises(n.NamespaceError, match="namespace-revision-exhausted") as refusal:
            n.execute_namespace(handle, request, {})
        assert refusal.value.operation_id is None
        assert refusal.value.namespace_id == case.initial.namespace_id
        assert refusal.value.effect == "none"
        assert handle.db.execute("SELECT count(*) FROM operations").fetchone() == operations
        assert not (case.root / "operations" / request.operation_id).exists()
    # Missing/unsupported protection must refuse before content validation.
    guarded = Case(tmp_path / "protection-before-content")
    with monkeypatch.context() as patch:

        def forbidden_content(*args, **kwargs):
            raise AssertionError("content touched before protection refusal")

        patch.setattr(cas, "_validated_authority", forbidden_content)
        protected_root = guarded.directory / "protected"
        session = LocalSession(protected_root, guarded.cas_root, guarded.directory / "domain.lock")
        with pytest.raises(n.NamespaceError, match="namespace-protection-unsupported"):
            n.create_namespace(
                protected_root,
                guarded.cas_root,
                session,
                replace(guarded.initial, protection={"kind": "protected"}),
            )
        assert not protected_root.exists()
        receipt_file = guarded.root / "operations" / guarded.initial.namespace_id / "receipt.json"
        receipt = n._decode(receipt_file.read_bytes())
        receipt["initial_info"]["protection"] = {"kind": "protected"}
        receipt_file.write_bytes(n.canonical_bytes(receipt))
        with pytest.raises(n.NamespaceError, match="namespace-protection-unsupported"):
            guarded.open()

    _new_content_semantics(tmp_path, monkeypatch)


def _child(case: Case, request: n.MutationRequest, boundary: str) -> subprocess.CompletedProcess:
    payload = json.dumps(asdict(request))
    code = f"""
import os,runpy,sys,json
from pathlib import Path
from castle import namespace as n,namespace_store as store,namespace_transactions as tx
caller=runpy.run_path({str(_EXAMPLE)!r})
session=caller['LocalSession'](
 Path({str(case.root)!r}),Path({str(case.cas_root)!r}),
 Path({str(case.directory / "domain.lock")!r}))
value=json.loads({payload!r})
request=n.MutationRequest(value['namespace_id'],n.StoreIdentity(**value['expected_cas']),value['expected_revision'],value['operation_id'],value['kind'],value['args'])
with n.open_namespace(Path({str(case.root)!r}),Path({str(case.cas_root)!r}),session) as handle:
 original=store._commit
 def commit(connection):
  row=connection.execute(
   'SELECT state FROM operations WHERE operation_id=?',
   (request.operation_id,)).fetchone()
  if {boundary!r}=='before-prepare' and row==('prepared',): os._exit(61)
  original(connection)
  if {boundary!r}=='prepared' and row==('prepared',): os._exit(61)
  if {boundary!r}=='committed' and row==('committed',): os._exit(61)
 store._commit=commit
 mkdir=Path.mkdir
 def interrupted_mkdir(self,*args,**kwargs):
  result=mkdir(self,*args,**kwargs)
  if {boundary!r}=='evidence-directory' and self.name==request.operation_id: os._exit(61)
  return result
 Path.mkdir=interrupted_mkdir
 opened=os.open
 def interrupted_open(file,*args,**kwargs):
  result=opened(file,*args,**kwargs)
  path=Path(file)
  if path.parent.name==request.operation_id:
   if {boundary!r}=='between-evidence-files' and path.name=='receipt.json': os._exit(61)
   if {boundary!r}=='receipt-temporary' and path.name=='receipt.tmp': os._exit(61)
  return result
 os.open=interrupted_open
 if {boundary!r} in ('expiry-request','expiry-receipt'):
  from datetime import datetime,UTC,timedelta
  tx._now=lambda: datetime.now(UTC)+timedelta(days=8)
  unlink=Path.unlink
  def interrupted_unlink(self,*args,**kwargs):
   result=unlink(self,*args,**kwargs)
   target=request.args['operation_ids'][0]
   name='request.json' if {boundary!r}=='expiry-request' else 'receipt.json'
   if self.parent.name==target and self.name==name: os._exit(61)
   return result
  Path.unlink=interrupted_unlink
 if {boundary!r}=='fork-finalization':
  write=handle.write_entry
  def fork_write(entry,**kwargs):
   write(entry,**kwargs)
   child=os.fork()
   if child==0:
    import gc
    assert handle.connection is None and handle.closed
    gc.collect()
    sys.exit(0)
   assert os.waitpid(child,0)[1]==0
   assert handle.db.in_transaction
  handle.write_entry=fork_write
 if {boundary!r}=='hot-journal':
  handle.db.execute('PRAGMA cache_size=1')
  handle.db.execute('PRAGMA cache_spill=ON')
  write=handle.write_entry
  count=[0]
  def partial(entry,**kwargs):
   write(entry,**kwargs)
   count[0]+=1
   if count[0]==5: os._exit(61)
  handle.write_entry=partial
 n.execute_namespace(handle,request,{{}})
 if {boundary!r} in ('response','fork-finalization'): os._exit(61)
 raise RuntimeError('boundary not reached')
"""
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(Path(cas.__file__).parents[1])},
        capture_output=True,
        text=True,
        timeout=45,
    )


def test_namespace_restart_and_storage_faults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for boundary in (
        "before-prepare",
        "prepared",
        "committed",
        "response",
        "evidence-directory",
        "between-evidence-files",
        "receipt-temporary",
        "fork-finalization",
    ):
        case = Case(tmp_path / boundary)
        with case.open() as handle:
            request = case.directory_request(handle, "recovered")
        before_cas = _cas_image(case.cas_root)
        child = _child(case, request, boundary)
        assert child.returncode == 61, child.stdout + child.stderr
        if boundary not in {"before-prepare", "response", "fork-finalization"}:
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                case.open()
            recovered = n.recover_namespace(case.root, case.cas_root, case.session, None)
            assert recovered.completed_operation_ids == sorted(
                set(recovered.completed_operation_ids)
            )
        with case.open() as handle:
            result = n.execute_namespace(handle, request, {})
            assert result.revision_after == 1 and handle.info().revision == 1
            assert _tree(handle)[request.args["entry_id"]]["name"] == "recovered"
        assert _cas_image(case.cas_root) == before_cas
    hot = Case(tmp_path / "hot")
    with hot.open() as handle:
        folder = _id()
        hot.apply(
            handle,
            "create-directory",
            {"entry_id": folder, "parent_id": hot.initial.root_id, "name": "large", "metadata": {}},
        )
        ids = [folder]
        for i in range(12):
            identity = _id()
            ids.append(identity)
            hot.apply(
                handle,
                "create-directory",
                {
                    "entry_id": identity,
                    "parent_id": folder,
                    "name": f"entry-{i}",
                    "metadata": {"padding": "x" * 8192},
                },
            )
        before = _tree(handle)
        mapping = {identity: _id() for identity in ids}
        request = hot.request(
            handle,
            "duplicate",
            {
                "entry_id": folder,
                "parent_id": hot.initial.root_id,
                "name": "copy",
                "identities": [{"source_id": k, "new_id": v} for k, v in mapping.items()],
            },
        )
    child = _child(hot, request, "hot-journal")
    assert child.returncode == 61, child.stdout + child.stderr
    assert (hot.root / "namespace.sqlite-journal").stat().st_size > 512
    original_recovery = store.NamespaceHandle._recover_receipts
    observed = []

    def observe_rollback(handle):
        observed.append(
            {
                row[0]: (row[1], row[2])
                for row in handle.db.execute(
                    "SELECT entry_id,parent_id,name FROM entries WHERE live=1"
                )
            }
        )
        original_recovery(handle)

    with monkeypatch.context() as patch:
        patch.setattr(store.NamespaceHandle, "_recover_receipts", observe_rollback)
        n.recover_namespace(hot.root, hot.cas_root, hot.session, None)
    with hot.open() as handle:
        assert set(_tree(handle)) == set(before) | set(mapping.values())
        assert n.execute_namespace(handle, request, {}).revision_after == len(before)
    assert observed == [{k: (v["parent_id"], v["name"]) for k, v in before.items()}]
    for boundary in ("expiry-request", "expiry-receipt"):
        case = Case(tmp_path / boundary)
        with case.open() as handle:
            original = case.directory_request(handle, "keep")
            n.execute_namespace(handle, original, {})
            request = case.request(
                handle, "expire-results", {"operation_ids": [original.operation_id]}
            )
        child = _child(case, request, boundary)
        assert child.returncode == 61, child.stdout + child.stderr
        image = {
            p.relative_to(case.root): p.read_bytes()
            for p in (case.root / "operations").rglob("*")
            if p.is_file()
        }
        with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
            case.open()
        assert image == {
            p.relative_to(case.root): p.read_bytes()
            for p in (case.root / "operations").rglob("*")
            if p.is_file()
        }
        assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 2
        with case.open() as handle:
            assert n.namespace_operation(handle, original.operation_id).state == "retired"
            assert len(_tree(handle)) == 2
            assert not (case.root / "operations" / original.operation_id).exists()
    for fault in ("receipt-write", "expiry-commit", "busy-commit"):
        case = Case(tmp_path / fault)
        with case.open() as handle:
            original = case.directory_request(handle, "keep")
            n.execute_namespace(handle, original, {})
            request = (
                case.request(handle, "expire-results", {"operation_ids": [original.operation_id]})
                if fault == "expiry-commit"
                else case.directory_request(handle, "next")
            )
            fired = []
            commit = store._commit
            canonical = n.canonical_bytes

            def fail_commit(connection):
                row = connection.execute(
                    "SELECT state FROM operations WHERE operation_id=?", (request.operation_id,)
                ).fetchone()
                if row == ("committed",) and not fired and fault != "receipt-write":
                    fired.append(True)
                    if fault == "expiry-commit":
                        commit(connection)
                        error = sqlite3.OperationalError("injected directory sync failure")
                        error.sqlite_errorcode = sqlite3.SQLITE_IOERR_DIR_FSYNC
                    else:
                        error = sqlite3.OperationalError("injected transient lock")
                        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                    raise error
                commit(connection)

            def fail_receipt(value, **kwargs):
                if (
                    isinstance(value, dict)
                    and value.get("state") == "durable"
                    and value.get("operation_id") == request.operation_id
                    and not fired
                ):
                    fired.append(True)
                    raise OSError("injected temporary receipt write failure")
                return canonical(value, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(tx, "_now", lambda: datetime.now(UTC) + timedelta(days=8))
                patch.setattr(store, "_commit", fail_commit)
                if fault == "receipt-write":
                    patch.setattr(n, "canonical_bytes", fail_receipt)
                with pytest.raises(
                    n.NamespaceError, match="namespace-storage-recovery-required"
                ) as error:
                    n.execute_namespace(handle, request, {})
                assert error.value.namespace_id == case.initial.namespace_id
                assert error.value.operation_id == request.operation_id
                assert error.value.effect == (
                    "namespace-committed" if fault == "receipt-write" else "recovery-required"
                )
            assert fired
            status = n.namespace_operation(handle, request.operation_id)
            assert status.state == "recovery-required" and status.next_action == "recover"
            assert status.result is None
            assert handle.db.execute("SELECT service_state FROM namespace").fetchone() == (
                ("ready",) if fault == "busy-commit" else ("recovery-required",)
            )
        with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
            case.open()
        assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 2
        with case.open() as handle:
            assert n.execute_namespace(handle, request, {}).revision_after == 2
            if fault == "expiry-commit":
                assert n.namespace_operation(handle, original.operation_id).state == "retired"
    # Unexpected exceptions inside a write transaction roll back its partial
    # rows and block this handle; only the durable prepared intent survives.
    case = Case(tmp_path / "unexpected-exception")
    with case.open() as handle:
        request = case.directory_request(handle, "recover")
        with monkeypatch.context() as patch:

            def interrupted_write(*args, **kwargs):
                raise KeyboardInterrupt()

            patch.setattr(handle, "write_entry", interrupted_write)
            with pytest.raises(KeyboardInterrupt):
                n.execute_namespace(handle, request, {})
        assert not handle.db.in_transaction and handle.failed
        assert len(handle.entries()) == 1
    assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 1
    owner = Case(tmp_path / "owner-death")
    ready = owner.directory / "ready"
    code = f"""import runpy,time
from pathlib import Path
from castle import namespace as n
caller=runpy.run_path({str(_EXAMPLE)!r})
session=caller['LocalSession'](
 Path({str(owner.root)!r}),Path({str(owner.cas_root)!r}),
 Path({str(owner.directory / "domain.lock")!r}))
with n.open_namespace(Path({str(owner.root)!r}),Path({str(owner.cas_root)!r}),session):
 Path({str(ready)!r}).write_text('owner-ready')
 time.sleep(60)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(Path(cas.__file__).parents[1])},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child owner did not reach readiness"
        with pytest.raises(n.NamespaceError, match="namespace-coordinator-busy"):
            owner.open()
        child.kill()
        assert child.wait(timeout=10) == -9
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.communicate(timeout=10)
    with owner.open() as handle:
        assert handle.info().revision == 0
        if hasattr(os, "fork"):
            read_fd, write_fd = os.pipe()
            ready_read, ready_write = os.pipe()
            forked = os.fork()
            if forked == 0:
                os.close(write_fd)
                os.close(ready_read)
                assert handle.connection is None and handle.closed
                os.write(ready_write, b"r")
                os.close(ready_write)
                os.read(read_fd, 1)
                os._exit(0)
            os.close(read_fd)
            os.close(ready_write)
            assert os.read(ready_read, 1) == b"r"
            os.close(ready_read)
            try:
                handle.close()
                with owner.open() as replacement:
                    assert replacement.info().revision == 0
            finally:
                os.write(write_fd, b"x")
                os.close(write_fd)
                assert os.waitpid(forked, 0)[1] == 0
    for boundary in ("connected", "before-commit", "after-commit"):
        root = owner.directory / f"initial-{boundary}"
        value = asdict(replace(owner.initial, namespace_id=_id(), root_id=_id()))
        code = f"""import runpy,json,os
from pathlib import Path
from castle import namespace as n,namespace_store as store
caller=runpy.run_path({str(_EXAMPLE)!r})
session=caller['LocalSession'](
 Path({str(root)!r}),Path({str(owner.cas_root)!r}),
 Path({str(owner.directory / "domain.lock")!r}))
value=json.loads({json.dumps(value)!r})
request=n.InitializeRequest(value['namespace_id'],value['root_id'],n.StoreIdentity(**value['expected_cas']),value['policy'],value['protection'])
connect=store.NamespaceHandle._connect
def connected(self,**kwargs):
 connect(self,**kwargs)
 if {boundary!r}=='connected': os._exit(62)
store.NamespaceHandle._connect=connected
commit=store._commit
def interrupted(connection):
 if {boundary!r}=='before-commit': os._exit(62)
 commit(connection)
 if {boundary!r}=='after-commit': os._exit(62)
store._commit=interrupted
n.create_namespace(Path({str(root)!r}),Path({str(owner.cas_root)!r}),session,request)
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            env={**os.environ, "PYTHONPATH": str(Path(cas.__file__).parents[1])},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 62, result.stderr
        session = LocalSession(root, owner.cas_root, owner.directory / "domain.lock")
        with pytest.raises(n.NamespaceError, match="namespace-authority-missing"):
            n.open_namespace(root, owner.cas_root, session)
        request = n.InitializeRequest(
            value["namespace_id"],
            value["root_id"],
            n.StoreIdentity(**value["expected_cas"]),
            value["policy"],
            value["protection"],
        )
        with pytest.raises(n.NamespaceError, match="namespace-root-unsafe"):
            n.create_namespace(root, owner.cas_root, session, request)
    for after_real_commit in (False, True):
        case = Case(tmp_path / f"io-{after_real_commit}")
        handle = case.open()
        request = case.directory_request(handle, "once")
        original_commit = store._commit
        fired = []

        def failing_commit(connection):
            row = connection.execute(
                "SELECT state FROM operations WHERE operation_id=?", (request.operation_id,)
            ).fetchone()
            if not fired and row == ("committed",):
                fired.append(True)
                if after_real_commit:
                    original_commit(connection)
                error = sqlite3.OperationalError("injected durability failure")
                error.sqlite_errorcode = (
                    sqlite3.SQLITE_IOERR_DIR_FSYNC
                    if after_real_commit
                    else sqlite3.SQLITE_IOERR_FSYNC
                )
                raise error
            original_commit(connection)

        with monkeypatch.context() as patch:
            patch.setattr(store, "_commit", failing_commit)
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                n.execute_namespace(handle, request, {})
        assert fired
        handle.close()
        with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
            case.open()
        receipt = n.recover_namespace(case.root, case.cas_root, case.session, None)
        assert receipt.state == "ready" and receipt.revision == 1
        with case.open() as handle:
            assert n.execute_namespace(handle, request, {}).revision_after == 1
            assert len(_tree(handle)) == 2
    case = Case(tmp_path / "directory-sync")
    with case.open() as handle:
        request = case.directory_request(handle, "sync")
        original_sync = store._sync_dir

        def failing_sync(root):
            if root.parent == case.root / "operations":
                raise OSError("injected operation-directory sync failure")
            original_sync(root)

        with monkeypatch.context() as patch:
            patch.setattr(store, "_sync_dir", failing_sync)
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                n.execute_namespace(handle, request, {})
    with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
        case.open()
    assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 1
    for bad in ("residue", "corruption", "epoch"):
        case = Case(tmp_path / bad)
        if bad == "residue":
            (case.cas_root / "unexpected").write_text("unknown")
        elif bad == "corruption":
            file = cas.object_path(case.cas_root, case.stored.cas_id)
            file.chmod(0o644)
            file.write_bytes(b"corrupt")
            file.chmod(0o444)
        else:
            metadata = json.loads((case.cas_root / "store.json").read_text())
            metadata["identity"]["epoch_id"] = _id()
            from castle import store_state

            (case.cas_root / "store.json").write_bytes(store_state._canonical(metadata))
        with pytest.raises(
            n.NamespaceError, match="namespace-(storage-recovery-required|binding-mismatch)"
        ):
            case.open()

    _new_content_restart(tmp_path, monkeypatch)
    _new_content_refusals(tmp_path, monkeypatch)
    _new_content_advice_proofs(tmp_path, monkeypatch)
    _new_receipt_shape_refusal(tmp_path)


def _new_spec(payload: bytes) -> dict:
    import hashlib

    return {"cas_id": "sha256:" + hashlib.sha256(payload).hexdigest(), "size": len(payload)}


def _new_file(case, handle, payload, *, entry_id=None, name="New", metadata=None):
    return case.request(
        handle,
        "create-file",
        {
            "entry_id": entry_id or _id(),
            "parent_id": case.initial.root_id,
            "name": name,
            "metadata": metadata or {},
            "content": _new_spec(payload),
        },
    )


def _new_content_semantics(tmp_path, monkeypatch):
    case = Case(tmp_path / "new-byte-semantics")
    first_bytes, next_bytes = b"first immutable new bytes\n", b"replacement new bytes\n"
    old, new = _new_spec(first_bytes), _new_spec(next_bytes)
    with case.open() as handle:
        create = _new_file(case, handle, first_bytes, metadata={"label": "preserved"})
        first = n.execute_namespace(handle, create, {old["cas_id"]: first_bytes})
        entry, copied = create.args["entry_id"], _id()
        duplicate = case.request(
            handle,
            "duplicate",
            {
                "entry_id": entry,
                "parent_id": case.initial.root_id,
                "name": "Copy",
                "identities": [{"source_id": entry, "new_id": copied}],
            },
        )
        n.execute_namespace(handle, duplicate, {})
        replacement = case.request(handle, "replace-content", {"entry_id": entry, "content": new})
        result = n.execute_namespace(handle, replacement, {new["cas_id"]: next_bytes})
        tree = _tree(handle)
        assert tree[entry] == {
            "entry_id": entry,
            "kind": "file",
            "parent_id": case.initial.root_id,
            "name": "New",
            "metadata": {"label": "preserved"},
            "cas_id": new["cas_id"],
            "size": len(next_bytes),
            "changed_revision": 3,
        }
        assert tree[copied]["cas_id"] == old["cas_id"]
        assert cas.object_path(case.cas_root, old["cas_id"]).read_bytes() == first_bytes
        assert cas.object_path(case.cas_root, new["cas_id"]).read_bytes() == next_bytes
        empty = _new_file(case, handle, b"", name="EmptyBytes")
        empty_result = n.execute_namespace(handle, empty, {_new_spec(b"")["cas_id"]: b""})
        assert cas.object_path(case.cas_root, _new_spec(b"")["cas_id"]).read_bytes() == b""
        journal_before = (case.cas_root / "journal.jsonl").read_bytes()
        # Replay resolves before source transport and stale revision.
        assert n.execute_namespace(handle, create, {"unused": bytearray(b"ignored")}) == first
        assert n.execute_namespace(handle, replacement, {}) == result
        with pytest.raises(n.NamespaceError, match="namespace-operation-conflict"):
            n.execute_namespace(
                handle, replace(replacement, args={"entry_id": copied, "content": old}), {}
            )
        assert (case.cas_root / "journal.jsonl").read_bytes() == journal_before
        deleted_copy = case.apply(handle, "delete-entry", {"entry_id": copied})
        assert _tree(handle)[entry]["cas_id"] == new["cas_id"]
        deleted_original = case.apply(handle, "delete-entry", {"entry_id": entry})
        assert cas.object_path(case.cas_root, old["cas_id"]).read_bytes() == first_bytes
        assert cas.object_path(case.cas_root, new["cas_id"]).read_bytes() == next_bytes
        dependencies = set(
            handle.db.execute(
                "SELECT cas_id FROM dependencies WHERE owner_kind='operation' AND owner_id=?",
                (create.operation_id,),
            )
        )
        assert dependencies == {(old["cas_id"],), (new["cas_id"],)}
        assert set(
            handle.db.execute(
                "SELECT owner_id,cas_id FROM dependencies WHERE owner_kind='operation'"
            )
        ) == {
            (create.operation_id, old["cas_id"]),
            (create.operation_id, new["cas_id"]),
            (duplicate.operation_id, old["cas_id"]),
            (replacement.operation_id, new["cas_id"]),
            (empty_result.operation_id, _new_spec(b"")["cas_id"]),
            (deleted_copy.operation_id, old["cas_id"]),
            (deleted_original.operation_id, new["cas_id"]),
        }
        assert set(
            handle.db.execute("SELECT owner_id,cas_id FROM dependencies WHERE owner_kind='entry'")
        ) == {
            (entry, new["cas_id"]),
            (copied, old["cas_id"]),
            (empty.args["entry_id"], _new_spec(b"")["cas_id"]),
        }
    with case.open() as handle:
        assert set(_tree(handle)) == {case.initial.root_id, empty.args["entry_id"]}
        assert n.execute_namespace(handle, replacement, {}) == result
        assert n.namespace_operation(handle, replacement.operation_id).state == "committed"

    case = Case(tmp_path / "source-refusals")
    data = b"validated transport\n"
    spec = _new_spec(data)
    with case.open() as handle:
        for index, (source, code) in enumerate(
            [
                ({spec["cas_id"]: bytearray(data)}, "namespace-input-unfrozen"),
                ({spec["cas_id"]: memoryview(data)}, "namespace-input-unfrozen"),
                ({spec["cas_id"]: case.source}, "namespace-input-unfrozen"),
                ({spec["cas_id"]: b"changed"}, "namespace-content-mismatch"),
                ({"not-a-content-id": data}, "namespace-request-invalid"),
                ({spec["cas_id"]: data, "unused": data}, "namespace-request-invalid"),
            ]
        ):
            request = _new_file(case, handle, data, name=f"transport-{index}")
            before = _cas_image(case.cas_root)
            with pytest.raises(n.NamespaceError, match="^" + code + "$") as error:
                n.execute_namespace(handle, request, source)
            assert error.value.effect == "none"
            assert n.namespace_operation(handle, request.operation_id).state == "unknown"
            assert _cas_image(case.cas_root) == before
            n.execute_namespace(handle, request, {spec["cas_id"]: data})
        assert len(journal.read_journal(case.cas_root)) == 2
        # A declared existing source counts as used but still adds no CAS event.
        assert len(handle.entries()) == 7

    case = Case(tmp_path / "captured-source-mapping")
    data = b"mapping captured exactly once"
    spec = _new_spec(data)

    class ChangingSource(Mapping):
        reads = 0

        def __iter__(self):
            return iter([spec["cas_id"]])

        def __len__(self):
            return 1

        def __getitem__(self, key):
            self.reads += 1
            assert key == spec["cas_id"]
            return data if self.reads == 1 else b"changed mapping value"

    with case.open() as handle:
        request = _new_file(case, handle, data)
        source = ChangingSource()
        case.session.active = False
        with pytest.raises(n.NamespaceError, match="namespace-session-expired"):
            n.execute_namespace(handle, request, source)
        assert source.reads == 0
        case.session.active = True
        n.execute_namespace(handle, request, source)
        assert source.reads == 1
        assert cas.object_path(case.cas_root, spec["cas_id"]).read_bytes() == data

    for mode in ("equal-revision", "same-request", "changed-request"):
        case = Case(tmp_path / ("content-race-" + mode))
        with case.open() as handle:
            payloads = [b"race A", b"race B"]
            a = _new_file(case, handle, payloads[0], name="A")
            b = _new_file(case, handle, payloads[1], name="B")
            if mode == "same-request":
                b, payloads[1] = a, payloads[0]
            elif mode == "changed-request":
                b = replace(b, operation_id=a.operation_id)
            barrier = threading.Barrier(2)

            def execute(pair):
                request, data = pair
                barrier.wait(timeout=10)
                try:
                    return n.execute_namespace(handle, request, {_new_spec(data)["cas_id"]: data})
                except n.NamespaceError as error:
                    return error.code

            with ThreadPoolExecutor(2) as pool:
                outcomes = list(pool.map(execute, zip([a, b], payloads, strict=True)))
            if mode == "same-request":
                assert outcomes[0] == outcomes[1] and isinstance(outcomes[0], n.MutationResult)
            else:
                assert sum(isinstance(x, n.MutationResult) for x in outcomes) == 1
                assert (
                    "namespace-revision-conflict"
                    if mode == "equal-revision"
                    else "namespace-operation-conflict"
                ) in outcomes
            assert handle.info().revision == 1 and len(_tree(handle)) == 2
            events = journal.read_journal(case.cas_root)
            assert len(events) == 2
            assert cas.verify(case.cas_root).ok
            if mode != "same-request":
                loser = next(payloads[i] for i, x in enumerate(outcomes) if isinstance(x, str))
                assert not cas.object_path(case.cas_root, _new_spec(loser)["cas_id"]).exists()


_CONTENT_CHILD = r"""
import json,os,runpy,sys
from pathlib import Path
from castle import namespace as n,namespace_store as store
from castle import namespace_content as content,cas_recovery as pub
root,cas_root,lock,example,raw,payload,point=sys.argv[1:]
root,cas_root=Path(root),Path(cas_root)
caller=runpy.run_path(example)
session=caller['LocalSession'](root,cas_root,Path(lock))
v=json.loads(raw)
request=n.MutationRequest(v['namespace_id'],n.StoreIdentity(**v['expected_cas']),v['expected_revision'],v['operation_id'],v['kind'],v['args'])
data=bytes.fromhex(payload)
with n.open_namespace(root,cas_root,session) as handle:
 original=store._commit
 def commit(connection):
  row=connection.execute(
   'SELECT state FROM operations WHERE operation_id=?',(request.operation_id,)).fetchone()
  if point=='before-final' and row==('committed',): os._exit(63)
  original(connection)
  if (point=='reserved' and row==('staging',) or point=='prepared' and row==('prepared',)
      or point=='after-final' and row==('committed',)): os._exit(63)
 store._commit=commit
 stage=content.write_stage
 def write_stage(*args):
  stage(*args)
  if point=='stage-complete': os._exit(63)
 content.write_stage=write_stage
 write=os.write
 def interrupted_write(fd,data):
  if point in {'stage-partial','publication-torn'}:
   for file in (root/'operations'/request.operation_id).iterdir():
    selected=(point=='stage-partial' and n._UUID.fullmatch(file.name) is not None
              or point=='publication-torn' and file.name.startswith('publication.')
              and file.suffix=='.json')
    if selected and os.fstat(fd).st_ino==file.stat().st_ino:
     write(fd,data[:max(1,len(data)//2)])
     os._exit(63)
  return write(fd,data)
 os.write=interrupted_write
 copy=pub._copy_object
 def copy_object(*args):
  copy(*args)
  if point=='cas-object': os._exit(63)
 pub._copy_object=copy_object
 pwrite=os.pwrite
 def append(fd,data,offset):
  if point=='cas-append':
   pwrite(fd,data[:len(data)//2],offset)
   os._exit(63)
  return pwrite(fd,data,offset)
 os.pwrite=append
 index=pub._index_commit
 def index_commit(connection):
  index(connection)
  if point=='cas-index': os._exit(63)
 pub._index_commit=index_commit
 unlink=Path.unlink
 def deleted(file,*args,**kwargs):
  unlink(file,*args,**kwargs)
  if file.parent.name==request.operation_id:
   if (point=='cleanup-stage' and n._UUID.fullmatch(file.name) is not None
       or point=='cleanup-record' and file.name.startswith('publication.')
       and file.suffix=='.json'): os._exit(63)
 Path.unlink=deleted
 receipt=handle._receipt_state
 def received(*args):
  receipt(*args)
  if point=='receipt': os._exit(63)
 handle._receipt_state=received
 n.execute_namespace(handle,request,{request.args['content']['cas_id']:data})
 if point=='response': os._exit(63)
 raise RuntimeError('point not reached')
"""


def _content_child(case, request, data, point):
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            _CONTENT_CHILD,
            str(case.root),
            str(case.cas_root),
            str(case.directory / "domain.lock"),
            str(_EXAMPLE),
            json.dumps(asdict(request)),
            data.hex(),
            point,
        ],
        env={**os.environ, "PYTHONPATH": str(Path(cas.__file__).parents[1])},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert child.returncode == 63, (point, child.stdout, child.stderr)


def _new_content_restart(tmp_path, monkeypatch):
    from castle import namespace_content as content

    for kind in ("create-file", "replace-content"):
        for point in (
            "reserved",
            "stage-partial",
            "stage-complete",
            "prepared",
            "publication-torn",
            "cas-object",
            "cas-append",
            "cas-index",
            "before-final",
            "after-final",
            "cleanup-stage",
            "cleanup-record",
            "receipt",
            "response",
        ):
            case = Case(tmp_path / (kind + "-" + point))
            data = b"new restart payload independently expected\n"
            spec = _new_spec(data)
            with case.open() as handle:
                entry_id = _id()
                if kind == "replace-content":
                    case.apply(
                        handle,
                        "create-file",
                        {
                            "entry_id": entry_id,
                            "parent_id": case.initial.root_id,
                            "name": "New",
                            "metadata": {"stable": True},
                            "content": {
                                "cas_id": case.stored.cas_id,
                                "size": case.source.stat().st_size,
                            },
                        },
                    )
                    request = case.request(handle, kind, {"entry_id": entry_id, "content": spec})
                else:
                    request = _new_file(
                        case, handle, data, entry_id=entry_id, metadata={"stable": True}
                    )
                before = _tree(handle)
                revision = handle.info().revision
            _content_child(case, request, data, point)
            operation = case.root / "operations" / request.operation_id
            evidence_before = (
                {p.name: p.read_bytes() for p in operation.iterdir()} if operation.exists() else {}
            )
            if point == "publication-torn":
                for action in (
                    case.open,
                    lambda: n.recover_namespace(case.root, case.cas_root, case.session, None),
                ):
                    with pytest.raises(
                        n.NamespaceError, match="namespace-storage-recovery-required"
                    ):
                        action()
                assert (
                    {p.name: p.read_bytes() for p in operation.iterdir()}
                    if operation.exists()
                    else {}
                ) == evidence_before
                assert not cas.object_path(case.cas_root, spec["cas_id"]).exists()
                continue
            if point not in {"receipt", "response"}:
                with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                    case.open()
                assert (
                    {p.name: p.read_bytes() for p in operation.iterdir()}
                    if operation.exists()
                    else {}
                ) == evidence_before
            if point == "before-final":
                # CAS is consistent but unreferenced; no namespace effect yet.
                assert cas.verify(case.cas_root).ok
                assert cas.object_path(case.cas_root, spec["cas_id"]).read_bytes() == data
                with sqlite3.connect(case.root / "namespace.sqlite") as db:
                    assert db.execute("SELECT revision FROM namespace").fetchone() == (revision,)
            recovered = n.recover_namespace(case.root, case.cas_root, case.session, None)
            incomplete_input = point in {"reserved", "stage-partial"}
            assert recovered.revision == revision + (0 if incomplete_input else 1)
            with case.open() as handle:
                if incomplete_input:
                    assert _tree(handle) == before
                    assert n.namespace_operation(handle, request.operation_id).state == "rejected"
                    with pytest.raises(n.NamespaceError, match="namespace-input-unfrozen"):
                        n.execute_namespace(handle, request, {spec["cas_id"]: data})
                    assert not cas.object_path(case.cas_root, spec["cas_id"]).exists()
                else:
                    result = n.execute_namespace(handle, request, {})
                    assert n.execute_namespace(handle, request, {}) == result
                    actual = _tree(handle)
                    assert set(actual) == set(before) | {entry_id}
                    assert actual[entry_id] == {
                        "entry_id": entry_id,
                        "kind": "file",
                        "parent_id": case.initial.root_id,
                        "name": "New",
                        "metadata": {"stable": True},
                        "cas_id": spec["cas_id"],
                        "size": len(data),
                        "changed_revision": revision + 1,
                    }
                    assert cas.object_path(case.cas_root, spec["cas_id"]).read_bytes() == data
                    assert n.namespace_operation(handle, request.operation_id).result == result
                assert {p.name for p in operation.iterdir()} == {"request.json", "receipt.json"}
            assert cas.verify(case.cas_root).ok
            from castle import journal

            assert len(journal.read_journal(case.cas_root)) == (1 if incomplete_input else 2)

    for point in (
        "stage-fsync",
        "stage-directory",
        "publication-sync",
        "final-before",
        "final-after",
        "cleanup",
        "complete-evidence",
    ):
        case = Case(tmp_path / ("content-fault-" + point))
        data = b"new fault bytes\n"
        spec = _new_spec(data)
        with case.open() as handle:
            request = _new_file(case, handle, data)
            fired = []
            fsync = os.fsync
            sync_dir = store._sync_dir
            commit = store._commit
            unlink = Path.unlink
            replace_file = os.replace

            def fail_sync(fd):
                info = os.fstat(fd)
                directory = case.root / "operations" / request.operation_id
                stages = (
                    [p for p in directory.iterdir() if n._UUID.fullmatch(p.name)]
                    if directory.exists()
                    else []
                )
                selected = point == "stage-fsync" and any(
                    info.st_ino == p.stat().st_ino for p in stages
                )
                selected |= (
                    point == "publication-sync"
                    and info.st_ino == (case.cas_root / "journal.jsonl").stat().st_ino
                    and info.st_size
                    > len((case.cas_root / "journal.jsonl").read_bytes().splitlines()[0]) + 1
                )
                if selected and not fired:
                    fired.append(True)
                    raise OSError("injected publication/stage sync")
                return fsync(fd)

            def fail_directory(directory):
                if (
                    point == "stage-directory"
                    and directory.name == request.operation_id
                    and any(n._UUID.fullmatch(p.name) for p in directory.iterdir())
                    and not fired
                ):
                    fired.append(True)
                    raise OSError("injected stage directory sync")
                return sync_dir(directory)

            def fail_commit(connection):
                row = connection.execute(
                    "SELECT state FROM operations WHERE operation_id=?", (request.operation_id,)
                ).fetchone()
                if point in {"final-before", "final-after"} and row == ("committed",) and not fired:
                    fired.append(True)
                    if point == "final-after":
                        commit(connection)
                    error = sqlite3.OperationalError("injected uncertain commit")
                    error.sqlite_errorcode = sqlite3.SQLITE_IOERR_FSYNC
                    raise error
                commit(connection)

            def fail_unlink(file, *args, **kwargs):
                unlink(file, *args, **kwargs)
                if (
                    point == "cleanup"
                    and file.parent.name == request.operation_id
                    and n._UUID.fullmatch(file.name)
                    and not fired
                ):
                    fired.append(True)
                    raise OSError("injected cleanup sync uncertainty")

            def fail_replace(source, destination):
                if (
                    point == "complete-evidence"
                    and Path(destination).name.startswith("publication.")
                    and not fired
                ):
                    if json.loads(Path(source).read_bytes())["state"] == "complete":
                        fired.append(True)
                        raise OSError("injected complete-evidence replace")
                return replace_file(source, destination)

            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", fail_sync)
                patch.setattr(store, "_sync_dir", fail_directory)
                patch.setattr(store, "_commit", fail_commit)
                patch.setattr(Path, "unlink", fail_unlink)
                patch.setattr(os, "replace", fail_replace)
                with pytest.raises(
                    n.NamespaceError, match="namespace-storage-recovery-required"
                ) as error:
                    n.execute_namespace(handle, request, {spec["cas_id"]: data})
                assert fired
                assert error.value.effect == (
                    "namespace-committed" if point == "cleanup" else "recovery-required"
                )
                assert (
                    n.namespace_operation(handle, request.operation_id).state == "recovery-required"
                )
        observed = []
        original_write = content.write_stage

        def fresh_stage(*args):
            observed.append(bytes(args[2]))
            return original_write(*args)

        with monkeypatch.context() as patch:
            patch.setattr(content, "write_stage", fresh_stage)
            assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 1
        if point in {"stage-fsync", "stage-directory"}:
            assert observed == [data]
        with case.open() as handle:
            assert n.execute_namespace(handle, request, {}).revision_after == 1
            assert cas.object_path(case.cas_root, spec["cas_id"]).read_bytes() == data


def _new_content_refusals(tmp_path, monkeypatch):
    from castle import namespace_content as content

    data = b"protected by owned staging\n"
    spec = _new_spec(data)
    for fault in ("unknown-payload", "missing-stage", "corrupt-stage", "persistent-stage-sync"):
        case = Case(tmp_path / ("new-content-refuse-" + fault))
        with case.open() as handle:
            request = _new_file(case, handle, data)
        _content_child(
            case,
            request,
            data,
            "stage-complete" if fault == "persistent-stage-sync" else "prepared",
        )
        operation = case.root / "operations" / request.operation_id
        with sqlite3.connect(case.root / "namespace.sqlite") as connection:
            recipe = n._decode(
                connection.execute(
                    "SELECT ownership_receipt FROM operations WHERE operation_id=?",
                    (request.operation_id,),
                ).fetchone()[0]
            )
        stage = operation / recipe["publication"]["request"]["staged_file_id"]
        witness = stage
        if fault == "unknown-payload":
            witness = operation / _id()
            witness.write_bytes(b"not owned by SQL recipe")
        elif fault == "missing-stage":
            stage.unlink()
        elif fault == "corrupt-stage":
            stage.chmod(0o600)
            stage.write_bytes(b"x" * len(data))
            stage.chmod(0o444)
        before = witness.read_bytes() if witness.exists() else None
        original = os.fsync

        def fail_stage(fd):
            if stage.exists() and os.fstat(fd).st_ino == stage.stat().st_ino:
                raise OSError("persistent stage fsync failure")
            return original(fd)

        with monkeypatch.context() as patch:
            if fault == "persistent-stage-sync":
                patch.setattr(os, "fsync", fail_stage)
            for _ in range(2):
                with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                    n.recover_namespace(case.root, case.cas_root, case.session, None)
        assert (witness.read_bytes() if witness.exists() else None) == before
        assert not cas.object_path(case.cas_root, spec["cas_id"]).exists()
        if fault == "persistent-stage-sync":
            assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 1

    for phase in ("before-reference", "before-acknowledge"):
        case = Case(tmp_path / ("new-content-revoke-" + phase))
        with case.open() as handle:
            request = _new_file(case, handle, data)
            with monkeypatch.context() as patch:
                if phase == "before-reference":
                    original = content.publish

                    def revoked(*args):
                        receipt = original(*args)
                        case.session.active = False
                        return receipt

                    patch.setattr(content, "publish", revoked)
                else:
                    original = handle._receipt_state

                    def revoked(*args):
                        original(*args)
                        case.session.active = False

                    patch.setattr(handle, "_receipt_state", revoked)
                with pytest.raises(n.NamespaceError, match="namespace-session-expired") as refusal:
                    n.execute_namespace(handle, request, {spec["cas_id"]: data})
                assert refusal.value.effect == (
                    "recovery-required" if phase == "before-reference" else "namespace-committed"
                )
                assert handle.db.execute("SELECT revision FROM namespace").fetchone() == (
                    (0,) if phase == "before-reference" else (1,)
                )
            case.session.active = True  # caller-fixture renewal, no credentials/grants
        assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 1
        with case.open() as handle:
            assert n.execute_namespace(handle, request, {}).revision_after == 1


def _new_content_advice_proofs(tmp_path, monkeypatch):
    data = b"complete stage even after another failed recovery\n"
    spec = _new_spec(data)
    for mode in ("short", "zero"):
        case = Case(tmp_path / ("stage-write-" + mode))
        with case.open() as handle:
            request = _new_file(case, handle, data)
            original_write, original_open = os.write, os.open
            fired, creation_flags = [], []
            operation = case.root / "operations" / request.operation_id

            def opened(file, flags, *args, **kwargs):
                if (
                    isinstance(file, (str, Path))
                    and Path(file).parent.resolve() == operation.resolve()
                    and flags & os.O_WRONLY
                    and n._UUID.fullmatch(Path(file).name)
                ):
                    creation_flags.append(flags)
                return original_open(file, flags, *args, **kwargs)

            def write(fd, chunk):
                stages = (
                    [p for p in operation.iterdir() if n._UUID.fullmatch(p.name)]
                    if operation.exists()
                    else []
                )
                if not fired and any(os.fstat(fd).st_ino == p.stat().st_ino for p in stages):
                    fired.append(True)
                    return (
                        0
                        if mode == "zero"
                        else original_write(fd, chunk[: max(1, len(chunk) // 2)])
                    )
                return original_write(fd, chunk)

            with monkeypatch.context() as patch:
                patch.setattr(os, "write", write)
                patch.setattr(os, "open", opened)
                if mode == "short":
                    assert (
                        n.execute_namespace(handle, request, {spec["cas_id"]: data}).revision_after
                        == 1
                    )
                    assert cas.object_path(case.cas_root, spec["cas_id"]).read_bytes() == data
                else:
                    with pytest.raises(
                        n.NamespaceError, match="namespace-storage-recovery-required"
                    ):
                        n.execute_namespace(handle, request, {spec["cas_id"]: data})
                    assert handle.db.execute(
                        "SELECT state FROM operations WHERE operation_id=?", (request.operation_id,)
                    ).fetchone() == ("staging",)
                    assert handle.db.execute("SELECT revision FROM namespace").fetchone() == (0,)
            assert fired and len(creation_flags) == 1, (mode, fired, creation_flags)
            assert creation_flags[0] & os.O_EXCL and not creation_flags[0] & os.O_TRUNC
        if mode == "zero":
            assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 0
            with case.open() as handle:
                assert (
                    n.namespace_operation(handle, request.operation_id).error_code
                    == "namespace-input-unfrozen"
                )
                assert not cas.object_path(case.cas_root, spec["cas_id"]).exists()

    case = Case(tmp_path / "in-place-stage-recovery")
    with case.open() as handle:
        request = _new_file(case, handle, data)
    _content_child(case, request, data, "stage-complete")
    operation = case.root / "operations" / request.operation_id
    stage = next(p for p in operation.iterdir() if n._UUID.fullmatch(p.name))
    original_pwrite = os.pwrite
    fired = []

    def interrupted(fd, chunk, offset):
        if os.fstat(fd).st_ino == stage.stat().st_ino and not fired:
            fired.append(True)
            original_pwrite(fd, chunk[: len(chunk) // 2], offset)
            raise OSError("recovery's own partial rewrite failed")
        return original_pwrite(fd, chunk, offset)

    with monkeypatch.context() as patch:
        patch.setattr(os, "pwrite", interrupted)
        with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
            n.recover_namespace(case.root, case.cas_root, case.session, None)
    assert fired and stage.read_bytes() == data
    assert not cas.object_path(case.cas_root, spec["cas_id"]).exists()
    assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 1

    case = Case(tmp_path / "source-precedence")

    class BrokenSource(Mapping):
        reads = 0
        failure = AttributeError

        def __iter__(self):
            return iter([spec["cas_id"]])

        def __len__(self):
            return 1

        def __getitem__(self, key):
            self.reads += 1
            raise self.failure("caller source failure must stay private")

    with case.open() as handle:
        for name, binding, revision, code in [
            ("stale", handle.info().cas, 99, "namespace-revision-conflict"),
            (
                "foreign",
                replace(handle.info().cas, store_id=_id()),
                0,
                "namespace-binding-mismatch",
            ),
        ]:
            request = replace(
                _new_file(case, handle, data, name=name),
                expected_cas=binding,
                expected_revision=revision,
            )
            source = BrokenSource()
            for _ in range(2):
                with pytest.raises(n.NamespaceError, match=code):
                    n.execute_namespace(handle, request, source)
            assert source.reads == 0
            assert n.namespace_operation(handle, request.operation_id).state == "rejected"
        for index, exception in enumerate((KeyError, AttributeError, ValueError)):
            request = _new_file(case, handle, data, name=f"callback-{index}")
            source = BrokenSource()
            source.failure = exception
            with pytest.raises(n.NamespaceError, match="^namespace-request-invalid$") as error:
                n.execute_namespace(handle, request, source)
            assert error.value.effect == "none" and source.reads == 1
            assert n.namespace_operation(handle, request.operation_id).state == "unknown"
            n.execute_namespace(handle, request, {spec["cas_id"]: data})
        request = _new_file(case, handle, data, name="bad-mapping")
        with pytest.raises(n.NamespaceError, match="^namespace-request-invalid$"):
            n.execute_namespace(handle, request, None)
        assert n.namespace_operation(handle, request.operation_id).state == "unknown"

    case = Case(tmp_path / "post-publication-invariant")
    with case.open() as handle:
        request = _new_file(case, handle, data)
        original = tx._changes

        def changed(h, value, info, **kwargs):
            if request.operation_id in h.publication_receipts:
                n._refuse("namespace-name-conflict")
            return original(h, value, info, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(tx, "_changes", changed)
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                n.execute_namespace(handle, request, {spec["cas_id"]: data})
        assert handle.db.execute(
            "SELECT state FROM operations WHERE operation_id=?", (request.operation_id,)
        ).fetchone() == ("prepared",)
        assert handle.db.execute("SELECT revision FROM namespace").fetchone() == (0,)
        assert cas.verify(case.cas_root).ok
    assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 1

    for fault in ("staging-shape", "pending-revision", "orphan-completion-temp"):
        case = Case(tmp_path / ("authority-" + fault))
        with case.open() as handle:
            request = _new_file(case, handle, data)
        _content_child(
            case,
            request,
            data,
            "cleanup-record" if fault == "orphan-completion-temp" else "reserved",
        )
        with sqlite3.connect(case.root / "namespace.sqlite") as connection:
            raw = connection.execute(
                "SELECT ownership_receipt FROM operations WHERE operation_id=?",
                (request.operation_id,),
            ).fetchone()[0]
            recipe = n._decode(raw)
            if fault == "staging-shape":
                recipe["publication"] = None
                connection.execute(
                    "UPDATE operations SET ownership_receipt=? WHERE operation_id=?",
                    (n.canonical_bytes(recipe).decode(), request.operation_id),
                )
            elif fault == "pending-revision":
                connection.execute("UPDATE namespace SET revision=1")
        witness = None
        if fault == "orphan-completion-temp":
            publication_id = recipe["publication"]["request"]["publication_id"]
            witness = (
                case.root
                / "operations"
                / request.operation_id
                / f"publication.{publication_id}.tmp"
            )
            witness.write_bytes(b"unproved replacement bytes")
        with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
            n.recover_namespace(case.root, case.cas_root, case.session, None)
        if witness is not None:
            assert witness.read_bytes() == b"unproved replacement bytes"


def _new_receipt_shape_refusal(tmp_path):
    case = Case(tmp_path / "retained-receipt-shape")
    data = b"closed persisted publication receipt"
    with case.open() as handle:
        request = _new_file(case, handle, data)
        n.execute_namespace(handle, request, {_new_spec(data)["cas_id"]: data})
    with sqlite3.connect(case.root / "namespace.sqlite") as connection:
        raw = connection.execute(
            "SELECT ownership_receipt FROM operations WHERE operation_id=?", (request.operation_id,)
        ).fetchone()[0]
        recipe = n._decode(raw)
        recipe["publication"]["receipt"]["unknown_field"] = True
        connection.execute(
            "UPDATE operations SET ownership_receipt=? WHERE operation_id=?",
            (n.canonical_bytes(recipe).decode(), request.operation_id),
        )
    for action in (
        case.open,
        lambda: n.recover_namespace(case.root, case.cas_root, case.session, None),
    ):
        with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
            action()
