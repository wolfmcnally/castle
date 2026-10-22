"""Inspect exact local artifacts, rebuild the sdist, and smoke isolated installs.

No upload, version selection, credentials, or source-import fallback is supplied.
"""

from __future__ import annotations

import argparse
import email.parser
import hashlib
import json
import os
import platform
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

VERSION = "0.2.0"

MODULES = frozenset(
    [
        "__init__",
        "cas",
        "cas_recovery",
        "cli",
        "excision",
        "journal",
        "journal_migrate_v1",
        "namespace",
        "namespace_cli",
        "namespace_content",
        "namespace_environment",
        "namespace_excision",
        "namespace_onboarding",
        "namespace_replacement",
        "namespace_snapshots",
        "namespace_store",
        "namespace_transactions",
        "store_merge",
        "store_state",
    ]
)
TESTS = frozenset(
    [
        "__init__",
        "conftest",
        "test_cas",
        "test_cas_recovery",
        "test_cli",
        "test_hermeticity",
        "test_journal",
        "test_journal_migrate_v1",
        "test_merge",
        "test_namespace",
        "test_namespace_consumers",
        "test_namespace_excision",
        "test_namespace_onboarding",
        "test_namespace_snapshots",
    ]
)
SOURCE_FILES = frozenset(
    {f"castle/{name}.py" for name in MODULES}
    | {f"tests/{name}.py" for name in TESTS}
    | {
        "examples/namespace_demo.py",
        "README.md",
        "CHANGELOG.md",
        "LICENSE",
        "RELEASE.md",
        "NAMESPACE_ENVIRONMENT.md",
        "pyproject.toml",
        "uv.lock",
        "PKG-INFO",
        ".python-version",
        ".gitignore",
    }
)
WHEEL_FILES = frozenset(
    {f"castle/{name}.py" for name in MODULES}
    | {
        f"castle-{VERSION}.dist-info/{name}"
        for name in (
            "METADATA",
            "WHEEL",
            "RECORD",
            "entry_points.txt",
            "licenses/LICENSE",
        )
    }
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or any(p in {".", ".."} for p in path.parts)
        or "\\" in name
    ):
        raise AssertionError("Unsafe archive member")
    return path


def metadata(raw: bytes) -> None:
    message = email.parser.BytesParser().parsebytes(raw)
    if message.get("Name") != "castle" or message.get("Version") != VERSION:
        raise AssertionError("Archive name/version mismatch")
    if message.get("Requires-Python") != ">=3.12":
        raise AssertionError("Archive runtime metadata mismatch")


def inspect_wheel(file: Path) -> tuple[dict[str, bytes], str]:
    with zipfile.ZipFile(file) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != WHEEL_FILES:
            raise AssertionError(
                f"Wheel allowlist differs: {sorted(set(names) ^ WHEEL_FILES)}"
            )
        for item in archive.infolist():
            safe_name(item.filename)
            if stat.S_ISLNK(item.external_attr >> 16):
                raise AssertionError("Wheel link refused")
        contents = {name: archive.read(name) for name in names}
    metadata(contents[f"castle-{VERSION}.dist-info/METADATA"])
    license_bytes = contents[f"castle-{VERSION}.dist-info/licenses/LICENSE"]
    if b"MIT License" not in license_bytes or b"Wolf McNally" not in license_bytes:
        raise AssertionError("MIT authorship/license missing")
    wheel_metadata = email.parser.BytesParser().parsebytes(
        contents[f"castle-{VERSION}.dist-info/WHEEL"]
    )
    generator = wheel_metadata.get("Generator", "")
    if "1.32.0" not in generator:
        raise AssertionError("Unexpected wheel backend generator")
    return contents, generator


