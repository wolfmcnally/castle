"""Disposable new-content namespace workflow, using public APIs only.

Run from the checkout with its Python wrapper. All created stores are synthetic
and temporary. LocalSession is a trusted caller fixture, not authentication or
an encryption backend. No active namespace shares its CAS with another writer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import sys
import tempfile
import unicodedata
import uuid
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path

from castle import cas, store_state
from castle import namespace as n
from castle.namespace_environment import LocalSession


def identity() -> str:
    return str(uuid.uuid4())


def initialize(namespace_root: Path, cas_root: Path, session: LocalSession) -> n.NamespaceInfo:
    return n.create_namespace(
        namespace_root,
        cas_root,
        session,
        n.InitializeRequest(
            identity(),
            identity(),
            store_state.read_store_identity(cas_root),
            {"policy_version": 1, "unicode_data_version": unicodedata.unidata_version},
            {"kind": "unprotected"},
        ),
    )


def mutation(
    handle: n.NamespaceHandle, kind: str, args: dict, *, operation_id: str | None = None
) -> n.MutationRequest:
    info = handle.info()
    return n.MutationRequest(
        info.namespace_id, info.cas, info.revision, operation_id or identity(), kind, args
    )


def browse(handle: n.NamespaceHandle, root_id: str) -> list[n.EntryRecord]:
    info = handle.info()
    return n.read_namespace(
        handle,
        n.ReadRequest(info.namespace_id, info.cas, None, "list-directory", {"entry_id": root_id}),
    ).value


def show(handle: n.NamespaceHandle, label: str, directory_id: str) -> None:
    print(label)
    print("directory:", directory_id)
    entries = browse(handle, directory_id)
    if not entries:
        print("  (empty)")
    for entry in entries:
        print(
            " ",
            entry.name,
            "id=",
            entry.entry_id,
            "parent=",
            entry.parent_id,
            "content=",
            entry.cas_id,
        )


def excision_workflow(root: Path) -> None:
    """A separate disposable cutover with explicit selection and a new session."""
    root.mkdir()
    predecessor, successor = root / "before", root / "after"
    namespace_root, domain_lock = root / "namespace", root / "domain.lock"
    source = root / "retained-input.txt"
    source.write_bytes(b"Caller-owned original input deliberately remains outside CAS cleanup.\n")
    retained = cas.ingest_file(predecessor, source, "disposable-cutover-seed")
    session = LocalSession(namespace_root, predecessor, domain_lock, create_domain_lock=True)
    info = initialize(namespace_root, predecessor, session)
    with n.open_namespace(namespace_root, predecessor, session) as handle:
        folder, selected = identity(), identity()
        n.execute_namespace(
            handle,
            mutation(
                handle,
                "create-directory",
                {
                    "entry_id": folder,
                    "parent_id": info.root_id,
                    "name": "Retained empty folder",
                    "metadata": {},
                },
            ),
            {},
        )
        payload = b"Disposable selected namespace content.\n"
        cas_id = "sha256:" + hashlib.sha256(payload).hexdigest()
        created = n.execute_namespace(
            handle,
            mutation(
                handle,
                "create-file",
                {
                    "entry_id": selected,
                    "parent_id": info.root_id,
                    "name": "Selected.txt",
                    "metadata": {},
                    "content": {"cas_id": cas_id, "size": len(payload)},
                },
            ),
            {cas_id: payload},
        )
        snapshot = n.execute_namespace(
            handle, mutation(handle, "snapshot", {"snapshot_id": identity()}), {}
        )
        request = mutation(
            handle,
            "excise",
            {
                "selection": {
                    "content_ids": [cas_id, snapshot.snapshot.cas_id],
                    "entry_ids": [selected],
                    "metadata_entry_ids": [],
                    "operation_ids": [created.operation_id, snapshot.operation_id],
                },
                "snapshot_actions": [
                    {
                        "snapshot_id": snapshot.snapshot.snapshot_id,
                        "action": "replace",
                        "remove_entry_ids": [selected],
                        "clear_metadata_entry_ids": [],
                    }
                ],
            },
        )
        result = n.execute_namespace(
            handle, request, {}, {"predecessor_root": predecessor, "successor_root": successor}
        )
        assert handle.closed and result.revision_after == 4
    next_session = LocalSession(namespace_root, successor, domain_lock)
    with n.open_namespace(namespace_root, successor, next_session) as handle:
        current = handle.info()
        assert [entry.name for entry in browse(handle, current.root_id)] == [
            "Retained empty folder"
        ]
        assert cas.require_object(successor, retained.cas_id).read_bytes() == source.read_bytes()
        assert not cas.object_path(successor, cas_id).exists()
        status = n.namespace_operation(handle, request.operation_id)
        assert status.state == "retired" and status.result == result
        view = n.read_namespace(
            handle,
            n.ReadRequest(
                current.namespace_id,
                current.cas,
                None,
                "inspect-snapshot",
                {"snapshot_id": snapshot.snapshot.snapshot_id},
            ),
        ).value
        assert view.manifest["origin"] == {"kind": "redacted", "operation_id": request.operation_id}
        assert {entry["entry_id"] for entry in view.manifest["entries"]} == {info.root_id, folder}
    recovered = n.recover_namespace(
        namespace_root,
        predecessor,
        session,
        {
            "predecessor_root": predecessor,
            "successor_root": successor,
            "operation_id": request.operation_id,
            "expected_predecessor": info.cas,
            "action": "resume",
        },
    )
    assert recovered.state == "ready" and recovered.revision == 4
    print(
        "Disposable excision: selected content removed, retained folder and "
        "redacted snapshot reopened."
    )
    print(
        "Selection-free repeat recovery confirms completion; original caller "
        "input remains outside cleanup."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation-id", help="UUIDv4 token for the duplicate and its retry")
    parser.add_argument(
        "--workspace",
        type=Path,
        help="absent synthetic directory to preserve for interactive commands",
    )
    options = parser.parse_args()
    if options.workspace is not None:
        options.workspace.mkdir(mode=0o700)
        options.workspace = options.workspace.resolve()
    token = options.operation_id or identity()
    n._uuid(token)
    context = (
        tempfile.TemporaryDirectory(prefix="castle-namespace-demo-")
        if options.workspace is None
        else nullcontext(str(options.workspace))
    )
    with context as directory:
        root = Path(directory).resolve()
        cas_root, namespace_root = root / "cas", root / "namespace"
        source = root / "stored.txt"
        source.write_text("Immutable shared content.\n")
        cas.ingest_file(cas_root, source, "namespace-initialization-seed")
        created_bytes = b"Actual newly published namespace bytes.\n"
        created_content = {
            "cas_id": "sha256:" + hashlib.sha256(created_bytes).hexdigest(),
            "size": len(created_bytes),
        }
        session = LocalSession(
            namespace_root, cas_root, root / "domain.lock", create_domain_lock=True
        )
        info = initialize(namespace_root, cas_root, session)
        folder, empty, file_id, target = identity(), identity(), identity(), identity()
        with n.open_namespace(namespace_root, cas_root, session) as handle:
            for kind, args in [
                (
                    "create-directory",
                    {
                        "entry_id": folder,
                        "parent_id": info.root_id,
                        "name": "Documents",
                        "metadata": {},
                    },
                ),
                (
                    "create-directory",
                    {"entry_id": empty, "parent_id": folder, "name": "Empty", "metadata": {}},
                ),
                (
                    "create-file",
                    {
                        "entry_id": file_id,
                        "parent_id": folder,
                        "name": "Stored.txt",
                        "metadata": {},
                        "content": created_content,
                    },
                ),
            ]:
                n.execute_namespace(
                    handle,
                    mutation(handle, kind, args),
                    {created_content["cas_id"]: created_bytes} if kind == "create-file" else {},
                )
            show(handle, "Original root:", info.root_id)
            show(handle, "Original directory:", folder)
            show(handle, "Browse the empty directory:", empty)
            n.execute_namespace(
                handle,
                mutation(
                    handle,
                    "create-directory",
                    {
                        "entry_id": target,
                        "parent_id": info.root_id,
                        "name": "Destination",
                        "metadata": {},
                    },
                ),
                {},
            )
            n.execute_namespace(
                handle,
                mutation(
                    handle,
                    "relocate",
                    {
                        "entry_id": folder,
                        "parent_id": info.root_id,
                        "name": "Renamed",
                    },
                ),
                {},
            )
            show(handle, "After rename (same directory ID):", info.root_id)
            n.execute_namespace(
                handle,
                mutation(
                    handle,
                    "relocate",
                    {
                        "entry_id": folder,
                        "parent_id": target,
                        "name": "Renamed",
                    },
                ),
                {},
            )
            show(handle, "After move to Destination (same ID, changed parent):", target)
            show(handle, "Moved directory, unchanged member/content IDs:", folder)
            resolved = n.read_namespace(
                handle,
                n.ReadRequest(
                    info.namespace_id,
                    info.cas,
                    None,
                    "resolve",
                    {"components": ["Destination", "Renamed", "Stored.txt"]},
                ),
            ).value
            print("Resolved moved file:", resolved.entry_id, "content=", resolved.cas_id)
            mapping = [{"source_id": old, "new_id": identity()} for old in (folder, empty, file_id)]
            request = mutation(
                handle,
                "duplicate",
                {
                    "entry_id": folder,
                    "parent_id": info.root_id,
                    "name": "Copy",
                    "identities": mapping,
                },
                operation_id=token,
            )
            first = n.execute_namespace(handle, request, {})
            again = n.execute_namespace(handle, request, {})
            print("Retry token:", token)
            print("Same-token retry equals the original result:", again == first)
            assert again == first
            show(handle, "Root after atomic duplicate:", info.root_id)
            copy_root = next(pair["new_id"] for pair in mapping if pair["source_id"] == folder)
            copy_empty = next(pair["new_id"] for pair in mapping if pair["source_id"] == empty)
            show(handle, "Copy has fresh IDs and shared content:", copy_root)
            show(handle, "Copied empty directory:", copy_empty)
            replacement = b"Replacement bytes keep the original entry identity.\n"
            replacement_content = {
                "cas_id": "sha256:" + hashlib.sha256(replacement).hexdigest(),
                "size": len(replacement),
            }
            replace_request = mutation(
                handle, "replace-content", {"entry_id": file_id, "content": replacement_content}
            )
            replaced = n.execute_namespace(
                handle, replace_request, {replacement_content["cas_id"]: replacement}
            )
            assert n.execute_namespace(handle, replace_request, {}) == replaced
            current = n.read_namespace(
                handle,
                n.ReadRequest(
                    info.namespace_id, info.cas, None, "get-entry", {"entry_id": file_id}
                ),
            ).value
            copy_file = next(pair["new_id"] for pair in mapping if pair["source_id"] == file_id)
            copied = n.read_namespace(
                handle,
                n.ReadRequest(
                    info.namespace_id, info.cas, None, "get-entry", {"entry_id": copy_file}
                ),
            ).value
            assert (
                current.entry_id == file_id
                and current.name == resolved.name
                and current.parent_id == resolved.parent_id
                and current.metadata == resolved.metadata
            )
            assert cas.object_path(cas_root, current.cas_id).read_bytes() == replacement
            assert cas.object_path(cas_root, copied.cas_id).read_bytes() == created_bytes
            print("Replaced original, stable ID:", current.entry_id)
            print(
                "Original entry now holds replacement bytes:",
                cas.object_path(cas_root, current.cas_id).read_bytes(),
            )
            print(
                "Independent copy retains earlier bytes:",
                cas.object_path(cas_root, copied.cas_id).read_bytes(),
            )
            n.execute_namespace(
                handle, mutation(handle, "delete-subtree", {"entry_id": copy_root}), {}
            )
            print(
                "Root after deleting the copy:",
                [entry.name for entry in browse(handle, info.root_id)],
            )
            print("Original directory members:", [entry.name for entry in browse(handle, folder)])
            print(
                "Duplicate retry status:", n.namespace_operation(handle, request.operation_id).state
            )
            imported_folder, imported_empty, imported_file = identity(), identity(), identity()
            imported_bytes = b"New bytes in one atomic imported forest.\n"
            imported_id = "sha256:" + hashlib.sha256(imported_bytes).hexdigest()
            inventory = [
                {
                    "entry_id": imported_folder,
                    "kind": "directory",
                    "parent_id": info.root_id,
                    "name": "Imported folder",
                    "metadata": {},
                    "cas_id": None,
                    "size": None,
                },
                {
                    "entry_id": imported_empty,
                    "kind": "directory",
                    "parent_id": imported_folder,
                    "name": "Explicit empty",
                    "metadata": {},
                    "cas_id": None,
                    "size": None,
                },
                {
                    "entry_id": imported_file,
                    "kind": "file",
                    "parent_id": imported_folder,
                    "name": "New file.txt",
                    "metadata": {},
                    "cas_id": imported_id,
                    "size": len(imported_bytes),
                },
            ]
            admitted = n.execute_namespace(
                handle,
                mutation(
                    handle,
                    "import-folder",
                    {"parent_id": info.root_id, "entries": list(reversed(inventory))},
                ),
                {imported_id: imported_bytes},
            )
            assert set(admitted.affected_entry_ids) == {
                imported_folder,
                imported_empty,
                imported_file,
            }
            show(handle, "Atomically admitted folder:", imported_folder)
            snapshot_request = mutation(handle, "snapshot", {"snapshot_id": identity()})
            captured = n.execute_namespace(handle, snapshot_request, {})
            record = captured.snapshot
            view = n.read_namespace(
                handle,
                n.ReadRequest(
                    info.namespace_id,
                    info.cas,
                    None,
                    "inspect-snapshot",
                    {"snapshot_id": record.snapshot_id},
                ),
            ).value
            assert view.snapshot.captured_revision == admitted.revision_after
            assert captured.revision_after == admitted.revision_after + 1
            assert {row["entry_id"] for row in view.manifest["entries"]} >= set(
                admitted.affected_entry_ids
            )

            class Destination:
                def publish(self, view, manifest_bytes, read_content):
                    assert read_content(imported_id) == imported_bytes
                    print("Authorized snapshot member bytes:", read_content(imported_id))
                    return n.ExportReceipt(view.snapshot.snapshot_id, view.snapshot.cas_id)

            exported = n.read_namespace(
                handle,
                n.ReadRequest(
                    info.namespace_id,
                    info.cas,
                    None,
                    "export-snapshot",
                    {"snapshot_id": record.snapshot_id},
                ),
                destination=Destination(),
            ).value
            assert exported.cas_id == record.cas_id
            assert n.execute_namespace(handle, snapshot_request, {}) == captured
            n.execute_namespace(
                handle, mutation(handle, "delete-snapshot", {"snapshot_id": record.snapshot_id}), {}
            )
            assert cas.object_path(cas_root, record.cas_id).is_file()
            print("Snapshot captured revision:", record.captured_revision)
            print("Snapshot catalog removed; immutable bytes remain retained.")
            print("Committed revision:", handle.info().revision)
        if options.workspace is not None:
            with n.open_namespace(namespace_root, cas_root, session) as handle:
                current = handle.info()
                interactive_bytes = b"New interactive replacement, not previously published.\n"
                interactive_content = {
                    "cas_id": "sha256:" + hashlib.sha256(interactive_bytes).hexdigest(),
                    "size": len(interactive_bytes),
                }
                snapshot_id = identity()
                inputs = {
                    "rename": replace(
                        mutation(
                            handle,
                            "relocate",
                            {"entry_id": folder, "parent_id": target, "name": "Interactive"},
                        ),
                        expected_revision=current.revision,
                    ),
                    "move": replace(
                        mutation(
                            handle,
                            "relocate",
                            {
                                "entry_id": folder,
                                "parent_id": current.root_id,
                                "name": "Interactive",
                            },
                        ),
                        expected_revision=current.revision + 1,
                    ),
                    "replace": replace(
                        mutation(
                            handle,
                            "replace-content",
                            {"entry_id": file_id, "content": interactive_content},
                        ),
                        expected_revision=current.revision + 2,
                    ),
                    "snapshot": replace(
                        mutation(handle, "snapshot", {"snapshot_id": snapshot_id}),
                        expected_revision=current.revision + 3,
                    ),
                    "inspect": n.ReadRequest(
                        current.namespace_id,
                        current.cas,
                        None,
                        "inspect-snapshot",
                        {"snapshot_id": snapshot_id},
                    ),
                    "export": n.ReadRequest(
                        current.namespace_id,
                        current.cas,
                        None,
                        "export-snapshot",
                        {"snapshot_id": snapshot_id},
                    ),
                    "collision": replace(
                        mutation(
                            handle,
                            "create-directory",
                            {
                                "entry_id": identity(),
                                "parent_id": current.root_id,
                                "name": "destination",
                                "metadata": {},
                            },
                        ),
                        expected_revision=current.revision + 4,
                    ),
                    "stale": replace(
                        mutation(
                            handle,
                            "create-directory",
                            {
                                "entry_id": identity(),
                                "parent_id": current.root_id,
                                "name": "Stale",
                                "metadata": {},
                            },
                        ),
                        expected_revision=current.revision + 3,
                    ),
                }
                for name, request in inputs.items():
                    (root / (name + ".json")).write_text(
                        json.dumps(asdict(request), indent=2) + "\n"
                    )
                (root / "new-bytes.bin").write_bytes(interactive_bytes)
                base = [
                    sys.executable,
                    "-m",
                    "castle.cli",
                    "namespace",
                    "--root",
                    str(cas_root),
                    "--namespace-root",
                    str(namespace_root),
                    "--domain-lock",
                    str(root / "domain.lock"),
                ]
                instructions = [
                    (
                        "Synthetic workspace retained. Run the following commands in order. "
                        "Mutation files predict revisions for rename, move, new-byte "
                        "replacement and snapshot. The final collision and stale requests "
                        "deliberately refuse without advancing revision. If you change the "
                        "sequence, inspect info and edit expected_revision for each NEW "
                        "intent; preserve the exact saved token/revision for retries."
                    ),
                    shlex.join([*base, "info"]),
                ]
                for verb, name in [
                    ("rename", "rename"),
                    ("move", "move"),
                    ("replace-content", "replace"),
                    ("snapshot", "snapshot"),
                    ("inspect-snapshot", "inspect"),
                    ("export-snapshot", "export"),
                    ("create-directory", "collision"),
                    ("create-directory", "stale"),
                ]:
                    command = [*base, verb, "--request-file", str(root / (name + ".json"))]
                    if name == "replace":
                        command += [
                            "--content",
                            interactive_content["cas_id"] + "=" + str(root / "new-bytes.bin"),
                        ]
                    if name == "export":
                        command += ["--out", str(root / "export")]
                    instructions.append(shlex.join(command))
                instructions.extend(
                    [
                        shlex.join([*base, "status", inputs["replace"].operation_id]),
                        shlex.join(
                            [*base, "replace-content", "--request-file", str(root / "replace.json")]
                        ),
                        (
                            "Observe stable entry IDs, actual replacement bytes, empty directories "
                            "under export/tree/, and the separate control export/snapshot.json. "
                            "The final retry omits the input source and returns the original "
                            "replacement result. Try another sibling name with a fresh token and "
                            "current revision. Manual naming/error/retry-window judgment remains "
                            "the operator's."
                        ),
                    ]
                )
                (root / "COMMANDS.txt").write_text("\n\n".join(instructions) + "\n")
                print("Interactive workspace commands:", root / "COMMANDS.txt")
        excision_workflow(root / "cutover-example")
        print(
            "Only disposable unprotected roots were used; physical erasure, protected "
            "deployment and hardware power-loss qualification remain unproved."
        )


if __name__ == "__main__":
    main()
