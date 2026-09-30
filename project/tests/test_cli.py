"""Behavioral tests for ``castle.cli`` — every shipped command and its misuse surface.

Conventions that hold module-wide, each guarding a criterion rather than a
preference:

* **Every filesystem path derives from ``tmp_path``.** Nothing here creates a
  store, object, index, or materialized file anywhere else. A stray write would
  both leak state and move the candidate identity this work's evidence binds to.
* **The suite is in-process.** ``cli.main([...])`` under ``capsys`` is faster
  than a subprocess per case and is the only form in which the library-fencing
  controls can observe whether the library was reached. ``capfdbinary`` carries
  the byte-fidelity paths, because the ``--cat`` route writes raw bytes to
  ``sys.stdout.buffer`` and ``capsys`` does not preserve those.
* **The library fence is derived from the library, never from the module under
  test.** A control the module under test could define into compliance is not a
  control; the fence therefore enumerates the library's own callables.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import gc
import hashlib
import importlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from castle import cas, cli, journal

# --------------------------------------------------------------------------
# Populations stated as literals, so no derivation can shrink a claim
# --------------------------------------------------------------------------

_COMMANDS: tuple[str, ...] = (
    "init",
    "ingest",
    "get",
    "stat",
    "names",
    "find",
    "verify",
    "rebuild-index",
    "journal-migrate-v1",
    "sync-s3",
)

# The authoritative guarded population, written independently of the shipped
# constant. A command joining the guarded set is a three-place edit — the
# module's constant, this literal, and the invocation table below — and the two
# equality controls fail until all three agree.
_EXPECTED_GUARDED: frozenset[str] = frozenset({"ingest", "rebuild-index", "journal-migrate-v1"})

# The floor the library fence must cover. Nine functions and five classes: a
# derivation that silently collapsed, or that reintroduced an exclusion of
# classes, could not pass.
_LIBRARY_FLOOR: frozenset[str] = frozenset(
    {
        "ingest",
        "rebuild_index",
        "verify",
        "require_object",
        "materialize",
        "object_stat",
        "store_stat",
        "names_for",
        "find_names",
        "IngestSummary",
        "IngestReceipt",
        "VerifyResult",
        "NameSighting",
        "CasError",
    }
)

_ABSENT_ID = "sha256:" + "0" * 64

_SOURCE_TOKEN = "<source-dir>"

# Every required operand *other than* ``--root``, so a root-omitted invocation
# fails for exactly one reason.
_MINIMAL_OPERANDS: dict[str, list[str]] = {
    "init": [],
    "ingest": ["--source", _SOURCE_TOKEN, "--source-id", "s"],
    "get": [_ABSENT_ID],
    "stat": [],
    "names": [_ABSENT_ID],
    "find": ["x"],
    "verify": [],
    "rebuild-index": [],
    "journal-migrate-v1": ["--expect-journal-sha256", "0" * 64],
    "sync-s3": ["--bucket", "b", "--profile", "p"],
}

# command -> (extra argv, the library entry point that command would call)
_GUARDED_INVOCATIONS: dict[str, tuple[list[str], str | None]] = {
    "ingest": (["--source", _SOURCE_TOKEN, "--source-id", "s"], "ingest"),
    "rebuild-index": ([], "rebuild_index"),
    "journal-migrate-v1": (["--expect-journal-sha256", "0" * 64], None),
}

_SYNC_REFUSAL = (
    "castle: error: sync-s3 is not implemented; "
    "encrypted S3 replication is reserved for a later phase"
)

_GUARD_WORDING = "store root is not an existing directory"

# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def _operands(command: str, table: dict[str, list[str]], source: Path) -> list[str]:
    return [str(source) if item == _SOURCE_TOKEN else item for item in table[command]]


def _source_tree(root: Path) -> dict[str, bytes]:
    """Write several files with varied bytes and return relpath -> content.

    One file carries an embedded NUL and high-byte, non-UTF-8 content, and one
    carries a name unique to this helper so a test can tell the tree it passed
    from any other tree.
    """
    contents = {
        "alpha.txt": b"alpha payload\n",
        "gamma/binary.bin": bytes(range(200, 256)) + b"\x00\x01\x00 mixed",
        "gamma/delta.txt": b"delta payload\n",
        "only-in-this-tree.bin": b"\x00unique marker\xff\n",
    }
    for relpath, payload in contents.items():
        target = root / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return contents


def _source_dir(tmp_path: Path, name: str = "tree") -> tuple[Path, dict[str, bytes]]:
    source = tmp_path / name
    source.mkdir()
    return source, _source_tree(source)


def _populated(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes]]:
    """An initialized, populated store built through the library, not the CLI.

    Building the fixture below the module under test is deliberate: a defect in
    the CLI cannot mask itself by also building the thing it is compared against.
    """
    store = tmp_path / "store"
    source, contents = _source_dir(tmp_path)
    journal.ensure_store(store)
    cas.ingest(store, source, "fixture")
    return store, source, contents


def _store(tmp_path: Path) -> Path:
    store, _source, _contents = _populated(tmp_path)
    return store


def _object_ids(store: Path) -> list[str]:
    objects, _names = cas.expected_index_rows(journal.read_journal(store))
    return sorted(objects)


def _id_for(store: Path, relpath: str) -> str:
    matches = cas.find_names(store, relpath)
    assert len(matches) == 1, f"expected exactly one name matching {relpath}: {matches}"
    return matches[0]["cas_id"]


def _listing(path: Path) -> set[str]:
    return {entry.name for entry in path.iterdir()}


_KIND_LABELS: dict[int, str] = {
    stat.S_IFDIR: "dir",
    stat.S_IFREG: "file",
    stat.S_IFLNK: "symlink",
    stat.S_IFIFO: "fifo",
    stat.S_IFSOCK: "socket",
    stat.S_IFCHR: "chardev",
    stat.S_IFBLK: "blockdev",
}


def _entry_kind(mode: int) -> str:
    """Name the entry kind *mode* encodes, leaving no kind unnamed.

    The fallback carries the raw type bits rather than a shared word, so two
    kinds this table does not list still read as two different kinds.
    """
    file_type = stat.S_IFMT(mode)
    return _KIND_LABELS.get(file_type, f"kind-{file_type:#o}")


def _tree_snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """Record every entry beneath *root*, whatever kind that entry is.

    Every entry is recorded through one total branch on its kind, so nothing
    can be added, removed, or replaced without moving the value: a directory,
    a regular file, a symlink, and every other kind a filesystem can hold each
    reach a case, and the recorded kind is the first element, so a path whose
    kind changed never compares equal to what stood there before. Content hash
    catches a same-size rewrite, the link target catches a retargeted symlink,
    inode catches a delete-and-recreate whose bytes happen to match, and
    recursion catches a store wiped and re-bootstrapped empty. Mtime is
    deliberately excluded: taking and releasing the lock is not obliged to
    leave timestamps untouched.
    """
    snapshot: dict[str, tuple[Any, ...]] = {}
    for entry in sorted(root.rglob("*")):
        relpath = entry.relative_to(root).as_posix()
        info = entry.lstat()
        kind = _entry_kind(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            snapshot[relpath] = (kind, stat.S_IMODE(info.st_mode), info.st_ino)
        elif stat.S_ISREG(info.st_mode):
            snapshot[relpath] = (
                kind,
                info.st_size,
                stat.S_IMODE(info.st_mode),
                info.st_ino,
                hashlib.sha256(entry.read_bytes()).hexdigest(),
            )
        elif stat.S_ISLNK(info.st_mode):
            snapshot[relpath] = (
                kind,
                stat.S_IMODE(info.st_mode),
                info.st_ino,
                os.readlink(entry),
            )
        else:
            snapshot[relpath] = (kind, stat.S_IMODE(info.st_mode), info.st_ino)
    return snapshot


def _seed_remotes(store: Path, rows: list[tuple[str, str, str, str, str]]) -> None:
    with sqlite3.connect(store / "cas.sqlite") as connection:
        connection.executemany(
            f"INSERT INTO remotes ({cas._REMOTES_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        connection.commit()


def _index_rows(store: Path) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    with sqlite3.connect(store / "cas.sqlite") as connection:
        objects = connection.execute(
            """
            SELECT cas_id, size, media_type, stored_relpath, first_seen_utc, batch
            FROM objects
            """
        ).fetchall()
        names = connection.execute(
            """
            SELECT cas_id, source_root_id, original_relpath, recorded_utc
            FROM names
            """
        ).fetchall()
    return objects, names


def _remote_rows(store: Path) -> list[tuple[Any, ...]]:
    with sqlite3.connect(store / "cas.sqlite") as connection:
        return connection.execute(f"SELECT {cas._REMOTES_COLUMNS} FROM remotes").fetchall()


def _store_state(store: Path) -> dict[str, Any]:
    """The store's whole logical content, captured without the module under test.

    A count and a list of basenames are two projections of the store, and a
    command that wrote somewhere neither projection looks would preserve both.
    This records the journal's bytes, every row of every index table, and every
    entry beneath the root by kind, complete relative path, and content — so
    "nothing was stored" can be held to the whole claim. Rows are ordered by
    their own text because the index is free to return them in any order.
    """
    objects, names = _index_rows(store)
    remotes = _remote_rows(store)
    # Index connections are closed before the tree is read, so the snapshot
    # observes a settled store rather than a write-ahead sidecar mid-checkpoint.
    gc.collect()
    return {
        "journal": (store / "journal.jsonl").read_bytes(),
        "objects": sorted(objects, key=repr),
        "names": sorted(names, key=repr),
        "remotes": sorted(remotes, key=repr),
        "tree": _tree_snapshot(store),
    }


def _child_env() -> dict[str, str]:
    """Let a child import ``castle`` from this deliverable's own location."""
    return {**os.environ, "PYTHONPATH": str(Path(cas.__file__).parents[1])}


