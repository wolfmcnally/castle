"""Behavioral tests for APFS cloning and deterministic store merging.

Every path is derived from ``tmp_path``. Refusal assertions name a diagnostic,
and every injected seam records positive reachability before its outcome is
accepted as evidence.
"""

from __future__ import annotations

import contextlib
import gc
import hashlib
import inspect
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from castle import cas, journal, store_merge, store_state

_WAIT_SECONDS = 30.0


@pytest.fixture(autouse=True)
def _clear_store_probe_and_immutable_test_state(tmp_path: Path) -> Any:
    cas._store_requires_user_immutable.cache_clear()
    yield
    cas._store_requires_user_immutable.cache_clear()
    mask = getattr(stat, "UF_IMMUTABLE", 0)
    for directory, directories, files in os.walk(tmp_path, topdown=False, followlinks=False):
        for name in [*files, *directories]:
            path = Path(directory) / name
            try:
                info = path.lstat()
                if mask and getattr(info, "st_flags", 0) & mask and hasattr(os, "chflags"):
                    os.chflags(path, info.st_flags & ~mask, follow_symlinks=False)
                if not stat.S_ISLNK(info.st_mode):
                    path.chmod(stat.S_IMODE(path.lstat().st_mode) | stat.S_IWUSR)
            except FileNotFoundError:
                pass


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _event_id(ordinal: int) -> str:
    return f"uuid:00000000-0000-4000-8000-{ordinal:012x}"


def _record(
    content: bytes,
    ordinal: int,
    *,
    at: str | None = None,
    source_root_id: str = "source-a",
    original_relpath: str | None = None,
    event_id: str | None = None,
    media_type: str = "text/plain",
    batch: str | None = None,
    outcome: str = "stored",
) -> dict[str, Any]:
    return {
        "schema": journal.CAS_JOURNAL_EVENT_SCHEMA,
        "event_id": event_id or _event_id(ordinal),
        "at": at or f"2026-08-22T00:00:{ordinal:02d}Z",
        "event": "ingest",
        "cas_id": _digest(content),
        "size": len(content),
        "media_type": media_type,
        "source_root_id": source_root_id,
        "original_relpath": original_relpath or f"file-{ordinal}.txt",
        "batch": batch,
        "outcome": outcome,
    }


def _write_store(
    root: Path,
    records: list[dict[str, Any]],
    contents: dict[str, bytes],
    *,
    rebuild: bool = True,
) -> None:
    journal.ensure_store(root)
    with journal.journal_lock(root):
        pass
    (root / journal.JOURNAL_FILENAME).write_bytes(journal.canonical_journal_bytes(records))
    for cas_id, content in contents.items():
        destination = cas.object_path(root, cas_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(0o444)
    if rebuild:
        cas.rebuild_index(root)


def _basic_store(root: Path) -> tuple[dict[str, Any], bytes]:
    content = b"canonical payload\n"
    record = _record(content, 1, original_relpath="kept.txt")
    _write_store(root, [record], {record["cas_id"]: content})
    gc.collect()
    return record, content


def _merge_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    canonical = tmp_path / "canonical"
    clone = tmp_path / "clone"
    first_content = b"canonical payload\n"
    second_content = b"clone-only payload\n"
    first = _record(first_content, 1, original_relpath="kept.txt")
    second = _record(
        second_content,
        2,
        source_root_id="source-b",
        original_relpath="clone-only.txt",
    )
    _write_store(canonical, [first], {first["cas_id"]: first_content})
    _write_store(
        clone,
        [first, second],
        {first["cas_id"]: first_content, second["cas_id"]: second_content},
    )
    _share_epoch(canonical, clone)
    return canonical, clone, first, second


def _share_epoch(source: Path, replica: Path) -> None:
    """Explicit replica fixture; native clone has its own propagation witness."""
    from dataclasses import replace

    identity = store_state.read_store_identity(source)
    local = store_state.read_store_identity(replica)
    (replica / "store.json").write_bytes(
        store_state._canonical(
            store_state._store_metadata(replace(identity, store_id=local.store_id))
        )
    )


def _index_rows(root: Path) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    with sqlite3.connect(root / cas.INDEX_FILENAME) as connection:
        objects = connection.execute(
            "SELECT cas_id, size, media_type, stored_relpath, first_seen_utc, batch "
            "FROM objects ORDER BY cas_id"
        ).fetchall()
        names = connection.execute(
            "SELECT cas_id, source_root_id, original_relpath, recorded_utc "
            "FROM names ORDER BY cas_id, source_root_id, original_relpath"
        ).fetchall()
    return objects, names


def _remotes(root: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(root / cas.INDEX_FILENAME) as connection:
        return connection.execute(
            f"SELECT {cas._REMOTES_COLUMNS} FROM remotes ORDER BY remote"
        ).fetchall()


def _authority_snapshot(root: Path) -> tuple[bytes, dict[str, tuple[bytes, int, int]]]:
    journal_bytes = (root / journal.JOURNAL_FILENAME).read_bytes()
    objects: dict[str, tuple[bytes, int, int]] = {}
    for path in sorted((root / "objects" / "sha256").rglob("*")):
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            objects[path.relative_to(root).as_posix()] = (
                path.read_bytes(),
                stat.S_IMODE(info.st_mode),
                getattr(info, "st_flags", 0),
            )
    return journal_bytes, objects


def _exact_file_snapshot(path: Path) -> tuple[bool, bytes, int, int, int, int]:
    if not os.path.lexists(path):
        return False, b"", 0, 0, 0, 0
    info = path.lstat()
    return (
        True,
        path.read_bytes(),
        stat.S_IMODE(info.st_mode),
        getattr(info, "st_flags", 0),
        info.st_dev,
        info.st_ino,
    )


def _tree_snapshot(root: Path) -> tuple[tuple[str, int, bytes | str], ...]:
    snapshot: list[tuple[str, int, bytes | str]] = []
    candidates = [root, *sorted(root.rglob("*"), key=lambda path: os.fsencode(path.as_posix()))]
    for candidate in candidates:
        info = candidate.lstat()
        relative = "." if candidate == root else candidate.relative_to(root).as_posix()
        if stat.S_ISREG(info.st_mode):
            payload: bytes | str = candidate.read_bytes()
        elif stat.S_ISLNK(info.st_mode):
            payload = os.readlink(candidate)
        else:
            payload = b""
        snapshot.append((relative, info.st_mode, payload))
    return tuple(snapshot)


def _assert_clean_store(root: Path) -> None:
    verdict = cas.verify(root)
    assert verdict.exit_code == cas.VERIFY_OK, verdict.errors
    assert verdict.errors == ()


def _assert_complete_clone_inventory(root: Path, *, expect_wal: bool) -> None:
    expected = {
        journal.JOURNAL_FILENAME,
        "store.json",
        journal.JOURNAL_LOCK_FILENAME,
        cas.INDEX_FILENAME,
        "objects",
    }
    if expect_wal:
        expected.add(f"{cas.INDEX_FILENAME}-wal")
    actual = {entry.name for entry in root.iterdir()}
    assert actual == expected, (
        f"unexpected top-level destination entries: {sorted(actual - expected)}; "
        f"missing expected entries: {sorted(expected - actual)}"
    )


def _converged_snapshot(
    root: Path,
) -> tuple[
    tuple[bytes, dict[str, tuple[bytes, int, int]]],
    tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]],
    list[tuple[Any, ...]],
]:
    _assert_clean_store(root)
    return _authority_snapshot(root), _index_rows(root), _remotes(root)


