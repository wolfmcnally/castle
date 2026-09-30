"""Authorized local excision by reconstruction into a successor epoch."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import sys
import uuid
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from castle import cas, journal
from castle import store_state as state
from castle.store_state import StoreIdentity, StoreStateError

_CODES = frozenset(
    {
        "excision-root-unsafe",
        "excision-request-invalid",
        "excision-destination-unowned",
        "excision-operation-mismatch",
        "excision-source-inconsistent",
        "excision-remotes-unreadable",
        "excision-selection-unknown",
        "excision-preparation-incomplete",
        "excision-recovery-state",
        "excision-retirement-committed",
        "excision-preparation-failed",
        "excision-retirement-incomplete",
    }
)


class ExcisionError(journal.CastleError):
    """A fixed, content-free excision refusal."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or code not in _CODES:
            raise ValueError("excision-request-invalid") from None
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ExcisionReceipt:
    schema: str
    operation_id: str
    predecessor: StoreIdentity
    successor: StoreIdentity
    outcome: str
    local_result: str

    def __post_init__(self) -> None:
        try:
            if (
                self.schema != "castle.excision_receipt.v1"
                or self.outcome != "completed"
                or self.local_result != "successor-active-predecessor-retired"
                or type(self.predecessor) is not StoreIdentity
                or type(self.successor) is not StoreIdentity
            ):
                raise ValueError()
            state._binding(
                {
                    "operation_id": self.operation_id,
                    "predecessor": self.predecessor.to_dict(),
                    "successor": self.successor.to_dict(),
                }
            )
        except (ValueError, StoreStateError):
            raise ExcisionError("excision-request-invalid") from None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "operation_id": self.operation_id,
            "predecessor": self.predecessor.to_dict(),
            "successor": self.successor.to_dict(),
            "outcome": self.outcome,
            "local_result": self.local_result,
        }


def _receipt(binding: dict[str, Any]) -> ExcisionReceipt:
    return ExcisionReceipt(
        "castle.excision_receipt.v1",
        binding["operation_id"],
        state._identity(binding["predecessor"]),
        state._identity(binding["successor"]),
        "completed",
        "successor-active-predecessor-retired",
    )


def _roots(source: Path, destination: Path) -> tuple[Path, Path]:
    try:
        source, destination = Path(source), Path(destination)
        if not stat.S_ISDIR(source.lstat().st_mode):
            raise OSError()
        source = source.resolve(strict=True)
        if state._present(destination):
            if not stat.S_ISDIR(destination.lstat().st_mode):
                raise OSError()
            destination = destination.resolve(strict=True)
        else:
            destination = destination.parent.resolve(strict=True) / destination.name
        if not destination.parent.is_dir() or (
            source == destination
            or source.is_relative_to(destination)
            or destination.is_relative_to(source)
        ):
            raise OSError()
        if not journal.journal_lock_is_safe(source / journal.JOURNAL_LOCK_FILENAME):
            raise OSError()
        if state._present(destination):
            if source.samefile(destination):
                raise OSError()
            lock = destination / journal.JOURNAL_LOCK_FILENAME
            if state._present(lock) and not journal.journal_lock_is_safe(lock):
                raise OSError()
        return source, destination
    except (OSError, RuntimeError, ValueError, TypeError):
        raise ExcisionError("excision-root-unsafe") from None


def _request(operation_id: str, expected_source: StoreIdentity) -> None:
    try:
        if not state._is_uuid(operation_id) or type(expected_source) is not StoreIdentity:
            raise ValueError()
        state._identity(expected_source.to_dict())
        if operation_id in expected_source.to_dict().values():
            raise ValueError()
    except (ValueError, StoreStateError):
        raise ExcisionError("excision-request-invalid") from None


def _selection(cas_ids: Sequence[str]) -> list[str]:
    if not isinstance(cas_ids, Sequence) or isinstance(cas_ids, (str, bytes)) or not cas_ids:
        raise ExcisionError("excision-request-invalid")
    if any(not isinstance(item, str) or not state._CAS_ID.fullmatch(item) for item in cas_ids):
        raise ExcisionError("excision-request-invalid")
    return sorted(set(cas_ids))


def _matches(binding: dict[str, Any], operation: str, expected: StoreIdentity) -> None:
    if binding["operation_id"] != operation or binding["predecessor"] != expected.to_dict():
        raise ExcisionError("excision-operation-mismatch")


