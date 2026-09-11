"""Receipt reduction must not promote incomplete installed or live evidence."""

import copy

import pytest

from scripts.v13_pmcp_pilot import GOALS, PilotRefused, digest_json, validate_receipt


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
