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


def main() -> None:
    prefix = Path(sys.prefix).resolve()
    module = Path(castle.__file__).resolve()
    if not module.is_relative_to(prefix):
        raise AssertionError(f"Castle import escaped installed environment: {module}")
    if castle.__version__ != "0.1.1":
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
        if run("--version").strip() != "castle 0.1.1":
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
    print("Installed wheel import, version, CLI and journal.v2 smoke passed")


if __name__ == "__main__":
    main()
