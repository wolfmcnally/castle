"""Single-owner namespace cutover, private dependency closure and recovery."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from collections.abc import Iterator
from dataclasses import asdict, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from castle import cas, excision, journal, store_state
from castle import namespace as n

if TYPE_CHECKING:
    from castle.namespace_store import NamespaceHandle

_SELECTION = {"content_ids", "entry_ids", "operation_ids", "metadata_entry_ids"}


def _identities(value: object, *, content: bool = False) -> list[str]:
    if not isinstance(value, list):
        n._refuse()
    if len(value) > n.MAX_ENTRIES:
        n._refuse("namespace-limit-exceeded")
    for identity in value:
        if content:
            if not isinstance(identity, str) or n._CAS.fullmatch(identity) is None:
                n._refuse()
        else:
            n._uuid(identity)
    if len(set(value)) != len(value):
        n._refuse()
    return sorted(value)


def normalize(args: dict[str, Any]) -> dict[str, Any]:
    """Close and normalize the specified request before its fingerprint."""
    if not isinstance(args, dict) or set(args) != {"selection", "snapshot_actions"}:
        n._refuse()
    selection = args["selection"]
    if not isinstance(selection, dict) or set(selection) != _SELECTION:
        n._refuse()
    selected = {
        key: _identities(value, content=key == "content_ids") for key, value in selection.items()
    }
    if not any(selected.values()):
        n._refuse()
    actions = args["snapshot_actions"]
    if not isinstance(actions, list):
        n._refuse()
    if len(actions) > n.MAX_ENTRIES:
        n._refuse("namespace-limit-exceeded")
    result = []
    used = set()
    for action in actions:
        if not isinstance(action, dict):
            n._refuse()
        identity = action.get("snapshot_id")
        n._uuid(identity)
        if identity in used:
            n._refuse()
        used.add(identity)
        if action.get("action") == "drop":
            if set(action) != {"snapshot_id", "action"}:
                n._refuse()
            result.append({"snapshot_id": identity, "action": "drop"})
        elif action.get("action") == "replace":
            if set(action) != {
                "snapshot_id",
                "action",
                "remove_entry_ids",
                "clear_metadata_entry_ids",
            }:
                n._refuse()
            result.append(
                {
                    "snapshot_id": identity,
                    "action": "replace",
                    "remove_entry_ids": _identities(action["remove_entry_ids"]),
                    "clear_metadata_entry_ids": _identities(action["clear_metadata_entry_ids"]),
                }
            )
        else:
            n._refuse()
    return {
        "selection": selected,
        "snapshot_actions": sorted(result, key=lambda item: item["snapshot_id"]),
    }


def roots(namespace_root: Path, predecessor_root: Path, value: object) -> tuple[Path, Path]:
    if (
        not isinstance(value, dict)
        or set(value) != {"predecessor_root", "successor_root"}
        or any(not isinstance(item, Path) for item in value.values())
    ):
        n._refuse()
    try:
        predecessor, successor = excision._roots(value["predecessor_root"], value["successor_root"])
        if predecessor != predecessor_root:
            n._refuse("namespace-binding-mismatch")
        if (
            namespace_root == successor
            or namespace_root in successor.parents
            or successor in namespace_root.parents
        ):
            n._refuse("namespace-root-unsafe")
        return predecessor, successor
    except excision.ExcisionError:
        n._refuse("namespace-root-unsafe")


def root_commitment(root: Path) -> str:
    return hashlib.sha256(n.canonical_bytes(str(root))).hexdigest()


def entry_closure(
    entries: dict[str, n.EntryRecord], identities: list[str], root_id: str
) -> set[str]:
    """Expand retained directories, including their deleted descendants."""
    if root_id in identities:
        n._refuse("namespace-root-immutable")
    if set(identities) - set(entries):
        n._refuse("namespace-entry-missing")
    children: dict[str, list[str]] = {}
    for entry in entries.values():
        if entry.parent_id is not None:
            children.setdefault(entry.parent_id, []).append(entry.entry_id)
    selected = set()
    pending = list(identities)
    while pending:
        identity = pending.pop()
        if identity in selected:
            continue
        selected.add(identity)
        if entries[identity].kind == "directory":
            pending.extend(children.get(identity, []))
        if len(selected) > n.MAX_ENTRIES:
            n._refuse("namespace-limit-exceeded")
    return selected


def snapshot_recipes(
    handle: NamespaceHandle,
    request: dict[str, Any],
    removed: set[str],
    cleared: set[str],
) -> tuple[list[tuple[n.SnapshotRecord, int, bytes]], list[str], int]:
    """Freeze sanitized bytes before retirement, without reading them later.

    Each bytes value becomes its own owned recipe file, not a concatenated SQL
    cell. The caller includes the returned owner-pair count in the whole effect
    bound before reserving any intent or publishing any file.
    """
    from castle import namespace_snapshots as snapshots
    from castle.namespace_store import _tree

    info = handle.load_info()
    actions = {item["snapshot_id"]: item for item in request["args"]["snapshot_actions"]}
    rows = handle.db.execute(
        "SELECT snapshot_id,cas_id,size,captured_revision,live FROM snapshots ORDER BY snapshot_id"
    ).fetchall()
    if set(actions) != {row[0] for row in rows}:
        n._refuse("namespace-dependency-conflict")
    selected_content = set(request["args"]["selection"]["content_ids"])
    recipes, dropped, pairs = [], [], 0
    for row in rows:
        record = n.SnapshotRecord(*row[:4])
        view = snapshots.decode(handle, record)
        entries = {item["entry_id"]: n.EntryRecord(**item) for item in view["entries"]}
        disclosing = any(
            entry.cas_id in selected_content
            or entry.entry_id in removed
            or (entry.entry_id in cleared and bool(entry.metadata))
            for entry in entries.values()
        )
        if disclosing and record.cas_id not in selected_content:
            n._refuse("namespace-dependency-conflict")
        action = actions[record.snapshot_id]
        if action["action"] == "drop":
            dropped.append(record.snapshot_id)
            continue
        removals = entry_closure(entries, action["remove_entry_ids"], info.root_id)
        metadata = set(action["clear_metadata_entry_ids"])
        if info.root_id in metadata:
            n._refuse("namespace-root-immutable")
        if metadata - set(entries):
            n._refuse("namespace-entry-missing")
        retained = {
            identity: replace(entry, metadata={}) if identity in metadata else entry
            for identity, entry in entries.items()
            if identity not in removals
        }
        if any(
            entry.cas_id in selected_content
            or entry.entry_id in removed
            or (entry.entry_id in cleared and bool(entry.metadata))
            for entry in retained.values()
        ):
            n._refuse("namespace-dependency-conflict")
        _tree(replace(info, revision=record.captured_revision), retained)
        value = dict(
            view,
            origin={"kind": "redacted", "operation_id": request["operation_id"]},
            entries=[n._plain(retained[identity]) for identity in sorted(retained)],
        )
        raw = n.canonical_bytes(value, limit=n.MAX_MANIFEST_BYTES)
        # Successor UUIDs have the same encoded width. Generation and nullable
        # predecessor_epoch_id are the only variable-width binding fields.
        # This object is capacity evidence only and is never published.
        future = dict(
            value,
            cas_binding={
                **view["cas_binding"],
                "generation": info.cas.generation + 1,
                "predecessor_epoch_id": info.cas.epoch_id,
            },
        )
        n.canonical_bytes(future, limit=n.MAX_MANIFEST_BYTES)
        pairs += len(entries)
        if pairs > n.MAX_ENTRIES:
            n._refuse("namespace-limit-exceeded")
        recipes.append((record, row[4], raw))
    return recipes, dropped, pairs


def _strings(value: object, *, scalar_values: bool = False) -> Iterator[str]:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif scalar_values:
            yield n.canonical_bytes(item).decode().removesuffix("\n")


def operation_disclosures(
    rows: list[dict[str, Any]],
    removed: set[str],
    cleared: set[str],
    content_ids: set[str],
    dropped_snapshots: set[str],
    selected_operations: set[str],
    dependencies: set[tuple[str, str, str, str]],
) -> set[str]:
    """Classify payload owners, not bare UUID sightings or equal other names.

    Exact CasID literals and confirmation digests can be innocent references;
    callers must resolve the resulting dependency refusal explicitly. This
    function never widens a selection or deletes a matched owner itself.
    """
    from castle import namespace_content as content

    disclosing = set(selected_operations)
    disclosing.update(
        owner
        for kind, owner, cas_id, _ in dependencies
        if kind == "operation" and cas_id in content_ids
    )
    payloads = {}
    requests = {}
    recipes = {}
    digests = {}
    unavailable = set()
    for row in rows:
        identity = row["operation_id"]
        if row["canonical_request"] is None:
            unavailable.add(identity)
            continue
        request = n._decode(row["canonical_request"])
        recipe = content.decode_recipe(row["ownership_receipt"])
        result = n._decode(row["terminal_result"]) if row["terminal_result"] else None
        error = n._decode(row["terminal_error"]) if row["terminal_error"] else None
        payload = [request, recipe, result, error]
        payloads[identity], requests[identity], recipes[identity] = payload, request, recipe
        digests[identity] = {
            row["fingerprint"],
            hashlib.sha256(row["canonical_request"].encode()).hexdigest(),
        }
        if any(
            match.group() in content_ids
            for text in _strings(payload)
            for match in n._CAS.finditer(text)
        ):
            disclosing.add(identity)
        kind, args = request["kind"], request["args"]
        # These are schema-owned relationships, not UUID-shaped strings in
        # arbitrary content. Logical Delete logs also disclose a removed
        # entry's identity/history, so excision must retire their payloads.
        references = {args[key] for key in ("entry_id", "parent_id") if key in args}
        if kind == "duplicate":
            references.update(identity for pair in args["identities"] for identity in pair.values())
        elif kind == "import-folder":
            references.update(
                identity
                for entry in args["entries"]
                for identity in (entry["entry_id"], entry["parent_id"])
            )
        if references & removed or (
            result is not None and set(result["affected_entry_ids"]) & removed
        ):
            disclosing.add(identity)
        fields = []
        if kind in {"create-file", "create-directory", "replace-content", "relocate"}:
            fields.append((args["entry_id"], set(args) & {"name", "metadata", "content"}))
        elif kind == "set-metadata":
            fields.append((args["entry_id"], {"metadata"}))
        elif kind == "import-folder":
            fields.extend(
                (item["entry_id"], {"name", "metadata", "content"}) for item in args["entries"]
            )
        elif kind == "duplicate":
            destination = next(
                (
                    item["new_id"]
                    for item in args["identities"]
                    if item["source_id"] == args["entry_id"]
                ),
                None,
            )
            if destination is None:
                n._refuse("namespace-storage-recovery-required")
            fields.append((destination, {"name"}))
        if any(
            (owner in removed and bool(names)) or (owner in cleared and "metadata" in names)
            for owner, names in fields
        ):
            disclosing.add(identity)
        for snapshot in (result.get("snapshot") if result else None, recipe.get("snapshot")):
            if snapshot and snapshot["snapshot_id"] in dropped_snapshots:
                disclosing.add(identity)

    # Expiry witnesses can confirm an earlier request even after its plaintext
    # is gone. Unknown original ownership is not a proof of non-disclosure.
    for identity, recipe in recipes.items():
        if set(recipe.get("expiry", {})) & unavailable:
            disclosing.add(identity)
    while True:
        before = set(disclosing)
        confirmation = set().union(*(digests.get(identity, set()) for identity in disclosing))
        confirmation.discard(None)
        for identity, payload in payloads.items():
            if set(recipes[identity].get("expiry", {})) & disclosing or any(
                text in confirmation for text in _strings(payload)
            ):
                disclosing.add(identity)
        if before == disclosing:
            return disclosing


def _source_conflicts(
    handle: NamespaceHandle, request: dict[str, Any], removed: set[str], cleared: set[str]
) -> set[str]:
    """Identify conservatively disclosing retained content, never select it.

    Equal journal components can be innocent; the outcome is explicit dependency
    refusal, never implicit selection. Names in unrelated namespace owner fields
    are separately permitted by the semantic owner predicate.
    """
    from castle import namespace_snapshots as snapshots

    names, metadata = set(), set()
    live_ids = set(handle.entries())

    def include(entry: n.EntryRecord, remove: bool, clear: bool) -> None:
        if remove and entry.name:
            names.add(entry.name)
        if remove or clear:
            metadata.update(text for text in _strings(entry.metadata, scalar_values=True) if text)

    for entry in handle.entries(live=False).values():
        include(entry, entry.entry_id in removed, entry.entry_id in cleared)
    for action in request["args"]["snapshot_actions"]:
        record = snapshots.catalog(handle, action["snapshot_id"], live=False)
        value = snapshots.catalog_view(handle, record)
        entries = {item["entry_id"]: n.EntryRecord(**item) for item in value["entries"]}
        extra_removed = (
            entry_closure(entries, action["remove_entry_ids"], value["root_id"])
            if action["action"] == "replace"
            else set()
        )
        extra_cleared = set(action.get("clear_metadata_entry_ids", []))
        for entry in entries.values():
            include(
                entry,
                entry.entry_id in removed
                or (entry.entry_id in extra_removed and entry.entry_id not in live_ids),
                entry.entry_id in cleared | extra_cleared,
            )
    forbidden = names | metadata
    selected = set(request["args"]["selection"]["content_ids"])

    def matches(text: str | None) -> bool:
        return isinstance(text, str) and (
            text in forbidden
            or bool(set(text.split("/")) & forbidden)
            or any(match.group() in selected for match in n._CAS.finditer(text))
        )

    conflicts = set()
    for event in journal._read_authority_journal(handle.cas_root):
        if event["cas_id"] not in selected and any(
            matches(event[key]) for key in ("source_root_id", "original_relpath", "batch")
        ):
            conflicts.add(event["cas_id"])
    for row in cas._snapshot_remotes(handle.cas_root):
        if row[0] not in selected and any(matches(value) for value in row[1:]):
            conflicts.add(row[0])
    # Unique literal CasID references are checked before retirement as well as
    # at cleanup. The scanner does not classify arbitrary names in documents.
    for identity in (set(handle.objects) - selected) if selected else ():
        n._authorize(handle.session, "excision-inventory")
        file = cas.object_path(handle.cas_root, identity)
        carry = ""
        with file.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                n._authorize(handle.session, "excision-inventory")
                text = carry + chunk.decode("latin1")
                if any(match.group() in selected for match in n._CAS.finditer(text)):
                    conflicts.add(identity)
                    break
                carry = text[-70:]
    return conflicts


def _selection_plan(
    handle: NamespaceHandle, request: dict[str, Any]
) -> tuple[dict[str, Any], list[tuple[n.SnapshotRecord, int, bytes]], set[str]]:
    """Compute one closure for strict execution and authorized proposals."""
    from castle import namespace_transactions as transactions

    n._authorize(handle.session, "excision-inventory")
    handle._cas_check()
    info = handle.load_info()
    selection = request["args"]["selection"]
    selected_content = set(selection["content_ids"])
    if selected_content - set(handle.objects):
        n._refuse("namespace-content-mismatch")
    entries = handle.entries(live=False)
    removed = entry_closure(entries, selection["entry_ids"], info.root_id)
    cleared = set(selection["metadata_entry_ids"])
    if info.root_id in cleared:
        n._refuse("namespace-root-immutable")
    if cleared - set(entries):
        n._refuse("namespace-entry-missing")
    if any(
        (entry.cas_id in selected_content and entry.entry_id not in removed)
        or (entry.parent_id in removed and entry.entry_id not in removed)
        for entry in entries.values()
    ):
        n._refuse("namespace-dependency-conflict")
    rows = [
        transactions._row(handle, row[0])
        for row in handle.db.execute("SELECT operation_id FROM operations").fetchall()
    ]
    selected_operations = set(selection["operation_ids"])
    if selected_operations - {row["operation_id"] for row in rows} - {request["operation_id"]}:
        n._refuse()
    recipes, dropped, pairs = snapshot_recipes(handle, request, removed, cleared)
    disclosed = operation_disclosures(
        rows,
        removed,
        cleared,
        selected_content,
        set(dropped),
        selected_operations,
        set(handle.db.execute("SELECT * FROM dependencies")),
    )
    selected_operations |= disclosed - {request["operation_id"]}
    count = len(removed | cleared) + len(selected_operations) + len(selected_content) + pairs
    if count > n.MAX_ENTRIES:
        n._refuse("namespace-limit-exceeded")
    # Cleanup scans the retained SQL image and successor manifests as well as
    # CAS objects. Refuse their selected literals before irreversible retirement;
    # retiring a disclosing operation does not clear the entry it once wrote.
    if selected_content:

        def retained_texts() -> Iterator[str]:
            for identity, entry in entries.items():
                if identity not in removed:
                    yield entry.name or ""
                    if entry.name is not None:
                        yield n._key(entry.name)
                    if identity not in cleared:
                        yield from _strings(entry.metadata)
            for _, _, raw in recipes:
                yield raw.decode("utf-8")

        if any(
            match.group() in selected_content
            for text in retained_texts()
            for match in n._CAS.finditer(text)
        ):
            n._refuse("namespace-dependency-conflict")
    conflicts = _source_conflicts(handle, request, removed, cleared)
    n._authorize(handle.session, "excision-inventory-result")
    return (
        {
            "entry_ids": sorted(removed),
            "metadata_entry_ids": sorted(cleared - removed),
            "operation_ids": sorted(selected_operations),
            "drop_snapshot_ids": dropped,
        },
        recipes,
        conflicts,
    )


def selection_plan(
    handle: NamespaceHandle, request: dict[str, Any]
) -> tuple[dict[str, Any], list[tuple[n.SnapshotRecord, int, bytes]]]:
    """Validate complete explicit closure before any durable cutover intent."""
    closure, recipes, conflicts = _selection_plan(handle, request)
    if conflicts or set(closure["operation_ids"]) - set(
        request["args"]["selection"]["operation_ids"]
    ) - {request["operation_id"]}:
        n._refuse("namespace-dependency-conflict")
    return closure, recipes


def operation_available(handle: NamespaceHandle, identity: str) -> None:
    """Apply the same token/reservation refusal to preview and fresh execution."""
    from castle import namespace_transactions as transactions

    row = transactions._row(handle, identity)
    if row is not None:
        n._refuse(
            "namespace-operation-retired"
            if row["state"] == "retired"
            else "namespace-operation-conflict"
        )
    if (
        identity in handle.load_info().cas.to_dict().values()
        or identity in handle.authority_ids()
        or handle.db.execute(
            "SELECT 1 FROM entry_reservations WHERE entry_id=? UNION ALL "
            "SELECT 1 FROM snapshot_reservations WHERE snapshot_id=?",
            (identity, identity),
        ).fetchone()
    ):
        n._refuse()


def inventory(handle: NamespaceHandle) -> n.ExcisionInventory:
    """Return complete bounded payload ownership without raw operation bodies."""
    from castle import namespace_snapshots as snapshots
    from castle import namespace_store as store

    n._authorize(handle.session, "excision-inventory")
    handle._cas_check()
    info = handle.load_info()
    total_bytes = 0

    def append(collection: list, item: Any) -> None:
        nonlocal total_bytes
        if len(collection) >= n.MAX_ENTRIES:
            n._refuse("namespace-limit-exceeded")
        # Member encodings are a lower bound on the enclosing object, so this
        # rejects only certain overflow while keeping collection memory bounded.
        total_bytes += len(n.canonical_bytes(n._plain(item), limit=n.MAX_MANIFEST_BYTES))
        if total_bytes > n.MAX_MANIFEST_BYTES:
            n._refuse("namespace-limit-exceeded")
        collection.append(item)

    entries = []
    for row in handle.db.execute("SELECT * FROM entries ORDER BY entry_id"):
        identity, kind, parent, name, metadata, cas_id, size, changed, live, key = row
        entry = n.EntryRecord(
            identity, kind, parent, name, n._decode(metadata), cas_id, size, changed
        )
        store._validate_entry(entry, info.revision)
        if live not in (0, 1) or key != (n._key(name) if name is not None else None):
            n._refuse("namespace-format-unsupported")
        append(entries, n.HistoricalEntry(entry, bool(live)))
    views, pairs = [], 0
    for row in handle.db.execute(
        "SELECT snapshot_id,cas_id,size,captured_revision,live FROM snapshots ORDER BY snapshot_id"
    ):
        if row[4] not in (0, 1):
            n._refuse("namespace-format-unsupported")
        record = n.SnapshotRecord(*row[:4])
        manifest = snapshots.catalog_view(handle, record)
        pairs += len(manifest["entries"])
        if pairs > n.MAX_ENTRIES:
            n._refuse("namespace-limit-exceeded")
        append(views, n.HistoricalSnapshot(n.SnapshotView(record, manifest), bool(row[4])))
    operations = []
    for identity, state in handle.db.execute(
        "SELECT operation_id,state FROM operations WHERE canonical_request IS NOT NULL "
        "ORDER BY operation_id"
    ):
        append(operations, n.HistoricalOperation(identity, state))
    dependencies = []
    for row in handle.db.execute("SELECT * FROM dependencies ORDER BY 1,2,3,4"):
        append(dependencies, n.ContentDependency(*row))
    content_ids = []
    for identity in sorted(handle.objects):
        append(content_ids, identity)
    result = n.ExcisionInventory(entries, views, operations, dependencies, content_ids)
    n.canonical_bytes(asdict(result), limit=n.MAX_MANIFEST_BYTES)
    n._authorize(handle.session, "excision-inventory-result")
    return result


def preview(
    handle: NamespaceHandle, info: n.NamespaceInfo, args: dict[str, Any]
) -> n.ExcisionPreview:
    """Propose explicit cleanup owners without reserving or changing authority."""
    from castle import namespace_transactions as transactions

    n._authorize(handle.session, "excision-inventory")
    request = n.MutationRequest(
        info.namespace_id,
        info.cas,
        info.revision,
        args["operation_id"],
        "excise",
        {"selection": args["selection"], "snapshot_actions": args["snapshot_actions"]},
    )
    value = transactions.normalize(request)
    operation_available(handle, value["operation_id"])
    if info.revision == n.MAX_INTEGER:
        n._refuse("namespace-revision-exhausted")
    closure, recipes, conflicts = _selection_plan(handle, value)
    proposed = None
    if not conflicts:
        selection = value["args"]["selection"]
        for field in ("entry_ids", "metadata_entry_ids", "operation_ids"):
            selection[field] = closure[field]
        proposed = replace(request, args=value["args"])
        strict, strict_recipes = selection_plan(handle, transactions.normalize(proposed))
        if strict != closure or strict_recipes != recipes:
            n._refuse("namespace-storage-recovery-required")
    n._authorize(handle.session, "excision-inventory-result")
    return n.ExcisionPreview(
        proposed, closure["entry_ids"], closure["metadata_entry_ids"], sorted(conflicts)
    )


_INTENT_KEYS = {
    "source_info",
    "page_size",
    "roots",
    "target_generation",
    "prior_marker",
    "closure",
    "recipes",
    "publications",
    "cas_receipt",
    "stage",
    "cleanup",
    "retained_authority",
    "image",
}
_INTENT_STAGES = {"staging", "prepared", "cas-complete", "publishing", "references-published"}


def _fresh(used: set[str]) -> str:
    while True:
        identity = str(uuid.uuid4())
        if identity not in used:
            used.add(identity)
            return identity


def _hash_records(records) -> str:
    result = hashlib.sha256()
    for record in records:
        result.update(record)
    return result.hexdigest()


def _retained_commitment(handle: NamespaceHandle, selected: set[str]) -> dict[str, str]:
    records = sorted(
        (
            event
            for event in journal._read_authority_journal(handle.cas_root)
            if event["cas_id"] not in selected
        ),
        key=journal.journal_sort_key,
    )
    remotes = sorted(
        row for row in cas._snapshot_remotes(handle.cas_root) if row[0] not in selected
    )
    objects = sorted(
        (key, value[1]) for key, value in handle.objects.items() if key not in selected
    )
    return {
        "journal": _hash_records(
            (journal.canonical_journal_line(event) + "\n").encode("utf-8") for event in records
        ),
        "remotes": _hash_records(store_state._canonical(list(row)) for row in remotes),
        "objects": _hash_records(store_state._canonical(list(row)) for row in objects),
    }


def _closed_file(file: Path) -> dict[str, Any]:
    from castle import namespace_replacement as replacement

    size = replacement.regular(file).st_size
    n._integer(size)
    return {"name": file.name, "size": size, "digest": replacement.digest(file)}


def _save_intent(
    handle: NamespaceHandle, operation: str, value: dict[str, Any], *, state: str | None = None
) -> None:
    from castle import namespace_content as content
    from castle import namespace_store as store

    n._authorize(handle.session, "excise-intent")
    row = handle.db.execute(
        "SELECT ownership_receipt FROM operations WHERE operation_id=?", (operation,)
    ).fetchone()
    if row is None:
        n._refuse("namespace-storage-recovery-required")
    outer = content.decode_recipe(row[0])
    if set(outer) != {"receipt", "cutover"}:
        n._refuse("namespace-storage-recovery-required")
    outer["cutover"] = value
    encoded = content.encode_recipe(outer)
    handle.db.execute("BEGIN IMMEDIATE")
    if state is None:
        handle.db.execute(
            "UPDATE operations SET ownership_receipt=? WHERE operation_id=?", (encoded, operation)
        )
    else:
        if state not in {"staging", "prepared"}:
            n._refuse("namespace-storage-recovery-required")
        handle.db.execute(
            "UPDATE operations SET ownership_receipt=?,state=? WHERE operation_id=?",
            (encoded, state, operation),
        )
    store._commit(handle.db)


def _old_intent(handle: NamespaceHandle) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the source generation without letting SQLite touch its journals."""
    from castle import namespace_replacement as replacement

    marker = replacement.read(handle.root)
    old = handle.root / replacement.filenames(marker)[1]
    return _source_file(handle, old, marker)


