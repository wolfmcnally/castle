"""APFS copy-on-write store cloning and deterministic merging.

Cloning carries the disposable SQLite index and WAL so the result is immediately
usable. Merge still treats source-side SQLite state as disposable and preserves
only target remotes through the core rebuild.

There is deliberately no byte-copy fallback. Both clone and publication use
Darwin's descriptor-anchored native primitives with no-follow and
resolve-beneath flags.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import re
import stat
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from castle import cas, journal, store_state

__all__ = [
    "CLONE_ORDER_SCHEMA",
    "MergeError",
    "OperationName",
    "OperationRecord",
    "OperationSink",
    "clone_store",
    "merge_into",
]

CLONE_ORDER_SCHEMA = "clone-order.v1"

type OperationName = Literal[
    "destination-root-created",
    "exclusion-applied",
    "exclusion-verified",
    "clonefile",
    "clone-complete",
]

CLONE_NOFOLLOW_ANY = 0x0008
CLONE_RESOLVE_BENEATH = 0x0010
RENAME_EXCL = 0x00000004
RENAME_NOFOLLOW_ANY = 0x00000010
RENAME_RESOLVE_BENEATH = 0x00000020

_OPERATIONS = frozenset(
    {
        "destination-root-created",
        "exclusion-applied",
        "exclusion-verified",
        "clonefile",
        "clone-complete",
    }
)
_DISPOSABLE_INDEX_NAMES = (
    cas.INDEX_FILENAME,
    f"{cas.INDEX_FILENAME}-wal",
    f"{cas.INDEX_FILENAME}-shm",
)
_HEX_PAIR = re.compile(r"^[0-9a-f]{2}$")
_OBJECT_NAME = re.compile(r"^[0-9a-f]{64}$")
_TRANSIENT_NAME = re.compile(
    r"^(?:\.cas-[0-9a-f]{32}\.(?:tmp|sqlite)(?:-(?:wal|shm))?"
    r"|\.cas-merge-[0-9a-f]{32}\.tmp"
    r"|\.cas-merge-journal\.[0-9a-f]{32}\.tmp)$"
)
_READ_ONLY_MODE = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH


class MergeError(journal.CastleError):
    """A clone or merge operation could not proceed safely."""


def _merge_owned_error(code: str, detail: str) -> journal.CastleError:
    return MergeError(f"{code}: {detail}")


@dataclass(frozen=True)
class OperationRecord:
    """One content-free record emitted after a clone operation completes."""

    sequence: int
    operation: OperationName
    attribute: str | None = None

    def as_mapping(self) -> dict[str, Any]:
        mapping: dict[str, Any] = {
            "sequence": self.sequence,
            "operation": self.operation,
        }
        if self.attribute is not None:
            mapping["attribute"] = self.attribute
        return mapping


type OperationSink = Callable[[OperationRecord], None]


def _require_supported_platform() -> None:
    if sys.platform != "darwin":
        raise _merge_owned_error(
            "store-merge-platform-unsupported",
            f"copy-on-write store cloning requires Darwin; this is {sys.platform}",
        )


class _DarwinClonefile:
    """Direct bindings for hardened clone and exclusive publication calls."""

    def __init__(self) -> None:
        _require_supported_platform()
        library = ctypes.CDLL(None, use_errno=True)
        for symbol in ("clonefileat", "renameatx_np"):
            if not hasattr(library, symbol):
                raise _merge_owned_error(
                    "store-merge-symbol-missing",
                    f"the installed C library exposes no {symbol}",
                )
        self._clonefileat = library.clonefileat
        self._clonefileat.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        self._clonefileat.restype = ctypes.c_int
        self._renameatx_np = library.renameatx_np
        self._renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        self._renameatx_np.restype = ctypes.c_int

    def clonefileat(
        self,
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        ctypes.set_errno(0)
        result = self._clonefileat(
            source_dir_fd,
            os.fsencode(source_name),
            destination_dir_fd,
            os.fsencode(destination_name),
            CLONE_NOFOLLOW_ANY | CLONE_RESOLVE_BENEATH,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            raise _merge_owned_error(
                "store-merge-clonefile-failed",
                "clonefileat refused with "
                f"{errno.errorcode.get(error_number, error_number)}; "
                "there is no byte-copy fallback",
            )

    def rename_exclusive(
        self,
        source_dir_fd: int,
        source_name: str,
        destination_dir_fd: int,
        destination_name: str,
    ) -> None:
        ctypes.set_errno(0)
        result = self._renameatx_np(
            source_dir_fd,
            os.fsencode(source_name),
            destination_dir_fd,
            os.fsencode(destination_name),
            RENAME_EXCL | RENAME_NOFOLLOW_ANY | RENAME_RESOLVE_BENEATH,
        )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise FileExistsError(error_number, "destination already exists")
            raise _merge_owned_error(
                "store-merge-publication-failed",
                f"renameatx_np refused with {errno.errorcode.get(error_number, error_number)}",
            )


@dataclass(frozen=True)
class _StoreInventory:
    root: Path
    records: tuple[dict[str, Any], ...]
    objects: dict[str, Path]
    disposable: tuple[Path, ...]


@dataclass(frozen=True)
class _ObjectSource:
    cas_id: str
    source: Path


@dataclass(frozen=True)
class _MergePlan:
    records: tuple[dict[str, Any], ...]
    journal_bytes: bytes
    objects_to_publish: tuple[_ObjectSource, ...]
    target_cas_ids: tuple[str, ...]


@dataclass(frozen=True)
class _EntryIdentity:
    device: int
    inode: int


@dataclass(frozen=True)
class _OwnedClone:
    parent_fd: int
    root_fd: int
    name: str
    identity: _EntryIdentity


def _record_operation(
    records: list[OperationRecord],
    instrument: OperationSink | None,
    operation: OperationName,
    *,
    attribute: str | None = None,
) -> None:
    if operation not in _OPERATIONS:
        raise _merge_owned_error(
            "store-merge-instrumentation-vocabulary",
            f"{operation!r} is not a clone operation",
        )
    if operation == "clonefile":
        if attribute is not None:
            raise _merge_owned_error(
                "store-merge-instrumentation-shape",
                "clonefile accepts no optional fields",
            )
    elif operation in {"exclusion-applied", "exclusion-verified"}:
        if attribute not in cas._SYNC_EXCLUSION_XATTRS:
            raise _merge_owned_error(
                "store-merge-instrumentation-shape",
                "an exclusion event requires only a declared attribute",
            )
    elif attribute is not None:
        raise _merge_owned_error(
            "store-merge-instrumentation-shape",
            f"{operation} accepts no optional fields",
        )
    record = OperationRecord(
        sequence=len(records) + 1,
        operation=operation,
        attribute=attribute,
    )
    records.append(record)
    if instrument is not None:
        instrument(record)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _absolute_spelling(path: Path) -> Path:
    if path.is_absolute():
        return path
    return Path.cwd() / path


def _require_real_store_root(path: Path, label: str) -> Path:
    absolute = _absolute_spelling(path)
    try:
        info = absolute.lstat()
    except OSError as exc:
        raise _merge_owned_error(
            "store-merge-root-unusable",
            f"{label} is not an existing directory: {exc}",
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise _merge_owned_error(
            "store-merge-path-symlink",
            f"{label} is a symlink",
        )
    if not stat.S_ISDIR(info.st_mode):
        raise _merge_owned_error(
            "store-merge-root-unusable",
            f"{label} is not an existing directory",
        )
    return absolute


def _canonical_store_root(path: Path, label: str) -> Path:
    root = _require_real_store_root(path, label)
    try:
        canonical = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _merge_owned_error(
            "store-merge-root-unusable",
            f"{label} cannot be canonicalized: {exc}",
        ) from exc
    return _require_real_store_root(canonical, label)


def _require_safe_lock(root: Path, label: str) -> None:
    lock_path = root / journal.JOURNAL_LOCK_FILENAME
    if not journal.journal_lock_is_safe(lock_path):
        raise _merge_owned_error(
            "store-merge-lock-unsafe",
            f"{label} has no safe inert journal lock",
        )
    try:
        links = lock_path.lstat().st_nlink
    except OSError as exc:
        raise _merge_owned_error(
            "store-merge-lock-unsafe",
            f"{label} journal lock cannot be inspected: {exc}",
        ) from exc
    if links != 1:
        raise _merge_owned_error(
            "store-merge-lock-aliased",
            f"{label} journal lock has {links} directory entries",
        )


def _sorted_entries(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            return sorted(entries, key=lambda entry: os.fsencode(entry.name))
    except OSError as exc:
        raise _merge_owned_error(
            "store-merge-tree-unreadable",
            f"cannot enumerate {directory.name or 'store root'}: {exc}",
        ) from exc


def _entry_stat(entry: os.DirEntry[str], relative: str) -> os.stat_result:
    try:
        return entry.stat(follow_symlinks=False)
    except OSError as exc:
        raise _merge_owned_error(
            "store-merge-tree-unreadable",
            f"cannot inspect {relative}: {exc}",
        ) from exc


def _raise_unsafe_entry(relative: str, detail: str) -> None:
    if _TRANSIENT_NAME.fullmatch(Path(relative).name):
        raise _merge_owned_error(
            "store-merge-transient-artifact",
            f"the store contains staging residue at {relative}",
        )
    raise _merge_owned_error(
        "store-merge-tree-unsafe",
        f"the store contains {detail} at {relative}",
    )


def _inventory_objects(
    root: Path,
    *,
    verify_object_content: bool,
) -> dict[str, Path]:
    objects_root = root / "objects"
    sha_root = objects_root / "sha256"
    try:
        objects_info = objects_root.lstat()
        sha_info = sha_root.lstat()
    except OSError as exc:
        raise _merge_owned_error(
            "store-merge-object-layout",
            f"the store has no complete objects/sha256 layout: {exc}",
        ) from exc
    if not stat.S_ISDIR(objects_info.st_mode) or not stat.S_ISDIR(sha_info.st_mode):
        raise _merge_owned_error(
            "store-merge-object-layout",
            "the store objects/sha256 layout is not made of real directories",
        )
    for entry in _sorted_entries(objects_root):
        if entry.name != "sha256":
            _raise_unsafe_entry(f"objects/{entry.name}", "an unsupported entry")

    found: dict[str, Path] = {}
    for first in _sorted_entries(sha_root):
        first_relative = f"objects/sha256/{first.name}"
        first_info = _entry_stat(first, first_relative)
        if not stat.S_ISDIR(first_info.st_mode) or not _HEX_PAIR.fullmatch(first.name):
            _raise_unsafe_entry(first_relative, "an invalid object-prefix entry")
        first_path = Path(first.path)
        for second in _sorted_entries(first_path):
            second_relative = f"{first_relative}/{second.name}"
            second_info = _entry_stat(second, second_relative)
            if not stat.S_ISDIR(second_info.st_mode) or not _HEX_PAIR.fullmatch(second.name):
                _raise_unsafe_entry(second_relative, "an invalid object-prefix entry")
            second_path = Path(second.path)
            for item in _sorted_entries(second_path):
                relative = f"{second_relative}/{item.name}"
                info = _entry_stat(item, relative)
                if not stat.S_ISREG(info.st_mode) or not _OBJECT_NAME.fullmatch(item.name):
                    _raise_unsafe_entry(relative, "an invalid object entry")
                if item.name[:2] != first.name or item.name[2:4] != second.name:
                    _raise_unsafe_entry(relative, "a misplaced object")
                if info.st_nlink != 1:
                    raise _merge_owned_error(
                        "store-merge-authority-aliased",
                        f"the object at {relative} has {info.st_nlink} directory entries",
                    )
                cas_id = f"sha256:{item.name}"
                candidate = Path(item.path)
                if verify_object_content:
                    try:
                        actual = cas.sha256_file(candidate)
                    except OSError as exc:
                        raise _merge_owned_error(
                            "store-merge-object-unreadable",
                            f"the object at {relative} cannot be hashed: {exc}",
                        ) from exc
                    if actual != cas_id:
                        raise _merge_owned_error(
                            "store-merge-object-identity",
                            f"the object at {relative} does not match {cas_id}",
                        )
                if not cas._write_protected(info):
                    raise _merge_owned_error(
                        "store-merge-object-unsealed",
                        f"the object at {relative} is writable",
                    )
                found[cas_id] = candidate
    return found


def _validate_store_tree(
    root: Path,
    label: str,
    *,
    verify_object_content: bool,
) -> _StoreInventory:
    """Inventory one store without following links or touching derived state."""
    root = _require_real_store_root(root, label)
    _require_safe_lock(root, label)
    store_state.read_store_identity(root)
    journal_path = root / journal.JOURNAL_FILENAME
    try:
        journal_info = journal_path.lstat()
    except OSError as exc:
        raise _merge_owned_error(
            "store-merge-journal-absent",
            f"{label} has no authoritative journal: {exc}",
        ) from exc
    if not stat.S_ISREG(journal_info.st_mode):
        raise _merge_owned_error(
            "store-merge-journal-unsafe",
            f"{label} journal is not a regular file",
        )
    if journal_info.st_nlink != 1:
        raise _merge_owned_error(
            "store-merge-authority-aliased",
            f"{label} journal has {journal_info.st_nlink} directory entries",
        )

    allowed = {
        "objects",
        "store.json",
        journal.JOURNAL_FILENAME,
        journal.JOURNAL_LOCK_FILENAME,
        *_DISPOSABLE_INDEX_NAMES,
    }
    disposable: list[Path] = []
    for entry in _sorted_entries(root):
        relative = entry.name
        info = _entry_stat(entry, relative)
        if entry.name not in allowed:
            _raise_unsafe_entry(relative, "an unsupported root entry")
        if entry.name in _DISPOSABLE_INDEX_NAMES:
            if not stat.S_ISREG(info.st_mode):
                _raise_unsafe_entry(relative, "a non-regular disposable index entry")
            disposable.append(Path(entry.path))
        elif entry.name == "objects" and not stat.S_ISDIR(info.st_mode):
            _raise_unsafe_entry(relative, "a non-directory objects entry")
        elif entry.name in {journal.JOURNAL_FILENAME, journal.JOURNAL_LOCK_FILENAME} and not (
            stat.S_ISREG(info.st_mode)
        ):
            _raise_unsafe_entry(relative, "a non-regular authority entry")

    records = tuple(journal.read_journal(root))
    objects = _inventory_objects(
        root,
        verify_object_content=verify_object_content,
    )
    return _StoreInventory(root, records, objects, tuple(disposable))


def _assert_rebuild_state_unaliased(target: Path) -> None:
    """Require every present target rebuild input to be regular and single-link.

    This is a point-in-time gate. The stable journal lock excludes cooperating
    Castle writers; it does not prevent a writer that ignores that lock from
    creating an alias after this observation and before rebuild finishes.
    """
    for name in _DISPOSABLE_INDEX_NAMES:
        candidate = Path(target) / name
        if not _lexists(candidate):
            continue
        try:
            info = candidate.lstat()
        except OSError as exc:
            raise _merge_owned_error(
                "store-merge-rebuild-state-unreadable",
                f"target {name} cannot be inspected: {exc}",
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise _merge_owned_error(
                "store-merge-rebuild-state-unsafe",
                f"target {name} is not a regular file",
            )
        if info.st_nlink != 1:
            raise _merge_owned_error(
                "store-merge-rebuild-state-aliased",
                f"target {name} has {info.st_nlink} directory entries",
            )


def _read_exclusion(destination: Path, attribute: str) -> bytes:
    try:
        result = subprocess.run(
            ["/usr/bin/xattr", "-p", "-x", attribute, str(destination)],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _merge_owned_error(
            "store-merge-exclusion-unreadable",
            f"could not read back {attribute}: {exc}",
        ) from exc
    try:
        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _merge_owned_error(
            "store-merge-exclusion-undecodable",
            f"{attribute} produced non-UTF-8 xattr output",
        ) from exc
    if result.returncode != 0:
        raise _merge_owned_error(
            "store-merge-exclusion-unreadable",
            f"could not read back {attribute}: {stderr.strip()}",
        )
    try:
        value = bytes.fromhex(stdout)
    except ValueError as exc:
        raise _merge_owned_error(
            "store-merge-exclusion-undecodable",
            f"{attribute} did not read back as hexadecimal bytes",
        ) from exc
    if value != b"1":
        raise _merge_owned_error(
            "store-merge-exclusion-unverified",
            f"{attribute} did not read back as byte 0x31",
        )
    return value


def _apply_and_verify_exclusions(
    destination: Path,
    records: list[OperationRecord],
    instrument: OperationSink | None,
) -> None:
    for attribute in cas._SYNC_EXCLUSION_XATTRS:
        try:
            result = subprocess.run(
                ["/usr/bin/xattr", "-w", "-x", attribute, "31", str(destination)],
                check=False,
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise _merge_owned_error(
                "store-merge-exclusion-failed",
                f"could not set {attribute}: {exc}",
            ) from exc
        try:
            stderr = result.stderr.decode("utf-8")
            result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _merge_owned_error(
                "store-merge-exclusion-undecodable",
                f"{attribute} produced non-UTF-8 xattr output",
            ) from exc
        if result.returncode != 0:
            raise _merge_owned_error(
                "store-merge-exclusion-failed",
                f"could not set {attribute}: {stderr.strip()}",
            )
        _record_operation(
            records,
            instrument,
            "exclusion-applied",
            attribute=attribute,
        )
    for attribute in cas._SYNC_EXCLUSION_XATTRS:
        _read_exclusion(destination, attribute)
        _record_operation(
            records,
            instrument,
            "exclusion-verified",
            attribute=attribute,
        )


def _entry_identity(info: os.stat_result) -> _EntryIdentity:
    return _EntryIdentity(info.st_dev, info.st_ino)


def _require_entry_identity(
    info: os.stat_result,
    expected: _EntryIdentity,
    label: str,
) -> None:
    if _entry_identity(info) != expected:
        raise OSError(errno.ESTALE, f"{label} was replaced")


def _stat_at(directory_fd: int, name: str, label: str) -> os.stat_result:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as exc:
        raise OSError(exc.errno, f"cannot inspect {label}: {exc.strerror}") from exc


def _clear_immutable_fd(descriptor: int, info: os.stat_result, label: str) -> None:
    mask = getattr(stat, "UF_IMMUTABLE", 0)
    flags = getattr(info, "st_flags", 0)
    if not mask or not flags & mask:
        return
    library = ctypes.CDLL(None, use_errno=True)
    if not hasattr(library, "fchflags"):
        raise OSError(errno.ENOTSUP, f"cannot clear UF_IMMUTABLE on {label}")
    fchflags = library.fchflags
    fchflags.argtypes = [ctypes.c_int, ctypes.c_uint]
    fchflags.restype = ctypes.c_int
    ctypes.set_errno(0)
    if fchflags(descriptor, flags & ~mask) != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, f"cannot clear UF_IMMUTABLE on {label}")


def _restore_owner_write_fd(descriptor: int) -> None:
    info = os.fstat(descriptor)
    mode = stat.S_IMODE(info.st_mode)
    if not mode & stat.S_IWUSR:
        os.fchmod(descriptor, mode | stat.S_IWUSR)


def _remove_at(
    parent_fd: int,
    operation: Callable[[], None],
    *,
    restore_parent_write: bool,
) -> None:
    try:
        operation()
        return
    except OSError as first:
        if first.errno not in {errno.EACCES, errno.EPERM} or not restore_parent_write:
            raise
    _restore_owner_write_fd(parent_fd)
    operation()


def _open_directory_at(
    parent_fd: int,
    name: str,
    expected: _EntryIdentity,
    label: str,
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        _require_entry_identity(os.fstat(descriptor), expected, label)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _cleanup_directory_contents(directory_fd: int) -> None:
    directory_info = os.fstat(directory_fd)
    _clear_immutable_fd(directory_fd, directory_info, "owned clone directory")
    with os.scandir(directory_fd) as entries:
        names = sorted((entry.name for entry in entries), key=os.fsencode)
    for name in names:
        info = _stat_at(directory_fd, name, f"owned clone entry {name}")
        identity = _entry_identity(info)
        if stat.S_ISDIR(info.st_mode):
            with contextlib.ExitStack() as descriptors:
                child_fd = _open_directory_at(
                    directory_fd,
                    name,
                    identity,
                    f"owned clone directory {name}",
                )
                descriptors.callback(os.close, child_fd)
                _cleanup_directory_contents(child_fd)
                _clear_immutable_fd(
                    child_fd,
                    os.fstat(child_fd),
                    f"owned clone directory {name}",
                )
                _require_entry_identity(
                    _stat_at(directory_fd, name, f"owned clone directory {name}"),
                    identity,
                    f"owned clone directory {name}",
                )
                _remove_at(
                    directory_fd,
                    lambda: os.rmdir(name, dir_fd=directory_fd),
                    restore_parent_write=True,
                )
            continue

        if info.st_nlink != 1:
            raise OSError(
                errno.EMLINK,
                f"owned clone entry {name} has {info.st_nlink} directory entries",
            )
        if stat.S_ISREG(info.st_mode):
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(name, flags, dir_fd=directory_fd)
            try:
                _require_entry_identity(
                    os.fstat(descriptor),
                    identity,
                    f"owned clone entry {name}",
                )
                _clear_immutable_fd(descriptor, os.fstat(descriptor), f"owned clone entry {name}")
            finally:
                os.close(descriptor)
        _require_entry_identity(
            _stat_at(directory_fd, name, f"owned clone entry {name}"),
            identity,
            f"owned clone entry {name}",
        )
        _remove_at(
            directory_fd,
            lambda: os.unlink(name, dir_fd=directory_fd),
            restore_parent_write=True,
        )


def _cleanup_owned_clone(owned: _OwnedClone) -> None:
    """Remove only the directory inode created by the failing clone call."""
    try:
        current = os.stat(owned.name, dir_fd=owned.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    _require_entry_identity(current, owned.identity, "the owned clone destination")
    _require_entry_identity(
        os.fstat(owned.root_fd),
        owned.identity,
        "the owned clone destination descriptor",
    )
    _cleanup_directory_contents(owned.root_fd)
    _clear_immutable_fd(
        owned.root_fd,
        os.fstat(owned.root_fd),
        "the owned clone destination",
    )
    try:
        current = os.stat(owned.name, dir_fd=owned.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    _require_entry_identity(current, owned.identity, "the owned clone destination")
    _remove_at(
        owned.parent_fd,
        lambda: os.rmdir(owned.name, dir_fd=owned.parent_fd),
        restore_parent_write=False,
    )


def _require_owned_clone_current(owned: _OwnedClone) -> None:
    current = os.stat(owned.name, dir_fd=owned.parent_fd, follow_symlinks=False)
    _require_entry_identity(current, owned.identity, "the owned clone destination")


def _files_equal(left: Path, right: Path) -> bool:
    """Compare actual bytes without loading either file whole."""
    try:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(1024 * 1024)
                right_chunk = right_handle.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError as exc:
        raise _merge_owned_error(
            "store-merge-byte-comparison-failed",
            f"object bytes could not be compared: {exc}",
        ) from exc


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags)


def _clone_one(api: _DarwinClonefile, source: Path, destination: Path) -> None:
    with contextlib.ExitStack() as descriptors:
        source_fd = _open_directory(source.parent)
        descriptors.callback(os.close, source_fd)
        destination_fd = _open_directory(destination.parent)
        descriptors.callback(os.close, destination_fd)
        api.clonefileat(source_fd, source.name, destination_fd, destination.name)


def _create_inert_lock(destination: Path) -> None:
    lock_path = destination / journal.JOURNAL_LOCK_FILENAME
    descriptor = os.open(
        lock_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o644,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def clone_store(
    source: Path,
    destination: Path,
    *,
    instrument: OperationSink | None = None,
) -> None:
    """Create one complete APFS clone without rebuilding or verifying it."""
    source_root = _canonical_store_root(Path(source), "the source store")
    destination_input = _absolute_spelling(Path(destination))
    if _lexists(destination_input):
        try:
            destination_info = destination_input.lstat()
        except OSError as exc:
            raise _merge_owned_error(
                "store-merge-path-unreadable",
                f"the destination cannot be inspected: {exc}",
            ) from exc
        if stat.S_ISLNK(destination_info.st_mode):
            raise _merge_owned_error(
                "store-merge-path-symlink",
                "the destination is a symlink",
            )
        raise _merge_owned_error(
            "store-merge-destination-exists",
            "the destination already exists; a clone never adopts it",
        )
    try:
        resolved_destination_parent = destination_input.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _merge_owned_error(
            "store-merge-root-unusable",
            f"the destination parent cannot be canonicalized: {exc}",
        ) from exc
    destination_parent = _require_real_store_root(
        resolved_destination_parent,
        "the destination parent",
    )
    destination_root = destination_parent / destination_input.name
    if (
        destination_root == source_root
        or destination_root.is_relative_to(source_root)
        or source_root.is_relative_to(destination_root)
    ):
        raise _merge_owned_error(
            "store-merge-root-overlap",
            "the source and destination trees must be disjoint",
        )
    if source_root.stat().st_dev != destination_parent.stat().st_dev:
        raise _merge_owned_error(
            "store-merge-cross-device",
            "source and destination parent are on different devices; "
            "there is no byte-copy fallback",
        )
    _require_safe_lock(source_root, "the source store")
    _require_supported_platform()
    # journal_lock bootstraps missing authority paths.  Reject an incomplete
    # source before entering that create-capable context so cloning cannot
    # repair or otherwise normalize the source it was asked to preserve.
    _validate_store_tree(
        source_root,
        "the source store",
        verify_object_content=False,
    )
    native = _DarwinClonefile()
    operation_records: list[OperationRecord] = []
    with contextlib.ExitStack() as descriptors:
        parent_fd = _open_directory(destination_parent)
        descriptors.callback(os.close, parent_fd)
        owned: _OwnedClone | None = None
        created_destination = False
        try:
            with journal._canonical_journal_lock(source_root):
                source_identity = store_state.read_store_identity(source_root)
                inventory = _validate_store_tree(
                    source_root,
                    "the source store",
                    verify_object_content=False,
                )
                expected_objects, _names = cas.expected_index_rows(inventory.records)
                if set(inventory.objects) != set(expected_objects):
                    missing = sorted(set(expected_objects) - set(inventory.objects))
                    extra = sorted(set(inventory.objects) - set(expected_objects))
                    raise _merge_owned_error(
                        "store-merge-inventory-disagreement",
                        f"source journal and object tree disagree; missing={missing} extra={extra}",
                    )
                for cas_id, row in expected_objects.items():
                    if inventory.objects[cas_id].lstat().st_size != row[1]:
                        raise _merge_owned_error(
                            "store-merge-object-metadata",
                            f"source object size disagrees with the journal for {cas_id}",
                        )
                index_path = source_root / cas.INDEX_FILENAME
                if index_path not in inventory.disposable:
                    raise _merge_owned_error(
                        "store-merge-index-absent",
                        "the source store has no regular cas.sqlite to clone",
                    )

                try:
                    os.mkdir(destination_root.name, mode=0o755, dir_fd=parent_fd)
                except FileExistsError as exc:
                    raise _merge_owned_error(
                        "store-merge-destination-exists",
                        "the destination already exists; a clone never adopts it",
                    ) from exc
                created_destination = True
                created_info = os.stat(
                    destination_root.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                root_fd = _open_directory_at(
                    parent_fd,
                    destination_root.name,
                    _entry_identity(created_info),
                    "the exclusively created clone destination",
                )
                descriptors.callback(os.close, root_fd)
                owned = _OwnedClone(
                    parent_fd,
                    root_fd,
                    destination_root.name,
                    _entry_identity(os.fstat(root_fd)),
                )

                def guarded_instrument(record: OperationRecord) -> None:
                    if instrument is not None:
                        instrument(record)
                    _require_owned_clone_current(owned)

                _record_operation(
                    operation_records,
                    guarded_instrument,
                    "destination-root-created",
                )
                _apply_and_verify_exclusions(
                    destination_root,
                    operation_records,
                    guarded_instrument,
                )

                clone_relpaths = [
                    Path(cas.INDEX_FILENAME),
                    Path(journal.JOURNAL_FILENAME),
                    Path("objects"),
                ]
                wal_path = source_root / f"{cas.INDEX_FILENAME}-wal"
                if wal_path in inventory.disposable:
                    clone_relpaths.append(Path(wal_path.name))
                clone_relpaths.sort(key=lambda path: os.fsencode(path.as_posix()))
                for relative in clone_relpaths:
                    _clone_one(native, source_root / relative, destination_root / relative)
                    _record_operation(
                        operation_records,
                        guarded_instrument,
                        "clonefile",
                    )

                _create_inert_lock(destination_root)
                clone_identity = store_state.StoreIdentity(
                    str(uuid.uuid4()),
                    source_identity.lineage_id,
                    source_identity.epoch_id,
                    source_identity.generation,
                    source_identity.predecessor_epoch_id,
                )
                store_state._publish_metadata(
                    destination_root / "store.json", store_state._store_metadata(clone_identity)
                )
                _fsync_directory(destination_root)
                _record_operation(operation_records, guarded_instrument, "clone-complete")
        except BaseException as original:
            if owned is not None:
                try:
                    _cleanup_owned_clone(owned)
                except BaseException as cleanup:
                    raise _merge_owned_error(
                        "store-merge-cleanup-failed",
                        f"owned destination cleanup failed: {cleanup}",
                    ) from original
            elif created_destination:
                raise _merge_owned_error(
                    "store-merge-cleanup-failed",
                    "owned destination cleanup refused because its inode could not be bound",
                ) from original
            raise


@contextlib.contextmanager
def _locked_stores(first: Path, second: Path) -> Iterator[None]:
    ordered = sorted((first, second), key=lambda path: os.fsencode(os.fspath(path)))
    with contextlib.ExitStack() as stack:
        for root in ordered:
            stack.enter_context(journal._stable_lock(root))
        yield


def _merge_records(
    target: Sequence[dict[str, Any]],
    source: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Return the strict identity union in canonical journal order."""
    merged: dict[str, dict[str, Any]] = {}
    for record in (*target, *source):
        event_id = str(record["event_id"])
        existing = merged.get(event_id)
        if existing is None:
            merged[event_id] = record
        elif existing != record:
            raise _merge_owned_error(
                "store-merge-event-identity",
                f"event id {event_id} names two different records",
            )
    ordered = sorted(merged.values(), key=journal.journal_sort_key)
    return tuple(journal.validate_journal_sequence(ordered, "merged journal record"))


