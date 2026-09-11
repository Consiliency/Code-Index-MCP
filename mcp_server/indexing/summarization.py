import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, cast

import mcp.types as types

from ..setup.semantic_preflight import EnrichmentModelResolution, resolve_enrichment_model
from ..storage.sqlite_store import SQLiteStore, assert_chunk_scheme_readable

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a concise code documentation assistant. "
    "Respond with exactly 2 sentences summarizing the code chunk: "
    "what it does, its key inputs/parameters, and what it returns or produces."
)

_USER_PROMPT_TEMPLATE = (
    "Describe this {language} code chunk in 2 concise sentences. "
    "Cover: what it does, its inputs/parameters, and what it returns or produces. "
    "Do not repeat the code. Do not output code.\n\n"
    "{parent_context}"
    "{file_context}"
    "Code chunk '{symbol}' (lines {start}-{end}):\n```{language}\n{content}\n```"
)

# Max characters of the surrounding file to include as context (~100k tokens ≈ 400k chars,
# but we keep it conservative to leave room for the chunk itself and the response.
_MAX_FILE_CONTEXT_CHARS = 60_000
_DOC_FILE_CONTEXT_CHARS = 8_000
_DOC_CONTEXT_LANGUAGES = {"markdown", "plaintext", "text", "rst"}
_DOC_PROCESS_SCOPE_CHUNK_LIMIT = 16
_REPO_SCOPE_DOC_PROCESS_SCOPE_CHUNK_LIMIT = 4
_REPO_SCOPE_DOC_FILE_CHUNK_WINDOW = 1
_REPO_SCOPE_FILE_LIMIT = 1
_DOC_TOPOLOGICAL_RECOVERY_CHUNK_LIMIT = 4
_SUMMARY_CALL_CANCEL_SETTLE_SECONDS = 1.0

# Files larger than this (in characters) skip the single-batch API call and fall back
# to the topological per-chunk path.
_BATCH_FILE_SIZE_THRESHOLD = 400_000
_PROFILE_BATCH_CHUNK_COUNT = 64


class FileTooLargeError(Exception):
    """Raised when a file exceeds the batch summarization size limit."""


@dataclass(frozen=True)
class GeneratedSummary:
    """Immutable normalized summary generated for one chunk."""

    chunk_id: str
    summary: str


def _normalize_generated_summaries(items: Sequence[Any]) -> Tuple[GeneratedSummary, ...]:
    normalized = []
    for item in items:
        if isinstance(item, GeneratedSummary):
            normalized.append(item)
            continue
        chunk_id = getattr(item, "chunk_id", None)
        summary = getattr(item, "summary", None)
        if not isinstance(chunk_id, str) or not isinstance(summary, str):
            raise TypeError("Generated summaries require string chunk_id and summary fields")
        normalized.append(GeneratedSummary(chunk_id=chunk_id, summary=summary))
    return tuple(normalized)


def _symbol_name(
    symbol_map: Dict[int, str],
    chunk: Dict[str, Any],
    fallback: str = "unknown",
) -> str:
    symbol_id = chunk.get("symbol_id")
    mapped = symbol_map.get(symbol_id) if isinstance(symbol_id, int) else None
    return mapped or str(chunk.get("node_type") or fallback)


@dataclass(frozen=True)
class SummaryGenerationResult:
    """Structured result for authoritative summary generation."""

    chunks_attempted: int = 0
    summaries_written: int = 0
    authoritative_chunks: int = 0
    missing_chunk_ids: Tuple[str, ...] = field(default_factory=tuple)
    files_attempted: int = 0
    files_summarized: int = 0
    batches_processed: int = 0
    remaining_chunks: int = 0
    scope_drained: bool = True
    blocked_call_reason: Optional[str] = None
    blocked_call_file_path: Optional[str] = None
    blocked_call_chunk_ids: Tuple[str, ...] = field(default_factory=tuple)
    blocked_call_timeout_seconds: Optional[float] = None
    cancelled: bool = False
    summaries: Tuple[GeneratedSummary, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing_chunk_ids", tuple(self.missing_chunk_ids))
        object.__setattr__(self, "blocked_call_chunk_ids", tuple(self.blocked_call_chunk_ids))
        object.__setattr__(self, "summaries", _normalize_generated_summaries(self.summaries))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunks_attempted": self.chunks_attempted,
            "summaries_written": self.summaries_written,
            "authoritative_chunks": self.authoritative_chunks,
            "missing_chunk_ids": list(self.missing_chunk_ids),
            "files_attempted": self.files_attempted,
            "files_summarized": self.files_summarized,
            "batches_processed": self.batches_processed,
            "remaining_chunks": self.remaining_chunks,
            "scope_drained": self.scope_drained,
            "blocked_call_reason": self.blocked_call_reason,
            "blocked_call_file_path": self.blocked_call_file_path,
            "blocked_call_chunk_ids": list(self.blocked_call_chunk_ids),
            "blocked_call_timeout_seconds": self.blocked_call_timeout_seconds,
            "cancelled": self.cancelled,
            "summaries": [
                {"chunk_id": item.chunk_id, "summary": item.summary} for item in self.summaries
            ],
        }