def _source_file(
    handle: NamespaceHandle, old: Path, marker: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    from castle import namespace_replacement as replacement
    from castle import namespace_store as store

    n._authorize(handle.session, "excise-source-control")
    replacement.regular(old)
    if any((old.parent / (old.name + suffix)).exists() for suffix in ("-journal", "-wal", "-shm")):
        replacement.invalid()
    original = handle.connection
    connection = sqlite3.connect(
        old.as_uri() + "?mode=ro&immutable=1", uri=True, isolation_level=None
    )
    try:
        handle.connection = connection
        info = handle.load_info()
        if (
            info.namespace_id != marker["namespace_id"]
            or info.database_generation != marker["source_generation"]
            or info.cas.to_dict() != marker["successor_cas"]
        ):
            replacement.invalid()
        request, intent = _intent(handle, marker["operation_id"])
        source = n._info(intent["source_info"])
        if (
            intent["stage"] != "references-published"
            or info.revision != source.revision + 1
            or intent["target_generation"] != marker["target_generation"]
            or source.cas.to_dict() != marker["predecessor_cas"]
            or intent["cas_receipt"]["successor"] != marker["successor_cas"]
        ):
            replacement.invalid()
        if (
            connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
            != store._TABLES
        ):
            replacement.invalid()
        return request, intent
    finally:
        handle.connection = original
        connection.close()


def _scan(handle: NamespaceHandle, request: dict[str, Any], value: dict[str, Any]) -> None:
    """Check retained authority independently before discarding private witnesses."""
    from castle import namespace_replacement as replacement
    from castle import namespace_snapshots as snapshots

    n._authorize(handle.session, "excise-scan")
    selected = set(request["args"]["selection"]["content_ids"])
    closure = value["closure"]
    if selected & set(handle.objects) or set(closure["entry_ids"]) & set(
        handle.entries(live=False)
    ):
        replacement.invalid()
    marker = replacement.read(handle.root)
    old = handle.root / replacement.filenames(marker)[1]
    original = handle.connection
    authority = sqlite3.connect(
        old.as_uri() + "?mode=ro&immutable=1", uri=True, isolation_level=None
    )
    try:
        handle.connection = authority
        expected_rows = _projection(handle, request, value)
    finally:
        handle.connection = original
        authority.close()
    for table, expected in expected_rows.items():
        if table != "dependencies" and handle.db.execute(
            f"SELECT * FROM {table} ORDER BY 1"
        ).fetchall() != sorted(expected):
            replacement.invalid()
    for identity in closure["metadata_entry_ids"]:
        if handle.entries(live=False)[identity].metadata:
            replacement.invalid()
    for operation in {*closure["operation_ids"], request["operation_id"]}:
        if handle.db.execute(
            "SELECT state FROM operations WHERE operation_id=?", (operation,)
        ).fetchone() != ("retired",):
            replacement.invalid()
    if set(closure["drop_snapshot_ids"]) & {
        row[0] for row in handle.db.execute("SELECT snapshot_id FROM snapshots")
    }:
        replacement.invalid()
    # Exact publication receipts define the only permitted additions to the
    # retained CAS. This is independent of the builder's reference projection.
    events = {event for saved in value["publications"] for event in saved["receipt"]["event_ids"]}
    additions = {saved["request"]["cas_id"] for saved in value["publications"]}
    records = sorted(
        (
            event
            for event in journal._read_authority_journal(handle.cas_root)
            if event["event_id"] not in events
        ),
        key=journal.journal_sort_key,
    )
    remotes = sorted(cas._snapshot_remotes(handle.cas_root))
    objects = sorted((key, row[1]) for key, row in handle.objects.items() if key not in additions)
    observed = {
        "journal": _hash_records(
            (journal.canonical_journal_line(event) + "\n").encode("utf-8") for event in records
        ),
        "remotes": _hash_records(store_state._canonical(list(row)) for row in remotes),
        "objects": _hash_records(store_state._canonical(list(row)) for row in objects),
    }
    if observed != value["retained_authority"]:
        replacement.invalid()
    for row in handle.db.execute(
        "SELECT snapshot_id,cas_id,size,captured_revision,live FROM snapshots"
    ):
        record = n.SnapshotRecord(*row[:4])
        manifest = snapshots.catalog_view(handle, record, allow_pending=True)
        if any(
            entry["cas_id"] in selected
            or entry["entry_id"] in closure["entry_ids"]
            or (entry["entry_id"] in closure["metadata_entry_ids"] and entry["metadata"])
            for entry in manifest["entries"]
        ):
            replacement.invalid()
    # Projection equality above is a consistency check. Owner/closure checks,
    # retained CAS commitments and literal scans are independent checks.
    # CasID literals can be innocent references. Preflight conservatively
    # refuses them; cleanup never guesses additional deletion authority.
    for file in [
        handle.root / "namespace.sqlite",
        *[cas.object_path(handle.cas_root, identity) for identity in handle.objects],
        *[
            file
            for directory in (handle.root / "operations").iterdir()
            if directory.name not in {*closure["operation_ids"], request["operation_id"]}
            for file in directory.iterdir()
        ],
    ]:
        replacement.regular(file)
        carry = ""
        with file.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                n._authorize(handle.session, "excise-scan")
                text = carry + chunk.decode("latin1")
                if any(match.group() in selected for match in n._CAS.finditer(text)):
                    replacement.invalid()
                carry = text[-70:]
    handle._validate(replacement_pending=True)


def _remove_directory(directory: Path, commitments: list[dict[str, Any]]) -> None:
    from castle import namespace_replacement as replacement
    from castle import namespace_store as store

    if not directory.exists():
        store._sync_dir(directory.parent)
        return
    if directory.is_symlink() or not directory.is_dir():
        replacement.invalid()
    expected = {item["name"]: item for item in commitments}
    if {file.name for file in directory.iterdir()} - set(expected):
        replacement.invalid()
    # The independent source SQL witness remains until all these files are
    # gone. receipt.json is last among an operation's own files.
    for item in sorted(commitments, key=lambda item: item["name"] == "receipt.json"):
        file = directory / item["name"]
        if file.exists():
            if replacement.regular(file).st_size != item["size"]:
                replacement.invalid()
            replacement.remove_owned(file, expected_digest=item["digest"])
        else:
            store._sync_dir(directory)
    directory.rmdir()
    store._sync_dir(directory.parent)


def _dispose(handle: NamespaceHandle, request: dict[str, Any], value: dict[str, Any]) -> None:
    from castle import cas_recovery as publication
    from castle import namespace_content as content
    from castle import namespace_replacement as replacement
    from castle import namespace_store as store

    n._authorize(handle.session, "excise-cleanup")
    for identity, files in value["cleanup"].items():
        _remove_directory(handle.root / "operations" / identity, files)
    directory = handle.root / "operations" / request["operation_id"]
    commitments = [
        {"name": name, "size": len(raw), "digest": hashlib.sha256(raw).hexdigest()}
        for name, raw in [
            ("request.json", n.canonical_bytes(request)),
            (
                "receipt.json",
                n.canonical_bytes(handle._ownership(request, n._info(value["source_info"]))),
            ),
        ]
    ]
    commitments.extend(
        {"name": item["file_id"], "size": item["size"], "digest": item["digest"]}
        for item in value["recipes"]
    )
    for saved in value["publications"]:
        item = _publication(saved, value)
        commitments.append(
            {
                "name": item.staged_file_id,
                "size": item.size,
                "digest": item.cas_id.removeprefix("sha256:"),
            }
        )
        record = publication._record_file(directory, item)
        temporary = directory / f"publication.{item.publication_id}.tmp"
        if record.exists():
            evidence = publication._load(handle.cas_root, directory, handle.binding, item)
            if evidence["state"] != "complete":
                replacement.invalid()
            content.validate_receipt(
                handle,
                {"state": "committed"},
                item,
                saved["receipt"],
                (handle.cas_root / journal.JOURNAL_FILENAME).read_bytes(),
            )
            commitments.append(_closed_file(record))
            if temporary.exists():
                replacement.regular(temporary)
                if not any(
                    publication._encoded(candidate).startswith(temporary.read_bytes())
                    for candidate in (evidence, {**evidence, "state": "prepared"})
                ):
                    replacement.invalid()
                commitments.append(_closed_file(temporary))
        elif temporary.exists():
            replacement.invalid()
    _remove_directory(directory, commitments)
    marker = replacement.read(handle.root)
    old = handle.root / replacement.filenames(marker)[1]
    replacement.remove_owned(old)
    store._sync_dir(handle.root)


def _target_inventory(handle: NamespaceHandle) -> None:
    from castle import namespace_content as content
    from castle import namespace_replacement as replacement

    expected = {"coordinator.lock", "operations", "namespace.sqlite", "replacement.json"}
    if {file.name for file in handle.root.iterdir()} != expected:
        replacement.invalid()
    for file in handle.root.iterdir():
        if file.name == "operations":
            if file.is_symlink() or not file.is_dir():
                replacement.invalid()
        else:
            replacement.regular(file)
    for directory in (handle.root / "operations").iterdir():
        n._uuid(directory.name)
        if directory.is_symlink() or not directory.is_dir():
            replacement.invalid()
        row = handle.db.execute(
            "SELECT state FROM operations WHERE operation_id=?", (directory.name,)
        ).fetchone()
        if row == ("retired",) or (row is None and directory.name != handle.anchor.namespace_id):
            replacement.invalid()
        content.inventory(handle, directory, structural=False)
        for file in directory.iterdir():
            replacement.regular(file)


def _complete(handle: NamespaceHandle, predecessor: Path, successor: Path) -> n.MutationResult:
    from castle import namespace_replacement as replacement
    from castle import namespace_store as store

    _target_open(handle, predecessor, successor)
    marker = replacement.read(handle.root)
    old = handle.root / replacement.filenames(marker)[1]
    with journal._stable_lock(successor):
        if marker["stage"] == "database-published":
            request, value = _old_intent(handle)
            _scan(handle, request, value)
            marker = _advance_marker(handle, "cleanup-scanned")
        if marker["stage"] == "cleanup-scanned":
            if old.exists():
                request, value = _old_intent(handle)
                _dispose(handle, request, value)
            _target_inventory(handle)
            marker = _advance_marker(handle, "cleanup-verified")
        if marker["stage"] == "cleanup-verified":
            _target_inventory(handle)
            handle._validate(replacement_pending=True)
            n._authorize(handle.session, "excise-completion")
            handle.db.execute("BEGIN IMMEDIATE")
            handle.db.execute(
                "UPDATE replacement_completion SET completed=1 WHERE operation_id=?",
                (marker["operation_id"],),
            )
            handle.db.execute("UPDATE namespace SET service_state='ready'")
            store._commit(handle.db)
            replacement.sync_file(handle.root / "namespace.sqlite")
            store._sync_dir(handle.root)
            marker = _advance_marker(handle, "complete")
        if marker["stage"] != "complete":
            replacement.invalid()
        # A visible complete marker may be the residue of a failed directory
        # sync. Every recovery acknowledgment freshly syncs that exact marker.
        replacement.publish(handle.root, marker, previous=marker)
        _target_inventory(handle)
        handle._validate()
        handle._recover_receipts()
        n._authorize(handle.session, "acknowledge")
        return completed_result(handle, marker["operation_id"])


def completed_result(handle: NamespaceHandle, operation: str) -> n.MutationResult:
    row = handle.db.execute(
        "SELECT cas_binding,revision_before,revision_after,completed "
        "FROM replacement_completion WHERE operation_id=?",
        (operation,),
    ).fetchone()
    if row is None or row[3] != 1:
        n._refuse("namespace-operation-retired")
    info = handle.load_info()
    return n.MutationResult(
        info.namespace_id,
        operation,
        None,
        "excise",
        row[1],
        row[2],
        [],
        None,
        store_state._identity(n._decode(row[0])),
    )


def _forward(
    handle: NamespaceHandle,
    request: dict[str, Any],
    value: dict[str, Any],
    predecessor: Path,
    successor: Path,
) -> n.MutationResult:
    if value["stage"] in {"prepared", "cas-complete"}:
        _cas_transition(handle, request, value, predecessor, successor)
        _publish_snapshots(handle, request, value, successor)
    elif value["stage"] == "publishing":
        _publish_snapshots(handle, request, value, successor)
    if value["stage"] in {"cas-complete", "publishing"}:
        _references(handle, request, value, successor)
    if value["stage"] != "references-published":
        n._refuse("namespace-storage-recovery-required")
    _fresh_database(handle, request, value, successor)
    return _complete(handle, predecessor, successor)


def execute(
    handle: NamespaceHandle, request: n.MutationRequest, sources: object, explicit_roots: object
) -> n.MutationResult:
    from collections.abc import Mapping

    from castle import namespace_transactions as tx

    n._authorize(handle.session, "excise")
    if (
        type(request) is not n.MutationRequest
        or request.kind != "excise"
        or not isinstance(sources, Mapping)
        or dict(sources)
    ):
        n._refuse()
    value = tx.normalize(request)
    started = False
    with handle.mutex:
        if handle.closed:
            # This invocation acquired no authority and started no effects.
            # The caller must use the explicit successor/recovery boundary to
            # learn the original token's outcome, rather than reuse this handle.
            raise n.NamespaceError(
                "namespace-storage-recovery-required",
                request.namespace_id,
                request.operation_id,
                effect="none",
            )
        try:
            with handle.admitted():
                predecessor, successor = roots(handle.root, handle.cas_root, explicit_roots)
                info = handle.load_info()
                if value["namespace_id"] != info.namespace_id:
                    n._refuse("namespace-binding-mismatch")
                row = tx._row(handle, value["operation_id"])
                if row is not None:
                    if row["state"] == "retired":
                        n._refuse("namespace-operation-retired")
                    if row["fingerprint"] != n._fingerprint(value):
                        n._refuse("namespace-operation-conflict")
                    return tx._replay(handle, row)
                if value["expected_cas"] != info.cas.to_dict():
                    n._refuse("namespace-binding-mismatch")
                if value["expected_revision"] != info.revision:
                    n._refuse("namespace-revision-conflict")
                if info.revision == n.MAX_INTEGER:
                    n._refuse("namespace-revision-exhausted")
                identity = value["operation_id"]
                operation_available(handle, identity)
                if successor.exists():
                    n._refuse("namespace-root-unsafe")
                intent = prepare_intent(handle, value, predecessor, successor)
                started = True
                handle.failed = True
            result = _forward(handle, value, intent, predecessor, successor)
            handle.close()
            return result
        except BaseException as error:
            # A possibly committed intent is never reported as a no-effect
            # failure merely because the call did not observe COMMIT return.
            if started or (not handle.closed and handle.write_started):
                handle.failed = True
                if handle.connection is not None and handle.db.in_transaction:
                    handle.db.execute("ROLLBACK")
                raise n.NamespaceError(
                    "namespace-storage-recovery-required",
                    request.namespace_id,
                    request.operation_id,
                    effect="recovery-required",
                ) from None
            if isinstance(error, n.NamespaceError):
                raise
            if isinstance(
                error,
                (OSError, sqlite3.Error, journal.CastleError, ValueError, TypeError, KeyError),
            ):
                n._refuse("namespace-storage-recovery-required")
            raise


def recover(handle: NamespaceHandle, explicit: object) -> n.RecoveryResult:
    """Specialized owner-held admission, before ordinary active-CAS/SQL open."""
    from castle import namespace_replacement as replacement
    from castle import namespace_transactions as tx

    if not isinstance(explicit, dict) or set(explicit) != {
        "predecessor_root",
        "successor_root",
        "operation_id",
        "expected_predecessor",
        "action",
    }:
        n._refuse("namespace-replacement-invalid")
    operation = n._uuid(explicit["operation_id"])
    expected = explicit["expected_predecessor"]
    if type(expected) is not n.StoreIdentity or explicit["action"] not in {
        "resume",
        "abort-before-retirement",
    }:
        n._refuse("namespace-replacement-invalid")
    predecessor, successor = roots(
        handle.root,
        handle.cas_root,
        {key: explicit[key] for key in ("predecessor_root", "successor_root")},
    )
    marker = replacement.read(handle.root)
    # Validate the sole selected generation before any engine open. A current
    # completed marker cannot invent a third root for historical recovery.
    _pending_inventory(handle, operation, marker)
    if marker is not None and marker["operation_id"] == operation:
        if marker["predecessor_cas"] != expected.to_dict():
            replacement.invalid()
        if marker["stage"] == "image-ready":
            old = handle.root / replacement.filenames(marker)[1]
            _, intent = _source_file(
                handle, old if old.exists() else handle.root / "namespace.sqlite", marker
            )
            if intent["roots"] != {
                "predecessor": root_commitment(predecessor),
                "successor": root_commitment(successor),
            }:
                replacement.invalid()
            _install_database(handle)
            marker = replacement.read(handle.root)
        if marker["stage"] not in {"blocked", "image-ready"}:
            if explicit["action"] != "resume":
                replacement.invalid()
            # The exact marker-bound SQL completion is control authority. Its
            # original root commitments are checked before any CAS payload I/O,
            # even after disposal of the private source intent.
            _target_open(handle, predecessor, successor)
            old = handle.root / replacement.filenames(marker)[1]
            if old.exists():
                _, intent = _old_intent(handle)
                if intent["roots"] != {
                    "predecessor": root_commitment(predecessor),
                    "successor": root_commitment(successor),
                }:
                    replacement.invalid()
            with journal._stable_lock(predecessor):
                retired = excision._retirement(predecessor, operation, expected)
                if retired is None or retired["transition"]["successor"] != marker["successor_cas"]:
                    replacement.invalid()
            result = _complete(handle, predecessor, successor)
            info = handle.load_info()
            return n.RecoveryResult(
                info.namespace_id, "ready", info.revision, info.cas, [result.operation_id]
            )
    if marker is not None and marker["stage"] != "complete" and marker["operation_id"] != operation:
        replacement.invalid()
    handle._read_anchor()
    handle._open()
    terminal = tx._row(handle, operation)
    if terminal is not None and terminal["state"] == "rejected":
        from castle import namespace_content as content

        outer = content.decode_recipe(terminal["ownership_receipt"])
        aborted = outer.get("cutover", {})
        if (
            explicit["action"] != "abort-before-retirement"
            or aborted.get("stage") != "aborted"
            or aborted.get("roots")
            != {
                "predecessor": root_commitment(predecessor),
                "successor": root_commitment(successor),
            }
        ):
            replacement.invalid()
        handle.binding = expected
        with journal._stable_lock(predecessor):
            handle._cas_check()
            handle._validate()
            handle._recover_receipts()
            n._authorize(handle.session, "acknowledge")
        info = handle.load_info()
        return n.RecoveryResult(info.namespace_id, "ready", info.revision, info.cas, [])
    request, intent = _intent(handle, operation)
    source = n._info(intent["source_info"])
    if source.cas != expected or intent["roots"] != {
        "predecessor": root_commitment(predecessor),
        "successor": root_commitment(successor),
    }:
        replacement.invalid()
    handle.binding = expected
    handle.epoch_intervals = replacement.epochs(
        handle.anchor,
        source,
        handle.db.execute("SELECT * FROM replacement_completion").fetchall(),
        intent["prior_marker"],
    )
    if (
        explicit["action"] == "abort-before-retirement"
        or tx._row(handle, operation)["recovery_action"] == "abort-before-retirement"
    ):
        return _abort(handle, request, intent, predecessor, successor)
    if intent["stage"] == "staging":
        with journal._stable_lock(predecessor):
            if excision._retirement(predecessor, operation, expected) is not None:
                replacement.invalid()
            handle._cas_check()
            # Only the still-verified original source may regenerate recipes.
            closure, recipes = selection_plan(handle, request)
            if closure != intent["closure"]:
                replacement.invalid()
            _fill_intent_files(handle, request, intent, [raw for _, _, raw in recipes])
    elif marker is None or marker == intent["prior_marker"]:
        replacement.publish(
            handle.root, _blocked_marker(request, intent), previous=intent["prior_marker"]
        )
    result = _forward(handle, request, intent, predecessor, successor)
    info = handle.load_info()
    return n.RecoveryResult(
        info.namespace_id, "ready", info.revision, info.cas, [result.operation_id]
    )


def _pending_inventory(
    handle: NamespaceHandle, operation: str, marker: dict[str, Any] | None
) -> None:
    from castle import namespace_replacement as replacement

    allowed = {
        "coordinator.lock",
        "operations",
        "namespace.sqlite",
        "namespace.sqlite-journal",
        f"replacement.{operation}.tmp",
    }
    if marker is not None:
        allowed.add("replacement.json")
        if marker["operation_id"] == operation and marker["stage"] != "complete":
            allowed.update(replacement.filenames(marker))
    if {file.name for file in handle.root.iterdir()} - allowed:
        replacement.invalid()
    for file in handle.root.iterdir():
        if file.name == "operations":
            if file.is_symlink() or not file.is_dir():
                replacement.invalid()
            for directory in file.iterdir():
                n._uuid(directory.name)
                if directory.is_symlink() or not directory.is_dir():
                    replacement.invalid()
                for child in directory.iterdir():
                    replacement.regular(child)
        else:
            replacement.regular(file)
    if not (handle.root / "operations").is_dir():
        replacement.invalid()


def _abort(
    handle: NamespaceHandle,
    request: dict[str, Any],
    value: dict[str, Any],
    predecessor: Path,
    successor: Path,
) -> n.RecoveryResult:
    from castle import namespace_content as content
    from castle import namespace_replacement as replacement
    from castle import namespace_store as store
    from castle import namespace_transactions as tx

    source = n._info(value["source_info"])
    operation = request["operation_id"]
    n._authorize(handle.session, "abort-before-retirement")
    # The CAS API's ordered locks prove no concurrent retirement writer.
    # A destination without a valid binding is never guessed to be ours.
    from contextlib import ExitStack

    with ExitStack() as locks:
        roots_to_lock = [predecessor] + ([successor] if successor.exists() else [])
        for root in sorted(roots_to_lock, key=bytes):
            locks.enter_context(journal._stable_lock(root))
        if (
            excision._retirement(predecessor, operation, source.cas) is not None
            or (predecessor / ".retired.json.tmp").exists()
        ):
            replacement.invalid()
        if successor.exists():
            destination = excision._destination(successor, operation, source.cas)
            if destination["state"] not in {"building", "prepared"}:
                replacement.invalid()
        handle._cas_check()
        if handle.load_info() != source:
            replacement.invalid()
        if value["stage"] not in {"staging", "prepared"} or value["cas_receipt"] is not None:
            replacement.invalid()
        handle.db.execute("BEGIN IMMEDIATE")
        handle.db.execute(
            "UPDATE operations SET recovery_action='abort-before-retirement' WHERE operation_id=?",
            (operation,),
        )
        store._commit(handle.db)
        directory = handle.root / "operations" / operation
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                replacement.invalid()
            known = {"request.json", "receipt.json"} | {
                item["file_id"] for item in value["recipes"]
            }
            if {file.name for file in directory.iterdir()} - known:
                replacement.invalid()
            # Aborting a partial recipe validates an exact prefix against the
            # still-active source view, never just its opaque filename.
            closure, recipes = selection_plan(handle, request)
            if closure != value["closure"]:
                replacement.invalid()
            for descriptor, (_, _, raw) in zip(value["recipes"], recipes, strict=True):
                if (
                    len(raw) != descriptor["size"]
                    or hashlib.sha256(raw).hexdigest() != descriptor["digest"]
                ):
                    replacement.invalid()
                file = directory / descriptor["file_id"]
                if file.exists():
                    replacement.regular(file)
                    if not raw.startswith(file.read_bytes()):
                        replacement.invalid()
                    replacement.remove_owned(file)
            store._sync_dir(directory)
        current = replacement.read(handle.root)
        blocked = _blocked_marker(request, value)
        if current not in (value["prior_marker"], blocked, None):
            replacement.invalid()
        temporary = handle.root / replacement.filenames(blocked)[2]
        if temporary.exists():
            replacement.regular(temporary)
            candidates = [blocked]
            if value["prior_marker"] is not None:
                candidates.append(value["prior_marker"])
            if not any(
                n.canonical_bytes(candidate).startswith(temporary.read_bytes())
                for candidate in candidates
            ):
                replacement.invalid()
            replacement.remove_owned(temporary)
        if current == blocked:
            replacement.remove_owned(
                handle.root / "replacement.json",
                expected_digest=hashlib.sha256(n.canonical_bytes(blocked)).hexdigest(),
            )
        if value["prior_marker"] is not None:
            replacement.publish(
                handle.root, value["prior_marker"], previous=None, temporary_operation=operation
            )
        else:
            store._sync_dir(handle.root)
        code = "namespace-io-failed"
        error = {
            "code": code,
            "namespace_id": source.namespace_id,
            "operation_id": operation,
            "actual_revision": source.revision,
            "effect": "none",
        }
        minimal = {
            "receipt": handle._ownership(request, source),
            "cutover": {"stage": "aborted", "roots": value["roots"]},
        }
        now = tx._now()
        from datetime import timedelta

        handle.db.execute("BEGIN IMMEDIATE")
        handle.db.execute(
            "UPDATE operations SET state='rejected',terminal_revision=?,completed_at=?,"
            "retire_after=?,terminal_error=?,ownership_receipt=?,recovery_action=NULL "
            "WHERE operation_id=?",
            (
                source.revision,
                now.isoformat(),
                (now + timedelta(days=7)).isoformat(),
                n.canonical_bytes(error).decode(),
                content.encode_recipe(minimal),
                operation,
            ),
        )
        handle.db.execute("UPDATE namespace SET service_state='ready'")
        handle.db.executemany(
            "INSERT INTO dependencies VALUES (?,?,?,?)",
            [
                ("operation", operation, identity, "request-result")
                for identity in request["args"]["selection"]["content_ids"]
            ],
        )
        store._commit(handle.db)
        handle.epoch_intervals = replacement.epochs(
            handle.anchor,
            source,
            handle.db.execute("SELECT * FROM replacement_completion").fetchall(),
            value["prior_marker"],
        )
        handle._restore_evidence(tx._row(handle, operation))
        handle._receipt_state(operation, "durable")
        handle._validate()
        n._authorize(handle.session, "acknowledge")
        return n.RecoveryResult(source.namespace_id, "ready", source.revision, source.cas, [])


def aborted_recipe(
    handle: NamespaceHandle, row: dict[str, Any], outer: dict[str, Any]
) -> dict[str, Any]:
    from castle import namespace_replacement as replacement

    request = n._decode(row["canonical_request"])
    value = outer.get("cutover")
    if (
        set(outer) != {"receipt", "cutover"}
        or not isinstance(value, dict)
        or set(value) != {"stage", "roots"}
        or value["stage"] != "aborted"
        or row["state"] != "rejected"
        or outer["receipt"] != handle._ownership(request, handle.ownership_info(row))
        or n._decode(row["terminal_error"])["code"] != "namespace-io-failed"
    ):
        replacement.invalid()
    if set(value["roots"]) != {"predecessor", "successor"} or any(
        not isinstance(digest, str) or replacement._DIGEST.fullmatch(digest) is None
        for digest in value["roots"].values()
    ):
        replacement.invalid()
    return {
        "receipt": outer["receipt"],
        "expiry": {},
        "publication": [],
        "precondition_error": "namespace-io-failed",
        "snapshot": None,
    }


def _projection(
    handle: NamespaceHandle, request: dict[str, Any], value: dict[str, Any]
) -> dict[str, list[tuple]]:
    """Logical rows only: selected payloads never enter the new page allocator."""
    from castle import namespace_store as store

    source = n._info(value["source_info"])
    rows = {
        table: handle.db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in store._TABLES
    }
    namespace = list(rows["namespace"][0])
    namespace[6] = value["target_generation"]
    namespace[8] = "recovery-required"
    rows["namespace"] = [tuple(namespace)]
    removed = set(value["closure"]["entry_ids"])
    rows["entries"] = [row for row in rows["entries"] if row[0] not in removed]
    retired = set(value["closure"]["operation_ids"]) | {request["operation_id"]}
    rows["operations"] = [
        (row[0], None, "retired", 0, None, None, None, None, None, None, None, None)
        if row[0] in retired
        else row
        for row in rows["operations"]
    ]
    rows["replacement_completion"].append(
        (
            request["operation_id"],
            value["target_generation"],
            n.canonical_bytes(value["cas_receipt"]["successor"]).decode(),
            source.revision,
            source.revision + 1,
            0,
            value["roots"]["predecessor"],
            value["roots"]["successor"],
        )
    )
    rows["replacement_completion"].sort()
    rows["dependencies"] = []
    return rows


def _image_bytes(
    handle: NamespaceHandle, request: dict[str, Any], value: dict[str, Any], successor: Path
) -> bytes:
    from castle import namespace_snapshots as snapshots
    from castle import namespace_store as store
    from castle import namespace_transactions as tx

    rows = _projection(handle, request, value)
    original = handle.connection
    old_root, old_binding, old_objects = handle.cas_root, handle.binding, handle.objects
    image = sqlite3.connect(":memory:", isolation_level=None)
    try:
        image.execute(f"PRAGMA page_size={value['page_size']}")
        image.execute("PRAGMA foreign_keys=ON")
        image.execute("PRAGMA synchronous=EXTRA")
        image.execute("PRAGMA fullfsync=ON")
        image.execute("BEGIN IMMEDIATE")
        for statement in store._TABLES.values():
            image.execute(statement)
        image.execute(
            "CREATE UNIQUE INDEX live_names ON entries(parent_id,comparison_key) WHERE live=1"
        )
        image.execute("PRAGMA user_version=1")
        for table in store._TABLES:
            for row in rows[table]:
                image.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in row)})", row)
        handle.connection = image
        handle.cas_root = successor
        handle.binding = store_state._identity(value["cas_receipt"]["successor"])
        handle.snapshot_cache.clear()
        with journal._stable_lock(successor):
            n._authorize(handle.session, "excise-image")
            records, _, _ = cas._validated_authority(successor, derived=True)
            handle.objects, _ = cas.expected_index_rows(records)
            dependencies = {
                ("entry", entry.entry_id, entry.cas_id, "content")
                for entry in handle.entries(live=False).values()
                if entry.cas_id
            }
            dependencies |= tx.operation_dependencies(handle)
            dependencies |= snapshots.dependencies(handle, allow_pending=True)
            image.executemany("INSERT INTO dependencies VALUES (?,?,?,?)", sorted(dependencies))
            store._commit(image)
            handle._validate(replacement_pending=True)
            if image.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                n._refuse("namespace-replacement-invalid")
            return image.serialize()
    finally:
        handle.connection = original
        handle.cas_root, handle.binding, handle.objects = old_root, old_binding, old_objects
        handle.snapshot_cache.clear()
        image.close()


