"""Real-v6 functional contract test for treesitter-chunker (CHUNKERSAFE Lane B).

Pins the treesitter-chunker 6.x surface the Code-Index-MCP callers depend on, so
a future 6.x change that moves an import, renames a ``CodeChunk`` field, or
reshapes chunk metadata fails here instead of silently degrading indexing.

It also pins the 6.0 identity behavior the index relies on
(Consiliency/Code-Index-MCP#115): LF Python chunk ids are unchanged from 4.x,
CRLF content and byte spans are raw, erroneous keyword-only ``lambda`` fragments
are excluded, and the CHUNKERSAFE scheme moves to ``treesitter_chunk_id_v6`` so a
4.x-built index is rebuilt in full instead of mixed.
"""

from __future__ import annotations

import pytest

chunker = pytest.importorskip("chunker")


def _chunker_major() -> int:
    try:
        from importlib.metadata import version

        return int(version("treesitter-chunker").split(".")[0])
    except Exception:  # pragma: no cover - defensive
        return 0


pytestmark = pytest.mark.skipif(
    _chunker_major() != 6,
    reason="real-v6 contract test requires treesitter-chunker 6.x",
)

# The exact call sites the dispatcher/plugins/graph use.
from chunker import CodeChunk, chunk_text  # noqa: E402

_PY_SRC = (
    "def foo(x):\n"
    "    '''Docstring.'''\n"
    "    return x + 1\n"
    "\n"
    "class Bar:\n"
    "    def baz(self):\n"
    "        return 2\n"
)


def test_chunk_text_returns_nonempty_chunks():
    chunks = chunk_text(_PY_SRC, "python")
    assert chunks, "v6 chunk_text produced no chunks"
    assert all(isinstance(c, CodeChunk) for c in chunks)


def test_codechunk_attribute_contract():
    """Every attribute the dispatcher (chunk.__dict__) and plugins read must exist."""
    chunk = chunk_text(_PY_SRC, "python")[0]
    # dispatcher_enhanced.py persists chunk.__dict__
    assert hasattr(chunk, "__dict__")
    for attr in (
        "chunk_id",
        "node_id",
        "definition_id",
        "parent_chunk_id",
        "byte_start",
        "byte_end",
        "start_line",
        "end_line",
        "node_type",
        "metadata",
    ):
        assert hasattr(chunk, attr), f"v6 CodeChunk lost attribute {attr!r}"
    assert isinstance(chunk.chunk_id, str) and chunk.chunk_id
    assert isinstance(chunk.byte_start, int) and isinstance(chunk.byte_end, int)
    assert isinstance(chunk.metadata, dict)


def test_v6_metadata_shape_is_adapter_compatible():
    """v4+ nests the symbol name under metadata['signature']['name']; the
    version-tolerant chunker_adapter must still recover a symbol name + signature."""
    from mcp_server.utils.chunker_adapter import ChunkerAdapter

    adapter = ChunkerAdapter()
    func_chunk = next(
        c for c in chunk_text(_PY_SRC, "python") if c.node_type == "function_definition"
    )
    name = adapter._get_symbol_name(func_chunk)
    assert name == "foo", f"adapter failed to recover v6 symbol name (got {name!r})"
    sig = adapter._get_signature(func_chunk)
    assert sig, "adapter recovered no signature from v6 metadata"


def test_graph_imports_resolve_and_are_callable():
    """xref/graph-cut imports (used by the graph features) still resolve under v6."""
    from chunker.core import chunk_file  # noqa: F401
    from chunker.graph.cut import graph_cut
    from chunker.graph.xref import build_xref

    assert callable(build_xref)
    assert callable(graph_cut)


def test_lane_a_guard_derives_v6_scheme():
    """The CHUNKERSAFE identity-scheme guard must recognize v6 as a distinct
    scheme, so a v4-built index (marker ``treesitter_chunk_id_v4``) trips the
    guard and is rebuilt in full rather than mixed with 6.0 ids."""
    from mcp_server.storage.sqlite_store import current_chunk_id_scheme

    scheme = current_chunk_id_scheme()
    assert scheme == "treesitter_chunk_id_v6", scheme
    assert scheme not in {"treesitter_chunk_id_v1", "treesitter_chunk_id_v4"}


def test_v4_artifact_manifest_is_rejected_under_v6():
    """A published 4.x artifact must not be imported into a 6.x runtime."""
    from mcp_server.artifacts.manifest_v2 import ArtifactManifestV2, ManifestUnit

    def _manifest(algorithm: str) -> ArtifactManifestV2:
        return ArtifactManifestV2(
            logical_artifact_id="repo-main-abc123",
            repo_id="owner/repo",
            branch="main",
            tracked_branch="main",
            commit="0123456789abcdef",
            schema_version="2",
            semantic_profile_hash="lexical-only",
            checksum="0" * 64,
            artifact_type="full",
            chunk_schema_version="2.0",
            chunk_identity_algorithm=algorithm,
            units=[
                ManifestUnit(
                    unit_type="lexical",
                    unit_id="lexical-main-abc123",
                    checksum="0" * 64,
                    size_bytes=1,
                )
            ],
        )

    _manifest("treesitter_chunk_id_v6").validate()
    with pytest.raises(ValueError, match="Unsupported chunk identity algorithm"):
        _manifest("treesitter_chunk_id_v4").validate()


