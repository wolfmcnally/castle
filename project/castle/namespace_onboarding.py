"""Explicit seed observations and admission; no inferred current authority."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from castle import cas, journal, store_state
from castle import namespace as n
from castle import namespace_store as store


@dataclass(frozen=True)
class SeedInventory:
    provenance: str
    cas_binding: n.StoreIdentity
    observations: list[dict[str, str]]
    ambiguous_paths: list[dict[str, Any]]
    cross_source_paths: list[dict[str, Any]]
    normalized_collisions: list[dict[str, Any]]
    unsafe_paths: list[dict[str, str]]
    directory_membership: str


def inspect_seed(cas_root: Path, session: n.AuthorizedSession) -> SeedInventory:
    n._authorize(session, "inspect-seed")
    try:
        root = Path(cas_root).absolute()
        if root.is_symlink():
            n._refuse("namespace-root-unsafe")
        root = root.resolve(strict=True)
        descriptor = session.describe_capabilities()
        if type(descriptor) is not n.CapabilityDescriptor:
            n._refuse("namespace-protection-unsupported")
        if not isinstance(descriptor.namespace_root, Path):
            n._refuse("namespace-protection-unsupported")
        store._descriptor(session, descriptor.namespace_root.resolve(), root)
        with session.acquire_domain_ownership(), journal._stable_lock(root):
            n._authorize(session, "inspect-seed")
            if session.describe_capabilities() != descriptor:
                n._refuse("namespace-protection-unsupported")
            try:
                binding = store_state.read_store_identity(root)
            except store_state.StoreStateError as error:
                if error.code in {"store-retired", "store-not-active", "store-epoch-mismatch"}:
                    n._refuse("namespace-binding-mismatch")
                n._refuse("namespace-storage-recovery-required")
            records, _, _ = cas._validated_authority(root, derived=True)
            observations = []
            paths = defaultdict(set)
            cross = defaultdict(set)
            normalized = defaultdict(set)
            unsafe = []
            for event in records:
                item = {key: event[key] for key in ("source_root_id", "original_relpath", "cas_id")}
                # Reserved opaque namespace provenance is an observation about
                # namespace-owned bytes, never an original-upload association.
                item["provenance"] = (
                    "namespace-owned"
                    if item["source_root_id"].startswith("namespace:")
                    else "reconstructed-observation"
                )
                observations.append(item)
                if item["provenance"] == "namespace-owned":
                    continue
                source, name = item["source_root_id"], item["original_relpath"]
                paths[(source, name)].add(item["cas_id"])
                cross[name].add(source)
                try:
                    components = name.split("/")
                    if not components:
                        n._refuse()
                    key = tuple(n._key(n._name(part)) for part in components)
                    normalized[(source, key)].add(name)
                except n.NamespaceError:
                    unsafe.append({"source_root_id": source, "original_relpath": name})
            result = SeedInventory(
                "reconstructed-observations",
                binding,
                observations,
                [
                    {"source_root_id": source, "original_relpath": name, "cas_ids": sorted(ids)}
                    for (source, name), ids in sorted(paths.items())
                    if len(ids) > 1
                ],
                [
                    {"original_relpath": name, "source_root_ids": sorted(roots)}
                    for name, roots in sorted(cross.items())
                    if len(roots) > 1
                ],
                [
                    {"source_root_id": source, "paths": sorted(names)}
                    for (source, _), names in sorted(normalized.items())
                    if len(names) > 1
                ],
                unsafe,
                "unknown",
            )
            n._authorize(session, "inspect-seed-result")
            return result
    except n.NamespaceError:
        # Preserve the caller's fixed access/session/busy refusal; backend
        # failures below instead become the fixed storage-recovery refusal.
        raise
    except (OSError, ValueError, TypeError, KeyError, journal.CastleError):
        n._refuse("namespace-storage-recovery-required")


def _control(handle: n.NamespaceHandle, identity: str) -> bool:
    size = handle.objects[identity][1]
    if size > n.MAX_MANIFEST_BYTES:
        return False
    raw = cas.object_path(handle.cas_root, identity).read_bytes()
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        return False
    return isinstance(value, dict) and value.get("schema") == "castle.namespace_snapshot.v1"


def admit_seed(handle: n.NamespaceHandle, request: n.MutationRequest) -> n.MutationResult:
    from castle import namespace_transactions as tx

    with handle.admitted():
        n._authorize(handle.session, "admit-seed")
        value = tx.normalize(request)
        if value["kind"] != "import-folder":
            n._refuse()
        frozen = replace(request, args=value["args"])
        row = tx._row(handle, value["operation_id"])
        if row is None:
            if value["expected_revision"] != 0 or handle.load_info().revision != 0:
                n._refuse()
            for item in value["args"]["entries"]:
                if item["cas_id"] is not None:
                    tx._content(handle, item["cas_id"], item["size"])
                    if _control(handle, item["cas_id"]):
                        n._refuse()
    # Public execute rechecks revision0 under admission. A concurrent winner
    # therefore rejects this seed atomically; no SQL seeding or nested CAS lock.
    return n.execute_namespace(handle, frozen, {})
