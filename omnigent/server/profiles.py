"""Open Engine stack-profile → session-policy loader (OE-1b Lane B bridge).

The `profiles/openengine_stack*.yaml` stack profiles declare a `guardrails.policies`
block (e.g. `opa_oe_boundaries`, the OPA boundary delegation). Nothing loaded those
profiles into a running session — this module is that bridge.

When a session is created with the label ``openengine.profile=<name>``, the create
handler calls :func:`apply_profile_session_policies`, which reads
``<profiles_dir>/<name>.yaml``, parses its ``guardrails.policies`` with the normal
spec parser, and writes each one as a **session policy** into the policy store keyed
by the session id. Because the PolicyEngine is rebuilt lazily on every enforcement
and loads session policies first (``runtime/policies/builder.py``), the policies go
live with no change to the engine-build sites — which is what makes the OPA
governance path activatable and testable end to end.

**Scope:** guardrails.policies only (the goal: activate the OPA boundaries). The
profile's MCP injection, skills, and session_labels are separate Lane B work.

**Safety:** the profile name comes from a client-supplied label, so it is strictly
validated (``[A-Za-z0-9_-]`` only) and the resolved path is confined to the profiles
dir — no path traversal. A requested profile is validated completely and attached
atomically, or session creation fails.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import yaml

from omnigent.spec import parse_default_policies
from omnigent.spec.types import FunctionPolicySpec

PROFILE_LABEL = "openengine.profile"
_PROFILES_DIR_ENV = "OMNIGENT_PROFILES_DIR"
# Repo-root /profiles by default. This file is omnigent/omnigent/server/profiles.py,
# so parents[2] is the repo root (which contains profiles/). Override via env for
# installed deployments where the profiles dir is elsewhere.
_DEFAULT_PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"
# Bounded charset + length; used with fullmatch so a trailing newline (which `$`
# would allow) cannot sneak through, and a too-long name can never reach the
# filesystem (where os.stat would raise ENAMETOOLONG).
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ProfileApplicationError(RuntimeError):
    """A requested governance profile could not be attached completely."""


def _profiles_dir() -> Path:
    return Path(os.environ.get(_PROFILES_DIR_ENV) or _DEFAULT_PROFILES_DIR)


def profile_name_from_labels(labels: dict | None) -> str | None:
    """Return the ``openengine.profile`` label value, or ``None`` if absent/empty."""
    if not labels:
        return None
    name = labels.get(PROFILE_LABEL)
    return name if isinstance(name, str) and name else None


def apply_profile_session_policies(session_id, profile_name, policy_store) -> int:
    """Attach every policy in a requested profile, or attach none.

    :raises ProfileApplicationError: If validation, persistence, or rollback fails.
    """
    if policy_store is None:
        raise ProfileApplicationError(
            f"no policy store configured for requested profile {profile_name!r}"
        )
    if not isinstance(profile_name, str) or not _SAFE_NAME.fullmatch(profile_name):
        raise ProfileApplicationError(f"unsafe profile name {profile_name!r}")

    try:
        profiles_dir = _profiles_dir().resolve()
        resolved = (profiles_dir / f"{profile_name}.yaml").resolve()
        resolved.relative_to(profiles_dir)
    except (OSError, ValueError) as exc:
        raise ProfileApplicationError(f"cannot resolve profile {profile_name!r}: {exc}") from exc
    if not resolved.is_file():
        raise ProfileApplicationError(f"profile {profile_name!r} not found at {resolved}")

    try:
        raw = yaml.safe_load(resolved.read_text()) or {}
        policies_block = (raw.get("guardrails") or {}).get("policies")
        specs = parse_default_policies(policies_block)
    except Exception as exc:
        raise ProfileApplicationError(f"failed to parse profile {profile_name!r}: {exc}") from exc

    from omnigent.policies.registry import is_registered_handler  # lazy: avoid import cycle

    validated: list[FunctionPolicySpec] = []
    for spec in specs or []:
        fn = getattr(spec, "function", None)
        if not isinstance(spec, FunctionPolicySpec) or fn is None or not fn.path:
            raise ProfileApplicationError(
                f"profile {profile_name!r} contains an unsupported policy specification"
            )
        if not is_registered_handler(fn.path):
            raise ProfileApplicationError(
                f"handler {fn.path!r} is not registered for policy {spec.name!r}"
            )
        validated.append(spec)
    if not validated:
        raise ProfileApplicationError(
            f"profile {profile_name!r} declares no attachable guardrail policies"
        )

    created_policy_ids: list[str] = []
    try:
        for spec in validated:
            fn = spec.function
            policy_id = f"pol_{uuid.uuid4().hex}"
            policy_store.create(
                policy_id=policy_id,
                session_id=session_id,
                name=spec.name,
                type="python",
                handler=fn.path,
                factory_params=fn.arguments,
            )
            created_policy_ids.append(policy_id)
    except Exception as exc:
        rollback_failures: list[str] = []
        for policy_id in reversed(created_policy_ids):
            try:
                policy_store.delete(policy_id, session_id)
            except Exception as rollback_exc:  # noqa: BLE001 - preserve rollback evidence.
                rollback_failures.append(f"{policy_id}: {rollback_exc}")
        detail = f"failed to attach profile {profile_name!r}: {exc}"
        if rollback_failures:
            detail += "; rollback failures: " + ", ".join(rollback_failures)
        raise ProfileApplicationError(detail) from exc

    return len(created_policy_ids)