def _advance_marker(handle: NamespaceHandle, stage: str) -> dict[str, Any]:
    from castle import namespace_replacement as replacement

    n._authorize(handle.session, "excise-marker")
    previous = replacement.read(handle.root)
    value = {**previous, "stage": stage}
    replacement.publish(handle.root, value, previous=previous)
    return value


def _fresh_database(
    handle: NamespaceHandle, request: dict[str, Any], value: dict[str, Any], successor: Path
) -> None:
    from castle import namespace_replacement as replacement
    from castle import namespace_store as store

    n._authorize(handle.session, "excise-image")
    raw = _image_bytes(handle, request, value, successor)
    commitment = {"size": len(raw), "digest": hashlib.sha256(raw).hexdigest()}
    if value["image"] not in (None, commitment):
        replacement.invalid()
    value["image"] = commitment
    _save_intent(handle, request["operation_id"], value)
    marker = replacement.read(handle.root)
    new_name, old_name, _ = replacement.filenames(marker)
    new = handle.root / new_name
    old = handle.root / old_name
    if old.exists():
        replacement.invalid()
    replacement.write_private(new, raw)
    store._sync_dir(handle.root)
    # SQLite is opened only after the exact complete image bytes are synced.
    # Its DELETE/EXTRA configuration and schema are checked on the real file.
    handle.db.close()
    handle.connection = None
    if (handle.root / "namespace.sqlite-journal").exists():
        replacement.invalid()
    image = sqlite3.connect(new.as_uri() + "?mode=rw", uri=True, isolation_level=None)
    try:
        image.execute("PRAGMA foreign_keys=ON")
        if image.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            replacement.invalid()
        image.execute("PRAGMA synchronous=EXTRA")
        image.execute("PRAGMA fullfsync=ON")
        if (
            image.execute("PRAGMA synchronous").fetchone() != (3,)
            or image.execute("PRAGMA fullfsync").fetchone() != (1,)
            or image.execute("PRAGMA foreign_keys").fetchone() != (1,)
            or image.execute("PRAGMA page_size").fetchone() != (value["page_size"],)
        ):
            replacement.invalid()
        if (
            image.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
            or image.execute("PRAGMA foreign_key_check").fetchall()
        ):
            replacement.invalid()
    finally:
        image.close()
    if replacement.digest(new) != commitment["digest"]:
        replacement.invalid()
    replacement.sync_file(new)
    store._sync_dir(handle.root)
    replacement.publish(
        handle.root,
        {**marker, "stage": "image-ready", "image_digest": commitment["digest"]},
        previous=marker,
    )
    _install_database(handle)


