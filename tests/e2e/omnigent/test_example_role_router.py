"""Structural test for the role_router supervisor bundle (examples/role_router).

role_router is the plan/decompose/delegate CO port: a claude-sdk supervisor
with vendor-diverse specialist sub-agents (recon / medic / engineer / qa /
judge / scribe) and a code-enforced two-tier Judge hill-climb gate. Pure
spec-load — no LLM, no credentials.

What breaks if this fails:
- the CO substrate drifts (harness / context window / a model pin that
  re-couples the supervisor to one provider),
- a specialist is dropped or its harness collapses onto one vendor
  (the Anthropic / OpenAI / Gemini independence the roster is built on),
- the hill-climb policy is removed or its factory arguments empty out
  (the Judge gate can be talked past; the resolver would use the factory
  itself as the evaluator and fail closed),
- a skill path under ``examples/role_router/skills`` is referenced but
  missing on disk (the CO prompt points at a file that will not load).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_role_router.py -> repo root is 3 parents up.
_ROLE_ROUTER_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "role_router"
_ROLE_ROUTER_CONFIG = _ROLE_ROUTER_BUNDLE / "config.yaml"

# Inline ``type: agent`` tools in config.yaml. The native directory loader
# only discovers children from ``agents/<dir>/``, so these do not appear on
# ``AgentSpec.sub_agents`` / ``tools.agents`` after ``load()``.
_SPECIALISTS: dict[str, str] = {
    "recon": "pi",
    "medic": "claude-sdk",
    "engineer": "openai-agents",
    "qa": "openai-agents",
    "judge": "pi",
    "scribe": "openai-agents",
}


@pytest.fixture(scope="module")
def role_router_spec() -> AgentSpec:
    """Load and validate the role_router bundle once for the module."""
    return load(_ROLE_ROUTER_BUNDLE)


def _inline_agent_tools() -> dict[str, dict[str, object]]:
    raw = yaml.safe_load(_ROLE_ROUTER_CONFIG.read_text(encoding="utf-8"))
    tools = raw["tools"]
    return {
        name: entry
        for name, entry in tools.items()
        if isinstance(entry, dict) and entry.get("type") == "agent"
    }


def test_co_executor(role_router_spec: AgentSpec) -> None:
    """
    The CO runs on claude-sdk with a 1M window and no pinned model or
    profile, so it inherits the user's configured Claude provider.

    Re-pinning a model here would re-couple the supervisor to one
    backend. A dropped or flattened harness would leave the CO unable
    to dispatch.
    """
    assert role_router_spec.name == "role_router"
    ex = role_router_spec.executor
    assert ex.config.get("harness") == "claude-sdk"
    assert ex.model is None
    assert ex.profile is None
    assert ex.context_window == 1000000


def test_specialist_roster() -> None:
    """
    The six specialists the README names are declared as inline
    ``type: agent`` tools, each on the harness that keeps the
    Anthropic / OpenAI / Gemini split.

    A missing/renamed role drops a pipeline stage. Same-harness
    collapse on every specialist would break the independent Judge
    and QA checks.
    """
    roster = _inline_agent_tools()
    assert sorted(roster) == sorted(_SPECIALISTS)
    for name, harness in _SPECIALISTS.items():
        executor = roster[name].get("executor")
        assert isinstance(executor, dict), name
        assert executor.get("harness") == harness, name
        assert executor.get("model"), name
    harnesses = {entry["executor"]["harness"] for entry in roster.values()}
    assert harnesses == {"claude-sdk", "openai-agents", "pi"}


def test_judge_hillclimb_gate(role_router_spec: AgentSpec) -> None:
    """
    The Judge hill-climb gate is a runner-side function policy, not
    prompt text. Dropping ``hillclimb_budget`` or emptying its
    arguments lets the CO talk past STOP_BUDGET / STOP_PLATEAU, or
    fails the first gated ``sys_session_send`` closed.
    """
    assert role_router_spec.guardrails is not None
    policy = next(p for p in role_router_spec.guardrails.policies if p.name == "hillclimb_budget")
    assert policy.function.path == "omnigent.inner.nessie.policies.hillclimb_budget"
    args = policy.function.arguments
    assert args, (
        f"hillclimb_budget function.arguments is empty ({args!r}); "
        "the resolver would use the factory itself as the evaluator"
    )
    assert args["max_rounds"] == 3
    assert args["max_flat_rounds"] == 2
    assert args["min_confidence_delta"] == 0.05
    assert args["refine_purpose"] == "review"
    assert "sys_session_send" in args["dispatch_tools"]
    mod, _, fn = policy.function.path.rpartition(".")
    assert hasattr(importlib.import_module(mod), fn), policy.function.path


def test_referenced_role_router_skills_exist(role_router_spec: AgentSpec) -> None:
    """
    Every ``examples/role_router/skills/...`` path the config cites
    must exist on disk, and ``load()`` must discover ``rlm-query``.

    A dangling skill path means the CO prompt points at a file the
    runner cannot load.
    """
    config = _ROLE_ROUTER_CONFIG.read_text(encoding="utf-8")
    prefix = "examples/role_router/skills/"
    referenced = [token.rstrip(".,;:") for token in config.split() if token.startswith(prefix)]
    assert referenced, "config.yaml cites no examples/role_router/skills path"
    repo_root = Path(__file__).resolve().parents[3]
    for rel in referenced:
        assert (repo_root / rel).is_file(), rel
    assert "rlm-query" in {s.name for s in role_router_spec.skills}
