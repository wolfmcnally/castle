# Castle

Castle is a standalone content-addressable snapshot store with a Python library and CLI. The deliverable lives in [project/](project/README.md); this root retains the agent methodology, canonical skills, policies and deterministic development tools.

## Setup and checks

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

MIT, copyright 2026 Wolf McNally. Version 0.1.0 is the first public tagged release target; GitHub Releases records publication status. [Provenance](PROVENANCE.md) distinguishes sourced implementation/proof evidence from new release validation. [Release procedure](project/RELEASE.md) covers inspected artifacts and isolated installation.