def unpack_source(file: Path, destination: Path) -> dict[str, bytes]:
    contents = {}
    with tarfile.open(file, "r:gz") as archive:
        seen = set()
        for item in archive:
            path = safe_name(item.name)
            if path.parts[0] != f"castle-{VERSION}" or item.name in seen:
                raise AssertionError("Source archive root/duplicate mismatch")
            seen.add(item.name)
            relative = PurePosixPath(*path.parts[1:])
            if item.isdir():
                if str(relative) not in {".", "castle", "tests", "examples"}:
                    raise AssertionError("Unexpected source directory")
                continue
            if not item.isfile() or str(relative) not in SOURCE_FILES:
                raise AssertionError(f"Source allowlist refused: {relative}")
            stream = archive.extractfile(item)
            if stream is None:
                raise AssertionError("Missing source member stream")
            contents[str(relative)] = stream.read()
    if set(contents) != SOURCE_FILES:
        raise AssertionError(
            f"Source allowlist differs: {sorted(set(contents) ^ SOURCE_FILES)}"
        )
    metadata(contents["PKG-INFO"])
    destination.mkdir()
    for name, data in contents.items():
        target = destination / name
        target.parent.mkdir(exist_ok=True, parents=True)
        with target.open("xb") as stream:
            stream.write(data)
    return contents


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, required=True, help="absent external output directory"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    package = root / "project"
    output = args.output.absolute()
    if (
        not args.uv.is_absolute()
        or not args.uv.is_file()
        or output.resolve().is_relative_to(root)
    ):
        raise AssertionError(
            "Explicit installed uv and external absent output required"
        )
    output.mkdir(mode=0o700)
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)

    def run(*command: object, cwd: Path = output) -> None:
        print("RUN", *map(str, command), flush=True)
        subprocess.run(
            list(map(str, command)), cwd=cwd, env=env, check=True, timeout=300
        )

    version = subprocess.check_output([str(args.uv), "--version"], text=True).strip()
    print("UV", args.uv, version, flush=True)
    run(
        args.uv,
        "build",
        "--project",
        package,
        "--python",
        sys.executable,
        "--out-dir",
        output / "dist",
    )
    wheel = output / f"dist/castle-{VERSION}-py3-none-any.whl"
    source = output / f"dist/castle-{VERSION}.tar.gz"
    ignore = output / "dist/.gitignore"
    # uv 0.9.26 creates this exact non-artifact sidecar in its output directory.
    if ignore.read_bytes() != b"*" or set((output / "dist").iterdir()) != {
        wheel,
        source,
        ignore,
    }:
        raise AssertionError("Unexpected build artifacts")
    wheel_contents, generator = inspect_wheel(wheel)
    unpacked = output / "source"
    source_contents = unpack_source(source, unpacked)
    for name in (f"castle/{module}.py" for module in MODULES):
        if wheel_contents[name] != source_contents[name]:
            raise AssertionError("Original wheel/source package bytes disagree")
    run(
        args.uv,
        "build",
        "--wheel",
        "--project",
        unpacked,
        "--python",
        sys.executable,
        "--out-dir",
        output / "rebuilt",
    )
    rebuilt = output / f"rebuilt/castle-{VERSION}-py3-none-any.whl"
    rebuilt_contents, rebuilt_generator = inspect_wheel(rebuilt)
    for name in (f"castle/{module}.py" for module in MODULES):
        if rebuilt_contents[name] != source_contents[name]:
            raise AssertionError("Rebuilt source archive changed package bytes")
    smoke_cwd = output / "external-cwd"
    smoke_cwd.mkdir()
    for label, artifact in (("wheel", wheel), ("sdist-wheel", rebuilt)):
        environment = output / (label + "-venv")
        run(args.uv, "venv", "--python", sys.executable, environment)
        python = environment / "bin/python"
        run(args.uv, "pip", "install", "--python", python, "--no-deps", artifact)
        run(python, root / ".github/scripts/package-smoke.py", cwd=smoke_cwd)
    receipt = {
        "schema": "castle.package_qualification.v1",
        "platform": sys.platform,
        "machine": platform.machine(),
        "python": platform.python_version(),
        "uv": version,
        "generator": generator,
        "rebuilt_generator": rebuilt_generator,
        "artifacts": {
            file.name if file != rebuilt else "sdist-rebuilt-wheel": digest(
                file.read_bytes()
            )
            for file in (wheel, source, rebuilt)
        },
        "package_files": {
            name: digest(data)
            for name, data in wheel_contents.items()
            if name.startswith("castle/")
        },
        "installed_smokes": ["wheel", "sdist-rebuilt-wheel"],
        "power_loss": "unqualified",
        "protected_adapter": "unqualified",
        "publication": "not-performed",
    }
    (output / "receipt.json").write_text(
        json.dumps(receipt, sort_keys=True, indent=2) + "\n"
    )
    print(
        "Exact wheel and source-to-wheel installed qualification passed; local artifacts only."
    )


if __name__ == "__main__":
    main()