def _install_database(handle: NamespaceHandle) -> None:
    """Resume both rename gaps using the digest before any target SQL write."""
    from castle import namespace_replacement as replacement
    from castle import namespace_store as store

    marker = replacement.read(handle.root)
    new_name, old_name, _ = replacement.filenames(marker)
    new, old, main = (
        handle.root / new_name,
        handle.root / old_name,
        handle.root / "namespace.sqlite",
    )
    if marker["stage"] != "image-ready":
        replacement.invalid()
    _, intent = _source_file(handle, old if old.exists() else main, marker)
    if intent["image"] is None or intent["image"]["digest"] != marker["image_digest"]:
        replacement.invalid()
    n._authorize(handle.session, "excise-install")
    if old.exists() and main.exists():
        # Visibility is not evidence of the earlier rename's durability.
        # Verify the original pristine digest, then publish a fresh complete
        # owned image and a fresh replacement/directory boundary. A failed
        # rewrite leaves only this intent's exact-prefix .new slot to repair.
        replacement.verify_image(main, marker)
        replacement.write_private(new, main.read_bytes())
        replacement.verify_image(new, marker)
        os.replace(new, main)
        store._sync_dir(handle.root)
    elif new.exists():
        replacement.verify_image(new, marker)
        if main.exists():
            if old.exists():
                replacement.invalid()
            replacement.regular(main)
            store_state._rename_new(main, old)
            store._sync_dir(handle.root)
        elif not old.exists():
            replacement.invalid()
        store_state._rename_new(new, main)
        store._sync_dir(handle.root)
    else:
        replacement.invalid()
    _advance_marker(handle, "database-published")


