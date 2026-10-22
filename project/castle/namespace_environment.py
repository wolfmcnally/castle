"""Trusted unprotected local namespace custody and explicit snapshot output.

This reference environment is not authentication, encryption or device-level
durability qualification. All direct store users must share its domain fence.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from castle import namespace as n

_OWNERS: weakref.WeakSet[LocalSession | DirectoryDestination] = weakref.WeakSet()


def _after_fork() -> None:
    for owner in list(_OWNERS):
        owner.active = False
        for descriptor in owner.descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass
        owner.descriptors.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _close(owner: LocalSession | DirectoryDestination, descriptor: int) -> None:
    if descriptor in owner.descriptors:
        owner.descriptors.remove(descriptor)
        os.close(descriptor)


def _path_error(error: OSError) -> None:
    if error.errno in {errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.EEXIST}:
        n._refuse("namespace-root-unsafe")
    raise error


def _authority_inodes(namespace_root: Path, cas_root: Path) -> set[tuple[int, int]]:
    result = set()
    for root in (namespace_root, cas_root):
        try:
            info = root.stat()
        except FileNotFoundError:
            continue  # Explicit initialization may not have created its root yet.
        result.add((info.st_dev, info.st_ino))
    return result


def _directory(
    owner: LocalSession | DirectoryDestination,
    root: Path,
    forbidden: set[tuple[int, int]] | None = None,
) -> int:
    """Anchor each existing path component without following a symlink."""
    root = Path(os.path.abspath(root))
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    owner.descriptors.append(descriptor)
    try:

        def check() -> None:
            info = os.fstat(descriptor)
            if forbidden and (info.st_dev, info.st_ino) in forbidden:
                n._refuse("namespace-root-unsafe")

        check()
        for component in root.parts[1:]:
            following = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            owner.descriptors.append(following)
            _close(owner, descriptor)
            descriptor = following
            check()
        return descriptor
    except OSError as error:
        _close(owner, descriptor)
        _path_error(error)
    except BaseException:
        _close(owner, descriptor)
        raise


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


class LocalSession:
    """Caller-owned Unix admission on explicit unprotected local roots.

    Only explicit initialization may create the shared domain lock. Existing
    locks are never unlinked, and every coordinator must use the same lock.
    """

    def __init__(
        self,
        namespace_root: Path,
        cas_root: Path,
        domain_lock: Path,
        *,
        create_domain_lock: bool = False,
    ) -> None:
        if os.name != "posix" or not all(
            hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY", "register_at_fork")
        ):
            n._refuse("namespace-protection-unsupported")
        self.namespace_root = Path(namespace_root).resolve()
        self.cas_root = Path(cas_root).resolve()
        original_lock = Path(domain_lock).absolute()
        if original_lock.is_symlink():
            n._refuse("namespace-root-unsafe")
        try:
            self.domain_lock = original_lock.parent.resolve(strict=True) / original_lock.name
        except OSError as error:
            _path_error(error)
        if any(
            self.domain_lock == root or self.domain_lock.is_relative_to(root)
            for root in (self.namespace_root, self.cas_root)
        ):
            n._refuse("namespace-root-unsafe")
        self.create_domain_lock = create_domain_lock
        self.active = True
        self.pid = os.getpid()
        self.descriptors: list[int] = []
        _OWNERS.add(self)

    def check_authorized(self, operation_kind: str) -> None:
        if not self.active or os.getpid() != self.pid:
            n._refuse("namespace-session-expired")

    def describe_capabilities(self) -> n.CapabilityDescriptor:
        return n.CapabilityDescriptor(
            None,
            hashlib.sha256(str(self.domain_lock).encode()).hexdigest(),
            self.namespace_root,
            self.cas_root,
            (
                "locking",
                "path-safety",
                "immutable-staging",
                "durable-publication",
                "sqlite-extra",
                "restart-recovery",
                "revocation",
                "authorized-export",
            ),
        )

    @contextmanager
    def acquire_domain_ownership(self) -> Iterator[None]:
        import fcntl

        self.check_authorized("domain-ownership")
        parent = _directory(
            self, self.domain_lock.parent, _authority_inodes(self.namespace_root, self.cas_root)
        )
        descriptor = None
        try:
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if self.create_domain_lock:
                try:
                    descriptor = os.open(
                        self.domain_lock.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent
                    )
                except FileExistsError:
                    descriptor = os.open(self.domain_lock.name, flags, dir_fd=parent)
            else:
                descriptor = os.open(self.domain_lock.name, flags, dir_fd=parent)
            self.descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                n._refuse("namespace-root-unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                n._refuse("namespace-coordinator-busy")
            if not _same_file(
                info, os.stat(self.domain_lock.name, dir_fd=parent, follow_symlinks=False)
            ):
                n._refuse("namespace-root-unsafe")
            os.fsync(descriptor)
            os.fsync(parent)
            self.create_domain_lock = False
            _close(self, parent)
            self.check_authorized("domain-ownership")
        except OSError as error:
            _path_error(error)
        else:
            yield
        finally:
            _close(self, parent)
            if descriptor is not None:
                _close(self, descriptor)


class DirectoryDestination:
    """Explicit absent export root: snapshot.json beside a member-only tree/.

    The caller owns destination custody and stability. Failure preserves any
    partial output; this adapter never recursively deletes or overwrites it.
    """

    def __init__(self, output: Path, namespace_root: Path, cas_root: Path) -> None:
        if os.name != "posix" or not all(
            hasattr(os, name) for name in ("O_DIRECTORY", "O_NOFOLLOW")
        ):
            n._refuse("namespace-protection-unsupported")
        self.output = Path(os.path.abspath(output))
        self.namespace_root = Path(namespace_root).resolve()
        self.cas_root = Path(cas_root).resolve()
        for root in (self.namespace_root, self.cas_root):
            if (
                self.output == root
                or self.output.is_relative_to(root)
                or root.is_relative_to(self.output)
            ):
                n._refuse("namespace-root-unsafe")
        self.active = True
        self.pid = os.getpid()
        self.descriptors: list[int] = []
        _OWNERS.add(self)

    def _check(self) -> None:
        if not self.active or self.pid != os.getpid():
            n._refuse("namespace-session-expired")

    def _file(self, parent: int, name: str, payload: bytes) -> None:
        self._check()
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
        )
        self.descriptors.append(descriptor)
        try:
            original = os.fstat(descriptor)
            offset = 0
            while offset < len(payload):
                self._check()
                written = os.write(descriptor, memoryview(payload)[offset:])
                if written <= 0:
                    raise OSError("export short write")
                offset += written
            os.fsync(descriptor)
            if not _same_file(original, os.stat(name, dir_fd=parent, follow_symlinks=False)):
                n._refuse("namespace-root-unsafe")
        finally:
            _close(self, descriptor)

    def publish(
        self,
        view: n.SnapshotView,
        manifest_bytes: bytes,
        read_content: Callable[[str], bytes],
    ) -> n.ExportReceipt:
        self._check()
        if (
            type(view) is not n.SnapshotView
            or manifest_bytes != n.canonical_bytes(view.manifest, limit=n.MAX_MANIFEST_BYTES)
            or "sha256:" + hashlib.sha256(manifest_bytes).hexdigest() != view.snapshot.cas_id
            or len(manifest_bytes) != view.snapshot.size
        ):
            n._refuse("namespace-request-invalid")
        entries = {item["entry_id"]: item for item in view.manifest["entries"]}
        root_id = view.manifest["root_id"]
        if len(entries) != len(view.manifest["entries"]) or root_id not in entries:
            n._refuse("namespace-request-invalid")
        children: dict[str, list[dict]] = {}
        for identity, entry in entries.items():
            if identity == root_id:
                if (
                    entry["kind"] != "directory"
                    or entry["parent_id"] is not None
                    or entry["name"] is not None
                ):
                    n._refuse("namespace-request-invalid")
                continue
            n._name(entry["name"])
            if (
                entry["parent_id"] not in entries
                or entries[entry["parent_id"]]["kind"] != "directory"
            ):
                n._refuse("namespace-request-invalid")
            children.setdefault(entry["parent_id"], []).append(entry)
        forbidden = _authority_inodes(self.namespace_root, self.cas_root)
        parent = _directory(self, self.output.parent, forbidden)
        stack: list[tuple[int, int, str, os.stat_result, Iterator[dict]]] = []
        try:
            self._check()
            os.mkdir(self.output.name, 0o700, dir_fd=parent)
            root = os.open(
                self.output.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            self.descriptors.append(root)
            output_info = os.fstat(root)
            self._file(root, "snapshot.json", manifest_bytes)
            os.mkdir("tree", 0o700, dir_fd=root)
            tree = os.open("tree", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            self.descriptors.append(tree)
            stack.append(
                (
                    tree,
                    root,
                    "tree",
                    os.fstat(tree),
                    iter(sorted(children.get(root_id, []), key=lambda item: item["entry_id"])),
                )
            )
            visited = {root_id}
            while stack:
                self._check()
                directory, prior, name, original, iterator = stack[-1]
                entry = next(iterator, None)
                if entry is None:
                    os.fsync(directory)
                    stack.pop()
                    if not _same_file(original, os.stat(name, dir_fd=prior, follow_symlinks=False)):
                        n._refuse("namespace-root-unsafe")
                    _close(self, directory)
                    continue
                identity = entry["entry_id"]
                if identity in visited:
                    n._refuse("namespace-request-invalid")
                visited.add(identity)
                if entry["kind"] == "directory":
                    os.mkdir(entry["name"], 0o700, dir_fd=directory)
                    child = os.open(
                        entry["name"],
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory,
                    )
                    self.descriptors.append(child)
                    stack.append(
                        (
                            child,
                            directory,
                            entry["name"],
                            os.fstat(child),
                            iter(
                                sorted(
                                    children.get(identity, []), key=lambda item: item["entry_id"]
                                )
                            ),
                        )
                    )
                elif entry["kind"] == "file":
                    payload = read_content(entry["cas_id"])
                    if (
                        not isinstance(payload, bytes)
                        or len(payload) != entry["size"]
                        or "sha256:" + hashlib.sha256(payload).hexdigest() != entry["cas_id"]
                    ):
                        n._refuse("namespace-content-mismatch")
                    self._file(directory, entry["name"], payload)
                else:
                    n._refuse("namespace-request-invalid")
            if visited != set(entries):
                n._refuse("namespace-request-invalid")
            if not _same_file(
                output_info, os.stat(self.output.name, dir_fd=parent, follow_symlinks=False)
            ):
                n._refuse("namespace-root-unsafe")
            os.fsync(root)
            os.fsync(parent)
            observed_parent = _directory(self, self.output.parent, forbidden)
            try:
                if not _same_file(os.fstat(parent), os.fstat(observed_parent)):
                    n._refuse("namespace-root-unsafe")
            finally:
                _close(self, observed_parent)
            self._check()
            return n.ExportReceipt(view.snapshot.snapshot_id, view.snapshot.cas_id)
        except OSError as error:
            _path_error(error)
        finally:
            for descriptor in list(reversed(self.descriptors)):
                _close(self, descriptor)