# --------------------------------------------------------------------------- #
# 6.0 identity pins (Consiliency/Code-Index-MCP#115)
# --------------------------------------------------------------------------- #
_LF_IDS = [
    "fa118e0fb54baf664bb5cc7feaca8c74a760e04a",
    "5bdb920ace9714c04d0d689a759ea9a93be89a25",
    "ae8459c1afac4a43e7f5d8130ba12841320c3093",
]
_PY_LF = "def foo(x):\n    return x + 1\n\nclass Bar:\n    def baz(self):\n        return 2\n"


def test_lf_python_chunk_ids_are_unchanged_from_v4():
    """6.0 keeps LF identities; these exact ids were produced by chunker 4.0.0."""
    assert [c.chunk_id for c in chunk_text(_PY_LF, "python")] == _LF_IDS


def test_chunk_text_crlf_content_and_spans_are_raw():
    crlf = _PY_LF.replace("\n", "\r\n")
    raw = crlf.encode("utf-8")
    chunks = chunk_text(crlf, "python")
    assert [c.node_type for c in chunks] == [
        "function_definition",
        "class_definition",
        "function_definition",
    ]
    for chunk in chunks:
        assert "\r\n" in chunk.content
        assert raw[chunk.byte_start : chunk.byte_end].decode("utf-8") == chunk.content
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 2), (4, 6), (5, 6)]
    # CRLF bytes are part of the identity, so CRLF and LF ids differ.
    assert set(c.chunk_id for c in chunks).isdisjoint(_LF_IDS)


def test_chunk_file_preserves_crlf_bytes_and_spans(tmp_path):
    """6.0 change (treesitter-chunker#464): chunk_file no longer normalizes CRLF."""
    from chunker import chunk_file

    target = tmp_path / "m.py"
    crlf = _PY_LF.replace("\n", "\r\n")
    target.write_bytes(crlf.encode("utf-8"))
    crlf_chunks = chunk_file(str(target), "python")
    raw = target.read_bytes()
    for chunk in crlf_chunks:
        assert "\r\n" in chunk.content
        assert raw[chunk.byte_start : chunk.byte_end].decode("utf-8") == chunk.content

    target.write_bytes(_PY_LF.encode("utf-8"))
    lf_ids = {c.chunk_id for c in chunk_file(str(target), "python")}
    assert lf_ids.isdisjoint({c.chunk_id for c in crlf_chunks})


def test_indexer_text_read_normalizes_crlf_to_lf_identities(tmp_path):
    """The dispatcher/plugins read sources with ``Path.read_text`` (universal
    newlines), so a CRLF checkout indexes with the same ids as LF. Persisted
    ``code_chunks`` therefore do not churn with the 6.0 CRLF change."""
    target = tmp_path / "m.py"
    target.write_bytes(_PY_LF.replace("\n", "\r\n").encode("utf-8"))
    content = target.read_text(encoding="utf-8")
    assert [c.chunk_id for c in chunk_text(content, "python")] == _LF_IDS


def test_erroneous_keyword_only_lambda_fragments_are_excluded():
    """6.0 change (treesitter-chunker#446): bare ``lambda`` error fragments are
    no longer emitted; complete lambdas keep their 4.x occurrence ids."""
    chunks = chunk_text("f = lambda *, a: a\ng = lambda x: x\n", "python")
    assert [(c.chunk_id, c.content) for c in chunks] == [
        ("e15fe45bd78b32b8b2e0db53dda1c5e751568602", "lambda *, a: a"),
        ("1babbd3c77238669522f8a6b3ff9386747be2bb6", "lambda x: x"),
    ]


def test_semantic_profile_fingerprint_moves_with_chunker_version():
    """Semantic profiles fingerprint the installed chunker version, so vectors
    and summaries recorded under 4.x read as profile-incompatible under 6.x."""
    from mcp_server.artifacts.semantic_profiles import SemanticProfile
    from mcp_server.config.settings import Settings

    label = Settings()._detect_treesitter_chunker_version()
    assert label.startswith("treesitter-chunker@6."), label

    base = {
        "provider": "voyage",
        "model_name": "voyage-code-3",
        "model_version": "1",
        "vector_dimension": 1024,
        "distance_metric": "cosine",
        "normalization_policy": "l2",
        "chunk_schema_version": "2.0",
    }
    v4 = SemanticProfile.from_dict("p", {**base, "chunker_version": "treesitter-chunker@4.0.0"})
    v6 = SemanticProfile.from_dict("p", {**base, "chunker_version": label})
    assert v4.compatibility_fingerprint != v6.compatibility_fingerprint
