"""The authoritative append-only journal: strict v2 events and the stable lock.

The journal is authority. Every runtime event is one line of canonical UTF-8
JSON in an append-only ``journal.jsonl``; the SQLite index is a disposable
cache reconstructed from this file plus the immutable object tree.

``journal.v2`` is the only shape this module reads or
writes. There is no compatibility reader: an unknown field refuses exactly as
a missing one does, so a v1 record fails on shape before anything else looks
at it.

This module defines the package-root exception. Lifecycle checks import the
state module only inside entry points, after journal initialization.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import threading
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO

#: The one runtime journal event schema. The owning parser supplies context;
#: the discriminator names the record family and its structural version.
CAS_JOURNAL_EVENT_SCHEMA = "journal.v2"

JOURNAL_FILENAME = "journal.jsonl"

#: The stable lock inode. Locking the journal itself was correct only while the
#: journal was append-only in place; the moment a merge replaces it with an
#: atomic rename, a holder of the old inode is holding a file nobody will ever
#: write to again. This sidecar is never replaced, carries no content, and is
#: not authority — see :func:`journal_lock_is_safe`.
JOURNAL_LOCK_FILENAME = "journal.lock"

_JOURNAL_FIELDS = frozenset(
    {
        "schema",
        "event_id",
        "at",
        "event",
        "cas_id",
        "size",
        "media_type",
        "source_root_id",
        "original_relpath",
        "batch",
        "outcome",
    }
)

#: The two native v2 event-identity grammars. A migrated identity is not a
#: compatibility shim: it is a v2 identity whose bytes happen to encode where it
#: came from, which is what makes an independent second migration of the same v1
#: journal produce the same identities.
_EVENT_ID_MINTED = re.compile(
    r"^uuid:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_EVENT_ID_MIGRATED = re.compile(r"^v1:[0-9]{16}:sha256:[0-9a-f]{64}$")

_CAS_ID = re.compile(r"^sha256:([0-9a-f]{64})$")

_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}


class CastleError(RuntimeError):
    """A Castle store could not complete a requested operation safely."""


class JournalError(CastleError):
    """The authoritative journal is malformed or internally inconsistent."""


def hex_digest(cas_id: str) -> str:
    """Return the bare 64-hex digest of *cas_id*, or refuse."""
    match = _CAS_ID.fullmatch(cas_id)
    if match is None:
        raise CastleError("CAS ID must be sha256: followed by 64 lowercase hex digits")
    return match.group(1)


def _iso_utc(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def new_event_id() -> str:
    """Mint one fresh runtime event identity."""
    return f"uuid:{uuid.uuid4()}"


def validate_event_id(value: object, where: str) -> str:
    """Return one well-formed v2 event identity, or refuse."""
    if not isinstance(value, str) or not (
        _EVENT_ID_MINTED.fullmatch(value) or _EVENT_ID_MIGRATED.fullmatch(value)
    ):
        raise JournalError(f"{where} has an invalid event id")
    return value


def parse_journal_timestamp(value: object, where: str) -> datetime:
    """Parse one strict UTC journal timestamp, or refuse.

    Parsed rather than compared as text, because the two spellings a UTC
    instant takes here do not sort as their instants do: ``…:00Z`` and
    ``…:00.000001Z`` compare in the opposite direction as strings, so a
    lexical order over these values is not a chronological one.
    """
    if not isinstance(value, str) or not value.endswith("Z"):
        raise JournalError(f"{where} has an invalid UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise JournalError(f"{where} has an invalid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise JournalError(f"{where} has an invalid UTC timestamp")
    return parsed


def journal_sort_key(record: dict[str, Any]) -> tuple[datetime, str]:
    """The one canonical order for journal events: ``(parsed at, event_id)``."""
    return (
        parse_journal_timestamp(record["at"], "journal record"),
        str(record["event_id"]),
    )


def canonical_journal_line(record: dict[str, Any]) -> str:
    """Serialize one record as its canonical single-line JSON form.

    ``ensure_ascii`` keeps its default of ``True``. That is a persisted-format
    decision, not a style one: flipping it would change the bytes already
    written for every record whose text fields are not pure ASCII.
    """
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def canonical_journal_bytes(records: Sequence[dict[str, Any]]) -> bytes:
    """Serialize a complete journal in canonical order and canonical encoding."""
    ordered = sorted(records, key=journal_sort_key)
    return "".join(canonical_journal_line(record) + "\n" for record in ordered).encode("utf-8")


def parse_journal_record(record: object, where: str) -> dict[str, Any]:
    """Validate one strict ``journal.v2`` record, or refuse.

    Strict in both directions: an unknown field refuses exactly as a missing
    one does. A v1 record therefore fails on shape before anything else looks
    at it, which is what "no compatibility reader" means in practice.
    """
    if not isinstance(record, dict) or set(record) != set(_JOURNAL_FIELDS):
        raise JournalError(f"{where} has an invalid shape")
    if record["schema"] != CAS_JOURNAL_EVENT_SCHEMA:
        raise JournalError(f"{where} is not {CAS_JOURNAL_EVENT_SCHEMA}")
    validate_event_id(record["event_id"], where)
    if not isinstance(record["cas_id"], str):
        raise JournalError(f"{where} has an invalid CAS ID")
    if (
        record["event"] != "ingest"
        or not isinstance(record["outcome"], str)
        or record["outcome"] not in {"stored", "dedup"}
    ):
        raise JournalError(f"{where} has an invalid event")
    hex_digest(record["cas_id"])
    if (
        isinstance(record["size"], bool)
        or not isinstance(record["size"], int)
        or record["size"] < 0
    ):
        raise JournalError(f"{where} has an invalid size")
    text_fields = ("media_type", "source_root_id", "original_relpath")
    if any(not isinstance(record[name], str) or not record[name] for name in text_fields):
        raise JournalError(f"{where} has invalid text")
    parse_journal_timestamp(record["at"], where)
    original = Path(record["original_relpath"])
    if original.is_absolute() or ".." in original.parts:
        raise JournalError(f"{where} has an unsafe original path")
    if record["batch"] is not None and not isinstance(record["batch"], str):
        raise JournalError(f"{where} has an invalid batch")
    return record


def validate_journal_sequence(
    records: Sequence[dict[str, Any]], label: str
) -> list[dict[str, Any]]:
    """Require unique identities in canonical order across a whole journal."""
    seen: dict[str, int] = {}
    previous: tuple[datetime, str] | None = None
    for position, record in enumerate(records, start=1):
        event_id = str(record["event_id"])
        if event_id in seen:
            raise JournalError(
                f"{label} {position} repeats the event id recorded at {seen[event_id]}"
            )
        seen[event_id] = position
        key = journal_sort_key(record)
        if previous is not None and key < previous:
            raise JournalError(f"{label} {position} breaks canonical (at, event_id) order")
        previous = key
    return list(records)


def read_journal(store: Path) -> list[dict[str, Any]]:
    """Read and strictly validate the whole journal of *store*."""
    from castle.store_state import _require_active

    _require_active(store)
    return _read_authority_journal(store)


def _read_authority_journal(store: Path) -> list[dict[str, Any]]:
    """Read v2 authority for a caller that owns lifecycle admission."""
    journal_path = Path(store) / JOURNAL_FILENAME
    if not journal_path.is_file():
        return []
    try:
        lines = journal_path.read_text(encoding="utf-8").splitlines()
    except UnicodeError as exc:
        raise JournalError("journal is not valid UTF-8") from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        where = f"journal line {line_number}"
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JournalError(f"{where} is invalid JSON") from exc
        records.append(parse_journal_record(record, where))
    return validate_journal_sequence(records, "journal line")


def ensure_store(store: Path) -> None:
    """Bring the store's bootstrap directories into being.

    This is store *bootstrap*, not object *layout*. The store-relative path an
    object takes is a pure function of its identity and belongs to
    ``castle.cas.object_relpath``, which this module cannot import; what lives
    here is only the pair of directories :func:`journal_lock` needs to exist
    before it can take a lock. Both spellings of ``objects/sha256`` must agree,
    and ``castle.cas`` is the authority on it.
    """
    from castle.store_state import _bootstrap

    _bootstrap(store)


def _thread_lock_for_key(key: str) -> threading.Lock:
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


def _thread_lock(store: Path) -> threading.Lock:
    return _thread_lock_for_key(str(store.resolve()))


def _canonical_thread_lock(store: Path) -> threading.Lock:
    return _thread_lock_for_key(str(store))


def journal_lock_is_safe(path: Path) -> bool:
    """Return whether *path* is exactly the expected inert lock sidecar.

    Deliberately narrow. A root enumerator that ignored anything *named*
    ``journal.lock`` would ignore a symlink pointing out of the store and a
    file somebody had written authority into, so the name is only the first of
    five conditions: regular, not a symlink, single-link, and zero bytes are the rest.
    """
    path = Path(path)
    if path.name != JOURNAL_LOCK_FILENAME:
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size == 0 and info.st_nlink == 1


@contextmanager
def journal_lock(store: Path) -> Iterator[TextIO]:
    """Hold the store's stable lock, then yield its journal handle.

    The order is the whole point. The sidecar is acquired first and the
    journal is opened only under it, so no caller ever holds a journal handle
    it did not take the lock for -- and the journal exists by the time the body
    runs even when the body appends nothing, which is what keeps an
    interrupted-lock window from looking like a store with artifacts and no
    authority.

    The second acquirer of a held lock **blocks** until release:
    ``flock(LOCK_EX)`` without ``LOCK_NB``.
    """
    with _journal_lock(store, canonical=False) as journal:
        yield journal


@contextmanager
def _canonical_journal_lock(store: Path) -> Iterator[TextIO]:
    """Hold the stable lock for a root already resolved by its caller."""
    with _journal_lock(store, canonical=True) as journal:
        yield journal


@contextmanager
def _journal_lock(store: Path, *, canonical: bool) -> Iterator[TextIO]:
    from castle.store_state import _require_active

    store = Path(store)
    ensure_store(store)
    with _stable_lock(store, canonical=canonical):
        _require_active(store, allow_absent=False)
        with (store / JOURNAL_FILENAME).open("a+", encoding="utf-8") as handle:
            yield handle


@contextmanager
def _stable_lock(store: Path, *, canonical: bool = True, create: bool = False) -> Iterator[None]:
    """Acquire only the stable inode; never create directories or open authority."""
    store = Path(store)
    lock_path = store / JOURNAL_LOCK_FILENAME
    thread_lock = _canonical_thread_lock(store) if canonical else _thread_lock(store)
    with thread_lock:
        try:
            # ``O_NONBLOCK`` is what makes the sidecar shape check below
            # reachable for every shape it names. Opening a FIFO read-only
            # without it does not refuse -- it blocks until some writer
            # appears, so a FIFO planted at this path would hang the process
            # instead of being rejected as "not an inert zero-byte regular
            # file". On a regular file the flag is inert, and it does not make
            # ``flock`` non-blocking: only ``LOCK_NB`` does that.
            descriptor = os.open(
                lock_path,
                os.O_RDONLY | (os.O_CREAT if create else 0) | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o644,
            )
        except OSError as exc:
            raise CastleError(f"journal lock is unusable: {exc}") from exc
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != 0 or info.st_nlink != 1:
                raise CastleError("journal lock sidecar is not an inert zero-byte regular file")
            # Acquisition and release are written as module-attribute lookups
            # (``fcntl.flock``), never as a bound ``from fcntl import flock``.
            # The mutual-exclusion proof in ``project/tests/test_journal.py``
            # observes acquisition by replacing ``fcntl.flock`` in the child
            # interpreter with a recording wrapper that delegates to the real
            # call; the attribute form resolves that wrapper at call time, so
            # the observation does not depend on how or when the patch was
            # applied. ``test_journal_module_calls_flock_through_the_fcntl_module``
            # holds the form.
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                if create:
                    os.fsync(descriptor)
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _last_journal_line(journal: TextIO) -> str | None:
    """Return the journal's final record line without reading the whole file.

    Positional reads off the descriptor, so the text handle's own append
    position is untouched. Reading the entire journal to learn its last
    timestamp would make an N-file ingest quadratic in the journal it is
    growing.
    """
    descriptor = journal.fileno()
    size = os.fstat(descriptor).st_size
    if size == 0:
        return None
    window = 4096
    while True:
        window = min(window, size)
        chunk = os.pread(descriptor, window, size - window)
        body = chunk.rstrip(b"\n")
        if not body:
            return None
        index = body.rfind(b"\n")
        if index >= 0:
            return body[index + 1 :].decode("utf-8")
        if window >= size:
            return body.decode("utf-8")
        window *= 2


def _require_terminated_tail(journal: TextIO) -> None:
    """Refuse a nonempty journal whose final byte is not a newline.

    Without this the layer creates the corruption it guards against.
    :func:`_last_journal_line` strips trailing newlines before it splits, so a
    file ending ``…}`` yields exactly the same tail string as one ending
    ``…}\\n`` -- the boundary check passes, and the next append then writes its
    record *before* writing any newline, leaving two JSON objects on one
    physical line that :func:`read_journal` can only refuse as invalid JSON.
    The implementation this layer transfers carries that latent defect. The
    journal format requires exactly one trailing newline per event, and
    :func:`canonical_journal_bytes` emits one for every record -- so a journal
    whose tail lacks it is, by definition, not canonical bytes. Refusing is
    therefore format compliance rather than repair.

    Deliberately a refusal and not a silent repair: the journal format specifies
    a persisted shape, not a repair semantic. The operator's bytes stay untouched and
    readable -- :func:`read_journal` still accepts an unterminated final line --
    while the one operation that would turn the defect into corruption refuses.

    Called from both :func:`require_appendable_journal` and
    :func:`append_record`, which is what makes non-concatenation structural
    rather than a convention every caller has to remember.
    """
    descriptor = journal.fileno()
    size = os.fstat(descriptor).st_size
    if size == 0:
        return
    if os.pread(descriptor, 1, size - 1) != b"\n":
        raise JournalError("journal tail is missing its terminal newline")


def require_appendable_journal(journal: TextIO) -> dict[str, Any] | None:
    """Refuse unless this journal may legally receive a v2 append.

    The append boundary has to be exactly as strict as :func:`read_journal`,
    or the append is itself the corruption. A v1 record carries an ``at`` key,
    so a tail check that merely asked whether one was present would accept a v1
    journal, write a v2 line onto it, and leave a store that neither this
    module's reader nor the migrator can parse -- damage created by the very
    operation meant to be guarded. What decides the question is the record's
    shape, so that is what gets read.

    Returns the parsed tail record, or ``None`` for an empty journal.
    """
    _require_terminated_tail(journal)
    line = _last_journal_line(journal)
    if line is None:
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        raise JournalError("journal tail is not valid JSON") from exc
    try:
        return parse_journal_record(payload, "journal tail")
    except JournalError as exc:
        raise JournalError(
            f"refusing to append: this journal is not {CAS_JOURNAL_EVENT_SCHEMA} ({exc})"
        ) from exc


def monotonic_journal_timestamp(previous: dict[str, Any] | None) -> str:
    """A timestamp that sorts strictly after the journal's current last record.

    Canonical order is ``(at, event_id)`` and the reader enforces it, so an
    append whose clock did not advance since the previous one would produce a
    journal this module's own reader rejects. Two appends inside one microsecond
    are rare and entirely possible; a random ``event_id`` cannot be relied on to
    break that tie in the ascending direction, so the timestamp does it.
    """
    now = datetime.now(UTC)
    if previous is None:
        return _iso_utc(now)
    parsed = parse_journal_timestamp(previous["at"], "journal tail")
    if now <= parsed:
        now = parsed + timedelta(microseconds=1)
    return _iso_utc(now)


def append_record(journal: TextIO, record: dict[str, Any]) -> None:
    """Append one record as canonical bytes, terminated by exactly one newline."""
    journal.seek(0, os.SEEK_END)
    _require_terminated_tail(journal)
    journal.write(canonical_journal_line(record))
    journal.write("\n")
    journal.flush()
    os.fsync(journal.fileno())
