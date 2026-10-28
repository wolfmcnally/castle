"""Check an installed Castle wheel using only disposable synthetic data."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unicodedata
import uuid
from pathlib import Path

import castle

VERSION = "0.2.1"


def main() -> None:
    prefix = Path(sys.prefix).resolve()
    module = Path(castle.__file__).resolve()
    if not module.is_relative_to(prefix):
        raise AssertionError(f"Castle import escaped installed environment: {module}")
    if castle.__version__ != VERSION:
        raise AssertionError(f"Unexpected version: {castle.__version__}")
    cli = prefix / "bin" / "castle"
    if not cli.is_file():
        raise AssertionError("Installed console entry point is absent")
    with tempfile.TemporaryDirectory(prefix="castle-wheel-smoke-") as directory:
        scratch = Path(directory).resolve()
        source, store = scratch / "source", scratch / "store"
        source.mkdir()
        (source / "example.txt").write_text("synthetic wheel smoke input\n")

        def run(*arguments: str) -> str:
            result = subprocess.run(
                [str(cli), *arguments],
                cwd=scratch,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                raise AssertionError(
                    f"{arguments[0]} failed ({result.returncode}): {result.stderr}"
                )
            print(f"{arguments[0]}: passed")
            return result.stdout

        run("--help")
        if run("--version").strip() != f"castle {VERSION}":
            raise AssertionError("Console version disagrees with package metadata")
        run("init", "--root", str(store))
        ingest = json.loads(
            run(
                "ingest",
                "--root",
                str(store),
                "--source",
                str(source),
                "--source-id",
                "smoke",
            )
        )
        if (
            ingest["new_objects"] != 1
            or ingest["files_seen"] != 1
            or ingest["refusals"]
        ):
            raise AssertionError("Synthetic ingest did not publish exactly one object")
        if json.loads(run("stat", "--root", str(store)))["objects"] != 1:
            raise AssertionError("Installed store did not report the synthetic object")
        for action in ("verify", "rebuild-index", "verify"):
            output = json.loads(run(action, "--root", str(store)))
            if action == "verify" and (output["ok"] is not True or output["errors"]):
                raise AssertionError(
                    "Installed verification did not establish clean authority"
                )
        records = [
            json.loads(line)
            for line in (store / "journal.jsonl").read_text().splitlines()
        ]
        if len(records) != 1 or records[0]["schema"] != "journal.v2":
            raise AssertionError(
                "Installed writer did not emit the public journal.v2 format"
            )

        from castle import namespace, namespace_cli, namespace_environment

        for loaded in (namespace, namespace_cli, namespace_environment):
            if loaded.__name__ not in sys.modules or not Path(
                loaded.__file__
            ).resolve().is_relative_to(prefix):
                raise AssertionError("Installed namespace import provenance failed")
        for name, loaded in tuple(sys.modules.items()):
            if name.startswith("castle.") and not Path(
                loaded.__file__
            ).resolve().is_relative_to(prefix):
                raise AssertionError("Installed namespace module escaped environment")
        namespace_root, domain = scratch / "namespace", scratch / "domain.lock"
        base = [
            "namespace",
            "--root",
            str(store),
            "--namespace-root",
            str(namespace_root),
            "--domain-lock",
            str(domain),
        ]
        binding = json.loads(run("namespace", "--root", str(store), "identity"))
        namespace_id, root_id, member = (str(uuid.uuid4()) for _ in range(3))
        request_file = scratch / "request.json"

        def request(verb: str, value: dict, *extra: str) -> dict:
            request_file.write_text(json.dumps(value))
            return json.loads(
                run(*base, verb, "--request-file", str(request_file), *extra)
            )

        initialized = request(
            "init",
            {
                "schema": "castle.namespace_initialize.v1",
                "namespace_id": namespace_id,
                "root_id": root_id,
                "expected_cas": binding,
                "policy": {
                    "policy_version": 1,
                    "unicode_data_version": unicodedata.unidata_version,
                },
                "protection": {"kind": "unprotected"},
            },
        )
        revision = initialized["revision"]

        def change(kind: str, args: dict, *extra: str) -> dict:
            nonlocal revision
            value = {
                "schema": "castle.namespace_request.v1",
                "namespace_id": namespace_id,
                "expected_cas": binding,
                "expected_revision": revision,
                "operation_id": str(uuid.uuid4()),
                "kind": kind,
                "args": args,
            }
            first = request(kind, value, *extra)
            if request(kind, value) != first:
                raise AssertionError("Installed same-token replay changed result")
            revision = first["revision_after"]
            return first

        input_file = scratch / "new.bin"
        first_bytes, last_bytes = (
            b"installed new namespace bytes\x00",
            b"installed replacement\xff",
        )
        for index, payload in enumerate((first_bytes, last_bytes)):
            content = "sha256:" + hashlib.sha256(payload).hexdigest()
            input_file.write_bytes(payload)
            args = {
                "entry_id": member,
                "content": {"cas_id": content, "size": len(payload)},
            }
            if index == 0:
                args.update(parent_id=root_id, name="snapshot.json", metadata={})
            change(
                "create-file" if index == 0 else "replace-content",
                args,
                "--content",
                content + "=" + str(input_file),
            )
            if json.loads(run(*base, "info"))["revision"] != revision:
                raise AssertionError("Installed acknowledgment revision mismatch")
            if (store / "objects").is_dir() is False:
                raise AssertionError(
                    "Installed publisher did not create object authority"
                )
        empty = str(uuid.uuid4())
        change(
            "create-directory",
            {"entry_id": empty, "parent_id": root_id, "name": "Empty", "metadata": {}},
        )
        snapshot_id = str(uuid.uuid4())
        snap = change("snapshot", {"snapshot_id": snapshot_id})["snapshot"]
        read = {
            "schema": "castle.namespace_read_request.v1",
            "namespace_id": namespace_id,
            "expected_cas": binding,
            "at_revision": None,
            "kind": "export-snapshot",
            "args": {"snapshot_id": snapshot_id},
        }
        output = scratch / "export"
        exported = request("export-snapshot", read, "--out", str(output))
        if exported["value"] != {"snapshot_id": snapshot_id, "cas_id": snap["cas_id"]}:
            raise AssertionError("Installed export receipt mismatch")
        if (output / "tree/snapshot.json").read_bytes() != last_bytes or not (
            output / "tree/Empty"
        ).is_dir():
            raise AssertionError("Installed export lost bytes or empty directory")
        manifest = json.loads((output / "snapshot.json").read_bytes())
        if member not in {row["entry_id"] for row in manifest["entries"]}:
            raise AssertionError("Installed replacement lost stable entry identity")
        # Delete is a namespace projection change, retaining bytes and logs.
        deletion = {
            "schema": "castle.namespace_request.v1",
            "namespace_id": namespace_id,
            "expected_cas": binding,
            "expected_revision": revision,
            "operation_id": str(uuid.uuid4()),
            "kind": "delete-entry",
            "args": {"entry_id": member},
        }
        deleted = request("delete", deletion)
        if request("delete", deletion) != deleted:
            raise AssertionError("Installed Delete retry changed outcome")
        revision = deleted["revision_after"]
        from castle import cas

        selected = [
            "sha256:" + hashlib.sha256(data).hexdigest()
            for data in (first_bytes, last_bytes)
        ]
        if any(not cas.require_object(store, key).is_file() for key in selected):
            raise AssertionError("Logical Delete removed content bytes")
        inventory_read = {
            "schema": "castle.namespace_read_request.v1",
            "namespace_id": namespace_id,
            "expected_cas": binding,
            "at_revision": revision,
            "kind": "inventory-excision",
            "args": {},
        }
        inventory = request("inventory-excision", inventory_read)["value"]
        if not any(
            item["entry"]["entry_id"] == member and not item["live"]
            for item in inventory["entries"]
        ):
            raise AssertionError(
                "Installed historical inventory omitted a deleted entry"
            )
        preview_read = {
            **inventory_read,
            "kind": "preview-excision",
            "args": {
                "operation_id": str(uuid.uuid4()),
                "selection": {
                    "content_ids": [*selected, snap["cas_id"]],
                    "entry_ids": [member],
                    "operation_ids": [],
                    "metadata_entry_ids": [],
                },
                "snapshot_actions": [{"snapshot_id": snapshot_id, "action": "drop"}],
            },
        }
        proposal = request("preview-excision", preview_read)["value"]
        if proposal["request"] is None or proposal["conflicting_content_ids"]:
            raise AssertionError("Installed closure proposal is unexpectedly blocked")
        proposed = proposal["request"]
        typed = namespace.MutationRequest(
            proposed["namespace_id"],
            namespace.StoreIdentity(**proposed["expected_cas"]),
            proposed["expected_revision"],
            proposed["operation_id"],
            proposed["kind"],
            proposed["args"],
        )
        successor = scratch / "successor"
        roots = {"predecessor_root": store, "successor_root": successor}
        session = namespace_environment.LocalSession(namespace_root, store, domain)
        with namespace.open_namespace(namespace_root, store, session) as handle:
            result = namespace.execute_namespace(handle, typed, {}, roots)
        if (
            result.cas_binding.generation != binding["generation"] + 1
            or result.revision_after != revision + 1
        ):
            raise AssertionError(
                "Installed excision did not advance epoch/revision once"
            )
        if any(
            cas.object_path(successor, key).exists()
            for key in [*selected, snap["cas_id"]]
        ):
            raise AssertionError("Installed excision retained selected bytes")
        new_base = [*base]
        new_base[new_base.index("--root") + 1] = str(successor)
        current = json.loads(run(*new_base, "info"))
        if current["cas"] != result.cas_binding.to_dict():
            raise AssertionError("Installed CLI/library successor identity differs")
        inventory_read.update(
            expected_cas=current["cas"], at_revision=current["revision"]
        )
        request_file.write_text(json.dumps(inventory_read))
        retained_inventory = json.loads(
            run(*new_base, "inventory-excision", "--request-file", str(request_file))
        )["value"]
        if member in {
            item["entry"]["entry_id"] for item in retained_inventory["entries"]
        }:
            raise AssertionError("Installed excision retained file metadata")
        recovery = {
            **roots,
            "operation_id": typed.operation_id,
            "expected_predecessor": typed.expected_cas,
            "action": "resume",
        }
        for _ in range(2):
            completed = namespace.recover_namespace(
                namespace_root,
                store,
                namespace_environment.LocalSession(namespace_root, store, domain),
                recovery,
            )
            if (
                completed.cas_binding != result.cas_binding
                or completed.revision != result.revision_after
            ):
                raise AssertionError(
                    "Installed repeated recovery changed the transition"
                )
        if not json.loads(run("verify", "--root", str(successor)))["ok"]:
            raise AssertionError("Installed successor CAS integrity failed")
        print(
            "Installed Delete/Excise inventory, preview, bytes, epoch and recovery passed"
        )
    print("Installed wheel import, version, CLI and journal.v2 smoke passed")


if __name__ == "__main__":
    main()
