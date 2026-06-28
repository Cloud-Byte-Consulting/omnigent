"""Tests for the opt-in :mod:`omnigent.tools.builtins.rlm_query` tool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from omnigent.tools.base import ToolContext
from omnigent.tools.builtins import get_builtin_tool
from omnigent.tools.builtins.rlm_query import RlmQueryTool

_CTX = ToolContext(task_id="task_test", agent_id="agent_test")


class _FakeRLM:
    """Small RLM stand-in that exercises wrapper construction and sub-calls."""

    last_kwargs: dict[str, Any] = {}

    def __init__(self, **kwargs: Any) -> None:
        self.depth = int(kwargs.get("depth", 0))
        self.backend_kwargs = kwargs.get("backend_kwargs")
        self.kwargs = kwargs
        _FakeRLM.last_kwargs = kwargs

    def _subcall(self, prompt: str, model: str | None = None) -> Any:
        del model
        return SimpleNamespace(response=f"sub:{prompt}")

    def completion(self, prompt: str | list[str], root_prompt: str | None = None) -> Any:
        del prompt, root_prompt
        first = self._subcall("one").response
        second = self._subcall("two").response
        return SimpleNamespace(response=f"{first}|{second}")


def _invoke(tool: RlmQueryTool, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke a tool and parse its JSON output."""
    return json.loads(tool.invoke(json.dumps(payload), _CTX))


def test_get_builtin_tool_instantiates_rlm_query() -> None:
    """``rlm_query`` is available through the opt-in builtin registry."""
    tool = get_builtin_tool("rlm_query")
    assert isinstance(tool, RlmQueryTool)
    assert tool.name() == "rlm_query"


def test_missing_model_key_returns_actionable_error(monkeypatch: Any) -> None:
    """OpenAI backend without OPENAI_API_KEY fails before importing RLM."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    tool = RlmQueryTool(rlm_loader=lambda: _FakeRLM)

    result = _invoke(tool, {"haystack": "context", "question": "what matters?"})

    assert result["error"] == "missing_model_key"
    assert "OPENAI_API_KEY" in result["message"]


def test_missing_rlm_library_returns_install_hint(monkeypatch: Any) -> None:
    """Absent ``rlms`` dependency yields a precise opt-in install hint."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tool = RlmQueryTool(rlm_loader=lambda: (_ for _ in ()).throw(ImportError("missing")))

    result = _invoke(tool, {"haystack": "context", "question": "what matters?"})

    assert result["error"] == "missing_rlm_library"
    assert "pip install 'rlms[docker]'" in result["install_hint"]


def test_model_args_cannot_select_unsandboxed_environment(monkeypatch: Any) -> None:
    """Security: environment/allow_local from tool-call ARGS are ignored (config-only).

    A prompt-injected haystack smuggling these out-of-schema keys must NOT escape the
    Docker containment floor into the unsandboxed local REPL.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tool = RlmQueryTool(rlm_loader=lambda: _FakeRLM)  # config defaults to docker

    result = _invoke(
        tool,
        {
            "haystack": "context",
            "question": "what matters?",
            "environment": "local",
            "allow_local_environment": True,
        },
    )

    assert "error" not in result
    assert result["metadata"]["environment"] == "docker"


def test_local_environment_requires_operator_config(monkeypatch: Any) -> None:
    """Operator CONFIG may request local, but only with the explicit allow flag."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tool = RlmQueryTool(config={"environment": "local"}, rlm_loader=lambda: _FakeRLM)

    result = _invoke(tool, {"haystack": "context", "question": "what matters?"})

    assert result["error"] == "unsafe_environment"


def test_missing_subcall_hook_fails_closed(monkeypatch: Any) -> None:
    """If upstream RLM lacks ``_subcall``, refuse to run rather than fan out ungoverned."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class _NoSubcall:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        def completion(self, *args: Any, **kwargs: Any) -> Any:
            del args, kwargs
            return SimpleNamespace(response="should-not-run")

    tool = RlmQueryTool(rlm_loader=lambda: _NoSubcall)

    result = _invoke(tool, {"haystack": "context", "question": "what matters?"})

    assert result["error"] == "rlm_query_failed"
    assert "_subcall" in result["message"]


def test_runs_fake_rlm_with_governed_defaults(monkeypatch: Any) -> None:
    """The wrapper disables custom tools and passes the intended RLM caps."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tool = RlmQueryTool(
        config={"model": "gpt-test", "max_iterations": "3"},
        rlm_loader=lambda: _FakeRLM,
    )

    result = _invoke(tool, {"haystack": "context", "question": "what matters?"})

    assert result["answer"] == "sub:one|sub:two"
    assert result["metadata"]["environment"] == "docker"
    assert result["metadata"]["max_depth"] == 1
    assert result["metadata"]["subcalls_started"] == 2
    assert _FakeRLM.last_kwargs["backend_kwargs"] == {"model_name": "gpt-test"}
    assert _FakeRLM.last_kwargs["custom_tools"] == {}
    assert _FakeRLM.last_kwargs["custom_sub_tools"] == {}
    assert _FakeRLM.last_kwargs["max_iterations"] == 3


def test_subcall_cap_returns_capped_error_without_dispatching_more(monkeypatch: Any) -> None:
    """The guarded ``_subcall`` patch enforces caps despite upstream callbacks being soft."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tool = RlmQueryTool(config={"max_subcalls": "1"}, rlm_loader=lambda: _FakeRLM)

    result = _invoke(tool, {"haystack": "context", "question": "what matters?"})

    assert "sub:one" in result["answer"]
    assert "RLM sub-call cap exceeded" in result["answer"]
    assert result["metadata"]["subcalls_started"] == 2
    assert result["metadata"]["subcall_cap_exceeded"] is True


def test_haystack_over_config_cap_is_rejected(monkeypatch: Any) -> None:
    """An operator haystack cap rejects oversized input before RLM runs."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tool = RlmQueryTool(config={"max_haystack_chars": "3"}, rlm_loader=lambda: _FakeRLM)

    result = _invoke(tool, {"haystack": "abcd", "question": "what matters?"})

    assert result["error"] == "haystack_too_large"


def test_invalid_json_arguments_is_rejected(monkeypatch: Any) -> None:
    """Malformed tool arguments fail with a precise error, never a crash."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tool = RlmQueryTool(rlm_loader=lambda: _FakeRLM)

    result = json.loads(tool.invoke("{not json", _CTX))

    assert result["error"] == "invalid_arguments"


def test_empty_question_is_rejected(monkeypatch: Any) -> None:
    """A blank question is rejected before any RLM work."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tool = RlmQueryTool(rlm_loader=lambda: _FakeRLM)

    result = _invoke(tool, {"haystack": "context", "question": "   "})

    assert result["error"] == "invalid_arguments"