def _v1_cli_store(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "v1-store"
    payload = b"migration CLI payload\n"
    cas_id = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    row = {
        "at": "2031-03-04T00:01:00Z",
        "event": "ingest",
        "cas_id": cas_id,
        "size": len(payload),
        "media_type": "text/plain",
        "source_root_id": "cli-test",
        "original_relpath": "payload.txt",
        "mtime": "2031-03-04T00:01:00Z",
        "batch": "cli",
        "outcome": "stored",
    }
    (root / "objects" / "sha256").mkdir(parents=True)
    (root / journal.JOURNAL_LOCK_FILENAME).touch()
    target = cas.object_path(root, cas_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    target.chmod(0o444)
    source = json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    (root / journal.JOURNAL_FILENAME).write_bytes(source)
    objects, names = cas.expected_index_rows([row])
    with sqlite3.connect(root / cas.INDEX_FILENAME) as connection:
        connection.executescript(cas._SCHEMA)
        connection.executemany(
            """
            INSERT INTO objects
                (cas_id, size, media_type, stored_relpath, first_seen_utc, batch)
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
    return root, hashlib.sha256(source).hexdigest()


# --------------------------------------------------------------------------
# The library fence: derived from the library, with no exclusion of any kind
# --------------------------------------------------------------------------

_FIRED: list[str] = []


def _library_entry_points() -> tuple[str, ...]:
    """Every callable name the library module exposes, with no exclusion.

    Derived from the library's own namespace rather than from the module under
    test: the module under test cannot shrink it by any spelling, and a library
    that grows a callable makes every fenced case stricter. Modules fall out on
    their own because a module is not callable.
    """
    return tuple(sorted(name for name, value in vars(cas).items() if callable(value)))


def _make_fake(name: str) -> Any:
    def _fake(*_args: object, **_kwargs: object) -> object:
        _FIRED.append(name)
        raise AssertionError(f"library reached: {name}")

    return _fake


@contextlib.contextmanager
def _forbid_library(*, terminal: str | None = None) -> Iterator[tuple[Any, frozenset[str]]]:
    """Replace every callable library entry point with a fake that records and raises.

    The module under test is reloaded under the fakes, so a from-import, a
    pre-bound alias, or a locally aliased attribute re-binds to a fake rather
    than escaping the fence. The originals are restored by hand rather than
    through ``monkeypatch``, because the restore has to precede the second
    reload and pytest tears a requested ``monkeypatch`` down afterwards.
    """
    names = _library_entry_points()
    originals = {name: getattr(cas, name) for name in names}
    _FIRED.clear()
    for name in names:
        setattr(cas, name, _make_fake(name))
    try:
        importlib.reload(cli)
        patched = frozenset(names)
        assert patched, "the library fence patched nothing"
        assert patched >= _LIBRARY_FLOOR
        if terminal is not None:
            assert terminal in patched
        yield cli, patched
    finally:
        for name, value in originals.items():
            setattr(cas, name, value)
        importlib.reload(cli)


# --------------------------------------------------------------------------
# Source-level harvests over the module under test
# --------------------------------------------------------------------------


def _cli_tree() -> ast.Module:
    return ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))


def _realized_subcommands(
    parser: argparse.ArgumentParser,
) -> tuple[argparse._SubParsersAction[argparse.ArgumentParser], dict[str, argparse.ArgumentParser]]:
    actions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    assert len(actions) == 1, f"expected one realized subcommand action, found {len(actions)}"
    action = actions[0]
    return action, dict(action.choices)


def _realized_option_strings(parser: argparse.ArgumentParser) -> frozenset[str]:
    return frozenset(option for action in parser._actions for option in action.option_strings)


def _assert_exact_migration_parser_surface(parser: argparse.ArgumentParser) -> None:
    """Measure the parser returned by ``build_parser``, not source spellings.

    This observation covers argparse actions and choices realized before the
    assertion. It cannot observe argv handling outside argparse or a mutation
    performed after the observation has completed.
    """
    _commands_action, commands = _realized_subcommands(parser)
    realized_commands = frozenset(commands)
    expected_commands = frozenset(_COMMANDS)
    assert realized_commands == expected_commands, (
        f"realized subcommand surface differs: {sorted(realized_commands ^ expected_commands)}"
    )
    migration = commands["journal-migrate-v1"]
    realized_options = _realized_option_strings(migration)
    expected_options = frozenset({"-h", "--help", "--root", "--expect-journal-sha256"})
    assert realized_options == expected_options, (
        "realized journal-migrate-v1 option surface differs: "
        f"{sorted(realized_options ^ expected_options)}"
    )
    positionals = [action.dest for action in migration._actions if not action.option_strings]
    assert positionals == [], f"unexpected realized migration positional surface: {positionals}"


def _cas_attribute_refs() -> list[tuple[str, str]]:
    """Every ``<module>.<attr>`` pair whose module name is one of the two owned ones.

    Used only to assert which module a name is read from, never as
    instrumentation.
    """
    refs: list[tuple[str, str]] = []
    for node in ast.walk(_cli_tree()):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in {"cas", "journal"}
        ):
            refs.append((node.value.id, node.attr))
    return refs


def _main_handler() -> ast.ExceptHandler:
    tree = _cli_tree()
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    ]
    assert len(functions) == 1, "expected exactly one top-level main()"
    tries = [node for node in ast.walk(functions[0]) if isinstance(node, ast.Try)]
    assert len(tries) == 1, "expected exactly one try statement in main()"
    handlers = tries[0].handlers
    assert len(handlers) == 1, "expected exactly one except clause in main()"
    return handlers[0]


def _handler_type_names(handler: ast.ExceptHandler) -> list[str]:
    """Every exception type one ``except`` clause names, one element per type.

    Splitting the unparsed clause on whitespace is what a tuple defeats: the
    tokens of ``(Exception, ValueError)`` are ``(Exception,`` and ``ValueError)``
    and neither is the name of anything. The tuple is therefore taken apart at
    the syntax level, so a type is found in any position and under any spelling.

    The walk recurses because a tuple may contain a tuple. Unwrapping one level
    only would return the inner group as a single unparsed string, which hides a
    forbidden name nested inside it and simultaneously rejects a correct catch
    set written with redundant parentheses -- a control that both misses the
    thing it looks for and fails when nothing is wrong.
    """
    if handler.type is None:
        return []

    def flatten(node: ast.expr) -> list[str]:
        if isinstance(node, ast.Tuple):
            return [name for element in node.elts for name in flatten(element)]
        return [ast.unparse(node)]

    return flatten(handler.type)


def _handlers() -> list[ast.ExceptHandler]:
    return [node for node in ast.walk(_cli_tree()) if isinstance(node, ast.ExceptHandler)]


def _handler_catch_names() -> list[str]:
    handler = _main_handler()
    assert handler.type is not None, "a bare except states no catch set"
    assert isinstance(handler.type, ast.Tuple), "the catch set must be a tuple"
    return _handler_type_names(handler)


def _attribute_chain(node: ast.expr) -> tuple[str | None, int]:
    """Return an attribute chain's root name and how many attribute levels it has."""
    depth = 0
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        depth += 1
        current = current.value
    if isinstance(current, ast.Name):
        return current.id, depth
    return None, depth


# --------------------------------------------------------------------------
# Recording wrappers: what the CLI passed in, not only what came back
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _IngestCall:
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    summary: cas.IngestSummary


@dataclass(frozen=True)
class _FindCall:
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    result: list[dict[str, str]]


def _recording_ingest(monkeypatch: pytest.MonkeyPatch) -> list[_IngestCall]:
    calls: list[_IngestCall] = []
    real = cas.ingest

    def recorder(*args: Any, **kwargs: Any) -> Any:
        summary = real(*args, **kwargs)
        calls.append(_IngestCall(args, dict(kwargs), summary))
        return summary

    monkeypatch.setattr(cas, "ingest", recorder)
    return calls


def _recording_find_names(monkeypatch: pytest.MonkeyPatch) -> list[_FindCall]:
    calls: list[_FindCall] = []
    real = cas.find_names

    def recorder(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        calls.append(_FindCall(args, dict(kwargs), result))
        return result

    monkeypatch.setattr(cas, "find_names", recorder)
    return calls


def _sole_error_line(text: str) -> str:
    lines = text.strip().splitlines()
    assert len(lines) == 1, f"expected exactly one stderr line, got {lines}"
    assert lines[0].startswith("castle: error:")
    return lines[0]


def _assert_verify_emission(out: str, exit_code: int) -> dict[str, Any]:
    parsed = json.loads(out)
    assert parsed["exit_code"] == exit_code
    assert (parsed["errors"] == []) is (exit_code == 0)
    assert parsed["ok"] is (exit_code == 0)
    return parsed


# --------------------------------------------------------------------------
# Parser shape and the per-command --root requirement
# --------------------------------------------------------------------------


def test_shipped_command_set_is_exactly_the_realized_set() -> None:
    assert _COMMANDS
    _assert_exact_migration_parser_surface(cli.build_parser())

    digest = "a" * 64
    option_parser = cli.build_parser()
    _commands_action, option_commands = _realized_subcommands(option_parser)
    migration = option_commands["journal-migrate-v1"]
    migration.add_argument("--force", action="store_true")
    parsed = option_parser.parse_args(
        [
            "journal-migrate-v1",
            "--root",
            "store",
            "--expect-journal-sha256",
            digest,
            "--force",
        ]
    )
    assert parsed.force is True
    assert "--force" in _realized_option_strings(migration)
    with pytest.raises(AssertionError, match="--force"):
        _assert_exact_migration_parser_surface(option_parser)

    command_parser = cli.build_parser()
    commands_action, command_choices = _realized_subcommands(command_parser)
    dynamic_command = "".join(("journal", "-", "shadow"))
    commands_action.add_parser(dynamic_command)
    assert dynamic_command in commands_action.choices
    assert command_parser.parse_args([dynamic_command]).command == dynamic_command
    assert dynamic_command not in command_choices
    with pytest.raises(AssertionError, match=dynamic_command):
        _assert_exact_migration_parser_surface(command_parser)


# --------------------------------------------------------------------------
# The anti-widening control: the read commands do not refuse an absent root
# --------------------------------------------------------------------------


def test_no_unguarded_command_brings_a_store_into_being(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "operand-source"
    source.mkdir()
    unguarded = [
        name
        for name in _COMMANDS
        if name not in _EXPECTED_GUARDED and name not in {"init", "sync-s3"}
    ]
    assert unguarded
    for index, command in enumerate(unguarded):
        root = tmp_path / f"absent-{index}"
        cli.main([command, "--root", str(root), *_operands(command, _MINIMAL_OPERANDS, source)])
        assert not root.exists(), f"{command} brought a store into being"
    _lifecycle_commands(tmp_path, source, capsys)


# --------------------------------------------------------------------------
# init: the sole sanctioned store creation
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# One handler, one line, three genuinely different error categories
# --------------------------------------------------------------------------


def test_every_error_category_becomes_one_error_line_and_the_categories_are_distinct(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = []
    for name in ("one", "two", "three"):
        parent = tmp_path / name
        parent.mkdir()
        roots.append(_store(parent))
    malformed_store, bad_journal_store, absent_object_store = roots
    (bad_journal_store / "journal.jsonl").write_text("not json at all\n", encoding="utf-8")

    cases = [
        ["names", "--root", str(malformed_store), "sha256:not-hex"],
        ["rebuild-index", "--root", str(bad_journal_store)],
        ["get", "--root", str(absent_object_store), _ABSENT_ID],
    ]
    for argv in cases:
        exit_code = cli.main(argv)
        captured = capsys.readouterr()
        assert exit_code == 2, argv
        line = _sole_error_line(captured.err)
        assert "Traceback" not in captured.err
        assert line

    with pytest.raises(journal.CastleError) as malformed:
        cas.names_for(malformed_store, "sha256:not-hex")
    with pytest.raises(journal.CastleError) as bad_journal:
        cas.rebuild_index(bad_journal_store)
    with pytest.raises(journal.CastleError) as absent_object:
        cas.require_object(absent_object_store, _ABSENT_ID)

    assert type(malformed.value) is journal.CastleError
    assert isinstance(bad_journal.value, journal.JournalError)
    assert isinstance(absent_object.value, cas.CasError)
    raised = {type(malformed.value), type(bad_journal.value), type(absent_object.value)}
    assert len(raised) == 3
    from castle import excision, store_state

    for owner, code in (
        (store_state.StoreStateError, "store-retired"),
        (store_state.StoreStateError, "store-identity-required"),
        (store_state.StoreStateError, "store-state-invalid"),
        (store_state.StoreStateError, "store-not-active"),
        (store_state.StoreStateError, "store-epoch-mismatch"),
        (store_state.StoreStateError, "store-identity-aliased"),
        (store_state.StoreStateError, "store-preparation-failed"),
        (excision.ExcisionError, "excision-recovery-state"),
    ):

        def refuse(*_args: Any) -> Any:
            raise owner(code) from OSError("private path and SQL cause")

        with monkeypatch.context() as controlled:
            controlled.setattr(cas, "require_object", refuse)
            assert cli.main(["get", "--root", str(malformed_store), _ABSENT_ID]) == 2
        captured = capsys.readouterr()
        assert captured.err == f"castle: error: {code}\n"
        assert captured.out == ""


# --------------------------------------------------------------------------
# sync-s3 refuses before anything is resolved
# --------------------------------------------------------------------------


def test_sync_s3_refuses_before_anything_is_resolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "absent-root"
    with _forbid_library() as (module, patched):
        assert patched
        assert patched >= _LIBRARY_FLOOR
        exit_code = module.main(["sync-s3", "--root", str(root), "--bucket", "b", "--profile", "p"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert _sole_error_line(captured.err) == _SYNC_REFUSAL
    assert _FIRED == []
    assert not root.exists()


# --------------------------------------------------------------------------
# JSON output shapes are the library's, and the arguments are the caller's
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["object_stat", "store_stat", "names_for", "find_names", "verify"])
def test_read_command_json_equals_the_library_value(
    case: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = _store(tmp_path)
    cas_id = _id_for(store, "alpha.txt")
    invocations: dict[str, tuple[list[str], Any]] = {
        "object_stat": (
            ["stat", "--root", str(store), cas_id],
            cas.object_stat(store, cas_id),
        ),
        "store_stat": (["stat", "--root", str(store)], cas.store_stat(store)),
        "names_for": (
            ["names", "--root", str(store), cas_id],
            cas.names_for(store, cas_id),
        ),
        "find_names": (
            ["find", "--root", str(store), "only-in-this"],
            cas.find_names(store, "only-in-this"),
        ),
        "verify": (["verify", "--root", str(store)], cas.verify(store, sample=None).as_dict()),
    }
    argv, expected = invocations[case]
    assert expected, "an empty library value cannot discriminate an unreshaped output"
    exit_code = cli.main(argv)
    captured = capsys.readouterr()
    assert exit_code == 0
    assert json.loads(captured.out) == expected


# --------------------------------------------------------------------------
# ingest option propagation
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Binary --cat, --out, and name selection
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# ingest exit codes
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# verify --full and --sample
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Import discipline, the owning-module rule, and the binding route
# --------------------------------------------------------------------------


def test_import_discipline() -> None:
    for first in ("journal", "store_state", "cas", "excision"):
        script = (
            "import sys\n"
            "import castle\n"
            "assert not any(name.startswith('castle.') for name in sys.modules)\n"
            f"import castle.{first}\n"
            "import castle.journal, castle.store_state, castle.cas, castle.excision, castle.cli\n"
            "assert 'castle.journal_migrate_v1' not in sys.modules\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
    tree = _cli_tree()
    nodes = [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]
    top_level = [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]
    assert nodes
    assert len(nodes) >= len(top_level)

    roots: set[str] = set()
    dotted: set[str] = set()
    castle_names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                dotted.add(alias.name)
                roots.add(alias.name.split(".")[0])
        else:
            assert node.level == 0, "the deliverable has no relative-import route"
            module = node.module or ""
            dotted.add(module)
            roots.add(module.split(".")[0])
            if module == "castle":
                castle_names.update(alias.name for alias in node.names)

    assert roots
    assert "castle" in roots
    assert roots <= set(sys.stdlib_module_names) | {"castle"}
    assert castle_names == {"__version__", "cas", "journal", "store_state", "journal_migrate_v1"}

    top_level_castle_names = {
        alias.name
        for node in top_level
        if isinstance(node, ast.ImportFrom) and node.module == "castle"
        for alias in node.names
    }
    assert top_level_castle_names == {"__version__", "cas", "journal", "store_state"}
    migration_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "castle"
        and [alias.name for alias in node.names] == ["journal_migrate_v1"]
    ]
    assert len(migration_imports) == 1
    owners = [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and migration_imports[0] in ast.walk(node)
    ]
    assert owners == ["_run_journal_migrate_v1"]

    imported_modules = {
        f"castle.{name}"
        for name in castle_names
        if isinstance(getattr(cli, name, None), types.ModuleType)
    }
    assert imported_modules == {"castle.cas", "castle.journal", "castle.store_state"}
    assert "castle.journal_migrate_v1" not in dotted
    assert "castle.store_merge" not in dotted
    assert "importlib" not in roots
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "__import__"
    ]


# --------------------------------------------------------------------------
# Entry point and dependency metadata
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# End-to-end: round trip, verify exit codes, index reconstruction
# --------------------------------------------------------------------------


def test_ingest_then_get_round_trips_byte_identically(
    tmp_path: Path, capfdbinary: pytest.CaptureFixture[bytes]
) -> None:
    store = tmp_path / "store"
    source, contents = _source_dir(tmp_path)
    assert cli.main(["init", "--root", str(store)]) == 0
    capfdbinary.readouterr()
    assert (
        cli.main(["ingest", "--root", str(store), "--source", str(source), "--source-id", "demo"])
        == 0
    )
    summary = json.loads(capfdbinary.readouterr().out.decode())
    relpaths = {receipt["relpath"] for receipt in summary["receipts"]}
    assert relpaths == set(contents)

    receipt = next(item for item in summary["receipts"] if item["relpath"] == "gamma/binary.bin")
    cas_id = receipt["cas_id"]
    expected = contents["gamma/binary.bin"]

    assert cli.main(["get", "--root", str(store), cas_id, "--cat"]) == 0
    assert capfdbinary.readouterr().out == expected

    destination = tmp_path / "materialized"
    destination.mkdir()
    assert cli.main(["get", "--root", str(store), cas_id, "--out", str(destination)]) == 0
    capfdbinary.readouterr()
    assert (destination / "binary.bin").read_bytes() == expected


def _lifecycle_commands(tmp_path: Path, source: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import uuid

    from castle import store_state

    for phase, code in (("retired", "store-retired"), ("prepared", "store-not-active")):
        root = tmp_path / phase
        journal.ensure_store(root)
        if phase == "retired":
            (root / "retired.json").write_bytes(b"retirement fences before parsing")
        else:
            previous = store_state.read_store_identity(root)
            successor = store_state.StoreIdentity(
                str(uuid.uuid4()), previous.lineage_id, str(uuid.uuid4()), 1, previous.epoch_id
            )
            binding = {
                "operation_id": str(uuid.uuid4()),
                "predecessor": previous.to_dict(),
                "successor": successor.to_dict(),
            }
            (root / "store.json").write_bytes(
                store_state._canonical(
                    store_state._store_metadata(
                        successor, "prepared", binding, "sha256:" + "a" * 64
                    )
                )
            )
        before = _tree_snapshot(root)
        seen = []
        for command in _COMMANDS:
            capsys.readouterr()
            result = cli.main(
                [command, "--root", str(root), *_operands(command, _MINIMAL_OPERANDS, source)]
            )
            captured = capsys.readouterr()
            seen.append(command)
            assert result == 2, command
            if command == "sync-s3":
                assert (
                    captured.err == "castle: error: sync-s3 is not implemented; "
                    "encrypted S3 replication is reserved for a later phase\n"
                )
            else:
                assert captured.err == f"castle: error: {code}\n", command
            assert captured.out == "", command
            assert _tree_snapshot(root) == before, command
        assert set(seen) == {
            "init",
            "ingest",
            "get",
            "stat",
            "names",
            "find",
            "verify",
            "rebuild-index",
            "journal-migrate-v1",
            "sync-s3",
        }