def _target_open(handle: NamespaceHandle, predecessor: Path, successor: Path) -> None:
    from castle import namespace_replacement as replacement

    n._authorize(handle.session, "excise-target-open")
    marker = replacement.read(handle.root)
    if handle.connection is not None:
        handle.connection.close()
        handle.connection = None
    handle.cas_root = successor
    current = successor.lstat()
    handle.cas_root_inode = (current.st_dev, current.st_ino)
    handle.binding = store_state._identity(marker["successor_cas"])
    handle._read_anchor()
    handle._open()
    info = handle.load_info()
    handle.epoch_intervals = replacement.epochs(
        handle.anchor,
        info,
        handle.db.execute("SELECT * FROM replacement_completion").fetchall(),
        marker,
        allow_pending=marker["stage"] != "complete",
    )
    proof = handle.db.execute(
        "SELECT predecessor_root_commitment,successor_root_commitment "
        "FROM replacement_completion WHERE operation_id=?",
        (marker["operation_id"],),
    ).fetchone()
    if proof != (root_commitment(predecessor), root_commitment(successor)):
        replacement.invalid()
    old = handle.root / replacement.filenames(marker)[1]
    if old.exists():
        _, original = _old_intent(handle)
        if proof != (original["roots"]["predecessor"], original["roots"]["successor"]):
            replacement.invalid()
    with journal._stable_lock(successor):
        handle._cas_check()
        handle._validate(replacement_pending=marker["stage"] != "complete")
    if handle.load_info().database_generation != marker["target_generation"]:
        replacement.invalid()
    if (handle.root / "namespace.sqlite-journal").exists():
        # SQLite may leave a zero-header DELETE journal after a process died
        # before COMMIT. After validating this exact target generation, make a
        # fresh engine-owned durable transaction settle it; never unlink a
        # journal ourselves or treat its mere visibility as a safe rollback.
        n._authorize(handle.session, "excise-engine-recovery")
        handle._reassert()
        replacement.sync_file(handle.root / "namespace.sqlite")
        store_state._fsync_directory(handle.root)
        if (handle.root / "namespace.sqlite-journal").exists():
            replacement.invalid()


