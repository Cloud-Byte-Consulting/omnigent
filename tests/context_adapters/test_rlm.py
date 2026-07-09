from __future__ import annotations

import asyncio

import pytest

from omnigent.context_adapters.rlm import (
    BoundedContextRequest,
    Citation,
    ContextDocument,
    ContextUsage,
    RLMContextAdapter,
    RLMContextResult,
)


def _request(**overrides: object) -> BoundedContextRequest:
    values: dict[str, object] = {
        "task_ref": "CLO-109",
        "session_id": "conv-1",
        "query": "What is the approval rule?",
        "documents": (ContextDocument("policy", "Transfers require supervisor approval."),),
        "max_input_chars": 1_000,
        "max_output_chars": 500,
        "max_tokens": 200,
        "max_cost_usd": 0.01,
        "timeout_seconds": 1.0,
        "allowed_models": frozenset({"cheap-code-model"}),
    }
    values.update(overrides)
    return BoundedContextRequest(**values)  # type: ignore[arg-type]


def _result(**overrides: object) -> RLMContextResult:
    values: dict[str, object] = {
        "task_ref": "CLO-109",
        "session_id": "conv-1",
        "summary": "Supervisor approval is required.",
        "citations": (
            Citation(
                document_id="policy",
                start=0,
                end=38,
                quote="Transfers require supervisor approval.",
            ),
        ),
        "usage": ContextUsage(
            model="cheap-code-model",
            input_tokens=80,
            output_tokens=20,
            cost_usd=0.001,
        ),
    }
    values.update(overrides)
    return RLMContextResult(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_returns_verified_citations_and_usage() -> None:
    seen: list[BoundedContextRequest] = []

    async def backend(request: BoundedContextRequest) -> RLMContextResult:
        seen.append(request)
        return _result()

    adapter = RLMContextAdapter(backend=backend)
    response = await adapter.retrieve(_request())

    assert response.result == _result()
    assert response.used_fallback is False
    assert seen[0].capabilities == frozenset()
    assert seen[0].content_trust == "untrusted"


@pytest.mark.asyncio
async def test_truncates_context_before_backend_and_reports_it() -> None:
    seen: list[BoundedContextRequest] = []

    async def backend(request: BoundedContextRequest) -> RLMContextResult:
        seen.append(request)
        doc = request.documents[0]
        return _result(
            citations=(Citation(doc.id, 0, len(doc.content), doc.content),),
        )

    request = _request(
        documents=(ContextDocument("policy", "0123456789"),),
        max_input_chars=6,
    )
    response = await RLMContextAdapter(backend=backend).retrieve(request)

    assert seen[0].documents[0].content == "012345"
    assert response.truncated_documents == ("policy",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_result, reason",
    [
        (_result(task_ref="other"), "identity_mismatch"),
        (_result(summary="x" * 501), "output_budget_exceeded"),
        (
            _result(
                usage=ContextUsage("cheap-code-model", 180, 30, 0.001),
            ),
            "token_budget_exceeded",
        ),
        (
            _result(
                usage=ContextUsage("unapproved-model", 10, 10, 0.001),
            ),
            "model_not_allowed",
        ),
        (
            _result(
                usage=ContextUsage("cheap-code-model", 10, 10, 0.011),
            ),
            "cost_budget_exceeded",
        ),
        (_result(citations=()), "citations_required"),
        (
            _result(citations=(Citation("policy", 0, 9, "fabricated"),)),
            "citation_mismatch",
        ),
    ],
)
async def test_invalid_adapter_result_falls_back_without_losing_parent_identity(
    bad_result: RLMContextResult,
    reason: str,
) -> None:
    fallback_calls: list[tuple[BoundedContextRequest, str]] = []

    async def backend(_: BoundedContextRequest) -> RLMContextResult:
        return bad_result

    async def fallback(request: BoundedContextRequest, fallback_reason: str) -> RLMContextResult:
        fallback_calls.append((request, fallback_reason))
        return _result(usage=ContextUsage("primary-model", 90, 30, 0.02))

    response = await RLMContextAdapter(backend=backend, fallback=fallback).retrieve(_request())

    assert response.used_fallback is True
    assert response.fallback_reason == reason
    assert fallback_calls[0][0].task_ref == "CLO-109"
    assert fallback_calls[0][0].session_id == "conv-1"


@pytest.mark.asyncio
async def test_timeout_falls_back_to_primary() -> None:
    async def backend(_: BoundedContextRequest) -> RLMContextResult:
        await asyncio.sleep(1)
        raise AssertionError("unreachable")

    async def fallback(_: BoundedContextRequest, reason: str) -> RLMContextResult:
        assert reason == "timeout"
        return _result(usage=ContextUsage("primary-model", 1, 1, 0.1))

    response = await RLMContextAdapter(backend=backend, fallback=fallback).retrieve(
        _request(timeout_seconds=0.001)
    )
    assert response.used_fallback is True


@pytest.mark.asyncio
async def test_cancellation_propagates_and_does_not_fallback() -> None:
    fallback_called = False

    async def backend(_: BoundedContextRequest) -> RLMContextResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def fallback(_: BoundedContextRequest, __: str) -> RLMContextResult:
        nonlocal fallback_called
        fallback_called = True
        return _result()

    task = asyncio.create_task(
        RLMContextAdapter(backend=backend, fallback=fallback).retrieve(_request())
    )
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert fallback_called is False


@pytest.mark.asyncio
async def test_failure_without_fallback_is_explicit() -> None:
    async def backend(_: BoundedContextRequest) -> RLMContextResult:
        raise RuntimeError("backend unavailable")

    with pytest.raises(RuntimeError, match="rlm adapter failed: backend_error"):
        await RLMContextAdapter(backend=backend).retrieve(_request())
