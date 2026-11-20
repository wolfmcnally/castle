"""Legacy recipient shapes convert without rewriting their recorded history."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "bin/multioperator"
sys.path.insert(0, str(ROOT / "lib"))

from agentic_starter import multioperator  # noqa: E402

SLUGS = {
    "release": "public-release-work",
    "1": "first-legacy-work",
    "2": "parked-design-work",
    "3": "accepted-follow-work",
}


def run(*args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(CLI), *map(str, args)],
        text=True,
        capture_output=True,
        env={**os.environ, "UV_OFFLINE": "1"},
        timeout=60,
    )


def phase(identifier: str, deps: list[str], informs: list[str], body: str) -> str:
    return (
        "---\nid: "
        + json.dumps(identifier)
        + "\ntitle: Work\ndepends_on: "
        + json.dumps(deps)
        + "\ninforms: "
        + json.dumps(informs)
        + "\n---\n\n"
        + body
    )


@pytest.fixture
def legacy(tmp_path: Path) -> Path:
    repo = tmp_path / "legacy"
    for directory in ("plan", "project", "bin", "tests", ".githooks"):
        (repo / directory).mkdir(parents=True)
    (repo / "plan/INDEX.md").write_text(
        "# Plan\n\n## Phase table\n\n| Phase | Title | Status |\n|---|---|---|\n"
        "| [Release](phase-release.md) | Release preparation | ⬅️ |\n"
        "| [Phase 1](phase-1.md) | First | ✅ |\n"
        "| [Phase 2](phase-2.md) | Parked design | 🚧 |\n"
        "| [Phase 3](phase-3.md) | Accepted follow-up | ✅ |\n\n"
        "## Initiative\nHandwritten context stays.\n"
    )
    files = {
        "release": phase("release", [], [], "Release body.\n"),
        "1": phase(
            "1",
            [],
            ["release"],
            "[Roadmap](roadmap.md), [Index](INDEX.md), [Brief](../briefs/x.md), "
            "[Next](phase-2.md#goal).\n",
        ),
        "2": phase("2", ["phase-1.md"], [], "Design parked without acceptance.\n"),
        "3": phase("3", ["phase-2.md"], [], "Accepted under the design's rulings.\n"),
    }
    for identifier, text in files.items():
        (repo / "plan" / ("phase-" + identifier + ".md")).write_text(text)
    (repo / "plan/roadmap.md").write_text("[First](phase-1.md) and [Third](phase-3.md).\n")
    (repo / "project/README.md").write_text(
        "See the [changelog](CHANGELOG.md) and [plan](../plan/phase-1.md).\n"
    )
    (repo / "project/CHANGELOG.md").write_text("# Changes\n")
    (repo / "LOG.md").write_bytes(b"# Activity\n\n## 2026-01-01 01:00 \xe2\x80\x94 START\nWork\n")
    (repo / "CLAUDE.md").write_text("# Constitution\nPreserve this rule.\n")
    (repo / "AGENTS.md").symlink_to("CLAUDE.md")
    (repo / "README.md").write_text("[History](LOG.md) [Changes](CHANGELOG.md)\n")
    (repo / "CHANGELOG.md").write_text("# Changes\n")
    for name in ("check-log", "log-append"):
        (repo / "bin" / name).write_text("#!/bin/sh\n")
    (repo / ".githooks/pre-commit").write_text("#!/bin/sh\n./bin/check-log --staged\n")
    (repo / "tests/proof-estate.yaml").write_text(
        "families:\n"
        "- id: log-tools\n"
        "  source_paths: &log-tools-paths\n"
        "    - bin/check-log\n"
        "    - bin/log-append\n"
        "    - lib/agentic_starter/log_blocks.py\n"
        "  covers: *log-tools-paths\n"
        "- id: hook-log-staged\n"
        '  selectors: ["hook:.githooks/pre-commit:./bin/check-log --staged"]\n'
    )
    for args in [
        ("init", "-b", "master"),
        ("add", "-A"),
        ("-c", "user.name=F", "-c", "user.email=f@example.test", "commit", "-m", "Seed"),
    ]:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


def test_legacy_shapes_convert_with_history_and_product_intact(
    tmp_path: Path, legacy: Path
) -> None:
    mapping = tmp_path / "mapping.json"
    mapping.write_text(
        json.dumps(
            {
                "writer": "operator-a",
                "phases": SLUGS,
                "owners": {"2": "operator-a"},
                "active_checkpoints": {"2": {"terminal": True, "evidence": "retained park"}},
                "retirements": {
                    "bin/" + name: hashlib.sha256((legacy / "bin" / name).read_bytes()).hexdigest()
                    for name in ("check-log", "log-append")
                },
            }
        )
    )
    product = {p.name: p.read_bytes() for p in (legacy / "project").iterdir()}
    output = tmp_path / "converted"
    command = run("rehearse", "--source", legacy, "--output", output, "--mapping", mapping)
    assert command.returncode == 0, "LEGACY SHAPE REFUSED: " + command.stderr

    state = multioperator.validate(output)
    heads = {key: (head["state"], head.get("owner")) for key, head in state["heads"].items()}
    assert heads == {
        "public-release-work": ("next", None),
        "first-legacy-work": ("completed", None),
        "parked-design-work": ("in-progress", "operator-a"),
        "accepted-follow-work": ("completed", None),
    }
    follow, _ = multioperator.frontmatter(
        (output / "plan/phases/accepted-follow-work.md").read_bytes()
    )
    assert follow["depends_on"] == ["parked-design-work"]
    first, body = multioperator.frontmatter(
        (output / "plan/phases/first-legacy-work.md").read_bytes()
    )
    assert first["informs"] == ["public-release-work"]
    assert body == (
        "\n[Roadmap](../roadmap.md), [Index](../INDEX.md), [Brief](../../briefs/x.md), "
        "[Next](parked-design-work.md#goal).\n"
    )
    assert (output / "plan/roadmap.md").read_text() == (
        "[First](phases/first-legacy-work.md) and [Third](phases/accepted-follow-work.md).\n"
    )
    assert {p.name: p.read_bytes() for p in (output / "project").iterdir()} == product
    assert (
        output / "README.md"
    ).read_text() == "[History](log/README.md) [Changes](CHANGELOG.md)\n"
    assert (output / ".collaboration-views/LOG.md").read_bytes() == (legacy / "LOG.md").read_bytes()
    estate = (output / "tests/proof-estate.yaml").read_text()
    assert estate.count("    - bin/multioperator\n") == 1
    assert '"hook:.githooks/pre-commit:./bin/multioperator validate --root . --staged"' in estate
    assert yaml.safe_load(estate)["families"][0]["covers"] == [
        "bin/multioperator",
        "lib/agentic_starter/log_blocks.py",
    ]


def genesis(phase_id: str, state: str, imported: bool) -> dict[str, object]:
    event: dict[str, object] = {
        "id": hashlib.md5(phase_id.encode()).hexdigest(),
        "phase": phase_id,
        "previous": None,
        "from": None,
        "state": state,
        "owner": "operator-a" if state == "in-progress" else None,
        "writer": "operator-a",
        "reason": multioperator.IMPORT_REASON if imported else "New phase",
    }
    if imported:
        event["source_phase"] = phase_id
    return event


PHASES = {
    "parked-design-work": {"order": "1", "depends_on": [], "informs": []},
    "accepted-follow-work": {"order": "2", "depends_on": ["parked-design-work"], "informs": []},
}


def test_only_imported_heads_may_precede_their_dependencies() -> None:
    parked = genesis("parked-design-work", "in-progress", imported=True)
    follow = genesis("accepted-follow-work", "completed", imported=True)
    assert multioperator.fold(PHASES, [parked, follow])["accepted-follow-work"]["state"] == (
        "completed"
    )
    not_imported = genesis("accepted-follow-work", "in-progress", imported=False)
    not_imported["claim"] = "claim-1"
    with pytest.raises(multioperator.Refusal, match="work precedes completed dependencies"):
        multioperator.fold(PHASES, [parked, not_imported])
    later = {
        "id": hashlib.md5(b"later").hexdigest(),
        "phase": "accepted-follow-work",
        "previous": not_imported["id"],
        "from": "in-progress",
        "state": "in-progress",
        "owner": "operator-a",
        "claim": "claim-2",
        "writer": "operator-a",
        "reason": "Continue",
    }
    with pytest.raises(multioperator.Refusal, match="work precedes completed dependencies"):
        multioperator.fold(PHASES, [parked, not_imported, later])
    forged = dict(follow, reason="Completed by hand")
    with pytest.raises(multioperator.Refusal, match="work precedes completed dependencies"):
        multioperator.fold(PHASES, [parked, forged])