def prepare_intent(
    handle: NamespaceHandle, request: dict[str, Any], predecessor: Path, successor: Path
) -> dict[str, Any]:
    """Reserve complete allocation ownership before any operation file exists."""
    from castle import namespace_content as content
    from castle import namespace_replacement as replacement
    from castle import namespace_store as store

    info = handle.load_info()
    closure, recipes = selection_plan(handle, request)
    used = {info.namespace_id, info.root_id, info.database_generation, request["operation_id"]}
    for table, column in (
        ("entry_reservations", "entry_id"),
        ("snapshot_reservations", "snapshot_id"),
        ("operations", "operation_id"),
        ("replacement_completion", "target_generation"),
    ):
        used.update(row[0] for row in handle.db.execute(f"SELECT {column} FROM {table}"))
    used.update(item[2] for item in getattr(handle, "epoch_intervals", []))
    target = _fresh(used)
    descriptors = []
    for record, live, raw in recipes:
        descriptors.append(
            {
                "snapshot": asdict(record),
                "live": live,
                "file_id": _fresh(used),
                "size": len(raw),
                "digest": hashlib.sha256(raw).hexdigest(),
            }
        )
    cleanup = {}
    for identity in closure["operation_ids"]:
        directory = handle.root / "operations" / identity
        if directory.exists():
            if directory.is_symlink() or not directory.is_dir():
                n._refuse("namespace-root-unsafe")
            if {file.name for file in directory.iterdir()} != {"request.json", "receipt.json"}:
                n._refuse("namespace-storage-recovery-required")
            cleanup[identity] = [
                _closed_file(directory / name) for name in ("request.json", "receipt.json")
            ]
    prior = replacement.read(handle.root)
    if prior is not None and prior["stage"] != "complete":
        n._refuse("namespace-storage-recovery-required")
    value = {
        "source_info": asdict(info),
        "page_size": handle.db.execute("PRAGMA page_size").fetchone()[0],
        "roots": {
            "predecessor": root_commitment(predecessor),
            "successor": root_commitment(successor),
        },
        "target_generation": target,
        "prior_marker": prior,
        "closure": closure,
        "recipes": descriptors,
        "publications": [],
        "cas_receipt": None,
        "stage": "staging",
        "cleanup": cleanup,
        "retained_authority": _retained_commitment(
            handle, set(request["args"]["selection"]["content_ids"])
        ),
        "image": None,
    }
    receipt = handle._ownership(request, info)
    outer = {"receipt": receipt, "cutover": value}
    # All variable-size manifest bodies are separate files. The reserved SQL
    # form contains only fixed-size descriptors plus the bounded closure.
    future_size = len(content.encode_recipe(outer).encode()) + 4096 * len(descriptors)
    if future_size > content.OWNERSHIP_LIMIT:
        n._refuse("namespace-limit-exceeded")
    encoded = content.encode_recipe(outer)
    handle.operation_id, handle.write_started, handle.effect = (
        request["operation_id"],
        True,
        "recovery-required",
    )
    handle.db.execute("BEGIN IMMEDIATE")
    handle.db.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            request["operation_id"],
            n._fingerprint(request),
            "staging",
            info.revision,
            None,
            None,
            None,
            n.canonical_bytes(request).decode(),
            None,
            None,
            encoded,
            "resume",
        ),
    )
    handle.db.execute("UPDATE namespace SET service_state='recovery-required'")
    store._commit(handle.db)
    _fill_intent_files(handle, request, value, [raw for _, _, raw in recipes])
    return value


