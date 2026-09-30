from __future__ import annotations

import copy
import runpy
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _normalized(path: Path) -> str:
    return " ".join(path.read_text().split())


POLICY = _normalized(REPO_ROOT / "policies" / "build-gates.md")
MECHANISTIC_POLICY = _normalized(REPO_ROOT / "policies" / "mechanistic-vs-intelligence.md")
ORCHESTRATION_POLICY = _normalized(REPO_ROOT / "policies" / "orchestration-evidence.md")
INCREMENTAL_BRIEF = _normalized(REPO_ROOT / "briefs" / "incremental-orchestration.md")
METHODOLOGY_BRIEF = _normalized(REPO_ROOT / "briefs" / "methodology.md")
BOOTSTRAP_BRIEF = _normalized(REPO_ROOT / "briefs" / "agentic-bootstrap.md")
CLAUDE = _normalized(REPO_ROOT / "CLAUDE.md")
KICKOFF = " ".join(
    _normalized(path) for path in sorted((REPO_ROOT / ".claude/skills/kickoff").glob("*.md"))
)
METHODOLOGY = _normalized(REPO_ROOT / ".claude" / "skills" / "methodology" / "SKILL.md")
PLANNER = _normalized(REPO_ROOT / ".claude" / "agents" / "phase-planner.md")
PLAN_REVIEWER = _normalized(REPO_ROOT / ".claude" / "agents" / "plan-reviewer.md")
CODER = _normalized(REPO_ROOT / ".claude" / "agents" / "phase-coder.md")
CODE_CRITIC = _normalized(REPO_ROOT / ".claude" / "agents" / "code-critic.md")
LEARN = _normalized(REPO_ROOT / ".claude" / "skills" / "learn" / "SKILL.md")
TEACH = _normalized(REPO_ROOT / ".claude" / "skills" / "teach" / "SKILL.md")
DEMO = _normalized(REPO_ROOT / ".claude" / "skills" / "demo" / "SKILL.md")
TREATISE = _normalized(REPO_ROOT / ".claude" / "skills" / "treatise" / "SKILL.md")
RESEARCH_POLICY = _normalized(REPO_ROOT / "policies" / "research-authority.md")
VERIFICATION_POLICY = _normalized(REPO_ROOT / "policies" / "verification-discipline.md")
TREATISE_POLICY = _normalized(REPO_ROOT / "policies" / "treatise.md")
USER_DEMO_POLICY = _normalized(REPO_ROOT / "policies" / "user-demo-protocols.md")


def test_atomic_contract_pins_behavioral_coverage_floor(tmp_path: Path) -> None:
    assert "Behavioral execution is the minimum test floor" in POLICY
    assert "source-text assertions may supplement it, but do not replace" in POLICY
    for skill in (LEARN, TEACH):
        assert "source-text" in skill
        assert "controlled" in skill

    # Exercise transferred runtime bytes with recipient-owned restrictions.
    for name in ("workflow.py", "advisory.py"):
        relative = f"lib/agentic_starter/{name}"
        for authority in (TEACH, LEARN, BOOTSTRAP_BRIEF):
            assert relative in authority
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, destination)
    copied = runpy.run_path(str(tmp_path / "lib/agentic_starter/workflow.py"))
    for harness, model in (("codex", "astra"), ("claude", "opus")):
        document = {
            "workflow": copy.deepcopy(copied["DEFAULT_WORKFLOW"]),
            "role_models": {"default": {}},
        }
        document["workflow"]["allowed_harnesses"] = [harness]
        resolved = copied["resolve"](document, harness)
        assert resolved["mode"] == "primary"
        assert resolved["roles"]["planner"]["execution"] == "inline"
        assert resolved["roles"]["coder"]["execution"] == "inline"
        for role in ("reviewer", "critic"):
            assert resolved["roles"][role]["model"] == model
            assert resolved["roles"][role]["execution"] == "independent"


def test_full_gate_receipt_contract_propagates_atomically() -> None:
    for document in (POLICY, LEARN, TEACH, BOOTSTRAP_BRIEF):
        assert "full-gate receipt" in document
        assert "environment fingerprint" in document
        assert "complete log" in document
        assert "fail" in document and "closed" in document
        assert "repository-selected runtime" in document
        assert "base-executable" in document
        assert "version-file proxy" in document or "version declaration" in document
    for document in (TEACH, BOOTSTRAP_BRIEF):
        assert "bin/check-receipt" in document
        assert "tests/test_check_receipt.py" in document


