"""Castle: a standalone content-addressable snapshot store.

The package root exports the version only and imports no submodule, so
importing ``castle`` loads nothing else. Consumers import the owning module:
``castle.journal`` owns the strict v2 event format, canonical journal bytes,
deterministic ordering, journal reads, the stable sidecar lock, and the
package-root exception ``CastleError``. ``castle.cas`` owns content identity,
object layout, byte-based media detection, the sealed read-only posture,
verification, the rebuildable SQLite index, and the full write path — ingest,
deduplication, atomic publication, and typed receipts. ``castle.store_merge``
owns Darwin/APFS copy-on-write cloning and deterministic merge-back.
``castle.journal_migrate_v1`` is the isolated, once-only v1 reader and
migration operation; ordinary runtime never imports it. ``castle.cli`` owns
the command-line front end over that library: the core commands, ``init`` as
the sole sanctioned way a store root comes into existence, the migration
command's lazy import, and one error rendering.
"""

__version__ = "0.1.1"