def _blocked_marker(request: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
    source = n._info(value["source_info"])
    return {
        "schema": "castle.namespace_replacement.v1",
        "namespace_id": source.namespace_id,
        "operation_id": request["operation_id"],
        "source_generation": source.database_generation,
        "target_generation": value["target_generation"],
        "predecessor_cas": source.cas.to_dict(),
        "successor_cas": value["cas_receipt"]["successor"]
        if value["cas_receipt"]
        else source.cas.to_dict(),
        "stage": "blocked",
        "image_digest": None,
    }


def _fill_intent_files(
    handle: NamespaceHandle, request: dict[str, Any], value: dict[str, Any], payloads: list[bytes]
) -> None:
    from castle import namespace_replacement as replacement
    from castle import namespace_store as store

    n._authorize(handle.session, "excise-intent-files")
    operation = request["operation_id"]
    directory = handle.root / "operations" / operation
    if not directory.exists():
        directory.mkdir(mode=0o700)
    if directory.is_symlink() or not directory.is_dir():
        n._refuse("namespace-root-unsafe")
    allowed = {"request.json", "receipt.json"} | {item["file_id"] for item in value["recipes"]}
    if {file.name for file in directory.iterdir()} - allowed:
        n._refuse("namespace-storage-recovery-required")
    source = n._info(value["source_info"])
    replacement.write_private(directory / "request.json", n.canonical_bytes(request))
    replacement.write_private(
        directory / "receipt.json", n.canonical_bytes(handle._ownership(request, source))
    )
    if len(payloads) != len(value["recipes"]):
        n._refuse("namespace-storage-recovery-required")
    for descriptor, raw in zip(value["recipes"], payloads, strict=True):
        n._authorize(handle.session, "excise-intent-files")
        if (
            len(raw) != descriptor["size"]
            or hashlib.sha256(raw).hexdigest() != descriptor["digest"]
        ):
            n._refuse("namespace-storage-recovery-required")
        replacement.write_private(directory / descriptor["file_id"], raw, mode=0o400)
    store._sync_dir(directory)
    store._sync_dir(directory.parent)
    value["stage"] = "prepared"
    _save_intent(handle, operation, value, state="prepared")
    replacement.publish(
        handle.root, _blocked_marker(request, value), previous=value["prior_marker"]
    )


def _intent(handle: NamespaceHandle, operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the SQL-owned cutover discriminator before using any file slot."""
    from castle import namespace_content as content
    from castle import namespace_replacement as replacement
    from castle import namespace_transactions as tx

    row = tx._row(handle, operation)
    if row is None or row["state"] not in {"staging", "prepared"}:
        replacement.invalid()
    request = n._decode(row["canonical_request"])
    reconstructed = n.MutationRequest(
        **{key: item for key, item in request.items() if key != "schema"}
    )
    reconstructed = replace(
        reconstructed, expected_cas=store_state._identity(reconstructed.expected_cas)
    )
    if tx.normalize(reconstructed) != request or request["kind"] != "excise":
        replacement.invalid()
    outer = content.decode_recipe(row["ownership_receipt"])
    if set(outer) != {"receipt", "cutover"}:
        replacement.invalid()
    value = outer["cutover"]
    if not isinstance(value, dict) or set(value) != _INTENT_KEYS:
        replacement.invalid()
    source = n._info(value["source_info"])
    if (
        value["stage"] not in _INTENT_STAGES
        or request["namespace_id"] != source.namespace_id
        or request["operation_id"] != operation
        or request["expected_cas"] != source.cas.to_dict()
        or request["expected_revision"] != source.revision
        or row["prepared_revision"] != source.revision
        or row["fingerprint"] != n._fingerprint(request)
        or outer["receipt"] != handle._ownership(request, source)
        or row["recovery_action"] not in {"resume", "abort-before-retirement"}
    ):
        replacement.invalid()
    n._uuid(value["target_generation"])
    if value["target_generation"] in {
        source.database_generation,
        operation,
        source.namespace_id,
        source.root_id,
    }:
        replacement.invalid()
    if type(value["page_size"]) is not int or value["page_size"] not in {
        512 << i for i in range(8)
    }:
        replacement.invalid()
    if not isinstance(value["roots"], dict) or set(value["roots"]) != {"predecessor", "successor"}:
        replacement.invalid()
    for digest in [*value["roots"].values(), *value["retained_authority"].values()]:
        if not isinstance(digest, str) or replacement._DIGEST.fullmatch(digest) is None:
            replacement.invalid()
    if set(value["retained_authority"]) != {"journal", "remotes", "objects"}:
        replacement.invalid()
    closure = value["closure"]
    if not isinstance(closure, dict) or set(closure) != {
        "entry_ids",
        "metadata_entry_ids",
        "operation_ids",
        "drop_snapshot_ids",
    }:
        replacement.invalid()
    for items in closure.values():
        if not isinstance(items, list) or items != sorted(set(items)):
            replacement.invalid()
        for identity in items:
            n._uuid(identity)
    if source.root_id in set(closure["entry_ids"]) | set(closure["metadata_entry_ids"]):
        replacement.invalid()
    if value["prior_marker"] is not None:
        previous = replacement.validate(value["prior_marker"])
        if (
            previous["stage"] != "complete"
            or previous["target_generation"] != source.database_generation
            or previous["successor_cas"] != source.cas.to_dict()
        ):
            replacement.invalid()
    used = {
        source.namespace_id,
        source.root_id,
        operation,
        source.database_generation,
        value["target_generation"],
    }
    snapshots = set()
    for descriptor in value["recipes"]:
        if not isinstance(descriptor, dict) or set(descriptor) != {
            "snapshot",
            "live",
            "file_id",
            "size",
            "digest",
        }:
            replacement.invalid()
        record = n._snapshot(descriptor["snapshot"])
        identity = n._uuid(descriptor["file_id"])
        if (
            record.snapshot_id in snapshots
            or identity in used
            or type(descriptor["live"]) is not int
            or descriptor["live"] not in {0, 1}
        ):
            replacement.invalid()
        snapshots.add(record.snapshot_id)
        used.add(identity)
        n._integer(descriptor["size"])
        if (
            descriptor["size"] > n.MAX_MANIFEST_BYTES
            or replacement._DIGEST.fullmatch(descriptor["digest"]) is None
        ):
            replacement.invalid()
    if value["cas_receipt"] is not None:
        from castle import excision

        saved = value["cas_receipt"]
        receipt = excision.ExcisionReceipt(
            saved["schema"],
            saved["operation_id"],
            store_state._identity(saved["predecessor"]),
            store_state._identity(saved["successor"]),
            saved["outcome"],
            saved["local_result"],
        )
        if (
            receipt.to_dict() != saved
            or receipt.operation_id != operation
            or receipt.predecessor != source.cas
        ):
            replacement.invalid()
    elif value["stage"] not in {"staging", "prepared"}:
        replacement.invalid()
    if not isinstance(value["publications"], list) or len(value["publications"]) > len(
        value["recipes"]
    ):
        replacement.invalid()
    for saved in value["publications"]:
        item = _publication(saved, value)
        if (
            item.namespace_id != source.namespace_id
            or item.operation_id != operation
            or item.publication_id in used
            or item.staged_file_id in used
            or item.publication_id == item.staged_file_id
        ):
            replacement.invalid()
        used.update((item.publication_id, item.staged_file_id))
        if saved["receipt"] is not None:
            content._receipt(saved["receipt"], item)
    if not isinstance(value["cleanup"], dict) or set(value["cleanup"]) - set(
        closure["operation_ids"]
    ):
        replacement.invalid()
    for identity, files in value["cleanup"].items():
        n._uuid(identity)
        if not isinstance(files, list) or [file["name"] for file in files] != [
            "request.json",
            "receipt.json",
        ]:
            replacement.invalid()
        for file in files:
            if (
                set(file) != {"name", "size", "digest"}
                or replacement._DIGEST.fullmatch(file["digest"]) is None
            ):
                replacement.invalid()
            n._integer(file["size"])
    if value["image"] is not None:
        if (
            set(value["image"]) != {"size", "digest"}
            or replacement._DIGEST.fullmatch(value["image"]["digest"]) is None
        ):
            replacement.invalid()
        n._integer(value["image"]["size"])
    return request, value


def _publication(saved: dict[str, Any], value: dict[str, Any]):
    from castle import cas_recovery as publication
    from castle import namespace_replacement as replacement

    if not isinstance(saved, dict) or set(saved) != {"request", "receipt"}:
        replacement.invalid()
    fields = dict(saved["request"])
    fields["cas_binding"] = store_state._identity(fields["cas_binding"])
    item = publication.PublicationRequest(**fields)
    publication._request(item, store_state._identity(value["cas_receipt"]["successor"]))
    return item


def _recipe_bytes(handle: NamespaceHandle, operation: str, value: dict[str, Any]) -> list[bytes]:
    from castle import namespace_replacement as replacement

    n._authorize(handle.session, "excise-recipes")
    result = []
    for descriptor in value["recipes"]:
        n._authorize(handle.session, "excise-recipes")
        file = handle.root / "operations" / operation / descriptor["file_id"]
        if (
            replacement.regular(file).st_size != descriptor["size"]
            or replacement.digest(file) != descriptor["digest"]
        ):
            replacement.invalid()
        result.append(file.read_bytes())
    return result


def _cas_transition(
    handle: NamespaceHandle,
    request: dict[str, Any],
    value: dict[str, Any],
    predecessor: Path,
    successor: Path,
) -> None:
    from castle import excision
    from castle import namespace_replacement as replacement

    n._authorize(handle.session, "excise")
    source = n._info(value["source_info"])
    with journal._stable_lock(predecessor):
        retired = excision._retirement(predecessor, request["operation_id"], source.cas)
        if retired is None:
            handle._cas_check()
            handle._validate(cutover_operation=request["operation_id"])
    if retired is None:
        receipt = excision.excise_store(
            predecessor,
            successor,
            cas_ids=request["args"]["selection"]["content_ids"],
            operation_id=request["operation_id"],
            expected_source=source.cas,
        )
    else:
        receipt = excision.recover_excision(
            predecessor, successor, operation_id=request["operation_id"], expected_source=source.cas
        )
    if value["cas_receipt"] is not None and value["cas_receipt"] != receipt.to_dict():
        replacement.invalid()
    value["cas_receipt"] = receipt.to_dict()
    value["stage"] = "cas-complete"
    _save_intent(handle, request["operation_id"], value)
    marker = replacement.read(handle.root)
    replacement.publish(handle.root, _blocked_marker(request, value), previous=marker)


def _publish_snapshots(
    handle: NamespaceHandle, request: dict[str, Any], value: dict[str, Any], successor: Path
) -> None:
    from castle import cas_recovery as publication
    from castle import namespace_content as content
    from castle import namespace_replacement as replacement
    from castle import namespace_store as store

    operation = request["operation_id"]
    n._authorize(handle.session, "excise-publish-snapshots")
    binding = store_state._identity(value["cas_receipt"]["successor"])
    payloads = []
    for raw in _recipe_bytes(handle, operation, value):
        manifest = n._decode(raw)
        manifest["cas_binding"] = binding.to_dict()
        payloads.append(n.canonical_bytes(manifest, limit=n.MAX_MANIFEST_BYTES))
    # Every stage allocation is independently durable before the first file.
    if not value["publications"] and payloads:
        used = {operation, handle.load_info().namespace_id, value["target_generation"]} | {
            item["file_id"] for item in value["recipes"]
        }
        used.update(handle.authority_ids())
        for table, column in (
            ("entry_reservations", "entry_id"),
            ("snapshot_reservations", "snapshot_id"),
            ("operations", "operation_id"),
        ):
            used.update(row[0] for row in handle.db.execute(f"SELECT {column} FROM {table}"))
        for raw in payloads:
            item = publication.PublicationRequest(
                handle.load_info().namespace_id,
                operation,
                _fresh(used),
                binding,
                "sha256:" + hashlib.sha256(raw).hexdigest(),
                len(raw),
                _fresh(used),
            )
            value["publications"].append({"request": asdict(item), "receipt": None})
        value["stage"] = "publishing"
        _save_intent(handle, operation, value)
    if len(value["publications"]) != len(payloads):
        replacement.invalid()
    directory = handle.root / "operations" / operation
    items = [_publication(saved, value) for saved in value["publications"]]
    for item, raw in zip(items, payloads, strict=True):
        n._authorize(handle.session, "excise-publication-stage")
        if item.cas_id != "sha256:" + hashlib.sha256(raw).hexdigest() or item.size != len(raw):
            replacement.invalid()
        replacement.write_private(directory / item.staged_file_id, raw, mode=0o400)
    store._sync_dir(directory)
    with journal._stable_lock(successor):
        n._authorize(handle.session, "excise-publication")
        started = [
            i for i, item in enumerate(items) if publication._record_file(directory, item).exists()
        ]
        if started != list(range(len(started))):
            replacement.invalid()
        for i in started[:-1]:
            if publication._load(successor, directory, binding, items[i])["state"] != "complete":
                replacement.invalid()
        order = ([started[-1]] if started else []) + [
            i for i in range(len(items)) if not started or i != started[-1]
        ]
        for i in order:
            item = items[i]
            n._authorize(handle.session, "excise-publication")
            receipt = publication._run_locked(
                successor,
                directory,
                binding,
                item,
                recover=publication._record_file(directory, item).exists(),
            )
            saved = asdict(receipt)
            saved["event_ids"] = list(receipt.event_ids)
            content._receipt(saved, item)
            if value["publications"][i]["receipt"] not in (None, saved):
                replacement.invalid()
            value["publications"][i]["receipt"] = saved
            _save_intent(handle, operation, value)


def _references(
    handle: NamespaceHandle, request: dict[str, Any], value: dict[str, Any], successor: Path
) -> None:
    from castle import namespace_content as content
    from castle import namespace_store as store
    from castle import namespace_transactions as tx

    n._authorize(handle.session, "excise-references")
    source = n._info(value["source_info"])
    binding = store_state._identity(value["cas_receipt"]["successor"])
    with journal._stable_lock(successor):
        if store_state.read_store_identity(successor) != binding:
            n._refuse("namespace-binding-mismatch")
        records, _, _ = cas._validated_authority(successor, derived=True)
        objects, _ = cas.expected_index_rows(records)
        raw_journal = (successor / journal.JOURNAL_FILENAME).read_bytes()
        prior_root, prior_binding = handle.cas_root, handle.binding
        try:
            handle.cas_root, handle.binding = successor, binding
            for saved in value["publications"]:
                item = _publication(saved, value)
                content.validate_receipt(
                    handle, {"state": "committed"}, item, saved["receipt"], raw_journal
                )
        finally:
            handle.cas_root, handle.binding = prior_root, prior_binding
    if any(
        identity not in objects
        for identity in {
            entry.cas_id
            for entry in handle.entries(live=False).values()
            if entry.cas_id and entry.entry_id not in value["closure"]["entry_ids"]
        }
    ):
        n._refuse("namespace-storage-recovery-required")
    value["stage"] = "references-published"
    from castle import namespace_content as content_io

    outer = {"receipt": handle._ownership(request, source), "cutover": value}
    snapshot_dependencies = set()
    for descriptor, saved, raw in zip(
        value["recipes"],
        value["publications"],
        _recipe_bytes(handle, request["operation_id"], value),
        strict=True,
    ):
        manifest = n._decode(raw)
        manifest["cas_binding"] = binding.to_dict()
        item = _publication(saved, value)
        if (
            "sha256:"
            + hashlib.sha256(n.canonical_bytes(manifest, limit=n.MAX_MANIFEST_BYTES)).hexdigest()
            != item.cas_id
        ):
            n._refuse("namespace-storage-recovery-required")
        snapshot_id = descriptor["snapshot"]["snapshot_id"]
        snapshot_dependencies.add(("snapshot", snapshot_id, item.cas_id, "manifest"))
        snapshot_dependencies.update(
            ("snapshot", snapshot_id, entry["cas_id"], "member")
            for entry in manifest["entries"]
            if entry["cas_id"]
        )
    handle.db.execute("BEGIN IMMEDIATE")
    for identity in value["closure"]["entry_ids"]:
        handle.db.execute("UPDATE entries SET live=0 WHERE entry_id=?", (identity,))
    for identity in value["closure"]["metadata_entry_ids"]:
        handle.db.execute(
            "UPDATE entries SET metadata=?,changed_revision=CASE WHEN live=1 "
            "THEN ? ELSE changed_revision END WHERE entry_id=?",
            ("{}\n", source.revision + 1, identity),
        )
    for identity in value["closure"]["drop_snapshot_ids"]:
        handle.db.execute("DELETE FROM snapshots WHERE snapshot_id=?", (identity,))
    for descriptor, saved in zip(value["recipes"], value["publications"], strict=True):
        item = _publication(saved, value)
        handle.db.execute(
            "UPDATE snapshots SET cas_id=?,size=? WHERE snapshot_id=?",
            (item.cas_id, item.size, descriptor["snapshot"]["snapshot_id"]),
        )
    handle.db.execute(
        "UPDATE namespace SET revision=?,cas_binding=?",
        (source.revision + 1, n.canonical_bytes(binding.to_dict()).decode()),
    )
    removed = set(value["closure"]["entry_ids"])
    retired = set(value["closure"]["operation_ids"])
    dependencies = {
        ("entry", entry.entry_id, entry.cas_id, "content")
        for entry in handle.entries(live=False).values()
        if entry.cas_id and entry.entry_id not in removed
    }
    dependencies |= {row for row in tx.operation_dependencies(handle) if row[1] not in retired}
    dependencies |= snapshot_dependencies
    handle.db.execute("DELETE FROM dependencies")
    handle.db.executemany("INSERT INTO dependencies VALUES (?,?,?,?)", sorted(dependencies))
    handle.db.execute(
        "UPDATE operations SET ownership_receipt=? WHERE operation_id=?",
        (content_io.encode_recipe(outer), request["operation_id"]),
    )
    store._commit(handle.db)
