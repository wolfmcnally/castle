"""Disposable new-content namespace workflow, using public APIs only.

Run from the checkout with its Python wrapper. All created stores are synthetic
and temporary. LocalSession is a trusted caller fixture, not authentication or
an encryption backend. No active namespace shares its CAS with another writer.
"""

from __future__ import annotations

import argparse
import hashlib
import fcntl
import os
import tempfile
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from castle import cas, store_state
from castle import namespace as n


def identity() -> str:
    return str(uuid.uuid4())


class LocalSession:
    """Explicit caller custody for these disposable local fixtures only."""

    def __init__(self, namespace_root: Path, cas_root: Path, domain_lock: Path) -> None:
        self.namespace_root = namespace_root.resolve()
        self.cas_root = cas_root.resolve()
        self.domain_lock = domain_lock.resolve()
        self.active = True
        self.descriptors: list[int] = []
        os.register_at_fork(after_in_child=self._child)

    def _child(self) -> None:
        for descriptor in self.descriptors:
            os.close(descriptor)
        self.descriptors.clear()
        self.active = False

    def check_authorized(self, operation_kind: str) -> None:
        if not self.active:
            raise n.NamespaceError("namespace-session-expired")

    def describe_capabilities(self) -> n.CapabilityDescriptor:
        return n.CapabilityDescriptor(
            None,
            "disposable-local-domain",
            self.namespace_root,
            self.cas_root,
            (
                "locking",
                "path-safety",
                "immutable-staging",
                "durable-publication",
                "sqlite-extra",
                "restart-recovery",
                "revocation",
                "authorized-export",
            ),
        )

    @contextmanager
    def acquire_domain_ownership(self) -> Iterator[None]:
        descriptor = os.open(
            self.domain_lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise n.NamespaceError("namespace-coordinator-busy") from None
            self.descriptors.append(descriptor)
            os.fsync(descriptor)
            store_state._fsync_directory(self.domain_lock.parent)
            yield
        finally:
            if descriptor in self.descriptors:
                self.descriptors.remove(descriptor)
                os.close(descriptor)


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation-id", help="UUIDv4 token for the duplicate and its retry")
    options = parser.parse_args()
    token = options.operation_id or identity()
    n._uuid(token)
    with tempfile.TemporaryDirectory(prefix="castle-namespace-demo-") as directory:
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
        session = LocalSession(namespace_root, cas_root, root / "domain.lock")
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
        print(
            (
                "Only disposable unprotected roots were used; epoch excision and protected "
                "deployment remain separate qualification."
            )
        )


if __name__ == "__main__":
    main()
