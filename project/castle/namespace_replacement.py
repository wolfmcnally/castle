"""Selection-free generation markers and exact owned replacement files.

Callers hold domain ownership and the namespace coordinator lock. A marker is
never authority for deleting a file whose private ownership was not validated.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any

from castle import cas, store_state
from castle import namespace as n
from castle.cas_recovery import _write_all

STAGES = (
    "blocked",
    "image-ready",
    "database-published",
    "cleanup-scanned",
    "cleanup-verified",
    "complete",
)
_KEYS = {
    "schema",
    "namespace_id",
    "operation_id",
    "source_generation",
    "target_generation",
    "predecessor_cas",
    "successor_cas",
    "stage",
    "image_digest",
}
_DIGEST = re.compile(r"[0-9a-f]{64}")


def invalid() -> None:
    n._refuse("namespace-replacement-invalid")


def regular(file: Path) -> os.stat_result:
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        invalid()
    return info


def sync_file(file: Path) -> None:
    regular(file)
    descriptor = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate(value: object) -> dict[str, Any]:
    try:
        if not isinstance(value, dict) or set(value) != _KEYS:
            invalid()
        if value["schema"] != "castle.namespace_replacement.v1" or value["stage"] not in STAGES:
            invalid()
        identifiers = [
            value[key]
            for key in ("namespace_id", "operation_id", "source_generation", "target_generation")
        ]
        for identity in identifiers:
            n._uuid(identity)
        if len(set(identifiers)) != len(identifiers):
            invalid()
        predecessor = store_state._identity(value["predecessor_cas"])
        successor = store_state._identity(value["successor_cas"])
        if predecessor == successor:
            if value["stage"] != "blocked":
                invalid()
        else:
            store_state._binding(
                {
                    "operation_id": value["operation_id"],
                    "predecessor": predecessor.to_dict(),
                    "successor": successor.to_dict(),
                }
            )
        digest = value["image_digest"]
        if value["stage"] == "blocked":
            if digest is not None:
                invalid()
        elif not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
            invalid()
        return value
    except (n.NamespaceError, store_state.StoreStateError, TypeError, ValueError, KeyError):
        invalid()


def read(root: Path) -> dict[str, Any] | None:
    file = root / "replacement.json"
    try:
        regular(file)
    except FileNotFoundError:
        return None
    try:
        return validate(n._decode(file.read_bytes()))
    except (n.NamespaceError, ValueError, TypeError, KeyError):
        invalid()


def filenames(marker: dict[str, Any]) -> tuple[str, str, str]:
    validate(marker)
    return (
        f"namespace.{marker['target_generation']}.sqlite.new",
        f"namespace.{marker['source_generation']}.sqlite.old",
        f"replacement.{marker['operation_id']}.tmp",
    )


def publish(
    root: Path,
    value: dict[str, Any],
    *,
    previous: dict[str, Any] | None,
    temporary_operation: str | None = None,
) -> None:
    """Publish only the caller's intent-bound value, retaining failed residue.

    An existing temporary must be an exact prefix of this independently known
    value. Visibility after a failed sync does not skip a fresh synchronization.
    """
    validate(value)
    if previous is not None:
        validate(previous)
        same = ("namespace_id", "operation_id", "source_generation", "target_generation")
        if any(value[key] != previous[key] for key in same):
            # A new operation may replace only a completed historical marker.
            if (
                previous["stage"] != "complete"
                or value["stage"] != "blocked"
                or previous["namespace_id"] != value["namespace_id"]
                or previous["operation_id"] == value["operation_id"]
                or previous["target_generation"] != value["source_generation"]
                or previous["successor_cas"] != value["predecessor_cas"]
            ):
                invalid()
        else:
            before, after = STAGES.index(previous["stage"]), STAGES.index(value["stage"])
            if after not in {before, before + 1}:
                invalid()
            if value["predecessor_cas"] != previous["predecessor_cas"]:
                invalid()
            if (
                previous["stage"] == "blocked"
                and previous["successor_cas"] != previous["predecessor_cas"]
                and previous["successor_cas"] != value["successor_cas"]
            ):
                invalid()
            if previous["stage"] != "blocked" and (
                value["successor_cas"] != previous["successor_cas"]
                or value["image_digest"] != previous["image_digest"]
            ):
                invalid()
    current = read(root)
    if current not in (previous, value):
        invalid()
    raw = n.canonical_bytes(value)
    temporary = root / f"replacement.{n._uuid(temporary_operation or value['operation_id'])}.tmp"
    write_private(temporary, raw)
    if temporary.read_bytes() != raw or validate(n._decode(temporary.read_bytes())) != value:
        invalid()
    if read(root) != current:
        invalid()
    if current is None:
        store_state._rename_new(temporary, root / "replacement.json")
    else:
        os.replace(temporary, root / "replacement.json")
    store_state._fsync_directory(root)


def write_private(file: Path, raw: bytes, *, mode: int = 0o600) -> None:
    """Fill only a privately reserved missing/exact-prefix slot and sync it."""
    try:
        regular(file)
    except FileNotFoundError:
        descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    else:
        if not raw.startswith(file.read_bytes()):
            invalid()
        file.chmod(0o600)
        descriptor = os.open(file, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        _write_all(descriptor, raw, offset=0)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if file.read_bytes() != raw:
        invalid()


def digest(file: Path) -> str:
    regular(file)
    return cas.sha256_file(file).removeprefix("sha256:")


def verify_image(file: Path, marker: dict[str, Any]) -> None:
    validate(marker)
    if marker["image_digest"] is None or digest(file) != marker["image_digest"]:
        invalid()


def remove_owned(file: Path, *, expected_digest: str | None = None) -> None:
    """Remove one privately validated slot; no recursive or filename inference."""
    try:
        regular(file)
    except FileNotFoundError:
        store_state._fsync_directory(file.parent)
        return
    if expected_digest is not None and digest(file) != expected_digest:
        invalid()
    file.unlink()
    store_state._fsync_directory(file.parent)


def epochs(
    initial: n.NamespaceInfo,
    current: n.NamespaceInfo,
    completion_rows: list[tuple],
    marker: dict[str, Any] | None,
    *,
    allow_pending: bool = False,
) -> list[tuple[int, n.StoreIdentity, str]]:
    """Validate immutable initial authority followed by exact safe transitions.

    The returned interval starts select historical ownership without changing
    a saved result or checking its old journal commitment against a new epoch.
    Only replacement's unavailable image-validation path permits a final
    incomplete row; ordinary admission always requires completed authority.
    """
    try:
        if any(
            getattr(initial, key) != getattr(current, key)
            for key in ("namespace_id", "root_id", "policy", "protection")
        ):
            invalid()
        n._integer(initial.revision)
        n._integer(current.revision)
        if initial.revision != 0:
            invalid()
        n._uuid(initial.database_generation)
        result = [(0, initial.cas, initial.database_generation)]
        generations = {initial.database_generation}
        operations = set()
        previous_revision = 0
        rows = sorted(completion_rows, key=lambda row: (row[4], row[0]))
        for index, row in enumerate(rows):
            if len(row) != 8:
                invalid()
            operation, generation, raw_binding, before, after, completed, predecessor, successor = (
                row
            )
            if any(
                not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None
                for digest in (predecessor, successor)
            ):
                invalid()
            n._uuid(operation)
            n._uuid(generation)
            n._integer(before)
            n._integer(after)
            if (
                operation in operations
                or generation in generations
                or generation in operations
                or generation in {operation, current.namespace_id, current.root_id}
                or operation in generations | {current.namespace_id, current.root_id}
                or before < previous_revision
                or after != before + 1
                or after > current.revision
                or type(completed) is not int
                or completed not in {0, 1}
                or (not completed and (not allow_pending or index != len(rows) - 1))
            ):
                invalid()
            binding = store_state._identity(n._decode(raw_binding))
            store_state._binding(
                {
                    "operation_id": operation,
                    "predecessor": result[-1][1].to_dict(),
                    "successor": binding.to_dict(),
                }
            )
            operations.add(operation)
            generations.add(generation)
            previous_revision = after
            result.append((after, binding, generation))
        if (current.cas, current.database_generation) != result[-1][1:]:
            invalid()
        if rows:
            validate(marker)
            last = rows[-1]
            if (
                marker["namespace_id"] != current.namespace_id
                or marker["operation_id"] != last[0]
                or marker["source_generation"] != result[-2][2]
                or marker["target_generation"] != last[1]
                or marker["predecessor_cas"] != result[-2][1].to_dict()
                or marker["successor_cas"] != result[-1][1].to_dict()
                or (not allow_pending and marker["stage"] != "complete")
            ):
                invalid()
        elif marker is not None:
            invalid()
        return result
    except (
        n.NamespaceError,
        store_state.StoreStateError,
        TypeError,
        ValueError,
        IndexError,
        KeyError,
    ):
        invalid()


def epoch_at(
    intervals: list[tuple[int, n.StoreIdentity, str]], revision: int
) -> tuple[n.StoreIdentity, str]:
    n._integer(revision)
    eligible = [item for item in intervals if item[0] <= revision]
    if not eligible:
        invalid()
    return eligible[-1][1:]
