"""Typed, serialized namespace operations and immutable content publication.

The caller supplies authorization and one stable protection-domain lock. This
library publishes immutable bytes under that fence, without cross-domain atomicity or encryption.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Protocol

from castle.journal import CastleError
from castle.store_state import StoreIdentity

MAX_ENTRIES = 100000
MAX_REQUEST_BYTES = 16777216
MAX_MANIFEST_BYTES = 67108864
MAX_INTEGER = 9007199254740991
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_CAS = re.compile(r"sha256:[0-9a-f]{64}")
_CODES = frozenset(
    "namespace-access-denied namespace-session-expired namespace-root-unsafe "
    "namespace-authority-missing namespace-format-unsupported namespace-binding-mismatch "
    "namespace-request-invalid namespace-limit-exceeded namespace-coordinator-busy "
    "namespace-revision-conflict namespace-revision-exhausted namespace-entry-missing "
    "namespace-kind-mismatch namespace-root-immutable namespace-name-conflict namespace-cycle "
    "namespace-directory-not-empty namespace-operation-conflict namespace-operation-retired "
    "namespace-content-mismatch namespace-input-unfrozen namespace-dependency-conflict "
    "namespace-protection-unsupported namespace-storage-recovery-required "
    "namespace-replacement-invalid namespace-io-failed".split()
)


class NamespaceError(CastleError):
    """A fixed safe refusal; backend details are deliberately not exposed."""

    def __init__(
        self,
        code: str,
        namespace_id: str | None = None,
        operation_id: str | None = None,
        actual_revision: int | None = None,
        effect: str = "none",
    ) -> None:
        if code not in _CODES or effect not in {"none", "namespace-committed", "recovery-required"}:
            raise ValueError("namespace-request-invalid")
        self.code = code
        self.namespace_id = namespace_id
        self.operation_id = operation_id
        self.actual_revision = actual_revision
        self.effect = effect
        super().__init__(code)


def _refuse(code: str = "namespace-request-invalid") -> None:
    raise NamespaceError(
        code,
        effect="recovery-required" if code == "namespace-storage-recovery-required" else "none",
    ) from None


def _uuid(value: object) -> str:
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        _refuse()
    return value


def _integer(value: object, *, nonnegative: bool = True) -> int:
    if type(value) is not int or not (-MAX_INTEGER <= value <= MAX_INTEGER):
        _refuse()
    if nonnegative and value < 0:
        _refuse()
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        _refuse()
    return value


def _name(value: object) -> str:
    value = unicodedata.normalize("NFC", _string(value))
    if (
        not 1 <= len(value.encode("utf-8")) <= 255
        or value in {".", ".."}
        or any(c in "/\\" or unicodedata.category(c) == "Cc" for c in value)
    ):
        _refuse()
    return value


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name.casefold())


def canonical_bytes(value: object, *, limit: int = MAX_REQUEST_BYTES) -> bytes:
    """Canonical UTF-8 scalar-order JSON, safe integers and exactly one LF."""
    parts: list[str] = []
    size = 0

    def emit(part: str) -> None:
        nonlocal size
        size += len(part.encode("utf-8"))
        if size > limit:
            _refuse("namespace-limit-exceeded")
        parts.append(part)

    def string(text: str) -> str:
        _string(text)
        return (
            '"'
            + "".join(
                '\\"'
                if c == '"'
                else "\\\\"
                if c == "\\"
                else f"\\u{ord(c):04x}"
                if ord(c) < 32
                else c
                for c in text
            )
            + '"'
        )

    def encode(item: object, depth: int) -> None:
        if depth > 64:
            _refuse("namespace-limit-exceeded")
        if item is None:
            emit("null")
        elif type(item) is bool:
            emit("true" if item else "false")
        elif type(item) is int:
            emit(str(_integer(item, nonnegative=False)))
        elif isinstance(item, str):
            emit(string(item))
        elif isinstance(item, list):
            emit("[")
            for index, child in enumerate(item):
                if index:
                    emit(",")
                encode(child, depth + 1)
            emit("]")
        elif isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                _refuse()
            emit("{")
            for index, k in enumerate(sorted(item)):
                if index:
                    emit(",")
                emit(string(k) + ":")
                encode(item[k], depth + 1)
            emit("}")
        else:
            _refuse()

    encode(value, 0)
    emit("\n")
    return "".join(parts).encode("utf-8")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _refuse()
        result[key] = value
    return result


def _decode(raw: str | bytes, *, limit: int = MAX_MANIFEST_BYTES) -> Any:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs)
        if canonical_bytes(value, limit=limit) != (raw.encode() if isinstance(raw, str) else raw):
            _refuse("namespace-format-unsupported")
        return value
    except (ValueError, UnicodeError, RecursionError):
        _refuse("namespace-format-unsupported")


def _metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        _refuse()

    def visit(item: object, depth: int) -> None:
        if isinstance(item, (dict, list)):
            if depth > 32:
                _refuse("namespace-limit-exceeded")
            for child in item.values() if isinstance(item, dict) else item:
                visit(child, depth + isinstance(child, (dict, list)))

    visit(value, 1)
    raw = canonical_bytes(value, limit=65537)
    return _decode(raw, limit=65537)


def _plain(value: Any) -> Any:
    return asdict(value) if is_dataclass(value) else value


type ProtectionMode = dict[str, Any]


@dataclass(frozen=True)
class CapabilityDescriptor:
    capability_id: str | None
    protection_domain_id: str
    namespace_root: Path
    cas_root: Path
    properties: tuple[str, ...]


class AuthorizedSession(Protocol):
    """Authorization and property truth belong to the caller's trusted host."""

    def acquire_domain_ownership(self) -> AbstractContextManager[None]: ...

    def describe_capabilities(self) -> CapabilityDescriptor: ...

    def check_authorized(self, operation_kind: str) -> None: ...


