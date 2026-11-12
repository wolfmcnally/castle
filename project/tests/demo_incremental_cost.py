"""Operator demo: ordinary namespace work costs the same at two namespace sizes.

Run: ./bin/python project/tests/demo_incremental_cost.py

It builds synthetic namespaces of 100 and 5000 entries in a temporary
directory, prints the SQL statements and entry validations spent by open, a
path lookup, a small directory page and a metadata change of the directory
holding almost every entry, then corrupts an entry no ordinary step touched:
the lookup still succeeds and the explicit diagnosis refuses. The temporary
data is removed afterwards. Nothing outside the temporary directory is read
or written.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unicodedata
import uuid
from pathlib import Path

from castle import cas, store_state
from castle import namespace as n
from castle import namespace_store as store
from castle.namespace_environment import LocalSession


def _id() -> str:
    return str(uuid.uuid4())


class _Store:
    def __init__(self, directory: Path) -> None:
        directory.mkdir()
        self.root, self.cas_root = directory / "namespace", directory / "cas"
        source = directory / "source"
        source.write_bytes(b"demo content\n")
        self.content = {
            "cas_id": cas.ingest_file(self.cas_root, source, "demo").cas_id,
            "size": source.stat().st_size,
        }
        self.session = LocalSession(
            self.root, self.cas_root, directory / "domain.lock", create_domain_lock=True
        )
        self.initial = n.InitializeRequest(
            _id(),
            _id(),
            store_state.read_store_identity(self.cas_root),
            {"policy_version": 1, "unicode_data_version": unicodedata.unidata_version},
            {"kind": "unprotected"},
        )
        n.create_namespace(self.root, self.cas_root, self.session, self.initial)

    def open(self) -> n.NamespaceHandle:
        return n.open_namespace(self.root, self.cas_root, self.session)

    def apply(self, handle: n.NamespaceHandle, kind: str, args: dict) -> None:
        info = handle.info()
        request = n.MutationRequest(info.namespace_id, info.cas, info.revision, _id(), kind, args)
        n.execute_namespace(handle, request, {})

    def read(self, handle: n.NamespaceHandle, kind: str, args: dict) -> object:
        info = handle.info()
        return n.read_namespace(
            handle, n.ReadRequest(info.namespace_id, info.cas, None, kind, args)
        ).value


def _build(directory: Path, size: int) -> tuple[_Store, dict[str, str]]:
    demo = _Store(directory)
    root = demo.initial.root_id
    ids = {"big": _id(), "small": _id(), "leaf": _id()}
    with demo.open() as handle:
        for key, name in (("big", "Big"), ("small", "Small")):
            demo.apply(
                handle,
                "create-directory",
                {"entry_id": ids[key], "parent_id": root, "name": name, "metadata": {}},
            )
        demo.apply(
            handle,
            "create-file",
            {
                "entry_id": ids["leaf"],
                "parent_id": ids["small"],
                "name": "leaf",
                "metadata": {},
                "content": demo.content,
            },
        )
        members = [
            {
                "entry_id": _id(),
                "kind": "file",
                "parent_id": ids["big"],
                "name": f"member-{index:05}",
                "metadata": {},
                "cas_id": demo.content["cas_id"],
                "size": demo.content["size"],
            }
            for index in range(size)
        ]
        ids["untouched"] = members[0]["entry_id"]
        demo.apply(
            handle,
            "import-folder",
            {"parent_id": ids["big"], "entries": sorted(members, key=lambda m: m["entry_id"])},
        )
    return demo, ids


def _measure(demo: _Store, ids: dict[str, str]) -> dict[str, tuple[int, int]]:
    counts = {"statements": 0, "entries": 0}
    connect, validate_entry = store.NamespaceHandle._connect, store._validate_entry

    def traced(self, *, create):
        connect(self, create=create)
        self.db.set_trace_callback(
            lambda _: counts.__setitem__("statements", counts["statements"] + 1)
        )

    def counted(*args):
        counts["entries"] += 1
        return validate_entry(*args)

    store.NamespaceHandle._connect, store._validate_entry = traced, counted
    result: dict[str, tuple[int, int]] = {}
    handle = None
    try:

        def step(label, action):
            counts.update(statements=0, entries=0)
            action()
            result[label] = (counts["statements"], counts["entries"])

        def opened():
            nonlocal handle
            handle = demo.open()

        step("open", opened)
        step("get-entry", lambda: demo.read(handle, "resolve", {"components": ["Small", "leaf"]}))
        step(
            "list small directory",
            lambda: demo.read(
                handle,
                "list-directory-page",
                {"entry_id": ids["small"], "after": None, "limit": 10},
            ),
        )
        step(
            "set-metadata on Big",
            lambda: demo.apply(
                handle, "set-metadata", {"entry_id": ids["big"], "metadata": {"k": 1}}
            ),
        )
    finally:
        store.NamespaceHandle._connect, store._validate_entry = connect, validate_entry
        if handle is not None:
            handle.close()
    return result


def main() -> None:
    sizes = (100, 5000)
    with tempfile.TemporaryDirectory(prefix="castle-demo-") as raw:
        built = {size: _build(Path(raw) / f"n{size}", size) for size in sizes}
        measured = {size: _measure(*built[size]) for size in sizes}
        print("SQL statements / entry validations per step")
        print(f"{'step':<24}" + "".join(f"{'N=' + str(size):>16}" for size in sizes))
        for label in measured[sizes[0]]:
            print(
                f"{label:<24}"
                + "".join(f"{'{} / {}'.format(*measured[size][label]):>16}" for size in sizes)
            )
        same = measured[sizes[0]] == measured[sizes[1]]
        print("identical at both sizes:", "yes" if same else "NO")

        demo, ids = built[sizes[1]]
        with sqlite3.connect(demo.root / "namespace.sqlite") as db:
            db.execute("UPDATE entries SET metadata='[' WHERE entry_id=?", (ids["untouched"],))
        print("\nCorrupted one entry that no step above touches.")
        with demo.open() as handle:
            demo.read(handle, "resolve", {"components": ["Small", "leaf"]})
            print("get-entry of another entry: succeeds")
            try:
                demo.read(handle, "diagnose", {})
                print("diagnose: UNEXPECTEDLY PASSED")
            except n.NamespaceError as error:
                print(f"diagnose: refused {error.code}")
    print("temporary data removed")


if __name__ == "__main__":
    main()
