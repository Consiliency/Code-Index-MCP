"""Receipt reduction must not promote incomplete installed or live evidence."""

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.v13_pmcp_pilot import (
    GOALS,
    OwnedContainer,
    OwnedProcess,
    PilotRefused,
    cgroup_processes,
    digest_json,
    validate_receipt,
    verify_saved_receipt,
)


@pytest.mark.parametrize("detach_at_shutdown", [False, True])
def test_owned_scope_catches_detached_children(tmp_path, detach_at_shutdown):
    marker = tmp_path / "child.pid"
    program = r"""
import os, signal, sys, time
def detach(*args):
    pid = os.fork()
    if pid == 0:
        os.setsid()
        signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))
        with open(sys.argv[1], 'w') as stream:
            stream.write(str(os.getpid()))
        time.sleep(60)
        sys.exit(0)
    if args:
        sys.exit(0)
if sys.argv[2] == 'late':
    signal.signal(signal.SIGTERM, detach)
else:
    detach()
time.sleep(60)
"""
    owner = OwnedProcess(
        [sys.executable, "-c", program, str(marker), "late" if detach_at_shutdown else "early"],
        tmp_path,
        dict(os.environ),
        "owned-test",
    )
    try:
        if not detach_at_shutdown:
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert marker.exists()
        else:
            time.sleep(0.2)
        owner.observe()
        owner.stop()
        assert not cgroup_processes(owner.group)
        assert owner.peak_rss_mib > 0
        if detach_at_shutdown:
            # A child born after group SIGTERM must remain a failed receipt even
            # though final SIGKILL cleanup removes it from the owned cgroup.
            assert owner.survivors and owner.exit_seconds >= 5
        else:
            assert not owner.survivors and owner.exit_seconds <= 5
    finally:
        if not owner.log.closed:
            owner.stop()


@pytest.mark.parametrize("elapsed,exit_code", [(1, 0), (6, 0), (1, 137)])
def test_container_retirement_records_slow_and_forced_exit(
    monkeypatch, tmp_path, manifest, elapsed, exit_code
):
    from scripts import v13_pmcp_pilot as pilot

    owner = OwnedContainer.__new__(OwnedContainer)
    owner.container, owner.root, owner.group = "a" * 64, tmp_path, tmp_path / "absent-cgroup"
    owner.peak_rss_mib = 80
    clock = iter([0, elapsed])
    monkeypatch.setattr(pilot.time, "monotonic", lambda: next(clock))
    calls = []

    def command(args, *rest, **kwargs):
        calls.append((args, kwargs))
        return (
            json.dumps({"Running": False, "ExitCode": exit_code, "OOMKilled": False})
            if args[1] == "inspect"
            else ""
        )

    monkeypatch.setattr(pilot, "run_command", command)
    owner.stop()
    value = receipt(manifest, "live")
    value.update(
        shutdown_seconds=[owner.exit_seconds],
        surviving_children=owner.survivors,
        peak_rss_mib=owner.peak_rss_mib,
    )
    if elapsed > 5 or exit_code == 137:
        with pytest.raises(PilotRefused, match="operational"):
            validate_receipt(value, manifest, "live")
    else:
        validate_receipt(value, manifest, "live")
    assert calls[0][1]["timeout"] == 6
    assert calls[-1][0] == ["docker", "rm", "--force", owner.container]


@pytest.fixture
def manifest():
    return {"source": "a" * 40, "tree": "b" * 40, "wheel_sha256": "c" * 64}


def receipt(manifest, kind):
    return {
        "kind": kind,
        "manifest_sha256": digest_json(manifest),
        "source": manifest["source"],
        "wheel_sha256": manifest["wheel_sha256"],
        "goals": dict.fromkeys(GOALS[kind], True),
        "shutdown_seconds": [1, 3.1],
        "surviving_children": [],
        "peak_rss_mib": 100,
        "latencies_ms": {"symbol": [20] * 40, "lexical": [60] * 40, "semantic": [100] * 40},
        "contention_successes": dict.fromkeys(("symbol", "lexical", "semantic"), 20),
        "budget": {"reserved_input_units": 2000, "elapsed_seconds": 20, "blocked": None},
    }


