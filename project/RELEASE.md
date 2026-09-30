# Preparing a release

Version 0.1.0 is the first public release target; GitHub Releases records publication status. The package includes the delivered portable core, deliberate v1 migration, Darwin-only clone/merge, and portable successor-epoch excision described in the README. The changelog states that scope; it is not a publication record.

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
uv pip install --python "$CASTLE_RELEASE_DIR/venv/bin/python" --no-deps "$CASTLE_RELEASE_DIR/dist/castle-0.1.0-py3-none-any.whl"
```

```bash
cd "$CASTLE_RELEASE_DIR"
```

```bash
./venv/bin/python -c 'import castle; print(castle.__version__, castle.__file__)'
```

Expect version `0.1.0` and a module path inside this new environment. Inspect the actual command inventory:

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

Record the accepted commit, artifact hashes, build/runtime versions and macOS/Linux results. Publish only the inspected wheel and source archive. This procedure does not itself create a tag, upload a package, change a remote or rewrite history.
