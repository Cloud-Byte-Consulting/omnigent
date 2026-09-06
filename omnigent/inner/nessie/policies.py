"""Backward-compat shim — policy handler paths in deployed configs still reference
``omnigent.inner.nessie.policies.*``.  Real implementation lives at
``omnigent.policies.builtins.orchestration``.

Fork-only hillclimb / RLM budget factories stay here so existing
``omnigent.inner.nessie.policies.*`` config paths keep working.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from omnigent.policies.builtins.orchestration import *  # noqa: F403
from omnigent.policies.builtins.orchestration import (
    _ALLOW,
    _decision,
    _Json,
    _tool_call,
)
from omnigent.policies.builtins.orchestration import (
    POLICY_REGISTRY as _new_registry,
)

# Re-advertise under the legacy handler paths so the policy registry accepts
# bundles that were deployed before the module was renamed.
_OLD = "omnigent.inner.nessie.policies."
_NEW = "omnigent.policies.builtins.orchestration."


def _legacy_entry(entry: dict[str, object]) -> dict[str, object]:
    handler = entry.get("handler")
    if not isinstance(handler, str):
        raise TypeError("policy registry handler must be a string")
    return {**entry, "handler": handler.replace(_NEW, _OLD), "internal_only": True}


def _run_key(intent: Any) -> str:
    """
    Stable, bounded refine-run key derived from the user's intent.

    Mirrors role-router's ``runKey`` (``core/repeatable-actions.mjs``):
    lower-case, strip punctuation, collapse whitespace, then a djb2 hash
    rendered base-36. Deterministic so the same intent maps to the same
    persisted round counter across turns / reloads.

    :param intent: The task intent text (or anything stringifiable), e.g.
        ``"Fix the flaky auth test"``.
    :returns: A short key like ``"r-1a2b3c"``.
    """
    norm = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", str(intent).lower())).strip()
    h = 5381
    for ch in norm:
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF  # djb2: h * 33 + c, 32-bit
    if h == 0:
        return "r-0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while h:
        h, rem = divmod(h, 36)
        out = digits[rem] + out
    return f"r-{out}"


def _clamp_confidence(raw: Any) -> float | None:
    """
    Coerce a verdict confidence to ``[0.0, 1.0]``, or ``None`` if unusable.

    A non-finite / non-numeric confidence is treated as UNKNOWN (``None``):
    it neither counts as improvement nor updates the running best, avoiding a
    spurious "gain" off a missing field. Mirrors computeDirective's handling.

    :param raw: The reported confidence, e.g. ``0.7`` / ``"0.7"`` / ``None``.
    :returns: A clamped float in ``[0, 1]`` or ``None``.
    """
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val:  # NaN
        return None
    return max(0.0, min(1.0, val))


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine distance (``1 − cosine similarity``) between two equal-length vectors.

    Returns ``1.0`` (maximally far) if either vector is zero-length, so a degenerate
    embedding never reads as "converged".
    """
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


def _resolve_embedder(spec: str):
    """Lazily build a local ``text -> vector`` embedder from a *spec* model name.

    Tries fastembed (light, ONNX, no torch) then sentence-transformers. Returns
    ``None`` if neither is installed — the semantic-convergence stop then stays
    inert rather than raising, so a missing optional dep never breaks the policy.
    """
    try:
        from fastembed import TextEmbedding

        model = TextEmbedding(model_name=spec)
        return lambda text: list(next(iter(model.embed([text]))))
    except Exception:  # noqa: BLE001 - optional embedder import must degrade gracefully.
        pass
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(spec)
        return lambda text: list(model.encode(text))
    except Exception:  # noqa: BLE001 - optional embedder import must degrade gracefully.
        import structlog

        structlog.get_logger(__name__).warning(
            "hillclimb semantic embedder unavailable; convergence stop inert", spec=spec
        )
        return None