def _retirement(source: Path, operation: str, expected: StoreIdentity) -> dict[str, Any] | None:
    if not state._present(source / "retired.json"):
        return None
    try:
        marker = state._read_metadata(source / "retired.json")
    except StoreStateError:
        raise ExcisionError("excision-recovery-state") from None
    _matches(marker["transition"], operation, expected)
    return marker


def _destination(destination: Path, operation: str, expected: StoreIdentity) -> dict[str, Any]:
    try:
        metadata = state._read_metadata(destination / "store.json")
    except StoreStateError:
        raise ExcisionError("excision-destination-unowned") from None
    if metadata["transition"] is None:
        raise ExcisionError("excision-destination-unowned")
    if not journal.journal_lock_is_safe(destination / "journal.lock"):
        raise ExcisionError("excision-root-unsafe")
    _matches(metadata["transition"], operation, expected)
    if state._present(destination / "retired.json"):
        raise ExcisionError("excision-recovery-state")
    return metadata


def _source_authority(
    source: Path,
) -> tuple[list[dict[str, Any]], dict[str, Path], list[tuple[str, ...]]]:
    try:
        return cas._validated_authority(source)
    except cas._UnreadableRemotes:
        raise ExcisionError("excision-remotes-unreadable") from None
    except cas._UnsafeAuthority:
        raise ExcisionError("excision-root-unsafe") from None
    except (OSError, journal.CastleError, sqlite3.Error, ValueError):
        raise ExcisionError("excision-source-inconsistent") from None


def _source_identity(
    source: Path, destination: Path, operation: str, expected: StoreIdentity
) -> StoreIdentity:
    residue = source / ".retired.json.tmp"
    if not state._present(residue):
        return state.read_store_identity(source)
    try:
        _destination(destination, operation, expected)
        state._regular(residue)
        metadata = state._read_metadata(source / "store.json")
        if metadata["state"] != "active" or any(
            state._present(source / name)
            for name in (
                ".store.json.tmp",
                ".excision-selection.json.tmp",
                "excision-selection.json",
            )
        ):
            raise ValueError()
        return state._identity(metadata["identity"])
    except (StoreStateError, ValueError):
        raise ExcisionError("excision-recovery-state") from None


@contextmanager
def _locks(source: Path, destination: Path) -> Iterator[None]:
    with ExitStack() as stack:
        for root in sorted((source, destination), key=os.fsencode):
            stack.enter_context(journal._stable_lock(root))
        yield


def _remove_file(path: Path, *, volatile: bool = False) -> None:
    try:
        info = state._regular(path)
        mask = getattr(stat, "UF_IMMUTABLE", 0)
        if mask and getattr(info, "st_flags", 0) & mask:
            os.chflags(path, info.st_flags & ~mask)
        path.unlink()
    except FileNotFoundError:
        if not volatile:
            raise
    state._fsync_directory(path.parent)


def _remove_tree(root: Path) -> None:
    # The complete subtree was checked before any removal. Recheck each node
    # as well; a retirement marker never licenses following a new link.
    for path in sorted(root.iterdir()):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            _remove_tree(path)
        else:
            _remove_file(path)
    root.rmdir()
    state._fsync_directory(root.parent)


def _remove_residue(root: Path) -> None:
    for name in sorted(state._TEMP_NAMES):
        path = root / name
        if state._present(path):
            _remove_file(path)


def _clean_payload(root: Path, *, predecessor: bool) -> None:
    cas._authority_paths(root)
    if predecessor and state._present(root / "excision-selection.json"):
        raise cas._UnsafeAuthority("selection is never predecessor payload")
    keep = (
        {"retired.json", "journal.lock"}
        if predecessor
        else {"store.json", "excision-selection.json", "journal.lock"}
    )
    for path in sorted(root.iterdir()):
        if path.name in keep:
            continue
        if path.name == "objects":
            _remove_tree(path)
        else:
            _remove_file(path, volatile=path.name in cas._SQLITE_SIDECARS)
    state._fsync_directory(root)


def _seal(root: Path) -> str:
    records, paths, remotes = cas._validated_authority(root, derived=True)
    if (root / "journal.jsonl").read_bytes() != journal.canonical_journal_bytes(records):
        raise ExcisionError("excision-recovery-state")
    preimage = {
        "objects": [[identity, paths[identity].stat().st_size] for identity in sorted(paths)],
        "journal_sha256": "sha256:"
        + hashlib.sha256(journal.canonical_journal_bytes(records)).hexdigest(),
        "remotes": sorted([list(row) for row in remotes]),
    }
    return "sha256:" + hashlib.sha256(state._canonical(preimage)).hexdigest()


