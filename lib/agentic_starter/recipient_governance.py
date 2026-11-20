"""Castle's own proof-estate assurances, kept beside the shared governance engine.

The shared engine validates estate shape, the ledger replay and witness receipts. Castle
also ratified checks the shared engine does not carry, and they bind every validation:

- count ceilings: at most `reset_limits` of the frozen baseline families and leaves, plus
  a positive `test_budget_delta` only under a named `budget_approval`;
- the completed reset holds both delete and consolidate dispositions, and its
  dispositions precede every lifecycle event;
- the frozen effectiveness corpus and its report agree exactly, with digest-bound
  patches, unchanged oracles and recall at or above the `reset_limits` floors.

`unwitnessed_history` names the historic admissions Castle recorded before witness receipts
existed and that stay without one, bound to the digest of the ledger prefix that holds them.

`assay` re-runs that corpus against disposable copies of the checkout.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

EFFECTIVENESS_FIELDS = {
    "record_type",
    "evidence_id",
    "evidence_class",
    "observed",
    "detected_by",
    "command",
    "patch_sha256",
    "oracle_sha256",
    "control_output_sha256",
    "output_sha256",
    "return_code",
}

CASE_FIELDS = {
    "id",
    "class",
    "patch",
    "patch_sha256",
    "command",
    "cwd",
    "oracle",
    "owner",
}


class RecipientGovernanceError(ValueError):
    """A Castle-specific assurance could not be evaluated."""


WITNESS_HISTORY_FIELDS = {"ledger_rows", "ledger_prefix_sha256", "unwitnessed", "reason"}


def unwitnessed_history(
    root: Path, manifest: dict[str, Any], ledger: list[dict[str, Any]]
) -> tuple[set[str], list[str]]:
    """Historic admissions that keep no witness receipt, and the errors in declaring them.

    Castle admitted proofs before witness receipts existed. Such an admission is not
    retroactively deficient for want of one, and some can never get one: a proof that
    asserts this estate is valid fails its own restored run while its own receipt is
    missing. `witness_history` names each such proof and binds the digest of the audit
    ledger prefix that holds its admission. A named proof must be admitted or repaired only
    inside that prefix; a later admission, repair or modification needs its own receipt.
    """
    declared = manifest.get("witness_history")
    if declared is None:
        return set(), []
    named = declared.get("unwitnessed") if isinstance(declared, dict) else None
    if (
        not isinstance(declared, dict)
        or set(declared) != WITNESS_HISTORY_FIELDS
        or type(declared["ledger_rows"]) is not int
        or declared["ledger_rows"] < 1
        or not isinstance(named, list)
        or not named
        or not all(isinstance(proof, str) and proof for proof in named)
        or len(set(named)) != len(named)
        or not isinstance(declared["reason"], str)
        or not declared["reason"].strip()
    ):
        return set(), [
            "witness_history must declare ledger_rows, ledger_prefix_sha256, "
            "the unwitnessed proofs and a reason"
        ]
    rows = declared["ledger_rows"]
    lines = (root / str(manifest["audit_ledger"])).read_bytes().splitlines(keepends=True)
    if (
        rows > len(lines)
        or rows > len(ledger)
        or hashlib.sha256(b"".join(lines[:rows])).hexdigest() != declared["ledger_prefix_sha256"]
    ):
        return set(), ["witness_history prefix does not match the audit ledger"]
    kinds = {"proof_admission", "proof_repair"}
    historic = {
        str(row.get("proof_id")) for row in ledger[:rows] if row.get("record_type") in kinds
    }
    later = {str(row.get("proof_id")) for row in ledger[rows:] if row.get("record_type") in kinds}
    errors = [
        f"witness_history names a proof not admitted only in its prefix: {proof}"
        for proof in named
        if proof not in historic or proof in later
    ]
    return (set() if errors else set(named)), errors


def _canonical_sha256(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def _validate_oracle(oracle: Any, case_id: str) -> list[str]:
    if not isinstance(oracle, dict) or set(oracle) != {"exit_code", "output_contains"}:
        return [
            f"effectiveness case {case_id} oracle must contain exactly "
            "exit_code and output_contains"
        ]
    errors: list[str] = []
    if not isinstance(oracle.get("exit_code"), int) or oracle["exit_code"] == 0:
        errors.append(f"effectiveness case {case_id} oracle exit_code must be nonzero")
    if not isinstance(oracle.get("output_contains"), str) or not oracle["output_contains"]:
        errors.append(f"effectiveness case {case_id} oracle output_contains must be text")
    return errors


def _oracle_matches(result: subprocess.CompletedProcess[str], oracle: dict[str, Any]) -> bool:
    output = result.stdout + "\n" + result.stderr
    return result.returncode == oracle["exit_code"] and oracle["output_contains"] in output


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        raise RecipientGovernanceError("recall denominator must be positive")
    return numerator / denominator


def _run(
    command: Sequence[str],
    root: Path,
    *,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def link_declared_runtime_store(root: Path, work: Path) -> None:
    declaration = root / "workspace-uv.conf"
    if not declaration.is_file():
        return
    stores = [
        line.removeprefix("store=").strip()
        for line in declaration.read_text().splitlines()
        if line.startswith("store=")
    ]
    if len(stores) != 1 or not stores[0]:
        raise RecipientGovernanceError("workspace-uv.conf must declare exactly one runtime store")
    relative = Path(stores[0])
    if relative.is_absolute():
        raise RecipientGovernanceError("workspace-uv.conf runtime store must be relative")
    source = (root / relative).resolve()
    if not source.is_dir():
        raise RecipientGovernanceError(f"declared runtime store is absent: {source}")
    destination = work / relative
    if destination.exists() or destination.is_symlink():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(source, target_is_directory=True)


def _load_yaml(path: Path) -> dict[str, Any]:
    # The shared engine's readers, so a caller sees one reading of every governance file.
    from agentic_starter import test_governance

    return test_governance.load_yaml(path)


def _load_ledger(path: Path) -> list[dict[str, Any]]:
    from agentic_starter import test_governance

    return test_governance.load_ledger(path)


def count_errors(
    manifest: dict[str, Any], baseline: dict[str, Any], current: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    limits = manifest.get("reset_limits")
    if not isinstance(limits, dict):
        errors.append("reset_limits must be a mapping")
        limits = {}
    budget = manifest.get("test_budget_delta", {"families": 0, "leaves": 0})
    approval = manifest.get("budget_approval")
    approval_named = isinstance(approval, str) and bool(approval.strip())
    if not isinstance(budget, dict):
        errors.append("test_budget_delta must be a mapping")
        budget = {"families": 0, "leaves": 0}
    for dimension in ("families", "leaves"):
        baseline_count = baseline.get("counts", {}).get(dimension)
        current_count = current["counts"][dimension]
        ratio_limit = limits.get(f"max_{dimension}_ratio")
        delta = budget.get(dimension, 0)
        if not isinstance(baseline_count, int) or baseline_count <= 0:
            errors.append(f"baseline {dimension} count must be positive")
            continue
        if not isinstance(ratio_limit, (int, float)) or not 0 < ratio_limit <= 1:
            errors.append(f"max_{dimension}_ratio must be in (0, 1]")
            continue
        if not isinstance(delta, int):
            errors.append(f"test_budget_delta.{dimension} must be an integer")
            continue
        if delta > 0 and not approval_named:
            errors.append(f"positive {dimension} budget requires a named budget_approval")
        allowed = int(baseline_count * ratio_limit) + delta
        if current_count > allowed:
            errors.append(
                f"{dimension} cap exceeded: current={current_count} "
                f"allowed={allowed} baseline={baseline_count}"
            )
    return errors


def reset_errors(ledger: list[dict[str, Any]]) -> list[str]:
    """The completed reset holds both removal kinds, and precedes every lifecycle event.

    Admissions no longer spend a retirement or a budget unit: the owner dropped that rule on
    2026-11-20. Admission rows recorded before then keep their `compensating_retirement`.
    """
    errors: list[str] = []
    dispositions = [row for row in ledger if row.get("record_type") == "proof_disposition"]
    if not {"delete", "consolidate"} <= {str(row.get("disposition")) for row in dispositions}:
        errors.append("reset must contain both delete and consolidate dispositions")
    disposition_phase_open = True
    for row in ledger:
        if row.get("record_type") == "proof_disposition":
            if not disposition_phase_open:
                errors.append("baseline dispositions must precede lifecycle events")
            continue
        disposition_phase_open = False
    return errors


def effectiveness_errors(
    root: Path, manifest: dict[str, Any], family_ids: list[Any]
) -> tuple[list[str], dict[str, float]]:
    errors: list[str] = []
    limits = manifest.get("reset_limits") if isinstance(manifest.get("reset_limits"), dict) else {}
    corpus_rel = manifest.get("effectiveness_corpus")
    report_rel = manifest.get("effectiveness_report")
    corpus = _load_yaml(root / corpus_rel) if isinstance(corpus_rel, str) else {}
    effectiveness = _load_ledger(root / report_rel) if isinstance(report_rel, str) else []
    cases = corpus.get("cases") if isinstance(corpus, dict) else None
    if corpus.get("selection_frozen") is not True:
        errors.append("effectiveness selection must be frozen before holdout execution")
    if not isinstance(cases, list):
        errors.append("effectiveness corpus cases must be a list")
        cases = []
    case_ids = [case.get("id") for case in cases if isinstance(case, dict)]
    if len(case_ids) != len(set(case_ids)):
        errors.append("effectiveness corpus contains duplicate case ids")
    evidence_ids = [row.get("evidence_id") for row in effectiveness]
    if len(evidence_ids) != len(set(evidence_ids)):
        errors.append("effectiveness report contains duplicate case ids")
    if set(evidence_ids) != set(case_ids):
        errors.append("effectiveness report does not cover the frozen corpus exactly")
    for case in cases:
        if not isinstance(case, dict) or set(case) != CASE_FIELDS:
            errors.append(f"effectiveness case has wrong fields: {case!r}")
            continue
        errors.extend(_validate_oracle(case["oracle"], str(case["id"])))
        patch = root / str(case["patch"])
        if not patch.is_file() or not patch.read_text().strip():
            errors.append(f"effectiveness patch is absent or empty: {case['patch']}")
            continue
        if case["patch_sha256"] != hashlib.sha256(patch.read_bytes()).hexdigest():
            errors.append(f"effectiveness patch digest drifted: {case['id']}")
    cases_by_id = {
        case["id"]: case
        for case in cases
        if isinstance(case, dict) and isinstance(case.get("id"), str)
    }
    recall: dict[str, float] = {}
    for evidence_class, limit_key in (
        ("historical_defect", "min_historical_recall"),
        ("holdout_mutant", "min_mutant_recall"),
    ):
        expected = [
            case for case in cases if isinstance(case, dict) and case.get("class") == evidence_class
        ]
        if len(expected) != 12:
            errors.append(f"{evidence_class} corpus must contain exactly 12 cases")
        evidence = [row for row in effectiveness if row.get("evidence_class") == evidence_class]
        observed = [row for row in evidence if row.get("observed") is True]
        for row in evidence:
            if set(row) != EFFECTIVENESS_FIELDS:
                errors.append(f"{evidence_class} {row.get('evidence_id')} has wrong fields")
                continue
            case = cases_by_id.get(row["evidence_id"])
            if case is None:
                continue
            if row["observed"] not in (True, False):
                errors.append(f"{evidence_class} {row['evidence_id']} has invalid observation")
            if row["command"] != case["command"]:
                errors.append(f"{evidence_class} {row['evidence_id']} command drifted")
            if row["patch_sha256"] != case["patch_sha256"]:
                errors.append(f"{evidence_class} {row['evidence_id']} patch digest drifted")
            if row["oracle_sha256"] != _canonical_sha256(case["oracle"]):
                errors.append(f"{evidence_class} {row['evidence_id']} oracle drifted")
            oracle_matched = (
                row["return_code"] == case["oracle"].get("exit_code") and row["observed"] is True
            )
            if not oracle_matched and row["detected_by"]:
                errors.append(f"{evidence_class} {row['evidence_id']} credits an unmatched oracle")
        detected = [
            row
            for row in observed
            if isinstance(row.get("detected_by"), list)
            and any(item in family_ids for item in row["detected_by"])
        ]
        minimum = limits.get(limit_key)
        if not evidence:
            errors.append(f"audit ledger has no {evidence_class} evidence")
            continue
        if not isinstance(minimum, (int, float)) or not 0 <= minimum <= 1:
            errors.append(f"{limit_key} must be between 0 and 1")
            continue
        score = _ratio(len(detected), len(evidence))
        recall[evidence_class] = score
        if score < minimum:
            errors.append(
                f"{evidence_class} recall below floor: "
                f"{len(detected)}/{len(evidence)}={score:.3f} < {minimum:.3f}"
            )
    return errors, recall


def validation_errors(
    root: Path,
    manifest: dict[str, Any],
    baseline: dict[str, Any],
    current: dict[str, Any],
    ledger: list[dict[str, Any]],
    family_ids: list[Any],
) -> tuple[list[str], dict[str, Any]]:
    errors = count_errors(manifest, baseline, current)
    errors.extend(reset_errors(ledger))
    try:
        effectiveness, recall = effectiveness_errors(root, manifest, family_ids)
    except (OSError, ValueError, RecipientGovernanceError) as exc:
        effectiveness, recall = [f"effectiveness evidence unreadable: {exc}"], {}
    errors.extend(effectiveness)
    return errors, {"recall": recall}


def assay(root: Path, *, evidence_class: str | None = None) -> list[dict[str, Any]]:
    manifest = _load_yaml(root / "tests/proof-estate.yaml")
    corpus = _load_yaml(root / str(manifest["effectiveness_corpus"]))
    cases = corpus.get("cases")
    if corpus.get("selection_frozen") is not True or not isinstance(cases, list):
        raise RecipientGovernanceError("effectiveness corpus is not frozen and complete")
    rows: list[dict[str, Any]] = []
    for case in cases:
        if evidence_class and case.get("class") != evidence_class:
            continue
        with tempfile.TemporaryDirectory(prefix="proof-assay-") as temporary:
            work = Path(temporary) / "repo"
            shutil.copytree(
                root,
                work,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache"),
            )
            link_declared_runtime_store(root, work)
            patch = root / str(case["patch"])
            patch_sha256 = hashlib.sha256(patch.read_bytes()).hexdigest()
            if case.get("patch_sha256") != patch_sha256:
                raise RecipientGovernanceError(f"assay patch digest drifted for {case['id']}")
            command = shlex.split(str(case["command"]))
            case_root = work / str(case.get("cwd", "."))
            oracle = case.get("oracle")
            oracle_errors = _validate_oracle(oracle, str(case["id"]))
            if oracle_errors:
                raise RecipientGovernanceError("; ".join(oracle_errors))
            # Each disposable checkout resolves its own declared runtime. Inherited
            # source-checkout pins cannot authorize a different copied root.
            environment = os.environ.copy()
            for pin in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "AGENTIC_UV_ISOLATION_ROOT"):
                environment.pop(pin, None)
            control = _run(command, case_root, environment=environment)
            control_output = (control.stdout + "\n" + control.stderr).encode()
            if control.returncode != 0:
                detail = (
                    "\n".join(
                        part for part in (control.stdout.strip(), control.stderr.strip()) if part
                    )
                    or "no diagnostic"
                )
                raise RecipientGovernanceError(
                    f"assay control failed before mutation for {case['id']} "
                    f"({control.returncode}): {detail}"
                )
            applied = _run(["git", "apply", str(patch)], work)
            if applied.returncode != 0:
                raise RecipientGovernanceError(
                    f"assay patch does not apply for {case['id']}: {applied.stderr.strip()}"
                )
            result = _run(command, case_root, environment=environment)
            output = (result.stdout + "\n" + result.stderr).encode()
            observed = _oracle_matches(result, oracle)
            rows.append(
                {
                    "record_type": "effectiveness",
                    "evidence_id": case["id"],
                    "evidence_class": case["class"],
                    "observed": observed,
                    "detected_by": [case["owner"]] if observed else [],
                    "command": case["command"],
                    "patch_sha256": patch_sha256,
                    "oracle_sha256": _canonical_sha256(oracle),
                    "control_output_sha256": hashlib.sha256(control_output).hexdigest(),
                    "output_sha256": hashlib.sha256(output).hexdigest(),
                    "return_code": result.returncode,
                }
            )
    return rows
