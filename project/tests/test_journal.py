"""Behavioral tests for ``castle.journal``.

Three conventions hold module-wide, and a future edit that breaks one breaks a
criterion rather than a preference:

* **Every filesystem path is derived from ``tmp_path``.** Nothing here creates a
  store, journal, or lock anywhere else. A test that wrote into the repository
  would both leak state and move the candidate identity this phase's evidence is
  bound to.
* **No ``pytest.raises`` is bare.** Every one carries a ``match=`` predicate, so
  a control cannot be silently reassigned to an unrelated refusal that happens
  to raise the same type.
* **A marker whose content a parent parses is published by ``os.replace``**, so
  its existence implies complete content. Markers that are only ever tested for
  existence (``ready``, ``release``, ``stop``) rely on that rename being atomic;
  the appended event logs (``holder-events``, ``waiter-events``, ``heartbeat``,
  ``pacer-events``) instead rely on one short ``O_APPEND`` write per record and
  on the reader counting only complete, newline-terminated lines.
"""

from __future__ import annotations

import ast
import json
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from castle import journal

# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------

_DIGEST_A = "1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f"
_EVENT_ID_A = "uuid:0f1e2d3c-4b5a-4998-8877-665544332211"
_EVENT_ID_B = "uuid:1a2b3c4d-5e6f-4a7b-9c8d-0e1f2a3b4c5d"


def _base_record() -> dict[str, Any]:
    """One valid twelve-field v2 record, in the contract's own field order."""
    return {
        "schema": journal.CAS_JOURNAL_EVENT_SCHEMA,
        "event_id": _EVENT_ID_A,
        "at": "2026-01-01T00:00:00Z",
        "event": "ingest",
        "cas_id": f"sha256:{_DIGEST_A}",
        "size": 12,
        "media_type": "text/plain",
        "source_root_id": "root-a",
        "original_relpath": "docs/a.txt",
        "batch": None,
        "outcome": "stored",
    }


def _record(**overrides: Any) -> dict[str, Any]:
    record = _base_record()
    record.update(overrides)
    return record


def _without(field: str) -> dict[str, Any]:
    record = _base_record()
    del record[field]
    return record


def _with_extra(field: str, value: Any) -> dict[str, Any]:
    record = _base_record()
    record[field] = value
    return record


EVENT_A = _base_record()
EVENT_B = _record(event_id=_EVENT_ID_B, at="2026-01-01T00:00:01Z")

#: The ten fields a v1 record carries: the twelve v2 fields minus ``schema``
#: and ``event_id``. It must refuse on shape, before any field is inspected.
_V1_RECORD = {
    "at": "2026-01-01T00:00:00Z",
    "event": "ingest",
    "cas_id": f"sha256:{_DIGEST_A}",
    "size": 12,
    "media_type": "text/plain",
    "source_root_id": "root-a",
    "original_relpath": "docs/a.txt",
    "batch": None,
    "outcome": "stored",
}

#: Hand-written, not re-serialized: sorted keys, ``,``/``:`` with no spaces,
#: ``size`` as a bare integer, one trailing newline per event, UTF-8.
_EXPECTED_CANONICAL_BYTES = (
    b'{"at":"2026-01-01T00:00:00Z","batch":null,"cas_id":"sha256:'
    b"1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f"
    b'","event":"ingest","event_id":"uuid:0f1e2d3c-4b5a-4998-8877-665544332211"'
    b',"media_type":"text/plain"'
    b',"original_relpath":"docs/a.txt","outcome":"stored"'
    b',"schema":"journal.v2","size":12'
    b',"source_root_id":"root-a"}\n'
    b'{"at":"2026-01-01T00:00:01Z","batch":null,"cas_id":"sha256:'
    b"1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f"
    b'","event":"ingest","event_id":"uuid:1a2b3c4d-5e6f-4a7b-9c8d-0e1f2a3b4c5d"'
    b',"media_type":"text/plain"'
    b',"original_relpath":"docs/a.txt","outcome":"stored"'
    b',"schema":"journal.v2","size":12'
    b',"source_root_id":"root-a"}\n'
)


def _write_lines(
    path: Path,
    *objects: Any,
    separators: tuple[str, str] = (",", ":"),
    sort_keys: bool = True,
) -> None:
    """Write *objects* one JSON object per line, at the requested spacing."""
    text = "".join(
        json.dumps(obj, sort_keys=sort_keys, separators=separators) + "\n" for obj in objects
    )
    path.write_text(text, encoding="utf-8")


def _write_raw(path: Path, text: str) -> None:
    """Write byte-exact journal content, including a deliberately absent newline."""
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# Canonical bytes are exact
# --------------------------------------------------------------------------


def test_canonical_bytes_match_the_exact_expected_literal() -> None:
    assert journal.canonical_journal_bytes([EVENT_A, EVENT_B]) == _EXPECTED_CANONICAL_BYTES


# --------------------------------------------------------------------------
# Serialization depends on content, not on input formatting
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Ordering is by parsed instant, then identity
# --------------------------------------------------------------------------


def test_journal_out_of_canonical_order_refuses_naming_the_line(tmp_path: Path) -> None:
    store = tmp_path / "store"
    journal.ensure_store(store)
    _write_lines(store / journal.JOURNAL_FILENAME, EVENT_B, EVENT_A)
    with pytest.raises(
        journal.JournalError,
        match=r"journal line 2 breaks canonical \(at, event_id\) order",
    ):
        journal.read_journal(store)
    duplicate = _record(at="2026-01-01T00:00:01Z")
    _write_lines(store / journal.JOURNAL_FILENAME, EVENT_A, duplicate)
    with pytest.raises(
        journal.JournalError, match=r"journal line 2 repeats the event id recorded at 1"
    ):
        journal.read_journal(store)