def _plan_merge(target: Path, source: Path) -> _MergePlan:
    """Validate both stores and compute the complete non-mutating target."""
    target_inventory = _validate_store_tree(
        target,
        "the target store",
        verify_object_content=True,
    )
    source_inventory = _validate_store_tree(
        source,
        "the source store",
        verify_object_content=True,
    )
    _assert_rebuild_state_unaliased(target)

    records = _merge_records(target_inventory.records, source_inventory.records)
    expected_objects, _names = cas.expected_index_rows(records)
    expected_ids = set(expected_objects)
    represented = set(target_inventory.objects) | set(source_inventory.objects)
    if represented != expected_ids:
        missing = sorted(expected_ids - represented)
        extra = sorted(represented - expected_ids)
        raise _merge_owned_error(
            "store-merge-inventory-disagreement",
            f"merged journal and object union disagree; missing={missing} extra={extra}",
        )

    objects_to_publish: list[_ObjectSource] = []
    for cas_id in sorted(expected_ids):
        expected_size = expected_objects[cas_id][1]
        target_path = target_inventory.objects.get(cas_id)
        source_path = source_inventory.objects.get(cas_id)
        for representation in (target_path, source_path):
            if representation is not None and representation.lstat().st_size != expected_size:
                raise _merge_owned_error(
                    "store-merge-object-metadata",
                    f"object size disagrees with the merged journal for {cas_id}",
                )
        if target_path is not None and source_path is not None:
            if not _files_equal(target_path, source_path):
                raise _merge_owned_error(
                    "store-merge-object-byte-conflict",
                    f"target and source bytes differ for {cas_id}",
                )
        elif target_path is None:
            if source_path is None:
                raise _merge_owned_error(
                    "store-merge-object-missing",
                    f"no verified representation exists for {cas_id}",
                )
            objects_to_publish.append(_ObjectSource(cas_id, source_path))

    return _MergePlan(
        records=records,
        journal_bytes=journal.canonical_journal_bytes(records),
        objects_to_publish=tuple(objects_to_publish),
        target_cas_ids=tuple(sorted(expected_ids)),
    )