def _topological_order(chunks: List[Dict]) -> List[str]:
    """Return chunk_ids in topological order — leaves (innermost scopes) first.

    Kahn's algorithm over the parent→child relationship.  Any cycles (rare in
    practice) are appended at the end in their original order.
    """
    chunk_map = {c["chunk_id"]: c for c in chunks}
    children: Dict[str, List[str]] = {cid: [] for cid in chunk_map}
    parent_of: Dict[str, Optional[str]] = {}
    for c in chunks:
        cid = c["chunk_id"]
        pid = c.get("parent_chunk_id")
        parent_of[cid] = pid
        if pid and pid in children:
            children[pid].append(cid)

    in_degree = {
        cid: (1 if parent_of.get(cid) and parent_of[cid] in chunk_map else 0) for cid in chunk_map
    }
    queue: List[str] = [cid for cid, deg in in_degree.items() if deg == 0]
    order: List[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for child in children.get(node, []):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    seen = set(order)
    order.extend(cid for cid in chunk_map if cid not in seen)
    return order


class ChunkWriter:
    """Base interface for semantic chunk summarization.

    Summarization is attempted via three paths in order:
    1. MCP ``sampling/createMessage`` — if the connected client declares
       sampling capability (e.g. Cursor).
    2. BAML ``SummarizeChunkAlone`` — if ``CEREBRAS_API_KEY`` is set.
    3. Direct LLM API — if ``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY`` is
       set in the environment (works in Claude Code which lacks sampling).
    """

    def __init__(
        self,
        db_path: str,
        qdrant_client: Any,
        session: Any = None,
        client_name: Optional[str] = None,
        summarization_config: Optional[Dict[str, Any]] = None,
    ):
        self.db_path = db_path
        self.qdrant_client = qdrant_client
        self.session = session
        self.client_name = client_name
        self.summarization_config = summarization_config or {}
        self._sqlite_store: Optional[SQLiteStore] = None
        self._profile_model_resolution: Optional[EnrichmentModelResolution] = None

    def _get_sqlite_store(self) -> SQLiteStore:
        if self._sqlite_store is None:
            self._sqlite_store = SQLiteStore(self.db_path)
        return self._sqlite_store

    def _prompt_fingerprint(self) -> str:
        payload = "\n".join(
            [
                _SYSTEM_PROMPT,
                _USER_PROMPT_TEMPLATE,
                str(self.summarization_config.get("base_url", "")),
                str(self.summarization_config.get("model_name", "")),
                str(self.summarization_config.get("profile_id", "")),
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _resolve_effective_profile_model(self) -> Optional[EnrichmentModelResolution]:
        if not self.summarization_config.get("base_url"):
            return None
        if self._profile_model_resolution is None:
            cfg = self.summarization_config
            self._profile_model_resolution = resolve_enrichment_model(
                base_url=str(cfg["base_url"]),
                configured_model=str(cfg.get("model_name", "gpt-4o-mini")),
                api_key_env=str(cfg.get("api_key_env", "OPENAI_API_KEY")),
                timeout_s=float(cfg.get("timeout_s", 10.0)),
            )
        return self._profile_model_resolution

    def _build_summary_audit_metadata(self, *, model_name: str) -> Dict[str, Any]:
        base_url = self.summarization_config.get("base_url")
        if base_url:
            provider_name = "openai_compatible"
        elif os.environ.get("CEREBRAS_API_KEY"):
            provider_name = "cerebras"
        elif os.environ.get("ANTHROPIC_API_KEY"):
            provider_name = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            provider_name = "openai"
        else:
            provider_name = "mcp_sampling"

        audit_metadata = {
            "provider_name": provider_name,
            "llm_model": model_name,
            "profile_id": self.summarization_config.get("profile_id"),
            "base_url": base_url,
            "prompt_fingerprint": self._prompt_fingerprint(),
        }
        resolution = self._resolve_effective_profile_model()
        if resolution is not None:
            audit_metadata["configured_model_name"] = resolution.configured_model
            audit_metadata["effective_model_name"] = resolution.effective_model
            audit_metadata["model_resolution_strategy"] = resolution.resolution_strategy
            audit_metadata["considered_model_ids"] = list(resolution.available_models)
        return audit_metadata

    def _persist_summary(
        self,
        *,
        chunk_hash: str,
        file_id: int,
        chunk_start: int,
        chunk_end: int,
        symbol: Optional[str],
        summary_text: str,
        model_name: str,
        is_authoritative: bool,
    ) -> bool:
        audit_metadata = self._build_summary_audit_metadata(model_name=model_name)
        return bool(
            self._get_sqlite_store().store_chunk_summary(
                chunk_hash=chunk_hash,
                file_id=file_id,
                chunk_start=chunk_start,
                chunk_end=chunk_end,
                summary_text=summary_text,
                llm_model=model_name,
                symbol=symbol,
                is_authoritative=is_authoritative,
                provider_name=str(audit_metadata["provider_name"]),
                profile_id=(
                    str(audit_metadata["profile_id"]) if audit_metadata.get("profile_id") else None
                ),
                prompt_fingerprint=str(audit_metadata["prompt_fingerprint"]),
                audit_metadata=audit_metadata,
            )
        )

    def _has_sampling_capability(self) -> bool:
        """Return True if the connected MCP client supports sampling/createMessage."""
        if self.session is None:
            return False
        try:
            params = getattr(self.session, "client_params", None)
            caps = params.capabilities if params else None
            return caps is not None and caps.sampling is not None
        except Exception:
            return False

    def _has_direct_api(self) -> bool:
        """Return True if any direct LLM API key or profile endpoint is configured."""
        if self.summarization_config.get("base_url"):
            return True
        return bool(
            os.environ.get("CEREBRAS_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )

    def can_summarize(self) -> bool:
        """Return True if any summarization path is available."""
        return self._has_sampling_capability() or self._has_direct_api()

    async def _call_cerebras_api(self, system: str, prompt: str) -> str:
        """Call Cerebras inference API via the openai SDK (OpenAI-compatible)."""
        from openai import AsyncOpenAI

        model = self.summarization_config.get("cerebras_model", "llama3.1-8b")
        client = AsyncOpenAI(
            api_key=os.environ["CEREBRAS_API_KEY"],
            base_url="https://api.cerebras.ai/v1",
        )
        response = await client.chat.completions.create(
            model=model,
            max_tokens=150,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        return str(response.choices[0].message.content or "")

    async def _call_anthropic_api(self, system: str, prompt: str) -> str:
        """Call Anthropic API directly via httpx (no SDK required)."""
        import httpx

        api_key = os.environ["ANTHROPIC_API_KEY"]
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": self.summarization_config.get(
                        "anthropic_model", "claude-haiku-4-5-20251001"
                    ),
                    "max_tokens": 150,
                    "system": system,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data["content"][0]["text"])

    async def _call_openai_api(self, system: str, prompt: str) -> str:
        """Call OpenAI API via the openai SDK."""
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
        response = await client.chat.completions.create(
            model=self.summarization_config.get("openai_model", "gpt-5.4-nano"),
            max_tokens=150,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        )
        return str(response.choices[0].message.content or "")

    async def _call_profile_api(
        self, system: str, prompt: str, *, max_tokens: int = 150
    ) -> tuple[str, str]:
        """Call the profile-configured OpenAI-compatible endpoint."""
        from openai import AsyncOpenAI

        cfg = self.summarization_config
        base_url = cfg["base_url"]
        resolution = self._resolve_effective_profile_model()
        model = (
            resolution.effective_model
            if resolution is not None
            else str(cfg.get("model_name", "gpt-4o-mini"))
        )
        api_key_env = cfg.get("api_key_env", "OPENAI_API_KEY")
        api_key = os.environ.get(api_key_env) or "vllm-local"
        request_timeout = max(10.0, float(cfg.get("timeout_s", 10.0)) * 3)
        client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=request_timeout)
        try:
            response = await client.chat.completions.create(
                model=model,
                max_tokens=max_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            )
            return str(response.choices[0].message.content or ""), model
        finally:
            await client.close()

    async def _call_direct_api(
        self, system: str, prompt: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Try profile endpoint first, then Cerebras, Anthropic, OpenAI."""
        if self.summarization_config.get("base_url"):
            try:
                return await self._call_profile_api(system, prompt)
            except Exception as exc:
                logger.warning(
                    "Profile summarization endpoint %s failed, falling back to env API: %s",
                    self.summarization_config["base_url"],
                    exc,
                )
        if os.environ.get("CEREBRAS_API_KEY"):
            return await self._call_cerebras_api(system, prompt), None
        elif os.environ.get("ANTHROPIC_API_KEY"):
            return await self._call_anthropic_api(system, prompt), None
        elif os.environ.get("OPENAI_API_KEY"):
            return await self._call_openai_api(system, prompt), None
        return None, None

    def _get_model_name(self) -> str:
        if self.summarization_config.get("model_name"):
            resolution = self._resolve_effective_profile_model()
            if resolution is not None:
                return str(resolution.effective_model)
            return str(self.summarization_config["model_name"])
        if os.environ.get("CEREBRAS_API_KEY"):
            return str(self.summarization_config.get("cerebras_model", "llama3.1-8b"))
        if os.environ.get("ANTHROPIC_API_KEY"):
            return str(
                self.summarization_config.get("anthropic_model", "claude-haiku-4-5-20251001")
            )
        return str(self.summarization_config.get("openai_model", "gpt-5.4-nano"))

    def _build_api_file_context(
        self, *, language: str, file_content: str, chunk_content: str
    ) -> str:
        if not file_content:
            return ""
        normalized_language = (language or "").lower()
        max_chars = _MAX_FILE_CONTEXT_CHARS
        if normalized_language in _DOC_CONTEXT_LANGUAGES:
            max_chars = _DOC_FILE_CONTEXT_CHARS
        if len(chunk_content) > max_chars // 2:
            return ""

        trimmed = file_content[:max_chars]
        if len(file_content) > max_chars:
            trimmed += "\n... [file truncated for context window]"
        return (
            f"Full source file ({language}):\n```{language}\n{trimmed}\n```\n\n"
            f"Now summarize only the following chunk from that file:\n\n"
        )

    async def summarize_chunk(
        self,
        chunk_hash: str,
        file_id: int,
        chunk_start: int,
        chunk_end: int,
        symbol: str,
        content: str,
        language: str = "unknown",
        parent_context: str = "",
        file_content: str = "",
        is_authoritative: bool = False,
    ) -> Optional[str]:
        """Generate a summary for a code chunk and persist it to SQLite.

        Tries MCP sampling first; then BAML ``SummarizeChunkAlone`` (Cerebras);
        then falls back to a direct LLM API call (Cerebras → Anthropic → OpenAI).

        *parent_context*: pre-computed summary of the enclosing scope, from
        the topological traversal — improves accuracy for nested symbols.

        *file_content*: the full source file text.  When provided, this is
        injected into the prompt so the model understands the chunk in its
        broader file context — especially valuable for cross-reference and
        import analysis.

        Returns the summary text on success, ``None`` otherwise.
        """
        if not self.can_summarize():
            logger.debug(
                "Skipping summary for '%s': no sampling capability and no API key",
                symbol,
            )
            return None

        # Skip if an authoritative summary already exists.
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT is_authoritative, summary_text FROM chunk_summaries WHERE chunk_hash = ?",
                (chunk_hash,),
            )
            row = cursor.fetchone()
            if row and row[0]:
                return str(row[1])

        # Prompt without file context — used by MCP sampling (tight token budget).
        sampling_prompt = _USER_PROMPT_TEMPLATE.format(
            language=language,
            parent_context=parent_context,
            file_context="",
            symbol=symbol,
            start=chunk_start,
            end=chunk_end,
            content=content,
        )

        # File context injected for direct API calls (large context window).
        api_file_context = self._build_api_file_context(
            language=language,
            file_content=file_content,
            chunk_content=content,
        )

        # Fallback manual prompt for non-BAML direct API paths.
        api_prompt = _USER_PROMPT_TEMPLATE.format(
            language=language,
            parent_context=parent_context,
            file_context=api_file_context,
            symbol=symbol,
            start=chunk_start,
            end=chunk_end,
            content=content,
        )

        summary_text: Optional[str] = None
        model_name: Optional[str] = None

        # Path 1: MCP sampling (client performs the inference, no file context)
        if self._has_sampling_capability():
            try:
                result = await self.session.create_message(
                    messages=[
                        types.SamplingMessage(
                            role="user",
                            content=types.TextContent(type="text", text=sampling_prompt),
                        )
                    ],
                    system_prompt=_SYSTEM_PROMPT,
                    max_tokens=150,
                )
                content_block = result.content
                if hasattr(content_block, "text"):
                    summary_text = content_block.text
                elif isinstance(content_block, list) and content_block:
                    first = content_block[0]
                    summary_text = getattr(first, "text", str(first))
                else:
                    summary_text = str(content_block)
                model_name = result.model or "mcp-sampling"
            except Exception as exc:
                logger.warning("MCP sampling failed for chunk '%s': %s", symbol, exc)

        # Path 2: BAML SummarizeChunkAlone (Cerebras, cache-friendly prompt structure)
        if summary_text is None and os.environ.get("CEREBRAS_API_KEY"):
            try:
                from mcp_server.indexing.baml_client.baml_client.async_client import b

                result = await b.SummarizeChunkAlone(
                    language=language,
                    symbol=symbol or "unknown",
                    line_start=chunk_start,
                    line_end=chunk_end,
                    content=content or "",
                    parent_context=parent_context or "",
                    file_context=api_file_context,
                )
                summary_text = result.summary
                model_name = self._get_model_name()
            except Exception as exc:
                logger.warning(
                    "BAML SummarizeChunkAlone failed for '%s': %s, falling back to direct API",
                    symbol,
                    exc,
                )

        # Path 3: Direct API fallback (Anthropic / OpenAI raw call, or Cerebras raw)
        if summary_text is None and self._has_direct_api():
            try:
                summary_text, resolved_model_name = await self._call_direct_api(
                    _SYSTEM_PROMPT, api_prompt
                )
                model_name = resolved_model_name or self._get_model_name()
            except Exception as exc:
                logger.warning("Direct API summarization failed for '%s': %s", symbol, exc)

        if summary_text is None:
            return None

        self._persist_summary(
            chunk_hash=chunk_hash,
            file_id=file_id,
            chunk_start=chunk_start,
            chunk_end=chunk_end,
            symbol=symbol,
            summary_text=summary_text,
            model_name=model_name or "unknown",
            is_authoritative=is_authoritative,
        )
        logger.info("Stored summary for chunk '%s' via %s", symbol, model_name)
        return summary_text


class FileBatchSummarizer(ChunkWriter):
    """Summarizes all chunks of a file in a single BAML API call.

    For files that exceed ``_BATCH_FILE_SIZE_THRESHOLD`` characters the batch
    call first falls back to bounded profile-backed chunk windows when that
    recovery path is configured, and only then to the topological per-chunk
    path (same Kahn's-sort logic as ``ComprehensiveChunkWriter``, but
    operating on ``Dict`` rows rather than raw SQL tuples).
    """

    def _repo_scope_call_timeout_seconds(self) -> float:
        raw_timeout = self.summarization_config.get("call_timeout_s")
        if raw_timeout is None:
            raw_timeout = self.summarization_config.get("timeout_s", 10.0)
        try:
            timeout_seconds = float(raw_timeout)
        except (TypeError, ValueError):
            timeout_seconds = 10.0
        return max(15.0, timeout_seconds * 3)

    def _blocked_call_result(
        self,
        *,
        reason: str,
        file_path: str,
        selected_chunks: List[Dict[str, Any]],
        remaining_chunks: List[Dict[str, Any]],
        existing_authoritative: int,
        timeout_seconds: Optional[float] = None,
    ) -> SummaryGenerationResult:
        return SummaryGenerationResult(
            chunks_attempted=len(selected_chunks),
            summaries_written=0,
            authoritative_chunks=existing_authoritative,
            missing_chunk_ids=tuple(chunk["chunk_id"] for chunk in remaining_chunks),
            files_attempted=1,
            files_summarized=0,
            remaining_chunks=len(remaining_chunks),
            scope_drained=False,
            blocked_call_reason=reason,
            blocked_call_file_path=file_path,
            blocked_call_chunk_ids=tuple(chunk["chunk_id"] for chunk in selected_chunks),
            blocked_call_timeout_seconds=timeout_seconds,
        )

    async def _await_summary_call_with_timeout(
        self,
        summary_call: Any,
        *,
        timeout_seconds: float,
    ) -> Sequence[Any]:
        """Bound the summary call and stop waiting once cancellation has been requested."""
        task = asyncio.create_task(summary_call)
        done, _pending = await asyncio.wait({task}, timeout=timeout_seconds)
        if done:
            return cast(Sequence[Any], await task)

        task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True),
                timeout=_SUMMARY_CALL_CANCEL_SETTLE_SECONDS,
            )
        except TimeoutError:
            logger.warning(
                "Timed-out summary call did not settle within %.1fs after cancellation",
                _SUMMARY_CALL_CANCEL_SETTLE_SECONDS,
            )
        raise TimeoutError

    async def _recover_with_profile_or_topological(
        self,
        *,
        file_id: int,
        file_path: str,
        file_content: str,
        chunks: List[Dict],
        symbol_map: Dict[int, str],
        warning_prefix: str,
        error: Exception,
        retry_profile_batch: bool = True,
    ) -> Tuple[GeneratedSummary, ...]:
        logger.warning(
            "%s for %s (%s), falling back to per-chunk path", warning_prefix, file_path, error
        )
        if retry_profile_batch and self.summarization_config.get("base_url"):
            try:
                return _normalize_generated_summaries(
                    await self._call_profile_batch_api(
                        file_id, file_path, file_content, chunks, symbol_map
                    )
                )
            except Exception as profile_exc:
                logger.warning(
                    "Profile batch fallback failed for %s (%s), falling back to per-chunk path",
                    file_path,
                    profile_exc,
                )
        return await self._summarize_topological(
            file_id,
            file_path,
            file_content,
            chunks,
            symbol_map,
            max_chunks=self._topological_recovery_chunk_limit(
                (chunks[0].get("language") or "unknown") if chunks else "unknown"
            ),
        )

    def _topological_recovery_chunk_limit(self, language: str) -> Optional[int]:
        normalized_language = (language or "").lower()
        if normalized_language in _DOC_CONTEXT_LANGUAGES:
            return _DOC_TOPOLOGICAL_RECOVERY_CHUNK_LIMIT
        return None

    def _parse_profile_batch_response(self, response_text: str) -> List[SimpleNamespace]:
        text = response_text.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if len(lines) >= 3:
                text = "\n".join(lines[1:-1]).strip()

        try:
            payload = json.loads(text)
            summaries = payload.get("summaries")
            if not isinstance(summaries, list):
                raise ValueError("Profile batch response did not include a summaries list")
        except Exception:
            pair_pattern = re.compile(
                r'"chunk_id"\s*:\s*"(?P<chunk_id>[^"]+)"\s*,\s*"summary"\s*:\s*"(?P<summary>(?:[^"\\]|\\.)*)"',
                re.DOTALL,
            )
            fallback_parsed = []
            for match in pair_pattern.finditer(text):
                summary = json.loads(f'"{match.group("summary")}"')
                fallback_parsed.append(
                    SimpleNamespace(
                        chunk_id=match.group("chunk_id"),
                        summary=summary.strip(),
                    )
                )
            if fallback_parsed:
                return fallback_parsed
            raise

        parsed: List[SimpleNamespace] = []
        for item in summaries:
            if not isinstance(item, dict):
                continue
            chunk_id = item.get("chunk_id")
            summary = item.get("summary")
            if isinstance(chunk_id, str) and isinstance(summary, str):
                parsed.append(SimpleNamespace(chunk_id=chunk_id, summary=summary.strip()))
        return parsed

    async def _call_profile_batch_api(
        self,
        file_id: int,
        file_path: str,
        file_content: str,
        chunks: List[Dict],
        symbol_map: Dict[int, str],
    ) -> List[Any]:
        del file_id, file_content
        language = (chunks[0].get("language") or "unknown") if chunks else "unknown"
        summaries: List[SimpleNamespace] = []

        for offset in range(0, len(chunks), _PROFILE_BATCH_CHUNK_COUNT):
            batch = chunks[offset : offset + _PROFILE_BATCH_CHUNK_COUNT]
            chunk_blocks = []
            for chunk in batch:
                symbol = _symbol_name(symbol_map, chunk)
                chunk_blocks.append(
                    "\n".join(
                        [
                            f"chunk_id: {chunk['chunk_id']}",
                            f"symbol: {symbol}",
                            f"lines: {chunk.get('line_start') or 1}-{chunk.get('line_end') or 1}",
                            f"content:\n```{language}\n{chunk.get('content') or ''}\n```",
                        ]
                    )
                )

            system = (
                "You summarize code/document chunks. "
                "Return strict JSON with shape "
                '{"summaries":[{"chunk_id":"...","summary":"..."}]}. '
                "Every summary must be exactly 2 concise sentences."
            )
            prompt = (
                f"File: {file_path}\n"
                f"Language: {language}\n"
                f"Summarize each chunk below and include every chunk_id exactly once.\n\n"
                + "\n\n".join(chunk_blocks)
            )
            response_text, _model_name = await self._call_profile_api(
                system,
                prompt,
                max_tokens=min(8000, max(600, len(batch) * 80)),
            )
            summaries.extend(self._parse_profile_batch_response(response_text))

        return summaries

    async def _call_batch_api(
        self,
        file_id: int,
        file_path: str,
        file_content: str,
        chunks: List[Dict],
        symbol_map: Dict[int, str],
    ) -> Sequence[Any]:
        """Call BAML SummarizeFileChunks for all chunks in a single request.

        Raises ``FileTooLargeError`` when *file_content* exceeds the threshold
        so callers can switch to the per-chunk fallback.
        """
        if len(file_content) > _BATCH_FILE_SIZE_THRESHOLD:
            raise FileTooLargeError(
                f"{file_path} ({len(file_content):,} chars) exceeds batch threshold "
                f"({_BATCH_FILE_SIZE_THRESHOLD:,} chars)"
            )

        from mcp_server.indexing.baml_client.baml_client.async_client import b
        from mcp_server.indexing.baml_client.baml_client.types import ChunkInput

        language = (chunks[0].get("language") or "unknown") if chunks else "unknown"
        chunk_inputs = [
            ChunkInput(
                chunk_id=c["chunk_id"],
                symbol=_symbol_name(symbol_map, c),
                node_type=c.get("node_type") or "unknown",
                line_start=c.get("line_start") or 1,
                line_end=c.get("line_end") or 1,
                content=c.get("content") or "",
            )
            for c in chunks
        ]

        result = await b.SummarizeFileChunks(
            language=language,
            file_path=file_path,
            file_content=file_content,
            chunks=chunk_inputs,
        )
        return cast(Sequence[Any], result.summaries)

    async def _summarize_topological(
        self,
        file_id: int,
        file_path: str,
        file_content: str,
        chunks: List[Dict],
        symbol_map: Dict[int, str],
        max_chunks: Optional[int] = None,
    ) -> Tuple[GeneratedSummary, ...]:
        """Per-chunk fallback: summarize in topological order (leaves first)."""
        order = _topological_order(chunks)
        if max_chunks is not None:
            order = order[:max_chunks]
        chunk_map = {c["chunk_id"]: c for c in chunks}
        stored_summaries: Dict[str, str] = {}
        results: List[GeneratedSummary] = []

        for chunk_id in order:
            c = chunk_map[chunk_id]
            symbol = _symbol_name(symbol_map, c)

            parent_context = ""
            pid = c.get("parent_chunk_id")
            if pid and pid in stored_summaries:
                parent_chunk = chunk_map.get(pid)
                parent_sym = (
                    (_symbol_name(symbol_map, parent_chunk, "parent")) if parent_chunk else "parent"
                )
                parent_context = (
                    f"Context from enclosing scope ({parent_sym}): " f"{stored_summaries[pid]}\n\n"
                )

            try:
                summary_text = await self.summarize_chunk(
                    chunk_hash=chunk_id,
                    file_id=file_id,
                    chunk_start=c.get("line_start") or 1,
                    chunk_end=c.get("line_end") or 1,
                    symbol=symbol,
                    content=c.get("content") or "",
                    language=c.get("language") or "unknown",
                    parent_context=parent_context,
                    file_content=file_content,
                    is_authoritative=True,
                )
                if summary_text:
                    stored_summaries[chunk_id] = summary_text
                    results.append(GeneratedSummary(chunk_id=chunk_id, summary=summary_text))
            except Exception as exc:
                logger.error("Failed to summarize chunk %s: %s", chunk_id, exc)

        return tuple(results)

    async def summarize_file_chunks(
        self,
        file_id: int,
        file_path: str,
        file_content: str,
        chunks: List[Dict],
        symbol_map: Optional[Dict[int, str]] = None,
        persist: bool = True,
        max_chunks: Optional[int] = None,
        call_timeout_seconds: Optional[float] = None,
    ) -> SummaryGenerationResult:
        """Summarize all chunks of a file, preferring the batch API path.

        Filters already-authoritative chunks, calls ``_call_batch_api``, and
        handles ``FileTooLargeError`` by trying bounded profile-batch recovery
        before delegating to the topological per-chunk path. Persists to
        ``chunk_summaries`` with
        ``is_authoritative=1`` when *persist* is ``True``.

        Returns one ``SummaryGenerationResult`` for every outcome.
        """
        if symbol_map is None:
            symbol_map = {}

        # Filter already-authoritative chunks.
        with sqlite3.connect(self.db_path) as conn:
            auth_hashes = {
                row[0]
                for row in conn.execute(
                    "SELECT chunk_hash FROM chunk_summaries WHERE is_authoritative=1"
                ).fetchall()
            }

        to_summarize = [c for c in chunks if c["chunk_id"] not in auth_hashes]
        if not to_summarize:
            return SummaryGenerationResult(
                chunks_attempted=len(chunks),
                summaries_written=0,
                authoritative_chunks=len(chunks),
                missing_chunk_ids=(),
                files_attempted=1,
                files_summarized=0,
            )
        active_chunks = to_summarize[:max_chunks] if max_chunks is not None else to_summarize
        existing_authoritative = len(chunks) - len(to_summarize)

        try:
            summary_call: Any
            if self.summarization_config.get("base_url"):
                summary_call = self._call_profile_batch_api(
                    file_id, file_path, file_content, active_chunks, symbol_map
                )
            else:
                summary_call = self._call_batch_api(
                    file_id, file_path, file_content, active_chunks, symbol_map
                )
            if call_timeout_seconds is not None:
                provider_summaries = await self._await_summary_call_with_timeout(
                    summary_call,
                    timeout_seconds=call_timeout_seconds,
                )
            else:
                provider_summaries = await summary_call
            summaries = _normalize_generated_summaries(provider_summaries)
        except TimeoutError:
            return self._blocked_call_result(
                reason="timeout",
                file_path=file_path,
                selected_chunks=active_chunks,
                remaining_chunks=to_summarize,
                existing_authoritative=existing_authoritative,
                timeout_seconds=call_timeout_seconds,
            )
        except FileTooLargeError as exc:
            recovery = await self._recover_with_profile_or_topological(
                file_id=file_id,
                file_path=file_path,
                file_content=file_content,
                chunks=active_chunks,
                symbol_map=symbol_map,
                warning_prefix="Large-file fallback",
                error=exc,
                retry_profile_batch=True,
            )
            summaries = recovery
        except Exception as exc:
            recovery = await self._recover_with_profile_or_topological(
                file_id=file_id,
                file_path=file_path,
                file_content=file_content,
                chunks=active_chunks,
                symbol_map=symbol_map,
                warning_prefix="Batch API failed",
                error=exc,
                retry_profile_batch=not self.summarization_config.get("base_url"),
            )
            summaries = recovery

        if persist and summaries:
            with sqlite3.connect(self.db_path) as conn:
                persisted_hashes = {
                    row[0]
                    for row in conn.execute(
                        "SELECT chunk_hash FROM chunk_summaries WHERE is_authoritative=1"
                    ).fetchall()
                }
            summaries_to_persist = [
                summary for summary in summaries if summary.chunk_id not in persisted_hashes
            ]
            chunk_lookup = {c["chunk_id"]: c for c in active_chunks}
            model_name = self._get_model_name() if summaries_to_persist else None
            for s in summaries_to_persist:
                c = chunk_lookup.get(s.chunk_id)
                if c is None:
                    continue
                sym = _symbol_name(symbol_map, c)
                self._persist_summary(
                    chunk_hash=s.chunk_id,
                    file_id=file_id,
                    chunk_start=c.get("line_start") or 1,
                    chunk_end=c.get("line_end") or 1,
                    symbol=sym,
                    summary_text=s.summary,
                    model_name=model_name or "unknown",
                    is_authoritative=True,
                )
            logger.info(
                "FileBatchSummarizer: persisted %d summaries for %s",
                len(summaries_to_persist),
                file_path,
            )

        summarized_chunk_ids = {s.chunk_id for s in summaries}
        missing_chunk_ids = [
            c["chunk_id"] for c in to_summarize if c["chunk_id"] not in summarized_chunk_ids
        ]
        return SummaryGenerationResult(
            chunks_attempted=len(active_chunks),
            summaries_written=len(summaries),
            authoritative_chunks=existing_authoritative + len(summaries),
            missing_chunk_ids=tuple(missing_chunk_ids),
            files_attempted=1,
            files_summarized=1 if summaries else 0,
            remaining_chunks=len(missing_chunk_ids),
            scope_drained=len(missing_chunk_ids) == 0,
            summaries=summaries,
        )


class ComprehensiveChunkWriter(FileBatchSummarizer):
    """Summarizes all un-summarized chunks in the index.

    Chunks are grouped by file so each file's content is read from disk only
    once.  Each file's chunks are handed off to ``FileBatchSummarizer``, which
    sends a single API call per file (or falls back to the topological
    per-chunk path for very large files).

    Requires either an active MCP session with sampling capability **or** an
    ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / ``CEREBRAS_API_KEY``
    environment variable.
    """

    def _fetch_unsummarized_rows(
        self, *, limit: int, target_paths: Optional[Sequence[Path]] = None
    ) -> List[Any]:
        normalized_paths = {
            Path(path).resolve(strict=False).as_posix() for path in (target_paths or []) if path
        }
        with sqlite3.connect(self.db_path) as conn:
            # Fail closed on scheme-dependent reads (CHUNKERSAFE Lane A).
            assert_chunk_scheme_readable(conn)
            where_clauses = ["cs.chunk_hash IS NULL"]
            params: list[Any] = []
            if normalized_paths:
                placeholders = ", ".join("?" for _ in normalized_paths)
                where_clauses.append(f"f.path IN ({placeholders})")
                params.extend(sorted(normalized_paths))
            params.append(limit)
            cursor = conn.execute(
                f"""SELECT c.chunk_id, c.file_id, c.line_start, c.line_end,
                           c.content, c.node_type, c.parent_chunk_id,
                           c.language, s.name AS symbol, f.path AS file_path,
                           c.symbol_id
                    FROM code_chunks c
                    JOIN files f ON c.file_id = f.id
                    LEFT JOIN symbols s ON c.symbol_id = s.id
                    LEFT JOIN chunk_summaries cs ON c.chunk_id = cs.chunk_hash
                    WHERE {" AND ".join(where_clauses)}
                    ORDER BY f.path, c.line_start, c.line_end, c.chunk_id
                    LIMIT ?""",
                tuple(params),
            )
            rows = cursor.fetchall()
        return rows

    def _count_unsummarized_rows(self, *, target_paths: Optional[Sequence[Path]] = None) -> int:
        normalized_paths = {
            Path(path).resolve(strict=False).as_posix() for path in (target_paths or []) if path
        }
        with sqlite3.connect(self.db_path) as conn:
            # Fail closed on scheme-dependent reads (CHUNKERSAFE Lane A).
            assert_chunk_scheme_readable(conn)
            where_clauses = ["cs.chunk_hash IS NULL"]
            params: list[Any] = []
            if normalized_paths:
                placeholders = ", ".join("?" for _ in normalized_paths)
                where_clauses.append(f"f.path IN ({placeholders})")
                params.extend(sorted(normalized_paths))
            cursor = conn.execute(
                f"""SELECT COUNT(*)
                    FROM code_chunks c
                    JOIN files f ON c.file_id = f.id
                    LEFT JOIN chunk_summaries cs ON c.chunk_id = cs.chunk_hash
                    WHERE {" AND ".join(where_clauses)}""",
                tuple(params),
            )
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def _process_scope_chunk_limit_for_language(
        self, language: str, limit: int, *, repo_scope: bool = False
    ) -> int:
        normalized_language = (language or "").lower()
        if normalized_language in _DOC_CONTEXT_LANGUAGES:
            if repo_scope:
                return min(limit, _REPO_SCOPE_DOC_PROCESS_SCOPE_CHUNK_LIMIT)
            return min(limit, _DOC_PROCESS_SCOPE_CHUNK_LIMIT)
        return limit

    def _file_chunk_window_for_language(
        self, language: str, *, repo_scope: bool = False
    ) -> Optional[int]:
        normalized_language = (language or "").lower()
        if repo_scope and normalized_language in _DOC_CONTEXT_LANGUAGES:
            return _REPO_SCOPE_DOC_FILE_CHUNK_WINDOW
        return None

    async def process_scope(
        self,
        *,
        limit: int = 500,
        target_paths: Optional[Sequence[Path]] = None,
        max_batches: Optional[int] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> SummaryGenerationResult:
        """Generate summaries for unsummarized chunks within an optional file scope."""
        if not self.can_summarize():
            logger.warning(
                "Cannot run summarization: no MCP sampling and no API key set "
                "(set CEREBRAS_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY)"
            )
            return SummaryGenerationResult()

        total_attempted = 0
        total_summaries_written = 0
        total_authoritative_chunks = 0
        total_files_attempted = 0
        total_files_summarized = 0
        missing_chunk_ids: List[str] = []
        remaining_chunks = 0

        batches_processed = 0
        repo_scope = len(target_paths or []) > 1
        call_timeout_seconds = self._repo_scope_call_timeout_seconds() if repo_scope else None
        while True:
            if cancel_check is not None and cancel_check():
                remaining_chunks = self._count_unsummarized_rows(target_paths=target_paths)
                return SummaryGenerationResult(
                    chunks_attempted=total_attempted,
                    summaries_written=total_summaries_written,
                    authoritative_chunks=total_authoritative_chunks,
                    missing_chunk_ids=tuple(missing_chunk_ids),
                    files_attempted=total_files_attempted,
                    files_summarized=total_files_summarized,
                    batches_processed=batches_processed,
                    remaining_chunks=remaining_chunks,
                    scope_drained=False,
                    cancelled=True,
                )
            rows = self._fetch_unsummarized_rows(limit=limit, target_paths=target_paths)
            if not rows:
                if total_attempted == 0:
                    logger.info("ComprehensiveChunkWriter: no unsummarized chunks found")
                remaining_chunks = 0
                break

            # Group chunks and symbol names by file_id.
            file_meta: Dict[int, tuple] = {}  # file_id -> (file_path, language)
            file_chunks: Dict[int, List[Dict]] = {}
            file_symbol_maps: Dict[int, Dict[int, str]] = {}
            max_files = _REPO_SCOPE_FILE_LIMIT if repo_scope else None

            for row in rows:
                (
                    chunk_id,
                    file_id,
                    line_start,
                    line_end,
                    chunk_content,
                    node_type,
                    parent_chunk_id,
                    language,
                    symbol,
                    file_path,
                    symbol_id,
                ) = row

                if file_id not in file_meta:
                    if max_files is not None and len(file_meta) >= max_files:
                        continue
                    file_meta[file_id] = (file_path, language or "unknown")
                    file_chunks[file_id] = []
                    file_symbol_maps[file_id] = {}

                chunk_cap = self._process_scope_chunk_limit_for_language(
                    language or "unknown",
                    limit,
                    repo_scope=repo_scope,
                )
                if len(file_chunks[file_id]) < chunk_cap:
                    file_chunks[file_id].append(
                        {
                            "chunk_id": chunk_id,
                            "file_id": file_id,
                            "line_start": line_start,
                            "line_end": line_end,
                            "content": chunk_content,
                            "node_type": node_type,
                            "parent_chunk_id": parent_chunk_id,
                            "language": language,
                            "symbol_id": symbol_id,
                        }
                    )
                if symbol_id and symbol:
                    file_symbol_maps[file_id][symbol_id] = symbol

            selected_chunk_count = sum(len(chunks) for chunks in file_chunks.values())
            if selected_chunk_count == 0:
                remaining_chunks = self._count_unsummarized_rows(target_paths=target_paths)
                break

            total_attempted += selected_chunk_count
            total_files_attempted += len(file_meta)
            pass_summaries_written = 0

            for file_id, (file_path, _) in file_meta.items():
                if cancel_check is not None and cancel_check():
                    remaining_chunks = self._count_unsummarized_rows(target_paths=target_paths)
                    return SummaryGenerationResult(
                        chunks_attempted=total_attempted,
                        summaries_written=total_summaries_written,
                        authoritative_chunks=total_authoritative_chunks,
                        missing_chunk_ids=tuple(missing_chunk_ids),
                        files_attempted=total_files_attempted,
                        files_summarized=total_files_summarized,
                        batches_processed=batches_processed,
                        remaining_chunks=remaining_chunks,
                        scope_drained=False,
                        cancelled=True,
                    )
                file_language = (
                    (file_meta[file_id][1] or "unknown") if file_id in file_meta else "unknown"
                )
                file_chunk_window = self._file_chunk_window_for_language(
                    file_language,
                    repo_scope=repo_scope,
                )
                try:
                    with open(file_path, encoding="utf-8", errors="replace") as fh:
                        file_content = fh.read()
                except Exception:
                    file_content = ""

                try:
                    file_result = await self.summarize_file_chunks(
                        file_id=file_id,
                        file_path=file_path,
                        file_content=file_content,
                        chunks=file_chunks[file_id],
                        symbol_map=file_symbol_maps[file_id],
                        persist=True,
                        max_chunks=file_chunk_window,
                        call_timeout_seconds=call_timeout_seconds,
                    )
                    pass_summaries_written += file_result.summaries_written
                    total_summaries_written += file_result.summaries_written
                    total_authoritative_chunks += file_result.authoritative_chunks
                    total_files_summarized += file_result.files_summarized
                    missing_chunk_ids.extend(file_result.missing_chunk_ids)
                    if file_result.blocked_call_reason is not None:
                        remaining_chunks = self._count_unsummarized_rows(target_paths=target_paths)
                        return SummaryGenerationResult(
                            chunks_attempted=total_attempted,
                            summaries_written=total_summaries_written,
                            authoritative_chunks=total_authoritative_chunks,
                            missing_chunk_ids=tuple(missing_chunk_ids),
                            files_attempted=total_files_attempted,
                            files_summarized=total_files_summarized,
                            batches_processed=batches_processed,
                            remaining_chunks=remaining_chunks,
                            scope_drained=False,
                            blocked_call_reason=file_result.blocked_call_reason,
                            blocked_call_file_path=file_result.blocked_call_file_path,
                            blocked_call_chunk_ids=file_result.blocked_call_chunk_ids,
                            blocked_call_timeout_seconds=file_result.blocked_call_timeout_seconds,
                        )
                except Exception as exc:
                    logger.error("Failed to summarize file %s: %s", file_path, exc)
                    missing_chunk_ids.extend(
                        [chunk["chunk_id"] for chunk in file_chunks.get(file_id, [])]
                    )

            if pass_summaries_written == 0:
                remaining_chunks = self._count_unsummarized_rows(target_paths=target_paths)
                logger.info(
                    "ComprehensiveChunkWriter: made no progress on %d remaining chunks",
                    len(rows),
                )
                break
            batches_processed += 1
            remaining_chunks = self._count_unsummarized_rows(target_paths=target_paths)
            if max_batches is not None and batches_processed >= max_batches:
                break

        logger.info(
            "ComprehensiveChunkWriter: stored %d/%d summaries",
            total_summaries_written,
            total_attempted,
        )
        return SummaryGenerationResult(
            chunks_attempted=total_attempted,
            summaries_written=total_summaries_written,
            authoritative_chunks=total_authoritative_chunks,
            missing_chunk_ids=tuple(missing_chunk_ids),
            files_attempted=total_files_attempted,
            files_summarized=total_files_summarized,
            batches_processed=batches_processed,
            remaining_chunks=remaining_chunks,
            scope_drained=remaining_chunks == 0,
        )

    async def process_all(self, limit: int = 500) -> int:
        """Generate summaries for every unsummarized chunk, grouped by file."""
        result = await self.process_scope(limit=limit)
        return result.summaries_written


class LazyChunkWriter(ChunkWriter):
    """Enqueues chunks seen during search and summarizes them in the background.

    Usage::

        writer = LazyChunkWriter(db_path=..., qdrant_client=None)
        writer.start()                      # launch background worker
        writer.update_session(session)      # call on every tool invocation
        writer.enqueue(chunk_data_dict)     # fire-and-forget after each search
    """

    def __init__(
        self,
        db_path: str,
        qdrant_client: Any,
        session: Any = None,
        client_name: Optional[str] = None,
        summarization_config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(db_path, qdrant_client, session, client_name, summarization_config)
        self.queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Start the background worker task (idempotent)."""
        if self._task is None:
            self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        """Cancel and await the owned background task before closing its storage."""
        if self._task is not None:
            task, self._task = self._task, None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._sqlite_store is not None:
            self._sqlite_store.close()
            self._sqlite_store = None

    def update_session(self, session: Any) -> None:
        """Refresh the MCP session reference used for sampling.

        Call this on every tool invocation so the writer always holds a live
        session even if the client reconnects.
        """
        self.session = session

    async def _worker(self) -> None:
        while True:
            chunk = await self.queue.get()
            try:
                await self.summarize_chunk(**chunk)
            except Exception as exc:
                logger.error("Error summarizing chunk (%s)", type(exc).__name__)
            finally:
                self.queue.task_done()

    def enqueue(self, chunk_data: Dict[str, Any]) -> None:
        """Add a chunk to the background summarization queue (non-blocking)."""
        self.queue.put_nowait(chunk_data)