# --------------------------------------------------------------------------
# Identity grammars accept exactly two shapes
# --------------------------------------------------------------------------

_VALID_EVENT_IDS = [
    _EVENT_ID_A,
    _EVENT_ID_B,
    f"v1:0000000000000001:sha256:{_DIGEST_A}",
    f"v1:9999999999999999:sha256:{_DIGEST_A}",
]

_INVALID_EVENT_IDS = [
    pytest.param(f"v1:0000000000000001:sha256:{_DIGEST_A.upper()}", id="uppercase-hex"),
    pytest.param(f"v1:0000000000000001:sha256:{_DIGEST_A[:-1]}", id="63-char-hex"),
    pytest.param(f"v1:0000000000000001:sha256:{_DIGEST_A}f", id="65-char-hex"),
    pytest.param(f"v1:0000000000000001:{_DIGEST_A}", id="missing-sha256-prefix"),
    pytest.param("uuid:0f1e2d3c-4b5a-3998-8877-665544332211", id="uuid-version-3"),
    pytest.param("uuid:0f1e2d3c-4b5a-4998-7877-665544332211", id="uuid-variant-7"),
    pytest.param("uuid:not-a-uuid", id="uuid-garbage"),
    pytest.param(f"v1:000000000000001:sha256:{_DIGEST_A}", id="15-digit-ordinal"),
    pytest.param(f"v1:00000000000000001:sha256:{_DIGEST_A}", id="17-digit-ordinal"),
    pytest.param("", id="empty-string"),
    pytest.param(None, id="none"),
    pytest.param(42, id="int"),
]


@pytest.mark.parametrize("value", _INVALID_EVENT_IDS)
def test_event_id_grammar_rejects_every_near_miss(value: object) -> None:
    with pytest.raises(journal.JournalError, match=r"journal line 1 has an invalid event id"):
        journal.validate_event_id(value, "journal line 1")


# --------------------------------------------------------------------------
# Refusal matrix: shape causes
# --------------------------------------------------------------------------

_SHAPE_CASES = [
    pytest.param(_with_extra("extra_field", "x"), id="unknown-thirteenth-field"),
    pytest.param(_without("schema"), id="missing-schema"),
    pytest.param(_without("batch"), id="missing-batch"),
    pytest.param(_V1_RECORD, id="v1-record"),
    pytest.param(None, id="non-dict-none"),
    pytest.param([], id="non-dict-list"),
    pytest.param("{}", id="non-dict-str"),
]


@pytest.mark.parametrize("record", _SHAPE_CASES)
def test_record_shape_refuses_unknown_missing_v1_and_non_dict(record: object) -> None:
    with pytest.raises(journal.JournalError, match=r"journal line 1 has an invalid shape"):
        journal.parse_journal_record(record, "journal line 1")


# --------------------------------------------------------------------------
# Refusal matrix: sequence causes
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Strict v2 parsing — field-value refusal causes
# --------------------------------------------------------------------------

_SCHEMA_MESSAGE = r"journal line 1 is not journal\.v2"
_EVENT_ID_MESSAGE = r"journal line 1 has an invalid event id"
_CAS_ID_MESSAGE = r"journal line 1 has an invalid CAS ID"
_EVENT_MESSAGE = r"journal line 1 has an invalid event"
_SIZE_MESSAGE = r"journal line 1 has an invalid size"
_TEXT_MESSAGE = r"journal line 1 has invalid text"
_TIMESTAMP_MESSAGE = r"journal line 1 has an invalid UTC timestamp"
_UNSAFE_PATH_MESSAGE = r"journal line 1 has an unsafe original path"
_BATCH_MESSAGE = r"journal line 1 has an invalid batch"

_REJECTION_ROWS: list[tuple[str, Any, str]] = [
    ("schema", "journal.v1", _SCHEMA_MESSAGE),
    ("schema", "", _SCHEMA_MESSAGE),
    ("event_id", "uuid:not-a-uuid", _EVENT_ID_MESSAGE),
    ("event_id", "", _EVENT_ID_MESSAGE),
    ("event_id", None, _EVENT_ID_MESSAGE),
    ("event_id", 42, _EVENT_ID_MESSAGE),
    ("cas_id", None, _CAS_ID_MESSAGE),
    ("cas_id", 42, _CAS_ID_MESSAGE),
    ("cas_id", ["x"], _CAS_ID_MESSAGE),
    ("event", "merge", _EVENT_MESSAGE),
    ("event", "ingest ", _EVENT_MESSAGE),
    ("event", "", _EVENT_MESSAGE),
    ("outcome", "skipped", _EVENT_MESSAGE),
    ("outcome", "", _EVENT_MESSAGE),
    ("outcome", None, _EVENT_MESSAGE),
    ("size", True, _SIZE_MESSAGE),
    ("size", False, _SIZE_MESSAGE),
    ("size", -1, _SIZE_MESSAGE),
    ("size", "1", _SIZE_MESSAGE),
    ("size", 1.0, _SIZE_MESSAGE),
    ("size", None, _SIZE_MESSAGE),
    ("media_type", "", _TEXT_MESSAGE),
    ("media_type", None, _TEXT_MESSAGE),
    ("media_type", 42, _TEXT_MESSAGE),
    ("source_root_id", "", _TEXT_MESSAGE),
    ("source_root_id", None, _TEXT_MESSAGE),
    ("source_root_id", 42, _TEXT_MESSAGE),
    ("original_relpath", "", _TEXT_MESSAGE),
    ("original_relpath", None, _TEXT_MESSAGE),
    ("original_relpath", 42, _TEXT_MESSAGE),
    ("at", "2026-01-01T00:00:00+00:00", _TIMESTAMP_MESSAGE),
    ("at", "2026-01-01T00:00:00", _TIMESTAMP_MESSAGE),
    ("at", "not-a-time", _TIMESTAMP_MESSAGE),
    ("at", "", _TIMESTAMP_MESSAGE),
    ("at", None, _TIMESTAMP_MESSAGE),
    ("at", 42, _TIMESTAMP_MESSAGE),
    ("original_relpath", "/etc/passwd", _UNSAFE_PATH_MESSAGE),
    ("original_relpath", "../a.txt", _UNSAFE_PATH_MESSAGE),
    ("original_relpath", "docs/../../a.txt", _UNSAFE_PATH_MESSAGE),
    ("batch", 42, _BATCH_MESSAGE),
    ("batch", ["x"], _BATCH_MESSAGE),
    ("batch", {}, _BATCH_MESSAGE),
]


