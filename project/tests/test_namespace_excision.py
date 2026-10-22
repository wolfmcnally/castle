"""Independent cutover protocols invoked by the retained namespace families."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from castle import namespace as n
from castle import namespace_excision as cutover
from castle import namespace_replacement as replacement
from castle import store_state


def identity(number):
    return f"{number:08x}-1111-4111-8111-111111111111"


def encoded(value):
    # Independent ASCII-only fixture serializer; all IDs/keys in these fixtures
    # are ASCII without control characters, so this has no escape-policy overlap.
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def primitive_proof(tmp_path, monkeypatch):
    root = tmp_path / "replacement-primitives"
    root.mkdir()
    predecessor = n.StoreIdentity(identity(5), identity(6), identity(7), 0, None)
    successor = n.StoreIdentity(identity(8), identity(6), identity(9), 1, identity(7))
    marker = {
        "schema": "castle.namespace_replacement.v1",
        "namespace_id": identity(1),
        "operation_id": identity(2),
        "source_generation": identity(3),
        "target_generation": identity(4),
        "predecessor_cas": asdict(predecessor),
        "successor_cas": asdict(predecessor),
        "stage": "blocked",
        "image_digest": None,
    }
    assert replacement.read(root) is None
    raw = encoded(marker)
    temporary = root / ("replacement." + identity(2) + ".tmp")
    temporary.write_bytes(raw[:37])
    original_write = os.pwrite
    with monkeypatch.context() as patch:
        calls = []

        def short_write(fd, data, offset):
            calls.append((len(data), offset))
            return original_write(fd, data[:3], offset)

        patch.setattr(os, "pwrite", short_write)
        replacement.publish(root, marker, previous=None)
        assert len(calls) > 10
    assert (root / "replacement.json").read_bytes() == raw
    assert not temporary.exists()
    assert replacement.read(root) == marker

    changed = deepcopy(marker)
    changed["successor_cas"] = asdict(successor)
    with monkeypatch.context() as patch:
        patch.setattr(os, "pwrite", lambda *args: 0)
        with pytest.raises(OSError):
            replacement.publish(root, changed, previous=marker)
    assert replacement.read(root) == marker
    replacement.publish(root, changed, previous=marker)
    assert replacement.read(root) == changed

    image = root / ("namespace." + identity(4) + ".sqlite.new")
    image.write_bytes(b"independent image byte fixture, not SQL qualification\n")
    image_digest = hashlib.sha256(image.read_bytes()).hexdigest()
    ready = dict(changed, stage="image-ready", image_digest=image_digest)
    with monkeypatch.context() as patch:
        original_sync = store_state._fsync_directory
        sync_calls = []

        def failed_sync(directory):
            sync_calls.append(directory)
            raise OSError("injected directory sync")

        patch.setattr(store_state, "_fsync_directory", failed_sync)
        with pytest.raises(OSError):
            replacement.publish(root, ready, previous=changed)
        assert sync_calls == [root]
        assert replacement.read(root) == ready
        patch.setattr(store_state, "_fsync_directory", original_sync)
    # Visibility did not acknowledge durability. This same operation must sync
    # the installed file/directory freshly before it can return.
    replacement.publish(root, ready, previous=changed)
    replacement.verify_image(image, ready)
    image.write_bytes(b"different image")
    before = image.read_bytes()
    with pytest.raises(n.NamespaceError, match="namespace-replacement-invalid"):
        replacement.verify_image(image, ready)
    assert image.read_bytes() == before

    malformed = []
    for key in marker:
        value = deepcopy(marker)
        del value[key]
        malformed.append(value)
    malformed.extend(
        [
            dict(marker, unknown=True),
            dict(marker, stage="complete"),
            dict(marker, target_generation=identity(3)),
            dict(marker, image_digest="f" * 64),
            dict(marker, operation_id=True),
            dict(ready, image_digest="sha256:" + image_digest),
        ]
    )
    for value in malformed:
        with pytest.raises(n.NamespaceError, match="namespace-replacement-invalid"):
            replacement.validate(value)
    with pytest.raises(n.NamespaceError, match="namespace-replacement-invalid"):
        replacement.publish(root, dict(ready, stage="complete"), previous=ready)
    assert replacement.read(root) == ready

    initial = n.NamespaceInfo(
        identity(1), identity(10), {}, predecessor, 0, identity(3), {"kind": "unprotected"}
    )
    current = replace(initial, cas=successor, database_generation=identity(4), revision=8)
    completion = (
        identity(2),
        identity(4),
        encoded(asdict(successor)).decode(),
        5,
        6,
        1,
        "a" * 64,
        "b" * 64,
    )
    complete = dict(ready, stage="complete")
    intervals = replacement.epochs(initial, current, [completion], complete)
    assert intervals == [(0, predecessor, identity(3)), (6, successor, identity(4))]
    assert replacement.epoch_at(intervals, 5) == (predecessor, identity(3))
    assert replacement.epoch_at(intervals, 6) == (successor, identity(4))
    third = n.StoreIdentity(identity(13), identity(6), identity(14), 2, identity(9))
    later = replace(current, cas=third, database_generation=identity(12), revision=12)
    second = (
        identity(11),
        identity(12),
        encoded(asdict(third)).decode(),
        9,
        10,
        1,
        "b" * 64,
        "c" * 64,
    )
    latest = dict(
        complete,
        operation_id=identity(11),
        source_generation=identity(4),
        target_generation=identity(12),
        predecessor_cas=asdict(successor),
        successor_cas=asdict(third),
    )
    intervals = replacement.epochs(initial, later, [second, completion], latest)
    assert replacement.epoch_at(intervals, 8) == (successor, identity(4))
    assert replacement.epoch_at(intervals, 10) == (third, identity(12))
    for rows in (
        [completion, (*second[:4], 11, 1, *second[6:])],
        [(*completion[:4], True, 1, *completion[6:])],
        [completion, (*second[:5], 0, *second[6:])],
        [second],
        [completion[:6], second],
        [(*completion[:6], None, completion[7]), second],
        [(*completion[:6], "A" * 64, completion[7]), second],
    ):
        with pytest.raises(n.NamespaceError, match="namespace-replacement-invalid"):
            replacement.epochs(initial, later, rows, latest)

    content = "sha256:" + "a" * 64
    selection = dict(content_ids=[content], entry_ids=[], operation_ids=[], metadata_entry_ids=[])
    args = dict(selection=selection, snapshot_actions=[])
    assert cutover.normalize(args) == args
    for value in (
        dict(args, selection=dict(selection, content_ids=[])),
        dict(args, selection=dict(selection, content_ids=[content, content])),
        dict(args, selection=dict(selection, entry_ids=[True])),
        dict(args, snapshot_actions=[{"snapshot_id": identity(20), "action": "unknown"}]),
    ):
        with pytest.raises(n.NamespaceError, match="namespace-request-invalid"):
            cutover.normalize(value)


def recipe_proof(tmp_path, monkeypatch, Case):
    case = Case(tmp_path / "excision-snapshot-recipe")
    file_id = str(uuid.uuid4())
    payload = b"removed-only snapshot recipe source sentinel\n"
    cas_id = "sha256:" + hashlib.sha256(payload).hexdigest()
    with case.open() as handle:
        parent = str(uuid.uuid4())
        innocent = str(uuid.uuid4())
        case.apply(
            handle,
            "create-directory",
            {
                "entry_id": parent,
                "parent_id": case.initial.root_id,
                "name": "Retained folder",
                "metadata": {},
            },
        )
        case.apply(
            handle,
            "create-directory",
            {
                "entry_id": innocent,
                "parent_id": parent,
                "name": "Removed-only name sentinel",
                "metadata": {"private": "Removed-only metadata sentinel"},
            },
        )
        created = n.execute_namespace(
            handle,
            case.request(
                handle,
                "create-file",
                {
                    "entry_id": file_id,
                    "parent_id": case.initial.root_id,
                    "name": "Removed-only name sentinel",
                    "metadata": {"private": "Removed-only metadata sentinel"},
                    "content": {"cas_id": cas_id, "size": len(payload)},
                },
            ),
            {cas_id: payload},
        )
        captured = case.apply(handle, "snapshot", {"snapshot_id": identity(40)})
        record = captured.snapshot
        second_capture = case.apply(handle, "snapshot", {"snapshot_id": identity(41)})
        second_record = second_capture.snapshot
        first_delete = case.apply(handle, "delete-snapshot", {"snapshot_id": record.snapshot_id})
        second_delete = case.apply(
            handle, "delete-snapshot", {"snapshot_id": second_record.snapshot_id}
        )
        operation = str(uuid.uuid4())
        selection = {
            "content_ids": sorted([cas_id, record.cas_id, second_record.cas_id]),
            "entry_ids": [file_id],
            "metadata_entry_ids": [],
            "operation_ids": [],
        }
        args = {
            "selection": selection,
            "snapshot_actions": [
                {
                    "snapshot_id": item.snapshot_id,
                    "action": "replace",
                    "remove_entry_ids": [file_id],
                    "clear_metadata_entry_ids": [],
                }
                for item in (record, second_record)
            ],
        }
        request = {"operation_id": operation, "args": cutover.normalize(args)}
        before = handle.db.execute("SELECT * FROM snapshots").fetchall()
        before_revision = handle.info().revision
        with handle.admitted():
            recipes, dropped, pairs = cutover.snapshot_recipes(handle, request, {file_id}, set())
            assert pairs == 8 and dropped == []
            assert len(recipes) == 2
            assert recipes[1][0] == second_record and recipes[1][1] == 0
            assert json.loads(recipes[1][2])["captured_revision"] == 4
            original, live, raw = recipes[0]
            assert original == record and live == 0
            manifest = json.loads(raw)
            assert manifest["origin"] == {"kind": "redacted", "operation_id": operation}
            assert manifest["captured_revision"] == 3
            expected_ids = {case.initial.root_id, parent, innocent}
            assert {item["entry_id"] for item in manifest["entries"]} == expected_ids
            innocent_record = next(
                item for item in manifest["entries"] if item["entry_id"] == innocent
            )
            assert innocent_record["name"] == "Removed-only name sentinel"
            assert innocent_record["metadata"] == {"private": "Removed-only metadata sentinel"}
            assert cas_id.encode() not in raw and file_id.encode() not in raw
            assert manifest["cas_binding"] == asdict(case.initial.expected_cas)
            missing = {"operation_id": operation, "args": dict(args, snapshot_actions=[])}
            with pytest.raises(n.NamespaceError, match="namespace-dependency-conflict"):
                cutover.snapshot_recipes(handle, missing, {file_id}, set())
            uncovered = deepcopy(request)
            uncovered["args"]["selection"]["content_ids"] = [cas_id]
            with pytest.raises(n.NamespaceError, match="namespace-dependency-conflict"):
                cutover.snapshot_recipes(handle, uncovered, {file_id}, set())
            retained_secret = deepcopy(request)
            retained_secret["args"]["snapshot_actions"][0]["remove_entry_ids"] = []
            with pytest.raises(n.NamespaceError, match="namespace-dependency-conflict"):
                cutover.snapshot_recipes(handle, retained_secret, {file_id}, set())
            drop = {
                "operation_id": operation,
                "args": dict(
                    args,
                    snapshot_actions=[
                        {"snapshot_id": item.snapshot_id, "action": "drop"}
                        for item in (record, second_record)
                    ],
                ),
            }
            assert cutover.snapshot_recipes(handle, drop, {file_id}, set()) == (
                [],
                [record.snapshot_id, second_record.snapshot_id],
                0,
            )
            with monkeypatch.context() as patch:
                patch.setattr(n, "MAX_ENTRIES", 4)
                with pytest.raises(n.NamespaceError, match="namespace-limit-exceeded"):
                    cutover.snapshot_recipes(handle, request, {file_id}, set())
        assert handle.info().revision == before_revision
        assert handle.db.execute("SELECT * FROM snapshots").fetchall() == before

        from castle import namespace_transactions as transactions

        with handle.admitted():
            rows = [
                transactions._row(handle, row[0])
                for row in handle.db.execute("SELECT operation_id FROM operations").fetchall()
            ]
            dependencies = set(handle.db.execute("SELECT * FROM dependencies"))
            disclosed = cutover.operation_disclosures(
                rows,
                {file_id},
                set(),
                {cas_id, record.cas_id, second_record.cas_id},
                set(),
                set(),
                dependencies,
            )
            assert disclosed == {
                created.operation_id,
                captured.operation_id,
                second_capture.operation_id,
                first_delete.operation_id,
                second_delete.operation_id,
            }
            # The two retained owners share a name/metadata value, not selected
            # ownership. A classifier that scores string presence over-fires.
            assert len(rows) == 7
            with pytest.raises(n.NamespaceError, match="namespace-dependency-conflict"):
                cutover.selection_plan(handle, request)
            complete_request = deepcopy(request)
            complete_request["args"]["selection"]["operation_ids"] = sorted(disclosed)
            plan, complete_recipes = cutover.selection_plan(handle, complete_request)
            assert plan == {
                "entry_ids": [file_id],
                "metadata_entry_ids": [],
                "operation_ids": sorted(disclosed),
                "drop_snapshot_ids": [],
            }
            assert len(complete_recipes) == 2
        # Qualify the private pre-retirement boundary on real public-created
        # authority. This is intentionally not an end-to-end excision claim.
        intent_request = n._plain(case.request(handle, "excise", complete_request["args"]))
        before_journal = (case.cas_root / "journal.jsonl").read_bytes()
        successor_root = case.directory / "successor"
        with handle.admitted():
            predecessor, successor_path = cutover.roots(
                case.root,
                case.cas_root,
                {"predecessor_root": case.cas_root, "successor_root": successor_root},
            )
            intent = cutover.prepare_intent(handle, intent_request, predecessor, successor_path)
            assert intent["stage"] == "prepared"
            assert handle.db.execute("SELECT service_state FROM namespace").fetchone() == (
                "recovery-required",
            )
            assert handle.db.execute(
                "SELECT state FROM operations WHERE operation_id=?",
                (intent_request["operation_id"],),
            ).fetchone() == ("prepared",)
        blocked = replacement.read(case.root)
        assert blocked["stage"] == "blocked"
        assert blocked["predecessor_cas"] == blocked["successor_cas"]
        assert blocked["image_digest"] is None
        marker_bytes = (case.root / "replacement.json").read_bytes()
        assert str(case.directory).encode() not in marker_bytes and b"sha256:" not in marker_bytes
        assert (case.cas_root / "journal.jsonl").read_bytes() == before_journal
        assert not successor_root.exists()
    with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
        case.open()
    recovery_roots = {
        "predecessor_root": case.cas_root,
        "successor_root": successor_root,
        "operation_id": intent_request["operation_id"],
        "expected_predecessor": case.initial.expected_cas,
        "action": "resume",
    }
    recovered = n.recover_namespace(case.root, case.cas_root, case.session, recovery_roots)
    assert recovered.state == "ready" and recovered.revision == 8
    assert recovered.cas_binding.generation == case.initial.expected_cas.generation + 1
    assert recovered.completed_operation_ids == [intent_request["operation_id"]]
    from castle import namespace_snapshots as snapshots

    successor_session = type(case.session)(
        case.root, successor_root, case.directory / "domain.lock"
    )
    with n.open_namespace(case.root, successor_root, successor_session) as handle:
        assert set(handle.entries()) == {case.initial.root_id, parent, innocent}
        assert file_id not in handle.entries(live=False)
        assert handle.entries()[innocent].metadata == {"private": "Removed-only metadata sentinel"}
        assert set(handle.objects) == {case.stored.cas_id} | {
            row[0] for row in handle.db.execute("SELECT cas_id FROM snapshots")
        }
        for record_row in handle.db.execute(
            "SELECT snapshot_id,cas_id,size,captured_revision,live FROM snapshots"
        ):
            assert record_row[4] == 0
            view = snapshots.catalog_view(handle, n.SnapshotRecord(*record_row[:4]))
            assert view["origin"] == {
                "kind": "redacted",
                "operation_id": intent_request["operation_id"],
            }
            assert {item["entry_id"] for item in view["entries"]} == {
                case.initial.root_id,
                parent,
                innocent,
            }
        status = n.namespace_operation(handle, intent_request["operation_id"])
        assert status.state == "retired" and status.result.kind == "excise"
        assert status.result.request_fingerprint is None and status.result.affected_entry_ids == []
        assert status.result.revision_before == 7 and status.result.revision_after == 8
        assert status.result.cas_binding == recovered.cas_binding
        assert replacement.read(case.root)["stage"] == "complete"
        assert not (case.root / "operations" / intent_request["operation_id"]).exists()
        for selected_operation in disclosed:
            assert n.namespace_operation(handle, selected_operation).state == "retired"
            assert not (case.root / "operations" / selected_operation).exists()
        assert len(list(case.root.iterdir())) == 4
    again = n.recover_namespace(case.root, case.cas_root, case.session, recovery_roots)
    assert again == recovered


def workflow_proof(tmp_path, monkeypatch, Case):
    """Two public cutovers, unchanged permitted outcomes, and explicit abort."""
    from castle import namespace_snapshots as snapshots

    case = Case(tmp_path / "excision-public-workflow")
    with case.open() as handle:
        initial_generation = handle.info().database_generation
        keep_request = case.directory_request(handle, "retained")
        keep_result = n.execute_namespace(handle, keep_request, {})
        benign_capture = case.apply(handle, "snapshot", {"snapshot_id": identity(59)})
        benign_delete_request = case.request(
            handle, "delete-snapshot", {"snapshot_id": identity(59)}
        )
        benign_delete = n.execute_namespace(handle, benign_delete_request, {})
        directory_id = identity(60)
        file_id = identity(61)
        shared_id = identity(62)
        created_dir = case.apply(
            handle,
            "create-directory",
            {
                "entry_id": directory_id,
                "parent_id": case.initial.root_id,
                "name": "Selected subtree",
                "metadata": {},
            },
        )
        payload = b"Only the selected content carries this excision sentinel.\n"
        cas_id = "sha256:" + hashlib.sha256(payload).hexdigest()
        created_file = n.execute_namespace(
            handle,
            case.request(
                handle,
                "create-file",
                {
                    "entry_id": file_id,
                    "parent_id": directory_id,
                    "name": "selected-file",
                    "metadata": {},
                    "content": {"cas_id": cas_id, "size": len(payload)},
                },
            ),
            {cas_id: payload},
        )
        shared_file = n.execute_namespace(
            handle,
            case.request(
                handle,
                "create-file",
                {
                    "entry_id": shared_id,
                    "parent_id": case.initial.root_id,
                    "name": "shared-file",
                    "metadata": {},
                    "content": {"cas_id": cas_id, "size": len(payload)},
                },
            ),
            {},
        )
        captured = case.apply(handle, "snapshot", {"snapshot_id": identity(63)})
        logical = case.apply(handle, "delete-entry", {"entry_id": shared_id})
        args = {
            "selection": {
                "content_ids": [cas_id, captured.snapshot.cas_id],
                "entry_ids": [directory_id],
                "metadata_entry_ids": [],
                "operation_ids": [
                    benign_capture.operation_id,
                    benign_delete.operation_id,
                    created_dir.operation_id,
                    created_file.operation_id,
                    shared_file.operation_id,
                    captured.operation_id,
                    logical.operation_id,
                ],
            },
            "snapshot_actions": [
                {"snapshot_id": identity(59), "action": "drop"},
                {
                    "snapshot_id": captured.snapshot.snapshot_id,
                    "action": "replace",
                    "remove_entry_ids": [directory_id, shared_id],
                    "clear_metadata_entry_ids": [],
                },
            ],
        }
        successor = case.directory / "second-cas"
        roots = {"predecessor_root": case.cas_root, "successor_root": successor}
        request = case.request(handle, "excise", args)
        with pytest.raises(n.NamespaceError, match="namespace-dependency-conflict"):
            n.execute_namespace(handle, request, {}, roots)
        assert not successor.exists() and replacement.read(case.root) is None
        assert n.namespace_operation(handle, request.operation_id).state == "unknown"
        args["selection"]["entry_ids"].append(shared_id)
        args["selection"]["operation_ids"].remove(benign_delete.operation_id)
        with pytest.raises(n.NamespaceError, match="namespace-dependency-conflict"):
            n.execute_namespace(handle, request, {}, roots)
        assert not successor.exists() and replacement.read(case.root) is None
        args["selection"]["operation_ids"].append(benign_delete.operation_id)
        first = n.execute_namespace(handle, request, {}, roots)
        assert handle.closed and first.revision_after == logical.revision_after + 1
    second_session = type(case.session)(case.root, successor, case.directory / "domain.lock")
    with n.open_namespace(case.root, successor, second_session) as handle:
        assert n.execute_namespace(handle, keep_request, {}) == keep_result
        assert n.namespace_operation(handle, keep_request.operation_id).result == keep_result
        assert n.namespace_operation(handle, benign_delete_request.operation_id).state == "retired"
        assert benign_capture.snapshot.cas_id in handle.objects
        assert set(handle.entries()) == {case.initial.root_id, keep_request.args["entry_id"]}
        assert handle.info().database_generation != initial_generation
        assert file_id not in handle.entries(live=False) and shared_id not in handle.entries(
            live=False
        )
        current_record = snapshots.catalog(handle, captured.snapshot.snapshot_id)
        first_redacted = snapshots.catalog_view(handle, current_record)
        assert first_redacted["captured_revision"] == captured.snapshot.captured_revision
        assert len(first_redacted["entries"]) == 2
        # Completed selection has gone. A token lookup refuses before comparing
        # the old binding/request or performing any new successor effects.
        with pytest.raises(n.NamespaceError, match="namespace-operation-retired"):
            n.execute_namespace(
                handle,
                request,
                {},
                {"predecessor_root": successor, "successor_root": case.directory / "never-created"},
            )
        assert not (case.directory / "never-created").exists()
        another_id = identity(64)
        another_payload = b"A distinct second selected object\n"
        another_cas = "sha256:" + hashlib.sha256(another_payload).hexdigest()
        another = n.execute_namespace(
            handle,
            case.request(
                handle,
                "create-file",
                {
                    "entry_id": another_id,
                    "parent_id": case.initial.root_id,
                    "name": "second-selected",
                    "metadata": {},
                    "content": {"cas_id": another_cas, "size": len(another_payload)},
                },
            ),
            {another_cas: another_payload},
        )
        second_request = case.request(
            handle,
            "excise",
            {
                "selection": {
                    "content_ids": [another_cas],
                    "entry_ids": [another_id],
                    "metadata_entry_ids": [],
                    "operation_ids": [another.operation_id],
                },
                "snapshot_actions": [
                    {
                        "snapshot_id": current_record.snapshot_id,
                        "action": "replace",
                        "remove_entry_ids": [],
                        "clear_metadata_entry_ids": [],
                    }
                ],
            },
        )
        third = case.directory / "third-cas"
        second = n.execute_namespace(
            handle, second_request, {}, {"predecessor_root": successor, "successor_root": third}
        )
    third_session = type(case.session)(case.root, third, case.directory / "domain.lock")
    with n.open_namespace(case.root, third, third_session) as handle:
        assert n.namespace_operation(handle, request.operation_id).result == first
        assert n.namespace_operation(handle, second_request.operation_id).result == second
        assert n.execute_namespace(handle, keep_request, {}) == keep_result
        record = snapshots.catalog(handle, current_record.snapshot_id)
        manifest = snapshots.catalog_view(handle, record)
        assert manifest["captured_revision"] == first_redacted["captured_revision"]
        assert manifest["entries"] == first_redacted["entries"]
        assert manifest["origin"] == {
            "kind": "redacted",
            "operation_id": second_request.operation_id,
        }
        assert manifest["cas_binding"] == asdict(second.cas_binding)
        # Earlier database generations are permanent authority identities.
        with pytest.raises(n.NamespaceError, match="namespace-request-invalid"):
            collision = case.directory_request(handle, "collision")
            collision.args["entry_id"] = initial_generation
            n.execute_namespace(handle, collision, {})
        before_abort = handle.info()
        selected = case.stored.cas_id
        abort_request = case.request(
            handle,
            "excise",
            {
                "selection": {
                    "content_ids": [selected],
                    "entry_ids": [],
                    "metadata_entry_ids": [],
                    "operation_ids": [],
                },
                "snapshot_actions": [
                    {
                        "snapshot_id": record.snapshot_id,
                        "action": "replace",
                        "remove_entry_ids": [],
                        "clear_metadata_entry_ids": [],
                    }
                ],
            },
        )
        fourth = case.directory / "fourth-cas"
        with monkeypatch.context() as patch:

            def stop_before_cas(*args):
                raise OSError("injected before retirement")

            patch.setattr(cutover, "_cas_transition", stop_before_cas)
            with pytest.raises(
                n.NamespaceError, match="namespace-storage-recovery-required"
            ) as error:
                n.execute_namespace(
                    handle, abort_request, {}, {"predecessor_root": third, "successor_root": fourth}
                )
            assert error.value.effect == "recovery-required"
    abort_roots = {
        "predecessor_root": third,
        "successor_root": fourth,
        "operation_id": abort_request.operation_id,
        "expected_predecessor": second.cas_binding,
        "action": "abort-before-retirement",
    }
    aborted = n.recover_namespace(case.root, third, third_session, abort_roots)
    assert aborted.state == "ready" and aborted.revision == before_abort.revision
    assert aborted.cas_binding == second.cas_binding and aborted.completed_operation_ids == []
    assert not fourth.exists()
    assert replacement.read(case.root)["operation_id"] == second_request.operation_id
    assert n.recover_namespace(case.root, third, third_session, abort_roots) == aborted
    with n.open_namespace(case.root, third, third_session) as handle:
        assert n.namespace_operation(handle, abort_request.operation_id).state == "rejected"
        with pytest.raises(n.NamespaceError, match="namespace-io-failed"):
            n.execute_namespace(
                handle, abort_request, {}, {"predecessor_root": third, "successor_root": fourth}
            )


_KILL_CHILD = """
import json, os, runpy, sys
from pathlib import Path
from castle import (
    namespace as n,
    namespace_excision as cutover,
    namespace_replacement as replacement,
    namespace_store as store,
    store_state,
)