@pytest.mark.parametrize("kind", ["offline", "live", "browser"])
def test_complete_receipt_contract(manifest, kind):
    validate_receipt(receipt(manifest, kind), manifest, kind)


@pytest.mark.parametrize("field", ["source", "wheel_sha256", "manifest_sha256"])
def test_candidate_or_manifest_drift_refused(manifest, field):
    value = receipt(manifest, "offline")
    value[field] = "wrong"
    with pytest.raises(PilotRefused, match="binding"):
        validate_receipt(value, manifest, "offline")


@pytest.mark.parametrize("kind", ["offline", "live", "browser"])
def test_missing_or_non_boolean_goal_cannot_pass(manifest, kind):
    value = receipt(manifest, kind)
    name = next(iter(GOALS[kind]))
    for missing in (False, True):
        broken = copy.deepcopy(value)
        if missing:
            del broken["goals"][name]
        else:
            broken["goals"][name] = "passed"
        with pytest.raises(PilotRefused, match="goals"):
            validate_receipt(broken, manifest, kind)


@pytest.mark.parametrize(
    "field,value",
    [
        ("shutdown_seconds", [5.01]),
        ("surviving_children", [123]),
        ("peak_rss_mib", 2048.01),
    ],
)
def test_operational_thresholds_are_not_advisory(manifest, field, value):
    result = receipt(manifest, "offline")
    result[field] = value
    with pytest.raises(PilotRefused, match="operational"):
        validate_receipt(result, manifest, "offline")


@pytest.mark.parametrize(
    "field,value",
    [
        ("reserved_input_units", 100001),
        ("elapsed_seconds", 900.01),
        ("blocked", "unsettled_transport"),
    ],
)
def test_live_allowance_limits_are_enforced(manifest, field, value):
    result = receipt(manifest, "live")
    result["budget"][field] = value
    with pytest.raises(PilotRefused, match="budget"):
        validate_receipt(result, manifest, "live")


@pytest.mark.parametrize("kind", ["symbol", "lexical", "semantic"])
def test_refusals_do_not_count_as_successful_contention_samples(manifest, kind):
    result = receipt(manifest, "live")
    result["contention_successes"][kind] = 19
    result["contention_refusals"] = {kind: 100}
    with pytest.raises(PilotRefused, match="contention"):
        validate_receipt(result, manifest, "live")


@pytest.mark.parametrize("kind,value", [("symbol", 101), ("lexical", 501), ("semantic", 501)])
def test_latency_threshold_and_missing_samples_refused(manifest, kind, value):
    result = receipt(manifest, "live")
    for values in ([value] * 40, []):
        result["latencies_ms"][kind] = values
        with pytest.raises(PilotRefused, match="latency"):
            validate_receipt(result, manifest, "live")


def test_browser_goals_without_artifacts_cannot_pass(tmp_path, manifest):
    (tmp_path / "browser.json").write_text(json.dumps(receipt(manifest, "browser")))
    with pytest.raises(PilotRefused, match="artifacts"):
        verify_saved_receipt(tmp_path, manifest, "browser")


