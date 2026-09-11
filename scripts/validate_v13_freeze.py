"""Validate v13 planning coverage; this does not certify runtime remediation."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

OWNERS = {
    "STATE": {"C01", "C02", "C03", "C04", "C16", "R03", "R10"},
    "DATA": {"C05", "C06", "C07", "C08", "C12", "C17", "C18", "R01", "R02"},
    "SAFETY": {"C09", "C13", "C14", "C15", "C19", "C20", "R04", "R05"},
    "DIST": {"C10", "C11", "C21", "R06", "R08", "R09", "R11", "R12"},
    "PILOT": {"R07"},
}
DECISIONS = {"local_inference_budget", "signing_mechanism"}
INVARIANTS = {
    "generation_binding",
    "registry_mutation",
    "registry_visibility",
    "writer_admission",
    "reader_admission",
    "retained_data",
    "publication",
    "rollback",
    "cleanup_debt",
    "metadata_failures",
    "pool_lifetime",
    "shared_file_schedule",
}
POLICIES = {
    "support",
    "ignore_and_edits",
    "pmcp",
    "qdrant",
    "signer",
    "pilot",
    "metrics",
    "http",
    "sandbox",
    "ci",
    "recovery",
    "proof_nodes",
    "evidence_schema",
}
MODELS = {"claude-fable-5", "gpt-5.6-sol", "grok-4.5", "Gemini 3.1 Pro"}
TABLE_DDL = re.compile(
    r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)


def application_tables(root: Path) -> set[str]:
    """Inventory literal DDL, excluding SQLite's generated FTS shadow tables."""
    source = root / "mcp_server/storage/sqlite_store.py"
    literals = [
        node.value
        for node in ast.walk(ast.parse(source.read_text()))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    literals.extend(
        path.read_text() for path in (root / "mcp_server/storage/migrations").glob("*.sql")
    )
    return {table for sql in literals for table in TABLE_DDL.findall(sql)}


def validate(contract: dict[str, Any], root: Path, *, acceptance: bool = False) -> list[str]:
    """Check coverage and approval-reference presence, not approval authenticity."""
    errors = []
    if contract.get("schema_version") != "v13.freeze.v1":
        errors.append("invalid contract schema_version")
    roadmap = root / "specs/phase-plans-v13.md"
    if hashlib.sha256(roadmap.read_bytes()).hexdigest() != contract.get("roadmap_sha256"):
        errors.append("reviewed roadmap digest mismatch")
    rows = contract.get("findings", [])
    expected = set().union(*OWNERS.values())
    ids = [row.get("id") for row in rows]
    if set(ids) != expected or len(ids) != len(expected):
        errors.append("finding inventory must contain C01-C21 and R01-R12 exactly once")
    for row in rows:
        identifier = row.get("id")
        if identifier not in OWNERS.get(row.get("owner"), set()):
            errors.append(f"finding {identifier}: wrong phase owner")
        for field in ("command", "acceptance", "evidence_status", "rollout_consequence"):
            if not row.get(field):
                errors.append(f"finding {identifier}: missing {field}")
        if row.get("tracking") != "roadmap_only" or row.get("disposition") != "required":
            errors.append(f"finding {identifier}: unsupported kickoff disposition")
        for field in ("source", "probe"):
            reference = row.get(field, {})
            path = (root / reference.get("path", "")).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                errors.append(f"finding {identifier}: missing or external {field} path")
                continue
            if field == "probe" and reference.get("kind") == "retained_counterexample":
                functions = {
                    node.name
                    for node in ast.walk(ast.parse(path.read_text()))
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                if reference.get("symbol") not in functions:
                    errors.append(f"finding {identifier}: missing probe symbol")
            elif field == "probe" and reference.get("kind") != "static_risk":
                errors.append(f"finding {identifier}: invalid probe kind")

    tables = contract.get("data_ownership", [])
    names = [row.get("table") for row in tables]
    if set(names) != application_tables(root) or len(names) != len(set(names)):
        errors.append("table inventory does not match production DDL")
    for row in tables:
        if not row.get("retention") or not row.get("kind") or not row.get("owner"):
            errors.append(f"table {row.get('table')}: missing retention or ownership")
    for key in INVARIANTS:
        if not contract.get("invariants", {}).get(key):
            errors.append(f"missing invariant {key}")
    policies = contract.get("policies", {})
    for key in POLICIES:
        if not policies.get(key):
            errors.append(f"missing policy {key}")
    qdrant = policies.get("qdrant", {})
    if set(qdrant.get("required_modes", [])) != {"file_backed", "client_server"}:
        errors.append("Qdrant topology coverage incomplete")
    if qdrant.get("point_boundaries") != [1000, 1001]:
        errors.append("Qdrant pagination boundary missing")
    if not qdrant.get("no_live_lock_removal") or not qdrant.get("no_backend_fallback"):
        errors.append("Qdrant ownership restrictions missing")
    panel = contract.get("review_panel", [])
    if {row.get("model") for row in panel} != MODELS or len(panel) != 4:
        errors.append("four-seat model inventory mismatch")
    pmcp = policies.get("pmcp", {})
    if pmcp.get("args", [])[-1:] != ["stdio"] or not pmcp.get("same_registry_for_cli_and_child"):
        errors.append("PMCP STDIO/shared-registry contract missing")
    pilot = policies.get("pilot", {})
    if pilot.get("commercial_egress") is not False or not pilot.get("retries_consume_budget"):
        errors.append("pilot egress or retry-budget restrictions missing")
    for key in ("input_token_limit", "local_inference_seconds_limit"):
        if not isinstance(pilot.get(key), int) or pilot[key] <= 0:
            errors.append(f"invalid pilot {key}")
    if policies.get("signer", {}).get("enforce_required") is not True:
        errors.append("attestation enforce restriction missing")
    decisions = contract.get("decisions", [])
    if {row.get("id") for row in decisions} != DECISIONS or len(decisions) != 2:
        errors.append("owner decision inventory mismatch")
    if acceptance:
        for row in decisions:
            if row.get("status") != "approved" or not row.get("approval_ref"):
                errors.append(f"owner approval pending: {row.get('id')}")
    return errors


def main() -> int:
    """Print a metadata-only result and fail closed on unaccepted decisions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acceptance", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    path = root / "docs/contracts/v13-freeze.json"
    contract = json.loads(path.read_text())
    errors = validate(contract, root, acceptance=args.acceptance)
    print(
        json.dumps(
            {
                "schema_version": "v13.freeze-check.v1",
                "mode": "acceptance" if args.acceptance else "structure",
                "ok": not errors,
                "contract_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "findings": len(contract.get("findings", [])),
                "tables": len(contract.get("data_ownership", [])),
                "errors": errors,
            },
            indent=2,
        )
    )
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
