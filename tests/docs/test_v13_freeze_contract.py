"""Offline checks for the v13 contract, not evidence of repaired runtime bugs."""

import copy
import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/validate_v13_freeze.py"
CONTRACT = ROOT / "docs/contracts/v13-freeze.json"


def load_contract():
    return json.loads(CONTRACT.read_text())


def validate(contract, acceptance=False):
    return runpy.run_path(str(SCRIPT))["validate"](contract, ROOT, acceptance=acceptance)


def test_complete_structural_contract():
    assert validate(load_contract()) == []


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_owner"])
def test_finding_inventory_cannot_drop_or_misroute_work(mutation):
    contract = load_contract()
    if mutation == "missing":
        contract["findings"].pop()
    elif mutation == "duplicate":
        contract["findings"].append(copy.deepcopy(contract["findings"][0]))
    else:
        contract["findings"][0]["owner"] = "SHIP"
    assert any("finding" in error for error in validate(contract))


def test_missing_table_or_retention_policy_is_rejected():
    contract = load_contract()
    contract["data_ownership"].pop()
    contract["data_ownership"][0]["retention"] = ""
    errors = validate(contract)
    assert any("table inventory" in error for error in errors)
    assert any("retention" in error for error in errors)


@pytest.mark.parametrize("field", ["source", "probe"])
def test_missing_source_or_probe_is_rejected(field):
    contract = load_contract()
    contract["findings"][0][field]["path"] = "missing-v13-evidence.py"
    assert any("missing" in error for error in validate(contract))


def test_missing_counterexample_symbol_is_rejected():
    contract = load_contract()
    contract["findings"][0]["probe"]["symbol"] = "test_invented_evidence"
    assert any("probe symbol" in error for error in validate(contract))


def test_changed_roadmap_digest_is_rejected():
    contract = load_contract()
    contract["roadmap_sha256"] = "0" * 64
    assert "reviewed roadmap digest mismatch" in validate(contract)


def test_pending_or_unattributed_owner_decision_cannot_pass():
    contract = load_contract()
    for decision in contract["decisions"]:
        decision["status"] = "pending"
        decision["approval_ref"] = None
    assert len([e for e in validate(contract, True) if "approval" in e]) == 2
    for decision in contract["decisions"]:
        decision["status"] = "approved"
    assert len([e for e in validate(contract, True) if "approval" in e]) == 2


def test_missing_decision_cannot_vacuously_pass():
    contract = load_contract()
    contract["decisions"] = []
    assert "owner decision inventory mismatch" in validate(contract, True)


def test_approval_is_contract_acceptance_not_runtime_proof():
    contract = load_contract()
    for decision in contract["decisions"]:
        decision["status"] = "approved"
        decision["approval_ref"] = "synthetic unit-test approval only"
    assert validate(contract, True) == []
    assert all(row["disposition"] == "required" for row in contract["findings"])


def test_cli_separates_structure_from_acceptance():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["mode"] == "structure"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--acceptance"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    pending = any(
        row["status"] != "approved" or not row["approval_ref"]
        for row in load_contract()["decisions"]
    )
    assert result.returncode == int(pending)
    assert json.loads(result.stdout)["mode"] == "acceptance"
