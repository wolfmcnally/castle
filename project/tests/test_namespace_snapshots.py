"""Independent folder/snapshot protocols run by the retained namespace families."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, fields, replace
from pathlib import Path

import pytest

from castle import cas, journal
from castle import cas_recovery as publication
from castle import namespace as n
from castle import namespace_content as content
from castle import namespace_snapshots as snapshots


def identity():
    return str(uuid.uuid4())


def forest(root):
    folder, empty, a, b = [identity() for _ in range(4)]
    raw = [b"independent folder A\n", b"independent folder B\n"]
    sources = {"sha256:" + hashlib.sha256(v).hexdigest(): v for v in raw}
    entries = [
        dict(
            entry_id=folder,
            kind="directory",
            parent_id=root,
            name="Folder",
            metadata={},
            cas_id=None,
            size=None,
        ),
        dict(
            entry_id=empty,
            kind="directory",
            parent_id=folder,
            name="Empty",
            metadata={},
            cas_id=None,
            size=None,
        ),
    ]
    entries += [
        dict(
            entry_id=key,
            kind="file",
            parent_id=folder,
            name=name,
            metadata={"integer": n.MAX_INTEGER},
            cas_id=key_id,
            size=len(sources[key_id]),
        )
        for key, name, key_id in zip([a, b], ["A", "B"], sources)
    ]
    return entries, sources


def semantic_proof(tmp_path, monkeypatch, Case, tree):
    bound_proof()
    advice_semantic_proof(tmp_path, monkeypatch, Case, tree)
    case = Case(tmp_path / "folder-snapshot")
    with case.open() as h:
        entries, sources = forest(case.initial.root_id)
        request = case.request(
            h,
            "import-folder",
            {"parent_id": case.initial.root_id, "entries": list(reversed(entries))},
        )
        result = n.execute_namespace(h, request, sources)
        assert (
            result.affected_entry_ids == sorted(e["entry_id"] for e in entries)
            and result.revision_after == 1
        )
        assert tree(h) == {
            case.initial.root_id: asdict(
                n.EntryRecord(case.initial.root_id, "directory", None, None, {}, None, None, 0)
            ),
            **{e["entry_id"]: {**e, "changed_revision": 1} for e in entries},
        }
        assert (
            n.execute_namespace(h, replace(request, args={**request.args, "entries": entries}), {})
            == result
        )
        before_records = journal.read_journal(case.cas_root)
        sid = identity()
        capture = case.request(h, "snapshot", {"snapshot_id": sid})
        captured = n.execute_namespace(h, capture, {})
        record = captured.snapshot
        assert (
            record.snapshot_id == sid
            and record.captured_revision == 1
            and captured.revision_after == 2
            and captured.affected_entry_ids == []
        )
        expected = {
            "schema": "castle.namespace_snapshot.v1",
            "namespace_id": case.initial.namespace_id,
            "policy": case.initial.policy,
            "captured_revision": 1,
            "root_id": case.initial.root_id,
            "cas_binding": asdict(case.initial.expected_cas),
            "origin": {"kind": "capture"},
            "entries": [tree(h)[key] for key in sorted(tree(h))],
        }
        # Independent stdlib encoding is equivalent for this fixture's safe strings;
        # separate literal scalar/control fixtures below distinguish its general encoder.
        expected_bytes = (
            json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode()
        assert cas.object_path(case.cas_root, record.cas_id).read_bytes() == expected_bytes
        assert record.cas_id == "sha256:" + hashlib.sha256(
            expected_bytes
        ).hexdigest() and record.size == len(expected_bytes)
        inspected = n.read_namespace(
            h,
            n.ReadRequest(
                case.initial.namespace_id,
                h.info().cas,
                None,
                "inspect-snapshot",
                {"snapshot_id": sid},
            ),
        ).value
        assert inspected == n.SnapshotView(record, expected)
        wanted = {("snapshot", sid, record.cas_id, "manifest")} | {
            ("snapshot", sid, key, "member") for key in sources
        }
        assert set(h.db.execute("SELECT * FROM dependencies WHERE owner_kind='snapshot'")) == wanted
        assert set(
            h.db.execute(
                "SELECT cas_id FROM dependencies WHERE owner_kind='operation' AND owner_id=?",
                (request.operation_id,),
            )
        ) == {(key,) for key in sources}
        assert set(
            h.db.execute(
                "SELECT cas_id FROM dependencies WHERE owner_kind='operation' AND owner_id=?",
                (capture.operation_id,),
            )
        ) == {(record.cas_id,)}
        assert len(journal.read_journal(case.cas_root)) == len(before_records) + 1
        # Current mutation never edits the explicit historic capture.
        file_id = entries[2]["entry_id"]
        case.apply(h, "delete-entry", {"entry_id": file_id})
        assert n.execute_namespace(h, capture, {}) == captured
        assert (
            n.read_namespace(
                h,
                n.ReadRequest(
                    case.initial.namespace_id,
                    h.info().cas,
                    None,
                    "inspect-snapshot",
                    {"snapshot_id": sid},
                ),
            ).value.manifest
            == expected
        )
        saved_reader = []

        class Destination:
            def publish(self, view, raw, read_content):
                assert not h.db.in_transaction and raw == expected_bytes
                for key, data in sources.items():
                    assert read_content(key) == data
                for action in (h.info, h.close):
                    with pytest.raises(n.NamespaceError, match="namespace-coordinator-busy"):
                        action()
                saved_reader.append(read_content)
                return n.ExportReceipt(sid, record.cas_id)

        read = n.ReadRequest(
            case.initial.namespace_id, h.info().cas, None, "export-snapshot", {"snapshot_id": sid}
        )
        rev = h.info().revision
        assert n.read_namespace(h, read, destination=Destination()).value == n.ExportReceipt(
            sid, record.cas_id
        )
        assert h.info().revision == rev
        with pytest.raises(n.NamespaceError, match="namespace-access-denied"):
            saved_reader[0](next(iter(sources)))

        class Broken:
            def publish(self, *args):
                raise OSError("private destination path must not escape")

        with pytest.raises(n.NamespaceError, match="^namespace-io-failed$"):
            n.read_namespace(h, read, destination=Broken())
        assert h.info().revision == rev
        for req, dest in [(read, None), (replace(read, kind="inspect-snapshot"), Destination())]:
            with pytest.raises(n.NamespaceError, match="namespace-request-invalid"):
                n.read_namespace(h, req, destination=dest)
        deletion = case.request(h, "delete-snapshot", {"snapshot_id": sid})
        deleted = n.execute_namespace(h, deletion, {})
        assert deleted.snapshot is None and deleted.affected_entry_ids == []
        assert (
            snapshots._retained(h)[0].snapshot == record and snapshots._retained(h)[0].live is False
        )
        assert set(h.db.execute("SELECT * FROM dependencies WHERE owner_kind='snapshot'")) == wanted
        assert cas.object_path(case.cas_root, record.cas_id).read_bytes() == expected_bytes
        for kind in ("inspect-snapshot", "export-snapshot"):
            with pytest.raises(n.NamespaceError, match="namespace-entry-missing"):
                n.read_namespace(
                    h,
                    replace(read, kind=kind),
                    destination=Destination() if kind == "export-snapshot" else None,
                )
        with pytest.raises(n.NamespaceError, match="namespace-request-invalid"):
            case.apply(
                h,
                "create-directory",
                {
                    "entry_id": sid,
                    "parent_id": case.initial.root_id,
                    "name": "Reuse",
                    "metadata": {},
                },
            )
    with case.open() as h:
        assert n.execute_namespace(h, capture, {}) == captured
        assert snapshots._retained(h)[0].dependencies == sorted({record.cas_id} | set(sources))

    _redacted_fixtures(tmp_path, Case, tree)

    for fault, code in [
        ("missing", "namespace-entry-missing"),
        ("file-parent", "namespace-kind-mismatch"),
        ("cycle", "namespace-cycle"),
        ("collision", "namespace-name-conflict"),
        ("reused", "namespace-request-invalid"),
        ("outside", "namespace-request-invalid"),
    ]:
        case = Case(
            tmp_path / ("forest-refusal-" + fault),
            root_id="ffffffff-ffff-4fff-bfff-ffffffffffff" if fault == "reused" else None,
        )
        with case.open() as h:
            entries, sources = forest(case.initial.root_id)
            other = identity()
            if fault == "outside":
                case.apply(
                    h,
                    "create-directory",
                    dict(entry_id=other, parent_id=case.initial.root_id, name="Other", metadata={}),
                )
            if fault == "missing":
                entries[0]["parent_id"] = identity()
            elif fault == "file-parent":
                entries[1]["parent_id"] = entries[2]["entry_id"]
            elif fault == "cycle":
                entries[0]["parent_id"] = entries[1]["entry_id"]
            elif fault == "collision":
                entries[3]["name"] = "a"
            elif fault == "reused":
                entries[0]["entry_id"] = case.initial.root_id
            elif fault == "outside":
                entries[0]["parent_id"] = other
            before = tree(h)
            journal_before = (case.cas_root / "journal.jsonl").read_bytes()
            rev = h.info().revision
            request = case.request(
                h, "import-folder", dict(parent_id=case.initial.root_id, entries=entries)
            )
            for _ in range(2):
                with pytest.raises(n.NamespaceError, match=code):
                    n.execute_namespace(h, request, sources)
            assert (
                tree(h) == before
                and h.info().revision == rev
                and (case.cas_root / "journal.jsonl").read_bytes() == journal_before
            )
            assert n.namespace_operation(h, request.operation_id).state == "rejected"
            assert set(
                h.db.execute(
                    "SELECT cas_id FROM dependencies WHERE owner_kind='operation' AND owner_id=?",
                    (request.operation_id,),
                )
            ) == {(key,) for key in sources}

    for mode in ("equal", "same", "changed"):
        case = Case(tmp_path / ("forest-race-" + mode))
        with case.open() as h:
            entries, sources = forest(case.initial.root_id)
            a = case.request(
                h, "import-folder", dict(parent_id=case.initial.root_id, entries=entries)
            )
            b = replace(a, args={**a.args, "entries": list(reversed(entries))})
            if mode != "same":
                entries2, sources2 = forest(case.initial.root_id)
                entries2[0]["name"] = "Other"
                b = replace(
                    a,
                    operation_id=identity() if mode == "equal" else a.operation_id,
                    args={"parent_id": case.initial.root_id, "entries": entries2},
                )
            else:
                sources2 = sources
            barrier = threading.Barrier(2)

            def execute(pair):
                req, data = pair
                barrier.wait(timeout=10)
                try:
                    return n.execute_namespace(h, req, data)
                except n.NamespaceError as e:
                    return e.code

            with ThreadPoolExecutor(2) as pool:
                outcomes = list(pool.map(execute, [(a, sources), (b, sources2)]))
            if mode == "same":
                assert outcomes[0] == outcomes[1] and isinstance(outcomes[0], n.MutationResult)
            else:
                assert (
                    sum(isinstance(x, n.MutationResult) for x in outcomes) == 1
                    and (
                        "namespace-revision-conflict"
                        if mode == "equal"
                        else "namespace-operation-conflict"
                    )
                    in outcomes
                )
            assert (
                h.info().revision == 1
                and len(tree(h)) == 5
                and len(journal.read_journal(case.cas_root)) == 3
            )

    case = Case(tmp_path / "snapshot-existing-payload")
    with case.open() as h:
        sid = identity()
        record, raw = snapshots.capture(h, sid)
    file = case.directory / "preexisting-manifest"
    file.write_bytes(raw)
    cas.ingest_file(case.cas_root, file, "explicit-fixture")
    before = (case.cas_root / "journal.jsonl").read_bytes()
    with case.open() as h:
        result = case.apply(h, "snapshot", {"snapshot_id": sid})
        assert (
            result.snapshot == record and (case.cas_root / "journal.jsonl").read_bytes() == before
        )
        assert (
            content.decode_recipe(
                h.db.execute(
                    "SELECT ownership_receipt FROM operations WHERE operation_id=?",
                    (result.operation_id,),
                ).fetchone()[0]
            )["publication"]
            == []
        )

    case = Case(tmp_path / "forest-source-shapes")
    with case.open() as h:
        entries, sources = forest(case.initial.root_id)
        request = case.request(
            h, "import-folder", dict(parent_id=case.initial.root_id, entries=entries)
        )
        for mapping in (
            {},
            {next(iter(sources)): next(iter(sources.values()))},
            {key: bytearray(raw) for key, raw in sources.items()},
            {key: b"wrong" for key in sources},
        ):
            with pytest.raises(
                n.NamespaceError, match="namespace-(input-unfrozen|content-mismatch)"
            ):
                n.execute_namespace(h, request, mapping)
            assert n.namespace_operation(h, request.operation_id).state == "unknown"
        bad = replace(
            request,
            args={
                **request.args,
                "entries": entries
                + [dict(entries[2], entry_id=identity(), name="Different", size=1)],
            },
        )
        with pytest.raises(n.NamespaceError, match="namespace-request-invalid"):
            n.execute_namespace(h, bad, sources)
        # Bound checks occur before reservation/SQL, including future receipt bytes.
        with monkeypatch.context() as patch:
            patch.setattr(content, "OWNERSHIP_LIMIT", 256)
            with pytest.raises(n.NamespaceError, match="namespace-limit-exceeded"):
                n.execute_namespace(h, request, sources)
        assert (
            not h.db.in_transaction
            and n.namespace_operation(h, request.operation_id).state == "unknown"
            and h.info().revision == 0
        )
        n.execute_namespace(h, request, sources)
        # Capture source encodings use exact scalar/control bytes independently.
        assert (
            n.canonical_bytes({"\ue000": "\n", "\U00010000": "é", "min": -n.MAX_INTEGER})
            == '{"min":-9007199254740991,"\ue000":"\\u000a","\U00010000":"é"}\n'.encode()
        )


def restart_proof(tmp_path, monkeypatch, Case, tree, child):
    recovery_edge_proof(tmp_path, monkeypatch, Case, child)
    isolated_catalog_proof(tmp_path, monkeypatch, Case)
    typed_outcome_proof(tmp_path, Case)
    recovery_receipt_guard_proof(tmp_path, monkeypatch, Case, child)
    for point in (
        "reserved",
        "stage-second-partial",
        "stage-complete",
        "prepared",
        "first-complete",
        "temp-second",
        "append-second",
        "index-second",
        "before-final",
        "after-final",
        "cleanup-stage",
        "cleanup-record",
        "receipt",
        "response",
    ):
        case = Case(tmp_path / ("forest-kill-" + point))
        with case.open() as h:
            entries, sources = forest(case.initial.root_id)
            request = case.request(
                h, "import-folder", dict(parent_id=case.initial.root_id, entries=entries)
            )
            before = tree(h)
        child(case, request, sources, point)
        directory = case.root / "operations" / request.operation_id
        prior_complete = [
            json.loads(p.read_bytes())
            for p in directory.glob("publication.*.json")
            if json.loads(p.read_bytes())["state"] == "complete"
        ]
        if point not in {"receipt", "response"}:
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                case.open()
        incomplete = point in {"reserved", "stage-second-partial", "stage-complete"}
        outcome = n.recover_namespace(case.root, case.cas_root, case.session, None)
        assert outcome.revision == (0 if incomplete else 1)
        with case.open() as h:
            if incomplete:
                assert (
                    tree(h) == before
                    and n.namespace_operation(h, request.operation_id).error_code
                    == "namespace-input-unfrozen"
                )
                assert len(journal.read_journal(case.cas_root)) == 1
            else:
                result = n.execute_namespace(h, request, {})
                assert result.affected_entry_ids == sorted(e["entry_id"] for e in entries)
                assert tree(h) == {
                    **before,
                    **{e["entry_id"]: {**e, "changed_revision": 1} for e in entries},
                }
                assert len(journal.read_journal(case.cas_root)) == 3
                for key, raw in sources.items():
                    assert cas.object_path(case.cas_root, key).read_bytes() == raw
                assert n.execute_namespace(h, request, {}) == result
                saved = content.decode_recipe(
                    h.db.execute(
                        "SELECT ownership_receipt FROM operations WHERE operation_id=?",
                        (request.operation_id,),
                    ).fetchone()[0]
                )
                receipts = {
                    item["request"]["publication_id"]: item["receipt"]
                    for item in saved["publication"]
                }
                for evidence in prior_complete:
                    assert receipts[evidence["publication_id"]]["event_ids"] == [
                        event["event_id"] for event in evidence["events"]
                    ]
            assert {
                p.name for p in (case.root / "operations" / request.operation_id).iterdir()
            } == {"request.json", "receipt.json"}
        assert cas.verify(case.cas_root).ok

    for point in (
        "reserved",
        "stage-partial",
        "stage-complete",
        "prepared",
        "cas-append",
        "before-final",
        "after-final",
        "cleanup-stage",
        "cleanup-record",
        "response",
    ):
        case = Case(tmp_path / ("snapshot-kill-" + point))
        with case.open() as h:
            request = case.request(h, "snapshot", {"snapshot_id": identity()})
            before = tree(h)
        child(case, request, {}, point)
        receipt = n.recover_namespace(case.root, case.cas_root, case.session, None)
        assert receipt.revision == 1
        with case.open() as h:
            result = n.execute_namespace(h, request, {})
            assert (
                result.snapshot.captured_revision == 0
                and result.revision_after == 1
                and tree(h) == before
            )
            assert len(journal.read_journal(case.cas_root)) == 2
            view = n.read_namespace(
                h,
                n.ReadRequest(
                    case.initial.namespace_id,
                    h.info().cas,
                    None,
                    "inspect-snapshot",
                    {"snapshot_id": request.args["snapshot_id"]},
                ),
            ).value
            assert view.manifest["entries"] == [before[key] for key in sorted(before)]

    # Retained snapshot dependencies and actual manifest bytes remain mandatory.
    for fault in ("dependency", "manifest"):
        case = Case(tmp_path / ("snapshot-refusal-" + fault))
        with case.open() as h:
            result = case.apply(h, "snapshot", {"snapshot_id": identity()})
        if fault == "dependency":
            with sqlite3.connect(case.root / "namespace.sqlite") as db:
                db.execute("DELETE FROM dependencies WHERE owner_kind='snapshot'")
        else:
            file = cas.object_path(case.cas_root, result.snapshot.cas_id)
            file.chmod(0o600)
            file.write_bytes(b"x" * result.snapshot.size)
            file.chmod(0o444)
        with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
            case.open()


def _redacted_fixtures(tmp_path, Case, tree):
    case = Case(tmp_path / "redacted-validation")
    with case.open() as h:
        entries, sources = forest(case.initial.root_id)
        n.execute_namespace(
            h,
            case.request(h, "import-folder", dict(parent_id=case.initial.root_id, entries=entries)),
            sources,
        )
        record = case.apply(h, "snapshot", {"snapshot_id": identity()}).snapshot
        original = snapshots.decode(h, record)
    operation = identity()
    origin = {"kind": "redacted", "operation_id": operation}
    fixtures = []
    for removed in (
        {entries[2]["entry_id"]},
        {entries[1]["entry_id"], entries[2]["entry_id"], entries[3]["entry_id"]},
    ):
        value = {
            **original,
            "origin": origin,
            "entries": [row for row in original["entries"] if row["entry_id"] not in removed],
        }
        fixtures.append((value, True))
    fixtures += [
        ({**fixtures[0][0], "origin": {"kind": "capture"}}, False),
        ({**fixtures[0][0], "captured_revision": 2}, False),
        ({**fixtures[0][0], "schema": "castle.namespace_snapshot.v99"}, False),
    ]
    orphan = json.loads(json.dumps(fixtures[0][0]))
    orphan["entries"] = [
        row for row in orphan["entries"] if row["entry_id"] != entries[0]["entry_id"]
    ]
    fixtures.append((orphan, False))
    changed = json.loads(json.dumps(fixtures[0][0]))
    changed["entries"][-1]["changed_revision"] = 2
    fixtures.append((changed, False))
    for i, (value, valid) in enumerate(fixtures):
        raw = (
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode()
        file = case.directory / f"fixture-{i}"
        file.write_bytes(raw)
        stored = cas.ingest_file(case.cas_root, file, "validator-fixture")
        candidate = n.SnapshotRecord(identity(), stored.cas_id, len(raw), 1)
        with case.open() as h:
            if valid:
                decoded = snapshots.decode(h, candidate, expected_origin=origin)
                assert (
                    decoded["captured_revision"] == 1
                    and decoded["origin"] == origin
                    and {e["entry_id"] for e in decoded["entries"]}
                    == {e["entry_id"] for e in value["entries"]}
                )
            else:
                with pytest.raises(
                    n.NamespaceError,
                    match="namespace-(storage-recovery-required|format-unsupported)",
                ):
                    snapshots.decode(h, candidate, expected_origin=origin)


def recovery_edge_proof(tmp_path, monkeypatch, Case, child):
    import os

    from castle import namespace_store as store

    case = Case(tmp_path / "generated-stage-fsync")
    with case.open() as h:
        request = case.request(h, "snapshot", {"snapshot_id": identity()})
    child(case, request, {}, "stage-partial")
    directory = case.root / "operations" / request.operation_id
    stage = next(p for p in directory.iterdir() if n._UUID.fullmatch(p.name))
    fsync = os.fsync
    fired = []

    def failed(fd):
        if os.fstat(fd).st_ino == stage.stat().st_ino:
            fired.append(True)
            raise OSError("persistent generated-stage sync failure")
        return fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", failed)
        for _ in range(2):
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                n.recover_namespace(case.root, case.cas_root, case.session, None)
    assert fired and stage.is_file() and len(journal.read_journal(case.cas_root)) == 1
    assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 1

    for fault in ("gap", "changed-generated-prefix"):
        case = Case(tmp_path / ("inventory-refusal-" + fault))
        with case.open() as h:
            if fault == "gap":
                entries, sources = forest(case.initial.root_id)
                request = case.request(
                    h, "import-folder", {"parent_id": case.initial.root_id, "entries": entries}
                )
            else:
                sources = {}
                request = case.request(h, "snapshot", {"snapshot_id": identity()})
        child(case, request, sources, "index-second" if fault == "gap" else "stage-partial")
        directory = case.root / "operations" / request.operation_id
        if fault == "gap":
            rows = [(json.loads(p.read_bytes()), p) for p in directory.glob("publication.*.json")]
            first = next(p for value, p in rows if value["state"] == "complete")
            first.unlink()
        else:
            stage = next(p for p in directory.iterdir() if n._UUID.fullmatch(p.name))
            stage.chmod(0o600)
            stage.write_bytes(b"X" + stage.read_bytes()[1:])
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
            n.recover_namespace(case.root, case.cas_root, case.session, None)
        assert {p.name: p.read_bytes() for p in directory.iterdir()} == before
        with sqlite3.connect(case.root / "namespace.sqlite") as db:
            assert db.execute("SELECT revision FROM namespace").fetchone() == (0,)

    for after in (False, True):
        case = Case(tmp_path / ("forest-uncertain-" + str(after)))
        with case.open() as h:
            entries, sources = forest(case.initial.root_id)
            request = case.request(
                h, "import-folder", {"parent_id": case.initial.root_id, "entries": entries}
            )
            commit = store._commit
            fired = []

            def uncertain(db):
                row = db.execute(
                    "SELECT state FROM operations WHERE operation_id=?", (request.operation_id,)
                ).fetchone()
                if row == ("committed",) and not fired:
                    fired.append(True)
                    if after:
                        commit(db)
                    error = sqlite3.OperationalError("unknown commit durability")
                    error.sqlite_errorcode = sqlite3.SQLITE_IOERR_FSYNC
                    raise error
                commit(db)

            with monkeypatch.context() as patch:
                patch.setattr(store, "_commit", uncertain)
                with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                    n.execute_namespace(h, request, sources)
            assert fired
        assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 1
        with case.open() as h:
            assert n.execute_namespace(h, request, {}).revision_after == 1 and len(h.entries()) == 5


def bound_proof():
    # Independent ASCII JSON accounting bounds every legal fixed-size item;
    # the runtime limit is private and must cover the full admitted entry count.
    binding = {
        "store_id": "f" * 36,
        "lineage_id": "f" * 36,
        "epoch_id": "f" * 36,
        "generation": n.MAX_INTEGER,
        "predecessor_epoch_id": "f" * 36,
    }
    request = dict(
        namespace_id="f" * 36,
        operation_id="f" * 36,
        publication_id="f" * 36,
        cas_binding=binding,
        cas_id="sha256:" + "f" * 64,
        size=n.MAX_INTEGER,
        staged_file_id="f" * 36,
    )
    receipt = {
        key: request[key]
        for key in ("publication_id", "operation_id", "cas_binding", "cas_id", "size")
    }
    receipt.update(
        event_ids=["v1:" + "9" * 16 + ":sha256:" + "f" * 64],
        journal_length=n.MAX_INTEGER,
        journal_sha256="f" * 64,
    )
    assert set(request) == {field.name for field in fields(publication.PublicationRequest)}
    assert set(receipt) == {field.name for field in fields(publication.PublicationReceipt)}
    item = json.dumps(
        {"request": request, "receipt": receipt}, separators=(",", ":"), sort_keys=True
    ).encode()
    assert (
        len(item) + 1 < 4096
        and (len(item) + 1) * n.MAX_ENTRIES + n.MAX_REQUEST_BYTES <= content.OWNERSHIP_LIMIT
    )


def advice_semantic_proof(tmp_path, monkeypatch, Case, tree):
    import os
    import signal

    case = Case(tmp_path / "export-negative-boundaries")
    with case.open() as h:
        entries, sources = forest(case.initial.root_id)
        n.execute_namespace(
            h,
            case.request(h, "import-folder", dict(parent_id=case.initial.root_id, entries=entries)),
            sources,
        )
        record = case.apply(h, "snapshot", {"snapshot_id": identity()}).snapshot
        read = n.ReadRequest(
            case.initial.namespace_id,
            h.info().cas,
            None,
            "export-snapshot",
            {"snapshot_id": record.snapshot_id},
        )
        for mode in (
            "nonmember",
            "manifest",
            "thread",
            "wrong-cas",
            "wrong-id",
            "wrong-type",
            "lock",
            "fork",
        ):

            class Destination:
                def publish(self, view, raw, reader):
                    if mode in {"nonmember", "manifest"}:
                        with pytest.raises(n.NamespaceError, match="namespace-access-denied"):
                            reader(case.stored.cas_id if mode == "nonmember" else record.cas_id)
                    elif mode == "thread":

                        def foreign():
                            with pytest.raises(n.NamespaceError, match="namespace-access-denied"):
                                reader(next(iter(sources)))

                        with ThreadPoolExecutor(1) as pool:
                            pool.submit(foreign).result(timeout=5)
                    elif mode == "lock":

                        def timeout(*args):
                            raise TimeoutError("nested lock did not release")

                        previous = signal.signal(signal.SIGALRM, timeout)
                        signal.alarm(2)
                        try:
                            with journal.journal_lock(case.cas_root):
                                assert journal.read_journal(case.cas_root)
                        finally:
                            signal.alarm(0)
                            signal.signal(signal.SIGALRM, previous)
                    elif mode == "fork":
                        original = case.session.check_authorized
                        case.session.check_authorized = lambda kind: None
                        left, right = os.pipe()
                        pid = os.fork()
                        if pid == 0:
                            try:
                                signal.alarm(5)
                                os.close(left)
                                try:
                                    reader(next(iter(sources)))
                                    result = b"accepted"
                                except n.NamespaceError as error:
                                    result = error.code.encode()
                                os.write(right, result)
                            finally:
                                os._exit(0)
                        os.close(right)
                        try:
                            result = os.read(left, 1024)
                            _, exit_code = os.waitpid(pid, 0)
                        finally:
                            os.close(left)
                            case.session.check_authorized = original
                        assert exit_code == 0 and result == b"namespace-access-denied"
                    if mode == "wrong-cas":
                        return n.ExportReceipt(record.snapshot_id, case.stored.cas_id)
                    if mode == "wrong-id":
                        return n.ExportReceipt(identity(), record.cas_id)
                    if mode == "wrong-type":
                        return {"snapshot_id": record.snapshot_id, "cas_id": record.cas_id}
                    return n.ExportReceipt(record.snapshot_id, record.cas_id)

            if mode in {"nonmember", "manifest", "thread"}:
                with pytest.raises(n.NamespaceError, match="namespace-access-denied"):
                    n.read_namespace(h, read, destination=Destination())
            elif mode.startswith("wrong"):
                with pytest.raises(n.NamespaceError, match="namespace-io-failed"):
                    n.read_namespace(h, read, destination=Destination())
            else:
                assert n.read_namespace(
                    h, read, destination=Destination()
                ).value == n.ExportReceipt(record.snapshot_id, record.cas_id)
        # Even if adapter code swallows a detected integrity error, no success.
        original = Path.read_bytes

        class Swallowed:
            def publish(self, view, raw, reader):
                target = cas.object_path(case.cas_root, next(iter(sources)))

                def corrupt(file):
                    return b"wrong" if file == target else original(file)

                with monkeypatch.context() as patch:
                    patch.setattr(Path, "read_bytes", corrupt)
                    with pytest.raises(
                        n.NamespaceError, match="namespace-storage-recovery-required"
                    ):
                        reader(next(iter(sources)))
                return n.ExportReceipt(record.snapshot_id, record.cas_id)

        with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
            n.read_namespace(h, read, destination=Swallowed())
        assert h.failed
    assert n.recover_namespace(case.root, case.cas_root, case.session, None).state == "ready"

    for first in ("mutation", "snapshot"):
        case = Case(tmp_path / ("snapshot-race-" + first))
        waiting, release = threading.Event(), threading.Event()
        with case.open() as h:
            entries, sources = forest(case.initial.root_id)
            mutate = case.request(
                h, "import-folder", dict(parent_id=case.initial.root_id, entries=entries)
            )
            snap = case.request(h, "snapshot", {"snapshot_id": identity()})
            if first == "mutation":
                snap = replace(snap, expected_revision=1)
            else:
                mutate = replace(mutate, expected_revision=1)
            before = tree(h)
            write = h.write_entry
            stage = content.write_stage

            def paused_entry(*args, **kwargs):
                write(*args, **kwargs)
                if not waiting.is_set():
                    waiting.set()
                    assert release.wait(timeout=10)

            def paused_stage(*args, **kwargs):
                stage(*args, **kwargs)
                if args[1].operation_id == snap.operation_id:
                    waiting.set()
                    assert release.wait(timeout=10)

            with monkeypatch.context() as patch:
                patch.setattr(h, "write_entry", paused_entry if first == "mutation" else write)
                patch.setattr(
                    content, "write_stage", paused_stage if first == "snapshot" else stage
                )
                with ThreadPoolExecutor(2) as pool:
                    leading = pool.submit(
                        n.execute_namespace,
                        h,
                        mutate if first == "mutation" else snap,
                        sources if first == "mutation" else {},
                    )
                    assert waiting.wait(timeout=10)
                    trailing = pool.submit(
                        n.execute_namespace,
                        h,
                        snap if first == "mutation" else mutate,
                        {} if first == "mutation" else sources,
                    )
                    release.set()
                    a, b = leading.result(timeout=10), trailing.result(timeout=10)
            result = b if first == "mutation" else a
            view = n.read_namespace(
                h,
                n.ReadRequest(
                    case.initial.namespace_id,
                    h.info().cas,
                    None,
                    "inspect-snapshot",
                    {"snapshot_id": snap.args["snapshot_id"]},
                ),
            ).value
            expected = (
                before
                if first == "snapshot"
                else {**before, **{e["entry_id"]: {**e, "changed_revision": 1} for e in entries}}
            )
            assert view.manifest["entries"] == [expected[key] for key in sorted(expected)]
            assert (
                result.snapshot.captured_revision == (0 if first == "snapshot" else 1)
                and h.info().revision == 2
            )
            assert not h.publication_receipts and not h.pending_captures

    case = Case(tmp_path / "unicode-folder-admission")
    with case.open() as h:
        entries, sources = forest(case.initial.root_id)
        entries[0]["name"] = "Cafe\u0301"
        entries[2]["name"] = "é.txt"
        entries[2]["metadata"] = {"\ue000": "\n", "\U00010000": "e\u0301"}
        req = case.request(
            h, "import-folder", dict(parent_id=case.initial.root_id, entries=entries)
        )
        n.execute_namespace(h, req, sources)
        actual = tree(h)
        assert (
            actual[entries[0]["entry_id"]]["name"] == "Café"
            and actual[entries[2]["entry_id"]]["metadata"] == entries[2]["metadata"]
        )
        for bad in (n.MAX_INTEGER + 1, 1.0):
            invalid = [
                dict(
                    entries[2],
                    entry_id=identity(),
                    name="Other",
                    parent_id=case.initial.root_id,
                    metadata={"bad": bad},
                )
            ]
            request = case.request(
                h, "import-folder", dict(parent_id=case.initial.root_id, entries=invalid)
            )
            with pytest.raises(n.NamespaceError, match="namespace-request-invalid"):
                n.execute_namespace(h, request, sources)
            assert n.namespace_operation(h, request.operation_id).state == "unknown"
        invalid = [
            dict(entries[2], entry_id=identity(), parent_id=case.initial.root_id, name="bad\nname")
        ]
        with pytest.raises(n.NamespaceError, match="namespace-request-invalid"):
            n.execute_namespace(
                h,
                case.request(
                    h, "import-folder", dict(parent_id=case.initial.root_id, entries=invalid)
                ),
                sources,
            )
        raw = n.canonical_bytes({"entries": [dict(entries[2], metadata={"zero": 0})]})
        with pytest.raises(n.NamespaceError):
            n._decode(raw.replace(b'"zero":0', b'"zero":-0'))


def isolated_catalog_proof(tmp_path, monkeypatch, Case):
    for fault in (
        "namespace",
        "root",
        "binding",
        "origin",
        "schema",
        "version",
        "revision",
        "integer",
        "missing",
    ):
        case = Case(tmp_path / ("isolated-snapshot-" + fault))
        with case.open() as h:
            result = case.apply(h, "snapshot", {"snapshot_id": identity()})
            record = result.snapshot
            body = snapshots.decode(h, record)
        if fault == "namespace":
            body["namespace_id"] = identity()
        elif fault == "root":
            body["root_id"] = identity()
        elif fault == "binding":
            body["cas_binding"]["store_id"] = identity()
        elif fault == "origin":
            body["origin"] = {"kind": "redacted", "operation_id": identity()}
        elif fault == "schema":
            body["schema"] = "ordinary.document.v1"
        elif fault == "version":
            body["schema"] = "castle.namespace_snapshot.v99"
        elif fault == "revision":
            body["captured_revision"] = 1
        elif fault == "integer":
            body["captured_revision"] = False
        if fault == "missing":
            cas.object_path(case.cas_root, record.cas_id).unlink()
        else:
            file = case.directory / "foreign-fixture"
            file.write_bytes(n.canonical_bytes(body))
            stored = cas.ingest_file(case.cas_root, file, "isolated-validator-fixture")
            replacement = replace(record, cas_id=stored.cas_id, size=file.stat().st_size)
            with sqlite3.connect(case.root / "namespace.sqlite") as db:
                # This is an intentional coherent negative association fixture,
                # after a real public capture; it never seeds a success workflow.
                row = db.execute(
                    "SELECT ownership_receipt,terminal_result FROM operations WHERE operation_id=?",
                    (result.operation_id,),
                ).fetchone()
                recipe = content.decode_recipe(row[0])
                terminal = n._decode(row[1])
                recipe["publication"] = []
                recipe["snapshot"] = asdict(replacement)
                terminal["snapshot"] = asdict(replacement)
                db.execute(
                    "UPDATE operations SET ownership_receipt=?,terminal_result=? "
                    "WHERE operation_id=?",
                    (
                        content.encode_recipe(recipe),
                        n.canonical_bytes(terminal).decode(),
                        result.operation_id,
                    ),
                )
                db.execute(
                    "UPDATE snapshots SET cas_id=?,size=? WHERE snapshot_id=?",
                    (replacement.cas_id, replacement.size, record.snapshot_id),
                )
                db.execute("DELETE FROM dependencies")
                db.execute(
                    "INSERT INTO dependencies VALUES ('snapshot',?,?,'manifest')",
                    (record.snapshot_id, replacement.cas_id),
                )
                db.execute(
                    "INSERT INTO dependencies VALUES ('operation',?,?,'request-result')",
                    (result.operation_id, replacement.cas_id),
                )

        def image(root):
            return {
                p.relative_to(root).as_posix(): p.read_bytes()
                for p in root.rglob("*")
                if p.is_file()
            }

        before_ops = image(case.root / "operations")
        before_cas = image(case.cas_root)
        for action in (
            case.open,
            lambda: n.recover_namespace(case.root, case.cas_root, case.session, None),
        ):
            with pytest.raises(n.NamespaceError, match="^namespace-storage-recovery-required$"):
                action()
            assert (
                image(case.root / "operations") == before_ops and image(case.cas_root) == before_cas
            )
        if fault in {"namespace", "origin", "schema"}:
            # Qualify the isolating oracle: removing only the semantic decoder
            # admits this otherwise-consistent graph. No store fixture is changed.
            with monkeypatch.context() as patch:
                patch.setattr(
                    snapshots,
                    "decode",
                    lambda handle, record, **kwargs: n._decode(
                        cas.object_path(handle.cas_root, record.cas_id).read_bytes()
                    ),
                )
                with case.open() as h:
                    assert h.info().revision == 1


def typed_outcome_proof(tmp_path, Case):
    for fault in ("result-bool", "result-join", "error-bool", "error-extra"):
        case = Case(tmp_path / ("typed-outcome-" + fault))
        with case.open() as h:
            result = case.apply(
                h,
                "create-directory",
                dict(
                    entry_id=identity(), parent_id=case.initial.root_id, name="Taken", metadata={}
                ),
            )
            operation_id = result.operation_id
            if fault == "result-join":
                case.apply(
                    h,
                    "create-directory",
                    dict(
                        entry_id=identity(),
                        parent_id=case.initial.root_id,
                        name="Other",
                        metadata={},
                    ),
                )
            if fault.startswith("error"):
                request = case.directory_request(h, "Taken")
                with pytest.raises(n.NamespaceError, match="namespace-name-conflict"):
                    n.execute_namespace(h, request, {})
                operation_id = request.operation_id
        with sqlite3.connect(case.root / "namespace.sqlite") as db:
            field = "terminal_error" if fault.startswith("error") else "terminal_result"
            value = n._decode(
                db.execute(
                    "SELECT " + field + " FROM operations WHERE operation_id=?", (operation_id,)
                ).fetchone()[0]
            )
            if fault == "result-bool":
                value.update(revision_before=False, revision_after=True)
            elif fault == "result-join":
                value.update(revision_before=1, revision_after=2)
            elif fault == "error-bool":
                value["actual_revision"] = True
            else:
                value["extra"] = "not an allowed field"
            db.execute(
                "UPDATE operations SET " + field + "=? WHERE operation_id=?",
                (n.canonical_bytes(value).decode(), operation_id),
            )
        for action in (
            case.open,
            lambda: n.recover_namespace(case.root, case.cas_root, case.session, None),
        ):
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                action()


def recovery_receipt_guard_proof(tmp_path, monkeypatch, Case, child):
    from castle import namespace_transactions as tx

    case = Case(tmp_path / "recovery-published-invariant")
    with case.open() as h:
        entries, sources = forest(case.initial.root_id)
        request = case.request(
            h, "import-folder", dict(parent_id=case.initial.root_id, entries=entries)
        )
    child(case, request, sources, "prepared")
    changes = tx._changes

    def inconsistent(handle, *args, **kwargs):
        if request.operation_id in handle.publication_receipts:
            n._refuse("namespace-name-conflict")
        return changes(handle, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(tx, "_changes", inconsistent)
        with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
            n.recover_namespace(case.root, case.cas_root, case.session, None)
    with sqlite3.connect(case.root / "namespace.sqlite") as db:
        assert db.execute(
            "SELECT state FROM operations WHERE operation_id=?", (request.operation_id,)
        ).fetchone() == ("prepared",)
    assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 1
