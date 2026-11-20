from __future__ import annotations

import copy
import re
import runpy
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _normalized(path: Path) -> str:
    return " ".join(path.read_text().split())


POLICY = _normalized(REPO_ROOT / "policies" / "build-gates.md")


COMMIT_STAGING_POLICY = _normalized(REPO_ROOT / "policies" / "commit-staging.md")


MECHANISTIC_POLICY = _normalized(REPO_ROOT / "policies" / "mechanistic-vs-intelligence.md")


ORCHESTRATION_POLICY = _normalized(REPO_ROOT / "policies" / "orchestration-evidence.md")


INCREMENTAL_BRIEF = _normalized(REPO_ROOT / "briefs" / "incremental-orchestration.md")


METHODOLOGY_BRIEF = _normalized(REPO_ROOT / "briefs" / "methodology.md")


RULE_ONE_BRIEF = _normalized(REPO_ROOT / "briefs" / "rule-one-diagnostic-learning.md")


BOOTSTRAP_BRIEF = _normalized(REPO_ROOT / "briefs" / "agentic-bootstrap.md")


CLAUDE = _normalized(REPO_ROOT / "AGENTS.md")


KICKOFF = " ".join(
    _normalized(path) for path in sorted((REPO_ROOT / ".claude/skills/kickoff").glob("*.md"))
)


METHODOLOGY = _normalized(REPO_ROOT / ".claude" / "skills" / "methodology" / "SKILL.md")


RULE_ONE = _normalized(REPO_ROOT / ".claude" / "skills" / "rule-one" / "SKILL.md")


PLANNER = _normalized(REPO_ROOT / ".claude" / "agents" / "phase-planner.md")


PLAN_REVIEWER = _normalized(REPO_ROOT / ".claude" / "agents" / "plan-reviewer.md")


CODER = _normalized(REPO_ROOT / ".claude" / "agents" / "phase-coder.md")


CODE_CRITIC = _normalized(REPO_ROOT / ".claude" / "agents" / "code-critic.md")


LEARN = _normalized(REPO_ROOT / ".claude" / "skills" / "learn" / "SKILL.md")


TEACH = _normalized(REPO_ROOT / ".claude" / "skills" / "teach" / "SKILL.md")


DEMO = _normalized(REPO_ROOT / ".claude" / "skills" / "demo" / "SKILL.md")


TREATISE = _normalized(REPO_ROOT / ".claude" / "skills" / "treatise" / "SKILL.md")


ASK = _normalized(REPO_ROOT / ".claude" / "skills" / "ask" / "SKILL.md")


RESEARCH_POLICY = _normalized(REPO_ROOT / "policies" / "research-authority.md")


VERIFICATION_POLICY = _normalized(REPO_ROOT / "policies" / "verification-discipline.md")


TREATISE_POLICY = _normalized(REPO_ROOT / "policies" / "treatise.md")


USER_DEMO_POLICY = _normalized(REPO_ROOT / "policies" / "user-demo-protocols.md")


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
    for document in (TEST_GOVERNANCE_BRIEF, LEARN, TEACH, BOOTSTRAP_BRIEF):
        assert "local" in document or "recipient" in document
        assert "full" in document
    # Castle's ratified ceilings and recall floors are Castle-local obligations: they live in
    # its own policy section and its recipient enforcement module, not in transfer documents.
    castle = TEST_GOVERNANCE_POLICY.split("## Castle's retained reset obligations", 1)[1]
    for phrase in ("20% of the frozen denominators", "80% recall", "80% kill recall"):
        assert phrase in castle
    enforcement = (REPO_ROOT / "lib/agentic_starter/recipient_governance.py").read_text()
    for phrase in ("reset_limits", "budget_approval", "min_historical_recall", "min_mutant_recall"):
        assert phrase in enforcement
    assert "Never copy donor family choices" in LEARN
    assert "Never seed the target" in TEACH


def _assert_owned_contract(root: Path, resource: str, clauses: tuple[str, ...]) -> None:
    entry = root / ".claude/skills/kickoff/SKILL.md"
    rows = [line.split("|") for line in entry.read_text().splitlines() if line.startswith("|")]
    assert any(
        len(row) == 5
        and row[2].strip() == f"[{resource}]({resource})"
        and row[3].strip().startswith("Read before ")
        for row in rows
    ), f"{resource}: no operative entry row"
    owner = " ".join((entry.parent / resource).read_text().split())
    for clause in clauses:
        assert clause in owner, f"{resource}: missing owned obligation: {clause}"


def _exercise_owned_contract(tmp_path: Path, resource: str, clauses: tuple[str, ...]) -> None:
    """Exact obligation witnesses; contextual meaning still belongs to review."""
    skill = tmp_path / ".claude/skills/kickoff"
    shutil.copytree(REPO_ROOT / ".claude/skills/kickoff", skill)
    _assert_owned_contract(tmp_path, resource, clauses)
    owner = skill / resource
    original = owner.read_text()
    entry = skill / "SKILL.md"
    entry.write_text(entry.read_text() + "\nUnrelated quotation: " + " ".join(clauses) + "\n")
    for clause in clauses:
        owner.write_text(" ".join(original.split()).replace(clause, "Obligation withdrawn."))
        with pytest.raises(AssertionError, match=f"{resource}: missing owned obligation"):
            _assert_owned_contract(tmp_path, resource, clauses)
    owner.write_text(original)
    _assert_owned_contract(tmp_path, resource, clauses)


