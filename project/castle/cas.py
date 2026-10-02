"""The content-addressed object store and its rebuildable query index.

The append-only journal is authority; the SQLite database beside it is a
disposable cache that :func:`rebuild_index` reconstructs from journal truth
plus the immutable object tree. This module owns content identity and a
streaming file-hash helper, object layout as a pure function of identity,
byte-based media classification with its two admitted refinements, the sealed
read-only posture, the journal-to-index fold, verification, and the read
surface. Journal authority and local lifecycle admission are shared with the
journal and store-state modules.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import uuid
import zipfile
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from castle.journal import (
    CAS_JOURNAL_EVENT_SCHEMA,
    JOURNAL_FILENAME,
    CastleError,
    JournalError,
    append_record,
    ensure_store,
    hex_digest,
    journal_lock,
    monotonic_journal_timestamp,
    new_event_id,
    read_journal,
    require_appendable_journal,
)
from castle.store_state import StoreStateError, _require_active

VERIFY_OK = 0
VERIFY_CORRUPT_OBJECT = 10
VERIFY_MISSING_OBJECT = 11
VERIFY_INDEX_DRIFT = 12

#: The persisted name of the query cache. Named once here so the one spelling
#: that must match the stored file cannot drift across its many call sites.
INDEX_FILENAME = "cas.sqlite"
_SQLITE_SIDECARS = frozenset(INDEX_FILENAME + suffix for suffix in ("-wal", "-shm", "-journal"))

_OBJECT_NAME = re.compile(r"^[0-9a-f]{64}$")

#: The two extended attributes that mark a store the host sync daemon has been
#: told to leave alone. Named once here; the merge layer reads the same names
#: rather than restating them.
_SYNC_EXCLUSION_XATTRS = (
    "com.dropbox.ignored",
    "com.apple.fileprovider.ignore#P",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (
    cas_id TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    stored_relpath TEXT NOT NULL,
    first_seen_utc TEXT NOT NULL,
    batch TEXT
);
CREATE TABLE IF NOT EXISTS names (
    cas_id TEXT NOT NULL,
    source_root_id TEXT NOT NULL,
    original_relpath TEXT NOT NULL,
    recorded_utc TEXT NOT NULL
);
-- The fact a sighting asserts is "this content was known at this path in this
-- source". Re-observing it later is the same fact, so the triple is the row's
-- identity and a repeat ingest adds nothing. recorded_utc is provenance about
-- the observation and is not part of what makes a sighting distinct; the first
-- observation's value is the one kept, matching the journal fold's order.
CREATE UNIQUE INDEX IF NOT EXISTS names_identity
    ON names(cas_id, source_root_id, original_relpath);
CREATE INDEX IF NOT EXISTS names_cas_id ON names(cas_id);
CREATE INDEX IF NOT EXISTS names_relpath ON names(original_relpath);
CREATE TABLE IF NOT EXISTS remotes (
    cas_id TEXT NOT NULL,
    remote TEXT NOT NULL,
    key TEXT NOT NULL,
    uploaded_utc TEXT NOT NULL,
    checksum_verified_utc TEXT NOT NULL
);
"""

_REMOTES_COLUMNS = "cas_id, remote, key, uploaded_utc, checksum_verified_utc"


class CasError(CastleError):
    """The object store could not complete a requested operation safely."""


@dataclass(frozen=True)
class MediaRefinement:
    """Exact authority for one closed detector-metadata transition."""

    refinement: str
    from_media_type: str
    to_media_type: str


@dataclass(frozen=True)
class MediaDetection:
    """A detected media type bound to immutable content by a process-local proof."""

    cas_id: str
    size: int
    media_type: str
    _proof: str = field(repr=False)


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of one verification pass: an exit code and its reasons."""

    exit_code: int
    checked_objects: int
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.exit_code == VERIFY_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "checked_objects": self.checked_objects,
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------
# The typed receipt contract
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NameSighting:
    """One durable name observation for a content-addressed object."""

    source_root_id: str
    original_relpath: str
    recorded_utc: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> NameSighting:
        """Rebuild one sighting from an index row or a serialized receipt.

        A refusal here concerns a handed-in dict, not authoritative journal
        content, so it is the object-store error rather than the journal one.
        """
        required = {"source_root_id", "original_relpath", "recorded_utc"}
        if not isinstance(value, dict) or set(value) != required:
            raise CasError("name sighting has an invalid shape")
        if any(not isinstance(value[key], str) or not value[key] for key in required):
            raise CasError("name sighting has invalid text")
        relative = Path(value["original_relpath"])
        if relative.is_absolute() or ".." in relative.parts:
            raise CasError("name sighting has an unsafe original path")
        return cls(**value)


@dataclass(frozen=True)
class IngestReceipt:
    """A typed per-file ingest outcome with object and name provenance."""

    relpath: str
    source_root_id: str
    cas_id: str
    size: int
    media_type: str
    outcome: str
    object_first_seen_utc: str
    object_first_batch: str | None
    first_sighting: NameSighting
    prior_sightings: tuple[NameSighting, ...]

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["prior_sightings"] = [item.as_dict() for item in self.prior_sightings]
        return value

    @classmethod
    def from_dict(cls, value: Any) -> IngestReceipt:
        """Rebuild one receipt from its serialized form, refusing malformed input.

        Like the sighting deserializer, a refusal here is about a handed-in
        dict rather than journal content and so is the object-store error. The
        identity is validated through the shared digest gate, which raises the
        package-root error on a malformed value; that root error is translated
        to the object-store error so this boundary always refuses with one type.
        """
        required = {
            "relpath",
            "source_root_id",
            "cas_id",
            "size",
            "media_type",
            "outcome",
            "object_first_seen_utc",
            "object_first_batch",
            "first_sighting",
            "prior_sightings",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise CasError("ingest receipt has an invalid shape")
        text_keys = {
            "relpath",
            "source_root_id",
            "cas_id",
            "media_type",
            "outcome",
            "object_first_seen_utc",
        }
        if any(not isinstance(value[key], str) or not value[key] for key in text_keys):
            raise CasError("ingest receipt has invalid text")
        try:
            hex_digest(value["cas_id"])
        except CastleError as exc:
            raise CasError("ingest receipt has an invalid CAS ID") from exc
        relative = Path(value["relpath"])
        if relative.is_absolute() or ".." in relative.parts:
            raise CasError("ingest receipt has an unsafe relpath")
        if value["outcome"] not in {"stored", "dedup"}:
            raise CasError("ingest receipt has an invalid outcome")
        size = value["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise CasError("ingest receipt has an invalid size")
        if value["object_first_batch"] is not None and not isinstance(
            value["object_first_batch"], str
        ):
            raise CasError("ingest receipt has an invalid first batch")
        if not isinstance(value["prior_sightings"], list):
            raise CasError("ingest receipt prior_sightings must be a list")
        return cls(
            relpath=value["relpath"],
            source_root_id=value["source_root_id"],
            cas_id=value["cas_id"],
            size=value["size"],
            media_type=value["media_type"],
            outcome=value["outcome"],
            object_first_seen_utc=value["object_first_seen_utc"],
            object_first_batch=value["object_first_batch"],
            first_sighting=NameSighting.from_dict(value["first_sighting"]),
            prior_sightings=tuple(
                NameSighting.from_dict(item) for item in value["prior_sightings"]
            ),
        )


@dataclass
class IngestSummary:
    """Aggregate counts and receipts across one ingest walk."""

    files_seen: int = 0
    new_objects: int = 0
    dedup_hits: int = 0
    logical_bytes: int = 0
    physical_bytes: int = 0
    refusals: list[str] = field(default_factory=list)
    receipts: list[IngestReceipt] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["receipts"] = [receipt.as_dict() for receipt in self.receipts]
        return value


# --------------------------------------------------------------------------
# Content identity and object layout
# --------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    """Return the prefixed SHA-256 identity of *path* without loading it whole."""
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256")
    return f"sha256:{digest.hexdigest()}"


def object_relpath(cas_id: str) -> Path:
    """Return the store-relative object path for *cas_id*."""
    digest = hex_digest(cas_id)
    return Path("objects") / "sha256" / digest[:2] / digest[2:4] / digest


def object_path(store: Path, cas_id: str) -> Path:
    """Return the object path without consulting the index."""
    return Path(store) / object_relpath(cas_id)


def require_object(store: Path, cas_id: str) -> Path:
    """Return a present regular object's computed path, or fail closed."""
    _require_active(store)
    candidate = object_path(store, cas_id)
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError as exc:
        raise CasError(f"object is missing: {cas_id}") from exc
    if not stat.S_ISREG(mode):
        raise CasError(f"object is not a regular file: {cas_id}")
    return candidate