def _saved_selection(destination: Path, metadata: dict[str, Any], selected: list[str]) -> bool:
    binding = metadata["transition"]
    selection_path = destination / "excision-selection.json"
    if state._present(selection_path):
        try:
            saved = state._read_metadata(selection_path)
        except StoreStateError:
            raise ExcisionError("excision-recovery-state") from None
        if saved["transition"] != binding or saved["cas_ids"] != selected:
            raise ExcisionError("excision-operation-mismatch")
        return True
    else:
        if metadata["state"] != "building" or {path.name for path in destination.iterdir()} - {
            "store.json",
            "journal.lock",
            *state._TEMP_NAMES,
        }:
            raise ExcisionError("excision-recovery-state")
        if state._present(destination / ".excision-selection.json.tmp"):
            raise ExcisionError("excision-recovery-state")
        return False


def _copy_exclusions(source: Path, destination: Path) -> None:
    if sys.platform == "darwin":
        try:
            for name in sorted(cas._sync_exclusion_attributes(source)):
                value = bytes.fromhex(
                    cas._run_xattr(["-p", "-x", name, str(source)]).decode("utf-8")
                )
                cas._run_xattr(["-w", "-x", name, value.hex(), str(destination)])
                observed = bytes.fromhex(
                    cas._run_xattr(["-p", "-x", name, str(destination)]).decode("utf-8")
                )
                if observed != value:
                    raise ExcisionError("excision-root-unsafe")
        except (cas.CasError, UnicodeError, ValueError):
            raise ExcisionError("excision-root-unsafe") from None
        cas._store_requires_user_immutable.cache_clear()


def _prepare_destination(
    source: Path,
    destination: Path,
    metadata: dict[str, Any],
    selected: list[str],
    authority: tuple[list[dict[str, Any]], dict[str, Path], list[tuple[str, ...]]],
) -> dict[str, Any]:
    binding = metadata["transition"]
    selection_path = destination / "excision-selection.json"
    _copy_exclusions(source, destination)
    if not _saved_selection(destination, metadata, selected):
        _remove_residue(destination)
        state._publish_metadata(
            selection_path,
            {
                "schema": "castle.excision_selection.v1",
                "transition": binding,
                "cas_ids": selected,
            },
        )
    _remove_residue(destination)
    identity = state._identity(metadata["identity"])
    if metadata["state"] == "prepared":
        building = state._store_metadata(identity, "building", binding)
        state._publish_metadata(destination / "store.json", building, previous=metadata)
        metadata = building
    elif metadata["state"] != "building":
        raise ExcisionError("excision-recovery-state")
    _clean_payload(destination, predecessor=False)
    records, paths, remotes = authority
    removed = set(selected)
    retained = [record for record in records if record["cas_id"] not in removed]
    retained_remotes = [row for row in remotes if row[0] not in removed]
    (destination / "objects" / "sha256").mkdir(parents=True)
    for identity in sorted(set(paths) - removed):
        output = cas.object_path(destination, identity)
        output.parent.mkdir(parents=True, exist_ok=True)
        with paths[identity].open("rb") as original, output.open("xb") as copied:
            shutil.copyfileobj(original, copied)
            copied.flush()
            os.fsync(copied.fileno())
        if cas.sha256_file(output) != identity:
            raise ExcisionError("excision-preparation-failed")
        with paths[identity].open("rb") as original, output.open("rb") as copied:
            while block := original.read(1024 * 1024):
                if copied.read(len(block)) != block:
                    raise ExcisionError("excision-preparation-failed")
            if copied.read(1):
                raise ExcisionError("excision-preparation-failed")
        cas._seal_object(destination, output, identity)
    with (destination / "journal.jsonl").open("xb") as handle:
        handle.write(journal.canonical_journal_bytes(retained))
        handle.flush()
        os.fsync(handle.fileno())
    cas._write_fresh_index(destination, retained, retained_remotes)
    for directory, _subdirs, _files in os.walk(destination, topdown=False):
        state._fsync_directory(Path(directory))
    prepared = state._store_metadata(
        state._identity(metadata["identity"]), "prepared", binding, _seal(destination)
    )
    state._publish_metadata(destination / "store.json", prepared, previous=metadata)
    return prepared