value = json.loads(sys.argv[1])
root, source, destination = map(
    Path, (value["root"], value["source"], value["destination"])
)
caller = runpy.run_path(value["caller"])
session = caller["LocalSession"](root, source, Path(value["domain"]))
fields = value["request"]
fields.pop("schema")
fields["expected_cas"] = n.StoreIdentity(**fields["expected_cas"])
request = n.MutationRequest(**fields)
point = value["point"]


def die(name):
    if point == name:
        os._exit(97)


with n.open_namespace(root, source, session) as handle:
    original_commit = store._commit

    def commit(connection):
        row = connection.execute(
            "SELECT state,ownership_receipt FROM operations WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()
        completion = connection.execute(
            "SELECT completed FROM replacement_completion WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()
        stage = (
            json.loads(row[1])["cutover"]["stage"]
            if row and row[1] and "cutover" in json.loads(row[1])
            else None
        )
        name = (
            "intent"
            if row and row[0] == "staging"
            else "references"
            if stage == "references-published"
            else "completion"
            if completion == (1,)
            else None
        )
        if name:
            die("before-" + name)
        original_commit(connection)
        if name:
            die("after-" + name)

    store._commit = commit
    original_private = replacement.write_private

    def private(file, raw, **kwargs):
        recipe = file.parent.name == request.operation_id and n._UUID.fullmatch(
            file.name
        )
        if (point == "recipe-prefix" and recipe) or (
            point == "image-prefix" and file.name.endswith(".sqlite.new")
        ):
            fd = os.open(
                file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            os.write(fd, raw[: max(1, len(raw) // 3)])
            os.fsync(fd)
            os._exit(97)
        result = original_private(file, raw, **kwargs)
        if recipe:
            die("recipe-durable")
        return result

    replacement.write_private = private
    original_marker = replacement.publish

    def marker(directory, payload, **kwargs):
        name = payload["stage"]
        if name == "blocked" and payload["predecessor_cas"] != payload["successor_cas"]:
            name = "successor-blocked"
        die("before-" + name)
        result = original_marker(directory, payload, **kwargs)
        die("after-" + name)
        return result

    replacement.publish = marker
    original_metadata = store_state._publish_metadata

    def metadata(file, payload, *args, **kwargs):
        if Path(file) == source / "retired.json":
            die("before-retirement")
        result = original_metadata(file, payload, *args, **kwargs)
        if Path(file) == source / "retired.json":
            die("after-retirement")
        return result

    store_state._publish_metadata = metadata
    original_save = cutover._save_intent

    def save(h, operation, payload, **kwargs):
        name = (
            "cas-receipt"
            if payload["stage"] == "cas-complete"
            else "publication-receipt"
            if payload["stage"] == "publishing"
            and any(item["receipt"] for item in payload["publications"])
            else None
        )
        if name:
            die("before-" + name)
        result = original_save(h, operation, payload, **kwargs)
        if name:
            die("after-" + name)
        return result

    cutover._save_intent = save
    original_rename = store_state._rename_new

    def rename(source_file, target_file):
        name = (
            "rename-old"
            if str(target_file).endswith(".sqlite.old")
            else "rename-target"
            if str(source_file).endswith(".sqlite.new")
            else None
        )
        if name:
            die("before-" + name)
        result = original_rename(source_file, target_file)
        if name:
            die("after-" + name)
        return result

    store_state._rename_new = rename
    original_unlink = Path.unlink

    def unlink(file, *args, **kwargs):
        if file.name.endswith(".sqlite.old"):
            die("before-old-disposal")
        result = original_unlink(file, *args, **kwargs)
        if file.name.endswith(".sqlite.old"):
            die("after-old-disposal")
        if file.parent.name in request.args["selection"]["operation_ids"]:
            if file.name == "request.json":
                die("selected-request-disposal")
            if file.name == "receipt.json":
                die("selected-receipt-disposal")
        return result

    Path.unlink = unlink
    result = n.execute_namespace(
        handle, request, {}, {"predecessor_root": source, "successor_root": destination}
    )
    die("response")
    raise RuntimeError("requested boundary not reached: " + point)
"""


def metadata_proof(tmp_path, Case):
    case = Case(tmp_path / "excision-metadata-only-selection")
    with case.open() as handle:
        live, deleted = identity(82), identity(83)
        for entry, name in ((live, "clear-live"), (deleted, "clear-deleted")):
            case.apply(
                handle,
                "create-directory",
                {
                    "entry_id": entry,
                    "parent_id": case.initial.root_id,
                    "name": name,
                    "metadata": {"private": "Removed-only metadata owner sentinel"},
                },
            )
        case.apply(handle, "delete-entry", {"entry_id": deleted})
        old_deleted = handle.entries(live=False)[deleted]
        operations = [row[0] for row in handle.db.execute("SELECT operation_id FROM operations")]
        request = case.request(
            handle,
            "excise",
            {
                "selection": {
                    "content_ids": [case.stored.cas_id],
                    "entry_ids": [],
                    "metadata_entry_ids": [live, deleted],
                    "operation_ids": operations,
                },
                "snapshot_actions": [],
            },
        )
        destination = case.directory / "successor"
        result = n.execute_namespace(
            handle, request, {}, {"predecessor_root": case.cas_root, "successor_root": destination}
        )
        assert result.revision_before == 3 and result.revision_after == 4
    session = type(case.session)(case.root, destination, case.directory / "domain.lock")
    with n.open_namespace(case.root, destination, session) as handle:
        assert (
            handle.entries()[live].metadata == {} and handle.entries()[live].changed_revision == 4
        )
        assert deleted not in handle.entries()
        assert handle.entries(live=False)[deleted] == replace(old_deleted, metadata={})
        assert handle.objects == {}
        assert (
            b"Removed-only metadata owner sentinel"
            not in (case.root / "namespace.sqlite").read_bytes()
        )


def _small_case(tmp_path, Case, label):
    case = Case(tmp_path / label)
    with case.open() as handle:
        selected = identity(80)
        data = b"Synthetic cutover fault and concurrency payload.\n"
        cas_id = "sha256:" + hashlib.sha256(data).hexdigest()
        created = n.execute_namespace(
            handle,
            case.request(
                handle,
                "create-file",
                {
                    "entry_id": selected,
                    "parent_id": case.initial.root_id,
                    "name": "Selected",
                    "metadata": {},
                    "content": {"cas_id": cas_id, "size": len(data)},
                },
            ),
            {cas_id: data},
        )
        capture = case.apply(handle, "snapshot", {"snapshot_id": identity(81)})
        request = case.request(
            handle,
            "excise",
            {
                "selection": {
                    "content_ids": [cas_id, capture.snapshot.cas_id],
                    "entry_ids": [selected],
                    "metadata_entry_ids": [],
                    "operation_ids": [created.operation_id, capture.operation_id],
                },
                "snapshot_actions": [
                    {
                        "snapshot_id": capture.snapshot.snapshot_id,
                        "action": "replace",
                        "remove_entry_ids": [selected],
                        "clear_metadata_entry_ids": [],
                    }
                ],
            },
        )
    destination = case.directory / "successor"
    roots = {"predecessor_root": case.cas_root, "successor_root": destination}
    recovery = {
        **roots,
        "expected_predecessor": request.expected_cas,
        "operation_id": request.operation_id,
        "action": "resume",
    }
    return case, request, roots, recovery


def root_proof(tmp_path, monkeypatch, Case):
    import shutil
    import sqlite3

    from castle import namespace_store as store

    case, request, roots, recovery = _small_case(tmp_path, Case, "completed-original-roots")
    with case.open() as handle:
        result = n.execute_namespace(handle, request, {}, roots)
    assert not (case.root / replacement.filenames(replacement.read(case.root))[1]).exists()
    expected = (
        cutover.root_commitment(roots["predecessor_root"]),
        cutover.root_commitment(roots["successor_root"]),
    )
    with sqlite3.connect(case.root / "namespace.sqlite") as db:
        original = db.execute("SELECT * FROM replacement_completion").fetchone()
        assert len(original) == 8 and original[6:] == expected
    assert (
        n.recover_namespace(case.root, case.cas_root, case.session, recovery).cas_binding
        == result.cas_binding
    )

    for role in ("predecessor_root", "successor_root"):
        replica = case.directory / (role + "-same-id-replica")
        shutil.copytree(roots[role], replica, copy_function=shutil.copy2)
        observed = []
        with monkeypatch.context() as patch:
            original_check = store.NamespaceHandle._cas_check

            def payload_check(*args, **kwargs):
                observed.append(True)
                return original_check(*args, **kwargs)

            patch.setattr(store.NamespaceHandle, "_cas_check", payload_check)
            before = (case.root / "namespace.sqlite").read_bytes()
            with pytest.raises(n.NamespaceError, match="namespace-replacement-invalid"):
                source = case.cas_root if role != "predecessor_root" else replica
                session = type(case.session)(case.root, source, case.directory / "domain.lock")
                n.recover_namespace(case.root, source, session, {**recovery, role: replica})
            assert not observed
            assert (case.root / "namespace.sqlite").read_bytes() == before
    for digest in ("", "A" * 64, "g" * 64, "0" * 64):
        with sqlite3.connect(case.root / "namespace.sqlite") as db:
            db.execute("UPDATE replacement_completion SET successor_root_commitment=?", (digest,))
        before = (case.root / "namespace.sqlite").read_bytes()
        with pytest.raises(n.NamespaceError, match="namespace-replacement-invalid"):
            n.recover_namespace(case.root, case.cas_root, case.session, recovery)
        assert (case.root / "namespace.sqlite").read_bytes() == before
    with sqlite3.connect(case.root / "namespace.sqlite") as db:
        db.execute("UPDATE replacement_completion SET successor_root_commitment=?", (expected[1],))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE replacement_completion SET predecessor_root_commitment=NULL")
        db.rollback()
        # Deliberate old-schema corruption, never workflow seeding or migration.
        db.execute("ALTER TABLE replacement_completion DROP COLUMN predecessor_root_commitment")
        db.execute("ALTER TABLE replacement_completion DROP COLUMN successor_root_commitment")
    with pytest.raises(n.NamespaceError, match="namespace-format-unsupported"):
        n.recover_namespace(case.root, case.cas_root, case.session, recovery)


def later_cutover_proof(tmp_path, monkeypatch, Case):
    import subprocess
    import sys
    from pathlib import Path

    for point in (
        "after-retirement",
        "after-cas-receipt",
        "after-publication-receipt",
        "after-references",
    ):
        case, first_request, roots, _ = _small_case(tmp_path, Case, "second-cutover-" + point)
        with case.open() as handle:
            first = n.execute_namespace(handle, first_request, {}, roots)
        case.cas_root = roots["successor_root"]
        case.session = type(case.session)(case.root, case.cas_root, case.directory / "domain.lock")
        with case.open() as handle:
            retained_request = case.directory_request(handle, "retained-second-epoch")
            retained = n.execute_namespace(handle, retained_request, {})
            data = b"Second cutover actual new bytes\n"
            content_id = "sha256:" + hashlib.sha256(data).hexdigest()
            file_id = str(uuid.uuid4())
            added = n.execute_namespace(
                handle,
                case.request(
                    handle,
                    "create-file",
                    {
                        "entry_id": file_id,
                        "parent_id": case.initial.root_id,
                        "name": "selected-second",
                        "metadata": {},
                        "content": {"cas_id": content_id, "size": len(data)},
                    },
                ),
                {content_id: data},
            )
            capture = case.apply(handle, "snapshot", {"snapshot_id": identity(82)})
            request = case.request(
                handle,
                "excise",
                {
                    "selection": {
                        "content_ids": [content_id, capture.snapshot.cas_id],
                        "entry_ids": [file_id],
                        "metadata_entry_ids": [],
                        "operation_ids": [added.operation_id, capture.operation_id],
                    },
                    "snapshot_actions": [
                        {
                            "snapshot_id": identity(81),
                            "action": "replace",
                            "remove_entry_ids": [],
                            "clear_metadata_entry_ids": [],
                        },
                        {
                            "snapshot_id": identity(82),
                            "action": "replace",
                            "remove_entry_ids": [file_id],
                            "clear_metadata_entry_ids": [],
                        },
                    ],
                },
            )
        successor = case.directory / "third"
        payload = {
            "root": str(case.root),
            "source": str(case.cas_root),
            "destination": str(successor),
            "domain": str(case.directory / "domain.lock"),
            "caller": str(Path(__file__).parents[1] / "examples/namespace_demo.py"),
            "request": asdict(request),
            "point": point,
        }
        child = subprocess.run(
            [sys.executable, "-c", _KILL_CHILD, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert child.returncode == 97, (point, child.stdout, child.stderr)
        recovery = {
            "predecessor_root": case.cas_root,
            "successor_root": successor,
            "expected_predecessor": request.expected_cas,
            "operation_id": request.operation_id,
            "action": "resume",
        }
        recovered = n.recover_namespace(case.root, case.cas_root, case.session, recovery)
        assert recovered.revision == request.expected_revision + 1
        session = type(case.session)(case.root, successor, case.directory / "domain.lock")
        with n.open_namespace(case.root, successor, session) as handle:
            assert n.execute_namespace(handle, retained_request, {}) == retained
            assert n.namespace_operation(handle, first_request.operation_id).result == first
            assert set(handle.entries()) == {
                case.initial.root_id,
                retained_request.args["entry_id"],
            }


def advice_boundary_proof(tmp_path, monkeypatch, Case):
    case, request, roots, _ = _small_case(tmp_path, Case, "foreign-successor-before-intent")
    roots["successor_root"].mkdir()
    foreign = roots["successor_root"] / "caller-owned"
    foreign.write_bytes(b"preserve foreign destination exactly\n")
    with case.open() as handle:
        before = handle.info()
        with pytest.raises(n.NamespaceError, match="namespace-root-unsafe") as error:
            n.execute_namespace(handle, request, {}, roots)
        assert error.value.effect == "none" and handle.info() == before
        assert n.namespace_operation(handle, request.operation_id).state == "unknown"
        assert replacement.read(case.root) is None
        assert not (case.root / "operations" / request.operation_id).exists()
    assert foreign.read_bytes() == b"preserve foreign destination exactly\n"

    case, request, roots, recovery = _small_case(tmp_path, Case, "revocation-after-cas")
    transition = cutover._cas_transition
    recipe_bytes = cutover._recipe_bytes
    observed = []
    with monkeypatch.context() as patch:

        def revoke(*args):
            transition(*args)
            case.session.active = False

        def recipes(*args):
            observed.append(True)
            return recipe_bytes(*args)

        patch.setattr(cutover, "_cas_transition", revoke)
        patch.setattr(cutover, "_recipe_bytes", recipes)
        with case.open() as handle:
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                n.execute_namespace(handle, request, {}, roots)
        assert not observed
    case.session.active = True
    assert n.recover_namespace(case.root, case.cas_root, case.session, recovery).state == "ready"

    for value_is_forbidden in (False, True):
        case, request, roots, _ = _small_case(
            tmp_path, Case, "metadata-key-" + str(value_is_forbidden)
        )
        with case.open() as handle:
            metadata = (
                {"private": "namespace-test"}
                if value_is_forbidden
                else {"namespace-test": "innocent value"}
            )
            change = case.apply(
                handle, "set-metadata", {"entry_id": identity(80), "metadata": metadata}
            )
            request.args["selection"]["operation_ids"].append(change.operation_id)
            request = replace(request, expected_revision=handle.info().revision)
            if value_is_forbidden:
                with pytest.raises(n.NamespaceError, match="namespace-dependency-conflict"):
                    n.execute_namespace(handle, request, {}, roots)
                assert not roots["successor_root"].exists()
                assert n.namespace_operation(handle, request.operation_id).state == "unknown"
            else:
                assert n.execute_namespace(handle, request, {}, roots).outcome == "committed"

    case, request, roots, _ = _small_case(tmp_path, Case, "abort-prior-marker-temporary")
    with case.open() as handle:
        n.execute_namespace(handle, request, {}, roots)
    prior = replacement.read(case.root)
    source = roots["successor_root"]
    session = type(case.session)(case.root, source, case.directory / "domain.lock")
    destination = case.directory / "unused-next-successor"
    with n.open_namespace(case.root, source, session) as handle:
        before = handle.info()
        pending = case.request(
            handle,
            "excise",
            {
                "selection": {
                    "content_ids": [case.stored.cas_id],
                    "entry_ids": [],
                    "metadata_entry_ids": [],
                    "operation_ids": [],
                },
                "snapshot_actions": [
                    {
                        "snapshot_id": identity(81),
                        "action": "replace",
                        "remove_entry_ids": [],
                        "clear_metadata_entry_ids": [],
                    }
                ],
            },
        )
        with monkeypatch.context() as patch:
            patch.setattr(
                cutover,
                "_cas_transition",
                lambda *args: (_ for _ in ()).throw(OSError("before next retirement")),
            )
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                n.execute_namespace(
                    handle, pending, {}, {"predecessor_root": source, "successor_root": destination}
                )
    recovery = {
        "predecessor_root": source,
        "successor_root": destination,
        "expected_predecessor": pending.expected_cas,
        "operation_id": pending.operation_id,
        "action": "abort-before-retirement",
    }
    writer = replacement.write_private
    fired = []
    with monkeypatch.context() as patch:

        def interrupted_restore(file, raw, **kwargs):
            if file.name == f"replacement.{pending.operation_id}.tmp" and n._decode(raw) == prior:
                file.write_bytes(raw[:29])
                fired.append(True)
                raise OSError("interrupted restoration of prior marker")
            return writer(file, raw, **kwargs)

        patch.setattr(replacement, "write_private", interrupted_restore)
        with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
            n.recover_namespace(case.root, source, session, recovery)
    assert fired and replacement.read(case.root) is None
    assert (case.root / f"replacement.{pending.operation_id}.tmp").exists()
    assert not (case.root / f"replacement.{request.operation_id}.tmp").exists()
    result = n.recover_namespace(case.root, source, session, recovery)
    assert result.revision == before.revision and result.cas_binding == before.cas
    assert replacement.read(case.root) == prior


def fault_proof(tmp_path, monkeypatch, Case):
    import sqlite3

    from castle import namespace_store as store
    from castle import namespace_transactions as transactions

    root_proof(tmp_path, monkeypatch, Case)
    later_cutover_proof(tmp_path, monkeypatch, Case)
    advice_boundary_proof(tmp_path, monkeypatch, Case)

    # A nested user metadata value is not the command's top-level kind.
    case = Case(tmp_path / "ordinary-pending-excision-word")
    with case.open() as handle:
        request = case.directory_request(handle, "ordinary")
        request = replace(request, args={**request.args, "metadata": {"kind": "excise"}})
        with monkeypatch.context() as patch:

            def interrupted_finish(*args):
                raise OSError("interrupted ordinary prepared command")

            patch.setattr(transactions, "finish", interrupted_finish)
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                n.execute_namespace(handle, request, {})
        assert handle.db.execute(
            "SELECT state FROM operations WHERE operation_id=?", (request.operation_id,)
        ).fetchone() == ("prepared",)
    assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 1
    with case.open() as handle:
        replay = n.execute_namespace(handle, request, {})
        assert replay.revision_after == 1
        entry = handle.entries()[request.args["entry_id"]]
        assert entry.metadata == {"kind": "excise"}

    abort_proof(tmp_path, monkeypatch, Case)
    for fault in (
        "zero-write",
        "file-sync",
        "directory-sync",
        "uncertain-intent",
        "uncertain-references",
        "uncertain-completion",
        "complete-sync",
    ):
        case, request, roots, recovery = _small_case(tmp_path, Case, "excision-fault-" + fault)
        fired = []
        handle = case.open()
        with monkeypatch.context() as patch:
            if fault == "zero-write":
                original = os.pwrite

                def fail_write(fd, raw, offset):
                    if not fired:
                        fired.append(True)
                        return 0
                    return original(fd, raw, offset)

                patch.setattr(os, "pwrite", fail_write)
            elif fault == "file-sync":
                original = replacement.sync_file

                def fail_file(file):
                    if not fired:
                        fired.append(True)
                        raise OSError("injected file synchronization failure")
                    original(file)

                patch.setattr(replacement, "sync_file", fail_file)
            elif fault in {"directory-sync", "complete-sync"}:
                original = store_state._fsync_directory

                def fail_directory(directory):
                    marker = replacement.read(case.root)
                    match = directory == case.root and (
                        fault == "directory-sync"
                        or marker is not None
                        and marker["stage"] == "complete"
                    )
                    if match:
                        fired.append(True)
                        raise OSError("injected persistent directory synchronization failure")
                    original(directory)

                patch.setattr(store_state, "_fsync_directory", fail_directory)
            else:
                original = store._commit

                def fail_commit(connection):
                    row = connection.execute(
                        "SELECT state,ownership_receipt FROM operations WHERE operation_id=?",
                        (request.operation_id,),
                    ).fetchone()
                    completion = connection.execute(
                        "SELECT completed FROM replacement_completion WHERE operation_id=?",
                        (request.operation_id,),
                    ).fetchone()
                    value = json.loads(row[1]).get("cutover", {}) if row and row[1] else {}
                    match = (
                        (fault == "uncertain-intent" and row and row[0] == "staging")
                        or (
                            fault == "uncertain-references"
                            and value.get("stage") == "references-published"
                        )
                        or (fault == "uncertain-completion" and completion == (1,))
                    )
                    original(connection)
                    if match and not fired:
                        fired.append(True)
                        error = sqlite3.OperationalError(
                            "injected post-COMMIT durability uncertainty"
                        )
                        error.sqlite_errorcode = sqlite3.SQLITE_IOERR_FSYNC
                        raise error

                patch.setattr(store, "_commit", fail_commit)
            with pytest.raises(
                n.NamespaceError, match="namespace-storage-recovery-required"
            ) as failure:
                n.execute_namespace(handle, request, {}, roots)
            assert failure.value.effect == "recovery-required" and fired, fault
            handle.close()
            if fault in {"directory-sync", "complete-sync"}:
                before = len(fired)
                with pytest.raises(n.NamespaceError):
                    n.recover_namespace(case.root, case.cas_root, case.session, recovery)
                assert len(fired) > before
        recovered = n.recover_namespace(case.root, case.cas_root, case.session, recovery)
        assert recovered.revision == 3 and recovered.state == "ready", fault
        assert n.recover_namespace(case.root, case.cas_root, case.session, recovery) == recovered
    # Wrong explicit roots, foreign marker generations, unknown sidecars and
    # corrupted images must refuse before renames or guessed cleanup.
    case, request, roots, recovery = _small_case(tmp_path, Case, "excision-invalid-images")
    with case.open() as handle:
        with monkeypatch.context() as patch:
            patch.setattr(
                cutover,
                "_install_database",
                lambda *args: (_ for _ in ()).throw(OSError("injected installation hold")),
            )
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                n.execute_namespace(handle, request, {}, roots)
    marker = replacement.read(case.root)
    assert marker["stage"] == "image-ready"

    def image():
        return {
            str(file): file.read_bytes()
            for root in (case.root, case.cas_root, roots["successor_root"])
            for file in root.rglob("*")
            if file.is_file()
        }

    before = image()
    with pytest.raises(n.NamespaceError):
        n.recover_namespace(
            case.root,
            case.cas_root,
            case.session,
            {**recovery, "successor_root": case.directory / "wrong-successor"},
        )
    assert image() == before and not (case.directory / "wrong-successor").exists()
    marker_file = case.root / "replacement.json"
    original_marker = marker_file.read_bytes()
    marker_file.write_bytes(encoded({**marker, "target_generation": identity(99)}))
    before = image()
    with pytest.raises(n.NamespaceError):
        n.recover_namespace(case.root, case.cas_root, case.session, recovery)
    assert image() == before
    marker_file.write_bytes(original_marker)
    unknown = case.root / "unknown.sqlite-journal"
    unknown.write_bytes(b"caller-owned unknown sidecar")
    before = image()
    with pytest.raises(n.NamespaceError):
        n.recover_namespace(case.root, case.cas_root, case.session, recovery)
    assert image() == before
    unknown.unlink()
    new_file = case.root / replacement.filenames(marker)[0]
    original_image = new_file.read_bytes()
    new_file.write_bytes(original_image[:-1] + bytes([original_image[-1] ^ 1]))
    before = image()
    with pytest.raises(n.NamespaceError):
        n.recover_namespace(case.root, case.cas_root, case.session, recovery)
    assert image() == before
    new_file.write_bytes(original_image)
    assert n.recover_namespace(case.root, case.cas_root, case.session, recovery).revision == 3


def abort_proof(tmp_path, monkeypatch, Case):
    for point in (
        "before-cas",
        "prepared-successor",
        "interrupted-cleanup",
        "retirement-temporary",
    ):
        case, request, roots, recovery = _small_case(tmp_path, Case, "excision-abort-" + point)
        with case.open() as handle:
            with monkeypatch.context() as patch:
                if point == "prepared-successor":
                    original = store_state._publish_metadata

                    def before_retirement(file, value, *args, **kwargs):
                        if file == case.cas_root / "retired.json":
                            raise OSError("injected before source retirement")
                        return original(file, value, *args, **kwargs)

                    patch.setattr(store_state, "_publish_metadata", before_retirement)
                else:

                    def before_cas(*args):
                        raise OSError("injected before CAS cutover")

                    patch.setattr(cutover, "_cas_transition", before_cas)
                with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                    n.execute_namespace(handle, request, {}, roots)
        recovery["action"] = "abort-before-retirement"
        if point == "retirement-temporary":
            temporary = case.cas_root / ".retired.json.tmp"
            temporary.write_bytes(b"uncertain retirement writer residue")
            before = {
                str(file): file.read_bytes()
                for root in (case.root, case.cas_root)
                for file in root.rglob("*")
                if file.is_file()
            }
            with pytest.raises(n.NamespaceError):
                n.recover_namespace(case.root, case.cas_root, case.session, recovery)
            assert before == {
                str(file): file.read_bytes()
                for root in (case.root, case.cas_root)
                for file in root.rglob("*")
                if file.is_file()
            }
            temporary.unlink()  # Test-owned corruption only; production refused it.
        if point == "interrupted-cleanup":
            original_remove = replacement.remove_owned
            fired = []

            def interrupted_remove(file, **kwargs):
                original_remove(file, **kwargs)
                if file.parent.name == request.operation_id and not fired:
                    fired.append(True)
                    raise OSError("injected after recipe removal")

            with monkeypatch.context() as patch:
                patch.setattr(replacement, "remove_owned", interrupted_remove)
                with pytest.raises(n.NamespaceError):
                    n.recover_namespace(case.root, case.cas_root, case.session, recovery)
            assert fired
            with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                case.open()
        result = n.recover_namespace(case.root, case.cas_root, case.session, recovery)
        assert result.state == "ready" and result.revision == 2
        assert result.cas_binding == request.expected_cas and result.completed_operation_ids == []
        assert replacement.read(case.root) is None
        assert n.recover_namespace(case.root, case.cas_root, case.session, recovery) == result
        with case.open() as handle:
            assert len(handle.entries()) == 2
            assert n.namespace_operation(handle, request.operation_id).state == "rejected"
        if point == "prepared-successor":
            metadata = store_state._read_metadata(roots["successor_root"] / "store.json")
            assert metadata["state"] == "prepared"
            assert metadata["transition"]["operation_id"] == request.operation_id
            # A prepared caller-owned destination is intentionally left fenced.
            with pytest.raises(store_state.StoreStateError):
                store_state.read_store_identity(roots["successor_root"])
        else:
            assert not roots["successor_root"].exists()


def concurrency_proof(tmp_path, monkeypatch, Case):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    case, request, roots, recovery = _small_case(tmp_path, Case, "excision-concurrent-admission")
    entered, release = threading.Event(), threading.Event()
    original = cutover._cas_transition
    handle = case.open()
    with monkeypatch.context() as patch:

        def paused(*args):
            entered.set()
            assert release.wait(10)
            return original(*args)

        patch.setattr(cutover, "_cas_transition", paused)
        with ThreadPoolExecutor(max_workers=3) as pool:
            worker = pool.submit(n.execute_namespace, handle, request, {}, roots)
            try:
                assert entered.wait(10)
                assert (
                    n.namespace_operation(handle, request.operation_id).state == "recovery-required"
                )
                with pytest.raises(n.NamespaceError, match="namespace-storage-recovery-required"):
                    handle.info()
                with pytest.raises(n.NamespaceError, match="namespace-coordinator-busy"):
                    case.open()
                # A competing same-token request cannot start a second cutover
                # while the durable intent is blocked under the sole owner.
                retry = pool.submit(n.execute_namespace, handle, request, {}, roots)
                release.set()
                result = worker.result(timeout=10)
                with pytest.raises(n.NamespaceError):
                    retry.result(timeout=10)
                assert result.revision_after == 3
            finally:
                release.set()
    assert handle.closed
    assert n.recover_namespace(case.root, case.cas_root, case.session, recovery).revision == 3
    # An admitted export holds the same owner until its destination returns.
    case, request, roots, _ = _small_case(tmp_path, Case, "excision-drains-export")
    callback, finish = threading.Event(), threading.Event()
    intent_started = threading.Event()
    original_prepare = cutover.prepare_intent

    def observe_prepare(*args):
        intent_started.set()
        return original_prepare(*args)

    class Destination:
        def publish(self, view, raw, read_content):
            callback.set()
            assert finish.wait(10)
            member = next(entry["cas_id"] for entry in view.manifest["entries"] if entry["cas_id"])
            assert read_content(member) == b"Synthetic cutover fault and concurrency payload.\n"
            return n.ExportReceipt(view.snapshot.snapshot_id, view.snapshot.cas_id)

    with case.open() as handle:
        info = handle.info()
        read = n.ReadRequest(
            info.namespace_id, info.cas, None, "export-snapshot", {"snapshot_id": identity(81)}
        )
        with monkeypatch.context() as patch:
            patch.setattr(cutover, "prepare_intent", observe_prepare)
            with ThreadPoolExecutor(max_workers=2) as pool:
                export = pool.submit(n.read_namespace, handle, read, destination=Destination())
                try:
                    assert callback.wait(10)
                    excise = pool.submit(n.execute_namespace, handle, request, {}, roots)
                    assert not intent_started.wait(0.05)
                    finish.set()
                    assert export.result(timeout=10).value.snapshot_id == identity(81)
                    assert excise.result(timeout=10).revision_after == 3
                finally:
                    finish.set()


def restart_proof(tmp_path, monkeypatch, Case):
    import subprocess
    import sys
    from pathlib import Path

    fault_proof(tmp_path, monkeypatch, Case)
    for point in (
        "before-intent",
        "after-intent",
        "recipe-prefix",
        "recipe-durable",
        "before-blocked",
        "after-blocked",
        "before-retirement",
        "after-retirement",
        "before-cas-receipt",
        "after-cas-receipt",
        "after-successor-blocked",
        "before-publication-receipt",
        "after-publication-receipt",
        "before-references",
        "after-references",
        "image-prefix",
        "after-image-ready",
        "before-rename-old",
        "after-rename-old",
        "before-rename-target",
        "after-rename-target",
        "after-database-published",
        "after-cleanup-scanned",
        "selected-request-disposal",
        "selected-receipt-disposal",
        "before-old-disposal",
        "after-old-disposal",
        "after-cleanup-verified",
        "before-completion",
        "after-completion",
        "before-complete",
        "after-complete",
        "response",
    ):
        case = Case(tmp_path / ("excision-kill-" + point))
        with case.open() as handle:
            selected_id = identity(70)
            data = (
                b"Independently chosen removed-only content sentinel for restart qualification.\n"
            )
            cas_id = "sha256:" + hashlib.sha256(data).hexdigest()
            created_request = case.request(
                handle,
                "create-file",
                {
                    "entry_id": selected_id,
                    "parent_id": case.initial.root_id,
                    "name": "Removed-only restart name sentinel",
                    "metadata": {"secret": "Removed-only restart metadata sentinel"},
                    "content": {"cas_id": cas_id, "size": len(data)},
                },
            )
            n.execute_namespace(handle, created_request, {cas_id: data})
            snapshot = case.apply(handle, "snapshot", {"snapshot_id": identity(71)}).snapshot
            operations = [
                row[0] for row in handle.db.execute("SELECT operation_id FROM operations")
            ]
            request = case.request(
                handle,
                "excise",
                {
                    "selection": {
                        "content_ids": [cas_id, snapshot.cas_id],
                        "entry_ids": [selected_id],
                        "metadata_entry_ids": [],
                        "operation_ids": operations,
                    },
                    "snapshot_actions": [
                        {
                            "snapshot_id": snapshot.snapshot_id,
                            "action": "replace",
                            "remove_entry_ids": [selected_id],
                            "clear_metadata_entry_ids": [],
                        }
                    ],
                },
            )
        destination = case.directory / "successor"
        payload = {
            "root": str(case.root),
            "source": str(case.cas_root),
            "destination": str(destination),
            "domain": str(case.directory / "domain.lock"),
            "caller": str(Path(__file__).parents[1] / "examples" / "namespace_demo.py"),
            "request": asdict(request),
            "point": point,
        }
        child = subprocess.run(
            [sys.executable, "-c", _KILL_CHILD, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert child.returncode == 97, (point, child.returncode, child.stdout, child.stderr)
        if point == "before-intent":
            assert n.recover_namespace(case.root, case.cas_root, case.session, None).revision == 2
            with case.open() as handle:
                result = n.execute_namespace(
                    handle,
                    request,
                    {},
                    {"predecessor_root": case.cas_root, "successor_root": destination},
                )
                assert result.revision_after == 3
        else:
            if point not in {"after-complete", "response"}:
                with pytest.raises(
                    n.NamespaceError, match="namespace-(storage-recovery-required|binding-mismatch)"
                ):
                    case.open()
            recovery = {
                "predecessor_root": case.cas_root,
                "successor_root": destination,
                "expected_predecessor": request.expected_cas,
                "operation_id": request.operation_id,
                "action": "resume",
            }
            recovered = n.recover_namespace(case.root, case.cas_root, case.session, recovery)
            assert recovered.state == "ready" and recovered.revision == 3, point
            assert (
                n.recover_namespace(case.root, case.cas_root, case.session, recovery) == recovered
            )
        next_session = type(case.session)(case.root, destination, case.directory / "domain.lock")
        with n.open_namespace(case.root, destination, next_session) as handle:
            assert set(handle.entries()) == {case.initial.root_id}, point
            assert handle.info().revision == 3
            assert n.namespace_operation(handle, request.operation_id).result.revision_after == 3
            assert cas_id not in handle.objects and snapshot.cas_id not in handle.objects
            assert selected_id not in handle.entries(live=False)
            assert all(n.namespace_operation(handle, op).state == "retired" for op in operations)
        removed_sentinels = [
            data,
            b"Removed-only restart name sentinel",
            b"Removed-only restart metadata sentinel",
            cas_id.encode(),
            snapshot.cas_id.encode(),
        ]
        # Independently chosen controls existed in public input, not supplied
        # by the production builder being tested. Reservations remain allowed.
        assert removed_sentinels[1] in encoded(asdict(created_request))
        assert removed_sentinels[2] in encoded(asdict(created_request))
        assert removed_sentinels[3] in encoded(asdict(created_request))
        assert removed_sentinels[4] in encoded(asdict(request))
        for directory in (case.root, destination, case.cas_root):
            for file in directory.rglob("*"):
                if file.is_file():
                    raw = file.read_bytes()
                    assert not any(sentinel in raw for sentinel in removed_sentinels), (point, file)
