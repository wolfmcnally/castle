"""Command-line front end over the content-addressable snapshot store.

Every command takes ``--root``, which names one already-resolved store root.
The path is handed to the library exactly as the caller typed it — neither
expanded nor resolved — so a relative spelling stays relative and a symlinked
root stays the root the caller named.

Three commands refuse a ``--root`` that is not an existing directory, and they
refuse it before the library is reached. The store layer creates what it needs
on first write, so without that refusal a mistyped root would quietly bring a
new store into being; ``init`` is the only sanctioned way a store root appears.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from castle import __version__, cas, journal, store_state

# Membership follows the behavior, not the name. A command belongs here when it
# reaches store bootstrap: ``rebuild-index`` reads as a pure cache operation and
# still takes the journal lock, which brings the store tree, the lock sidecar,
# and the journal into being. A command later found to reach bootstrap joins
# this set, and the test suite holds an independent copy of the same population
# that has to be updated with it.
_ROOT_MUST_EXIST: frozenset[str] = frozenset({"ingest", "rebuild-index", "journal-migrate-v1"})


def _json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="The store root to operate on.",
    )


def _require_existing_root(root: Path) -> None:
    """Refuse a root under which no store can already exist.

    A regular file or a dangling symlink is refused for the same reason an
    absent path is: no store can live there, and admitting it would hand the
    library a root it would then have to refuse for an unrelated reason.
    """
    if not root.is_dir():
        raise journal.CastleError(f"store root is not an existing directory: {root}")


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser and every command's own arguments."""
    parser = argparse.ArgumentParser(
        prog="castle",
        description="Operate a content-addressable snapshot store.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"castle {__version__}",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_parser = commands.add_parser("init", help="create a store root")
    _add_root(init_parser)

    ingest = commands.add_parser("ingest", help="snapshot a source tree")
    _add_root(ingest)
    ingest.add_argument("--source", type=Path, required=True)
    ingest.add_argument("--source-id", required=True)
    ingest.add_argument("--batch")
    ingest.add_argument("--dry-run", action="store_true")

    get = commands.add_parser("get", help="resolve or rematerialize an object")
    _add_root(get)
    get.add_argument("cas_id")
    output = get.add_mutually_exclusive_group()
    output.add_argument("--out", type=Path, nargs="?", const=Path("."))
    output.add_argument("--cat", action="store_true")
    get.add_argument("--name")

    stat_parser = commands.add_parser("stat", help="show object or store totals")
    _add_root(stat_parser)
    stat_parser.add_argument("cas_id", nargs="?")

    names = commands.add_parser("names", help="list names for an object")
    _add_root(names)
    names.add_argument("cas_id")

    find = commands.add_parser("find", help="find journaled names")
    _add_root(find)
    find.add_argument("substring")

    verify = commands.add_parser("verify", help="verify objects and metadata")
    _add_root(verify)
    scope = verify.add_mutually_exclusive_group()
    scope.add_argument("--sample", type=int)
    scope.add_argument("--full", action="store_true")

    rebuild = commands.add_parser("rebuild-index", help="rebuild SQLite cache")
    _add_root(rebuild)

    migrate = commands.add_parser("journal-migrate-v1", help="migrate a v1 journal once")
    _add_root(migrate)
    migrate.add_argument("--expect-journal-sha256", required=True)

    sync = commands.add_parser("sync-s3", help="reserved for a later phase")
    _add_root(sync)
    sync.add_argument("--bucket", required=True)
    sync.add_argument("--prefix", default="")
    sync.add_argument("--profile", required=True)
    return parser


def _run_journal_migrate_v1(arguments: argparse.Namespace) -> int:
    from castle import journal_migrate_v1

    result = journal_migrate_v1.migrate_v1_to_v2(
        arguments.root,
        expect_journal_sha256=arguments.expect_journal_sha256,
    )
    _json(result.as_mapping())
    return 0


def _run(arguments: argparse.Namespace) -> int:
    # sync-s3 is unimplemented and refuses before anything is resolved, so it
    # never asks a question whose answer it would not use -- including whether
    # the root exists.
    if arguments.command == "sync-s3":
        raise journal.CastleError(
            "sync-s3 is not implemented; encrypted S3 replication is reserved for a later phase"
        )
    root = arguments.root
    if arguments.command in _ROOT_MUST_EXIST:
        _require_existing_root(root)
    if arguments.command != "journal-migrate-v1":
        store_state._require_active(root)
    if arguments.command == "init":
        journal.ensure_store(root)
        with journal.journal_lock(root):
            pass
        print(root)
        return 0
    if arguments.command == "ingest":
        summary = cas.ingest(
            root,
            arguments.source,
            arguments.source_id,
            batch=arguments.batch,
            dry_run=arguments.dry_run,
        )
        _json(summary.as_dict())
        return 1 if summary.refusals else 0
    if arguments.command == "get":
        source = cas.require_object(root, arguments.cas_id)
        if arguments.cat:
            if arguments.name is not None:
                raise cas.CasError("--name requires --out")
            with source.open("rb") as handle:
                shutil.copyfileobj(handle, sys.stdout.buffer)
            # An in-process caller reads the captured bytes with no interpreter
            # shutdown between the write and the read, so the buffer has to be
            # flushed here rather than at exit.
            sys.stdout.buffer.flush()
            return 0
        if arguments.out is not None:
            result = cas.materialize(
                root,
                arguments.cas_id,
                arguments.out,
                selected_name=arguments.name,
                use_original_name=arguments.out == Path("."),
            )
            print(result)
            return 0
        if arguments.name is not None:
            raise cas.CasError("--name requires --out")
        print(source)
        return 0
    if arguments.command == "stat":
        value = (
            cas.object_stat(root, arguments.cas_id) if arguments.cas_id else cas.store_stat(root)
        )
        _json(value)
        return 0
    if arguments.command == "names":
        _json(cas.names_for(root, arguments.cas_id))
        return 0
    if arguments.command == "find":
        _json(cas.find_names(root, arguments.substring))
        return 0
    if arguments.command == "verify":
        if arguments.sample is not None and arguments.sample < 1:
            raise cas.CasError("--sample must be at least 1")
        result = cas.verify(root, sample=arguments.sample)
        _json(result.as_dict())
        return result.exit_code
    if arguments.command == "rebuild-index":
        _json(cas.rebuild_index(root))
        return 0
    if arguments.command == "journal-migrate-v1":
        return _run_journal_migrate_v1(arguments)
    # Reaching this is a defect in the dispatcher, not a user error, so it is
    # deliberately outside the handler below.
    raise AssertionError(f"unhandled command: {arguments.command}")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse *argv*, dispatch it, and render every user-facing failure alike."""
    try:
        return _run(build_parser().parse_args(argv))
    except (OSError, journal.CastleError, sqlite3.Error) as exc:
        print(f"castle: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
