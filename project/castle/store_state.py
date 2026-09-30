"""Closed local identity and lifecycle authority for snapshot stores."""

from __future__ import annotations

import ctypes
import json
import os
import re
import stat
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from castle.journal import CastleError

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_CAS_ID = re.compile(r"sha256:[0-9a-f]{64}")
_STATE_CODES = frozenset(
    {
        "store-retired",
        "store-identity-required",
        "store-state-invalid",
        "store-not-active",
        "store-epoch-mismatch",
        "store-identity-aliased",
        "store-preparation-failed",
    }
)
_METADATA_NAMES = {"store.json", "excision-selection.json", "retired.json"}
_TEMP_NAMES = {f".{name}.tmp" for name in _METADATA_NAMES}


class StoreStateError(CastleError):
    """A fixed, content-free lifecycle refusal."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or code not in _STATE_CODES:
            raise ValueError("store-state-invalid") from None
        self.code = code
        super().__init__(code)


def _invalid() -> None:
    raise StoreStateError("store-state-invalid") from None


def _is_uuid(value: object) -> bool:
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


@dataclass(frozen=True)
class StoreIdentity:
    store_id: str
    lineage_id: str
    epoch_id: str
    generation: int
    predecessor_epoch_id: str | None

    def __post_init__(self) -> None:
        if not all(_is_uuid(value) for value in (self.store_id, self.lineage_id, self.epoch_id)):
            _invalid()
        if type(self.generation) is not int or self.generation < 0:
            _invalid()
        if self.generation == 0:
            if self.predecessor_epoch_id is not None:
                _invalid()
        elif not _is_uuid(self.predecessor_epoch_id) or self.predecessor_epoch_id == self.epoch_id:
            _invalid()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _identity(value: object) -> StoreIdentity:
    if not isinstance(value, dict) or set(value) != {
        "store_id",
        "lineage_id",
        "epoch_id",
        "generation",
        "predecessor_epoch_id",
    }:
        _invalid()
    return StoreIdentity(**value)


def _binding(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"operation_id", "predecessor", "successor"}:
        _invalid()
    predecessor = _identity(value["predecessor"])
    successor = _identity(value["successor"])
    operation = value["operation_id"]
    if (
        not _is_uuid(operation)
        or operation
        in {
            predecessor.store_id,
            predecessor.lineage_id,
            predecessor.epoch_id,
            predecessor.predecessor_epoch_id,
            successor.store_id,
            successor.epoch_id,
        }
        or predecessor.lineage_id != successor.lineage_id
        or successor.generation != predecessor.generation + 1
        or successor.predecessor_epoch_id != predecessor.epoch_id
        or successor.epoch_id == predecessor.epoch_id
        or successor.store_id == predecessor.store_id
    ):
        _invalid()
    return value


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _validate_metadata(name: str, value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid()
    if name == "store.json":
        if set(value) != {"schema", "identity", "state", "transition", "prepared_sha256"}:
            _invalid()
        identity = _identity(value["identity"])
        phase = value["state"]
        if value["schema"] != "castle.store.v1" or phase not in ("active", "building", "prepared"):
            _invalid()
        transition = value["transition"]
        if transition is None:
            if phase != "active":
                _invalid()
        elif _identity(_binding(transition)["successor"]) != identity:
            _invalid()
        seal = value["prepared_sha256"]
        if phase == "prepared":
            if not isinstance(seal, str) or not _CAS_ID.fullmatch(seal):
                _invalid()
        elif seal is not None:
            _invalid()
    elif name == "retired.json":
        if set(value) != {"schema", "transition", "state"}:
            _invalid()
        if value["schema"] != "castle.retirement.v1" or value["state"] not in (
            "retired",
            "complete",
        ):
            _invalid()
        _binding(value["transition"])
    elif name == "excision-selection.json":
        if set(value) != {"schema", "transition", "cas_ids"}:
            _invalid()
        if value["schema"] != "castle.excision_selection.v1":
            _invalid()
        _binding(value["transition"])
        ids = value["cas_ids"]
        if not isinstance(ids, list) or not ids:
            _invalid()
        if any(not isinstance(item, str) or not _CAS_ID.fullmatch(item) for item in ids):
            _invalid()
        if ids != sorted(set(ids)):
            _invalid()
    else:
        _invalid()
    return value


def _present(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _regular(path: Path) -> os.stat_result:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        _invalid()
    return info


def _read_metadata(path: Path, *, name: str | None = None) -> dict[str, Any]:
    try:
        info = _regular(path)
        if (name or path.name) == "excision-selection.json" and stat.S_IMODE(info.st_mode) != 0o600:
            _invalid()
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        _validate_metadata(name or path.name, value)
        if raw != _canonical(value):
            _invalid()
        return value
    except (OSError, UnicodeError, ValueError, TypeError):
        raise StoreStateError("store-state-invalid") from None


def _fsync_directory(root: Path) -> None:
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_new(temporary: Path, target: Path) -> None:
    """Atomically publish a new name without overwriting or leaving a link pair."""
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameatx_np", None)
    flags = 0x00000004
    if rename is None:
        rename = getattr(library, "renameat2", None)
        flags = 1
    if rename is None:
        raise OSError("exclusive metadata publication unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if rename(
            descriptor, os.fsencode(temporary.name), descriptor, os.fsencode(target.name), flags
        ):
            raise OSError(ctypes.get_errno(), "metadata publication failed")
    finally:
        os.close(descriptor)


def _publish_metadata(
    path: Path, value: dict[str, Any], *, previous: dict[str, Any] | None = None
) -> None:
    """Publish exact validated bytes; only a validated owner may replace a target."""
    _validate_metadata(path.name, value)
    if previous is not None:
        _validate_metadata(path.name, previous)
        if path.name == "store.json" and previous["identity"] != value["identity"]:
            _invalid()
        if previous["transition"] != value["transition"]:
            _invalid()
        if path.name == "store.json" and (previous["state"], value["state"]) not in {
            ("building", "prepared"),
            ("prepared", "building"),
            ("prepared", "active"),
        }:
            _invalid()
        if path.name == "retired.json" and (previous["state"], value["state"]) != (
            "retired",
            "complete",
        ):
            _invalid()
    temporary = path.with_name(f".{path.name}.tmp")
    raw = _canonical(value)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    if _read_metadata(temporary, name=path.name) != value or temporary.read_bytes() != raw:
        _invalid()
    if previous is None:
        _rename_new(temporary, path)
    else:
        if _read_metadata(path) != previous:
            _invalid()
        os.replace(temporary, path)
    _fsync_directory(path.parent)


def _store_metadata(
    identity: StoreIdentity,
    state: str = "active",
    transition: dict[str, Any] | None = None,
    seal: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": "castle.store.v1",
        "identity": identity.to_dict(),
        "state": state,
        "transition": transition,
        "prepared_sha256": seal,
    }


def _fresh_identity() -> StoreIdentity:
    return StoreIdentity(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()), 0, None)


def _require_active(store: Path, *, allow_absent: bool = True) -> StoreIdentity | None:
    """Inspect lifecycle before content I/O, preserving absent ordinary reads."""
    store = Path(store)
    try:
        if _present(store / "retired.json"):
            raise StoreStateError("store-retired")
        if not _present(store):
            if allow_absent:
                return None
            _invalid()
        if not stat.S_ISDIR(store.lstat().st_mode):
            _invalid()
        metadata = store / "store.json"
        if not _present(metadata):
            if any(store.iterdir()):
                if any(_present(store / name) for name in _TEMP_NAMES):
                    _invalid()
                raise StoreStateError("store-identity-required")
            if allow_absent:
                return None
            raise StoreStateError("store-identity-required")
        value = _read_metadata(metadata)
        if value["state"] != "active":
            raise StoreStateError("store-not-active")
        if any(_present(store / name) for name in _TEMP_NAMES) or _present(
            store / "excision-selection.json"
        ):
            _invalid()
        return _identity(value["identity"])
    except OSError:
        raise StoreStateError("store-state-invalid") from None


def read_store_identity(store: Path) -> StoreIdentity:
    identity = _require_active(Path(store), allow_absent=False)
    assert identity is not None
    return identity


def _bootstrap(store: Path) -> None:
    from castle import journal

    store = Path(store)
    if _require_active(store) is not None:
        return
    store.mkdir(parents=True, exist_ok=True)
    with journal._stable_lock(store, create=True):
        if _present(store / "retired.json") or _present(store / "store.json"):
            _require_active(store, allow_absent=False)
            return
        if {item.name for item in store.iterdir()} != {journal.JOURNAL_LOCK_FILENAME}:
            raise StoreStateError("store-identity-required")
        (store / "objects" / "sha256").mkdir(parents=True)
        with (store / journal.JOURNAL_FILENAME).open("xb") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(store / "objects" / "sha256")
        _fsync_directory(store / "objects")
        _fsync_directory(store)
        _publish_metadata(store / "store.json", _store_metadata(_fresh_identity()))
        _fsync_directory(store.parent)


def _migration_lifecycle(store: Path) -> None:
    if _present(store / "retired.json") or _present(store / "store.json"):
        _require_active(store, allow_absent=False)
    elif any(_present(store / name) for name in _TEMP_NAMES | {"excision-selection.json"}):
        _invalid()


def prepare_store(store: Path) -> StoreIdentity:
    """Enroll valid offline v2 authority without rewriting any existing payload."""
    from castle import cas, journal

    try:
        root = Path(store)
        if not stat.S_ISDIR(root.lstat().st_mode):
            raise OSError()
        root = root.resolve(strict=True)
        _migration_lifecycle(root)
        with journal._stable_lock(root):
            _migration_lifecycle(root)
            cas._validated_authority(root)
            if _present(root / "store.json"):
                identity = read_store_identity(root)
                _fsync_directory(root)
                return identity
            identity = _fresh_identity()
            _publish_metadata(root / "store.json", _store_metadata(identity))
            return identity
    except StoreStateError:
        raise
    except (OSError, CastleError, ValueError, RuntimeError):
        raise StoreStateError("store-preparation-failed") from None


def _require_same_epoch(first: Path, second: Path) -> None:
    left, right = read_store_identity(first), read_store_identity(second)
    if left.lineage_id != right.lineage_id or left.epoch_id != right.epoch_id:
        raise StoreStateError("store-epoch-mismatch")
    if (left.generation, left.predecessor_epoch_id) != (
        right.generation,
        right.predecessor_epoch_id,
    ):
        _invalid()
    if left.store_id == right.store_id:
        raise StoreStateError("store-identity-aliased")