@pytest.mark.parametrize("valid", [True, False])
def test_prepare_delivered_wheel_never_builds(tmp_path, monkeypatch, valid):
    from scripts import v13_pmcp_pilot as pilot

    wheel = tmp_path / "index_it_mcp-1.4.1-py3-none-any.whl"
    wheel.write_bytes(b"registry wheel fixture")
    root = tmp_path / "owned"
    calls = []
    monkeypatch.setattr(pilot, "source_identity", lambda: {"source": "a" * 40})

    def run(command, directory, label, **kwargs):
        assert command[:2] != ["uv", "build"]
        calls.append(command)
        if label == "pilot-lock-export":
            (directory / "constraints.txt").write_text("dependency==1\n")
        return "{}" if label == "installed-identity" else "fixture"

    monkeypatch.setattr(pilot, "run_command", run)
    digest = pilot.digest_file(wheel) if valid else "0" * 64
    if not valid:
        with pytest.raises(PilotRefused, match="digest_mismatch"):
            pilot.prepare(root, wheel, digest)
        assert not calls
    else:
        result = pilot.prepare(root, wheel, digest)
        assert result["artifact_origin"] == "registry"
        assert result["wheel_sha256"] == digest
        assert (root / "dist" / wheel.name).read_bytes() == wheel.read_bytes()
        assert (root / "dist" / wheel.name).as_uri() in " ".join(result["uvx_prefix"])


def test_artifact_replacement_cannot_change_the_validated_snapshot(tmp_path, manifest, monkeypatch):
    from scripts import v13_pmcp_pilot as pilot

    result = receipt(manifest, "browser")
    asset = tmp_path / "asset.json"
    asset.write_text('{"original": true}')
    result["artifacts"] = [
        {"role": role, "path": asset.name, "sha256": pilot.digest_file(asset)}
        for role in (
            "inspector_screenshot",
            "admin_screenshot",
            "browser_actions",
            "browser_session",
        )
    ]
    (tmp_path / "browser.json").write_text(json.dumps(result))

    def replace_then_check(root, manifest, kind, result, copies, expected_approval):
        replacement = root / "replacement.json"
        replacement.write_text('{"unvalidated": true}')
        replacement.replace(asset)
        assert json.loads(copies[asset].read_bytes()) == {"original": True}

    monkeypatch.setattr(pilot, "_verify_receipt_artifacts", replace_then_check)
    with pytest.raises(PilotRefused, match="evidence_changed"):
        verify_saved_receipt(tmp_path, manifest, "browser")


@pytest.mark.parametrize("damage", ["missing", "drift", "outside"])
def test_browser_artifacts_are_bound_and_confined(tmp_path, manifest, damage):
    from scripts.v13_pmcp_pilot import digest_file

    result = receipt(manifest, "browser")
    asset = tmp_path / "asset.json"
    asset.write_text("{}")
    result["artifacts"] = [
        {"role": role, "path": "asset.json", "sha256": digest_file(asset)}
        for role in (
            "inspector_screenshot",
            "admin_screenshot",
            "browser_actions",
            "browser_session",
        )
    ]
    if damage == "missing":
        asset.unlink()
    elif damage == "drift":
        asset.write_text("changed")
    else:
        result["artifacts"][0]["path"] = "../asset.json"
    (tmp_path / "browser.json").write_text(json.dumps(result))
    with pytest.raises(PilotRefused, match="artifact"):
        verify_saved_receipt(tmp_path, manifest, "browser")


def test_rehearsal_cannot_be_live_acceptance(manifest):
    result = receipt(manifest, "live")
    result["rehearsal"] = True
    with pytest.raises(PilotRefused, match="rehearsal"):
        validate_receipt(result, manifest, "live")


@pytest.mark.parametrize(
    "ids,preferred,expected",
    [
        (["chat", "other"], "chat", "chat"),
        (["served-alias"], "chat", "served-alias"),
    ],
)
def test_model_selection_uses_reported_catalog(ids, preferred, expected):
    from scripts.v13_pmcp_pilot import select_model

    assert select_model({"data": [{"id": value} for value in ids]}, preferred) == expected


@pytest.mark.parametrize("catalog", [{}, {"data": []}, {"data": [{"id": "a"}, {"id": "b"}]}])
def test_ambiguous_model_catalog_refused(catalog):
    from scripts.v13_pmcp_pilot import select_model

    with pytest.raises(PilotRefused, match="model_catalog"):
        select_model(catalog, "unreported")


