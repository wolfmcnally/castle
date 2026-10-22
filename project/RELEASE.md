# Preparing a release

Version 0.2.0 adds the namespace capabilities. It adds the qualified namespace library/CLI, new-byte publication, frozen folders, explicit snapshots/export and coordinated library excision/recovery to the existing CAS core. The historical 0.1.1 release and artifacts remain unchanged. GitHub Releases records actual publication status; the changelog states capability scope, not authority to publish.

## Build local artifacts

Work from this standalone package directory with `uv` installed. The managed runtime, development lockfile and build backend version are declared here. These commands build local artifacts and do not upload them.

```bash
uv sync --locked --managed-python
```

Create a fresh directory for the artifacts and clean-install check:

```bash
CASTLE_RELEASE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/castle-release.XXXXXX")
```

```bash
uv build --out-dir "$CASTLE_RELEASE_DIR/dist"
```

Inspect both the wheel and source archive before approving them. Each must include the MIT license; the source archive must include the package source, README, changelog and this procedure. The wheel must contain only package modules, distribution metadata and license material. Neither artifact may include stores, private inputs, credentials, repository planning or execution evidence. Check the metadata version against `castle.__version__`, record the archive hashes and tool versions, and retain the exact artifacts being considered for publication.

## Disposable clean-install check

Create an environment outside this checkout so an import cannot accidentally come from the source directory:

```bash
uv venv --managed-python --python 3.12 "$CASTLE_RELEASE_DIR/venv"
```

Install the exact wheel without resolving runtime dependencies:

```bash
uv pip install --python "$CASTLE_RELEASE_DIR/venv/bin/python" --no-deps "$CASTLE_RELEASE_DIR/dist/castle-0.2.0-py3-none-any.whl"
```

```bash
cd "$CASTLE_RELEASE_DIR"
```

```bash
./venv/bin/python -c 'import castle; print(castle.__version__, castle.__file__)'
```

Expect version `0.2.0` and a module path inside this new environment. Inspect the actual command inventory:

```bash
./venv/bin/castle --help
```

Create only disposable input:

```bash
mkdir source
```

```bash
printf 'disposable release smoke input\n' > source/example.txt
```

```bash
./venv/bin/castle init --root store
```

```bash
./venv/bin/castle ingest --root store --source source --source-id release-smoke
```

```bash
./venv/bin/castle stat --root store
```

```bash
./venv/bin/castle verify --root store
```

Expect one stored object and a clean verification. Rebuild and verify the derived index:

```bash
./venv/bin/castle rebuild-index --root store
```

```bash
./venv/bin/castle verify --root store
```

Repeat artifact installation and the smoke on each release platform. Repository maintainers run `./bin/check` on macOS and Linux against the exact candidate. The README’s disposable excision walkthrough supplies the library recovery demo; its human acceptance remains separate from the CLI smoke.

## Maintainer release decisions

Before a tag or upload, approve the exact version, capabilities and inspected artifact hashes. Run the clean-install smoke on each supported platform and record results. Registry name availability and publication credentials are separate prerequisites; do not assume the distribution name is available.

Persisted schema identifiers are part of the data format, not cosmetic package labels. Any change requires an explicit compatibility and migration design. No release step here migrates live stores or grants permission to operate on real data.

Record the accepted commit, artifact hashes, build/runtime versions and macOS/Linux results. Publish only the inspected wheel and source archive plus their inspected SHA256SUMS file. This procedure does not itself create a tag, upload a package, change a remote or rewrite history.


## Namespace release qualification

Version 0.2.0 release qualification must retain exact artifact and disclosure approval before any tag or upload. The repository's `.github/scripts/package-qualify.py` is the shared local/CI qualification driver: provide the resolved installed uv via --uv and an absent external directory via --output. It builds and independently allowlists the wheel/sdist, checks metadata/license/backend, records exact hashes, safely unpacks the validated sdist and builds its wheel with the pinned backend. Each wheel is installed without runtime dependency resolution into its own external environment, then the existing installed smoke exercises CAS-only commands, namespace create/replace/retry, stable IDs, empty directories and snapshot export. The source archive itself is not claimed to be installed bytes; the rebuilt wheel's hash/generator records that chain.

Record the native and Linux candidate/artifact/runtime/filesystem results and all Darwin-only cases unrun on Linux. Consult NAMESPACE_ENVIRONMENT.md for the complete host/protected qualification matrix. Local synthetic tests, process kills and injected sync failures do not certify hardware power loss, physical erasure, native VFS coverage, encrypted custody or external backups. Manual namespace error/naming/retry-window judgment and a qualified consumer release remain open owner criteria. No command here tags, publishes, uploads or operates on live data.


## Publication decision procedure

The intended existing destination is the public wolfmcnally/castle GitHub repository and its GitHub Releases assets. For version 0.2.0 the tag is v0.2.0. For publication, obtain explicit approval of the accepted source commit, all unpushed source history, its inspected wheel and source archive plus a matching SHA256SUMS file, and the exact capability/platform/privacy disclosures. Publish only those approved identities to this destination. No package registry or PyPI publication is included. Preparation alone authorizes no tag, source push, release creation or upload; published 0.1.1 bytes must not be replaced. GitHub Releases records publication status.

Before deciding, review [plain privacy and publication limits](NAMESPACE_ENVIRONMENT.md#plain-privacy-and-publication-limits), the source/history disclosure findings, actual platform results/skips and the exact retained artifact hashes. Source history and release-package allowlists are separate scopes. Manual namespace usability remains an owner judgment; encrypted custody, native VFS/device hardware power loss, physical erasure, live-data adoption and external copies remain unqualified and outside the proposed claim.