def _finish(
    source: Path, destination: Path, marker: dict[str, Any], metadata: dict[str, Any]
) -> ExcisionReceipt:
    binding = marker["transition"]
    if metadata["transition"] is None or metadata["state"] not in ("prepared", "active"):
        raise ExcisionError("excision-recovery-state")
    if metadata["transition"] != binding:
        raise ExcisionError("excision-operation-mismatch")
    try:
        if state._present(source / "store.json"):
            predecessor = state._read_metadata(source / "store.json")
            if (
                predecessor["identity"] != binding["predecessor"]
                or predecessor["state"] != "active"
            ):
                raise ValueError()
        if metadata["state"] == "prepared":
            if _seal(destination) != metadata["prepared_sha256"]:
                raise ValueError()
        else:
            cas._validated_authority(destination, derived=True)
    except (OSError, journal.CastleError, sqlite3.Error, ValueError):
        raise ExcisionError("excision-recovery-state") from None
    # Structural source validation intentionally does not require its partial
    # object/journal authority to agree after retirement.
    # A visible rename can outlive a failed directory fsync. Establish the
    # validated retirement fence durably before deleting any payload on retry.
    state._fsync_directory(source)
    _clean_payload(source, predecessor=True)
    _remove_residue(destination)
    if state._present(destination / "excision-selection.json"):
        _remove_file(destination / "excision-selection.json")
    if metadata["state"] == "prepared":
        for name in sorted(cas._SQLITE_SIDECARS):
            path = destination / name
            try:
                info = state._regular(path)
            except FileNotFoundError:
                continue
            if info.st_size and name != cas.INDEX_FILENAME + "-shm":
                raise ExcisionError("excision-recovery-state")
            _remove_file(path, volatile=True)
    state._fsync_directory(source)
    state._fsync_directory(destination)
    if {path.name for path in source.iterdir()} != {"retired.json", "journal.lock"}:
        raise ExcisionError("excision-retirement-incomplete")
    inventory = {path.name for path in destination.iterdir()}
    if metadata["state"] == "active":
        # Validated active growth may live in committed WAL. Its database and
        # sidecars are current authority-derived state, not preparation residue.
        inventory -= cas._SQLITE_SIDECARS
    if inventory != {
        "objects",
        "journal.jsonl",
        "cas.sqlite",
        "journal.lock",
        "store.json",
    }:
        raise ExcisionError("excision-recovery-state")
    if metadata["state"] == "prepared":
        if _seal(destination) != metadata["prepared_sha256"]:
            raise ExcisionError("excision-recovery-state")
        active = state._store_metadata(state._identity(metadata["identity"]), "active", binding)
        state._publish_metadata(destination / "store.json", active, previous=metadata)
        metadata = active
    if state._read_metadata(destination / "store.json") != metadata:
        raise ExcisionError("excision-recovery-state")
    state._fsync_directory(destination)
    complete = {**marker, "state": "complete"}
    state._publish_metadata(source / "retired.json", complete, previous=marker)
    return _receipt(binding)