# --------------------------------------------------------------------------
# Append refusal
# --------------------------------------------------------------------------


def _seed_journal(tmp_path: Path, text: str) -> Path:
    """Create a store whose journal holds exactly *text*, byte for byte."""
    store = tmp_path / "store"
    journal.ensure_store(store)
    _write_raw(store / journal.JOURNAL_FILENAME, text)
    return store


def test_append_refuses_a_torn_tail_line(tmp_path: Path) -> None:
    """A torn line that *is* newline-terminated isolates the JSON guard.

    The terminating newline is deliberate. Without it the tail would refuse one
    guard earlier, on the missing terminal newline, and this control would be
    proving the wrong refusal.
    """
    torn = journal.canonical_journal_line(EVENT_A)[:40] + "\n"
    store = _seed_journal(tmp_path, torn)
    with journal.journal_lock(store) as handle:
        with pytest.raises(journal.JournalError, match=r"journal tail is not valid JSON"):
            journal.require_appendable_journal(handle)


# --------------------------------------------------------------------------
# Process-level machinery: constants, child scripts, and shared helpers
# --------------------------------------------------------------------------

_BARRIER_DEADLINE_S = 10.0
_EXCLUSION_WINDOW_FLOOR_S = 0.5
_EXCLUSION_WINDOW_CAP_S = 2.0
_CALIBRATION_MULTIPLE = 200
_BLOCKED_MULTIPLE = 50
_MIN_HEARTBEATS = 5
_MIN_PACER_CYCLES = 10
_HEARTBEAT_INTERVAL_S = 0.01
_POLL_INTERVAL_S = 0.02
_HOLD_DEADLINE_S = 20.0
_CHILD_JOIN_TIMEOUT_S = 30.0
_GIL_PROBE_S = 0.2
_MIN_GIL_LOOP_ITERATIONS = 10_000

#: Every observing child begins with this preamble, which replaces
#: ``fcntl.flock`` in *its own* interpreter with a recorder that stamps the
#: call and then delegates to the real one. It is not a mock: the real lock is
#: taken. Two properties make it a sound observation point. It runs before the
#: child imports ``castle.journal``, so the module binds the wrapper whichever
#: import form it uses; and ``castle.journal`` calls ``fcntl.flock`` as a
#: module attribute, so the lookup resolves at call time and the observation
#: does not depend on that ordering either. It is provably past the sidecar
#: ``open()`` and the shape check -- ``flock`` is called with the descriptor
#: those produced -- and one Python call frame before the syscall.
#:
#: ``sys.argv[1]`` is the child's own ``flock-calls`` path in every script;
#: each child gets a separate file so one child's calls never pollute another's.
_OBSERVED_FLOCK_PREAMBLE = r"""
import fcntl
import os
import sys
import time

_flock_calls_path = sys.argv[1]
_last_exclusive = [None]


def _append_line(path, text):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(descriptor, (text + "\n").encode("utf-8"))
    finally:
        os.close(descriptor)


def _publish(path):
    temporary = path + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.close(descriptor)
    os.replace(temporary, path)


_real_flock = fcntl.flock


def _observed_flock(descriptor, operation):
    if operation & fcntl.LOCK_EX:
        stamp = time.perf_counter()
        _last_exclusive[0] = stamp
        _append_line(_flock_calls_path, "EX %.9f" % stamp)
    return _real_flock(descriptor, operation)


fcntl.flock = _observed_flock
"""

#: argv: flock-calls, store, ready, release, holder-events, poll, deadline
_HOLDER_SCRIPT = (
    _OBSERVED_FLOCK_PREAMBLE
    + r"""
import castle.journal as journal

_store, _ready, _release, _events = sys.argv[2:6]
_poll = float(sys.argv[6])
_deadline = float(sys.argv[7])

with journal.journal_lock(_store):
    _append_line(_events, "enter %.9f" % time.perf_counter())
    _publish(_ready)
    _limit = time.perf_counter() + _deadline
    while not os.path.exists(_release):
        if time.perf_counter() > _limit:
            _append_line(_events, "timeout")
            sys.exit(3)
        time.sleep(_poll)
    _append_line(_events, "exit %.9f" % time.perf_counter())
sys.exit(0)
"""
)

#: argv: flock-calls, store, waiter-events, heartbeat, heartbeat-interval
_WAITER_SCRIPT = (
    _OBSERVED_FLOCK_PREAMBLE
    + r"""
import threading

import castle.journal as journal

_store, _events, _heartbeat = sys.argv[2:5]
_interval = float(sys.argv[5])


def _beat():
    counter = 0
    while True:
        counter += 1
        _append_line(_heartbeat, "hb %d %.9f" % (counter, time.perf_counter()))
        time.sleep(_interval)


threading.Thread(target=_beat, daemon=True).start()

with journal.journal_lock(_store):
    _append_line(_events, "enter %.9f" % time.perf_counter())
    _append_line(_events, "exit %.9f" % time.perf_counter())
sys.exit(0)
"""
)

