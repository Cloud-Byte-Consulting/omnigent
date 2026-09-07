"""Exercise the shipped role-router bundle and its supervisor wiring."""

from pathlib import Path

from omnigent.spec import load
from tests.e2e.omnigent._example_helpers import (
    assert_completed_one_shot,
    run_one_shot_at_path,
)
from tests.e2e.omnigent.conftest import configure_mock_llm


def test_role_router_registers_specialists_and_guardrails(omnigent_repo_root: Path) -> None:
    agent = load(omnigent_repo_root / "examples/role_router")
    assert agent.name == "role_router"
    assert agent.executor.config["harness"] == "claude-sdk"
    assert agent.executor.model is None
    assert {"recon", "medic", "engineer", "qa", "judge", "scribe"} == set(agent.tools.agents)
    assert {child.name: child.executor.config["harness"] for child in agent.sub_agents} == {
        "recon": "pi",
        "medic": "claude-sdk",
        "engineer": "openai-agents",
        "qa": "openai-agents",
        "judge": "pi",
        "scribe": "openai-agents",
    }
    for child in agent.sub_agents:
        assert child.os_env is not None
        assert child.os_env.type == "caller_process"
    assert agent.guardrails is not None
    assert {
        "blast_radius",
        "spawn_bounds",
        "rlm_subcall_bounds",
        "rlm_cost_plan",
        "hillclimb_budget",
        "headless_subagent_purpose_guard",
    } <= {policy.name for policy in agent.guardrails.policies}


def test_role_router_one_shot(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
) -> None:
    # Use the mock-backed harness so loading the real bundle needs no vendor login.
    configure_mock_llm(mock_llm_server_url, [{"text": "OK"}])
    result = run_one_shot_at_path(
        omnigent_python=omnigent_python,
        omnigent_repo_root=omnigent_repo_root,
        omnigent_credentials_env=mock_credentials_env,
        yaml_path=omnigent_repo_root / "examples/role_router",
        harness="openai-agents",
        model="mock-model",
    )
    assert_completed_one_shot(result, "role_router")
