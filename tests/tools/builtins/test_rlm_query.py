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


def test_rejects_local_environment_unless_config_allows(monkeypatch: Any) -> None:
    """The wrapper defaults to the Docker containment floor."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    tool = RlmQueryTool(rlm_loader=lambda: _FakeRLM)

    result = _invoke(
        tool,
        {"haystack": "context", "question": "what matters?", "environment": "local"},
    )

    assert result["error"] == "unsafe_environment"


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
