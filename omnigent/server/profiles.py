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
dir — no path traversal. Loading is best-effort: a bad/missing profile or an
unregistered handler is logged and skipped, never fatal to session creation.
"""

from __future__ import annotations

import os
import re
import sys
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


def _profiles_dir() -> Path:
    return Path(os.environ.get(_PROFILES_DIR_ENV) or _DEFAULT_PROFILES_DIR)


def profile_name_from_labels(labels: dict | None) -> str | None:
    """Return the ``openengine.profile`` label value, or ``None`` if absent/empty."""
    if not labels:
        return None
    name = labels.get(PROFILE_LABEL)
    return name if isinstance(name, str) and name else None


def apply_profile_session_policies(session_id, profile_name, policy_store) -> int:
    """Attach a stack profile's ``guardrails.policies`` as session policies.

    :param session_id: The conversation/session id to scope the policies to.
    :param profile_name: The profile name from the ``openengine.profile`` label
        (resolves to ``<profiles_dir>/<profile_name>.yaml``).
    :param policy_store: The session policy store (``get_policy_store()``); may be
        ``None`` (then nothing is attached).
    :returns: The number of policies attached.

    Best-effort and fail-safe: an unsafe name, missing/unparseable profile, or
    unregistered handler is logged to stderr and skipped — never raised — so a
    label typo cannot brick session creation. (A profile that fails to attach
    leaves the session UNGOVERNED; the warning is the operator's signal.)
    """
    if policy_store is None:
        print(
            f"openengine.profile: no policy store; cannot apply profile {profile_name!r}",
            file=sys.stderr,
        )
        return 0
    if not isinstance(profile_name, str) or not _SAFE_NAME.fullmatch(profile_name):
        print(f"openengine.profile: rejecting unsafe profile name {profile_name!r}", file=sys.stderr)
        return 0

    # Resolve through symlinks, confirm containment, and stat — all inside one try
    # so ANY filesystem error (e.g. ENAMETOOLONG) or a symlink escape (ValueError
    # from relative_to) is a skip, never a raise into the post-create call site
    # (which would orphan the conversation).
    try:
        profiles_dir = _profiles_dir().resolve()
        resolved = (profiles_dir / f"{profile_name}.yaml").resolve()
        resolved.relative_to(profiles_dir)
        present = resolved.is_file()
    except (OSError, ValueError) as exc:
        print(f"openengine.profile: cannot resolve profile {profile_name!r}: {exc}", file=sys.stderr)
        return 0
    if not present:
        print(f"openengine.profile: profile {profile_name!r} not found at {resolved}", file=sys.stderr)
        return 0

    try:
        raw = yaml.safe_load(resolved.read_text()) or {}
        policies_block = (raw.get("guardrails") or {}).get("policies")
        specs = parse_default_policies(policies_block)
    except Exception as exc:  # noqa: BLE001 — any parse/IO failure is non-fatal
        print(f"openengine.profile: failed to parse profile {profile_name!r}: {exc}", file=sys.stderr)
        return 0

    from omnigent.policies.registry import is_registered_handler  # lazy: avoid import cycle

    attached = 0
    for spec in specs or []:
        fn = getattr(spec, "function", None)
        if not isinstance(spec, FunctionPolicySpec) or fn is None or not fn.path:
            continue
        if not is_registered_handler(fn.path):
            print(
                f"openengine.profile: handler {fn.path!r} not registered; skipping policy {spec.name!r}",
                file=sys.stderr,
            )
            continue
        try:
            policy_store.create(
                policy_id=f"pol_{uuid.uuid4().hex}",
                session_id=session_id,
                name=spec.name,
                type="python",
                handler=fn.path,
                factory_params=fn.arguments,
            )
            attached += 1
        except Exception as exc:  # noqa: BLE001 — e.g. IntegrityError if already applied
            print(
                f"openengine.profile: policy {spec.name!r} not attached for {session_id}: {exc}",
                file=sys.stderr,
            )
    print(
        f"openengine.profile: applied {attached} policy(ies) from {profile_name!r} to {session_id}",
        file=sys.stderr,
    )
    return attached