def test_orchestration_contract_is_candidate_bound_and_incremental() -> None:
    for document in (ORCHESTRATION_POLICY, KICKOFF):
        assert "kickoff-tree-id" in document
        assert "authority" in document
        assert "drift" in document
        assert "revision packet" in document
        assert "focused" in document
        assert "./bin/check all" in document
    acceptance = _normalized(REPO_ROOT / ".claude/skills/kickoff/acceptance.md")
    assert "approved product candidate id" in acceptance
    assert "proves the unchanged implementation candidate" in acceptance
    assert "requiring each id to equal its own pre-gate value" in acceptance


def test_self_improvement_bundle_propagates_atomically() -> None:
    required = (
        ".claude/skills/sweep/SKILL.md",
        "briefs/harness-self-improvement.md",
        "policies/lessons.md",
        "bin/lessons",
        "bin/check-catalogs",
        "tests/test_lessons.py",
        "tests/test_check_catalogs.py",
        "lessons-archived",
    )
    for phrase in required:
        assert phrase in TEACH
        assert phrase in BOOTSTRAP_BRIEF


def test_universal_demo_and_treatise_bundle_propagates_atomically() -> None:
    for skill in ("demo", "treatise"):
        canonical = f".claude/skills/{skill}/SKILL.md"
        mirror = f".agents/skills/{skill}"
        assert canonical in BOOTSTRAP_BRIEF
        assert mirror in BOOTSTRAP_BRIEF
        assert skill in TEACH
        mirror_path = REPO_ROOT / ".agents" / "skills" / skill
        assert mirror_path.is_symlink()
        assert mirror_path.resolve() == REPO_ROOT / ".claude" / "skills" / skill
    assert "policies/user-demo-protocols.md" in DEMO
    assert "policies/treatise.md" in TREATISE
    assert "canonical" in TREATISE_POLICY and "publication" in TREATISE_POLICY
    assert "universal `demo` skill" in USER_DEMO_POLICY


def test_research_authority_contract_propagates_and_stays_allow_by_default() -> None:
    assert "allow-by-default" in RESEARCH_POLICY
    assert "same-host structural neighbors" in RESEARCH_POLICY
    assert "GET" in RESEARCH_POLICY
    for document in (TEACH, BOOTSTRAP_BRIEF, KICKOFF):
        assert "research" in document
    for role in (PLANNER, PLAN_REVIEWER):
        assert "originate" in role and "retriev" in role
    for role in (CODER, CODE_CRITIC):
        assert "Do not originate" in role


def test_material_review_counts_are_reproducible() -> None:
    assert "Material counts are reproducible" in VERIFICATION_POLICY
    for document in (PLAN_REVIEWER, CODE_CRITIC, KICKOFF):
        assert "material count" in document
        assert "exact command or deterministic procedure" in document


def test_human_wall_clock_efficiency_is_ambient_and_effectiveness_preserving() -> None:
    for document in (
        CLAUDE,
        POLICY,
        MECHANISTIC_POLICY,
        INCREMENTAL_BRIEF,
        KICKOFF,
    ):
        assert "wall-clock" in document or "time savings" in document
        assert "substantial" in document

    for document in (POLICY, MECHANISTIC_POLICY, INCREMENTAL_BRIEF, KICKOFF, CODER):
        assert "fixed" in document or "numeric" in document
        assert "marginal" in document

    for document in (CLAUDE, KICKOFF, PLANNER, CODER, CODE_CRITIC):
        assert "handoff" in document


TEST_GOVERNANCE_POLICY = _normalized(REPO_ROOT / "policies" / "test-suite-governance.md")
TEST_GOVERNANCE_BRIEF = _normalized(REPO_ROOT / "briefs" / "test-suite-value-governance.md")


def test_proof_estate_governance_propagates_without_local_judgments() -> None:
    required = (
        "briefs/test-suite-value-governance.md",
        "policies/test-suite-governance.md",
        "bin/test-governance",
        "lib/agentic_starter/test_governance.py",
        "tests/test_test_governance.py",
        "tests/test_pre_commit.py",
        "reports/test-governance/README.md",
    )
    for phrase in required:
        for document in (LEARN, TEACH, BOOTSTRAP_BRIEF):
            assert phrase in document, f"{phrase} missing from a transfer authority"
    for document in (
        TEST_GOVERNANCE_POLICY,
        TEST_GOVERNANCE_BRIEF,
        LEARN,
        TEACH,
        BOOTSTRAP_BRIEF,
    ):
        assert "20%" in document
        assert "80%" in document
        assert "local" in document or "recipient" in document
    assert "Never copy donor family choices" in LEARN
    assert "Never seed the target" in TEACH
