"""Offline synthetic workload and request envelopes for the bounded v13 pilot."""

from __future__ import annotations

import json
from pathlib import Path

from chunker import chunk_text

SYNTHETIC_CORPUS = {
    "ledger": {
        "balance.py": "def available_balance(credits, debits):\n    return sum(credits) - sum(debits)\n",
    },
    "catalog": {
        "catalog.py": "def find_product(products, code):\n    return next((p for p in products if p['code'] == code), None)\n",
    },
}

# PILOT must enforce these bounds against serialized input before each request.
# Retries consume another slot; these are budgets, not measured inference costs.
REQUEST_ENVELOPES = {
    "summary": {"requests": 4, "max_input_utf8_bytes": 8192, "framing_input_units": 128},
    "document_embedding": {"requests": 6, "max_input_utf8_bytes": 4096, "framing_input_units": 32},
    "query_embedding": {"requests": 40, "max_input_utf8_bytes": 512, "framing_input_units": 32},
    "provenance_probe": {"requests": 12, "max_input_utf8_bytes": 128, "framing_input_units": 33},
}


def estimate() -> dict:
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / "docs/contracts/v13-freeze.json").read_text()
    )["policies"]["pilot"]
    files = [
        (f"{repo}/{path}", content)
        for repo, entries in SYNTHETIC_CORPUS.items()
        for path, content in entries.items()
    ]
    chunks = sum(len(chunk_text(content, "python", path)) for path, content in files)
    token_bound = sum(
        item["requests"] * (item["max_input_utf8_bytes"] + item["framing_input_units"])
        for item in REQUEST_ENVELOPES.values()
    )
    return {
        "schema": "v13-pilot-estimate.v1",
        "synthetic_only": True,
        "inference_requests_made": 0,
        "measured_quality_or_performance": False,
        "repositories": len(SYNTHETIC_CORPUS),
        "files": len(files),
        "source_utf8_bytes": sum(len(content.encode("utf-8")) for _, content in files),
        "source_chunks": chunks,
        "request_envelopes": REQUEST_ENVELOPES,
        "input_token_upper_bound": token_bound,
        "approved_input_token_limit": policy["input_token_limit"],
        "remaining_envelope_tokens": policy["input_token_limit"] - token_bound,
        "approved_inference_seconds_limit": policy["local_inference_seconds_limit"],
        "within_approved_token_budget": token_bound <= policy["input_token_limit"],
        "admission": "PILOT must count serialized UTF-8 input bytes, retries and elapsed time before every request",
    }


if __name__ == "__main__":
    result = estimate()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["within_approved_token_budget"] else 1)