@pytest.mark.parametrize("damage", [None, "binding", "unstarted", "shutdown", "actions", "image"])
def test_browser_artifact_contents_are_verified(tmp_path, manifest, damage):
    from PIL import Image

    from scripts.v13_pmcp_pilot import digest_file

    result = receipt(manifest, "browser")
    binding = {key: result[key] for key in ("source", "wheel_sha256", "manifest_sha256")}
    session = {
        **binding,
        "session_started": True,
        "shutdown_seconds": [1],
        "surviving_children": [],
        "peak_rss_mib": 100,
    }
    actions = {
        **binding,
        "events": [
            {"goal": goal, "ok": True, "observed": {"fixture": True}} for goal in GOALS["browser"]
        ],
    }
    if damage == "binding":
        session["source"] = "wrong"
    elif damage == "unstarted":
        session["session_started"] = False
    elif damage == "shutdown":
        session["shutdown_seconds"] = [6]
    elif damage == "actions":
        actions["events"].pop()
    (tmp_path / "session.json").write_text(json.dumps(session))
    (tmp_path / "actions.json").write_text(json.dumps(actions))
    for name in ("admin", "inspector"):
        Image.new("RGB", (100, 100), "white").save(tmp_path / (name + ".png"))
    if damage == "image":
        (tmp_path / "admin.png").write_bytes(b"not an image")
    result["artifacts"] = [
        {"role": role, "path": name, "sha256": digest_file(tmp_path / name)}
        for role, name in (
            ("browser_session", "session.json"),
            ("browser_actions", "actions.json"),
            ("admin_screenshot", "admin.png"),
            ("inspector_screenshot", "inspector.png"),
        )
    ]
    (tmp_path / "browser.json").write_text(json.dumps(result))
    if damage:
        with pytest.raises(PilotRefused):
            verify_saved_receipt(tmp_path, manifest, "browser")
    else:
        verify_saved_receipt(tmp_path, manifest, "browser")


def test_hashes_and_goal_flags_cannot_replace_live_records(tmp_path, manifest):
    from scripts.v13_pmcp_pilot import digest_file

    (tmp_path / "not-a-ledger.json").write_text("{}")
    result = receipt(manifest, "live")
    result["rehearsal"] = False
    result["artifacts"] = [
        {
            "role": role,
            "path": "not-a-ledger.json",
            "sha256": digest_file(tmp_path / "not-a-ledger.json"),
        }
        for role in ("allowance_ledger", "runtime_provenance")
    ]
    (tmp_path / "live.json").write_text(json.dumps(result))
    with pytest.raises(PilotRefused):
        verify_saved_receipt(tmp_path, manifest, "live")


