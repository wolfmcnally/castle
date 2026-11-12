"""Atomic namespace tree changes and deterministic durable retry outcomes."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from castle import cas
from castle import namespace as n
from castle import namespace_content as content_io
from castle import namespace_snapshots as snapshots
from castle import namespace_store as store

_ARGS = {
    "create-directory": {"entry_id", "parent_id", "name", "metadata"},
    "create-file": {"entry_id", "parent_id", "name", "metadata", "content"},
    "set-metadata": {"entry_id", "metadata"},
    "replace-content": {"entry_id", "content"},
    "relocate": {"entry_id", "parent_id", "name"},
    "duplicate": {"entry_id", "parent_id", "name", "identities"},
    "delete-entry": {"entry_id"},
    "delete-subtree": {"entry_id"},
    "expire-results": {"operation_ids"},
    "import-folder": {"parent_id", "entries"},
    "snapshot": {"snapshot_id"},
    "delete-snapshot": {"snapshot_id"},
    "excise": {"selection", "snapshot_actions"},
}


def _now() -> datetime:
    return datetime.now(UTC)


def normalize(request: n.MutationRequest) -> dict[str, Any]:
    if type(request) is not n.MutationRequest or type(request.expected_cas) is not n.StoreIdentity:
        n._refuse()
    try:
        value = n._decode(n.canonical_bytes(asdict(request)))
    except RecursionError:
        n._refuse("namespace-limit-exceeded")
    n._uuid(value["namespace_id"])
    n._uuid(value["operation_id"])
    n._integer(value["expected_revision"])
    kind, args = value["kind"], value["args"]
    if (
        not isinstance(kind, str)
        or kind not in _ARGS
        or not isinstance(args, dict)
        or set(args) != _ARGS[kind]
    ):
        n._refuse()
    for key in ("entry_id", "parent_id", "snapshot_id"):
        if key in args:
            n._uuid(args[key])
    if "name" in args:
        args["name"] = n._name(args["name"])
    if "metadata" in args:
        args["metadata"] = n._metadata(args["metadata"])
    if "content" in args:
        content = args["content"]
        if not isinstance(content, dict) or set(content) != {"cas_id", "size"}:
            n._refuse()
        if not isinstance(content["cas_id"], str) or n._CAS.fullmatch(content["cas_id"]) is None:
            n._refuse()
        n._integer(content["size"])
    if kind == "import-folder":
        incoming = args["entries"]
        if not isinstance(incoming, list) or len(incoming) > n.MAX_ENTRIES:
            n._refuse("namespace-limit-exceeded")
        for item in incoming:
            if not isinstance(item, dict) or set(item) != {
                "entry_id",
                "kind",
                "parent_id",
                "name",
                "metadata",
                "cas_id",
                "size",
            }:
                n._refuse()
            n._uuid(item["entry_id"])
            n._uuid(item["parent_id"])
            item["name"] = n._name(item["name"])
            item["metadata"] = n._metadata(item["metadata"])
            if item["kind"] == "directory":
                if item["cas_id"] is not None or item["size"] is not None:
                    n._refuse()
            elif item["kind"] == "file":
                if not isinstance(item["cas_id"], str) or n._CAS.fullmatch(item["cas_id"]) is None:
                    n._refuse()
                n._integer(item["size"])
            else:
                n._refuse()
        if len({item["entry_id"] for item in incoming}) != len(incoming):
            n._refuse()
        args["entries"] = sorted(incoming, key=lambda item: item["entry_id"])
        content_io.specs(value)
    if "identities" in args:
        identities = args["identities"]
        if not isinstance(identities, list) or not identities or len(identities) > n.MAX_ENTRIES:
            n._refuse("namespace-limit-exceeded")
        for pair in identities:
            if not isinstance(pair, dict) or set(pair) != {"source_id", "new_id"}:
                n._refuse()
            n._uuid(pair["source_id"])
            n._uuid(pair["new_id"])
        if len({p["source_id"] for p in identities}) != len(identities) or len(
            {p["new_id"] for p in identities}
        ) != len(identities):
            n._refuse()
        args["identities"] = sorted(identities, key=lambda p: p["source_id"])
    if "operation_ids" in args:
        values = args["operation_ids"]
        if not isinstance(values, list) or len(values) > n.MAX_ENTRIES:
            n._refuse("namespace-limit-exceeded")
        for identity in values:
            n._uuid(identity)
        if len(set(values)) != len(values) or value["operation_id"] in values:
            n._refuse()
        args["operation_ids"] = sorted(values)
    if kind == "excise":
        from castle import namespace_excision as cutover

        value["args"] = cutover.normalize(args)
    n.canonical_bytes(value)
    return value


def _row(handle: store.NamespaceHandle, operation_id: str) -> dict[str, Any] | None:
    cursor = handle.db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,))
    row = cursor.fetchone()
    return None if row is None else dict(zip([d[0] for d in cursor.description], row, strict=True))


def _raise_saved(handle: store.NamespaceHandle, row: dict[str, Any]) -> None:
    error = n._terminal_error(n._decode(row["terminal_error"]))
    n._authorize(handle.session, "acknowledge")
    raise error from None


def _replay(handle: store.NamespaceHandle, row: dict[str, Any]) -> n.MutationResult:
    handle._check_operation(row, handle.load_info())
    if row["state"] == "retired":
        n._refuse("namespace-operation-retired")
    if row["state"] == "rejected":
        _raise_saved(handle, row)
    if row["state"] != "committed" or row["terminal_result"] is None:
        n._refuse("namespace-storage-recovery-required")
    result = n._result(n._decode(row["terminal_result"]))
    n._authorize(handle.session, "acknowledge")
    return result


def prepare(
    handle: store.NamespaceHandle,
    request: dict[str, Any],
    *,
    evidence_present: bool = False,
    publications=None,
    snapshot: n.SnapshotRecord | None = None,
    rejection: str | None = None,
) -> None:
    info = handle.load_info()
    if info.revision == n.MAX_INTEGER:
        n._refuse("namespace-revision-exhausted")
    identity = request["operation_id"]
    if (
        handle.db.execute(
            "SELECT 1 FROM entry_reservations WHERE entry_id=?", (identity,)
        ).fetchone()
        or handle.db.execute(
            "SELECT 1 FROM snapshot_reservations WHERE snapshot_id=?", (identity,)
        ).fetchone()
        or identity in handle.authority_ids()
    ):
        n._refuse()
    receipt: dict[str, Any] = {
        "receipt": handle._ownership(request, info),
        "expiry": {},
        "publication": [
            {"request": asdict(item), "receipt": None} for item in (publications or [])
        ],
        "snapshot": asdict(snapshot) if snapshot else None,
        "precondition_error": rejection,
    }
    if request["kind"] == "expire-results":
        for target in request["args"]["operation_ids"]:
            target_row = _row(handle, target)
            if target_row is not None:
                handle._check_operation(target_row, info)
            if target_row and target_row["state"] in {"committed", "rejected"}:
                directory = handle.root / "operations" / target
                receipt["expiry"][target] = {
                    "receipt": handle._receipt(target),
                    "request_sha256": hashlib.sha256(
                        (directory / "request.json").read_bytes()
                    ).hexdigest(),
                }
    content_io.check_capacity(receipt)
    encoded_receipt = content_io.encode_recipe(receipt)
    handle.db.execute("BEGIN IMMEDIATE")
    # At most one operation is ever unsettled; a new one needs proof the last one finished.
    (marked,) = handle.db.execute("SELECT unsettled_operation FROM coordinator_state").fetchone()
    if marked is not None:
        if not handle._settled(marked):
            n._refuse("namespace-storage-recovery-required")
        handle.db.execute("UPDATE coordinator_state SET unsettled_operation=NULL")
    handle.operation_id = identity
    handle.write_started = True
    handle.effect = "recovery-required"
    handle.db.execute(
        "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            identity,
            n._fingerprint(request),
            "staging" if publications or rejection is not None else "prepared",
            info.revision,
            None,
            None,
            None,
            n.canonical_bytes(request).decode(),
            None,
            None,
            encoded_receipt,
            None,
        ),
    )
    store._commit(handle.db)
    if not evidence_present:
        handle._evidence(request, info)


def execute(
    handle: store.NamespaceHandle, request: n.MutationRequest, sources: Mapping[str, object]
) -> n.MutationResult:
    n._authorize(handle.session, request.kind if type(request) is n.MutationRequest else "execute")
    value = normalize(request)
    info = handle.load_info()
    if value["namespace_id"] != info.namespace_id:
        n._refuse("namespace-binding-mismatch")
    row = _row(handle, value["operation_id"])
    if row is not None:
        handle.operation_id = value["operation_id"]
        # Prove the touched row before any domain refusal is derived from it.
        handle._check_operation(row, info)
        if row["state"] == "retired":
            # Retirement removes the operation directory before its expiry
            # settles, and ordinary admission refuses an unsettled expiry, so
            # a surviving directory here is a forged or damaged row.
            if (handle.root / "operations" / row["operation_id"]).exists():
                n._refuse("namespace-storage-recovery-required")
            n._refuse("namespace-operation-retired")
        if row["fingerprint"] != n._fingerprint(value):
            n._refuse("namespace-operation-conflict")
        return _replay(handle, row)
    rejection = (
        "namespace-binding-mismatch"
        if value["expected_cas"] != asdict(info.cas)
        else "namespace-revision-conflict"
        if value["expected_revision"] != info.revision
        else None
    )
    data = {}
    snapshot = None
    if rejection is None:
        data = content_io.source(handle, value, sources)
        try:
            _changes(handle, value, info, available={key: len(raw) for key, raw in data.items()})
            if value["kind"] == "snapshot":
                snapshot, raw = snapshots.capture(handle, value["args"]["snapshot_id"])
                data = {snapshot.cas_id: raw}
            elif value["kind"] == "delete-snapshot":
                snapshot = snapshots.catalog(handle, value["args"]["snapshot_id"])
        except n.NamespaceError as error:
            if error.code in {
                "namespace-storage-recovery-required",
                "namespace-protection-unsupported",
            }:
                raise
            rejection = error.code
    publications = content_io.new_requests(handle, value, data) if rejection is None else []
    prepare(handle, value, publications=publications, snapshot=snapshot, rejection=rejection)
    if snapshot is not None and value["kind"] == "snapshot":
        handle.pending_captures[(value["operation_id"], snapshot)] = data[snapshot.cas_id]
    if publications:
        for item in publications:
            content_io.write_stage(handle, item, data[item.cas_id])
        content_io.promote(handle, value["operation_id"])
    return finish(handle, value["operation_id"])


class _TreeView:
    """The touched part of the live tree plus this request's overlay.

    Every row it loads passes touch_entry; loaded originals also pass their
    ancestry. Children come from live_names, so no mutation loads the tree.
    """

    def __init__(self, handle: store.NamespaceHandle, info: n.NamespaceInfo) -> None:
        self.handle, self.info = handle, info
        self.overlay: dict[str, n.EntryRecord | None] = {}
        # Live overlay entries indexed once per write, so a request's checks
        # stay linear in its own size (CODE-F020).
        self._added_children: dict[str, set[str]] = {}
        self._added_names: dict[tuple[str | None, str], set[str]] = {}

    def original(self, identity: str) -> n.EntryRecord | None:
        """A stored row this request rests on; any finding about it is damage.

        A read may report the specific code, but a mutation must not record a
        durable domain rejection that rests on a damaged row (CODE-F018).
        """
        try:
            touched = self.handle.touch_entry(identity, self.info.revision)
            if touched is None or not touched[1]:
                return None
            self.handle.touch_ancestry(touched[0], self.info)
        except n.NamespaceError as error:
            if error.code in store._DIAGNOSE_PASSTHROUGH:
                raise
            raise n.NamespaceError(
                "namespace-storage-recovery-required", effect="recovery-required"
            ) from error
        return touched[0]

    def get(self, identity: str | None) -> n.EntryRecord | None:
        if identity is None:
            return None
        if identity in self.overlay:
            return self.overlay[identity]
        return self.original(identity)

    def __contains__(self, identity: object) -> bool:
        return isinstance(identity, str) and self.get(identity) is not None

    def __getitem__(self, identity: str) -> n.EntryRecord:
        found = self.get(identity)
        if found is None:
            raise KeyError(identity)
        return found

    def _unindex(self, identity: str) -> None:
        previous = self.overlay.get(identity)
        if previous is not None:
            self._added_children[previous.parent_id].discard(identity)
            self._added_names[(previous.parent_id, n._key(previous.name))].discard(identity)

    def __setitem__(self, identity: str, entry: n.EntryRecord) -> None:
        self._unindex(identity)
        self.overlay[identity] = entry
        self._added_children.setdefault(entry.parent_id, set()).add(identity)
        self._added_names.setdefault((entry.parent_id, n._key(entry.name)), set()).add(identity)

    def __delitem__(self, identity: str) -> None:
        self._unindex(identity)
        self.overlay[identity] = None

    def children(self, identity: str) -> list[str]:
        stored = [
            child
            for (child,) in self.handle.db.execute(
                "SELECT entry_id FROM entries WHERE parent_id=? AND live=1 "
                "ORDER BY comparison_key,entry_id",
                (identity,),
            )
            if child not in self.overlay
        ]
        return stored + sorted(self._added_children.get(identity, ()))

    def has_children(self, identity: str) -> bool:
        """Whether a directory has a live child, without loading its children."""
        if self._added_children.get(identity):
            return True
        for (child,) in self.handle.db.execute(
            "SELECT entry_id FROM entries WHERE parent_id=? AND live=1", (identity,)
        ):
            if child not in self.overlay:
                return True
        return False

    def members(self, identity: str) -> set[str]:
        result: set[str] = set()
        pending = [identity]
        while pending:
            current = pending.pop()
            if current in result:
                n._refuse("namespace-cycle")
            result.add(current)
            pending.extend(self.children(current))
        return result

    def check(self, info: n.NamespaceInfo) -> None:
        """store._tree over the next state, restricted to what this request changed."""
        handle = self.handle
        originals = {key: self.original(key) for key in self.overlay}
        live, total, _ = store.tree_totals(handle)
        for key, entry in self.overlay.items():
            if originals[key] is not None:
                live, total = live - 1, total - store.entry_bytes(originals[key])
            if entry is not None:
                live, total = live + 1, total + store.entry_bytes(entry)
        if live > n.MAX_ENTRIES:
            self._limit()
        for key, entry in self.overlay.items():
            if entry is None:
                if self.has_children(key):
                    n._refuse("namespace-format-unsupported")
                continue
            store._validate_entry(entry, info.revision)
            if key == info.root_id:
                n._refuse("namespace-format-unsupported")
            parent = self.get(entry.parent_id)
            if parent is None or parent.kind != "directory":
                n._refuse("namespace-format-unsupported")
            comparison = n._key(entry.name)
            stored = [
                other
                for (other,) in handle.db.execute(
                    "SELECT entry_id FROM entries WHERE parent_id=? AND comparison_key=? "
                    "AND live=1",
                    (entry.parent_id, comparison),
                )
                if other not in self.overlay
            ]
            added = self._added_names.get((entry.parent_id, comparison), set()) - {key}
            for other in stored:
                # A durable rejection may rest only on a sibling that passes
                # touched validation itself (CODE-F013).
                sibling = self.original(other)
                if (
                    sibling is None
                    or sibling.parent_id != entry.parent_id
                    or n._key(sibling.name) != comparison
                ):
                    n._refuse("namespace-storage-recovery-required")
            if stored or added:
                n._refuse("namespace-name-conflict")
        for key, entry in self.overlay.items():
            seen: set[str] = set()
            current = entry
            while current is not None and current.entry_id != info.root_id:
                if current.entry_id in seen:
                    n._refuse("namespace-cycle")
                seen.add(current.entry_id)
                current = self.get(current.parent_id)
            if entry is not None and current is None:
                n._refuse("namespace-cycle")
        from castle.namespace_snapshots import manifest

        empty = len(n.canonical_bytes(manifest(info, {}), limit=n.MAX_MANIFEST_BYTES))
        if empty + total + max(0, live - 1) > n.MAX_MANIFEST_BYTES:
            self._limit()

    def _limit(self) -> None:
        """A limit rejection derived from tree_totals first re-derives them (PLAN-F006)."""
        summary = store.summarize(self.handle.entries())
        if store.tree_totals(self.handle) != summary:
            n._refuse("namespace-storage-recovery-required")
        n._refuse("namespace-limit-exceeded")


def _content(handle: store.NamespaceHandle, cas_id: str, size: int) -> None:
    row = handle.content_row(cas_id)
    if row is None or row[1] != size:
        n._refuse("namespace-content-mismatch")
    file = cas.object_path(handle.cas_root, cas_id)
    if cas.sha256_file(file) != cas_id or not cas._write_protected(file.stat()):
        n._refuse("namespace-storage-recovery-required")


def _changes(
    handle: store.NamespaceHandle,
    request: dict[str, Any],
    info: n.NamespaceInfo,
    *,
    available: dict[str, int] | None = None,
) -> tuple[dict[str, n.EntryRecord], set[str]]:
    entries = _TreeView(handle, info)
    kind, args = request["kind"], request["args"]
    revision = info.revision + 1
    affected: set[str] = set()
    verified_content = set()

    def verify_content(identity: str, size: int) -> None:
        if (identity, size) not in verified_content:
            _content(handle, identity, size)
            verified_content.add((identity, size))

    def entry(identity: str) -> n.EntryRecord:
        if identity not in entries:
            n._refuse("namespace-entry-missing")
        return entries[identity]

    def parent(identity: str) -> None:
        if entry(identity).kind != "directory":
            n._refuse("namespace-kind-mismatch")

    def fresh(identity: str) -> None:
        if (
            identity in handle.authority_ids() | {request["operation_id"]}
            or handle.db.execute(
                "SELECT 1 FROM entry_reservations WHERE entry_id=?", (identity,)
            ).fetchone()
            or handle.db.execute(
                "SELECT 1 FROM operations WHERE operation_id=?", (identity,)
            ).fetchone()
            or handle.db.execute(
                "SELECT 1 FROM snapshot_reservations WHERE snapshot_id=?", (identity,)
            ).fetchone()
        ):
            n._refuse("namespace-request-invalid")

    if kind in {"create-directory", "create-file"}:
        fresh(args["entry_id"])
        parent(args["parent_id"])
        content = args.get("content")
        if content is not None and (available or {}).get(content["cas_id"]) != content["size"]:
            verify_content(content["cas_id"], content["size"])
        added = n.EntryRecord(
            args["entry_id"],
            "file" if content is not None else "directory",
            args["parent_id"],
            args["name"],
            args["metadata"],
            content["cas_id"] if content else None,
            content["size"] if content else None,
            revision,
        )
        entries[added.entry_id] = added
        affected.add(added.entry_id)
    elif kind == "replace-content":
        source = entry(args["entry_id"])
        if source.entry_id == info.root_id:
            n._refuse("namespace-root-immutable")
        if source.kind != "file":
            n._refuse("namespace-kind-mismatch")
        content = args["content"]
        if (available or {}).get(content["cas_id"]) != content["size"]:
            verify_content(content["cas_id"], content["size"])
        entries[source.entry_id] = replace(
            source, cas_id=content["cas_id"], size=content["size"], changed_revision=revision
        )
        affected.add(source.entry_id)
    elif kind in {"relocate", "set-metadata", "delete-entry", "delete-subtree", "duplicate"}:
        source = entry(args["entry_id"])
        if source.entry_id == info.root_id and kind != "duplicate":
            n._refuse("namespace-root-immutable")
        # Only the subtree operations walk the subtree (CODE-F007); the others
        # stay proportional to the touched rows and the new parent's ancestry.
        membership = (
            entries.members(source.entry_id)
            if kind in {"delete-subtree", "duplicate"}
            else {source.entry_id}
        )
        if kind == "relocate":
            parent(args["parent_id"])
            ancestor = entries.get(args["parent_id"])
            while ancestor is not None:
                if ancestor.entry_id == source.entry_id:
                    n._refuse("namespace-cycle")
                ancestor = entries.get(ancestor.parent_id)
            entries[source.entry_id] = replace(
                source, parent_id=args["parent_id"], name=args["name"], changed_revision=revision
            )
            affected.add(source.entry_id)
        elif kind == "set-metadata":
            entries[source.entry_id] = replace(
                source, metadata=args["metadata"], changed_revision=revision
            )
            affected.add(source.entry_id)
        elif kind in {"delete-entry", "delete-subtree"}:
            if kind == "delete-entry" and entries.has_children(source.entry_id):
                n._refuse("namespace-directory-not-empty")
            if kind == "delete-subtree" and source.kind != "directory":
                n._refuse("namespace-kind-mismatch")
            affected = membership
            for identity in affected:
                del entries[identity]
        else:
            parent(args["parent_id"])
            mapping = {pair["source_id"]: pair["new_id"] for pair in args["identities"]}
            if set(mapping) != membership:
                n._refuse("namespace-request-invalid")
            frozen = {identity: entries[identity] for identity in membership}
            for old, new in mapping.items():
                fresh(new)
                current = frozen[old]
                if current.cas_id is not None:
                    _content(handle, current.cas_id, current.size)
                entries[new] = replace(
                    current,
                    entry_id=new,
                    parent_id=args["parent_id"]
                    if old == source.entry_id
                    else mapping[current.parent_id],
                    name=args["name"] if old == source.entry_id else current.name,
                    changed_revision=revision,
                )
                affected.add(new)
    elif kind == "import-folder":
        parent(args["parent_id"])
        incoming = {item["entry_id"]: item for item in args["entries"]}
        for item in incoming.values():
            fresh(item["entry_id"])
        for item in incoming.values():
            target = item["parent_id"]
            if target != args["parent_id"]:
                if target not in incoming:
                    n._refuse(
                        "namespace-request-invalid"
                        if target in entries
                        else "namespace-entry-missing"
                    )
                if incoming[target]["kind"] != "directory":
                    n._refuse("namespace-kind-mismatch")
            if item["cas_id"] is not None and (available or {}).get(item["cas_id"]) != item["size"]:
                verify_content(item["cas_id"], item["size"])
            entries[item["entry_id"]] = n.EntryRecord(**item, changed_revision=revision)
            affected.add(item["entry_id"])
    elif kind in {"snapshot", "delete-snapshot"}:
        snapshots.precondition(handle, request)
    elif kind != "expire-results":
        n._refuse()
    if len(affected) > n.MAX_ENTRIES:
        n._refuse("namespace-limit-exceeded")
    entries.check(replace(info, revision=revision))
    return entries, affected


def _expire(handle: store.NamespaceHandle, request: dict[str, Any], current_time: datetime) -> None:
    for identity in request["args"]["operation_ids"]:
        row = _row(handle, identity)
        if row is None or row["state"] not in {"committed", "rejected"}:
            continue
        deadline = datetime.fromisoformat(row["retire_after"])
        if current_time < deadline:
            continue
        handle.db.execute(
            (
                "UPDATE operations SET "
                "fingerprint=NULL,state='retired',prepared_revision=0,terminal_rev"
                "ision=NULL,completed_at=NULL,retire_after=NULL,canonical_request="
                "NULL,terminal_result=NULL,terminal_error=NULL,ownership_receipt=N"
                "ULL,recovery_action=NULL WHERE operation_id=?"
            ),
            (identity,),
        )


def cleanup_expired(handle: store.NamespaceHandle, row: dict[str, Any]) -> None:
    if row["state"] not in {"committed", "rejected"} or row["canonical_request"] is None:
        return
    request = n._decode(row["canonical_request"])
    if request["kind"] != "expire-results":
        return
    recipe = content_io.decode_recipe(row["ownership_receipt"])
    if not recipe["expiry"]:
        return
    # A rejected expiry owns no disposal. Its copied proof is unnecessary,
    # including if a later, independent expiry retired the same target.
    for identity, expected in recipe["expiry"].items() if row["state"] == "committed" else []:
        target = _row(handle, identity)
        if target is None:
            n._refuse("namespace-storage-recovery-required")
        if target["state"] != "retired":
            continue
        directory = handle.root / "operations" / identity
        if not directory.exists():
            # A prior failed sync or process death can leave visible absence.
            # Establish its durability before discarding the ownership proof.
            store._sync_dir(directory.parent)
            continue
        for name in ("request.json", "receipt.json"):
            file = directory / name
            if not file.exists():
                continue
            store._regular(file)
            raw = file.read_bytes()
            if name == "request.json":
                valid = hashlib.sha256(raw).hexdigest() == expected["request_sha256"]
            else:
                valid = n._decode(raw) == expected["receipt"]
            if not valid:
                n._refuse("namespace-storage-recovery-required")
            file.unlink()
        directory.rmdir()
        store._sync_dir(directory.parent)
    # Keep the complete recipe through every disposal/sync failure. Only a
    # durable SQL commit may forget it; an uncertain commit blocks admission
    # and explicit recovery reconciles whichever version actually survived.
    recipe["expiry"] = {}
    handle.write_started = True
    handle.db.execute("BEGIN IMMEDIATE")
    handle.db.execute(
        "UPDATE operations SET ownership_receipt=? WHERE operation_id=?",
        (content_io.encode_recipe(recipe), row["operation_id"]),
    )
    store._commit(handle.db)


def finish(handle: store.NamespaceHandle, operation_id: str) -> n.MutationResult:
    row = _row(handle, operation_id)
    if row is None:
        n._refuse("namespace-storage-recovery-required")
    if row["state"] not in {"staging", "prepared"}:
        return _replay(handle, row)
    recipe, publications = content_io.recipe(handle, row)
    request = n._decode(row["canonical_request"])
    reconstructed = n.MutationRequest(
        request["namespace_id"],
        n.StoreIdentity(**request["expected_cas"]),
        request["expected_revision"],
        request["operation_id"],
        request["kind"],
        request["args"],
    )
    if normalize(reconstructed) != request or n._fingerprint(request) != row["fingerprint"]:
        n._refuse("namespace-storage-recovery-required")
    info = handle.load_info()
    rejection = None
    publication_receipt = handle.publication_receipts.get(operation_id, [])
    try:
        if recipe["precondition_error"] is not None:
            n._refuse(recipe["precondition_error"])
        if row["state"] == "staging":
            n._refuse("namespace-input-unfrozen")
        if request["namespace_id"] != info.namespace_id or request["expected_cas"] != asdict(
            info.cas
        ):
            n._refuse("namespace-binding-mismatch")
        if request["expected_revision"] != info.revision:
            n._refuse("namespace-revision-conflict")
        _changes(handle, request, info, available={item.cas_id: item.size for item in publications})
        if publications:
            publication_receipt = handle.publication_receipts.get(operation_id)
            if publication_receipt is None:
                publication_receipt = content_io.publish(handle, row)
            handle.publication_receipts[operation_id] = publication_receipt
            handle._cas_check(full=False)
            # Locate the appended lines; drop proofs taken before publication.
            handle._cas_position()
            handle.forget_touched()
        entries, affected = _changes(handle, request, info)
    except n.NamespaceError as error:
        if error.code in {
            "namespace-storage-recovery-required",
            "namespace-protection-unsupported",
        }:
            raise
        if publication_receipt:
            # A published command cannot become a durable domain rejection.
            n._refuse("namespace-storage-recovery-required")
        rejection = n.NamespaceError(
            error.code, info.namespace_id, operation_id, info.revision, "none"
        )
        entries, affected = None, set()
    completed = _now()
    handle.write_started = True
    handle.effect = "recovery-required"
    handle.db.execute("BEGIN IMMEDIATE")
    if rejection is None:
        content_io.save_receipt(handle, row, publication_receipt)
    if rejection is None:
        old_entries = {key: entries.original(key) for key in affected}
        # Vacate affected live names first; no observer sees intermediate rows.
        for identity in affected:
            if old_entries[identity] is not None:
                handle.vacate_entry(old_entries[identity])
        for identity in sorted(affected):
            if entries.get(identity) is not None:
                handle.db.execute(
                    "INSERT OR IGNORE INTO entry_reservations VALUES (?)", (identity,)
                )
                handle.write_entry(entries[identity])
            else:
                handle.write_entry(
                    old_entries[identity], live=False, touched_revision=info.revision + 1
                )
        if request["kind"] == "expire-results":
            _expire(handle, request, completed)
        snapshot_result = snapshots.commit(handle, request, recipe["snapshot"])
        result = n.MutationResult(
            info.namespace_id,
            operation_id,
            row["fingerprint"],
            request["kind"],
            info.revision,
            info.revision + 1,
            sorted(affected),
            snapshot_result,
            info.cas,
        )
        handle.db.execute(
            "UPDATE namespace SET revision=?,service_state='ready'", (result.revision_after,)
        )
        payload = n.canonical_bytes(asdict(result)).decode()
        terminal_error = None
    else:
        result = None
        payload = None
        terminal_error = n.canonical_bytes(
            {
                "code": rejection.code,
                "namespace_id": rejection.namespace_id,
                "operation_id": operation_id,
                "actual_revision": info.revision,
                "effect": "none",
            }
        ).decode()
        handle.db.execute("UPDATE namespace SET service_state='ready'")
    handle.db.execute(
        (
            "UPDATE operations SET "
            "state=?,terminal_revision=?,completed_at=?,retire_after=?,termina"
            "l_result=?,terminal_error=? WHERE operation_id=?"
        ),
        (
            "rejected" if rejection else "committed",
            info.revision if rejection else result.revision_after,
            completed.isoformat(),
            (completed + timedelta(days=7)).isoformat(),
            payload,
            terminal_error,
            operation_id,
        ),
    )
    handle.db.execute("UPDATE coordinator_state SET unsettled_operation=?", (operation_id,))
    n._authorize(handle.session, request["kind"])
    store._commit(handle.db)
    handle.publication_receipts.pop(operation_id, None)
    for key in [key for key in handle.pending_captures if key[0] == operation_id]:
        del handle.pending_captures[key]
    handle.effect = "none" if rejection else "namespace-committed"
    cleanup_expired(handle, _row(handle, operation_id))
    content_io.cleanup(handle, _row(handle, operation_id))
    handle._receipt_state(operation_id, "durable")
    n._authorize(handle.session, "acknowledge")
    if rejection is not None:
        raise rejection from None
    return result


def _live(handle: store.NamespaceHandle, info: n.NamespaceInfo, identity: str) -> n.EntryRecord:
    """One live entry and its ancestry, validated against current bytes."""
    touched = handle.touch_entry(identity, info.revision)
    if touched is None or not touched[1]:
        n._refuse("namespace-entry-missing")
    handle.touch_ancestry(touched[0], info)
    return touched[0]


def _child(
    handle: store.NamespaceHandle, info: n.NamespaceInfo, parent: n.EntryRecord, name: str
) -> n.EntryRecord:
    """The live child of a validated directory named by the comparison key, via live_names."""
    rows = handle.db.execute(
        "SELECT entry_id FROM entries WHERE parent_id=? AND comparison_key=? AND live=1",
        (parent.entry_id, n._key(name)),
    ).fetchall()
    if len(rows) != 1:
        n._refuse("namespace-entry-missing")
    return _live(handle, info, rows[0][0])


def _probe(handle: store.NamespaceHandle, info: n.NamespaceInfo) -> n.NamespaceProbe:
    live, _, files = store.tree_totals(handle)
    position = handle._position()
    if position is None:
        n._refuse("namespace-storage-recovery-required")
    return n.NamespaceProbe(
        info.namespace_id,
        info.database_generation,
        info.revision,
        info.cas,
        live,
        files,
        position[1],
        position[2],
        position[7],
    )


def _page(
    handle: store.NamespaceHandle, info: n.NamespaceInfo, args: dict[str, Any]
) -> n.DirectoryPage:
    directory = _live(handle, info, args["entry_id"])
    if directory.kind != "directory":
        n._refuse("namespace-kind-mismatch")
    # live_names is unique on (parent_id, comparison_key), so the key alone
    # orders a page, and each form seeks rather than scans (CODE-F024).
    if args["after"] is None:
        rows = handle.db.execute(
            "SELECT entry_id FROM entries WHERE parent_id=? AND live=1 "
            "ORDER BY comparison_key LIMIT ?",
            (directory.entry_id, args["limit"] + 1),
        ).fetchall()
    else:
        rows = handle.db.execute(
            "SELECT entry_id FROM entries WHERE parent_id=? AND live=1 AND comparison_key>? "
            "ORDER BY comparison_key LIMIT ?",
            (directory.entry_id, n._key(args["after"]), args["limit"] + 1),
        ).fetchall()
    entries = [_live(handle, info, identity) for (identity,) in rows[: args["limit"]]]
    more = len(rows) > args["limit"]
    return n.DirectoryPage(entries, entries[-1].name if more else None)


def _changes_since(
    handle: store.NamespaceHandle, info: n.NamespaceInfo, args: dict[str, Any]
) -> n.ChangePage:
    if args["database_generation"] != info.database_generation:
        # A cutover issued a fresh authority: the caller resynchronizes.
        raise n.NamespaceError(
            "namespace-revision-conflict", info.namespace_id, actual_revision=info.revision
        )
    rows = handle.db.execute(
        "SELECT entry_id,touched_revision FROM entries WHERE (touched_revision,entry_id)>(?,?) "
        "ORDER BY touched_revision,entry_id LIMIT ?",
        (args["after_revision"], args["after_entry_id"] or "", args["limit"] + 1),
    ).fetchall()
    changes = []
    for identity, touched_revision in rows[: args["limit"]]:
        entry, live = handle.touch_entry(identity, info.revision)
        if live:
            handle.touch_ancestry(entry, info)
        changes.append(n.EntryChange(entry, live, touched_revision))
    more = len(rows) > args["limit"]
    return n.ChangePage(
        changes,
        changes[-1].touched_revision if more else None,
        changes[-1].entry.entry_id if more else None,
    )


def read(
    handle: store.NamespaceHandle,
    request: n.ReadRequest,
    *,
    destination: n.SnapshotDestination | None = None,
) -> n.ReadResult:
    n._authorize(handle.session, "read")
    if type(request) is not n.ReadRequest or type(request.expected_cas) is not n.StoreIdentity:
        n._refuse()
    if isinstance(request.kind, str) and request.kind in {"inventory-excision", "preview-excision"}:
        n._authorize(handle.session, "excision-inventory")
    value = n._decode(n.canonical_bytes(asdict(request)))
    n._uuid(value["namespace_id"])
    if value["at_revision"] is not None:
        n._integer(value["at_revision"])
    args, kind = value["args"], value["kind"]
    expected = {
        "info": set(),
        "get-entry": {"entry_id"},
        "list-directory": {"entry_id"},
        "resolve": {"components"},
        "inspect-snapshot": {"snapshot_id"},
        "export-snapshot": {"snapshot_id"},
        "inventory-excision": set(),
        "preview-excision": {"operation_id", "selection", "snapshot_actions"},
        "probe": set(),
        "list-directory-page": {"entry_id", "after", "limit"},
        "changes-since": {"database_generation", "after_revision", "after_entry_id", "limit"},
        "diagnose": set(),
    }
    if (
        not isinstance(kind, str)
        or kind not in expected
        or not isinstance(args, dict)
        or set(args) != expected[kind]
    ):
        n._refuse()
    if (kind == "export-snapshot") != (destination is not None):
        n._refuse()
    if kind == "preview-excision" and value["at_revision"] is None:
        n._refuse()
    if "snapshot_id" in args:
        n._uuid(args["snapshot_id"])
    if "entry_id" in args:
        n._uuid(args["entry_id"])
    if "limit" in args:
        if n._integer(args["limit"]) < 1 or args["limit"] > n.MAX_PAGE:
            n._refuse()
    if kind == "list-directory-page" and args["after"] is not None:
        args["after"] = n._name(args["after"])
    if kind == "changes-since":
        n._uuid(args["database_generation"])
        n._integer(args["after_revision"])
        if args["after_entry_id"] is not None:
            n._uuid(args["after_entry_id"])
    if kind == "resolve":
        if not isinstance(args["components"], list) or len(args["components"]) > n.MAX_ENTRIES:
            n._refuse()
        args["components"] = [n._name(v) for v in args["components"]]
    handle.db.execute("BEGIN")
    try:
        info = handle.load_info()
        if value["namespace_id"] != info.namespace_id or value["expected_cas"] != asdict(info.cas):
            n._refuse("namespace-binding-mismatch")
        if value["at_revision"] is not None and value["at_revision"] != info.revision:
            raise n.NamespaceError(
                "namespace-revision-conflict", info.namespace_id, actual_revision=info.revision
            )
        if kind in {"inventory-excision", "preview-excision"}:
            from castle import namespace_excision as cutover

            # Whole-sequence reads keep 0.3.0's full validation before they run.
            handle._cas_check()
            handle._validate()
            result = (
                cutover.inventory(handle)
                if kind == "inventory-excision"
                else cutover.preview(handle, info, args)
            )
        elif kind in {"inspect-snapshot", "export-snapshot"}:
            record = snapshots.catalog(handle, args["snapshot_id"])
            result = n.SnapshotView(record, snapshots.catalog_view(handle, record))
        elif kind == "info":
            result = info
        elif kind == "probe":
            result = _probe(handle, info)
        elif kind == "diagnose":
            result = n.NamespaceDiagnosis(**handle.diagnose_admitted())
        elif kind == "list-directory-page":
            result = _page(handle, info, args)
        elif kind == "changes-since":
            result = _changes_since(handle, info, args)
        elif kind == "resolve":
            result = _live(handle, info, info.root_id)
            for component in args["components"]:
                if result.kind != "directory":
                    n._refuse("namespace-kind-mismatch")
                result = _child(handle, info, result, component)
        else:
            result = _live(handle, info, args["entry_id"])
            if kind == "list-directory":
                if result.kind != "directory":
                    n._refuse("namespace-kind-mismatch")
                result = [
                    _live(handle, info, identity)
                    for (identity,) in handle.db.execute(
                        "SELECT entry_id FROM entries WHERE parent_id=? AND live=1 "
                        "ORDER BY comparison_key,entry_id",
                        (result.entry_id,),
                    ).fetchall()
                ]
        n.canonical_bytes(
            result.manifest
            if isinstance(result, n.SnapshotView)
            else n._plain(result)
            if not isinstance(result, list)
            else [asdict(e) for e in result],
            limit=n.MAX_MANIFEST_BYTES,
        )
        handle.db.execute("COMMIT")
    except BaseException:
        if handle.db.in_transaction:
            handle.db.execute("ROLLBACK")
        raise
    n._authorize(handle.session, "read-result")
    return n.ReadResult(info.namespace_id, info.revision, info.cas, result)


def operation_target(handle: store.NamespaceHandle, status: n.OperationStatus) -> n.OperationTarget:
    """The entry a committed operation acted on, from its validated stored request."""
    primary = None
    if status.state == "committed" and status.result is not None:
        row = _row(handle, status.operation_id)
        request = n._decode(row["canonical_request"]) if row and row["canonical_request"] else None
        kind = request["kind"] if request else None
        args = request["args"] if request else {}
        if kind in {
            "create-directory",
            "create-file",
            "replace-content",
            "relocate",
            "set-metadata",
            "delete-entry",
            "delete-subtree",
        }:
            primary = args["entry_id"]
        elif kind == "duplicate":
            primary = next(
                pair["new_id"]
                for pair in args["identities"]
                if pair["source_id"] == args["entry_id"]
            )
    return n.OperationTarget(*store.astuple_status(status), primary)


def operation(handle: store.NamespaceHandle, operation_id: str) -> n.OperationStatus:
    n._authorize(handle.session, "status")
    n._uuid(operation_id)
    handle.db.execute("BEGIN")
    try:
        info = handle.load_info()
        row = _row(handle, operation_id)
        if row is not None:
            handle._check_operation(row, info)
        if row is None:
            result = n.OperationStatus(
                info.namespace_id, operation_id, "unknown", None, None, "none"
            )
        else:
            state = row["state"]
            completed = None
            if (
                state == "retired"
                and handle.db.execute(
                    "SELECT 1 FROM replacement_completion WHERE operation_id=? AND completed=1",
                    (operation_id,),
                ).fetchone()
            ):
                from castle import namespace_excision as cutover

                completed = cutover.completed_result(handle, operation_id)
            if state == "staging":
                state = "recovery-required"
            if state not in {"prepared", "committed", "rejected", "retired", "recovery-required"}:
                n._refuse("namespace-storage-recovery-required")
            error = n._decode(row["terminal_error"])["code"] if row["terminal_error"] else None
            result = n.OperationStatus(
                info.namespace_id,
                operation_id,
                state,
                n._result(n._decode(row["terminal_result"]))
                if row["terminal_result"]
                else completed,
                error,
                "recover"
                if state == "recovery-required"
                else "retry-same"
                if state == "prepared"
                else "refresh-new-token"
                if state == "rejected"
                else "none",
            )
        handle.db.execute("COMMIT")
    except BaseException:
        if handle.db.in_transaction:
            handle.db.execute("ROLLBACK")
        raise
    n._authorize(handle.session, "status-result")
    return result


def operation_dependencies(handle: store.NamespaceHandle) -> set[tuple[str, str, str, str]]:
    entries = handle.entries(live=False)
    expected = set()
    for (identity,) in handle.db.execute("SELECT operation_id FROM operations").fetchall():
        row = _row(handle, identity)
        if row["state"] not in {"committed", "rejected"}:
            continue
        request = n._decode(row["canonical_request"])
        content_ids = set(content_io.specs(request))
        value = content_io.recipe(handle, row)[0]
        if request["kind"] == "excise":
            content_ids.update(request["args"]["selection"]["content_ids"])
        content_ids.update(item["request"]["cas_id"] for item in value["publication"])
        if row["state"] == "committed":
            result = n._result(n._decode(row["terminal_result"]))
            content_ids.update(
                entries[key].cas_id
                for key in result.affected_entry_ids
                if key in entries and entries[key].cas_id is not None
            )
            if result.snapshot:
                content_ids.add(result.snapshot.cas_id)
            if request["kind"] == "delete-snapshot":
                content_ids.add(n._snapshot(value["snapshot"]).cas_id)
        expected.update(("operation", identity, cas_id, "request-result") for cas_id in content_ids)
    return expected