# --------------------------------------------------------------------------
# Byte-based media classification
# --------------------------------------------------------------------------

_OOXML_PACKAGE_TYPES = (
    (
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    (
        "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
)


def classify_ooxml_package(path: Path) -> str | None:
    """Return an OOXML media type read from package content, or ``None``.

    An OOXML document is a ZIP container, so the host magic-byte tool can only
    report ``application/zip``. The part the package actually carries is the
    authoritative discriminator; the filename suffix never is.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            names = frozenset(archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return None
    if "[Content_Types].xml" not in names:
        return None
    for member, media_type in _OOXML_PACKAGE_TYPES:
        if member in names:
            return media_type
    return None


def classify_media_type(path: Path) -> str:
    """Classify one readable file by magic bytes, refusing uncertain output."""
    try:
        result = subprocess.run(
            ["file", "--mime-type", "--brief", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CasError(f"file(1) could not inspect {path.name}") from exc
    media_type = result.stdout.strip().split(";", 1)[0]
    if not media_type or "/" not in media_type:
        raise CasError(f"file(1) returned no valid media type for {path.name}")
    if media_type == "application/zip":
        refined = classify_ooxml_package(path)
        if refined is not None:
            return refined
    return media_type


_KNOWN_MEDIA_REFINEMENTS = {
    (
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ): "ooxml-word-package",
    (
        "application/zip",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ): "ooxml-spreadsheet-package",
}


def known_media_refinement(
    previous_media_type: str,
    current_media_type: str,
) -> MediaRefinement | None:
    """Return exact authority for a named, closed media transition, or ``None``."""
    name = _KNOWN_MEDIA_REFINEMENTS.get((previous_media_type, current_media_type))
    if name is None:
        return None
    return MediaRefinement(name, previous_media_type, current_media_type)


_MEDIA_DETECTION_KEY = os.urandom(32)


def _media_detection_proof(cas_id: str, size: int, media_type: str) -> str:
    message = f"{cas_id}\0{size}\0{media_type}".encode()
    return hmac.new(_MEDIA_DETECTION_KEY, message, hashlib.sha256).hexdigest()


def detect_stored_media(store: Path, cas_id: str, expected_size: int) -> MediaDetection:
    """Classify one verified immutable object and authenticate the result."""
    stored = require_object(store, cas_id)
    size = stored.stat().st_size
    if size != expected_size or sha256_file(stored) != cas_id:
        raise CasError("stored object does not match expected immutable content")
    media_type = classify_media_type(stored)
    return MediaDetection(
        cas_id,
        size,
        media_type,
        _media_detection_proof(cas_id, size, media_type),
    )


def _validate_media_detection(
    detection: MediaDetection,
    cas_id: str,
    size: int,
) -> str:
    """Return the detected media type only if its proof still binds it here."""
    expected = _media_detection_proof(
        detection.cas_id,
        detection.size,
        detection.media_type,
    )
    if (
        detection.cas_id != cas_id
        or detection.size != size
        or not hmac.compare_digest(detection._proof, expected)
    ):
        raise CasError("media detection is not bound to expected CAS content")
    return detection.media_type


# --------------------------------------------------------------------------
# The sealed read-only posture
# --------------------------------------------------------------------------


def _write_protected(file_stat: os.stat_result, *, immutable_mask: int | None = None) -> bool:
    """Return whether owner writes are denied by mode or the OS immutable flag."""
    if file_stat.st_mode & 0o222 == 0:
        return True
    mask = getattr(stat, "UF_IMMUTABLE", 0) if immutable_mask is None else immutable_mask
    return bool(mask and getattr(file_stat, "st_flags", 0) & mask)


def _run_xattr(arguments: Sequence[str]) -> bytes:
    """Use the native Darwin attribute tool, refusing an unreadable result."""
    try:
        result = subprocess.run(
            ["/usr/bin/xattr", *arguments],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise CasError("sync-exclusion operation failed") from None
    if result.returncode != 0:
        raise CasError("sync-exclusion operation failed")
    return result.stdout


def _sync_exclusion_attributes(store: Path) -> set[str]:
    """Read declared attribute names without treating probe failure as absence."""
    try:
        attributes = set(_run_xattr([str(store)]).decode("utf-8").splitlines())
    except UnicodeError:
        raise CasError("sync-exclusion attributes are unreadable") from None
    return attributes.intersection(_SYNC_EXCLUSION_XATTRS)


@cache
def _store_requires_user_immutable(store: Path) -> bool:
    """Whether *store* carries a sync-exclusion attribute, memoized per path."""
    if not hasattr(os, "chflags") or not getattr(stat, "UF_IMMUTABLE", 0):
        return False
    try:
        return bool(_sync_exclusion_attributes(store))
    except CasError:
        return False


def _seal_object(store: Path, destination: Path, cas_id: str) -> None:
    """Reassert and verify the immutable posture of one published object."""
    if sha256_file(destination) != cas_id:
        raise CasError(f"existing object is corrupt: {cas_id}")
    read_only = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
    mask = getattr(stat, "UF_IMMUTABLE", 0)
    current = destination.lstat()
    needs_mode = stat.S_IMODE(current.st_mode) != read_only
    if needs_mode and hasattr(os, "chflags") and mask and getattr(current, "st_flags", 0) & mask:
        # A sync daemon may rewrite an already-immutable object's mode after
        # publication; the flag has to come off before chmod can repair it.
        os.chflags(destination, current.st_flags & ~mask)
    if needs_mode:
        destination.chmod(read_only)
    if _store_requires_user_immutable(store):
        current_flags = getattr(destination.stat(), "st_flags", 0)
        if not current_flags & mask:
            os.chflags(destination, current_flags | mask)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    if not _write_protected(destination.lstat()):
        raise CasError(f"published object remained writable: {cas_id}")


def seal_objects(store: Path, cas_ids: Sequence[str]) -> None:
    """Reassert and verify the immutable posture across a completed batch."""
    _require_active(store)
    store = Path(store)
    for cas_id in sorted(set(cas_ids)):
        destination = require_object(store, cas_id)
        _seal_object(store, destination, cas_id)


# --------------------------------------------------------------------------
# The SQLite query index and its reconstruction
# --------------------------------------------------------------------------


@contextmanager
def _connect_index(store: Path) -> Iterator[sqlite3.Connection]:
    """One index transaction on a connection that is closed on exit.

    A bare ``sqlite3.Connection`` used as a context manager commits or rolls back
    but never closes, so the file stayed open until garbage collection, and any
    caller that checks for open handles before a destructive step saw one."""
    connection = sqlite3.connect(Path(store) / INDEX_FILENAME, timeout=30)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_SCHEMA)
        with connection:
            yield connection
    finally:
        connection.close()


def expected_index_rows(
    records: Sequence[dict[str, Any]],
) -> tuple[dict[str, tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Fold the journal into the object and name rows the index must hold.

    A cross-record inconsistency for one identity is a defect in journal
    *content*, so it refuses as a journal error rather than a store one.
    """
    objects: dict[str, tuple[Any, ...]] = {}
    seen_sightings: set[tuple[str, str, str]] = set()
    names: list[tuple[Any, ...]] = []
    for record in records:
        cas_id = record["cas_id"]
        row = (
            cas_id,
            record["size"],
            record["media_type"],
            object_relpath(cas_id).as_posix(),
            record["at"],
            record.get("batch"),
        )
        if cas_id in objects:
            prior = objects[cas_id]
            if prior[1] != row[1] or prior[3] != row[3]:
                raise JournalError(f"inconsistent metadata for {cas_id}")
            if prior[2] != row[2]:
                refinement = known_media_refinement(prior[2], row[2])
                if refinement is None:
                    raise JournalError(f"inconsistent metadata for {cas_id}")
                objects[cas_id] = (
                    prior[0],
                    prior[1],
                    row[2],
                    prior[3],
                    prior[4],
                    prior[5],
                )
        else:
            objects[cas_id] = row
        sighting = (cas_id, record["source_root_id"], record["original_relpath"])
        if sighting not in seen_sightings:
            seen_sightings.add(sighting)
            names.append((*sighting, record["at"]))
    return objects, names


def _wal_checksum(data: bytes, order: str, sums: tuple[int, int]) -> tuple[int, int]:
    first, second = sums
    for left, right in struct.iter_unpack(order + "II", data):
        first = (first + left + second) & 0xFFFFFFFF
        second = (second + right + first) & 0xFFFFFFFF
    return first, second


def _inspection_image(current: Path) -> bytes:
    """Read a quiescent database and its committed WAL into process-local memory."""
    image = bytearray(current.read_bytes())
    if len(image) < 100 or image[:16] != b"SQLite format 3\0":
        raise sqlite3.DatabaseError("invalid database header")
    page_size = int.from_bytes(image[16:18], "big")
    page_size = 65536 if page_size == 1 else page_size
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise sqlite3.DatabaseError("invalid database page size")
    if image[18:20] not in (b"\x01\x01", b"\x02\x02"):
        raise sqlite3.DatabaseError("unsupported database format")
    rollback = current.with_name(current.name + "-journal")
    if _lexists(rollback):
        with rollback.open("rb") as handle:
            if handle.read(8) == b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7":
                raise sqlite3.DatabaseError("database requires rollback recovery")
    wal_path = current.with_name(current.name + "-wal")
    wal = wal_path.read_bytes() if _lexists(wal_path) else b""
    if wal:
        if len(wal) < 32 or image[18:20] != b"\x02\x02":
            raise sqlite3.DatabaseError("invalid WAL header")
        magic, version, wal_page_size = struct.unpack_from(">III", wal)
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000:
            raise sqlite3.DatabaseError("unsupported WAL format")
        if wal_page_size != page_size:
            raise sqlite3.DatabaseError("WAL page size mismatch")
        order = "<" if magic == 0x377F0682 else ">"
        sums = _wal_checksum(wal[:24], order, (0, 0))
        if sums != struct.unpack_from(">II", wal, 24):
            raise sqlite3.DatabaseError("invalid WAL header checksum")
        frames: list[tuple[int, int]] = []
        committed_frames = committed_pages = 0
        for offset in range(32, len(wal) - 24 - page_size + 1, 24 + page_size):
            page, pages = struct.unpack_from(">II", wal, offset)
            if not page or wal[offset + 8 : offset + 16] != wal[16:24]:
                break
            sums = _wal_checksum(wal[offset : offset + 8], order, sums)
            sums = _wal_checksum(wal[offset + 24 : offset + 24 + page_size], order, sums)
            if sums != struct.unpack_from(">II", wal, offset + 16):
                break
            frames.append((page, offset + 24))
            if pages:
                committed_frames, committed_pages = len(frames), pages
        if committed_frames:
            length = committed_pages * page_size
            if length > len(image) + committed_frames * page_size:
                raise sqlite3.DatabaseError("incomplete WAL database image")
            image.extend(b"\0" * max(0, length - len(image)))
            del image[length:]
            for page, offset in frames[:committed_frames]:
                if page <= committed_pages:
                    start = (page - 1) * page_size
                    image[start : start + page_size] = wal[offset : offset + page_size]
            image[28:32] = committed_pages.to_bytes(4, "big")
            image[92:96] = image[24:28]
    # sqlite3_deserialize requires rollback-mode header bytes. Only the private
    # memory image changes; the source database and WAL are never opened by SQLite.
    image[18:20] = b"\x01\x01"
    return bytes(image)


@contextmanager
def _inspect_index(current: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.deserialize(_inspection_image(current))
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA query_only=ON")
        yield connection


def _snapshot_remotes(store: Path) -> list[tuple[Any, ...]]:
    """Return every replica pin the current index holds, or refuse.

    The ``remotes`` table is the one part of the schema the journal does not
    imply: an upload to a remote is never journaled, so a rebuild that
    recreated only journal-derived rows would silently drop it. There is no
    "discard remotes to repair" path — an existing index whose rows cannot be
    read refuses here, before the authoritative index is touched, rather than
    lose custody metadata to a transient read error.
    """
    current = Path(store) / INDEX_FILENAME
    if not (current.exists() or current.is_symlink()):
        return []
    info = current.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise CasError("existing index is not a regular file; remotes cannot be preserved")
    try:
        with _inspect_index(current) as connection:
            rows = connection.execute(f"SELECT {_REMOTES_COLUMNS} FROM remotes").fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise CasError(f"existing index remotes cannot be snapshotted: {exc}") from exc
    return [tuple(row) for row in rows]


def _tree_inventory(store: Path) -> tuple[set[str], list[str]]:
    """Enumerate the object tree, separating valid identities from stray paths."""
    root = Path(store) / "objects" / "sha256"
    if not root.exists():
        return set(), []
    found: set[str] = set()
    invalid: list[str] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        if not _OBJECT_NAME.fullmatch(candidate.name):
            invalid.append(candidate.relative_to(store).as_posix())
            continue
        cas_id = f"sha256:{candidate.name}"
        if candidate == object_path(store, cas_id):
            found.add(cas_id)
        else:
            invalid.append(candidate.relative_to(store).as_posix())
    return found, invalid


class _UnsafeAuthority(CasError):
    """A store contains an entry that cannot be treated as owned payload."""


class _UnreadableRemotes(CasError):
    """Existing remote custody rows cannot be read without mutation."""


def _authority_paths(store: Path) -> dict[str, Path]:
    """Validate the full structural inventory, including partially removed payload."""
    allowed = {
        "objects",
        "journal.jsonl",
        "journal.lock",
        "cas.sqlite",
        "store.json",
        "retired.json",
        "excision-selection.json",
        ".store.json.tmp",
        ".retired.json.tmp",
        ".excision-selection.json.tmp",
    } | _SQLITE_SIDECARS
    found: dict[str, Path] = {}
    pending = [store]
    while pending:
        directory = pending.pop()
        for item in sorted(directory.iterdir()):
            relative = item.relative_to(store).parts
            try:
                info = item.lstat()
            except FileNotFoundError:
                # SQLite may remove a coordination file when its final reader
                # closes, even while the store's journal lock is held. Missing
                # authority or object paths must still refuse below the caller.
                if len(relative) == 1 and relative[0] in _SQLITE_SIDECARS:
                    continue
                raise
            if len(relative) == 1:
                valid = relative[0] in allowed
                directory_expected = relative[0] == "objects"
            else:
                directory_expected = len(relative) < 5
                valid = relative[0] == "objects" and relative[1] == "sha256"
                if len(relative) >= 3:
                    valid = valid and re.fullmatch(r"[0-9a-f]{2}", relative[2]) is not None
                if len(relative) >= 4:
                    valid = valid and re.fullmatch(r"[0-9a-f]{2}", relative[3]) is not None
                if len(relative) >= 5:
                    valid = (
                        valid
                        and len(relative) == 5
                        and _OBJECT_NAME.fullmatch(relative[4]) is not None
                        and relative[4].startswith(relative[2] + relative[3])
                    )
            if not valid or (
                not stat.S_ISDIR(info.st_mode)
                if directory_expected
                else not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            ):
                raise _UnsafeAuthority("unsafe authority inventory")
            if directory_expected:
                pending.append(item)
            elif len(relative) == 5:
                found["sha256:" + item.name] = item
    return found


def _validated_authority(
    store: Path, *, derived: bool = False
) -> tuple[list[dict[str, Any]], dict[str, Path], list[tuple[str, ...]]]:
    """Fully validate v2 payload without consulting ordinary lifecycle admission."""
    from castle.journal import JOURNAL_FILENAME, _read_authority_journal

    paths = _authority_paths(store)
    if not (store / JOURNAL_FILENAME).is_file() or not (store / "objects" / "sha256").is_dir():
        raise CasError("incomplete authority")
    records = _read_authority_journal(store)
    objects, names = expected_index_rows(records)
    if set(paths) != set(objects):
        raise CasError("authority inventory disagreement")
    try:
        requires_immutable = sys.platform == "darwin" and bool(_sync_exclusion_attributes(store))
    except CasError:
        raise _UnsafeAuthority("required object posture cannot be inspected") from None
    immutable_mask = getattr(stat, "UF_IMMUTABLE", 0)
    if requires_immutable and (not immutable_mask or not hasattr(os, "chflags")):
        raise _UnsafeAuthority("required object posture unavailable")
    for cas_id, path in paths.items():
        info = path.lstat()
        if (
            info.st_size != objects[cas_id][1]
            or sha256_file(path) != cas_id
            or not _write_protected(info)
        ):
            raise CasError("authority content disagreement")
        if requires_immutable and not getattr(info, "st_flags", 0) & immutable_mask:
            raise _UnsafeAuthority("required object posture unavailable")
    try:
        remotes = _snapshot_remotes(store)
    except (OSError, CasError, sqlite3.Error):
        raise _UnreadableRemotes("unreadable remote rows") from None
    if any(
        len(row) != 5 or any(not isinstance(value, str) for value in row) or row[0] not in objects
        for row in remotes
    ):
        raise CasError("invalid remote authority")
    if derived and _index_errors(store, objects, names):
        raise CasError("derived authority disagreement")
    return records, paths, remotes


def _write_fresh_index(
    store: Path, records: Sequence[dict[str, Any]], remotes: Sequence[tuple[str, ...]]
) -> None:
    """Construct a new destination database without importing predecessor pages."""
    objects, names = expected_index_rows(records)
    target = store / INDEX_FILENAME
    with target.open("xb"):
        pass
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.executescript(_SCHEMA)
        connection.executemany("INSERT INTO objects VALUES (?, ?, ?, ?, ?, ?)", objects.values())
        connection.executemany("INSERT INTO names VALUES (?, ?, ?, ?)", names)
        connection.executemany("INSERT INTO remotes VALUES (?, ?, ?, ?, ?)", remotes)
        connection.commit()
    with target.open("rb") as handle:
        os.fsync(handle.fileno())
    if _index_errors(store, objects, names) or Counter(_snapshot_remotes(store)) != Counter(
        remotes
    ):
        raise CasError("fresh index disagrees with retained authority")


def rebuild_index(store: Path) -> dict[str, int]:
    """Atomically reconstruct the query cache from the journal, under the lock.

    ``objects`` and ``names`` are rederived from journal truth; ``remotes`` is
    carried across intact. There is no merge-only switch: a knob deciding
    whether durable custody metadata survives is a knob that is one day off.
    """
    store = Path(store)
    with journal_lock(store):
        return rebuild_index_under_lock(store)


def rebuild_index_under_lock(store: Path) -> dict[str, int]:
    """Reconstruct the query cache for a caller already holding the journal lock.

    The file lock is per open file description, so a caller already inside the
    lock that re-entered the public entry point would block on itself. Merge
    publication needs the rebuild to run under the same lock that authorized
    the journal replacement, so the two entry points differ only in who takes
    the lock.
    """
    store = Path(store)
    _require_active(store)
    # Merge may have replaced the journal. Open by path to check and
    # synchronize the current inode while holding the stable sidecar lock.
    # Visible bytes after a failed append fsync do not establish durability.
    with (store / JOURNAL_FILENAME).open("r+", encoding="utf-8") as authority:
        require_appendable_journal(authority)
        records = read_journal(store)
        authority.flush()
        os.fsync(authority.fileno())
    return _rebuild_authority_index(store, records)


def _rebuild_authority_index(store: Path, records: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Rebuild validated v2 authority for ordinary runtime or isolated migration."""
    objects, names = expected_index_rows(records)
    remotes = _snapshot_remotes(store)
    tree_ids, invalid_paths = _tree_inventory(store)
    unjournaled = sorted(tree_ids - set(objects))
    if unjournaled or invalid_paths:
        details = unjournaled + sorted(invalid_paths)
        raise CasError("object tree contains paths absent from the journal: " + ", ".join(details))
    temporary = store / f".cas-{uuid.uuid4().hex}.sqlite"
    try:
        connection = sqlite3.connect(temporary)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            connection.executemany(
                """
                INSERT INTO objects
                    (cas_id, size, media_type, stored_relpath,
                     first_seen_utc, batch)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                objects.values(),
            )
            connection.executemany(
                """
                INSERT INTO names
                    (cas_id, source_root_id, original_relpath, recorded_utc)
                VALUES (?, ?, ?, ?)
                """,
                names,
            )
            connection.executemany(
                f"INSERT INTO remotes ({_REMOTES_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
                remotes,
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        current = store / INDEX_FILENAME
        if current.exists():
            with closing(sqlite3.connect(current)) as old_connection:
                old_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)
        Path(f"{temporary}-wal").unlink(missing_ok=True)
        Path(f"{temporary}-shm").unlink(missing_ok=True)
    # Read the remotes back from the replaced index rather than trusting the
    # insert: this is the defensive check that the rename-replace preserved them.
    if Counter(_snapshot_remotes(store)) != Counter(remotes):
        raise CasError("rebuilt index did not preserve the remotes snapshot")
    return {
        "objects": len(objects),
        "names": len(names),
        "remotes": len(remotes),
    }


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def _index_errors(
    store: Path,
    objects: dict[str, tuple[Any, ...]],
    names: list[tuple[Any, ...]],
) -> list[str]:
    """Return every way the on-disk index disagrees with the journal fold."""
    index_path = Path(store) / INDEX_FILENAME
    if not index_path.is_file():
        return ["index missing"]
    try:
        with _inspect_index(index_path) as connection:
            actual_objects = connection.execute(
                """
                SELECT cas_id, size, media_type, stored_relpath,
                       first_seen_utc, batch
                FROM objects
                """
            ).fetchall()
            actual_names = connection.execute(
                """
                SELECT cas_id, source_root_id, original_relpath, recorded_utc
                FROM names
                """
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return [f"index unreadable: {exc}"]
    errors: list[str] = []
    if Counter(actual_objects) != Counter(objects.values()):
        errors.append("objects table differs from journal")
    if Counter(actual_names) != Counter(names):
        errors.append("names table differs from journal")
    expected_ids = set(objects)
    tree_ids, invalid_paths = _tree_inventory(store)
    extras = sorted(tree_ids - expected_ids)
    if extras:
        errors.append(f"unjournaled objects: {', '.join(extras)}")
    if invalid_paths:
        errors.append(f"invalid object paths: {', '.join(sorted(invalid_paths))}")
    return errors


def verify(store: Path, *, sample: int | None = None) -> VerifyResult:
    """Verify objects plus journal/index agreement with stable failure codes."""
    store = Path(store)
    _require_active(store)
    try:
        records = read_journal(store)
        objects, names = expected_index_rows(records)
    except StoreStateError:
        raise
    except (OSError, CastleError) as exc:
        # A journal this module cannot read or fold is index drift, not an
        # exception out of verify: both the read and the fold raise the
        # package-root error, which is caught here to yield the drift code.
        return VerifyResult(VERIFY_INDEX_DRIFT, 0, (str(exc),))
    missing: list[str] = []
    corrupt: list[str] = []
    for cas_id, row in objects.items():
        candidate = object_path(store, cas_id)
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError:
            missing.append(f"missing object: {cas_id}")
            continue
        except OSError as exc:
            corrupt.append(f"object unreadable: {cas_id}: {exc}")
            continue
        if not stat.S_ISREG(candidate_stat.st_mode):
            corrupt.append(f"object is not a regular file: {cas_id}")
        elif candidate_stat.st_size != row[1]:
            corrupt.append(f"size mismatch: {cas_id}")
        elif not _write_protected(candidate_stat):
            corrupt.append(f"object is writable: {cas_id}")
    available = [cas_id for cas_id in sorted(objects) if object_path(store, cas_id).is_file()]
    selected = available if sample is None else available[:sample]
    already_corrupt = {message.rsplit(" ", 1)[-1] for message in corrupt}
    for cas_id in selected:
        try:
            mismatch = sha256_file(object_path(store, cas_id)) != cas_id
        except OSError as exc:
            corrupt.append(f"object unreadable: {cas_id}: {exc}")
            continue
        if cas_id not in already_corrupt and mismatch:
            corrupt.append(f"digest mismatch: {cas_id}")
    index_errors = _index_errors(store, objects, names)
    errors = tuple(missing + corrupt + index_errors)
    if missing:
        exit_code = VERIFY_MISSING_OBJECT
    elif corrupt:
        exit_code = VERIFY_CORRUPT_OBJECT
    elif index_errors:
        exit_code = VERIFY_INDEX_DRIFT
    else:
        exit_code = VERIFY_OK
    return VerifyResult(exit_code, len(selected), errors)


# --------------------------------------------------------------------------
# The read surface
# --------------------------------------------------------------------------


def names_for(store: Path, cas_id: str) -> list[dict[str, str]]:
    """Return every indexed name sighting for *cas_id* in durable order."""
    _require_active(store)
    hex_digest(cas_id)
    index_path = Path(store) / INDEX_FILENAME
    if not index_path.is_file():
        raise CasError("index is missing; run rebuild-index")
    with closing(sqlite3.connect(index_path)) as connection:
        rows = connection.execute(
            """
            SELECT source_root_id, original_relpath, recorded_utc
            FROM names WHERE cas_id = ? ORDER BY rowid
            """,
            (cas_id,),
        ).fetchall()
    return [
        {
            "source_root_id": row[0],
            "original_relpath": row[1],
            "recorded_utc": row[2],
        }
        for row in rows
    ]


def find_names(store: Path, substring: str) -> list[dict[str, str]]:
    """Return every indexed name whose original path contains *substring*."""
    _require_active(store)
    index_path = Path(store) / INDEX_FILENAME
    if not index_path.is_file():
        raise CasError("index is missing; run rebuild-index")
    with closing(sqlite3.connect(index_path)) as connection:
        rows = connection.execute(
            """
            SELECT cas_id, source_root_id, original_relpath
            FROM names
            WHERE instr(original_relpath, ?) > 0
            ORDER BY original_relpath, cas_id
            """,
            (substring,),
        ).fetchall()
    return [
        {"cas_id": row[0], "source_root_id": row[1], "original_relpath": row[2]} for row in rows
    ]


def _choose_name(store: Path, cas_id: str, selected: str | None) -> str:
    """Resolve the durable name to materialize under, or refuse ambiguity."""
    entries = names_for(store, cas_id)
    if not entries:
        raise CasError(f"no journaled name for {cas_id}")
    if selected is None:
        return entries[0]["original_relpath"]
    exact = [row for row in entries if row["original_relpath"] == selected]
    if len(exact) == 1:
        return exact[0]["original_relpath"]
    basenames = [row for row in entries if Path(row["original_relpath"]).name == selected]
    if len(basenames) == 1:
        return basenames[0]["original_relpath"]
    if len(basenames) > 1:
        raise CasError(f"name is ambiguous for {cas_id}: {selected}")
    raise CasError(f"name is not journaled for {cas_id}: {selected}")


def materialize(
    store: Path,
    cas_id: str,
    output: Path,
    *,
    selected_name: str | None = None,
    use_original_name: bool = False,
) -> Path:
    """Copy one object out to *output*, restoring writable owner permissions."""
    source = require_object(Path(store), cas_id)
    target = Path(output)
    if use_original_name or target.is_dir():
        original = _choose_name(Path(store), cas_id, selected_name)
        target /= Path(original).name
    elif selected_name is not None:
        raise CasError("--name requires --out with a directory or no path")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    return target


def object_stat(store: Path, cas_id: str) -> dict[str, Any]:
    """Return the indexed metadata and name sightings for one object."""
    _require_active(store)
    hex_digest(cas_id)
    index_path = Path(store) / INDEX_FILENAME
    if not index_path.is_file():
        raise CasError("index is missing; run rebuild-index")
    with closing(sqlite3.connect(index_path)) as connection:
        row = connection.execute(
            """
            SELECT cas_id, size, media_type, stored_relpath, first_seen_utc, batch
            FROM objects WHERE cas_id = ?
            """,
            (cas_id,),
        ).fetchone()
    if row is None:
        raise CasError(f"object is not indexed: {cas_id}")
    return {
        "cas_id": row[0],
        "size": row[1],
        "media_type": row[2],
        "stored_relpath": row[3],
        "first_seen_utc": row[4],
        "batch": row[5],
        "names": names_for(Path(store), cas_id),
    }


def store_stat(store: Path) -> dict[str, Any]:
    """Return object count, logical and physical byte totals, and last ingest."""
    _require_active(store)
    index_path = Path(store) / INDEX_FILENAME
    if not index_path.is_file():
        raise CasError("index is missing; run rebuild-index")
    with closing(sqlite3.connect(index_path)) as connection:
        objects, logical = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM objects"
        ).fetchone()
        last_ingest = connection.execute("SELECT MAX(recorded_utc) FROM names").fetchone()[0]
        cas_ids = [row[0] for row in connection.execute("SELECT cas_id FROM objects")]
    physical = sum(
        candidate.stat().st_blocks * 512
        for cas_id in cas_ids
        if (candidate := object_path(Path(store), cas_id)).is_file()
    )
    return {
        "objects": objects,
        "bytes_logical": logical,
        "bytes_physical": physical,
        "last_ingest": last_ingest,
    }


# --------------------------------------------------------------------------
# The write path: source helpers and staging
# --------------------------------------------------------------------------


def _lexists(path: Path) -> bool:
    """Return whether *path* is present, counting a dangling symlink as present."""
    return path.exists() or path.is_symlink()


def _same_volume(source: Path, destination_parent: Path) -> bool:
    """Return whether *source* and the destination parent share one device."""
    return source.stat().st_dev == destination_parent.stat().st_dev


def _exclusive_temp(parent: Path) -> Path:
    """Create and return a fresh, exclusively-owned staging temp under *parent*."""
    parent.mkdir(parents=True, exist_ok=True)
    while True:
        candidate = parent / f".cas-{uuid.uuid4().hex}.tmp"
        try:
            descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(descriptor)
        return candidate


def _clone_file(source: Path, destination: Path) -> bool:
    """Attempt a Darwin copy-on-write clone; return False when unavailable."""
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(
            ["cp", "-c", str(source), str(destination)],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _copy_and_verify(source: Path, destination: Path, cas_id: str) -> None:
    """Copy when cloning is unavailable, then verify the destination's bytes.

    The re-hash is the whole point of this path: a clone shares blocks and
    cannot silently corrupt, but a byte copy can, so the copied destination is
    hashed and only an identity match is allowed to stand.
    """
    shutil.copyfile(source, destination)
    if sha256_file(destination) != cas_id:
        raise CasError(f"copy verification failed for {source.name}")


def _stage_object(source: Path, destination: Path, cas_id: str) -> Path:
    """Stage one object into an exclusive temp, sealed read-only and fsynced.

    Attempts cloning on Darwin when source and destination share a volume,
    otherwise copies and re-hashes. Any failure drops the temp, so a refusal
    leaves no residue for the tree inventory to trip on.
    """
    temporary = _exclusive_temp(destination.parent)
    try:
        cloned = False
        if _same_volume(source, destination.parent):
            cloned = _clone_file(source, temporary)
        if not cloned:
            _copy_and_verify(source, temporary, cas_id)
        elif sha256_file(temporary) != cas_id:
            raise CasError(f"clone verification failed for {source.name}")
        temporary.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# The incremental index write
# --------------------------------------------------------------------------


def _validate_index_record(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    media_refinement: MediaRefinement | None,
) -> bool:
    """Check one append against the currently indexed object without mutating.

    Returns whether the append is an admitted media refinement (the caller then
    advances the row) rather than a plain insert-or-dedup. A metadata conflict
    is a disagreement about object content across records for one identity — the
    same invariant the journal fold enforces — so it refuses as journal content,
    not as an object-store error, and the two writers stay in lockstep.
    """
    row = connection.execute(
        "SELECT size, media_type FROM objects WHERE cas_id = ?",
        (record["cas_id"],),
    ).fetchone()
    if row is None:
        if media_refinement is not None:
            raise JournalError("media refinement requires an indexed object")
        return False
    indexed_size, indexed_media_type = row
    if indexed_size != record["size"]:
        raise JournalError(f"inconsistent metadata for {record['cas_id']}")
    if indexed_media_type == record["media_type"]:
        if media_refinement is not None:
            raise JournalError("media refinement does not match indexed metadata")
        return False
    expected = known_media_refinement(indexed_media_type, record["media_type"])
    if media_refinement is None or media_refinement != expected:
        raise JournalError(f"inconsistent metadata for {record['cas_id']}")
    return True


def _index_record(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    *,
    media_refinement: MediaRefinement | None,
) -> None:
    """Apply one validated append: advance the media row or insert the object, then the name."""
    advance_media = _validate_index_record(connection, record, media_refinement)
    if advance_media:
        connection.execute(
            "UPDATE objects SET media_type = ? WHERE cas_id = ?",
            (record["media_type"], record["cas_id"]),
        )
    else:
        connection.execute(
            """
            INSERT OR IGNORE INTO objects
                (cas_id, size, media_type, stored_relpath, first_seen_utc, batch)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                record["cas_id"],
                record["size"],
                record["media_type"],
                object_relpath(record["cas_id"]).as_posix(),
                record["at"],
                record.get("batch"),
            ),
        )
    connection.execute(
        """
        INSERT OR IGNORE INTO names
            (cas_id, source_root_id, original_relpath, recorded_utc)
        VALUES (?, ?, ?, ?)
        """,
        (
            record["cas_id"],
            record["source_root_id"],
            record["original_relpath"],
            record["at"],
        ),
    )


# --------------------------------------------------------------------------
# The write path proper
# --------------------------------------------------------------------------


def _ingest_one(
    store: Path,
    source: Path,
    original_relpath: str,
    source_root_id: str,
    batch: str | None,
    media_refinement: MediaRefinement | None = None,
    expected_cas_id: str | None = None,
    expected_size: int | None = None,
    media_detection: MediaDetection | None = None,
) -> tuple[IngestReceipt, int]:
    """Stage, publish, journal, and index one file; return its receipt and physical bytes.

    Refusals about the source, its media binding, staging, sealing, or
    publication are object-store errors; only the incremental index validator's
    metadata-consistency refusals are journal-content errors, matching the fold.
    """
    cas_id = sha256_file(source)
    size = source.stat().st_size
    if expected_cas_id is not None and cas_id != expected_cas_id:
        raise CasError("source identity changed before CAS mutation")
    if expected_size is not None and size != expected_size:
        raise CasError("source size changed before CAS mutation")
    if media_detection is not None and (expected_cas_id is None or expected_size is None):
        raise CasError("media detection requires expected CAS identity and size")
    media_type = (
        _validate_media_detection(media_detection, cas_id, size)
        if media_detection is not None
        else classify_media_type(source)
    )
    if media_refinement is not None or expected_cas_id is not None:
        if sha256_file(source) != cas_id:
            raise CasError("source identity changed during CAS preflight")
        if source.stat().st_size != size:
            raise CasError("source size changed during CAS preflight")
    destination = object_path(store, cas_id)
    temporary: Path | None = None
    published_unjournaled: Path | None = None
    if media_refinement is not None and not _lexists(destination):
        raise CasError("media refinement requires an existing object")
    # Establish lifecycle authority before staging makes an absent root nonempty.
    ensure_store(store)
    # Stage only after admission under the stable lock: inventory checks
    # must distinguish unresolved residue from another writer's active temp.
    # Cleanup owns a published object only until append is attempted. Once a
    # write/flush/fsync can have exposed a record, retain both blob and journal
    # evidence on failure; uncertainty is not proof that the blob is unowned.
    with journal_lock(store) as journal:
        try:
            # Admit only strict, appendable authority before staging or
            # publication. Failures remain visible to the per-file collector.
            journal_tail = require_appendable_journal(journal)
            records = read_journal(store)
            objects, names = expected_index_rows(records)
            if not _lexists(store / INDEX_FILENAME):
                if records:
                    raise CasError("index missing; rebuild-index required before ingest")
                with _connect_index(store):
                    pass
            errors = _index_errors(store, objects, names)
            if any(not object_path(store, identity).is_file() for identity in objects):
                errors.append("journaled object missing")
            if errors:
                inventory_error = any(
                    error.startswith(
                        (
                            "unjournaled objects:",
                            "invalid object paths:",
                            "journaled object missing",
                        )
                    )
                    for error in errors
                )
                remedy = (
                    "; rebuild-index cannot resolve object inventory; "
                    "operator action required before ingest"
                    if inventory_error
                    else "; rebuild-index and verify required before ingest"
                )
                raise CasError("; ".join(errors) + remedy)
            # Resynchronize even a repeated sighting whose folded index rows
            # did not change. No new publication precedes this checkpoint.
            journal.flush()
            os.fsync(journal.fileno())
            with _connect_index(store) as connection:
                object_row = connection.execute(
                    "SELECT first_seen_utc, batch FROM objects WHERE cas_id = ?",
                    (cas_id,),
                ).fetchone()
                prior_rows = connection.execute(
                    """
                    SELECT source_root_id, original_relpath, recorded_utc
                    FROM names WHERE cas_id = ? ORDER BY rowid
                    """,
                    (cas_id,),
                ).fetchall()
            created = False
            if _lexists(destination):
                destination_mode = destination.lstat().st_mode
                if not stat.S_ISREG(destination_mode) or sha256_file(destination) != cas_id:
                    raise CasError(f"existing object is corrupt: {cas_id}")
                if not _write_protected(destination.lstat()):
                    same_batch = (
                        batch is not None and object_row is not None and object_row[1] == batch
                    )
                    if not same_batch:
                        raise CasError(f"existing object is corrupt: {cas_id}")
                    _seal_object(store, destination, cas_id)
                    if not _write_protected(destination.lstat()):
                        raise CasError(f"existing object remained writable: {cas_id}")
            else:
                temporary = _stage_object(source, destination, cas_id)
                os.rename(temporary, destination)
                temporary = None
                # Before the append attempt, failures can safely remove this
                # new object. Reassert posture after rename because a sync-managed
                # parent may rewrite cloned-file metadata on publication.
                published_unjournaled = destination
                _seal_object(store, destination, cas_id)
                if not _write_protected(destination.lstat()):
                    raise CasError(f"published object remained writable: {cas_id}")
                created = True
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            record = {
                "schema": CAS_JOURNAL_EVENT_SCHEMA,
                # Minted inside the locked boundary, beside the timestamp it is
                # ordered with, so two writers cannot mint against the same tail.
                "event_id": new_event_id(),
                "at": monotonic_journal_timestamp(journal_tail),
                "event": "ingest",
                "cas_id": cas_id,
                "size": size,
                "media_type": media_type,
                "source_root_id": source_root_id,
                "original_relpath": original_relpath,
                "batch": batch,
                "outcome": "stored" if created else "dedup",
            }
            with _connect_index(store) as connection:
                # Validate before the append so a rejected record never lands in
                # the journal ahead of an index that would refuse it.
                _validate_index_record(connection, record, media_refinement)
                # The append may become visible before it reports failure.
                # Retaining bytes is safe; deleting a referenced blob is not.
                published_unjournaled = None
                append_record(journal, record)
                # Only successful append synchronization permits index update
                # and a receipt. An uncertain append returns its original error.
                _index_record(connection, record, media_refinement=media_refinement)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if published_unjournaled is not None:
                # Sealing may have set Darwin's deletion-prohibiting flag. Only
                # this preappend-owned object can be unsealed for safe cleanup.
                flags = getattr(published_unjournaled.lstat(), "st_flags", 0)
                mask = getattr(stat, "UF_IMMUTABLE", 0)
                if hasattr(os, "chflags") and flags & mask:
                    os.chflags(published_unjournaled, flags & ~mask)
                published_unjournaled.unlink(missing_ok=True)
    current_sighting = NameSighting(
        source_root_id=source_root_id,
        original_relpath=original_relpath,
        recorded_utc=record["at"],
    )
    prior_sightings = tuple(NameSighting(*row) for row in prior_rows)
    receipt = IngestReceipt(
        relpath=original_relpath,
        source_root_id=source_root_id,
        cas_id=cas_id,
        size=size,
        media_type=media_type,
        outcome=record["outcome"],
        object_first_seen_utc=object_row[0] if object_row is not None else record["at"],
        object_first_batch=object_row[1] if object_row is not None else batch,
        first_sighting=prior_sightings[0] if prior_sightings else current_sighting,
        prior_sightings=prior_sightings,
    )
    physical = destination.stat().st_blocks * 512 if created else 0
    return receipt, physical


def _validate_roots(store: Path, source: Path) -> tuple[Path, Path]:
    """Resolve *store* and *source* and require them to be disjoint.

    A source may be a directory to walk or a single regular file to snapshot on
    its own. A symlink is refused here rather than followed, matching the walk's
    treatment of one it finds inside a tree.
    """
    # Symlink-ness is tested on the path as given, before resolution: resolve()
    # follows the link, so a check made afterwards can never fire.
    if source.is_symlink():
        raise CasError("source must be a directory or a regular file")
    source = source.resolve(strict=True)
    store = store.resolve(strict=False)
    if not (source.is_dir() or source.is_file()):
        raise CasError("source must be a directory or a regular file")
    if store == source or store.is_relative_to(source) or source.is_relative_to(store):
        raise CasError("store and source trees must be disjoint")
    return store, source


def ingest(
    store: Path,
    source: Path,
    source_root_id: str,
    *,
    batch: str | None = None,
    dry_run: bool = False,
) -> IngestSummary:
    """Snapshot every regular file beneath *source* into *store*.

    *source* may instead be a single regular file, which is snapshotted on its
    own under its base name. Every other behaviour is the walk's, unchanged: the
    same counting, the same receipts, the same sealing pass, and the same
    summary shape, so a caller reads one result whichever kind of source it
    named.

    Symlinks are refused by name and a per-file failure — including an
    unappendable journal or an index inconsistency — is collected into the
    summary rather than aborting the walk, so one bad file does not sink an
    otherwise-good tranche. With *dry_run* the walk only classifies and counts:
    it takes no lock, publishes nothing, and writes no journal or index.
    """
    _require_active(store)
    if (
        not source_root_id.strip()
        or Path(source_root_id).is_absolute()
        or "\n" in source_root_id
        or "\r" in source_root_id
    ):
        raise CasError("source_root_id must be a non-empty opaque label")
    store, source = _validate_roots(Path(store), Path(source))
    summary = IngestSummary()
    # A single-file source is one candidate named by its basename; a directory
    # is its sorted walk. Everything downstream is identical either way.
    candidates = [source] if source.is_file() else sorted(source.rglob("*"))
    for candidate in candidates:
        relative = (
            candidate.name if candidate == source else candidate.relative_to(source).as_posix()
        )
        if candidate.is_symlink() or not candidate.is_file():
            if candidate.is_symlink():
                summary.refusals.append(f"{relative}: symlinks are refused")
            continue
        summary.files_seen += 1
        try:
            cas_id = sha256_file(candidate)
            size = candidate.stat().st_size
            summary.logical_bytes += size
            if dry_run:
                classify_media_type(candidate)
                if object_path(store, cas_id).exists():
                    summary.dedup_hits += 1
                else:
                    summary.new_objects += 1
                continue
            receipt, physical = _ingest_one(
                store,
                candidate,
                relative,
                source_root_id,
                batch,
            )
            summary.receipts.append(receipt)
            if receipt.outcome == "stored":
                summary.new_objects += 1
                summary.physical_bytes += physical
            else:
                summary.dedup_hits += 1
        except StoreStateError:
            raise
        except (OSError, CastleError, sqlite3.Error) as exc:
            # The re-point that makes the collector see a journal-content or
            # root refusal as this file's outcome instead of an aborting escape:
            # the object-store and journal-content errors are siblings under one
            # root, so catching the object-store child alone would miss the
            # journal one and sink the whole walk.
            summary.refusals.append(f"{relative}: {exc}")
    created_ids = {receipt.cas_id for receipt in summary.receipts if receipt.outcome == "stored"}
    for cas_id in sorted(created_ids):
        try:
            seal_objects(store, [cas_id])
        except StoreStateError:
            raise
        except (OSError, CastleError) as exc:
            summary.refusals.append(f"store-finalize: {exc}")
    return summary


def ingest_file(
    store: Path,
    source: Path,
    source_root_id: str,
    *,
    original_relpath: str | None = None,
    batch: str | None = None,
    media_refinement: MediaRefinement | None = None,
    expected_cas_id: str | None = None,
    expected_size: int | None = None,
    media_detection: MediaDetection | None = None,
) -> IngestReceipt:
    """Ingest exactly one file and return its typed durable receipt."""
    _require_active(store)
    if (
        not source_root_id.strip()
        or Path(source_root_id).is_absolute()
        or "\n" in source_root_id
        or "\r" in source_root_id
    ):
        raise CasError("source_root_id must be a non-empty opaque label")
    store = Path(store).resolve()
    source = Path(source).resolve()
    if not source.is_file() or source.is_symlink():
        raise CasError(f"source is not a regular file: {source}")
    store.parent.mkdir(parents=True, exist_ok=True)
    if store == source or store in source.parents:
        raise CasError("CAS store cannot contain the source file")
    relative = original_relpath or source.name
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise CasError("original_relpath must be a safe relative path")
    # Bootstrap the store tree before the write path. `_ingest_one` refuses on
    # several source and media-binding preconditions before it ever takes the
    # lock whose acquisition would otherwise create the tree, yet a store
    # created on write is the observable posture after such a refusal — so this
    # call is load-bearing, not redundant with the lock's own bootstrap.
    ensure_store(store)
    receipt, _physical = _ingest_one(
        store,
        source,
        relative_path.as_posix(),
        source_root_id,
        batch,
        media_refinement,
        expected_cas_id,
        expected_size,
        media_detection,
    )
    return receipt


# --------------------------------------------------------------------------
# Receipt reconstruction
# --------------------------------------------------------------------------


def sightings_for(store: Path, cas_id: str) -> tuple[NameSighting, ...]:
    """Return every indexed name sighting for *cas_id* in durable order."""
    return tuple(NameSighting.from_dict(row) for row in names_for(store, cas_id))


def receipt_for(
    store: Path,
    cas_id: str,
    *,
    source_root_id: str | None = None,
    relpath: str | None = None,
) -> IngestReceipt:
    """Reconstruct the latest matching receipt from authoritative journal state.

    The requested identity is validated through the shared digest gate, which
    raises the package-root error on a malformed value; that root error is
    translated to the object-store error so a bad requested identity refuses
    with the same type an absent one does.
    """
    _require_active(store)
    try:
        hex_digest(cas_id)
    except CastleError as exc:
        raise CasError("CAS ID must be sha256: followed by 64 lowercase hex digits") from exc
    records = read_journal(Path(store))
    matches = [
        row
        for row in records
        if row["cas_id"] == cas_id
        and (source_root_id is None or row["source_root_id"] == source_root_id)
        and (relpath is None or row["original_relpath"] == relpath)
    ]
    if not matches:
        raise CasError(f"no matching ingest receipt for {cas_id}")
    all_rows = [row for row in records if row["cas_id"] == cas_id]
    objects, _names = expected_index_rows(all_rows)
    current = matches[-1]
    current_index = max(index for index, row in enumerate(all_rows) if row is current)
    prior = tuple(
        NameSighting(
            source_root_id=row["source_root_id"],
            original_relpath=row["original_relpath"],
            recorded_utc=row["at"],
        )
        for row in all_rows[:current_index]
    )
    first_row = all_rows[0]
    object_row = objects[cas_id]
    first_sighting = NameSighting(
        source_root_id=first_row["source_root_id"],
        original_relpath=first_row["original_relpath"],
        recorded_utc=first_row["at"],
    )
    return IngestReceipt(
        relpath=current["original_relpath"],
        source_root_id=current["source_root_id"],
        cas_id=cas_id,
        size=object_row[1],
        media_type=object_row[2],
        outcome=current["outcome"],
        object_first_seen_utc=first_row["at"],
        object_first_batch=first_row.get("batch"),
        first_sighting=first_sighting,
        prior_sightings=prior,
    )