def test_research_authority_contract_propagates_and_stays_allow_by_default(
    tmp_path: Path,
) -> None:
    assert "allow-by-default" in RESEARCH_POLICY
    assert "same-host structural neighbors" in RESEARCH_POLICY
    assert "GET" in RESEARCH_POLICY
    for document in (TEACH, BOOTSTRAP_BRIEF, KICKOFF):
        assert "research" in document
    for role in (PLANNER, PLAN_REVIEWER):
        assert "originate" in role and "retriev" in role
    for role in (CODER, CODE_CRITIC):
        assert "Do not originate" in role
    _exercise_owned_contract(
        tmp_path / "preflight",
        "preflight.md",
        (
            "Run `./bin/kickoff-config show research`",
            "coder and critic may retrieve plan/brief-identified resources and same-host "
            "structural neighbors but may not originate searches.",
            "Installed MCP servers and plugins remain available by default unless the project "
            "or phase explicitly narrows them.",
        ),
    )
    _exercise_owned_contract(
        tmp_path / "dispatch",
        "dispatch.md",
        (
            "the watcher supplies research/access directives, schemas, credential handling, "
            "routing and budgets; never reconstruct weaker substitutes from memory.",
            "Native dispatches use the generated `span-recipe` commands and the same resolved "
            "research directive and output contract.",
        ),
    )


def test_material_review_counts_are_reproducible() -> None:
    assert "Material counts are reproducible" in VERIFICATION_POLICY
    for document in (PLAN_REVIEWER, CODE_CRITIC, KICKOFF):
        assert "material count" in document
        assert "exact command or deterministic procedure" in document


def test_optional_cross_harness_coordination_stays_optional(tmp_path: Path) -> None:
    """Instruction contract: prefer supported coordination without making it a dependency.

    Risk: dispatch retains the old unconditional direct-launch preference, or optional
    absence turns into a new gate. The owning instructions are the interface being
    changed, so clause withdrawal at that owner is the cheapest independent oracle;
    this does not qualify a runtime adapter or a live host. Finite filesystem copies,
    no model, provider, service, credentials or new production seam.
    """
    policy = _normalized(REPO_ROOT / "policies/role-models.md")
    clauses = (
        "cross-harness launching and communication are optional capabilities, never prerequisites.",
        "Prefer an available supported cross-harness launcher and communication surface "
        "over direct native harness launches and ad hoc peer messaging.",
        "When no supported cross-harness launch path is available, use the existing "
        "direct native dispatch path.",
        "Do not retry a denied or uncertain launch through another transport.",
    )

    def assert_policy(text: str) -> None:
        for clause in clauses:
            assert clause in text, "OPTIONAL COORDINATION POLICY LOST: " + clause

    assert_policy(policy)
    for clause in clauses:
        with pytest.raises(AssertionError, match="OPTIONAL COORDINATION POLICY LOST"):
            assert_policy(policy.replace(clause, "Obligation withdrawn."))
    _exercise_owned_contract(
        tmp_path / "dispatch",
        "dispatch.md",
        (
            "Prefer an available supported cross-harness launcher and communication surface "
            "over direct native harness launches.",
            "When no supported cross-harness launch path is available, use the existing "
            "direct native dispatch path.",
            "A denied or uncertain launch is not retried through another transport; "
            "inspect actual state and follow governed recovery before any authorized retry.",
        ),
    )
    _exercise_owned_contract(
        tmp_path / "preflight",
        "preflight.md",
        (
            "A missing optional integration is not a preflight failure, and communication "
            "availability alone does not establish launch support.",
        ),
    )


def test_mini_method_delivers_within_existing_authority() -> None:
    """Guard the actual copyable instruction interface against the escaped no-push rule.

    There is no executable emitter: setup copies this fenced block verbatim. Inspecting
    that artifact is the cheapest independent oracle, not a claim about agent behavior.
    Four finite text checks cover delivery, setup, retained limits and stale overrides;
    no service, model, Git mutation or test-only production seam is needed.
    """
    brief = (REPO_ROOT / "briefs/mini-method.md").read_text()
    block = brief.split("````markdown\n", 1)[1].split("\n````", 1)[0]
    delivery = block.partition("- **Delivery.**")[2].split("\n- **", 1)[0]
    assert "Commit and push approved project work without a separate human approval" in delivery, (
        "MINI DELIVERY AUTHORITY LOST"
    )
    setup = block.split("## Setup", 1)[1].split("## Project", 1)[0]
    assert all(
        clause in setup
        for clause in (
            "Never create or change a remote.",
            "Apply the delivery rule below",
            "retain the local commit and report the missing destination",
        )
    ), "MINI SETUP DELIVERY BOUNDARY LOST"
    assert all(
        clause in block
        for clause in (
            "applicable objective checks pass",
            "stage only attributable explicit paths",
            "existing unambiguous authorized upstream and publication scope",
            "non-force fast-forward push",
            "actual remote tip agree and the tree is clean",
            "uncertain custody park delivery",
            "Preserve explicit task-specific local-only or no-publication holds",
            "Never bypass hooks or native permission denials, disclose secrets, rewrite history",
            "pending human judgment does not block qualified delivery",
        )
    ), "MINI DELIVERY SAFEGUARDS LOST"
    assert not re.search(
        r"never push|do not push|push only when|commit, never push|"
        r"anything outward-facing are theirs to approve",
        brief,
        re.IGNORECASE,
    ), "MINI STALE DELIVERY OVERRIDE"


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
    for harness, model in (("codex", "sol"), ("claude", "opus")):
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