#: argv: flock-calls, its own store, pacer-events, stop, interval, deadline
_PACER_SCRIPT = (
    _OBSERVED_FLOCK_PREAMBLE
    + r"""
import castle.journal as journal

_store, _events, _stop = sys.argv[2:5]
_interval = float(sys.argv[5])
_deadline = float(sys.argv[6])

_cycles = 0
_limit = time.perf_counter() + _deadline
while not os.path.exists(_stop) and time.perf_counter() <= _limit:
    with journal.journal_lock(_store):
        _cycles += 1
        _append_line(
            _events,
            "cycle %d %.9f %.9f" % (_cycles, _last_exclusive[0], time.perf_counter()),
        )
    time.sleep(_interval)
sys.exit(0 if _cycles > 0 else 4)
"""
)

#: argv: store whose journal.lock is already a FIFO
_FIFO_SCRIPT = r"""
import sys

import castle.journal as journal

try:
    with journal.journal_lock(sys.argv[1]):
        pass
except journal.CastleError as exc:
    if "not an inert zero-byte regular file" in str(exc):
        sys.exit(0)
    sys.stderr.write("unexpected refusal: %s\n" % exc)
    sys.exit(2)
sys.stderr.write("the FIFO sidecar did not refuse\n")
sys.exit(3)
"""

_IMPORT_ISOLATION_SCRIPT = r"""
import sys

import castle

sys.exit(1 if "castle.journal" in sys.modules else 0)
"""

_PERF_COUNTER_SCRIPT = r"""
import time

print("%.9f" % time.perf_counter())
"""


def _child_env() -> dict[str, str]:
    """Let a child import ``castle`` from this module's own location."""
    return {**os.environ, "PYTHONPATH": str(Path(journal.__file__).parents[1])}


def _spawn_child(script: str, *args: object, out_path: Path) -> subprocess.Popen[bytes]:
    """Start a genuinely concurrent child, output captured to a file.

    Never ``PIPE``: an unread pipe can deadlock a child that outfills it.
    """
    with out_path.open("wb") as sink:
        return subprocess.Popen(
            [sys.executable, "-c", script, *(str(argument) for argument in args)],
            env=_child_env(),
            stdout=sink,
            stderr=subprocess.STDOUT,
        )