def hillclimb_budget(
    *,
    max_rounds: int = 3,
    max_flat_rounds: int = 2,
    min_confidence_delta: float = 0.05,
    embedder: Callable[[str], list[float]] | None = None,
    embedder_model: str | None = None,
    semantic_epsilon: float = 0.06,
    semantic_patience: int = 2,
    semantic_field: str = "output",
    dispatch_tools: tuple[str, ...] = ("sys_session_send",),
    refine_purpose: str = "review",
    state_prefix: str = "hillclimb:",
) -> Callable[[_Json], _Json]:
    """
    Factory: code-enforce a STICKY rework budget on the refine/verify loop.

    Ports role-router's ``computeDirective`` + ``HILLCLIMB_DEFAULTS``
    (``core/repeatable-actions.mjs``) as a function-path policy. The
    supervisor drives a hill-climb loop — verify, route gaps back, re-verify —
    and an over-eager LLM will keep re-verifying the same intent forever. This
    policy makes the cap a HARD limit in code, not a prompt suggestion: the
    per-intent round counter lives in CLOSURE state on the resolved policy
    callable (persisted across turns in-process, no ``reset_turn`` — unlike
    :func:`spawn_bounds`), so the supervisor cannot talk past it. This is the
    contract the runner gate provides: ``RunnerToolPolicyGate`` builds no
    ``session_state`` and drops ``state_updates``, so closure state is the only
    thing that survives between dispatches.

    Each counted dispatch (a *dispatch_tools* call tagged
    ``args.purpose == refine_purpose``) is one verify round. State is keyed by
    a djb2 hash of the intent (:func:`_run_key`) under *state_prefix*, holding
    ``{round, best_confidence, last_gap_count, flat_rounds, terminal}``:

    - **Budget** — DENY once ``round`` would exceed *max_rounds* (default 3).
    - **Plateau** — DENY after *max_flat_rounds* (default 2) consecutive
      non-improving rounds. "Improvement" is concrete: fewer blocking gaps, or
      a confidence gain ``> min_confidence_delta`` (default 0.05) over the best
      so far — never raw confidence drift, which is noisy.
    - **Converged** — DENY once consecutive verify *outputs* stop changing in
      meaning: cosine distance between successive embeddings of *semantic_field*
      stays ``< semantic_epsilon`` for *semantic_patience* rounds. Judge-free
      (one local embed pass per round, no tokens / no LLM); inert unless an
      *embedder* is wired, so the default behavior is unchanged.
    - **Sticky** — once a run hits a terminal directive it STAYS terminal: a
      later re-verify of the same intent re-DENIES rather than resetting the
      cap. Only a brand-new intent (new key, no prior state) starts fresh.

    The stop verdict is **DENY** (not ASK) on purpose: it is a hard refusal
    that tells the supervisor to stop refining and escalate to the user.
    The supervisor passes the merged verdict's ``confidence`` / ``gaps`` in the
    dispatch ``args`` so plateau detection has something to measure; when they
    are absent the policy still enforces the round budget.

    :param max_rounds: Max rework cycles before a forced stop, e.g. ``3``.
    :param max_flat_rounds: Consecutive non-improving rounds tolerated before a
        plateau stop, e.g. ``2``.
    :param min_confidence_delta: Confidence gain that counts as real
        improvement, e.g. ``0.05``.
    :param embedder: Optional ``text -> vector`` function for the judge-free
        semantic-convergence stop. ``None`` (default) disables it — behavior is
        then byte-identical to before. Should be a cheap LOCAL embedder
        (e.g. ``BAAI/bge-small-en-v1.5``), never an LLM call.
    :param embedder_model: Optional model spec resolved to a local embedder at
        build time via :func:`_resolve_embedder` — the config-driven way to wire
        the stop (YAML can't pass a callable). Ignored when *embedder* is given;
        resolves to inert if the embedding dep is not installed.
    :param semantic_epsilon: Max cosine distance between successive outputs that
        still counts as "unchanged", e.g. ``0.06``.
    :param semantic_patience: Consecutive sub-epsilon rounds before the converged
        stop fires, e.g. ``2`` (keep ``>= 2`` to tolerate one noisy round; the
        paper's monotone-descent claim is a measured conjecture, not a theorem).
    :param semantic_field: ``args`` field holding the per-round output text to
        embed, e.g. ``"output"``.
    :param dispatch_tools: Tool names that carry a verify round, e.g.
        ``("sys_session_send",)``. A YAML list is accepted (coerced to a set).
    :param refine_purpose: The ``args.purpose`` value marking a verify round,
        e.g. ``"review"``. Dispatches with any other purpose pass through.
    :param state_prefix: Namespace prefix for the per-run closure key, e.g.
        ``"hillclimb:"``.
    :returns: An evaluator ``fn(event)`` that keeps per-run state in a closure
        (cross-turn, in-process) and returns a V0 decision.
    """
    if embedder is None and embedder_model:
        embedder = _resolve_embedder(embedder_model)
    counted = set(dispatch_tools)
    # Per-intent run state, keyed by state_prefix + _run_key(intent). Lives in
    # this closure for the gate's lifetime — no reset_turn, so the budget is
    # genuinely cross-turn (the runner gate provides no session_state). ponytail:
    # in-process only; a mid-task runner restart resets the budget (role-router
    # used .role-router/state.json). Add file/session persistence only if
    # restart-survival is required.
    runs: dict = {}

    def _evaluate(event: _Json) -> _Json:
        """
        Advance / cap the per-intent hill-climb round counter.

        :param event: V0 ``tool_call`` event for a *dispatch_tools* call; the
            verdict signal lives in ``data.arguments.args``
            (``purpose`` / ``intent`` / ``confidence`` / ``gaps``).
        :returns: ALLOW (CONTINUE_REFINE) while within budget, else DENY
            (STOP_BUDGET / STOP_PLATEAU). Non-refine calls pass through ALLOW.
        """
        args = _tool_call(event, counted)
        if args is None:
            return _ALLOW
        # Sub-agent dispatches nest the task fields under ``args``; fall back to
        # the top-level arguments for flatter callers.
        child = args.get("args") if isinstance(args.get("args"), dict) else args
        if child.get("purpose") != refine_purpose:
            return _ALLOW  # only the verify/refine loop is budgeted

        key = state_prefix + _run_key(child.get("intent") or child.get("input") or "")
        prev = runs.get(key)

        # Sticky terminal: a halted run stays halted — re-verifying the same
        # intent cannot reset the cap. Only a new intent (no prior state) is fresh.
        if isinstance(prev, dict) and prev.get("terminal"):
            return _decision(
                "DENY",
                f"Hill-climb run already halted ({prev['terminal']}). Escalate to "
                "the user; start a NEW task/intent to reset the rework budget.",
            )

        prev = prev if isinstance(prev, dict) else {}
        round_ = int(prev.get("round", 0)) + 1
        best_conf = float(prev.get("best_confidence", 0.0))
        last_gap_count = prev.get("last_gap_count")
        flat = int(prev.get("flat_rounds", 0))

        gaps = child.get("gaps")
        gap_count = len(gaps) if isinstance(gaps, list) else 0
        conf = _clamp_confidence(child.get("confidence"))

        gaps_improved = last_gap_count is None or gap_count < last_gap_count
        conf_improved = conf is not None and conf > best_conf + min_confidence_delta
        improved = gaps_improved or conf_improved
        flat_rounds = 0 if improved else flat + 1
        best_confidence = max(best_conf, conf) if conf is not None else best_conf

        # Judge-free semantic convergence: embed each verify OUTPUT and stop once
        # consecutive outputs stop changing in meaning (cosine distance < epsilon
        # for `semantic_patience` rounds). Inert — and behavior-preserving — when
        # no embedder is wired or the output field is absent. ponytail: one local
        # embed pass per round, zero LLM / zero tokens (the paper's judge-free
        # signal); a bad embedder call degrades to "not converged", never a stop.
        sem_flat = int(prev.get("semantic_flat", 0))
        last_emb = prev.get("last_embedding")
        cur_emb = None
        if embedder is not None:
            text = child.get(semantic_field)
            if isinstance(text, str) and text:
                try:
                    cur_emb = list(embedder(text))
                except Exception:  # noqa: BLE001 - embedder failures keep the stop inert.
                    cur_emb = None
                if cur_emb is not None and isinstance(last_emb, list):
                    try:
                        last = [float(x) for x in last_emb]
                    except (TypeError, ValueError):
                        pass
                    else:
                        sem_flat = (
                            sem_flat + 1
                            if _cosine_distance(cur_emb, last) < semantic_epsilon
                            else 0
                        )

        # Persist this round in the closure BEFORE a terminal verdict; the record
        # is mutated in place on stop, so the sticky terminal flag survives.
        record = {
            "round": round_,
            "best_confidence": best_confidence,
            "last_gap_count": gap_count,
            "flat_rounds": flat_rounds,
            "semantic_flat": sem_flat,
            "last_embedding": cur_emb if cur_emb is not None else last_emb,
        }
        runs[key] = record

        if cur_emb is not None and sem_flat >= semantic_patience:
            record["terminal"] = "STOP_CONVERGED"
            return _decision(
                "DENY",
                f"Output converged — no semantic change for {sem_flat} consecutive "
                f"rounds (cosine < {semantic_epsilon}); accept the result instead "
                "of refining again.",
            )

        if round_ > max_rounds:
            record["terminal"] = "STOP_BUDGET"
            return _decision(
                "DENY",
                f"Reached the rework budget ({max_rounds} cycles) without a PASS; "
                "escalate to the user instead of refining again.",
            )
        if flat_rounds >= max_flat_rounds:
            record["terminal"] = "STOP_PLATEAU"
            return _decision(
                "DENY",
                f"No measurable improvement for {flat_rounds} consecutive rounds "
                "(gaps not shrinking, confidence flat); escalate to the user.",
            )
        return _ALLOW

    return _evaluate


