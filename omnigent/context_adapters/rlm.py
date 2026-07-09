"""Bounded contract for an optional Recursive Language Model context adapter.

The backend is deliberately injected.  Omnigent owns orchestration, identity,
budgets, fallback, and verification; an RLM worker only receives untrusted text
and returns a compact, cited result.  It is never granted tool capabilities.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True, slots=True)
class ContextDocument:
    id: str
    content: str

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("document id must not be empty")


@dataclass(frozen=True, slots=True)
class Citation:
    document_id: str
    start: int
    end: int
    quote: str


@dataclass(frozen=True, slots=True)
class ContextUsage:
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float

    def __post_init__(self) -> None:
        if self.input_tokens < 0 or self.output_tokens < 0 or self.cost_usd < 0:
            raise ValueError("usage values must be non-negative")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class BoundedContextRequest:
    task_ref: str
    session_id: str
    query: str
    documents: tuple[ContextDocument, ...]
    max_input_chars: int
    max_output_chars: int
    max_tokens: int
    max_cost_usd: float
    timeout_seconds: float
    allowed_models: frozenset[str]
    content_trust: Literal["untrusted"] = "untrusted"
    capabilities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.task_ref.strip() or not self.session_id.strip():
            raise ValueError("task_ref and session_id are required")
        if not self.query.strip() or not self.documents:
            raise ValueError("query and at least one document are required")
        if len({document.id for document in self.documents}) != len(self.documents):
            raise ValueError("document ids must be unique")
        if (
            min(
                self.max_input_chars,
                self.max_output_chars,
                self.max_tokens,
            )
            <= 0
        ):
            raise ValueError("character and token budgets must be positive")
        if self.max_cost_usd < 0 or self.timeout_seconds <= 0:
            raise ValueError("cost must be non-negative and timeout must be positive")
        if not self.allowed_models:
            raise ValueError("at least one adapter model must be allowed")
        if self.content_trust != "untrusted" or self.capabilities:
            raise ValueError("RLM context workers must be untrusted and capability-free")


@dataclass(frozen=True, slots=True)
class RLMContextResult:
    task_ref: str
    session_id: str
    summary: str
    citations: tuple[Citation, ...]
    usage: ContextUsage


@dataclass(frozen=True, slots=True)
class ContextAdapterResponse:
    result: RLMContextResult
    truncated_documents: tuple[str, ...] = ()
    used_fallback: bool = False
    fallback_reason: str | None = None


Backend = Callable[[BoundedContextRequest], Awaitable[RLMContextResult]]
Fallback = Callable[[BoundedContextRequest, str], Awaitable[RLMContextResult]]


class _RejectedResult(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class RLMContextAdapter:
    """Run and verify an optional RLM backend under parent-owned limits."""

    def __init__(self, *, backend: Backend, fallback: Fallback | None = None) -> None:
        self._backend = backend
        self._fallback = fallback

    async def retrieve(self, request: BoundedContextRequest) -> ContextAdapterResponse:
        bounded_request, truncated = _truncate_request(request)
        try:
            result = await asyncio.wait_for(
                self._backend(bounded_request),
                timeout=bounded_request.timeout_seconds,
            )
            _validate_result(bounded_request, result, enforce_adapter_budget=True)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return await self._use_fallback(bounded_request, truncated, "timeout")
        except _RejectedResult as exc:
            return await self._use_fallback(bounded_request, truncated, exc.reason)
        # The backend is a plugin boundary.  Its exception taxonomy is not
        # trusted or imported into core, so all ordinary failures become the
        # same non-sensitive fallback reason.
        except Exception:  # noqa: BLE001
            return await self._use_fallback(bounded_request, truncated, "backend_error")

        return ContextAdapterResponse(result=result, truncated_documents=truncated)

    async def _use_fallback(
        self,
        request: BoundedContextRequest,
        truncated: tuple[str, ...],
        reason: str,
    ) -> ContextAdapterResponse:
        if self._fallback is None:
            raise RuntimeError(f"rlm adapter failed: {reason}") from None
        result = await self._fallback(request, reason)
        _validate_result(request, result, enforce_adapter_budget=False)
        return ContextAdapterResponse(
            result=result,
            truncated_documents=truncated,
            used_fallback=True,
            fallback_reason=reason,
        )


def _truncate_request(
    request: BoundedContextRequest,
) -> tuple[BoundedContextRequest, tuple[str, ...]]:
    remaining = request.max_input_chars
    documents: list[ContextDocument] = []
    truncated: list[str] = []
    for document in request.documents:
        content = document.content[:remaining]
        documents.append(ContextDocument(document.id, content))
        if len(content) != len(document.content):
            truncated.append(document.id)
        remaining -= len(content)
    return replace(request, documents=tuple(documents)), tuple(truncated)


def _validate_result(
    request: BoundedContextRequest,
    result: RLMContextResult,
    *,
    enforce_adapter_budget: bool,
) -> None:
    if result.task_ref != request.task_ref or result.session_id != request.session_id:
        raise _RejectedResult("identity_mismatch")
    if len(result.summary) > request.max_output_chars:
        raise _RejectedResult("output_budget_exceeded")
    if result.summary and not result.citations:
        raise _RejectedResult("citations_required")

    if enforce_adapter_budget:
        if result.usage.model not in request.allowed_models:
            raise _RejectedResult("model_not_allowed")
        if result.usage.total_tokens > request.max_tokens:
            raise _RejectedResult("token_budget_exceeded")
        if Decimal(str(result.usage.cost_usd)) > Decimal(str(request.max_cost_usd)):
            raise _RejectedResult("cost_budget_exceeded")

    documents = {document.id: document.content for document in request.documents}
    for citation in result.citations:
        content = documents.get(citation.document_id)
        if (
            content is None
            or citation.start < 0
            or citation.end < citation.start
            or citation.end > len(content)
            or content[citation.start : citation.end] != citation.quote
        ):
            raise _RejectedResult("citation_mismatch")


__all__ = [
    "BoundedContextRequest",
    "Citation",
    "ContextAdapterResponse",
    "ContextDocument",
    "ContextUsage",
    "RLMContextAdapter",
    "RLMContextResult",
]
