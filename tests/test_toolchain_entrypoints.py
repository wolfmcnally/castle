from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = ("setup", "test", "python")
SYMLINK_INVOCATIONS = (
    ("setup", ()),
    ("test", ("tests/test_check.py", "-q")),
    ("python", ("--version",)),
)
PROBE = (
    "import castle.cli, pytest, subprocess; "
    "subprocess.run(['ruff','--version'], check=True, stdout=subprocess.DEVNULL)"
)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _write_python_stub(path: Path) -> None:
    _write_executable(
        path,
        """#!/usr/bin/env bash
printf '%s\n' "$0"
""",
    )


@pytest.fixture
def toolchain_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "repo"
    (root / "bin").mkdir(parents=True)
    (root / "project").mkdir()
    shutil.copy2(REPO_ROOT / "bin" / "_python-toolchain", root / "bin" / "_python-toolchain")
    for entrypoint in ENTRYPOINTS:
        shutil.copy2(REPO_ROOT / "bin" / entrypoint, root / "bin" / entrypoint)
    _write_executable(
        root / "bin" / "test-governance",
        """#!/usr/bin/env bash
printf '%s\n' 'FOCUSED' 'fixture selection' 'tests/test_check.py'
""",
    )
    (root / "project" / ".python-version").write_text("3.11\n")
    (root / "project" / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (root / "project" / "uv.lock").write_text("version = 1\n")

    family_store = tmp_path / "family-uv"
    (family_store / "cache").mkdir(parents=True)
    (family_store / "python").mkdir(parents=True)
    (root / "workspace-uv.conf").write_text(
        "schema=agentic.workspace_uv.v1\n"
        "project=fixture\n"
        "project_root=project\n"
        "store=../family-uv\n"
        "cache=cache\n"
        "python_install=python\n"
    )

    log_path = tmp_path / "calls.log"
    tool_dir = tmp_path / "tools"
    tool_dir.mkdir()
    _write_executable(
        tool_dir / "uv",
        """#!/usr/bin/env bash
set -u
printf 'uv cwd=%s args=%s\\n' "$PWD" "$*" >> "$TOOLCHAIN_TEST_LOG"
if [[ -n "${TOOLCHAIN_TEST_FAIL_MATCH:-}" && "$*" == *"$TOOLCHAIN_TEST_FAIL_MATCH"* ]]; then
  exit "${TOOLCHAIN_TEST_FAIL_CODE:-23}"
fi
if [[ "$*" == python\\ find\\ --no-project\\ * ]]; then
  printf '%s\\n' "${@: -1}"
elif [[ "$*" == "cache dir" ]]; then
  printf '%s\\n' "$TOOLCHAIN_TEST_CACHE_ROOT"
elif [[ "$*" == "python dir" ]]; then
  printf '%s\\n' "$TOOLCHAIN_TEST_MANAGED_ROOT"
fi
""",
    )
    environment = os.environ.copy()
    environment["PATH"] = f"{tool_dir}:{environment['PATH']}"
    for inherited in (
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "AGENTIC_UV_ISOLATION_ROOT",
        "VIRTUAL_ENV",
    ):
        environment.pop(inherited, None)
    environment["TOOLCHAIN_TEST_LOG"] = str(log_path)
    environment["TOOLCHAIN_TEST_MANAGED_ROOT"] = str(family_store / "python")
    environment["TOOLCHAIN_TEST_CACHE_ROOT"] = str(family_store / "cache")
    return root, environment


def _run_path(
    executable: Path,
    environment: dict[str, str],
    *arguments: str,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(executable), *arguments],
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _run(
    root: Path,
    environment: dict[str, str],
    entrypoint: str,
    *arguments: str,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run_path(root / "bin" / entrypoint, environment, *arguments, cwd=cwd or root)


def _calls(environment: dict[str, str]) -> list[str]:
    log_path = Path(environment["TOOLCHAIN_TEST_LOG"])
    return log_path.read_text().splitlines() if log_path.exists() else []


def _family_probes(root: Path) -> list[str]:
    return [f"uv cwd={root} args=cache dir", f"uv cwd={root} args=python dir"]


def test_test_defaults_to_every_repository_test_from_any_cwd(
    toolchain_repo: tuple[Path, dict[str, str]], tmp_path: Path
) -> None:
    root, environment = toolchain_repo

    result = _run(root, environment, "test", cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert _calls(environment) == _family_probes(root) + [
        (
            f"uv cwd={root} args=run --project {root / 'project'} --locked "
            f"--managed-python python -c {PROBE}"
        ),
        (
            f"uv cwd={root} args=run --project {root / 'project'} --locked "
            "--managed-python python -m pytest -q project/tests tests"
        ),
    ]


def _assert_selected_repository(
    root: Path,
    environment: dict[str, str],
    result: subprocess.CompletedProcess[str],
) -> None:
    assert result.returncode == 0, result.stderr
    calls = _calls(environment)
    assert calls
    assert calls[:2] == _family_probes(root)
    project_calls = [call for call in calls if " args=run " in call or " args=sync " in call]
    assert project_calls
    assert all(f"--project {root / 'project'} --locked" in call for call in project_calls), calls


@pytest.mark.parametrize(("entrypoint", "arguments"), SYMLINK_INVOCATIONS)
def test_installed_symlink_chain_selects_the_owning_repository(
    toolchain_repo: tuple[Path, dict[str, str]],
    tmp_path: Path,
    entrypoint: str,
    arguments: tuple[str, ...],
) -> None:
    root, environment = toolchain_repo
    first_dir = tmp_path / "first-bin"
    second_dir = tmp_path / "second-bin"
    first_dir.mkdir()
    second_dir.mkdir()
    (first_dir / entrypoint).symlink_to(root / "bin" / entrypoint)
    launcher = second_dir / entrypoint
    launcher.symlink_to(Path("..") / "first-bin" / entrypoint)

    result = _run_path(launcher, environment, *arguments, cwd=tmp_path)

    _assert_selected_repository(root, environment, result)


@pytest.mark.parametrize(
    "arguments",
    [("--vital",), ("--changed-from", "HEAD~1")],
)
def test_test_governed_lanes_run_selected_proofs(
    toolchain_repo: tuple[Path, dict[str, str]], arguments: tuple[str, ...]
) -> None:
    root, environment = toolchain_repo

    result = _run(root, environment, "test", *arguments)

    assert result.returncode == 0, result.stderr
    assert "TEST GOVERNED FOCUSED: fixture selection" in result.stdout
    assert _calls(environment) == _family_probes(root) + [
        (
            f"uv cwd={root} args=run --project {root / 'project'} --locked "
            f"--managed-python python -c {PROBE}"
        ),
        (
            f"uv cwd={root} args=run --project {root / 'project'} --locked "
            "--managed-python python -m pytest -q tests/test_check.py"
        ),
    ]


@pytest.mark.parametrize(
    ("entrypoint", "arguments"),
    [
        ("setup", ()),
        ("test", ("tests/test_check.py", "-q")),
        ("python", ("--version",)),
    ],
)
def test_authoritative_runtime_override_is_used_for_probe_and_command(
    toolchain_repo: tuple[Path, dict[str, str]],
    tmp_path: Path,
    entrypoint: str,
    arguments: tuple[str, ...],
) -> None:
    root, environment = toolchain_repo
    runtime = tmp_path / "candidate-python"
    _write_python_stub(runtime)
    environment["TOOLCHAIN_PYTHON"] = str(runtime)

    result = _run(root, environment, entrypoint, *arguments)

    assert result.returncode == 0, result.stderr
    calls = _calls(environment)
    assert len(calls) == 6
    assert calls[:2] == _family_probes(root)
    assert calls[2] == f"uv cwd={root} args=python find --no-project {runtime}"
    assert calls[3] == f"uv cwd={root} args=python dir"
    assert all(f"--python {runtime} --no-managed-python" in call for call in calls[4:])

    if entrypoint == "setup":
        declaration = root / "workspace-uv.conf"
        public_config = (
            "schema=agentic.workspace_uv.v1\nproject=fixture\nproject_root=project\n"
            "store=.runtime\ncache=cache\npython_install=python\n"
        )
        owned = root / ".runtime"
        environment["TOOLCHAIN_TEST_CACHE_ROOT"] = str(owned / "cache")
        environment["TOOLCHAIN_TEST_MANAGED_ROOT"] = str(owned / "python")
        declaration.write_text(
            public_config.replace("schema=agentic.workspace_uv.v1", "schema=invalid")
        )
        refused = _run(root, environment, "setup")
        assert refused.returncode != 0
        assert not owned.exists()
        outside = tmp_path / "outside-runtime"
        outside.mkdir()
        owned.symlink_to(outside, target_is_directory=True)
        declaration.write_text(public_config)
        refused = _run(root, environment, "setup")
        assert refused.returncode != 0
        assert "unsafe checkout-owned" in refused.stderr
        assert list(outside.iterdir()) == []
        owned.unlink()
        environment["AGENTIC_UV_ISOLATION_ROOT"] = str(tmp_path / "absent-isolation")
        refused = _run(root, environment, "setup")
        assert refused.returncode != 0
        assert not owned.exists()
        environment.pop("AGENTIC_UV_ISOLATION_ROOT")
        admitted = _run(root, environment, "setup")
        assert admitted.returncode == 0, admitted.stderr
        assert (owned / "cache").is_dir()
        assert (owned / "python").is_dir()
