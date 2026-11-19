"""Compatibility tests for TreeSitter Chunker adapter behavior."""

try:
    from chunker import chunk_text
except ImportError:
    chunk_text = None  # type: ignore[assignment]

from mcp_server.utils.chunker_adapter import get_adapter


def test_chunker_adapter_extracts_symbol_names_from_latest_chunker_metadata():
    chunks = chunk_text(
        "def hello(name):\n    return f'hi {name}'\n\nclass Greeter:\n    pass\n",
        "python",
    )

    adapter = get_adapter()
    symbols = [adapter.chunk_to_symbol_dict(chunk) for chunk in chunks]

    assert symbols[0]["symbol"] == "hello"
    assert symbols[0]["kind"] == "function"
    assert symbols[0]["signature"] == "hello(name)"
    assert symbols[1]["symbol"] == "Greeter"
    assert symbols[1]["kind"] == "class"