def _coerce_int(value: Any, default: int) -> int:
    """
    Coerce a policy/event value to int, falling back on malformed input.

    :param value: Candidate integer.
    :param default: Value returned when coercion fails.
    :returns: Coerced integer or *default*.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _rlm_haystack_chars(haystack: Any) -> int:
    """
    Return total haystack characters for a potential ``rlm_query`` call.

    :param haystack: ``rlm_query`` haystack arg (string or list of strings).
    :returns: Character count; unsupported shapes count as zero.
    """
    if isinstance(haystack, str):
        return len(haystack)
    if isinstance(haystack, list):
        return sum(len(item) for item in haystack if isinstance(item, str))
    return 0


def _rlm_projected_tokens(
    args: _Json,
    *,
    chars_per_token: float,
    default_max_subcalls: int,
    max_subcalls_per_call: int,
) -> int:
    """
    Estimate worst-case input tokens for one governed ``rlm_query`` call.

    The RLM library cannot report OpenAI cost before the run, and plain
    OpenAI backends do not make ``max_budget`` meaningful. This estimate is
    intentionally simple and deterministic: root input tokens plus one
    additional root-sized slice per allowed sub-call. Operators can tighten
    or loosen it with ``chars_per_token`` and the sub-call caps.

    :param args: ``rlm_query`` tool arguments.
    :param chars_per_token: Character/token approximation, e.g. ``4``.
    :param default_max_subcalls: Default when the tool call omits
        ``max_subcalls``.
    :param max_subcalls_per_call: Hard ceiling used for projection.
    :returns: Projected token count.
    """
    chars = _rlm_haystack_chars(args.get("haystack"))
    question = args.get("question")
    if isinstance(question, str):
        chars += len(question)
    per_root = int((chars / max(chars_per_token, 1.0)) + 0.999)
    requested = _coerce_int(args.get("max_subcalls"), default_max_subcalls)
    requested = max(0, min(requested, max_subcalls_per_call))
    return per_root * (1 + requested)


def rlm_subcall_bounds(
    *,
    max_subcalls_per_call: int = 8,
    max_calls_per_turn: int = 1,
    default_max_subcalls: int = 8,
    tool_names: tuple[str, ...] = ("rlm_query",),
) -> Callable[[_Json], _Json]:
    """
    Factory: bound ``rlm_query`` tool fan-out at the tool-call edge.

    The wrapper enforces the internal RLM sub-call cap inside the RLM
    invocation. This policy prevents the agent from requesting a larger cap
    than the operator permits and limits how many heavyweight RLM invocations
    it can launch in one turn.

    :param max_subcalls_per_call: Maximum ``max_subcalls`` the tool call may
        request. A missing ``max_subcalls`` uses *default_max_subcalls*.
    :param max_calls_per_turn: Maximum direct ``rlm_query`` tool calls in one
        orchestrator turn.
    :param default_max_subcalls: Assumed request cap when omitted.
    :param tool_names: Tool names to count; defaults to ``("rlm_query",)``.
    :returns: Stateful evaluator with ``reset_turn``.
    """
    counted = set(tool_names)
    state = {"calls": 0}

    def _evaluate(event: _Json) -> _Json:
        args = _tool_call(event, counted)
        if args is None:
            return _ALLOW
        requested = _coerce_int(args.get("max_subcalls"), default_max_subcalls)
        if requested > max_subcalls_per_call:
            # Validation DENY — the call never runs, so it must NOT consume the
            # per-turn budget (else a retry-after-fixing-max_subcalls is wrongly blocked).
            return _decision(
                "DENY",
                f"rlm_query requested max_subcalls={requested}, above the cap "
                f"{max_subcalls_per_call}. Retry with max_subcalls <= "
                f"{max_subcalls_per_call}.",
            )
        state["calls"] += 1
        if state["calls"] > max_calls_per_turn:
            return _decision(
                "DENY",
                f"Exceeded {max_calls_per_turn} rlm_query calls this turn; "
                "wait for the current long-context run to finish before launching another.",
            )
        return _ALLOW

    def reset_turn() -> None:
        state["calls"] = 0

    _evaluate.reset_turn = reset_turn  # type: ignore[attr-defined]
    return _evaluate


def rlm_cost_plan(
    *,
    max_estimated_tokens: int,
    ask_thresholds_tokens: tuple[int, ...] = (),
    max_estimated_cost_usd: float | None = None,
    usd_per_1k_tokens: float | None = None,
    chars_per_token: float = 4.0,
    max_subcalls_per_call: int = 8,
    default_max_subcalls: int = 8,
    tool_names: tuple[str, ...] = ("rlm_query",),
) -> Callable[[_Json], _Json]:
    """
    Factory: token-derived budget gate for governed ``rlm_query`` calls.

    RLM's own ``max_budget`` is backend-dependent and is a no-op when the
    backend does not report cost. This policy estimates the worst-case token
    exposure before the tool runs, ASKs at configured checkpoints, and DENYs
    above a hard token or USD projection cap.

    :param max_estimated_tokens: Hard projected-token cap.
    :param ask_thresholds_tokens: Optional soft projected-token checkpoints.
    :param max_estimated_cost_usd: Optional hard USD cap derived from
        ``usd_per_1k_tokens``.
    :param usd_per_1k_tokens: Pricing used for projected USD.
    :param chars_per_token: Character/token approximation.
    :param max_subcalls_per_call: Upper bound used in projection.
    :param default_max_subcalls: Assumed sub-call cap when omitted.
    :param tool_names: Tool names to gate.
    :returns: Evaluator returning ALLOW, ASK, or DENY.
    :raises ValueError: On invalid factory configuration.
    """
    if max_estimated_tokens <= 0:
        raise ValueError("max_estimated_tokens must be > 0")
    if chars_per_token <= 0:
        raise ValueError("chars_per_token must be > 0")
    if max_subcalls_per_call < 0 or default_max_subcalls < 0:
        raise ValueError("sub-call caps must be >= 0")
    thresholds = tuple(sorted({int(t) for t in ask_thresholds_tokens}))
    for threshold in thresholds:
        if not (0 < threshold < max_estimated_tokens):
            raise ValueError("ask_thresholds_tokens must be within (0, max_estimated_tokens)")
    if (max_estimated_cost_usd is None) != (usd_per_1k_tokens is None):
        raise ValueError("max_estimated_cost_usd and usd_per_1k_tokens must be set together")
    if max_estimated_cost_usd is not None and max_estimated_cost_usd <= 0:
        raise ValueError("max_estimated_cost_usd must be > 0")
    if usd_per_1k_tokens is not None and usd_per_1k_tokens <= 0:
        raise ValueError("usd_per_1k_tokens must be > 0")

    counted = set(tool_names)
    # ponytail: approval high-water mark in CLOSURE state (like rlm_subcall_bounds /
    # hillclimb_budget). The runner gate provides no session_state and drops
    # state_updates, so a session-state ASK never persists and re-fires every turn. The
    # re-invoke after a human approves IS the approval — record the crossed threshold so
    # a re-eval at/below it proceeds; reset_turn re-arms per turn.
    approved = {"tokens": 0}

    def _evaluate(event: _Json) -> _Json:
        args = _tool_call(event, counted)
        if args is None:
            return _ALLOW
        projected = _rlm_projected_tokens(
            args,
            chars_per_token=chars_per_token,
            default_max_subcalls=default_max_subcalls,
            max_subcalls_per_call=max_subcalls_per_call,
        )
        if projected > max_estimated_tokens:
            return _decision(
                "DENY",
                f"rlm_query projected {projected} tokens, above hard cap "
                f"{max_estimated_tokens}. Narrow the haystack or reduce max_subcalls.",
            )
        if max_estimated_cost_usd is not None and usd_per_1k_tokens is not None:
            projected_cost = projected * usd_per_1k_tokens / 1000.0
            if projected_cost > max_estimated_cost_usd:
                return _decision(
                    "DENY",
                    f"rlm_query projected ${projected_cost:.4f}, above hard cap "
                    f"${max_estimated_cost_usd:.4f}. Narrow the haystack or use a cheaper model.",
                )
        if thresholds:
            crossed = max((t for t in thresholds if projected >= t), default=None)
            if crossed is not None and crossed > approved["tokens"]:
                approved["tokens"] = crossed
                return {
                    "result": "ASK",
                    "reason": (
                        f"rlm_query projects {projected} tokens and crossed the "
                        f"{crossed}-token warning threshold. Continue?"
                    ),
                }
        return _ALLOW

    def reset_turn() -> None:
        approved["tokens"] = 0

    _evaluate.reset_turn = reset_turn  # type: ignore[attr-defined]
    return _evaluate


# ── Registry ─────────────────────────────────────────────────────────────────

POLICY_REGISTRY = [_legacy_entry(entry) for entry in _new_registry] + [
    {
        "handler": "omnigent.inner.nessie.policies.hillclimb_budget",
        "kind": "factory",
        "name": "Cap Hill-Climb Rework Rounds (sticky budget)",
        "description": "Code-enforces a per-intent rework budget on the refine/verify loop "
        "(max rounds + plateau detection, persisted in session_state and sticky once halted) "
        "so the supervisor cannot refine the same intent forever",
    },
    {
        "handler": "omnigent.inner.nessie.policies.rlm_subcall_bounds",
        "kind": "factory",
        "name": "Limit RLM Query Fan-Out",
        "description": "Limits direct rlm_query invocations per turn and rejects requests "
        "whose internal max_subcalls exceeds the operator cap.",
    },
    {
        "handler": "omnigent.inner.nessie.policies.rlm_cost_plan",
        "kind": "factory",
        "name": "RLM Token-Derived Cost Plan",
        "description": "Estimates rlm_query token exposure from haystack size and allowed "
        "sub-call fan-out; ASKs at warning thresholds and DENYs above hard token or "
        "projected-USD caps.",
    },
]
