"""Closed JSON transport over namespace APIs; no independent storage semantics."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Iterator, Mapping
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, TextIO

_MUTATIONS = {
    "create-directory": "create-directory",
    "create-file": "create-file",
    "replace-content": "replace-content",
    "rename": "relocate",
    "move": "relocate",
    "duplicate": "duplicate",
    "delete": "delete-entry",
    "delete-subtree": "delete-subtree",
    "set-metadata": "set-metadata",
    "import-folder": "import-folder",
    "snapshot": "snapshot",
    "delete-snapshot": "delete-snapshot",
    "expire-results": "expire-results",
}
_READS = {
    "read": "get-entry",
    "list": "list-directory",
    "resolve": "resolve",
    "inspect-snapshot": "inspect-snapshot",
    "export-snapshot": "export-snapshot",
}


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("namespace", help="explicit unprotected local namespace")
    parser.add_argument("--root", type=Path, required=True, help="existing active CAS root")
    parser.add_argument("--namespace-root", type=Path)
    parser.add_argument(
        "--domain-lock", type=Path, help="shared external domain lock; never removed"
    )
    verbs = parser.add_subparsers(dest="namespace_command", required=True)
    for name in ("identity", "init", "info", "status", "recover", *_MUTATIONS, *_READS):
        verb = verbs.add_parser(name)
        if name in {"init", "recover", *_MUTATIONS, *_READS}:
            verb.add_argument("--request-file", type=Path, required=True)
        if name == "status":
            verb.add_argument("operation_id")
        if name in {"create-file", "replace-content", "import-folder"}:
            sources = verb.add_mutually_exclusive_group()
            sources.add_argument("--content", action="append", default=[], metavar="CasID=FILE")
            sources.add_argument("--content-file", type=Path)
        if name == "export-snapshot":
            verb.add_argument("--out", type=Path, required=True, help="absent export root")


def _invalid() -> None:
    from castle import namespace as n

    n._refuse()


def _pairs(items: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            _invalid()
        result[key] = value
    return result


def _integer(value: str) -> int:
    if value == "-0":
        _invalid()
    return int(value)


def _noninteger(value: str) -> None:
    _invalid()


def _json_file(file: Path) -> Any:
    from castle import namespace as n

    try:
        with file.open("rb") as stream:
            raw = stream.read(n.MAX_REQUEST_BYTES + 1)
        if len(raw) > n.MAX_REQUEST_BYTES:
            n._refuse("namespace-limit-exceeded")
        result = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_int=_integer,
            parse_float=_noninteger,
            parse_constant=_noninteger,
        )
        n.canonical_bytes(result)
        return result
    except (OSError, ValueError, RecursionError):
        _invalid()


def _request(file: Path, kind: type, expected: str | None = None) -> Any:
    from castle import namespace as n
    from castle import store_state

    value = _json_file(file)
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(kind)}:
        _invalid()
    value = dict(value)
    if value.pop("schema") != kind.__dataclass_fields__["schema"].default:
        _invalid()
    try:
        value["expected_cas"] = store_state._identity(value["expected_cas"])
        result = kind(**value)
    except (TypeError, ValueError, store_state.StoreStateError):
        _invalid()
    if expected is not None and result.kind != expected:
        n._refuse()
    return result


class _Sources(Mapping[str, bytes]):
    """Do not inspect transport until execute has checked a retained outcome."""

    def __init__(self, associations: dict[str, Path], file: Path | None) -> None:
        self.associations = associations
        self.file = file
        self.loaded = file is None

    def _load(self) -> None:
        from castle import namespace as n

        if not self.loaded:
            value = _json_file(self.file)
            if not isinstance(value, dict) or any(
                not isinstance(key, str)
                or n._CAS.fullmatch(key) is None
                or not isinstance(file, str)
                or not file
                for key, file in value.items()
            ):
                _invalid()
            self.associations = {key: Path(file) for key, file in value.items()}
            self.loaded = True

    def __iter__(self) -> Iterator[str]:
        self._load()
        return iter(self.associations)

    def __len__(self) -> int:
        self._load()
        return len(self.associations)

    def __getitem__(self, key: str) -> bytes:
        self._load()
        file = self.associations[key]
        descriptor = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                _invalid()
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                payload = stream.read()
            after = os.fstat(descriptor)
            named = file.stat(follow_symlinks=False)
            signatures = [
                (i.st_dev, i.st_ino, i.st_size, i.st_mtime_ns, i.st_ctime_ns)
                for i in (before, after, named)
            ]
            if (
                signatures[0] != signatures[1]
                or signatures[1] != signatures[2]
                or len(payload) != before.st_size
            ):
                _invalid()
            return payload
        finally:
            os.close(descriptor)


def _sources(arguments: argparse.Namespace) -> Mapping[str, bytes]:
    from castle import namespace as n

    result = {}
    for item in getattr(arguments, "content", []):
        key, separator, file = item.partition("=")
        if not separator or n._CAS.fullmatch(key) is None or not file or key in result:
            _invalid()
        result[key] = Path(file)
    return _Sources(result, getattr(arguments, "content_file", None))


def _run(arguments: argparse.Namespace) -> Any:
    from castle import namespace as n
    from castle import store_state
    from castle.namespace_environment import DirectoryDestination, LocalSession

    verb = arguments.namespace_command
    recovery = _json_file(arguments.request_file) if verb == "recover" else None
    if recovery is not None:
        if not isinstance(recovery, dict) or set(recovery) != {
            "predecessor_root",
            "successor_root",
            "operation_id",
            "expected_predecessor",
            "action",
        }:
            _invalid()
        try:
            recovery["expected_predecessor"] = store_state._identity(
                recovery["expected_predecessor"]
            )
            for key in ("predecessor_root", "successor_root"):
                if not isinstance(recovery[key], str) or not recovery[key]:
                    _invalid()
                recovery[key] = Path(recovery[key])
            if recovery["predecessor_root"].resolve() != arguments.root.resolve():
                _invalid()
        except (OSError, store_state.StoreStateError):
            _invalid()
    if not arguments.root.is_dir() or arguments.root.is_symlink():
        n._refuse("namespace-root-unsafe")
    if recovery is None:
        try:
            store_state._require_active(arguments.root)
            binding = store_state.read_store_identity(arguments.root)
        except store_state.StoreStateError:
            n._refuse("namespace-binding-mismatch")
    if verb == "identity":
        return binding
    if arguments.namespace_root is None or arguments.domain_lock is None:
        _invalid()
    namespace_root, lock = arguments.namespace_root, arguments.domain_lock
    if (
        namespace_root.is_symlink()
        or not namespace_root.parent.is_dir()
        or not lock.parent.is_dir()
        or lock.is_symlink()
        or (verb != "init" and not namespace_root.is_dir())
        or (verb != "init" and not lock.is_file())
    ):
        n._refuse("namespace-root-unsafe")
    session = LocalSession(namespace_root, arguments.root, lock, create_domain_lock=verb == "init")
    if verb == "init":
        return n.create_namespace(
            namespace_root,
            arguments.root,
            session,
            _request(arguments.request_file, n.InitializeRequest),
        )
    if verb == "recover":
        return n.recover_namespace(namespace_root, arguments.root, session, recovery)
    request = None
    sources = {}
    if verb in _MUTATIONS:
        request = _request(arguments.request_file, n.MutationRequest, _MUTATIONS[verb])
        sources = _sources(arguments)
    elif verb in _READS:
        request = _request(arguments.request_file, n.ReadRequest, _READS[verb])
    with n.open_namespace(namespace_root, arguments.root, session) as handle:
        if verb == "info":
            return handle.info()
        if verb == "status":
            return n.namespace_operation(handle, arguments.operation_id)
        if verb in _MUTATIONS:
            return n.execute_namespace(handle, request, sources)
        destination = (
            DirectoryDestination(arguments.out, namespace_root, arguments.root)
            if verb == "export-snapshot"
            else None
        )
        return n.read_namespace(handle, request, destination=destination)


def _failed_response(stream: TextIO) -> int:
    # A failed buffered flush must not be retried by interpreter finalization:
    # CPython would replace our unknown-response status with exit 120.
    try:
        stream.close()
    except (OSError, ValueError):
        pass  # The failed terminal response is discarded, never a rollback.
    return 74


def run(arguments: argparse.Namespace) -> int:
    """Compute a typed result before emitting it; transport failure is unknown."""
    from castle import journal
    from castle import namespace as n

    try:
        value = asdict(_run(arguments))
    except n.NamespaceError as error:
        value = {
            key: getattr(error, key)
            for key in ("code", "namespace_id", "operation_id", "actual_revision", "effect")
        }
        try:
            print(json.dumps(value, sort_keys=True), file=sys.stderr, flush=True)
        except (OSError, ValueError):
            return _failed_response(sys.stderr)
        return 2
    except (OSError, journal.CastleError):
        # No raw paths/backend details and no false assertion of rollback.
        value = {
            "code": "namespace-storage-recovery-required",
            "namespace_id": None,
            "operation_id": None,
            "actual_revision": None,
            "effect": "recovery-required",
        }
        try:
            print(json.dumps(value, sort_keys=True), file=sys.stderr, flush=True)
        except (OSError, ValueError):
            return _failed_response(sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    try:
        # ASCII JSON escaping also works with a caller-selected ASCII stream;
        # decoded names/content metadata retain their exact Unicode values.
        print(json.dumps(value, sort_keys=True), flush=True)
        return 0
    except (OSError, ValueError):
        return _failed_response(sys.stdout)
    except KeyboardInterrupt:
        return 130