def _owned_entry_info_at(
    directory_fd: int,
    name: str,
    identity: _EntryIdentity,
    label: str,
) -> os.stat_result:
    info = _stat_at(directory_fd, name, label)
    _require_entry_identity(info, identity, label)
    if info.st_nlink != 1:
        raise OSError(errno.EMLINK, f"{label} has {info.st_nlink} directory entries")
    return info


def _clear_owned_entry_immutable(
    directory_fd: int,
    name: str,
    identity: _EntryIdentity,
    label: str,
) -> None:
    info = _owned_entry_info_at(directory_fd, name, identity, label)
    mask = getattr(stat, "UF_IMMUTABLE", 0)
    if not mask or not getattr(info, "st_flags", 0) & mask:
        return
    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        _require_entry_identity(os.fstat(descriptor), identity, label)
        _clear_immutable_fd(descriptor, os.fstat(descriptor), label)
    finally:
        os.close(descriptor)


def _remove_owned_entry(
    directory_fd: int,
    name: str,
    identity: _EntryIdentity,
    label: str,
) -> None:
    try:
        _clear_owned_entry_immutable(directory_fd, name, identity, label)
    except FileNotFoundError:
        return
    _owned_entry_info_at(directory_fd, name, identity, label)
    os.unlink(name, dir_fd=directory_fd)