def excise_store(
    source: Path,
    destination: Path,
    *,
    cas_ids: Sequence[str],
    operation_id: str,
    expected_source: StoreIdentity,
) -> ExcisionReceipt:
    """Reconstruct retained authority and irreversibly retire the local predecessor."""
    source, destination = _roots(source, destination)
    try:
        cas._authority_paths(source)
        if state._present(destination):
            cas._authority_paths(destination)
    except (OSError, cas._UnsafeAuthority):
        raise ExcisionError("excision-root-unsafe") from None
    _request(operation_id, expected_source)
    selected = _selection(cas_ids)
    allocated: dict[str, Any] | None = None
    try:
        with journal._stable_lock(source):
            if _retirement(source, operation_id, expected_source) is not None:
                raise ExcisionError("excision-retirement-committed")
            if (
                _source_identity(source, destination, operation_id, expected_source)
                != expected_source
            ):
                raise ExcisionError("excision-operation-mismatch")
            if state._present(destination):
                metadata = _destination(destination, operation_id, expected_source)
                if metadata["state"] not in ("building", "prepared"):
                    raise ExcisionError("excision-recovery-state")
                _saved_selection(destination, metadata, selected)
            authority = _source_authority(source)
            if set(selected) - set(authority[1]):
                raise ExcisionError("excision-selection-unknown")
            if not state._present(destination):
                successor = StoreIdentity(
                    str(uuid.uuid4()),
                    expected_source.lineage_id,
                    str(uuid.uuid4()),
                    expected_source.generation + 1,
                    expected_source.epoch_id,
                )
                binding = state._binding(
                    {
                        "operation_id": operation_id,
                        "predecessor": expected_source.to_dict(),
                        "successor": successor.to_dict(),
                    }
                )
                destination.mkdir()
                _copy_exclusions(source, destination)
                descriptor = os.open(
                    destination / "journal.lock",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o644,
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                state._fsync_directory(destination)
                state._fsync_directory(destination.parent)
                allocated = state._store_metadata(successor, "building", binding)
        with _locks(source, destination):
            if _retirement(source, operation_id, expected_source) is not None:
                raise ExcisionError("excision-retirement-committed")
            if (
                _source_identity(source, destination, operation_id, expected_source)
                != expected_source
            ):
                raise ExcisionError("excision-operation-mismatch")
            if allocated is not None:
                state._publish_metadata(destination / "store.json", allocated)
            metadata = _destination(destination, operation_id, expected_source)
            _saved_selection(destination, metadata, selected)
            authority = _source_authority(source)
            if set(selected) - set(authority[1]):
                raise ExcisionError("excision-selection-unknown")
            metadata = _prepare_destination(source, destination, metadata, selected, authority)
            marker = {
                "schema": "castle.retirement.v1",
                "transition": metadata["transition"],
                "state": "retired",
            }
            # A retry may remove only safe source residue bound to this operation.
            if state._present(source / ".retired.json.tmp"):
                _remove_file(source / ".retired.json.tmp")
            state._publish_metadata(source / "retired.json", marker)
            return _finish(source, destination, marker, metadata)
    except (ExcisionError, StoreStateError):
        raise
    except (OSError, journal.CastleError, sqlite3.Error, ValueError):
        code = (
            "excision-retirement-incomplete"
            if state._present(source / "retired.json")
            else "excision-preparation-failed"
        )
        raise ExcisionError(code) from None


def recover_excision(
    source: Path, destination: Path, *, operation_id: str, expected_source: StoreIdentity
) -> ExcisionReceipt:
    """Complete a committed retirement forward, or return its historical receipt."""
    source, destination = _roots(source, destination)
    _request(operation_id, expected_source)
    try:
        with journal._stable_lock(source):
            marker = _retirement(source, operation_id, expected_source)
            if marker is None:
                if (
                    _source_identity(source, destination, operation_id, expected_source)
                    != expected_source
                ):
                    raise ExcisionError("excision-operation-mismatch")
                if state._present(destination):
                    metadata = _destination(destination, operation_id, expected_source)
                    if metadata["state"] not in ("building", "prepared"):
                        raise ExcisionError("excision-recovery-state")
                raise ExcisionError("excision-preparation-incomplete")
            if not state._present(destination):
                raise ExcisionError("excision-recovery-state")
            if not journal.journal_lock_is_safe(destination / "journal.lock"):
                raise ExcisionError("excision-recovery-state")
        with _locks(source, destination):
            marker = _retirement(source, operation_id, expected_source)
            if marker is None:
                raise ExcisionError("excision-recovery-state")
            if marker["state"] == "complete":
                binding = marker["transition"]
                try:
                    if state._present(destination / "retired.json"):
                        successor = state._identity(
                            state._read_metadata(destination / "retired.json")["transition"][
                                "predecessor"
                            ]
                        )
                    else:
                        successor = state.read_store_identity(destination)
                except StoreStateError:
                    raise ExcisionError("excision-recovery-state") from None
                if successor.to_dict() != binding["successor"]:
                    raise ExcisionError("excision-operation-mismatch")
                state._fsync_directory(source)
                return _receipt(binding)
            try:
                metadata = state._read_metadata(destination / "store.json")
            except StoreStateError:
                raise ExcisionError("excision-recovery-state") from None
            if state._present(destination / "retired.json"):
                raise ExcisionError("excision-recovery-state")
            return _finish(source, destination, marker, metadata)
    except (ExcisionError, StoreStateError):
        raise
    except (OSError, journal.CastleError, sqlite3.Error, ValueError):
        code = (
            "excision-retirement-incomplete"
            if state._present(source / "retired.json")
            else "excision-preparation-failed"
        )
        raise ExcisionError(code) from None