def _child_environment() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(Path(store_merge.__file__).parents[1])}


def _publish_marker(path: Path, text: str = "ready\n") -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _await_marker(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        if path.is_file():
            return
        return_code = process.poll()
        if return_code is not None:
            stdout, stderr = process.communicate()
            pytest.fail(
                f"child exited {return_code} before {path.name}"
                f"\nstdout:\n{stdout}\nstderr:\n{stderr}"
            )
        time.sleep(0.01)
    process.terminate()
    stdout, stderr = process.communicate(timeout=5)
    pytest.fail(f"timed out waiting for {path.name}\nstdout:\n{stdout}\nstderr:\n{stderr}")


_PAUSED_MERGE_SCRIPT = r"""
import os
from pathlib import Path
import sys
import time

from castle import store_merge

canonical, clone, ready, release = map(Path, sys.argv[1:])
real_plan = store_merge._plan_merge

def publish(path, text):
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)

def paused_plan(canonical_root, clone_root):
    plan = real_plan(canonical_root, clone_root)
    publish(ready, "ready\n")
    deadline = time.monotonic() + 30
    while not release.is_file():
        if time.monotonic() >= deadline:
            raise TimeoutError("release marker did not arrive")
        time.sleep(0.01)
    return plan

store_merge._plan_merge = paused_plan
store_merge.merge_into(canonical, clone)
"""


_INGEST_CHILD_SCRIPT = r"""
import contextlib
import os
from pathlib import Path
import sys

from castle import cas

canonical, source, attempted, acquired, done = map(Path, sys.argv[1:])
real_lock = cas.journal_lock

def publish(path, text):
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)

@contextlib.contextmanager
def observed_lock(root):
    publish(attempted, f"{root}\n")
    with real_lock(root) as handle:
        publish(acquired, f"{root}\n")
        yield handle

cas.journal_lock = observed_lock
cas.ingest_file(canonical, source, "later-source")
publish(done, "done\n")
"""


_BARRIER_MERGE_SCRIPT = r"""
import contextlib
import os
from pathlib import Path
import sys
import time

from castle import store_merge

canonical, clone, requested, release = map(Path, sys.argv[1:])
real_lock = store_merge.journal._stable_lock
request_count = 0

def publish(path, text):
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)

@contextlib.contextmanager
def observed_lock(root):
    global request_count
    request_count += 1
    if request_count == 1:
        publish(requested, f"{root}\n")
        deadline = time.monotonic() + 30
        while not release.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("release marker did not arrive")
            time.sleep(0.01)
    with real_lock(root) as handle:
        yield handle

store_merge.journal._stable_lock = observed_lock
store_merge.merge_into(canonical, clone)
"""


def test_public_api_signatures_and_none_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert store_merge.__all__ == [
        "CLONE_ORDER_SCHEMA",
        "MergeError",
        "OperationName",
        "OperationRecord",
        "OperationSink",
        "clone_store",
        "merge_into",
    ]
    module_functions = {
        name
        for name, value in vars(store_merge).items()
        if not name.startswith("_")
        and inspect.isfunction(value)
        and value.__module__ == store_merge.__name__
    }
    assert module_functions == {"clone_store", "merge_into"}
    clone_signature = inspect.signature(store_merge.clone_store, eval_str=True)
    merge_signature = inspect.signature(store_merge.merge_into, eval_str=True)
    assert [
        (parameter.name, parameter.kind, parameter.default, parameter.annotation)
        for parameter in clone_signature.parameters.values()
    ] == [
        ("source", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty, Path),
        ("destination", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty, Path),
        (
            "instrument",
            inspect.Parameter.KEYWORD_ONLY,
            None,
            store_merge.OperationSink | None,
        ),
    ]
    assert clone_signature.return_annotation is None
    assert list(store_merge.OperationRecord.__dataclass_fields__) == [
        "sequence",
        "operation",
        "attribute",
    ]
    assert [
        (parameter.name, parameter.kind, parameter.default, parameter.annotation)
        for parameter in merge_signature.parameters.values()
    ] == [
        ("target", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty, Path),
        ("source", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty, Path),
    ]
    assert merge_signature.return_annotation is None
    source = tmp_path / "source"
    _basic_store(source)
    cloned = tmp_path / "cloned"
    if sys.platform == "darwin":
        assert store_merge.clone_store(source, cloned) is None
        assert store_merge.merge_into(source, cloned) is None

    target = tmp_path / "target"
    _basic_store(target)
    refused_destination = tmp_path / "refused-destination"
    reached: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        reached.append("forbidden boundary")
        pytest.fail("unsupported operation reached authority, lock, native, or output boundary")

    for malformed in (False, True):
        if malformed:
            (source / journal.JOURNAL_FILENAME).write_bytes(b"malformed journal\n")
            (target / journal.JOURNAL_FILENAME).write_bytes(b"malformed journal\n")
        before = (
            _authority_snapshot(source),
            _authority_snapshot(target),
            _tree_snapshot(tmp_path),
        )
        with monkeypatch.context() as controlled:
            controlled.setattr(store_merge.sys, "platform", "linux")
            controlled.setattr(store_merge, "_validate_store_tree", forbidden)
            controlled.setattr(journal, "read_journal", forbidden)
            controlled.setattr(cas, "read_journal", forbidden)
            controlled.setattr(journal, "_canonical_journal_lock", forbidden)
            controlled.setattr(sqlite3, "connect", forbidden)
            controlled.setattr(Path, "open", forbidden)
            controlled.setattr(os, "open", forbidden)
            controlled.setattr(store_merge.ctypes, "CDLL", forbidden)
            controlled.setattr(subprocess, "run", forbidden)
            for operation in (
                lambda: store_merge.clone_store(source, refused_destination, instrument=forbidden),
                lambda: store_merge.merge_into(target, source),
            ):
                with pytest.raises(store_merge.MergeError) as caught:
                    operation()
                assert type(caught.value) is store_merge.MergeError
                assert str(caught.value) == (
                    "store-merge-platform-unsupported: "
                    "copy-on-write store cloning requires Darwin; this is linux"
                )
        assert reached == []
        assert not refused_destination.exists()
        assert (
            _authority_snapshot(source),
            _authority_snapshot(target),
            _tree_snapshot(tmp_path),
        ) == before
    _epoch_preconditions(tmp_path / "epochs", monkeypatch)


@pytest.mark.skipif(
    sys.platform != "darwin", reason="requires successful native clone audit channels"
)
def test_clone_store_has_no_object_read_or_unapproved_child_channel(tmp_path: Path) -> None:
    import uuid

    source = tmp_path / "source"
    record, _content = _basic_store(source)
    old = store_state.read_store_identity(source)
    predecessor = store_state.StoreIdentity(
        str(uuid.uuid4()), old.lineage_id, str(uuid.uuid4()), 0, None
    )
    identity = store_state.StoreIdentity(
        old.store_id, old.lineage_id, old.epoch_id, 1, predecessor.epoch_id
    )
    binding = {
        "operation_id": str(uuid.uuid4()),
        "predecessor": predecessor.to_dict(),
        "successor": identity.to_dict(),
    }
    (source / "store.json").write_bytes(
        store_state._canonical(store_state._store_metadata(identity, "active", binding))
    )
    destination = tmp_path / "destination"
    second_destination = tmp_path / "second-destination"
    script = r"""
import ast
import inspect
import os
from pathlib import Path
import subprocess
import sys

from castle import cas, store_merge


class AuditBlocked(RuntimeError):
    pass


source, destination, second_destination = map(Path, sys.argv[1:4])
object_path = cas.object_path(source, sys.argv[4])
allowed_xattr_argv = {
    ("/usr/bin/xattr", "-w", "-x", attribute, "31", str(destination))
    for attribute in cas._SYNC_EXCLUSION_XATTRS
} | {
    ("/usr/bin/xattr", "-p", "-x", attribute, str(destination))
    for attribute in cas._SYNC_EXCLUSION_XATTRS
} | {
    ("/usr/bin/xattr", "-w", "-x", attribute, "31", str(second_destination))
    for attribute in cas._SYNC_EXCLUSION_XATTRS
} | {
    ("/usr/bin/xattr", "-p", "-x", attribute, str(second_destination))
    for attribute in cas._SYNC_EXCLUSION_XATTRS
}
blocked = []
allowed_children = []


def primitive_path(raw_path):
    if isinstance(raw_path, bytes):
        primitive_text = bytes.decode(
            raw_path,
            sys.getfilesystemencoding(),
            sys.getfilesystemencodeerrors(),
        )
    elif isinstance(raw_path, str):
        primitive_text = str.__str__(raw_path)
    elif isinstance(raw_path, os.PathLike):
        protocol_value = raw_path.__fspath__()
        if isinstance(protocol_value, bytes):
            return primitive_path(protocol_value)
        if isinstance(protocol_value, str):
            return primitive_path(protocol_value)
        return None
    else:
        return None
    assert type(primitive_text) is str
    return primitive_text


def object_leaf_channel(raw_path):
    primitive_path_value = primitive_path(raw_path)
    if primitive_path_value is None:
        return None
    parts = [
        component
        for component in str.split(primitive_path_value, os.sep)
        if component not in {"", "."}
    ]
    if not parts:
        return None
    folded_parts = [str.casefold(component) for component in parts]
    leaf = folded_parts[-1]
    if len(leaf) != 64 or any(
        character not in "0123456789abcdef" for character in leaf
    ):
        return None
    full_path_leaf = (
        len(folded_parts) >= 5
        and folded_parts[-5:-3] == ["objects", "sha256"]
        and len(folded_parts[-3]) == 2
        and len(folded_parts[-2]) == 2
        and all(
            character in "0123456789abcdef"
            for character in "".join(folded_parts[-3:-1])
        )
    )
    if full_path_leaf:
        return "full-path", primitive_path_value
    return "normalized-trailing-leaf", primitive_path_value


def process_argv(event, arguments):
    if event == "subprocess.Popen":
        return os.fsdecode(arguments[0]), tuple(os.fsdecode(value) for value in arguments[1])
    if event == "os.posix_spawn":
        return os.fsdecode(arguments[0]), tuple(os.fsdecode(value) for value in arguments[1])
    return event, ()


def audit(event, arguments):
    object_match = object_leaf_channel(arguments[0]) if event == "open" else None
    if object_match is not None:
        object_channel, primitive_path = object_match
        blocked.append((f"object-open:{object_channel}", primitive_path))
        raise AuditBlocked(f"object-read-blocked: {primitive_path!r}")
    if event in {"subprocess.Popen", "os.posix_spawn", "os.system", "os.fork"}:
        executable, argv = process_argv(event, arguments)
        if executable == "/usr/bin/xattr" and argv in allowed_xattr_argv:
            allowed_children.append((event, argv))
            return
        blocked.append(("process-launch", event, executable, argv))
        raise AuditBlocked(f"unapproved-child-blocked: {event}: {executable}: {argv}")


sys.addaudithook(audit)


def require_blocked_object_open(raw_path, *, expected_channel, dir_fd=None):
    before = len(blocked)
    expected_path = primitive_path(raw_path)
    assert expected_path is not None
    open_options = {} if dir_fd is None else {"dir_fd": dir_fd}
    try:
        descriptor = os.open(raw_path, os.O_RDONLY, **open_options)
    except AuditBlocked as exc:
        assert "object-read-blocked" in str(exc)
    else:
        os.close(descriptor)
        raise AssertionError(f"object-open positive control did not fire: {expected_path!r}")
    assert len(blocked) == before + 1
    assert blocked[-1] == (f"object-open:{expected_channel}", expected_path)


class SplitOverride(str):
    def split(self, *_args, **_kwargs):
        return ["not-an-object"]


class CasefoldOverride(str):
    def casefold(self):
        return "not-an-object"


class DecodeOverride(bytes):
    def decode(self, *_args, **_kwargs):
        return "not-an-object"


class DecodeOverridePathLike(os.PathLike):
    def __init__(self, path):
        self.path = path

    def __fspath__(self):
        return self.path


sha256_root = source / "objects" / "sha256"
first_prefix = object_path.parent.parent
leaf_prefix = object_path.parent
sha256_relative = object_path.relative_to(sha256_root).as_posix()
first_relative = object_path.relative_to(first_prefix).as_posix()
leaf_relative = object_path.name
uppercase_leaf = leaf_relative.upper()
mixed_case_leaf = "".join(
    character.upper() if index % 2 else character
    for index, character in enumerate(leaf_relative)
)
redundant_separator_relative = sha256_relative.replace("/", "//")
dot_relative = f"{object_path.parent.parent.name}/./{object_path.parent.name}/./{leaf_relative}"
traversal_relative = f"../{object_path.parent.name}/{leaf_relative}"
trailing_separator_relative = f"{leaf_relative}/"
bytes_leaf = os.fsencode(leaf_relative)
split_override_leaf = SplitOverride(leaf_relative)
casefold_override_leaf = CasefoldOverride(leaf_relative)
decode_override_leaf = DecodeOverride(bytes_leaf)
decode_override_pathlike = DecodeOverridePathLike(DecodeOverride(bytes_leaf))
assert split_override_leaf.split(os.sep) == ["not-an-object"]
assert casefold_override_leaf.casefold() == "not-an-object"
assert decode_override_leaf.decode() == "not-an-object"
assert decode_override_pathlike.__fspath__().decode() == "not-an-object"
assert primitive_path(decode_override_leaf) == leaf_relative
assert primitive_path(decode_override_pathlike) == leaf_relative
require_blocked_object_open(
    os.fspath(object_path),
    expected_channel="full-path",
)
sha256_fd = os.open(sha256_root, os.O_RDONLY | os.O_DIRECTORY)
first_prefix_fd = os.open(first_prefix, os.O_RDONLY | os.O_DIRECTORY)
leaf_prefix_fd = os.open(leaf_prefix, os.O_RDONLY | os.O_DIRECTORY)
try:
    object_inode = os.stat(
        leaf_relative,
        dir_fd=leaf_prefix_fd,
        follow_symlinks=False,
    ).st_ino
    for alias, alias_fd in (
        (uppercase_leaf, leaf_prefix_fd),
        (mixed_case_leaf, leaf_prefix_fd),
        (redundant_separator_relative, sha256_fd),
        (dot_relative, sha256_fd),
        (traversal_relative, leaf_prefix_fd),
        (bytes_leaf, leaf_prefix_fd),
        (split_override_leaf, leaf_prefix_fd),
        (casefold_override_leaf, leaf_prefix_fd),
        (decode_override_leaf, leaf_prefix_fd),
        (decode_override_pathlike, leaf_prefix_fd),
    ):
        assert os.stat(alias, dir_fd=alias_fd, follow_symlinks=False).st_ino == object_inode

    require_blocked_object_open(
        sha256_relative,
        expected_channel="normalized-trailing-leaf",
        dir_fd=sha256_fd,
    )
    require_blocked_object_open(
        first_relative,
        expected_channel="normalized-trailing-leaf",
        dir_fd=first_prefix_fd,
    )
    require_blocked_object_open(
        leaf_relative,
        expected_channel="normalized-trailing-leaf",
        dir_fd=leaf_prefix_fd,
    )
    require_blocked_object_open(
        uppercase_leaf,
        expected_channel="normalized-trailing-leaf",
        dir_fd=leaf_prefix_fd,
    )
    require_blocked_object_open(
        mixed_case_leaf,
        expected_channel="normalized-trailing-leaf",
        dir_fd=leaf_prefix_fd,
    )
    require_blocked_object_open(
        redundant_separator_relative,
        expected_channel="normalized-trailing-leaf",
        dir_fd=sha256_fd,
    )
    require_blocked_object_open(
        dot_relative,
        expected_channel="normalized-trailing-leaf",
        dir_fd=sha256_fd,
    )
    require_blocked_object_open(
        traversal_relative,
        expected_channel="normalized-trailing-leaf",
        dir_fd=leaf_prefix_fd,
    )
    require_blocked_object_open(
        trailing_separator_relative,
        expected_channel="normalized-trailing-leaf",
        dir_fd=leaf_prefix_fd,
    )
    require_blocked_object_open(
        bytes_leaf,
        expected_channel="normalized-trailing-leaf",
        dir_fd=leaf_prefix_fd,
    )
    require_blocked_object_open(
        split_override_leaf,
        expected_channel="normalized-trailing-leaf",
        dir_fd=leaf_prefix_fd,
    )
    require_blocked_object_open(
        casefold_override_leaf,
        expected_channel="normalized-trailing-leaf",
        dir_fd=leaf_prefix_fd,
    )
    require_blocked_object_open(
        decode_override_leaf,
        expected_channel="normalized-trailing-leaf",
        dir_fd=leaf_prefix_fd,
    )
    require_blocked_object_open(
        decode_override_pathlike,
        expected_channel="normalized-trailing-leaf",
        dir_fd=leaf_prefix_fd,
    )
finally:
    os.close(leaf_prefix_fd)
    os.close(first_prefix_fd)
    os.close(sha256_fd)

try:
    subprocess.run(["/usr/bin/shasum", str(object_path)], check=False)
except AuditBlocked as exc:
    assert "unapproved-child-blocked" in str(exc)
else:
    raise AssertionError("child-launch positive control did not fire")

assert [entry[0] for entry in blocked] == [
    "object-open:full-path",
    *["object-open:normalized-trailing-leaf"] * 14,
    "process-launch",
]
blocked.clear()

# This Python-audit instrument does not observe raw native syscalls made by
# in-process native code or reads made by unrelated processes outside this
# clone interpreter's process tree. It assumes an uncompromised Python
# interpreter and does not defend against a caller that monkeypatches builtins,
# replaces os functions, or otherwise subverts the runtime observing clone_store.
residual_blind_spots = {
    "raw native syscalls issued by in-process native code",
    "reads by unrelated processes outside the clone interpreter process tree",
    "runtime subversion through monkeypatched builtins or replaced os functions",
}
assert len(residual_blind_spots) == 3

native_tree = ast.parse(inspect.getsource(store_merge._DarwinClonefile))
library_bindings = {
    node.attr
    for node in ast.walk(native_tree)
    if isinstance(node, ast.Attribute)
    and isinstance(node.value, ast.Name)
    and node.value.id == "library"
}
assert library_bindings == {"clonefileat", "renameatx_np"}


def forbidden_seam(name):
    def refuse(*_args, **_kwargs):
        raise AssertionError(f"forbidden fidelity seam reached: {name}")
    return refuse


store_merge._files_equal = forbidden_seam("_files_equal")
cas.verify = forbidden_seam("cas.verify")
store_merge.clone_store(source, destination)
assert blocked == []
assert len(allowed_children) == 4

cas.sha256_file = forbidden_seam("cas.sha256_file")
store_merge.clone_store(source, second_destination)
assert blocked == []
assert len(allowed_children) == 8
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(source),
            str(destination),
            str(second_destination),
            record["cas_id"],
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_child_environment(),
    )
    assert completed.returncode == 0, (
        f"clone read/process instrument failed\nstdout:\n{completed.stdout}"
        f"\nstderr:\n{completed.stderr}"
    )
    identities = [identity.store_id]
    for root in (destination, second_destination):
        cloned = store_state.read_store_identity(root)
        identities.append(cloned.store_id)
        assert (
            cloned.lineage_id,
            cloned.epoch_id,
            cloned.generation,
            cloned.predecessor_epoch_id,
        ) == (
            identity.lineage_id,
            identity.epoch_id,
            identity.generation,
            identity.predecessor_epoch_id,
        )
        metadata = store_state._read_metadata(root / "store.json")
        assert metadata["transition"] is None and metadata["prepared_sha256"] is None
        assert not {
            "retired.json",
            "excision-selection.json",
            ".store.json.tmp",
            ".retired.json.tmp",
            ".excision-selection.json.tmp",
        }.intersection(p.name for p in root.iterdir())
        _assert_complete_clone_inventory(root, expect_wal=(root / "cas.sqlite-wal").exists())
    assert len(set(identities)) == 3


@pytest.mark.parametrize("write_target", ["clone", "source"])
@pytest.mark.skipif(sys.platform != "darwin", reason="requires native copy-on-write independence")
def test_clone_store_database_and_journal_are_copy_on_write_independent(
    tmp_path: Path,
    write_target: str,
) -> None:
    source = tmp_path / "source"
    _basic_store(source)
    clone = tmp_path / "clone"
    store_merge.clone_store(source, clone)
    roots = {"source": source, "clone": clone}
    target = roots[write_target]
    preserved = roots["source" if write_target == "clone" else "clone"]
    preserved_before = {
        journal.JOURNAL_FILENAME: _exact_file_snapshot(preserved / journal.JOURNAL_FILENAME),
        cas.INDEX_FILENAME: _exact_file_snapshot(preserved / cas.INDEX_FILENAME),
    }
    target_before = {
        journal.JOURNAL_FILENAME: _exact_file_snapshot(target / journal.JOURNAL_FILENAME),
        cas.INDEX_FILENAME: _exact_file_snapshot(target / cas.INDEX_FILENAME),
    }
    incoming = tmp_path / f"incoming-{write_target}.txt"
    incoming.write_bytes(f"new bytes for {write_target}\n".encode())

    cas.ingest_file(target, incoming, f"{write_target}-writer")

    assert {
        journal.JOURNAL_FILENAME: _exact_file_snapshot(preserved / journal.JOURNAL_FILENAME),
        cas.INDEX_FILENAME: _exact_file_snapshot(preserved / cas.INDEX_FILENAME),
    } == preserved_before
    assert {
        journal.JOURNAL_FILENAME: _exact_file_snapshot(target / journal.JOURNAL_FILENAME),
        cas.INDEX_FILENAME: _exact_file_snapshot(target / cas.INDEX_FILENAME),
    } != target_before
    assert len(journal.read_journal(target)) == 2
    assert len(journal.read_journal(preserved)) == 1


def test_clone_store_refuses_native_failure_without_copying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    _basic_store(source)
    destination = tmp_path / "destination"
    before = _authority_snapshot(source)
    reached: list[str] = []

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        reached.append("clonefileat")
        raise store_merge._merge_owned_error(
            "store-merge-clonefile-failed",
            "injected native refusal; there is no byte-copy fallback",
        )

    monkeypatch.setattr(store_merge._DarwinClonefile, "clonefileat", refuse)
    if sys.platform != "darwin":
        # Only the injected native-call failure is under test in this lane.
        monkeypatch.setattr(store_merge, "_require_supported_platform", lambda: None)
        monkeypatch.setattr(store_merge._DarwinClonefile, "__init__", lambda _self: None)
        monkeypatch.setattr(store_merge, "_apply_and_verify_exclusions", lambda *_a, **_k: None)
    monkeypatch.setattr(cas, "_clone_file", lambda *_a, **_k: pytest.fail("cas clone fallback"))
    monkeypatch.setattr(cas, "_stage_object", lambda *_a, **_k: pytest.fail("cas staging"))
    monkeypatch.setattr(shutil, "copyfile", lambda *_a, **_k: pytest.fail("byte copy"))
    with pytest.raises(journal.CastleError, match="store-merge-clonefile-failed"):
        store_merge.clone_store(source, destination)
    assert reached == ["clonefileat"]
    assert not destination.exists()
    assert _authority_snapshot(source) == before


def test_clone_store_resolves_source_and_destination_parent_once_at_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    source = real_parent / "source"
    _basic_store(source)
    via_alias = tmp_path / "via-alias"
    via_alias.symlink_to(real_parent, target_is_directory=True)
    source_argument = via_alias / "source"
    destination = via_alias / "destination"
    real_resolve = Path.resolve
    calls: list[tuple[Path, bool]] = []

    def observed_resolve(path: Path, strict: bool = False) -> Path:
        calls.append((path, strict))
        return real_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", observed_resolve)
    if sys.platform == "darwin":
        store_merge.clone_store(source_argument, destination)
    else:
        with pytest.raises(store_merge.MergeError) as caught:
            store_merge.clone_store(source_argument, destination)
        assert str(caught.value) == (
            "store-merge-platform-unsupported: "
            f"copy-on-write store cloning requires Darwin; this is {sys.platform}"
        )
        assert not destination.exists()

    assert calls == [
        (source_argument, True),
        (destination.parent, True),
    ]


@pytest.mark.parametrize("unsafe", ["symlink", "fifo", "socket", "device", "staging"])
def test_clone_store_refuses_unsafe_tree_shapes_without_following(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe: str,
) -> None:
    if sys.platform != "darwin":
        monkeypatch.setattr(store_merge, "_require_supported_platform", lambda: None)
    source = tmp_path / "source"
    _basic_store(source)
    placed = source / "unsafe"
    if unsafe == "symlink":
        placed.symlink_to(tmp_path / "outside")
    elif unsafe == "fifo":
        os.mkfifo(placed)
    elif unsafe in {"socket", "device"}:
        placed.write_bytes(f"{unsafe}-shape stand-in".encode())
        real_entry_stat = store_merge._entry_stat

        def special_stat(entry: os.DirEntry[str], relative: str) -> os.stat_result:
            if Path(entry.path) == placed:
                kind = stat.S_IFSOCK if unsafe == "socket" else stat.S_IFCHR
                return os.stat_result((kind | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return real_entry_stat(entry, relative)

        monkeypatch.setattr(store_merge, "_entry_stat", special_stat)
    else:
        placed = source / ".cas-0123456789abcdef0123456789abcdef.tmp"
        placed.write_bytes(b"residue")
    with pytest.raises(journal.CastleError, match="store-merge-(?:tree-unsafe|transient)"):
        store_merge.clone_store(source, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


@pytest.mark.parametrize("authority", ["journal", "object"])
def test_clone_store_refuses_hardlinked_authority_without_mutation(
    tmp_path: Path,
    authority: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if sys.platform != "darwin":
        monkeypatch.setattr(store_merge, "_require_supported_platform", lambda: None)
    source = tmp_path / "source"
    record, _content = _basic_store(source)
    authority_path = (
        source / journal.JOURNAL_FILENAME
        if authority == "journal"
        else cas.object_path(source, record["cas_id"])
    )
    external = tmp_path / f"outside-{authority}"
    os.link(authority_path, external)
    before = _authority_snapshot(source)
    with pytest.raises(journal.CastleError, match="store-merge-authority-aliased"):
        store_merge.clone_store(source, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()
    assert authority_path.stat().st_ino == external.stat().st_ino
    assert _authority_snapshot(source) == before


def test_merge_into_refuses_alias_obscured_nested_root_overlap_before_lock_or_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outer = tmp_path / "outer-store"
    inner = outer / "inner-store"
    _basic_store(outer)
    _basic_store(inner)
    outer_anchor = tmp_path / "outer-anchor"
    inner_anchor = outer / "inner-anchor"
    outer_anchor.mkdir()
    inner_anchor.mkdir()
    outer_alias = tmp_path / "outer-alias"
    inner_alias = tmp_path / "inner-alias"
    outer_alias.symlink_to(outer_anchor, target_is_directory=True)
    inner_alias.symlink_to(inner_anchor, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    outer_argument = Path(outer_alias.name) / ".." / outer.name
    inner_argument = Path(inner_alias.name) / ".." / inner.name
    outer_spelling = tmp_path / outer_argument
    inner_spelling = tmp_path / inner_argument
    assert not outer_argument.is_relative_to(inner_argument)
    assert not inner_argument.is_relative_to(outer_argument)
    assert inner.is_relative_to(outer)
    before = {
        outer: _authority_snapshot(outer),
        inner: _authority_snapshot(inner),
    }
    lock_entries: list[Path] = []
    real_resolve = Path.resolve
    resolve_calls: list[tuple[Path, bool]] = []

    def observed_resolve(path: Path, strict: bool = False) -> Path:
        resolve_calls.append((path, strict))
        return real_resolve(path, strict=strict)

    @contextlib.contextmanager
    def forbidden_lock(root: Path) -> Any:
        lock_entries.append(root)
        pytest.fail("nested merge overlap reached the stable-lock boundary")
        yield

    monkeypatch.setattr(Path, "resolve", observed_resolve)
    monkeypatch.setattr(journal, "_canonical_journal_lock", forbidden_lock)
    for target, source in (
        (outer_argument, inner_argument),
        (inner_argument, outer_argument),
    ):
        with pytest.raises(journal.CastleError, match="^store-merge-root-overlap:"):
            store_merge.merge_into(target, source)

    assert resolve_calls == [
        (outer_spelling, True),
        (inner_spelling, True),
        (inner_spelling, True),
        (outer_spelling, True),
    ]
    assert lock_entries == []
    assert _authority_snapshot(outer) == before[outer]
    assert _authority_snapshot(inner) == before[inner]


@pytest.mark.skipif(sys.platform != "darwin", reason="requires native merge object publication")
def test_merge_into_unions_disjoint_objects(tmp_path: Path) -> None:
    canonical, clone, first, second = _merge_fixture(tmp_path)
    assert store_merge.merge_into(canonical, clone) is None
    assert journal.read_journal(canonical) == [first, second]
    assert cas.object_path(canonical, first["cas_id"]).read_bytes() == b"canonical payload\n"
    assert cas.object_path(canonical, second["cas_id"]).read_bytes() == b"clone-only payload\n"
    _assert_clean_store(canonical)

    before = (_authority_snapshot(canonical), _index_rows(canonical), _remotes(canonical))
    assert store_merge.merge_into(canonical, clone) is None
    after = (_authority_snapshot(canonical), _index_rows(canonical), _remotes(canonical))
    assert after == before
    _assert_clean_store(canonical)


def test_journal_union_is_commutative_associative_and_deterministic() -> None:
    first = _record(b"alpha", 1, at="2026-08-22T00:00:00Z")
    second = _record(b"bravo", 2, at="2026-08-22T00:00:00.000001Z")
    third = _record(b"charlie", 3, at="2026-08-22T02:00:00Z")
    assert first["at"] > second["at"]
    assert journal.journal_sort_key(first) < journal.journal_sort_key(second)
    left = store_merge._merge_records([first], [second])
    right = store_merge._merge_records([second], [first])
    assert left == right
    assert left == (first, second)
    assert store_merge._merge_records(left, [third]) == store_merge._merge_records(
        [first],
        store_merge._merge_records([second], [third]),
    )
    assert store_merge._merge_records(left, left) == left
    assert list(left) == sorted(left, key=journal.journal_sort_key)


def test_merge_into_rejects_controlled_digest_collision_at_byte_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = tmp_path / "canonical"
    clone = tmp_path / "clone"
    claimed_content = b"equal-size-A"
    other_content = b"equal-size-B"
    assert len(claimed_content) == len(other_content)
    record = _record(claimed_content, 1)
    claimed_id = record["cas_id"]
    _write_store(canonical, [record], {claimed_id: claimed_content})
    _write_store(clone, [record], {claimed_id: claimed_content})
    _share_epoch(canonical, clone)
    clone_object = cas.object_path(clone, claimed_id)
    clone_object.chmod(0o644)
    clone_object.write_bytes(other_content)
    clone_object.chmod(0o444)
    canonical_object = cas.object_path(canonical, claimed_id)
    real_hash = cas.sha256_file
    real_equal = store_merge._files_equal
    observations: list[bool] = []

    def collision_hash(path: Path) -> str:
        if Path(path) in {canonical_object, clone_object}:
            return claimed_id
        return real_hash(path)

    def observed(left: Path, right: Path) -> bool:
        result = real_equal(left, right)
        observations.append(result)
        return result

    before = _authority_snapshot(canonical)
    monkeypatch.setattr(cas, "sha256_file", collision_hash)
    monkeypatch.setattr(store_merge, "_files_equal", observed)
    if sys.platform != "darwin":
        monkeypatch.setattr(store_merge._DarwinClonefile, "__init__", lambda _self: None)
    with pytest.raises(journal.CastleError, match="store-merge-object-byte-conflict"):
        store_merge.merge_into(canonical, clone)
    assert observations == [False]
    assert _authority_snapshot(canonical) == before


@pytest.mark.skipif(
    sys.platform != "darwin", reason="requires native merge interruption and resumption"
)
def test_merge_into_resumes_after_journal_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical, clone, first, second = _merge_fixture(tmp_path)
    uninterrupted = tmp_path / "uninterrupted"
    reference_clone = tmp_path / "reference-clone"
    _write_store(
        uninterrupted,
        [first],
        {first["cas_id"]: cas.object_path(canonical, first["cas_id"]).read_bytes()},
    )
    _write_store(
        reference_clone,
        [first, second],
        {
            first["cas_id"]: cas.object_path(clone, first["cas_id"]).read_bytes(),
            second["cas_id"]: cas.object_path(clone, second["cas_id"]).read_bytes(),
        },
    )
    _share_epoch(uninterrupted, reference_clone)
    store_merge.merge_into(uninterrupted, reference_clone)
    expected = _converged_snapshot(uninterrupted)
    target = journal.canonical_journal_bytes([first, second])
    real_fsync_directory = store_merge._fsync_directory
    interrupted: list[Path] = []

    def one_shot(directory: Path) -> None:
        if (
            directory == canonical
            and not interrupted
            and (canonical / journal.JOURNAL_FILENAME).read_bytes() == target
        ):
            interrupted.append(directory)
            raise OSError("injected after journal replace before directory fsync")
        real_fsync_directory(directory)

    monkeypatch.setattr(store_merge, "_fsync_directory", one_shot)
    with pytest.raises(OSError, match="injected after journal replace before directory fsync"):
        store_merge.merge_into(canonical, clone)
    assert interrupted == [canonical]
    assert journal.read_journal(canonical) == [first, second]
    repair_fsyncs: list[Path] = []

    def observed_repair(directory: Path) -> None:
        repair_fsyncs.append(directory)
        real_fsync_directory(directory)

    monkeypatch.setattr(store_merge, "_fsync_directory", observed_repair)
    store_merge.merge_into(canonical, clone)
    assert canonical in repair_fsyncs
    assert _converged_snapshot(canonical) == expected
    assert journal.read_journal(canonical) == [first, second]


@pytest.mark.skipif(sys.platform != "darwin", reason="requires concurrent successful native merges")
def test_opposite_direction_merges_cannot_deadlock(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_content = b"first\n"
    second_content = b"second\n"
    first = _record(first_content, 1)
    second = _record(second_content, 2)
    _write_store(first_root, [first], {first["cas_id"]: first_content})
    _write_store(second_root, [second], {second["cas_id"]: second_content})
    _share_epoch(first_root, second_root)
    release = tmp_path / "opposite-release"
    requested_paths = [tmp_path / "first-requested", tmp_path / "second-requested"]
    arguments = [
        (first_root, second_root, requested_paths[0]),
        (second_root, first_root, requested_paths[1]),
    ]
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _BARRIER_MERGE_SCRIPT,
                str(canonical),
                str(clone),
                str(ready),
                str(release),
            ],
            env=_child_environment(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for canonical, clone, ready in arguments
    ]
    try:
        for requested, process in zip(requested_paths, processes, strict=True):
            _await_marker(requested, process)
        expected_first = min((first_root, second_root), key=lambda path: os.fsencode(path))
        requested_roots = [
            Path(path.read_text(encoding="utf-8").strip()) for path in requested_paths
        ]
        assert requested_roots == [expected_first, expected_first]
        _publish_marker(release)
        diagnostics: list[str] = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=_WAIT_SECONDS)
            diagnostics.append(f"rc={process.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}")
        assert [process.returncode for process in processes] == [0, 0], "\n".join(diagnostics)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
    assert journal.read_journal(first_root) == [first, second]
    assert journal.read_journal(second_root) == [first, second]
    _assert_clean_store(first_root)
    _assert_clean_store(second_root)


_IMPORT_SCRIPT = r"""
import sys

import castle
from castle import store_merge

assert "castle.journal" in sys.modules
assert "castle.cas" in sys.modules
assert "castle.store_merge" in sys.modules
assert "castle.journal_migrate_v1" not in sys.modules
assert "castle.cli" not in sys.modules
assert store_merge.CLONE_ORDER_SCHEMA == "clone-order.v1"
"""


def _epoch_preconditions(parent: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uuid
    from dataclasses import replace

    for kind, code in (
        ("lineage", "store-epoch-mismatch"),
        ("epoch", "store-epoch-mismatch"),
        ("equal-generation", "store-epoch-mismatch"),
        ("contradictory", "store-state-invalid"),
        ("aliased", "store-identity-aliased"),
        ("retired", "store-retired"),
        ("prepared", "store-not-active"),
    ):
        first, second, _a, _b = _merge_fixture(parent / kind)
        identity = store_state.read_store_identity(second)
        if kind == "lineage":
            identity = replace(identity, lineage_id=str(uuid.uuid4()))
        elif kind in ("epoch", "equal-generation"):
            identity = replace(identity, epoch_id=str(uuid.uuid4()))
        elif kind == "contradictory":
            identity = replace(identity, generation=1, predecessor_epoch_id=str(uuid.uuid4()))
        elif kind == "aliased":
            identity = replace(identity, store_id=store_state.read_store_identity(first).store_id)
        elif kind == "retired":
            (second / "retired.json").write_bytes(b"retirement fences even when malformed")
        if kind == "prepared":
            predecessor = store_state.read_store_identity(first)
            identity = replace(
                identity,
                generation=1,
                epoch_id=str(uuid.uuid4()),
                predecessor_epoch_id=predecessor.epoch_id,
            )
            binding = {
                "operation_id": str(uuid.uuid4()),
                "predecessor": predecessor.to_dict(),
                "successor": identity.to_dict(),
            }
            updated = store_state._store_metadata(
                identity, "prepared", binding, "sha256:" + "a" * 64
            )
        else:
            updated = store_state._store_metadata(identity)
        (second / "store.json").write_bytes(store_state._canonical(updated))
        before = (_tree_snapshot(first), _tree_snapshot(second))
        reached = []

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            reached.append("authority")
            pytest.fail("epoch refusal occurred after content admission")

        with monkeypatch.context() as controlled:
            controlled.setattr(store_merge._DarwinClonefile, "__init__", lambda _self: None)
            controlled.setattr(store_merge, "_plan_merge", forbidden)
            controlled.setattr(cas, "sha256_file", forbidden)
            controlled.setattr(journal, "read_journal", forbidden)
            for target, source in ((first, second), (second, first)):
                with pytest.raises(store_state.StoreStateError, match=f"^{code}$"):
                    store_merge.merge_into(target, source)
        assert reached == []
        assert (_tree_snapshot(first), _tree_snapshot(second)) == before