@pytest.fixture(params=["original", "renewed"])
def live_records(tmp_path, manifest, request, monkeypatch):
    import hashlib

    from scripts import v13_pilot_budget as budget
    from scripts.v13_pilot_budget import ENDPOINTS, BudgetLedger
    from scripts.v13_pilot_estimate import REQUEST_ENVELOPES, SYNTHETIC_CORPUS
    from scripts.v13_pmcp_pilot import QDRANT_IMAGE, QUERY_TEXTS

    result = receipt(manifest, "live")
    result.update(rehearsal=False, workflow_completed=True, samples=[], index_intervals=[])
    ledger_root = tmp_path / "ledger"
    approval = budget.RENEWED_APPROVAL if request.param == "renewed" else budget.APPROVAL
    if request.param == "renewed":
        monkeypatch.setattr(budget, "RENEWED_ROOT", ledger_root)
    BudgetLedger.initialize(ledger_root, digest_json(manifest), approval=approval)
    ledger = BudgetLedger(
        ledger_root, digest_json(manifest), clock=lambda: (1000.0, 100.0), approval=approval
    )
    for request_class, count in (
        ("provenance_probe", 2),
        ("summary", 1),
        ("document_embedding", 1),
        ("query_embedding", 40),
    ):
        for _ in range(count):
            request = ledger.reserve(
                "enrichment" if request_class == "summary" else "embedding",
                100,
                request_class=request_class,
                envelope=REQUEST_ENVELOPES[request_class],
            )
            ledger.finish(request, "success", 200)
    result["budget"] = {**ledger.snapshot(), "elapsed_seconds": 20}
    for repo_index, repo in enumerate(SYNTHETIC_CORPUS):
        for index in range(20):
            for kind_index, kind in enumerate(("symbol", "lexical", "semantic")):
                started = 101 + repo_index * 3 + (index * 3 + kind_index) * 0.03
                ended = started + 0.02
                result["samples"].append(
                    {
                        "kind": kind,
                        "repository": repo,
                        "started": started,
                        "ended": ended,
                        "milliseconds": (ended - started) * 1000,
                        "ready_success": True,
                    }
                )
    result["index_intervals"] = [
        {"repository": "catalog", "started": 100.0, "ended": 103.0, "success": True},
        {"repository": "ledger", "started": 103.0, "ended": 107.0, "success": True},
    ]
    result["latencies_ms"] = {
        kind: [sample["milliseconds"] for sample in result["samples"] if sample["kind"] == kind]
        for kind in ("symbol", "lexical", "semantic")
    }
    result["contention_successes"] = dict.fromkeys(("symbol", "lexical", "semantic"), 40)
    workload = {
        "corpus": SYNTHETIC_CORPUS,
        "request_envelopes": REQUEST_ENVELOPES,
        "query_texts": QUERY_TEXTS,
        "measured_queries_per_class_per_repository": 20,
        "rehearsal": False,
        "manifest_sha256": digest_json(manifest),
    }
    metadata = {
        "models": {"embedding": "unit-fixture", "enrichment": "unit-chat"},
        "dimension": 8,
        "immutable_revision": "unreported",
        "qdrant_image": QDRANT_IMAGE,
        "endpoints": ENDPOINTS,
        "workload_sha256": digest_json(workload),
    }
    repositories = []
    for repo, filename in (("ledger", "bookkeeping.py"), ("catalog", "catalog.py")):
        repositories.append(
            {
                "repository": repo,
                "commit": "a" * 40,
                "generation": "b" * 32,
                "point_count": 1,
                "mapping_count": 1,
                "point_ids": ["1"],
                "attested": True,
                "collection_manifest": {
                    "indexed_commit": "a" * 40,
                    "point_set_id": hashlib.sha256(b"1").hexdigest(),
                    "corpus_sha256": hashlib.sha256(filename.encode()).hexdigest(),
                    "profile_fingerprint": "c" * 32,
                    "provider_id": "unit-fixture",
                    "provenance_version": "collection-provenance.v1",
                },
                "embedding_provenance": {
                    "served_model_id": {"source": "reported", "value": "unit-fixture"},
                    "dimension": {"source": "reported", "value": 8},
                    "model_revision": {"source": "declared", "value": "unreported"},
                },
            }
        )
    return (
        result,
        ledger,
        {
            "workload": workload,
            "runtime_metadata": metadata,
            "runtime_provenance": {"repositories": repositories},
        },
    )


