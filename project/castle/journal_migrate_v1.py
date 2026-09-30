"""The isolated, once-only reader and migrator for v1 journal authority.

Ordinary Castle runtime remains strict journal v2. This module is loaded only
when a caller explicitly requests migration, validates the complete source
twice around the stable journal lock, and publishes canonical v2 bytes through
one atomic replacement.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from castle import cas, journal, store_state

__all__ = [
    "MIGRATION_RESULT_SCHEMA",
    "JournalMigrationError",
    "MigrationResult",
    "migrate_v1_to_v2",
]

MIGRATION_RESULT_SCHEMA = "migration.v1"

_V1_FIELDS = frozenset(
    {
        "at",
        "event",
        "cas_id",
        "size",
        "media_type",
        "source_root_id",
        "original_relpath",
        "mtime",
        "batch",
        "outcome",
    }
)


class JournalMigrationError(journal.CastleError):
    """A v1 journal could not be migrated safely."""

    code: str
    detail: str

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class MigrationResult:
    """The nondisclosing structural result of one completed migration."""

    root: Path
    source_sha256: str
    target_sha256: str
    event_count: int
    object_count: int

    def as_mapping(self) -> dict[str, Any]:
        """Return the exact stable migration-result mapping."""
        return {
            "schema": MIGRATION_RESULT_SCHEMA,
            "root": str(self.root),
            "from": "v1",
            "to": "v2",
            "journal_sha256_before": self.source_sha256,
            "journal_sha256_after": self.target_sha256,
            "event_count": self.event_count,
            "object_count": self.object_count,
        }


@dataclass(frozen=True)
class _MigrationPlan:
    root: Path
    source_bytes: bytes
    source_sha256: str
    records: tuple[dict[str, Any], ...]
    target_bytes: bytes
    target_sha256: str


def _absolute_spelling(path: Path) -> Path:
    path = Path(path)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def _canonical_store_root(path: Path) -> Path:
    absolute = _absolute_spelling(path)
    try:
        info = absolute.lstat()
    except OSError as exc:
        raise JournalMigrationError(
            "cas-migration-root-unusable",
            f"store root is not an existing directory: {absolute}: {exc}",
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise JournalMigrationError(
            "cas-migration-path-symlink",
            f"store root is a symlink: {absolute}",
        )
    if not stat.S_ISDIR(info.st_mode):
        raise JournalMigrationError(
            "cas-migration-root-unusable",
            f"store root is not an existing directory: {absolute}",
        )
    try:
        canonical = absolute.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise JournalMigrationError(
            "cas-migration-root-unusable",
            f"store root cannot be canonicalized: {absolute}: {exc}",
        ) from exc
    try:
        canonical_info = canonical.lstat()
    except OSError as exc:
        raise JournalMigrationError(
            "cas-migration-root-unusable",
            f"canonical store root is unusable: {canonical}: {exc}",
        ) from exc
    if not stat.S_ISDIR(canonical_info.st_mode):
        raise JournalMigrationError(
            "cas-migration-root-unusable",
            f"canonical store root is not a directory: {canonical}",
        )
    return canonical


def _require_no_store_symlinks(root: Path) -> None:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
        except OSError as exc:
            raise JournalMigrationError(
                "cas-migration-root-unusable",
                f"store tree cannot be enumerated at {directory}: {exc}",
            ) from exc
        for entry in entries:
            candidate = Path(entry.path)
            relative = candidate.relative_to(root).as_posix()
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise JournalMigrationError(
                    "cas-migration-root-unusable",
                    f"store entry cannot be inspected at {relative}: {exc}",
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise JournalMigrationError(
                    "cas-migration-path-symlink",
                    f"store contains a symlink at {relative}",
                )
            if stat.S_ISDIR(info.st_mode):
                pending.append(candidate)


def _parse_v1_record(record: object, where: str) -> dict[str, Any]:
    if not isinstance(record, dict):
        raise JournalMigrationError("cas-migration-shape", f"{where} is not an object")
    if "schema" in record or "event_id" in record:
        raise JournalMigrationError(
            "cas-migration-already-v2",
            f"{where} already carries v2 identity; migration is once-only",
        )
    if set(record) != _V1_FIELDS:
        raise JournalMigrationError(
            "cas-migration-shape",
            f"{where} is not an exact ten-field v1 ingest record",
        )
    if record["event"] != "ingest" or record["outcome"] not in {"stored", "dedup"}:
        raise JournalMigrationError("cas-migration-shape", f"{where} has an invalid event")
    if not isinstance(record["cas_id"], str):
        raise JournalMigrationError("cas-migration-shape", f"{where} has an invalid CAS ID")
    try:
        journal.hex_digest(record["cas_id"])
    except journal.CastleError as exc:
        raise JournalMigrationError("cas-migration-shape", f"{where}: {exc}") from exc
    if (
        isinstance(record["size"], bool)
        or not isinstance(record["size"], int)
        or record["size"] < 0
    ):
        raise JournalMigrationError("cas-migration-shape", f"{where} has an invalid size")
    for name in ("media_type", "source_root_id", "original_relpath", "mtime"):
        if not isinstance(record[name], str) or not record[name]:
            raise JournalMigrationError(
                "cas-migration-shape",
                f"{where} has invalid {name}",
            )
    try:
        journal.parse_journal_timestamp(record["at"], where)
    except journal.CastleError as exc:
        raise JournalMigrationError("cas-migration-shape", str(exc)) from exc
    original = Path(record["original_relpath"])
    if original.is_absolute() or ".." in original.parts:
        raise JournalMigrationError(
            "cas-migration-shape",
            f"{where} has an unsafe original path",
        )
    if record["batch"] is not None and not isinstance(record["batch"], str):
        raise JournalMigrationError("cas-migration-shape", f"{where} has an invalid batch")
    return dict(record)


def _read_v1_journal(
    journal_path: Path,
    *,
    expect_journal_sha256: str,
) -> tuple[bytes, str, list[dict[str, Any]]]:
    try:
        source_bytes = journal_path.read_bytes()
    except OSError as exc:
        raise JournalMigrationError(
            "cas-migration-journal-unreadable",
            f"{journal_path}: {exc}",
        ) from exc
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if source_sha256 != expect_journal_sha256:
        raise JournalMigrationError(
            "cas-migration-digest-mismatch",
            f"{journal_path} hashes {source_sha256}, the caller expected {expect_journal_sha256}",
        )
    try:
        text = source_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JournalMigrationError(
            "cas-migration-encoding",
            f"{journal_path} is not valid UTF-8",
        ) from exc
    if not text:
        raise JournalMigrationError(
            "cas-migration-empty",
            f"{journal_path} holds no events; there is nothing to migrate",
        )
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    records: list[dict[str, Any]] = []
    for ordinal, line in enumerate(lines, start=1):
        where = f"journal line {ordinal}"
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JournalMigrationError(
                "cas-migration-malformed",
                f"{where} is invalid JSON",
            ) from exc
        records.append(_parse_v1_record(parsed, where))
    return source_bytes, source_sha256, records


def _v1_event_id(ordinal: int, record: dict[str, Any]) -> str:
    if ordinal < 1 or ordinal > 9_999_999_999_999_999:
        raise JournalMigrationError(
            "cas-migration-ordinal",
            f"source line ordinal out of range: {ordinal}",
        )
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return f"v1:{ordinal:016d}:sha256:{digest}"


def _require_object_agreement(root: Path, records: Sequence[dict[str, Any]]) -> int:
    journaled = {record["cas_id"] for record in records}
    tree_ids, invalid_paths = cas._tree_inventory(root)
    if invalid_paths:
        raise JournalMigrationError(
            "cas-migration-object-tree",
            f"{root} holds invalid object paths: {', '.join(sorted(invalid_paths))}",
        )
    unjournaled = sorted(tree_ids - journaled)
    if unjournaled:
        raise JournalMigrationError(
            "cas-migration-object-tree",
            f"{root} holds objects absent from the journal: {', '.join(unjournaled)}",
        )
    return len(journaled)


def _plan_v1_to_v2(root: Path, *, expect_journal_sha256: str) -> _MigrationPlan:
    _require_no_store_symlinks(root)
    journal_path = root / journal.JOURNAL_FILENAME
    try:
        info = journal_path.lstat()
    except OSError as exc:
        raise JournalMigrationError(
            "cas-migration-journal-absent",
            f"{journal_path}: {exc}",
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise JournalMigrationError(
            "cas-migration-journal-unsafe",
            f"{journal_path} is not a regular file",
        )
    source_bytes, source_sha256, source_records = _read_v1_journal(
        journal_path,
        expect_journal_sha256=expect_journal_sha256,
    )
    migrated: list[dict[str, Any]] = []
    for ordinal, record in enumerate(source_records, start=1):
        target = {
            "schema": journal.CAS_JOURNAL_EVENT_SCHEMA,
            "event_id": _v1_event_id(ordinal, record),
            "at": record["at"],
            "event": record["event"],
            "cas_id": record["cas_id"],
            "size": record["size"],
            "media_type": record["media_type"],
            "source_root_id": record["source_root_id"],
            "original_relpath": record["original_relpath"],
            "batch": record["batch"],
            "outcome": record["outcome"],
        }
        try:
            migrated.append(journal.parse_journal_record(target, f"migrated event {ordinal}"))
        except journal.CastleError as exc:
            raise JournalMigrationError("cas-migration-invalid-fold", str(exc)) from exc
    try:
        ordered = sorted(migrated, key=journal.journal_sort_key)
        journal.validate_journal_sequence(ordered, "migrated event")
        cas.expected_index_rows(ordered)
    except journal.CastleError as exc:
        raise JournalMigrationError("cas-migration-invalid-fold", str(exc)) from exc
    _require_object_agreement(root, ordered)
    target_bytes = journal.canonical_journal_bytes(ordered)
    return _MigrationPlan(
        root=root,
        source_bytes=source_bytes,
        source_sha256=source_sha256,
        records=tuple(ordered),
        target_bytes=target_bytes,
        target_sha256=hashlib.sha256(target_bytes).hexdigest(),
    )


def _fsync_directory(root: Path) -> None:
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _stage_and_publish_journal(root: Path, target_bytes: bytes) -> None:
    journal_path = root / journal.JOURNAL_FILENAME
    handle = tempfile.NamedTemporaryFile(
        dir=root,
        prefix=".cas-journal-migrate.",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(target_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.read_bytes() != target_bytes:
            raise JournalMigrationError(
                "cas-migration-staging",
                "the staged journal did not read back as written",
            )
        os.replace(temporary, journal_path)
        _fsync_directory(root)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def migrate_v1_to_v2(
    root: Path,
    *,
    expect_journal_sha256: str,
) -> MigrationResult:
    """Convert one exactly authorized v1 journal to strict canonical v2."""
    canonical_root = _canonical_store_root(root)
    store_state._migration_lifecycle(canonical_root)
    planned = _plan_v1_to_v2(
        canonical_root,
        expect_journal_sha256=expect_journal_sha256,
    )
    with journal._stable_lock(canonical_root):
        store_state._migration_lifecycle(canonical_root)
        confirmed = _plan_v1_to_v2(
            canonical_root,
            expect_journal_sha256=expect_journal_sha256,
        )
        if (
            confirmed.source_bytes != planned.source_bytes
            or confirmed.target_bytes != planned.target_bytes
        ):
            raise JournalMigrationError(
                "cas-migration-source-changed",
                f"{canonical_root} changed between validation and migration",
            )
        _stage_and_publish_journal(canonical_root, confirmed.target_bytes)
        try:
            cas._rebuild_authority_index(canonical_root, confirmed.records)
        except cas.CasError as exc:
            raise JournalMigrationError(
                "cas-migration-index-rebuild",
                f"{canonical_root}: the journal is already migrated but its derived index "
                f"could not be rebuilt ({exc}); first call prepare_store offline, then repair with "
                f"castle rebuild-index --root {canonical_root}",
            ) from exc
    return MigrationResult(
        root=canonical_root,
        source_sha256=confirmed.source_sha256,
        target_sha256=confirmed.target_sha256,
        event_count=len(confirmed.records),
        object_count=len({record["cas_id"] for record in confirmed.records}),
    )