def _ensure_object_parent_durable(target: Path, destination_parent: Path) -> None:
    base = target / "objects" / "sha256"
    relative = destination_parent.relative_to(base)
    current = base
    with contextlib.ExitStack() as descriptors:
        parent_fd = _open_directory(base)
        descriptors.callback(os.close, parent_fd)
        for component in relative.parts:
            try:
                os.mkdir(component, dir_fd=parent_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise _merge_owned_error(
                    "store-merge-object-directory",
                    f"could not create the object prefix {component}: {exc}",
                ) from exc
            os.fsync(parent_fd)
            current /= component
            try:
                info = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
                child_fd = _open_directory_at(
                    parent_fd,
                    component,
                    _entry_identity(info),
                    f"object prefix {current.relative_to(target).as_posix()}",
                )
            except OSError as exc:
                raise _merge_owned_error(
                    "store-merge-object-directory",
                    f"the object prefix {current.relative_to(target).as_posix()} is unsafe: {exc}",
                ) from exc
            descriptors.callback(os.close, child_fd)
            parent_fd = child_fd


def _verify_existing_object(destination: Path, source: Path, cas_id: str) -> None:
    try:
        info = destination.lstat()
    except OSError as exc:
        raise _merge_owned_error(
            "store-merge-destination-conflict",
            f"target object for {cas_id} cannot be inspected: {exc}",
        ) from exc
    if not stat.S_ISREG(info.st_mode) or cas.sha256_file(destination) != cas_id:
        raise _merge_owned_error(
            "store-merge-destination-conflict",
            f"target custody contains another representation for {cas_id}",
        )
    if not _files_equal(destination, source):
        raise _merge_owned_error(
            "store-merge-destination-conflict",
            f"target custody contains different bytes for {cas_id}",
        )


def _publish_object(
    target: Path,
    object_source: _ObjectSource,
    native: _DarwinClonefile,
) -> None:
    """Publish one missing target object exclusively and seal it."""
    destination = cas.object_path(target, object_source.cas_id)
    _ensure_object_parent_durable(target, destination.parent)
    if _lexists(destination):
        _verify_existing_object(destination, object_source.source, object_source.cas_id)
        cas.seal_objects(target, [object_source.cas_id])
        _fsync_directory(destination.parent)
        return

    temporary = destination.parent / f".cas-merge-{uuid.uuid4().hex}.tmp"
    with contextlib.ExitStack() as descriptors:
        source_fd = _open_directory(object_source.source.parent)
        descriptors.callback(os.close, source_fd)
        destination_fd = _open_directory(destination.parent)
        descriptors.callback(os.close, destination_fd)
        temporary_identity: _EntryIdentity | None = None
        try:
            native.clonefileat(
                source_fd,
                object_source.source.name,
                destination_fd,
                temporary.name,
            )
            info = _stat_at(destination_fd, temporary.name, "the staged object")
            temporary_identity = _entry_identity(info)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise _merge_owned_error(
                    "store-merge-staged-unsafe",
                    f"the staged representation for {object_source.cas_id} is unsafe",
                )
            if cas.sha256_file(temporary) != object_source.cas_id or not _files_equal(
                temporary,
                object_source.source,
            ):
                raise _merge_owned_error(
                    "store-merge-staged-identity",
                    f"the staged representation does not match {object_source.cas_id}",
                )
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            _clear_owned_entry_immutable(
                destination_fd,
                temporary.name,
                temporary_identity,
                "the staged object",
            )
            try:
                native.rename_exclusive(
                    destination_fd,
                    temporary.name,
                    destination_fd,
                    destination.name,
                )
            except FileExistsError:
                _remove_owned_entry(
                    destination_fd,
                    temporary.name,
                    temporary_identity,
                    "the staged object",
                )
                temporary_identity = None
                _verify_existing_object(destination, object_source.source, object_source.cas_id)
            else:
                temporary_identity = None
        except BaseException as original:
            if temporary_identity is not None:
                try:
                    _remove_owned_entry(
                        destination_fd,
                        temporary.name,
                        temporary_identity,
                        "the staged object",
                    )
                except BaseException as cleanup:
                    raise _merge_owned_error(
                        "store-merge-staging-cleanup-failed",
                        f"owned object staging cleanup failed: {cleanup}",
                    ) from original
            raise
    cas.seal_objects(target, [object_source.cas_id])
    _verify_existing_object(destination, object_source.source, object_source.cas_id)
    _fsync_directory(destination.parent)


def _publish_journal(target: Path, journal_bytes: bytes) -> None:
    """Replace the target journal through a staged, read-back-verified file."""
    destination = target / journal.JOURNAL_FILENAME
    if destination.read_bytes() == journal_bytes:
        _fsync_directory(target)
        return
    temporary = target / f".cas-merge-journal.{uuid.uuid4().hex}.tmp"
    with contextlib.ExitStack() as descriptors:
        target_fd = _open_directory(target)
        descriptors.callback(os.close, target_fd)
        descriptor = os.open(
            temporary.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=target_fd,
        )
        temporary_identity: _EntryIdentity | None = None
        try:
            temporary_identity = _entry_identity(os.fstat(descriptor))
            try:
                remaining = memoryview(journal_bytes)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written <= 0:
                        raise _merge_owned_error(
                            "store-merge-journal-staging",
                            "the journal staging write made no progress",
                        )
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if temporary.read_bytes() != journal_bytes:
                raise _merge_owned_error(
                    "store-merge-journal-staging",
                    "the staged merged journal did not read back as written",
                )
            os.replace(
                temporary.name,
                destination.name,
                src_dir_fd=target_fd,
                dst_dir_fd=target_fd,
            )
            _fsync_directory(target)
        except BaseException as original:
            if temporary_identity is None:
                try:
                    os.close(descriptor)
                except BaseException as cleanup:
                    raise _merge_owned_error(
                        "store-merge-staging-cleanup-failed",
                        f"unbound journal staging descriptor cleanup failed: {cleanup}",
                    ) from original
                raise _merge_owned_error(
                    "store-merge-staging-cleanup-failed",
                    "journal staging cleanup refused because its inode could not be bound",
                ) from original
            try:
                _remove_owned_entry(
                    target_fd,
                    temporary.name,
                    temporary_identity,
                    "the staged journal",
                )
            except BaseException as cleanup:
                raise _merge_owned_error(
                    "store-merge-staging-cleanup-failed",
                    f"owned journal staging cleanup failed: {cleanup}",
                ) from original
            raise


def _fsync_directory(directory: Path) -> None:
    descriptor = _open_directory(directory)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _resolved_merge_roots(target: Path, source: Path) -> tuple[Path, Path]:
    target_root = _canonical_store_root(target, "the target store")
    source_root = _canonical_store_root(source, "the source store")
    _require_safe_lock(target_root, "the target store")
    _require_safe_lock(source_root, "the source store")
    try:
        same = os.path.samefile(target_root, source_root)
    except OSError as exc:
        raise _merge_owned_error(
            "store-merge-root-unusable",
            f"store roots cannot be compared: {exc}",
        ) from exc
    if same or target_root.is_relative_to(source_root) or source_root.is_relative_to(target_root):
        raise _merge_owned_error(
            "store-merge-root-overlap",
            "the target and source roots must be distinct and non-overlapping",
        )
    return target_root, source_root


def merge_into(target: Path, source: Path) -> None:
    """Merge source into target under both stable locks."""
    target_root, source_root = _resolved_merge_roots(Path(target), Path(source))
    native = _DarwinClonefile()
    with _locked_stores(target_root, source_root):
        store_state._require_same_epoch(target_root, source_root)
        plan = _plan_merge(target_root, source_root)
        for object_source in plan.objects_to_publish:
            _publish_object(target_root, object_source, native)
        for cas_id in plan.target_cas_ids:
            candidate = cas.require_object(target_root, cas_id)
            if cas.sha256_file(candidate) != cas_id:
                raise _merge_owned_error(
                    "store-merge-recovery-verification",
                    f"target object does not match {cas_id} before journal publication",
                )
        _publish_journal(target_root, plan.journal_bytes)
        if (target_root / journal.JOURNAL_FILENAME).read_bytes() != plan.journal_bytes or tuple(
            journal.read_journal(target_root)
        ) != plan.records:
            raise _merge_owned_error(
                "store-merge-journal-publication",
                "the published journal does not match the planned union",
            )
        _assert_rebuild_state_unaliased(target_root)
        cas.rebuild_index_under_lock(target_root)
        cas.seal_objects(target_root, plan.target_cas_ids)
        verdict = cas.verify(target_root)
        if not verdict.ok:
            raise _merge_owned_error(
                "store-merge-verification-failed",
                f"the merged target store did not verify: {list(verdict.errors)}",
            )
