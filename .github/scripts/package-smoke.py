"""Check an installed Castle wheel using only disposable synthetic data."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import castle


def main() -> None:
    prefix = Path(sys.prefix).resolve()
    module = Path(castle.__file__).resolve()
    if not module.is_relative_to(prefix):
        raise AssertionError(f"Castle import escaped installed environment: {module}")
    if castle.__version__ != "0.1.0":
        raise AssertionError(f"Unexpected version: {castle.__version__}")
    cli = prefix / "bin" / "castle"
    if not cli.is_file():
        raise AssertionError("Installed console entry point is absent")
    with tempfile.TemporaryDirectory(prefix="castle-wheel-smoke-") as directory:
        scratch = Path(directory)
        source, store = scratch / "source", scratch / "store"
        source.mkdir()
        (source / "example.txt").write_text("synthetic wheel smoke input\n")

        def run(*arguments: str) -> str:
            result = subprocess.run(
                [str(cli), *arguments],
                cwd=scratch,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0:
                raise AssertionError(
                    f"{arguments[0]} failed ({result.returncode}): {result.stderr}"
                )
            print(f"{arguments[0]}: passed")
            return result.stdout

        run("--help")
        if run("--version").strip() != "castle 0.1.0":
            raise AssertionError("Console version disagrees with package metadata")
        run("init", "--root", str(store))
        ingest = json.loads(
            run(
                "ingest",
                "--root",
                str(store),
                "--source",
                str(source),
                "--source-id",
                "smoke",
            )
        )
        if (
            ingest["new_objects"] != 1
            or ingest["files_seen"] != 1
            or ingest["refusals"]
        ):
            raise AssertionError("Synthetic ingest did not publish exactly one object")
        if json.loads(run("stat", "--root", str(store)))["objects"] != 1:
            raise AssertionError("Installed store did not report the synthetic object")
        for action in ("verify", "rebuild-index", "verify"):
            output = json.loads(run(action, "--root", str(store)))
            if action == "verify" and (output["ok"] is not True or output["errors"]):
                raise AssertionError(
                    "Installed verification did not establish clean authority"
                )
        records = [
            json.loads(line)
            for line in (store / "journal.jsonl").read_text().splitlines()
        ]
        if len(records) != 1 or records[0]["schema"] != "journal.v2":
            raise AssertionError(
                "Installed writer did not emit the public journal.v2 format"
            )
    print("Installed wheel import, version, CLI and journal.v2 smoke passed")


if __name__ == "__main__":
    main()