@pytest.mark.parametrize(
    "damage",
    [
        None,
        "ledger",
        "accounting",
        "inflight",
        "envelope",
        "unknown_class",
        "samples_missing",
        "sample_refusal",
        "sample_timing",
        "sample_outside_budget",
        "sample_repository",
        "contention_count",
        "contention_interval",
        "workload",
        "endpoints",
        "provenance",
        "corpus",
        "point_ids",
        "mapping_count",
        "model",
        "dimension",
        "revision_missing",
        "revision_mismatch",
        "rehearsal",
        "incomplete",
        "duplicate_artifact",
    ],
)
def test_live_record_reduction_is_consistent_and_read_only(
    tmp_path, manifest, live_records, damage
):
    import sqlite3

    from scripts.v13_pmcp_pilot import digest_file

    result, ledger, documents = live_records
    ledger_path = ledger.root / "ledger.sqlite"
    if damage == "ledger":
        ledger_path.write_bytes(b"{}")
    elif damage == "accounting":
        result["budget"]["reserved_input_units"] += 1
    elif damage in {"inflight", "envelope", "unknown_class"}:
        with sqlite3.connect(ledger_path) as db:
            if damage == "inflight":
                db.execute(
                    "UPDATE requests SET outcome='inflight',finished_wall=NULL WHERE rowid=1"
                )
            else:
                db.execute(
                    "UPDATE requests SET request_class=?",
                    ("summary" if damage == "envelope" else "unknown",),
                )
        result["budget"] = {**ledger.snapshot(), "elapsed_seconds": 20}
    elif damage == "samples_missing":
        result["samples"].pop()
    elif damage == "sample_refusal":
        result["samples"][0]["ready_success"] = False
    elif damage == "sample_timing":
        result["samples"][0]["milliseconds"] = 0
    elif damage == "sample_outside_budget":
        result["samples"][0].update(started=10000, ended=10000.02)
    elif damage == "sample_repository":
        result["samples"][0]["repository"] = "catalog"
    elif damage == "contention_count":
        result["contention_successes"]["symbol"] = 39
    elif damage == "contention_interval":
        result["index_intervals"][0]["success"] = False
    elif damage == "workload":
        documents["workload"]["corpus"] = {}
    elif damage == "endpoints":
        documents["runtime_metadata"]["endpoints"] = {}
    elif damage == "provenance":
        documents["runtime_provenance"]["repositories"] = []
    elif damage == "corpus":
        documents["runtime_provenance"]["repositories"][0]["collection_manifest"][
            "corpus_sha256"
        ] = None
    elif damage == "point_ids":
        documents["runtime_provenance"]["repositories"][0]["point_ids"] = ["2"]
    elif damage == "mapping_count":
        documents["runtime_provenance"]["repositories"][0]["mapping_count"] = 0
    elif damage == "model":
        documents["runtime_provenance"]["repositories"][0]["embedding_provenance"][
            "served_model_id"
        ]["value"] = "wrong"
    elif damage == "dimension":
        documents["runtime_metadata"]["dimension"] = 9
    elif damage == "revision_missing":
        del documents["runtime_provenance"]["repositories"][0]["embedding_provenance"][
            "model_revision"
        ]
    elif damage == "revision_mismatch":
        documents["runtime_metadata"]["immutable_revision"] = "invented-revision"
    elif damage == "rehearsal":
        documents["workload"]["rehearsal"] = True
    elif damage == "incomplete":
        result["workflow_completed"] = False
    paths = {"allowance_ledger": ledger_path.relative_to(tmp_path).as_posix()}
    for role, document in documents.items():
        paths[role] = role + ".json"
        (tmp_path / paths[role]).write_text(json.dumps(document))
    result["artifacts"] = [
        {"role": role, "path": path, "sha256": digest_file(tmp_path / path)}
        for role, path in paths.items()
    ]
    if damage == "duplicate_artifact":
        result["artifacts"].append(result["artifacts"][0])
    (tmp_path / "live.json").write_text(json.dumps(result))
    before = {path: (tmp_path / path).read_bytes() for path in paths.values()}
    if damage:
        with pytest.raises(PilotRefused):
            verify_saved_receipt(tmp_path, manifest, "live", expected_approval=ledger.approval)
    else:
        verify_saved_receipt(tmp_path, manifest, "live", expected_approval=ledger.approval)
        from scripts.v13_pilot_budget import RENEWED_APPROVAL

        if ledger.approval == RENEWED_APPROVAL:
            with pytest.raises(PilotRefused):
                verify_saved_receipt(tmp_path, manifest, "live")
    assert before == {path: (tmp_path / path).read_bytes() for path in paths.values()}