REQUIRED_PROPERTIES = frozenset(
    {
        "locking",
        "path-safety",
        "immutable-staging",
        "durable-publication",
        "sqlite-extra",
        "restart-recovery",
        "revocation",
        "authorized-export",
    }
)


def _authorize(session: AuthorizedSession, kind: str) -> None:
    try:
        session.check_authorized(kind)
    except NamespaceError:
        raise
    except Exception:
        _refuse("namespace-access-denied")


@dataclass(frozen=True)
class InitializeRequest:
    namespace_id: str
    root_id: str
    expected_cas: StoreIdentity
    policy: dict[str, Any]
    protection: dict[str, Any]
    schema: str = field(default="castle.namespace_initialize.v1", init=False)


@dataclass(frozen=True)
class MutationRequest:
    namespace_id: str
    expected_cas: StoreIdentity
    expected_revision: int
    operation_id: str
    kind: str
    args: dict[str, Any]
    schema: str = field(default="castle.namespace_request.v1", init=False)


@dataclass(frozen=True)
class ReadRequest:
    namespace_id: str
    expected_cas: StoreIdentity
    at_revision: int | None
    kind: str
    args: dict[str, Any]
    schema: str = field(default="castle.namespace_read_request.v1", init=False)


@dataclass(frozen=True)
class NamespaceInfo:
    namespace_id: str
    root_id: str
    policy: dict[str, Any]
    cas: StoreIdentity
    revision: int
    database_generation: str
    protection: dict[str, Any]
    schema: str = field(default="castle.namespace_info.v1", init=False)


@dataclass(frozen=True)
class EntryRecord:
    entry_id: str
    kind: str
    parent_id: str | None
    name: str | None
    metadata: dict[str, Any]
    cas_id: str | None
    size: int | None
    changed_revision: int


@dataclass(frozen=True)
class MutationResult:
    namespace_id: str
    operation_id: str
    request_fingerprint: str
    kind: str
    revision_before: int
    revision_after: int
    affected_entry_ids: list[str]
    snapshot: None
    cas_binding: StoreIdentity
    outcome: str = field(default="committed", init=False)
    schema: str = field(default="castle.namespace_result.v1", init=False)


@dataclass(frozen=True)
class ReadResult:
    namespace_id: str
    revision: int
    cas_binding: StoreIdentity
    value: NamespaceInfo | EntryRecord | list[EntryRecord]
    schema: str = field(default="castle.namespace_read.v1", init=False)


@dataclass(frozen=True)
class OperationStatus:
    namespace_id: str
    operation_id: str
    state: str
    result: MutationResult | None
    error_code: str | None
    next_action: str


@dataclass(frozen=True)
class RecoveryResult:
    namespace_id: str
    state: str
    revision: int
    cas_binding: StoreIdentity
    completed_operation_ids: list[str]


def _info(value: dict[str, Any]) -> NamespaceInfo:
    value = dict(value)
    if value.pop("schema", None) != "castle.namespace_info.v1":
        _refuse("namespace-format-unsupported")
    value["cas"] = StoreIdentity(**value["cas"])
    return NamespaceInfo(**value)


def _result(value: dict[str, Any]) -> MutationResult:
    value = dict(value)
    if (
        value.pop("schema", None) != "castle.namespace_result.v1"
        or value.pop("outcome", None) != "committed"
    ):
        _refuse("namespace-format-unsupported")
    value["cas_binding"] = StoreIdentity(**value["cas_binding"])
    return MutationResult(**value)


def create_namespace(
    namespace_root: Path, cas_root: Path, session: AuthorizedSession, request: InitializeRequest
) -> NamespaceInfo:
    from castle.namespace_store import NamespaceHandle

    with NamespaceHandle(namespace_root, cas_root, session, initialize=request) as handle:
        return handle.info()


def open_namespace(
    namespace_root: Path, cas_root: Path, session: AuthorizedSession
) -> NamespaceHandle:
    from castle.namespace_store import NamespaceHandle

    return NamespaceHandle(namespace_root, cas_root, session)


def execute_namespace(
    handle: NamespaceHandle,
    request: MutationRequest,
    content_sources: Mapping[str, bytes],
    excision_roots: object = None,
) -> MutationResult:
    if excision_roots is not None:
        _authorize(handle.session, request.kind if type(request) is MutationRequest else "execute")
        _refuse()
    return handle.execute(request, content_sources)


def read_namespace(handle: NamespaceHandle, request: ReadRequest) -> ReadResult:
    return handle.read(request)


def namespace_operation(handle: NamespaceHandle, operation_id: str) -> OperationStatus:
    return handle.operation(operation_id)


def recover_namespace(
    namespace_root: Path,
    cas_root: Path,
    session: AuthorizedSession,
    recovery_roots: object,
) -> RecoveryResult:
    _authorize(session, "recover")
    if recovery_roots is not None:
        _refuse("namespace-replacement-invalid")
    with NamespaceHandle(namespace_root, cas_root, session, recover=True) as handle:
        info = handle.info()
        return RecoveryResult(
            info.namespace_id, "ready", info.revision, info.cas, sorted(set(handle.recovered))
        )


def _fingerprint(request: dict[str, Any]) -> str:
    value = {k: v for k, v in request.items() if k != "operation_id"}
    value["schema"] = "castle.namespace_fingerprint.v1"
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


from castle.namespace_store import NamespaceHandle  # noqa: E402
