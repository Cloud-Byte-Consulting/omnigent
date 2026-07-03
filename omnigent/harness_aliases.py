"""Shared harness-name alias helpers.

Keep user-facing shorthand spellings at the edges while the rest of
Omnigent continues to use canonical harness identifiers internally.
"""

from __future__ import annotations

from omnigent.harness_plugins import harness_aliases, native_harnesses

HARNESS_ALIASES: dict[str, str] = harness_aliases()

# Canonical native-CLI harness spellings. These harnesses type messages into
# a resident terminal process and mirror their transcript back to Omnigent, so
# the runner must not replay Omnigent history or treat a completed queue call
# as a full in-process model turn. ``AgentSpec.harness_kind`` returns these
# canonical spellings for native agents, so no executor-type aliasing is needed
# here.
NATIVE_HARNESSES: frozenset[str] = native_harnesses()


# Open Engine agent-code suffix → canonical Omnigent harness.
#
# Open Engine agent codes name a *persistent vendor CLI* claiming Linear tasks,
# so the default resolution is that vendor's NATIVE harness (the CLI/TUI bridge),
# NOT the in-process SDK orchestrator. In particular ``claude`` → ``claude-native``
# (the Claude Code CLI), deliberately overriding the user-facing
# ``HARNESS_ALIASES["claude"] = "claude-sdk"`` shorthand, which is wrong for an
# interactive agent session.
#
# This table mirrors ``agent_code_harness_map`` in
# ``omnigent/profiles/openengine_stack.yaml``. Keep the two in sync until
# ``sessions.py`` reads the profile directly (the single-source TODO).
# ponytail: two copies (here + the profile) until Lane B wires profile loading;
# collapse to one source when that lands.
OPENENGINE_AGENT_HARNESS: dict[str, str] = {
    "claude": "claude-native",
    "codex": "codex-native",
    "cursor": "claude-sdk",
    "copilot": "copilot",
    "gemini": "claude-sdk",
    "kiro": "kiro-native",
    "windsurf": "claude-sdk",
}


def harness_for_agent_code(agent_code: str) -> str:
    """Return the canonical harness for an Open Engine agent code.

    Open Engine agent codes follow the convention ``<operator>-<suffix>``,
    e.g. ``alex-claude`` or ``sam-codex``. The suffix (everything after the
    last hyphen) is resolved through :data:`OPENENGINE_AGENT_HARNESS` first
    (Open Engine prefers the native CLI harness, e.g. ``"claude"`` →
    ``"claude-native"``), then falls back to :data:`HARNESS_ALIASES`.

    :param agent_code: Agent code string, e.g. ``"alex-claude"``.
    :returns: Canonical harness id, e.g. ``"claude-native"`` for
        ``"alex-claude"`` or ``"codex-native"`` for ``"sam-codex"``.
    """
    suffix = agent_code.rsplit("-", 1)[-1]
    if suffix in OPENENGINE_AGENT_HARNESS:
        return OPENENGINE_AGENT_HARNESS[suffix]
    return canonicalize_harness(suffix) or suffix


def canonicalize_harness(harness: str | None) -> str | None:
    """Return the canonical harness identifier for *harness*.

    Unknown names are returned unchanged so callers can still produce
    their normal validation error messages.
    """
    if harness is None:
        return None
    return HARNESS_ALIASES.get(harness, harness)


def is_claude_sdk_harness_name(harness: str | None) -> bool:
    """Return ``True`` for the canonical Claude SDK harness and aliases."""
    return canonicalize_harness(harness) == "claude-sdk"


def is_native_harness(harness: str | None) -> bool:
    """Return whether *harness* is a native CLI harness.

    Native harnesses boot a vendor TUI in a terminal and route user messages
    into that running process. Accepts the canonical native spellings that
    :attr:`AgentSpec.harness_kind` returns plus their reversed aliases.

    :param harness: A harness id, e.g. ``"codex-native"`` or ``"claude_sdk"``;
        ``None`` returns ``False``.
    :returns: ``True`` for a native CLI harness, else ``False``.
    """
    if harness is None:
        return False
    return (canonicalize_harness(harness) or harness) in NATIVE_HARNESSES


def native_terminal_name(harness: str | None) -> str | None:
    """Return the tmux terminal short-name a native harness runs its CLI in.

    Native CLI panes are keyed ``(conversation_id, <short-name>, "main")`` in the
    terminal registry, where the short name is the canonical native harness id
    with the ``-native`` suffix dropped — e.g. ``"claude-native"`` -> ``"claude"``,
    ``"native-codex"`` -> ``"codex"``, ``"opencode-native"`` -> ``"opencode"``.

    :param harness: A harness id (canonical or reversed alias), e.g.
        ``"cursor-native"``; ``None`` or a non-native harness returns ``None``.
    :returns: The terminal short-name, e.g. ``"cursor"``, or ``None`` when
        *harness* is not a native CLI harness.
    """
    if not is_native_harness(harness):
        return None
    canonical = canonicalize_harness(harness) or harness
    # Canonical native ids are ``<name>-native``; some accepted aliases keep the
    # reversed ``native-<name>`` spelling (not all are folded by
    # ``canonicalize_harness``), so strip either affix.
    return canonical.removesuffix("-native").removeprefix("native-")


if __name__ == "__main__":
    # Open Engine prefers native CLI harnesses; "claude" → "claude-native"
    # (decision), distinct from the SDK shorthand in HARNESS_ALIASES.
    assert harness_for_agent_code("alex-claude") == "claude-native", "alex-claude → claude-native"
    assert harness_for_agent_code("sam-codex") == "codex-native", "sam-codex → codex-native"
    assert harness_for_agent_code("bob-cursor") == "claude-sdk", "bob-cursor → claude-sdk"
    # Suffix not in the OE map falls back to HARNESS_ALIASES (agy → antigravity).
    assert harness_for_agent_code("alice-agy") == "antigravity", "alice-agy → antigravity"
    print("harness_aliases self-check passed")
