# Castle

Castle is a content-addressable storage (CAS) system for preserving and verifying immutable file snapshots, created by Wolf McNally. Objects are identified by cryptographic hashes of their contents, allowing identical data to share a stored object and applications to verify that retrieved bytes match what was originally captured.

An authoritative journal records ingestion events and provenance, while a rebuildable SQLite index supports efficient lookup. Applications can ingest files, retrieve or materialize stored objects, inspect their recorded origins, and verify the consistency of a store through a Python library and command-line interface.

Castle’s objects and journal events form a state-based conflict-free replicated data type (CRDT) within each merge epoch. Copies can evolve independently and merge their accumulated content and history deterministically, regardless of merge order or repetition. This supports parallel and offline workflows while preserving provenance. Applications control when and how replicas exchange data.

Castle provides a reusable foundation for archival, document-processing, and data-custody workflows. Its core runs on macOS and Linux, with additional native store-cloning and merging capabilities on macOS/APFS.

## Setup and checks

The deliverable lives in [project/](project/README.md); this root retains the agent methodology, canonical skills, policies and deterministic development tools.

Requires [uv](https://docs.astral.sh/uv/) >=0.9.26. The checkout provisions its own locked Python 3.12 runtime under ignored `.runtime/`; no sibling repository is required.

```bash
./bin/setup
```

```bash
./bin/check all
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CLAUDE.md](CLAUDE.md), [policies](policies/README.md) and [briefs](briefs/BRIEF.md). Canonical agent resources are in `.claude/`; `.agents/` and `.codex/` retain portable mirrors, including teach/learn workflows.

## Product scope

The portable core supports ingest, lookup, materialization, journal verification and index repair. Native store clone and merge require Darwin/APFS; Linux refuses these operations explicitly. Excision reconstructs a successor and retires the local predecessor; it does not prove physical erasure or deletion of external copies. Cloud replication is unimplemented. [Package documentation](project/README.md) owns the full capability and recovery contract.

## License and release

MIT, copyright 2026 Wolf McNally. Version 0.1.1 repairs ingest preservation after uncertain journal writes without changing the public API or store format; GitHub Releases records publication status. [Provenance](PROVENANCE.md) distinguishes sourced implementation/proof evidence from new release validation. [Release procedure](project/RELEASE.md) covers inspected artifacts and isolated installation.