@pytest.mark.parametrize("live_records", ["original"], indirect=True)
@pytest.mark.parametrize("damage", [None, "binding", "not_rehearsal", "incomplete", "artifact"])
def test_saved_rehearsal_requires_actual_bound_records(tmp_path, manifest, live_records, damage):
    from scripts.v13_pmcp_pilot import digest_file

    result, ledger, documents = live_records
    result["rehearsal"] = documents["workload"]["rehearsal"] = True
    documents["runtime_metadata"]["workload_sha256"] = digest_json(documents["workload"])
    paths = {"allowance_ledger": (ledger.root / "ledger.sqlite").relative_to(tmp_path).as_posix()}
    for role, document in documents.items():
        paths[role] = role + ".json"
        (tmp_path / paths[role]).write_text(json.dumps(document))
    result["artifacts"] = [
        {"role": role, "path": path, "sha256": digest_file(tmp_path / path)}
        for role, path in paths.items()
    ]
    if damage == "binding":
        result["manifest_sha256"] = "0" * 64
    elif damage == "not_rehearsal":
        result["rehearsal"] = False
    elif damage == "incomplete":
        result["workflow_completed"] = False
    elif damage == "artifact":
        (tmp_path / paths["workload"]).write_text("{}")
    (tmp_path / "rehearsal.json").write_text(json.dumps(result))
    if damage:
        with pytest.raises(PilotRefused):
            verify_saved_receipt(tmp_path, manifest, "rehearsal")
    else:
        verify_saved_receipt(tmp_path, manifest, "rehearsal")


@pytest.mark.asyncio
async def test_runtime_provenance_counts_points_separately_from_mappings(tmp_path, httpx_mock):
    import hashlib
    import sqlite3

    from scripts.v13_pmcp_pilot import runtime_provenance

    repo = tmp_path / "repos/ledger"
    repo.mkdir(parents=True)
    (repo / "bookkeeping.py").write_text("pass\n")
    database = tmp_path / "generation.db"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE semantic_points(point_id INTEGER, collection TEXT)")
        db.executemany("INSERT INTO semantic_points VALUES (?,?)", [(1, "fixture"), (1, "fixture")])
    info = {
        "name": "ledger",
        "index_path": str(database),
        "index_generation": "generation",
        "last_indexed_commit": "a" * 40,
    }
    (tmp_path / "registry.json").write_text(json.dumps({"repo": info}))
    (tmp_path / "runtime-metadata.json").write_text(
        json.dumps({"models": {"embedding": "model"}, "dimension": 8})
    )
    metadata_dir = database.with_suffix(".semantic")
    metadata_dir.mkdir()
    (metadata_dir / ".index_metadata.json").write_text(
        json.dumps(
            {
                "semantic_profiles": {
                    "pilot": {
                        "collection_name": "fixture",
                        "attested": True,
                        "provenance": {
                            "served_model_id": {"source": "reported", "value": "model"},
                            "dimension": {"source": "reported", "value": 8},
                        },
                    }
                }
            }
        )
    )
    sentinel = {
        "__provenance__": True,
        "indexed_commit": "a" * 40,
        "point_set_id": hashlib.sha256(b"1").hexdigest(),
        "corpus_sha256": hashlib.sha256(b"bookkeeping.py").hexdigest(),
        "profile_fingerprint": "profile",
    }
    httpx_mock.add_response(
        method="POST",
        url="http://127.0.0.1:1/collections/fixture/points/scroll",
        json={
            "result": {
                "points": [{"id": "sentinel", "payload": sentinel}, {"id": 1, "payload": {}}]
            }
        },
    )
    records = await runtime_provenance({"root": tmp_path}, "http://127.0.0.1:1")
    assert records[0]["point_count"] == 1
    assert records[0]["mapping_count"] == 2
    assert records[0]["point_ids"] == ["1"]
