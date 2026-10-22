"""Consumer protocols composed into existing CLI/namespace proof families."""

from __future__ import annotations

import ast
import gc
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import unicodedata
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest

from castle import cas, cli, store_state
from castle import namespace as n
from castle.namespace_environment import DirectoryDestination, LocalSession


def _id() -> str:
    return str(uuid.uuid4())


def isolation_protocol() -> None:
    """Independent import inventory for every delivered namespace module."""
    directory = Path(n.__file__).parent
    for file in directory.glob("namespace*.py"):
        tree = ast.parse(file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [item.name.split(".")[0] for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            assert set(roots) <= set(sys.stdlib_module_names) | {"castle"}


def command_protocol(root: Path) -> None:
    """Real public subprocesses, independently expected bytes/tree/outcomes."""
    root.mkdir()
    root = root.resolve()
    cas_root, namespace_root, lock = root / "cas", root / "namespace", root / "domain.lock"
    base = [
        "namespace",
        "--root",
        str(cas_root),
        "--namespace-root",
        str(namespace_root),
        "--domain-lock",
        str(lock),
    ]

    def invoke(args: list[str], code: int = 0) -> dict:
        result = subprocess.run(
            [sys.executable, "-m", "castle.cli", *args], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == code, (args, result.stdout, result.stderr)
        if code == 0:
            assert result.stderr == ""
            return json.loads(result.stdout)
        assert result.stdout == ""
        value = json.loads(result.stderr)
        assert set(value) == {"code", "namespace_id", "operation_id", "actual_revision", "effect"}
        assert str(root) not in result.stderr
        return value

    assert cli.main(["init", "--root", str(cas_root)]) == 0
    assert cli.main(["rebuild-index", "--root", str(cas_root)]) == 0
    binding = invoke(["namespace", "--root", str(cas_root), "identity"])
    assert binding == store_state.read_store_identity(cas_root).to_dict()
    assert not lock.exists() and not namespace_root.exists()
    initialization = n.InitializeRequest(
        _id(),
        _id(),
        n.StoreIdentity(**binding),
        {"policy_version": 1, "unicode_data_version": unicodedata.unidata_version},
        {"kind": "unprotected"},
    )

    def transport(
        verb: str, request: object, extra: list[str] | None = None, code: int = 0
    ) -> dict:
        file = root / "request.json"
        file.write_text(
            json.dumps(
                asdict(request) if hasattr(request, "__dataclass_fields__") else request, indent=2
            )
        )
        actual = invoke([*base, verb, "--request-file", str(file), *(extra or [])], code)
        if code == 0 and hasattr(request, "__dataclass_fields__"):
            with n.open_namespace(
                namespace_root, cas_root, LocalSession(namespace_root, cas_root, lock)
            ) as handle:
                if type(request) is n.MutationRequest:
                    expected = n.execute_namespace(handle, request, {})
                elif type(request) is n.ReadRequest:
                    destination = (
                        DirectoryDestination(
                            root / ("api-export-" + _id()), namespace_root, cas_root
                        )
                        if request.kind == "export-snapshot"
                        else None
                    )
                    expected = n.read_namespace(handle, request, destination=destination)
                else:
                    expected = handle.info()
                assert actual == asdict(expected)
        return actual

    info = transport("init", initialization)
    assert info["revision"] == 0
    request = root / "request.json"
    folder, empty, member, destination = _id(), _id(), _id(), _id()

    def change(verb: str, kind: str, args: dict, extra: list[str] | None = None) -> dict:
        current = invoke([*base, "info"])
        value = n.MutationRequest(
            info["namespace_id"], n.StoreIdentity(**binding), current["revision"], _id(), kind, args
        )
        return transport(verb, value, extra)

    for identity, parent, name in [
        (folder, info["root_id"], "Folder"),
        (empty, folder, "Empty"),
        (destination, info["root_id"], "Destination"),
    ]:
        change(
            "create-directory",
            "create-directory",
            {"entry_id": identity, "parent_id": parent, "name": name, "metadata": {}},
        )
    payload = bytes(range(256)) + b"\x00caller-owned"
    content = "sha256:" + hashlib.sha256(payload).hexdigest()
    source = root / "source.bin"
    source.write_bytes(payload)
    created = change(
        "create-file",
        "create-file",
        {
            "entry_id": member,
            "parent_id": folder,
            "name": "snapshot.json",
            "metadata": {},
            "content": {"cas_id": content, "size": len(payload)},
        },
        ["--content", f"{content}={source}"],
    )
    saved = json.loads(request.read_text())
    for supplied, expected in (
        (root / "absent", "namespace-request-invalid"),
        (source, "namespace-content-mismatch"),
    ):
        invalid = {**saved, "operation_id": _id(), "expected_revision": created["revision_after"]}
        invalid["args"] = {
            **saved["args"],
            "entry_id": _id(),
            "name": "Input refusal",
            "content": {"cas_id": "sha256:" + "1" * 64, "size": len(payload)},
        }
        failed = transport(
            "create-file", invalid, ["--content", "sha256:" + "1" * 64 + "=" + str(supplied)], 2
        )
        assert failed["code"] == expected and failed["effect"] == "none"
        assert invoke([*base, "status", invalid["operation_id"]])["state"] == "unknown"
    assert invoke([*base, "status", created["operation_id"]])["result"] == created
    for verb in ("rename", "move", "delete"):
        wrong = {**saved, "operation_id": _id()}
        assert transport(verb, wrong, code=2)["code"] == "namespace-request-invalid"
        assert invoke([*base, "status", wrong["operation_id"]])["state"] == "unknown"
    for special in ("link-input", "fifo-input"):
        named = root / special
        if special == "link-input":
            named.symlink_to(source)
        else:
            os.mkfifo(named)
        invalid = {**saved, "operation_id": _id(), "expected_revision": created["revision_after"]}
        invalid["args"] = {**saved["args"], "entry_id": _id(), "name": "Unsafe input"}
        assert (
            transport("create-file", invalid, ["--content", f"{content}={named}"], 2)["code"]
            == "namespace-request-invalid"
        )
        assert invoke([*base, "status", invalid["operation_id"]])["state"] == "unknown"
    wrong_base = list(base)
    wrong_base[wrong_base.index("--namespace-root") + 1] = str(root / "absent-namespace")
    assert invoke([*wrong_base, "info"], 2)["code"] == "namespace-root-unsafe"
    assert not (root / "absent-namespace").exists()
    other_cas = root / "other-cas"
    assert cli.main(["init", "--root", str(other_cas)]) == 0
    assert cli.main(["rebuild-index", "--root", str(other_cas)]) == 0
    wrong_base = list(base)
    wrong_base[wrong_base.index("--root") + 1] = str(other_cas)
    assert invoke([*wrong_base, "info"], 2)["code"] == "namespace-binding-mismatch"
    protected = asdict(initialization)
    protected["protection"] = {"kind": "protected"}
    assert transport("init", protected, code=2)["code"] == "namespace-protection-unsupported"
    recovery = {
        "predecessor_root": str(other_cas),
        "successor_root": str(root / "successor"),
        "operation_id": _id(),
        "expected_predecessor": binding,
        "action": "resume",
    }
    assert transport("recover", recovery, code=2)["code"] == "namespace-request-invalid"
    assert invoke([*base, "info"])["revision"] == created["revision_after"]
    source.unlink()
    assert (
        transport("create-file", saved, ["--content-file", str(root / "absent-associations")])
        == created
    )
    assert transport("create-file", saved, ["--content", f"{content}={source}"]) == created
    assert (
        invoke([*base, "recover", "--request-file", str(_write(root / "recovery.json", None))])[
            "state"
        ]
        == "ready"
    )
    # CLI rename and move are exact presentations of relocate, preserving IDs.
    change(
        "rename", "relocate", {"entry_id": folder, "parent_id": info["root_id"], "name": "Renamed"}
    )
    change("move", "relocate", {"entry_id": folder, "parent_id": destination, "name": "Renamed"})
    copied = {old: _id() for old in (folder, empty, member)}
    change(
        "duplicate",
        "duplicate",
        {
            "entry_id": folder,
            "parent_id": info["root_id"],
            "name": "Copy",
            "identities": [{"source_id": old, "new_id": new} for old, new in copied.items()],
        },
    )
    replacement = b"independently expected replacement\n"
    replacement_id = "sha256:" + hashlib.sha256(replacement).hexdigest()
    source.write_bytes(replacement)
    associations = _write(root / "sources.json", {replacement_id: str(source)})
    changed = change(
        "replace-content",
        "replace-content",
        {"entry_id": member, "content": {"cas_id": replacement_id, "size": len(replacement)}},
        ["--content-file", str(associations)],
    )
    assert cas.require_object(cas_root, content).read_bytes() == payload
    assert cas.require_object(cas_root, replacement_id).read_bytes() == replacement
    for verb, kind, args, expected in [
        ("read", "get-entry", {"entry_id": member}, member),
        ("resolve", "resolve", {"components": ["Destination", "Renamed", "snapshot.json"]}, member),
        ("read", "get-entry", {"entry_id": copied[member]}, copied[member]),
    ]:
        value = transport(
            verb, n.ReadRequest(info["namespace_id"], n.StoreIdentity(**binding), None, kind, args)
        )["value"]
        assert value["entry_id"] == expected
        assert value["cas_id"] == (content if expected == copied[member] else replacement_id)
    listed = transport(
        "list",
        n.ReadRequest(
            info["namespace_id"],
            n.StoreIdentity(**binding),
            None,
            "list-directory",
            {"entry_id": empty},
        ),
    )["value"]
    assert listed == []
    snapshot_id = _id()
    snapshot = change("snapshot", "snapshot", {"snapshot_id": snapshot_id})["snapshot"]
    view_request = n.ReadRequest(
        info["namespace_id"],
        n.StoreIdentity(**binding),
        None,
        "inspect-snapshot",
        {"snapshot_id": snapshot_id},
    )
    view = transport("inspect-snapshot", view_request)["value"]
    assert view["snapshot"] == snapshot
    out = root / "export"
    export_request = n.ReadRequest(
        info["namespace_id"],
        n.StoreIdentity(**binding),
        None,
        "export-snapshot",
        {"snapshot_id": snapshot_id},
    )
    assert (
        transport("export-snapshot", export_request, ["--out", str(out)])["value"]["cas_id"]
        == snapshot["cas_id"]
    )
    assert (out / "tree/Destination/Renamed/snapshot.json").read_bytes() == replacement
    assert (out / "tree/Copy/snapshot.json").read_bytes() == payload
    assert (out / "tree/Copy/Empty").is_dir()
    assert json.loads((out / "snapshot.json").read_bytes()) == view["manifest"]
    refused = transport("export-snapshot", export_request, ["--out", str(out)], 2)
    assert refused["code"] == "namespace-root-unsafe"
    change("delete-subtree", "delete-subtree", {"entry_id": copied[folder]})
    assert transport("inspect-snapshot", view_request)["value"] == view
    change("delete", "delete-entry", {"entry_id": empty})
    change("set-metadata", "set-metadata", {"entry_id": member, "metadata": {"label": "allowed"}})
    change("delete-snapshot", "delete-snapshot", {"snapshot_id": snapshot_id})
    frozen, frozen_empty, frozen_file = _id(), _id(), _id()
    inventory = [
        {
            "entry_id": frozen,
            "kind": "directory",
            "parent_id": info["root_id"],
            "name": "Frozen",
            "metadata": {},
            "cas_id": None,
            "size": None,
        },
        {
            "entry_id": frozen_empty,
            "kind": "directory",
            "parent_id": frozen,
            "name": "Empty",
            "metadata": {},
            "cas_id": None,
            "size": None,
        },
        {
            "entry_id": frozen_file,
            "kind": "file",
            "parent_id": frozen,
            "name": "stored.bin",
            "metadata": {},
            "cas_id": content,
            "size": len(payload),
        },
    ]
    admitted = change(
        "import-folder", "import-folder", {"parent_id": info["root_id"], "entries": inventory}
    )
    assert set(admitted["affected_entry_ids"]) == {frozen, frozen_empty, frozen_file}
    change("expire-results", "expire-results", {"operation_ids": []})
    # Actual subprocess same-token race: busy is retryable, never a second commit.
    current = invoke([*base, "info"])
    raced = n.MutationRequest(
        info["namespace_id"],
        n.StoreIdentity(**binding),
        current["revision"],
        _id(),
        "create-directory",
        {"entry_id": _id(), "parent_id": info["root_id"], "name": "Race", "metadata": {}},
    )
    race_file = _write(root / "race.json", asdict(raced))

    def race():
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "castle.cli",
                *base,
                "create-directory",
                "--request-file",
                str(race_file),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        races = list(pool.map(lambda _: race(), range(2)))
    assert any(result.returncode == 0 for result in races)
    for result in races:
        assert result.returncode in {0, 2}
        if result.returncode == 2:
            assert json.loads(result.stderr)["code"] == "namespace-coordinator-busy"
    retried = transport("create-directory", raced)
    assert retried["revision_after"] == current["revision"] + 1
    assert invoke([*base, "info"])["revision"] == retried["revision_after"]
    current = invoke([*base, "info"])
    response_request = n.MutationRequest(
        info["namespace_id"],
        n.StoreIdentity(**binding),
        current["revision"],
        _id(),
        "create-directory",
        {"entry_id": _id(), "parent_id": info["root_id"], "name": "Response café", "metadata": {}},
    )
    response_file = _write(root / "response.json", asdict(response_request))
    reader, writer = os.pipe()
    os.close(reader)
    try:
        response = subprocess.run(
            [
                sys.executable,
                "-m",
                "castle.cli",
                *base,
                "create-directory",
                "--request-file",
                str(response_file),
            ],
            stdout=writer,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    finally:
        os.close(writer)
    assert response.returncode == 74 and response.stderr == b""
    assert invoke([*base, "status", response_request.operation_id])["state"] == "committed"
    ascii_response = subprocess.run(
        [
            sys.executable,
            "-m",
            "castle.cli",
            *base,
            "create-directory",
            "--request-file",
            str(response_file),
        ],
        env={**os.environ, "PYTHONIOENCODING": "ascii"},
        capture_output=True,
        timeout=30,
    )
    assert ascii_response.returncode == 0 and ascii_response.stderr == b""
    assert json.loads(ascii_response.stdout)["operation_id"] == response_request.operation_id
    bad = _write(root / "bad-response.json", {})
    reader, writer = os.pipe()
    os.close(reader)
    try:
        response = subprocess.run(
            [
                sys.executable,
                "-m",
                "castle.cli",
                *base,
                "create-directory",
                "--request-file",
                str(bad),
            ],
            stdout=subprocess.PIPE,
            stderr=writer,
            timeout=30,
        )
    finally:
        os.close(writer)
    assert response.returncode == 74 and response.stdout == b""
    current = invoke([*base, "info"])
    interrupted = n.MutationRequest(
        info["namespace_id"],
        n.StoreIdentity(**binding),
        current["revision"],
        _id(),
        "create-directory",
        {
            "entry_id": _id(),
            "parent_id": info["root_id"],
            "name": "Interrupted response",
            "metadata": {},
        },
    )
    interrupt_file = _write(root / "interrupt.json", asdict(interrupted))
    child = """import json,os,signal,sys
from castle import cli
class InterruptedOutput:
 def write(self,value): os.kill(os.getpid(),signal.SIGINT)
 def flush(self): pass
sys.stdout=InterruptedOutput()
raise SystemExit(cli.main(json.loads(sys.argv[1])))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            child,
            json.dumps([*base, "create-directory", "--request-file", str(interrupt_file)]),
        ],
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 130 and result.stdout == b"" and result.stderr == b""
    assert invoke([*base, "status", interrupted.operation_id])["state"] == "committed"
    for raw in (json.dumps(saved).encode("utf-16"), b"\xef\xbb\xbf" + json.dumps(saved).encode()):
        request.write_bytes(raw)
        assert (
            invoke([*base, "create-file", "--request-file", str(request)], 2)["code"]
            == "namespace-request-invalid"
        )
    # Execute every printed retained-workspace command, not a parallel handwritten demo.
    workspace = root / "persistent-demo"
    example = Path(n.__file__).parents[1] / "examples/namespace_demo.py"
    result = subprocess.run(
        [sys.executable, str(example), "--workspace", str(workspace)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    executed = []
    for block in (workspace / "COMMANDS.txt").read_text().split("\n\n"):
        if not block.startswith(shlex.quote(sys.executable) + " "):
            continue
        command = shlex.split(block)
        if not command or command[0] != sys.executable:
            continue
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        request_name = (
            Path(command[command.index("--request-file") + 1]).stem
            if "--request-file" in command
            else None
        )
        if request_name in {"collision", "stale"}:
            assert result.returncode == 2 and result.stdout == ""
            assert json.loads(result.stderr)["code"] == (
                "namespace-name-conflict"
                if request_name == "collision"
                else "namespace-revision-conflict"
            )
        else:
            assert result.returncode == 0 and result.stderr == "", (
                command,
                result.stdout,
                result.stderr,
            )
            json.loads(result.stdout)
        executed.append(command)
    assert len(executed) == 11
    assert (
        workspace / "export/tree/Interactive/Stored.txt"
    ).read_bytes() == b"New interactive replacement, not previously published.\n"
    assert (workspace / "export/tree/Interactive/Empty").is_dir()
    # Stale and malformed transports cannot reserve a new outcome or refresh a revision.
    stale = {**saved, "operation_id": _id()}
    assert transport("create-file", stale, code=2)["code"] == "namespace-revision-conflict"
    for text in ('{"schema":1,"schema":2}', "-0", "1.0", "1e0", "NaN", '{"unknown":true}'):
        request.write_text(text)
        assert (
            invoke([*base, "create-directory", "--request-file", str(request)], 2)["code"]
            == "namespace-request-invalid"
        )
    assert changed["affected_entry_ids"] == [member]
    # Domain lock typo is never an implicit independent coordinator domain.
    lock.rename(root / "held-lock")
    assert invoke([*base, "info"], 2)["code"] == "namespace-root-unsafe"
    assert not lock.exists()


def _write(file: Path, value: object) -> Path:
    file.write_text(json.dumps(value))
    return file


def environment_protocol(case, monkeypatch) -> None:
    """Admission before access, weak fork ownership, collision/link-safe export."""
    session = LocalSession(case.root, case.cas_root, case.directory / "domain.lock")
    reference = weakref.ref(session)
    del session
    gc.collect()
    assert reference() is None
    absent = case.directory / "absent.lock"
    with pytest.raises(n.NamespaceError, match="namespace-root-unsafe") as missing:
        n.open_namespace(case.root, case.cas_root, LocalSession(case.root, case.cas_root, absent))
    assert missing.value.effect == "none"
    assert not absent.exists()
    denied = LocalSession(case.root, case.cas_root, case.directory / "domain.lock")
    denied.active = False
    touched = []

    def forbidden(*args, **kwargs):
        touched.append(True)
        raise AssertionError("Castle I/O preceded authorization")

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", forbidden)
        patch.setattr(sqlite3, "connect", forbidden)
        patch.setattr(cas, "_validated_authority", forbidden)
        with pytest.raises(n.NamespaceError, match="namespace-session-expired"):
            n.open_namespace(case.root, case.cas_root, denied)
    assert touched == []
    with case.open() as handle:
        folder = _id()
        case.apply(
            handle,
            "create-directory",
            {
                "entry_id": folder,
                "parent_id": case.initial.root_id,
                "name": "Empty",
                "metadata": {},
            },
        )
        member, archive_member = _id(), _id()
        case.apply(
            handle,
            "create-file",
            {
                "entry_id": member,
                "parent_id": case.initial.root_id,
                "name": "snapshot.json",
                "metadata": {},
                "content": {"cas_id": case.stored.cas_id, "size": case.source.stat().st_size},
            },
        )
        original = case.apply(handle, "snapshot", {"snapshot_id": _id()}).snapshot
        case.apply(
            handle,
            "create-file",
            {
                "entry_id": archive_member,
                "parent_id": case.initial.root_id,
                "name": "Archived manifest",
                "metadata": {},
                "content": {"cas_id": original.cas_id, "size": original.size},
            },
        )
        snapshot = case.apply(handle, "snapshot", {"snapshot_id": _id()}).snapshot
        info = handle.info()
        read = n.ReadRequest(
            info.namespace_id,
            info.cas,
            None,
            "export-snapshot",
            {"snapshot_id": snapshot.snapshot_id},
        )
        outside = case.directory / "outside"
        outside.mkdir()
        symlink = case.directory / "link"
        symlink.symlink_to(outside, target_is_directory=True)
        for output in (case.root / "escaped", case.directory / "link/output"):
            with pytest.raises(n.NamespaceError):
                n.read_namespace(
                    handle, read, destination=DirectoryDestination(output, case.root, case.cas_root)
                )
        assert list(outside.iterdir()) == []
        destination = DirectoryDestination(case.directory / "authorized", case.root, case.cas_root)
        exported = n.read_namespace(handle, read, destination=destination)
        assert exported.value == n.ExportReceipt(snapshot.snapshot_id, snapshot.cas_id)
        assert (case.directory / "authorized/tree/Empty").is_dir()
        view = n.read_namespace(
            handle,
            n.ReadRequest(
                info.namespace_id,
                info.cas,
                None,
                "inspect-snapshot",
                {"snapshot_id": snapshot.snapshot_id},
            ),
        ).value
        projection = {
            row["entry_id"]: (
                info.namespace_id,
                row["entry_id"],
                row["cas_id"],
                snapshot.snapshot_id,
            )
            for row in view.manifest["entries"]
            if row["kind"] == "file"
        }
        reconciled_revision = view.snapshot.captured_revision
        assert set(projection) == {member, archive_member}
        assert projection[archive_member][2] == original.cas_id
        assert snapshot.cas_id not in {citation[2] for citation in projection.values()}
        case.apply(
            handle, "relocate", {"entry_id": member, "parent_id": info.root_id, "name": "Changed"}
        )
        payload = b"later content outside the historical projection"
        content = "sha256:" + hashlib.sha256(payload).hexdigest()
        n.execute_namespace(
            handle,
            case.request(
                handle,
                "replace-content",
                {"entry_id": member, "content": {"cas_id": content, "size": len(payload)}},
            ),
            {content: payload},
        )
        case.apply(handle, "delete-entry", {"entry_id": member})
        assert reconciled_revision < handle.info().revision
        historical = n.read_namespace(
            handle,
            n.ReadRequest(
                info.namespace_id,
                info.cas,
                None,
                "inspect-snapshot",
                {"snapshot_id": snapshot.snapshot_id},
            ),
        ).value
        row = next(row for row in historical.manifest["entries"] if row["entry_id"] == member)
        assert projection[member] == (
            info.namespace_id,
            member,
            row["cas_id"],
            snapshot.snapshot_id,
        )
        assert row["cas_id"] == case.stored.cas_id
        other = type(case)(case.directory / "other-domain")
        before = handle.info()
        tree = handle.entries()
        with pytest.raises(n.NamespaceError, match="namespace-entry-missing"):
            n.execute_namespace(
                handle,
                case.request(
                    handle,
                    "relocate",
                    {
                        "entry_id": folder,
                        "parent_id": other.initial.root_id,
                        "name": "Cross domain",
                    },
                ),
                {},
            )
        assert handle.info() == before and handle.entries() == tree
        # Valid names and snapshot IDs cannot override a revoked caller session.
        case.session.active = False
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", forbidden)
            patch.setattr(sqlite3, "connect", forbidden)
            patch.setattr(cas, "_validated_authority", forbidden)
            for kind, args in [
                ("resolve", {"components": ["Empty"]}),
                ("inspect-snapshot", {"snapshot_id": snapshot.snapshot_id}),
            ]:
                with pytest.raises(n.NamespaceError, match="namespace-session-expired"):
                    n.read_namespace(
                        handle, n.ReadRequest(info.namespace_id, info.cas, None, kind, args)
                    )
        case.session.active = True
        assert touched == []
        partial = case.directory / "partial"
        broken = DirectoryDestination(partial, case.root, case.cas_root)
        original_file = broken._file
        calls = []

        def fail_member(parent, name, payload):
            calls.append(name)
            if len(calls) == 2:
                raise OSError("synthetic destination write failure")
            return original_file(parent, name, payload)

        with monkeypatch.context() as patch:
            patch.setattr(broken, "_file", fail_member)
            with pytest.raises(n.NamespaceError, match="namespace-io-failed"):
                n.read_namespace(handle, read, destination=broken)
        assert (partial / "snapshot.json").is_file()
        assert handle.info() == before
        # On case-insensitive filesystems, identity checks catch lexical aliases.
        alias = case.root.with_name(case.root.name.swapcase())
        if alias.exists() and alias.samefile(case.root):
            for root in (case.root, case.cas_root):
                variant = root.with_name(root.name.swapcase())
                with pytest.raises(n.NamespaceError, match="namespace-root-unsafe"):
                    n.read_namespace(
                        handle,
                        read,
                        destination=DirectoryDestination(
                            variant / "alias-output", case.root, case.cas_root
                        ),
                    )
                assert not (root / "alias-output").exists()
                with pytest.raises(n.NamespaceError, match="namespace-root-unsafe"):
                    with LocalSession(
                        case.root, case.cas_root, variant / "alias-lock", create_domain_lock=True
                    ).acquire_domain_ownership():
                        raise AssertionError("authority alias admitted")
                assert not (root / "alias-lock").exists()
            print("CASE-INSENSITIVE-AUTHORITY-ALIASES exercised")
        else:
            print("CASE-INSENSITIVE-AUTHORITY-ALIASES not-run: case-sensitive filesystem")