def _run_child(script: str, *args: object, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a child expected to finish on its own, bounded by *timeout*."""
    return subprocess.run(
        [sys.executable, "-c", script, *(str(argument) for argument in args)],
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _complete_lines(path: Path) -> list[str]:
    """Return only whole newline-terminated lines, so a partial tail is invisible."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [line for line in text.split("\n")[:-1] if line]


def _parse_pacer_cycles(path: Path) -> list[tuple[int, float, float]]:
    cycles: list[tuple[int, float, float]] = []
    for line in _complete_lines(path):
        parts = line.split()
        if len(parts) == 4 and parts[0] == "cycle":
            cycles.append((int(parts[1]), float(parts[2]), float(parts[3])))
    return cycles


def _stamp(lines: Sequence[str], keyword: str) -> float:
    for line in lines:
        parts = line.split()
        if parts and parts[0] == keyword:
            return float(parts[-1])
    raise AssertionError(f"no {keyword!r} record among {list(lines)!r}")


def _publish_marker(path: Path) -> None:
    """Publish a one-shot marker by atomic rename."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(b"")
    os.replace(temporary, path)


def _child_output(label: str, path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        text = "<no output file>"
    return f"\n--- {label} output ---\n{text}"


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.kill()
    process.wait(timeout=5)


def _await_marker(path: Path, *, message: str, diagnostics: Callable[[], str]) -> None:
    limit = time.perf_counter() + _BARRIER_DEADLINE_S
    while not path.exists():
        if time.perf_counter() > limit:
            pytest.fail(f"{message} (waited {_BARRIER_DEADLINE_S}s){diagnostics()}")
        time.sleep(_POLL_INTERVAL_S)


def _observe_exclusion(
    waiter: subprocess.Popen[bytes],
    pacer: subprocess.Popen[bytes],
    *,
    waiter_paths: dict[str, Path],
    pacer_paths: dict[str, Path],
    diagnostics: Callable[[], str],
    also_alive: Sequence[tuple[str, subprocess.Popen[bytes]]] = (),
    window_floor: float = _EXCLUSION_WINDOW_FLOOR_S,
) -> tuple[float, float]:
    """Prove the waiter is excluded, over a window measured during this run.

    Returns ``(t_free, window)``: the uncontended acquisition latency the pacer
    measured concurrently, and the exclusion window derived from it.
    """
    limit = time.perf_counter() + _BARRIER_DEADLINE_S
    while True:
        reached = bool(_complete_lines(waiter_paths["flock_calls"]))
        cycles = _parse_pacer_cycles(pacer_paths["events"])
        if reached and cycles:
            break
        if time.perf_counter() > limit:
            problems = []
            if not reached:
                problems.append("the waiter never reached `fcntl.flock`")
            if not cycles:
                problems.append("the control process never acquired a free lock")
            pytest.fail("; ".join(problems) + diagnostics())
        time.sleep(_POLL_INTERVAL_S)

    t_free = statistics.median([entered - called for _, called, entered in cycles])
    window = max(window_floor, _CALIBRATION_MULTIPLE * t_free)
    if window > _EXCLUSION_WINDOW_CAP_S:
        pytest.fail(
            "could not establish an evidentially meaningful exclusion window: "
            f"uncontended acquisition measured {t_free:.6f}s, which needs a "
            f"{window:.3f}s window, above the {_EXCLUSION_WINDOW_CAP_S}s cap." + diagnostics()
        )

    window_start = time.perf_counter()
    heartbeats_at_start = len(_complete_lines(waiter_paths["heartbeat"]))
    cycles_at_start = len(cycles)
    watched = [("waiter", waiter), ("pacer", pacer), *also_alive]
    while True:
        assert not waiter_paths["events"].exists(), (
            "the waiter entered the critical section while the lock was held" + diagnostics()
        )
        for label, process in watched:
            assert process.poll() is None, (
                f"the {label} process died inside the exclusion window"
                f" (exit {process.returncode}){diagnostics()}"
            )
        if time.perf_counter() - window_start >= window:
            break
        time.sleep(_POLL_INTERVAL_S)

    heartbeat_gain = len(_complete_lines(waiter_paths["heartbeat"])) - heartbeats_at_start
    cycle_gain = len(_parse_pacer_cycles(pacer_paths["events"])) - cycles_at_start
    assert heartbeat_gain >= _MIN_HEARTBEATS, (
        f"the waiter's heartbeat advanced only {heartbeat_gain} times in {window:.3f}s,"
        " so its failure to acquire is not evidence of exclusion — the process was"
        f" not demonstrably running{diagnostics()}"
    )
    assert cycle_gain >= _MIN_PACER_CYCLES, (
        f"the control process completed only {cycle_gain} free-lock acquisitions in"
        f" {window:.3f}s, so the waiter completing zero is not a meaningful"
        f" comparison{diagnostics()}"
    )
    return t_free, window


# --------------------------------------------------------------------------
# Instrument qualification — the locking and exclusion tests below are
# meaningless unless these two hold on this platform
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Structural properties of the shipped module: stdlib-only imports, the
# module-attribute flock call the exclusion proof observes, and a package
# root that loads no submodule
# --------------------------------------------------------------------------


def _journal_module_tree() -> ast.Module:
    return ast.parse(Path(journal.__file__).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Lock shape refusals and the stable inode
# --------------------------------------------------------------------------


def test_lock_survives_atomic_replacement_of_the_journal(tmp_path: Path) -> None:
    """The lock is held on the sidecar inode, which the rename does not touch.

    An implementation that locked ``journal.jsonl`` would let the waiter
    straight through after the replacement, because the waiter would open the
    *new* inode — and the heartbeat and the control process prove that both the
    waiter and the machine were running throughout the window in which it did
    not.
    """
    store = tmp_path / "store"
    pacer_store = tmp_path / "pacer-store"
    waiter_paths = {
        "flock_calls": tmp_path / "waiter-flock-calls",
        "events": tmp_path / "waiter-events",
        "heartbeat": tmp_path / "heartbeat",
    }
    pacer_paths = {
        "flock_calls": tmp_path / "pacer-flock-calls",
        "events": tmp_path / "pacer-events",
    }
    stop = tmp_path / "stop"
    waiter_out = tmp_path / "waiter.out"
    pacer_out = tmp_path / "pacer.out"

    def diagnostics() -> str:
        return _child_output("waiter", waiter_out) + _child_output("pacer", pacer_out)

    with journal.journal_lock(store):
        pass
    lock_path = store / journal.JOURNAL_LOCK_FILENAME
    inode_before = lock_path.stat().st_ino

    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(b"")

    waiter: subprocess.Popen[bytes] | None = None
    pacer: subprocess.Popen[bytes] | None = None
    try:
        with journal.journal_lock(store):
            os.replace(replacement, store / journal.JOURNAL_FILENAME)
            assert lock_path.stat().st_ino == inode_before
            waiter = _spawn_child(
                _WAITER_SCRIPT,
                waiter_paths["flock_calls"],
                store,
                waiter_paths["events"],
                waiter_paths["heartbeat"],
                _HEARTBEAT_INTERVAL_S,
                out_path=waiter_out,
            )
            pacer = _spawn_child(
                _PACER_SCRIPT,
                pacer_paths["flock_calls"],
                pacer_store,
                pacer_paths["events"],
                stop,
                _HEARTBEAT_INTERVAL_S,
                _HOLD_DEADLINE_S,
                out_path=pacer_out,
            )
            _observe_exclusion(
                waiter,
                pacer,
                waiter_paths=waiter_paths,
                pacer_paths=pacer_paths,
                diagnostics=diagnostics,
            )
            _publish_marker(stop)
            assert lock_path.stat().st_ino == inode_before

        assert waiter.wait(timeout=_CHILD_JOIN_TIMEOUT_S) == 0, diagnostics()
        assert pacer.wait(timeout=_CHILD_JOIN_TIMEOUT_S) == 0, diagnostics()
        assert len(_complete_lines(waiter_paths["events"])) == 2, diagnostics()
    finally:
        _terminate(waiter)
        _terminate(pacer)
    _assert_retired_waiter(tmp_path)


# --------------------------------------------------------------------------
# Mutual exclusion, bounded
# --------------------------------------------------------------------------


def test_two_processes_never_overlap_in_the_critical_section(tmp_path: Path) -> None:
    """Two processes contending for one store's lock never overlap.

    **The second acquirer blocks until release**; it does not refuse
    immediately. ``journal_lock`` calls ``flock(LOCK_EX)`` with no ``LOCK_NB``.

    Falsifiers that are deterministic, with no dependence on scheduling:
    deleting the ``fcntl.flock`` call, replacing it with ``lockf`` or any other
    mechanism, or locking the wrong descriptor family all leave the waiter's
    ``flock-calls`` file empty, and the bounded wait for it fails; adding
    ``LOCK_NB`` makes the waiter raise and die, which the liveness assertions
    catch and which would also contradict this docstring. A wedged holder
    expires its own deadline and exits 3.

    The residual, named honestly: an implementation that *calls* ``flock`` but
    does not exclude is caught by observation rather than by determinism. For
    this test to be green against one, the OS would have to starve the waiter's
    main thread for the whole measured window while freely running its sibling
    heartbeat thread in the same process and a second process besides. That is
    a scheduler-fairness violation, not an unlucky interleaving.
    """
    store = tmp_path / "store"
    pacer_store = tmp_path / "pacer-store"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    stop = tmp_path / "stop"
    holder_events = tmp_path / "holder-events"
    holder_flock_calls = tmp_path / "holder-flock-calls"
    waiter_paths = {
        "flock_calls": tmp_path / "waiter-flock-calls",
        "events": tmp_path / "waiter-events",
        "heartbeat": tmp_path / "heartbeat",
    }
    pacer_paths = {
        "flock_calls": tmp_path / "pacer-flock-calls",
        "events": tmp_path / "pacer-events",
    }
    holder_out = tmp_path / "holder.out"
    waiter_out = tmp_path / "waiter.out"
    pacer_out = tmp_path / "pacer.out"

    def diagnostics() -> str:
        return (
            _child_output("holder", holder_out)
            + _child_output("waiter", waiter_out)
            + _child_output("pacer", pacer_out)
        )

    holder: subprocess.Popen[bytes] | None = None
    waiter: subprocess.Popen[bytes] | None = None
    pacer: subprocess.Popen[bytes] | None = None
    try:
        holder = _spawn_child(
            _HOLDER_SCRIPT,
            holder_flock_calls,
            store,
            ready,
            release,
            holder_events,
            _POLL_INTERVAL_S,
            _HOLD_DEADLINE_S,
            out_path=holder_out,
        )
        _await_marker(
            ready,
            message="the holder never published its lock-held barrier",
            diagnostics=diagnostics,
        )
        waiter = _spawn_child(
            _WAITER_SCRIPT,
            waiter_paths["flock_calls"],
            store,
            waiter_paths["events"],
            waiter_paths["heartbeat"],
            _HEARTBEAT_INTERVAL_S,
            out_path=waiter_out,
        )
        pacer = _spawn_child(
            _PACER_SCRIPT,
            pacer_paths["flock_calls"],
            pacer_store,
            pacer_paths["events"],
            stop,
            _HEARTBEAT_INTERVAL_S,
            _HOLD_DEADLINE_S,
            out_path=pacer_out,
        )
        t_free, window = _observe_exclusion(
            waiter,
            pacer,
            waiter_paths=waiter_paths,
            pacer_paths=pacer_paths,
            diagnostics=diagnostics,
            also_alive=(("holder", holder),),
        )
        _publish_marker(stop)
        _publish_marker(release)

        assert holder.wait(timeout=_CHILD_JOIN_TIMEOUT_S) == 0, (
            f"the holder exited {holder.returncode}"
            " (3 means it timed out waiting for release)" + diagnostics()
        )
        assert waiter.wait(timeout=_CHILD_JOIN_TIMEOUT_S) == 0, (
            "the waiter never acquired the lock after release" + diagnostics()
        )
        assert pacer.wait(timeout=_CHILD_JOIN_TIMEOUT_S) == 0, diagnostics()

        holder_lines = _complete_lines(holder_events)
        waiter_lines = _complete_lines(waiter_paths["events"])
        waiter_calls = _complete_lines(waiter_paths["flock_calls"])
        assert len(holder_lines) == 2, diagnostics()
        assert len(waiter_lines) == 2, diagnostics()
        assert len(waiter_calls) == 1, diagnostics()

        waiter_flock_called = float(waiter_calls[0].split()[-1])
        waiter_entered = _stamp(waiter_lines, "enter")
        holder_exit = _stamp(holder_lines, "exit")

        blocked_for = waiter_entered - waiter_flock_called
        assert blocked_for >= _BLOCKED_MULTIPLE * t_free, (
            f"the waiter's acquisition took {blocked_for:.6f}s, under the"
            f" {_BLOCKED_MULTIPLE}x margin over the {t_free:.6f}s an uncontended"
            f" acquisition took during this run (window {window:.3f}s)"
        )
        assert waiter_flock_called < holder_exit < waiter_entered, (
            "the waiter did not call flock before the holder left its section and"
            f" enter after: {waiter_flock_called!r} / {holder_exit!r} /"
            f" {waiter_entered!r}"
        )
    finally:
        _terminate(holder)
        _terminate(waiter)
        _terminate(pacer)
    _assert_retired_waiter(tmp_path)


def _metadata_identity() -> dict[str, Any]:
    return {
        "store_id": "11111111-1111-4111-8111-111111111111",
        "lineage_id": "22222222-2222-4222-8222-222222222222",
        "epoch_id": "33333333-3333-4333-8333-333333333333",
        "generation": 0,
        "predecessor_epoch_id": None,
    }


def test_store_metadata_is_closed_and_canonical(tmp_path: Path) -> None:
    import dataclasses
    import inspect

    from castle import store_state as state

    identity = state.StoreIdentity(**_metadata_identity())
    assert identity.to_dict() == _metadata_identity()
    assert [field.name for field in dataclasses.fields(identity)] == list(_metadata_identity())
    with pytest.raises(dataclasses.FrozenInstanceError, match="cannot assign"):
        identity.generation = 1
    for function in (state.read_store_identity, state.prepare_store):
        signature = inspect.signature(function, eval_str=True)
        assert list(signature.parameters) == ["store"]
        assert signature.parameters["store"].annotation is Path
        assert signature.return_annotation is state.StoreIdentity
    assert list(inspect.signature(state.StoreStateError).parameters) == ["code"]
    assert state.StoreStateError.__bases__ == (journal.CastleError,)
    with pytest.raises(ValueError, match="^store-state-invalid$"):
        state.StoreStateError("private arbitrary text")
    root = tmp_path / "store"
    root.mkdir()
    metadata = {
        "schema": "castle.store.v1",
        "identity": _metadata_identity(),
        "state": "active",
        "transition": None,
        "prepared_sha256": None,
    }
    expected = (
        b'{"identity":{"epoch_id":"33333333-3333-4333-8333-333333333333",'
        b'"generation":0,"lineage_id":"22222222-2222-4222-8222-222222222222",'
        b'"predecessor_epoch_id":null,"store_id":"11111111-1111-4111-8111-111111111111"},'
        b'"prepared_sha256":null,"schema":"castle.store.v1","state":"active","transition":null}\n'
    )
    state._publish_metadata(root / "store.json", metadata)
    assert (root / "store.json").read_bytes() == expected
    assert state.read_store_identity(root) == identity
    invalid = [
        {**metadata, "extra": 1},
        {key: value for key, value in metadata.items() if key != "state"},
        {**metadata, "schema": "unknown"},
        {**metadata, "state": "prepared"},
        {**metadata, "state": "building"},
        {**metadata, "prepared_sha256": "sha256:" + "a" * 64},
        {**metadata, "state": []},
    ]
    for field, value in (
        ("generation", True),
        ("generation", -1),
        ("generation", 1.0),
        ("generation", 1),
        ("store_id", "11111111-1111-3111-8111-111111111111"),
        ("lineage_id", 5),
        ("epoch_id", "A3333333-3333-4333-8333-333333333333"),
        ("predecessor_epoch_id", "44444444-4444-4444-8444-444444444444"),
    ):
        invalid.append({**metadata, "identity": {**_metadata_identity(), field: value}})
        with pytest.raises(state.StoreStateError, match="^store-state-invalid$"):
            state.StoreIdentity(**{**_metadata_identity(), field: value})
    for value in invalid:
        (root / "store.json").write_bytes(state._canonical(value))
        with pytest.raises(state.StoreStateError, match="^store-state-invalid$"):
            state.read_store_identity(root)
    malformed = [
        expected.replace(b'"state":"active"', b'"state":"active","state":"active"'),
        b"\xef\xbb\xbf" + expected,
        expected + b"{}",
        expected.replace(b'"generation":0', b'"generation":NaN'),
        expected.replace(b'"generation":0', b'"generation":Infinity'),
        b"\xff",
    ]
    for raw in malformed:
        (root / "store.json").write_bytes(raw)
        with pytest.raises(state.StoreStateError, match="^store-state-invalid$"):
            state.read_store_identity(root)
    successor = {
        **_metadata_identity(),
        "store_id": "44444444-4444-4444-8444-444444444444",
        "epoch_id": "55555555-5555-4555-8555-555555555555",
        "generation": 1,
        "predecessor_epoch_id": _metadata_identity()["epoch_id"],
    }
    binding = {
        "operation_id": "66666666-6666-4666-8666-666666666666",
        "predecessor": _metadata_identity(),
        "successor": successor,
    }
    prepared = {
        **metadata,
        "identity": successor,
        "state": "prepared",
        "transition": binding,
        "prepared_sha256": "sha256:" + "a" * 64,
    }
    assert state._validate_metadata("store.json", prepared) == prepared
    for name, value in (
        (
            "retired.json",
            {"schema": "castle.retirement.v1", "transition": binding, "state": "retired"},
        ),
        (
            "excision-selection.json",
            {
                "schema": "castle.excision_selection.v1",
                "transition": binding,
                "cas_ids": ["sha256:" + "a" * 64],
            },
        ),
    ):
        assert state._validate_metadata(name, value) == value
        for malformed in (
            {**value, "extra": "private"},
            {key: item for key, item in value.items() if key != "transition"},
            {**value, "schema": "unknown"},
            {**value, "transition": {**binding, "operation_id": identity.store_id}},
            {**value, "transition": {**binding, "extra": 0}},
        ):
            with pytest.raises(state.StoreStateError, match="^store-state-invalid$"):
                state._validate_metadata(name, malformed)
        path = root / name
        path.write_bytes(state._canonical(value))
        path.chmod(0o600)
        assert state._read_metadata(path) == value
        if name == "excision-selection.json":
            for ids in (
                [],
                ["sha256:" + "a" * 64] * 2,
                ["sha256:" + "b" * 64, "sha256:" + "a" * 64],
                [True],
                "sha256:" + "a" * 64,
            ):
                with pytest.raises(state.StoreStateError, match="^store-state-invalid$"):
                    state._validate_metadata(name, {**value, "cas_ids": ids})
            path.chmod(0o644)
            with pytest.raises(state.StoreStateError, match="^store-state-invalid$"):
                state._read_metadata(path)
        else:
            with pytest.raises(state.StoreStateError, match="^store-state-invalid$"):
                state._validate_metadata(name, {**value, "state": "active"})
        path.unlink()
    for field, value in (
        ("generation", 2),
        ("lineage_id", successor["store_id"]),
        ("store_id", identity.store_id),
        ("predecessor_epoch_id", successor["epoch_id"]),
    ):
        wrong_binding = {**binding, "successor": {**successor, field: value}}
        with pytest.raises(state.StoreStateError, match="^store-state-invalid$"):
            state._validate_metadata("store.json", {**prepared, "transition": wrong_binding})
    for phase, seal in (
        ("building", "sha256:" + "a" * 64),
        ("active", "sha256:" + "a" * 64),
        ("prepared", None),
        ("prepared", "a" * 64),
    ):
        with pytest.raises(state.StoreStateError, match="^store-state-invalid$"):
            state._validate_metadata(
                "store.json", {**prepared, "state": phase, "prepared_sha256": seal}
            )
    (root / "store.json").unlink()
    external = tmp_path / "external"
    external.write_bytes(expected)
    for kind in ("symlink", "hardlink", "directory", "fifo"):
        path = root / "store.json"
        if kind == "symlink":
            path.symlink_to(external)
        elif kind == "hardlink":
            os.link(external, path)
        elif kind == "directory":
            path.mkdir()
        else:
            os.mkfifo(path)
        with pytest.raises(state.StoreStateError, match="^store-state-invalid$"):
            state.read_store_identity(root)
        path.rmdir() if kind == "directory" else path.unlink()
        assert external.read_bytes() == expected


def test_prepare_store_preserves_offline_v2_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import sqlite3
    from contextlib import closing

    from castle import cas
    from castle import store_state as state

    def offline(root: Path, indexed: bool) -> None:
        (root / "objects" / "sha256").mkdir(parents=True)
        (root / "journal.lock").touch()
        payload = b"offline authority\n"
        cas_id = "sha256:" + hashlib.sha256(payload).hexdigest()
        object_file = cas.object_path(root, cas_id)
        object_file.parent.mkdir(parents=True)
        object_file.write_bytes(payload)
        object_file.chmod(0o444)
        record = _record(cas_id=cas_id, size=len(payload))
        (root / "journal.jsonl").write_bytes(journal.canonical_journal_bytes([record]))
        if indexed:
            with closing(sqlite3.connect(root / "cas.sqlite")) as db:
                db.executescript(cas._SCHEMA)
                db.executemany(
                    "INSERT INTO remotes VALUES (?, ?, ?, ?, ?)",
                    [(cas_id, "local", "key", "at", "checked")] * 2,
                )
                db.commit()

    def snapshot(root: Path) -> dict[str, bytes]:
        return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}

    for indexed in (False, True):
        root = tmp_path / str(indexed)
        offline(root, indexed)
        before = snapshot(root)
        for operation in (journal.ensure_store, journal.read_journal, state.read_store_identity):
            with pytest.raises(state.StoreStateError, match="^store-identity-required$"):
                operation(root)
        assert snapshot(root) == before
        identity = state.prepare_store(root)
        assert identity.generation == 0 and identity.predecessor_epoch_id is None
        assert len({identity.store_id, identity.lineage_id, identity.epoch_id}) == 3
        after = snapshot(root)
        assert {k: v for k, v in after.items() if k != "store.json"} == before
        assert state.prepare_store(root) == identity
        assert snapshot(root) == after
    for kind in ("journal", "outcome-list", "outcome-dict", "object", "remote", "temporary"):
        root = tmp_path / kind
        offline(root, True)
        if kind == "journal":
            (root / "journal.jsonl").write_bytes(b'{"v1":true}\n')
        elif kind in ("outcome-list", "outcome-dict"):
            record = json.loads((root / "journal.jsonl").read_text())
            assert journal.parse_journal_record(record, "control") == record
            record["outcome"] = [] if kind == "outcome-list" else {}
            (root / "journal.jsonl").write_text(json.dumps(record) + "\n")
            with pytest.raises(journal.JournalError, match="^control has an invalid event$"):
                journal.parse_journal_record(record, "control")
        elif kind == "object":
            next((root / "objects").rglob("?" * 64)).unlink()
        elif kind == "remote":
            (root / "cas.sqlite").write_bytes(b"unreadable index")
        else:
            (root / ".store.json.tmp").write_bytes(b"unbound")
        before = snapshot(root)
        code = "store-state-invalid" if kind == "temporary" else "store-preparation-failed"
        with pytest.raises(state.StoreStateError, match=f"^{code}$") as caught:
            state.prepare_store(root)
        assert str(caught.value) == code, kind
        assert caught.value.__cause__ is None, kind
        assert caught.value.__context__ is None or caught.value.__suppress_context__, kind
        assert snapshot(root) == before, kind
    root = tmp_path / "published-durability-failure"
    offline(root, False)
    reached = []
    real_sync = state._fsync_directory

    def fail(directory: Path) -> None:
        if directory == root.resolve() and (root / "store.json").exists():
            reached.append("published")
            raise OSError("injected directory fsync")
        real_sync(directory)

    with monkeypatch.context() as controlled:
        controlled.setattr(state, "_fsync_directory", fail)
        with pytest.raises(state.StoreStateError, match="^store-preparation-failed$"):
            state.prepare_store(root)
    assert reached == ["published"]
    identity = state.read_store_identity(root)
    assert state.prepare_store(root) == identity


def _assert_retired_waiter(tmp_path: Path) -> None:
    root = tmp_path / "retired-waiter"
    ready, output = tmp_path / "queued", tmp_path / "retired-waiter.out"
    journal.ensure_store(root)
    inode = (root / "journal.lock").stat().st_ino
    script = r"""
import fcntl
import json
import os
from pathlib import Path
import sys
from castle import journal
from castle.store_state import StoreStateError
root, ready = map(Path, sys.argv[1:])
real = fcntl.flock
def observe(fd, operation):
    if operation == fcntl.LOCK_EX:
        temporary = ready.with_suffix(".tmp")
        temporary.write_text(str(os.fstat(fd).st_ino))
        os.replace(temporary, ready)
    return real(fd, operation)
fcntl.flock = observe
try:
    with journal.journal_lock(root):
        raise AssertionError("retired waiter entered")
except StoreStateError as error:
    print(json.dumps([str(error), (root / "journal.lock").stat().st_ino]))
"""
    process = None
    try:
        with journal.journal_lock(root):
            process = _spawn_child(script, root, ready, out_path=output)
            _await_marker(
                ready,
                message="waiter never attempted lock",
                diagnostics=lambda: _child_output("retired waiter", output),
            )
            assert int(ready.read_text()) == inode
            (root / "retired.json").write_bytes(b"fenced, even malformed")
            (root / "journal.jsonl").unlink()
        assert process.wait(timeout=_CHILD_JOIN_TIMEOUT_S) == 0, output.read_text()
        assert json.loads(output.read_text()) == ["store-retired", inode]
        assert not (root / "journal.jsonl").exists()
        assert (root / "journal.lock").stat().st_ino == inode
    finally:
        _terminate(process)
