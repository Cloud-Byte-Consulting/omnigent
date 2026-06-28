"""Opt-in Recursive Language Model query tool.

``rlm_query`` wraps the research ``rlms`` package behind Omnigent's
ordinary tool and policy surface. It is intentionally not registered by
default: an agent must opt in via ``tools.builtins`` before the model can
invoke it.
"""

from __future__ import annotations

import importlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from omnigent.tools.base import Tool, ToolContext

_PATCH_LOCK = threading.RLock()

_DEFAULT_BACKEND = "openai"
_DEFAULT_MODEL = "gpt-5.4"
_DEFAULT_ENVIRONMENT = "docker"
_DEFAULT_MAX_DEPTH = 1
_DEFAULT_MAX_ITERATIONS = 12
_DEFAULT_MAX_SUBCALLS = 8
_DEFAULT_MAX_CONCURRENT_SUBCALLS = 2
_DEFAULT_MAX_TIMEOUT_SECONDS = 900.0
_DEFAULT_MAX_OUTPUT_CHARS = 20000
_DEFAULT_MAX_HAYSTACK_CHARS = 2_000_000
_DEFAULT_MAX_QUESTION_CHARS = 20000
# ponytail: runtime token backstop (RLM's own cap). The OPA rlm_cost_plan token
# projection is the primary cost gate; this just bounds a runaway run. Operator-tune.
_DEFAULT_MAX_TOKENS = 5_000_000
_INSTALL_HINT = "Install the optional RLM dependency with: pip install 'rlms[docker]'"

# The ONLY fields a model may set per call (the get_schema() params). Every other
# field — environment, backend, docker_image, the caps — is operator-config only.
_MODEL_ARGS = frozenset(
    {"haystack", "question", "model", "max_depth", "max_subcalls", "max_timeout_seconds"}
)


class RlmQueryTool(Tool):
    """Run a governed recursive long-context query through RLM."""

    def __init__(
        self,
        config: dict[str, str] | None = None,
        *,
        rlm_loader: Callable[[], type[Any]] | None = None,
    ) -> None:
        """Create the tool.

        :param config: Optional ``tools.builtins`` config from the agent
            spec. Values arrive as strings from the YAML parser.
        :param rlm_loader: Test hook for injecting a fake RLM class.
        """
        self._config = dict(config or {})
        self._rlm_loader = rlm_loader or _load_rlm_class

    @classmethod
    def name(cls) -> str:
        """:returns: ``"rlm_query"``."""
        return "rlm_query"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Opt-in long-context question answering using Recursive Language Models. "
            "Use only when the supplied haystack is too large or too dense for normal "
            "context handling; the call runs with bounded recursive sub-calls."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI-format schema for ``rlm_query``."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "haystack": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}},
                            ],
                            "description": (
                                "Long context to inspect. Prefer a string for one corpus "
                                "or an array of document strings for a partitioned corpus."
                            ),
                        },
                        "question": {
                            "type": "string",
                            "description": "The focused question to answer from the haystack.",
                        },
                        "model": {
                            "type": "string",
                            "description": "Optional model override. Defaults to tool config.",
                        },
                        "max_depth": {
                            "type": "integer",
                            "description": (
                                "Maximum RLM recursion depth. Default 1; values above 1 "
                                "should be explicitly justified."
                            ),
                        },
                        "max_subcalls": {
                            "type": "integer",
                            "description": (
                                "Maximum internal rlm_query sub-calls for this invocation."
                            ),
                        },
                        "max_timeout_seconds": {
                            "type": "number",
                            "description": "Wall-clock timeout passed to RLM.",
                        },
                    },
                    "required": ["haystack", "question"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Run a bounded RLM completion and return JSON.

        :param arguments: JSON with ``haystack`` and ``question``.
        :param ctx: Tool context. Currently unused; included for Tool API
            compatibility.
        :returns: JSON string with either ``answer`` and ``metadata`` or
            ``error`` and ``message``.
        """
        del ctx
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return _json_error("invalid_arguments", f"arguments must be JSON: {exc}")

        try:
            request, error = _build_request(args, self._config)
        except (TypeError, ValueError) as exc:
            return _json_error("invalid_arguments", str(exc))
        if error is not None:
            return error

        assert request is not None
        if _missing_backend_key(request.backend):
            return _json_error(
                "missing_model_key",
                f"Backend {request.backend!r} requires {_backend_key_name(request.backend)} "
                "in the environment.",
                install_hint=_INSTALL_HINT,
            )

        try:
            RLM = self._rlm_loader()
        except ImportError:
            return _json_error(
                "missing_rlm_library",
                "RLM library is not installed.",
                install_hint=_INSTALL_HINT,
            )
        except Exception as exc:
            return _json_error("rlm_import_failed", f"Could not load RLM library: {exc}")

        limiter = _SubcallLimiter(max_subcalls=request.max_subcalls)
        try:
            answer = _run_completion(RLM, request, limiter)
        except Exception as exc:
            return _json_error(
                "rlm_query_failed",
                f"{type(exc).__name__}: {exc}",
                metadata=limiter.metadata(),
            )

        answer = _trim(answer, request.max_output_chars)
        return json.dumps(
            {
                "answer": answer,
                "metadata": {
                    **limiter.metadata(),
                    "backend": request.backend,
                    "model": request.model,
                    "environment": request.environment,
                    "max_depth": request.max_depth,
                    "max_iterations": request.max_iterations,
                    "max_timeout_seconds": request.max_timeout_seconds,
                    "custom_tools": "disabled",
                    "custom_sub_tools": "disabled",
                },
            },
            ensure_ascii=False,
        )


@dataclass(frozen=True)
class _RlmRequest:
    haystack: str | list[str]
    question: str
    backend: str
    model: str
    environment: str
    max_depth: int
    max_iterations: int
    max_subcalls: int
    max_concurrent_subcalls: int
    max_timeout_seconds: float
    max_tokens: int | None
    max_budget_usd: float | None
    max_output_chars: int
    environment_kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class _SubcallLimiter:
    """Invocation-local RLM sub-call counter."""

    max_subcalls: int
    count: int = 0
    capped: bool = False
    capped_at: int | None = None

    def register(self, depth: int, model: str, prompt: str) -> bool:
        """Count one sub-call and return whether it may proceed."""
        self.count += 1
        if self.count > self.max_subcalls:
            self.capped = True
            self.capped_at = self.count
            return False
        return True

    def callback(self, depth: int, model: str, preview: str) -> None:
        """Telemetry callback passed to RLM's public hook."""
        del depth, model, preview
        # Enforcement happens in the guarded _subcall patch because upstream RLM
        # swallows exceptions from on_subcall_start.

    def metadata(self) -> dict[str, Any]:
        """Return JSON metadata for the invocation."""
        return {
            "subcalls_started": self.count,
            "max_subcalls": self.max_subcalls,
            "subcall_cap_exceeded": self.capped,
            "subcall_capped_at": self.capped_at,
        }


def _load_rlm_class() -> type[Any]:
    """Import and return ``rlm.RLM`` lazily."""
    module = importlib.import_module("rlm")
    RLM = getattr(module, "RLM", None)
    if RLM is None:
        raise ImportError("module 'rlm' does not export RLM")
    return RLM


def _run_completion(RLM: type[Any], request: _RlmRequest, limiter: _SubcallLimiter) -> str:
    """Instantiate RLM, guard its sub-call dispatch, and run completion."""
    backend_kwargs = {"model_name": request.model}
    rlm = RLM(
        backend=request.backend,
        backend_kwargs=backend_kwargs,
        environment=request.environment,
        environment_kwargs=request.environment_kwargs,
        max_depth=request.max_depth,
        max_iterations=request.max_iterations,
        max_budget=request.max_budget_usd,
        max_timeout=request.max_timeout_seconds,
        max_tokens=request.max_tokens,
        max_concurrent_subcalls=request.max_concurrent_subcalls,
        on_subcall_start=limiter.callback,
        custom_tools={},
        custom_sub_tools={},
        verbose=False,
        persistent=False,
    )

    with _guarded_rlm_subcalls(RLM, limiter):
        result = rlm.completion(request.haystack, root_prompt=request.question)
    return str(getattr(result, "response", ""))


class _guarded_rlm_subcalls:
    """Temporarily guard ``RLM._subcall`` for this invocation."""

    def __init__(self, RLM: type[Any], limiter: _SubcallLimiter) -> None:
        self._RLM = RLM
        self._limiter = limiter
        self._original: Any = None

    def __enter__(self) -> None:
        _PATCH_LOCK.acquire()
        self._original = getattr(self._RLM, "_subcall", None)
        if self._original is None:
            # Fail closed: the fan-out cap is enforced ONLY via this patch, so a
            # renamed/removed upstream _subcall must stop the run, not silently run
            # ungoverned. Release the lock first — __exit__ won't run if __enter__ raises.
            _PATCH_LOCK.release()
            raise RuntimeError(
                "RLM._subcall is unavailable; refusing to run rlm_query without the "
                "sub-call fan-out cap."
            )
        limiter = self._limiter
        original = self._original

        def guarded(instance: Any, prompt: str, model: str | None = None) -> Any:
            depth = int(getattr(instance, "depth", 0)) + 1
            resolved_model = _resolve_subcall_model(instance, model)
            if not limiter.register(depth, resolved_model, prompt):
                return _make_error_completion(
                    root_model=resolved_model,
                    prompt=prompt,
                    response=(
                        f"Error: RLM sub-call cap exceeded "
                        f"({limiter.max_subcalls} per tool invocation)."
                    ),
                )
            return original(instance, prompt, model)

        self._RLM._subcall = guarded  # type: ignore[attr-defined]

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._original is not None:
            self._RLM._subcall = self._original  # type: ignore[attr-defined]
        _PATCH_LOCK.release()


def _resolve_subcall_model(instance: Any, model: str | None) -> str:
    """Best-effort model name for cap metadata and error completions."""
    if model:
        return model
    backend_kwargs = getattr(instance, "backend_kwargs", None)
    if isinstance(backend_kwargs, dict):
        value = backend_kwargs.get("model_name")
        if isinstance(value, str) and value:
            return value
    return "unknown"


def _make_error_completion(*, root_model: str, prompt: str, response: str) -> Any:
    """Build an RLM-like completion object without requiring the library in tests."""
    try:
        types_mod = importlib.import_module("rlm.core.types")
        usage = types_mod.UsageSummary(model_usage_summaries={})
        return types_mod.RLMChatCompletion(
            root_model=root_model,
            prompt=prompt,
            response=response,
            usage_summary=usage,
            execution_time=0.0,
        )
    except Exception:
        return SimpleNamespace(
            root_model=root_model,
            prompt=prompt,
            response=response,
            usage_summary=None,
            execution_time=0.0,
        )


def _build_request(
    args: dict[str, Any],
    config: dict[str, str],
) -> tuple[_RlmRequest | None, str | None]:
    """Validate user/config input and build an internal request."""
    haystack = args.get("haystack")
    if not isinstance(haystack, str) and not (
        isinstance(haystack, list) and all(isinstance(item, str) for item in haystack)
    ):
        return None, _json_error(
            "invalid_arguments",
            "'haystack' must be a string or list of strings.",
        )
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        return None, _json_error("invalid_arguments", "'question' must be a non-empty string.")

    # Security: invoke() does not validate args against the schema, so a model could
    # smuggle out-of-schema fields (environment, backend, docker_image, caps) via extra
    # JSON keys — e.g. a prompt-injected haystack flipping environment to the unsandboxed
    # 'local' REPL. Drop everything but the declared schema params; the rest is config-only.
    args = {k: v for k, v in args.items() if k in _MODEL_ARGS}

    backend = _str_arg(args, config, "backend", _DEFAULT_BACKEND)
    model = _str_arg(args, config, "model", _DEFAULT_MODEL)
    environment = _str_arg(args, config, "environment", _DEFAULT_ENVIRONMENT)
    allow_local = _bool_config(config, "allow_local_environment", False)
    if environment == "local" and not allow_local:
        return None, _json_error(
            "unsafe_environment",
            "rlm_query only allows environment='docker' by default. "
            "Set allow_local_environment only for trusted local experiments.",
        )
    if environment not in {"docker", "local"}:
        return None, _json_error(
            "unsupported_environment",
            "rlm_query currently supports environment='docker' or explicitly allowed 'local'.",
        )

    max_haystack_chars = _int_arg(args, config, "max_haystack_chars", _DEFAULT_MAX_HAYSTACK_CHARS)
    if _text_len(haystack) > max_haystack_chars:
        return None, _json_error(
            "haystack_too_large",
            f"haystack has {_text_len(haystack)} chars, above cap {max_haystack_chars}.",
        )
    max_question_chars = _int_arg(args, config, "max_question_chars", _DEFAULT_MAX_QUESTION_CHARS)
    if len(question) > max_question_chars:
        return None, _json_error(
            "question_too_large",
            f"question has {len(question)} chars, above cap {max_question_chars}.",
        )

    request = _RlmRequest(
        haystack=haystack,
        question=question,
        backend=backend,
        model=model,
        environment=environment,
        max_depth=_positive_int_arg(args, config, "max_depth", _DEFAULT_MAX_DEPTH),
        max_iterations=_positive_int_arg(args, config, "max_iterations", _DEFAULT_MAX_ITERATIONS),
        max_subcalls=_nonnegative_int_arg(args, config, "max_subcalls", _DEFAULT_MAX_SUBCALLS),
        max_concurrent_subcalls=_positive_int_arg(
            args, config, "max_concurrent_subcalls", _DEFAULT_MAX_CONCURRENT_SUBCALLS
        ),
        max_timeout_seconds=_positive_float_arg(
            args, config, "max_timeout_seconds", _DEFAULT_MAX_TIMEOUT_SECONDS
        ),
        max_tokens=_positive_int_arg(args, config, "max_tokens", _DEFAULT_MAX_TOKENS),
        max_budget_usd=_optional_positive_float_arg(args, config, "max_budget_usd"),
        max_output_chars=_positive_int_arg(
            args,
            config,
            "max_output_chars",
            _DEFAULT_MAX_OUTPUT_CHARS,
        ),
        environment_kwargs=_environment_kwargs(args, config),
    )
    return request, None


def _environment_kwargs(args: dict[str, Any], config: dict[str, str]) -> dict[str, Any]:
    """Build RLM environment kwargs from explicit, safe config fields."""
    image = _str_arg(args, config, "docker_image", "")
    return {"image": image} if image else {}


def _missing_backend_key(backend: str) -> bool:
    """Return whether a known backend lacks its required key."""
    low = backend.lower()
    if low == "openai":
        return not bool(os.environ.get("OPENAI_API_KEY"))
    if low == "openrouter":
        return not bool(os.environ.get("OPENROUTER_API_KEY"))
    if low == "portkey":
        return not bool(os.environ.get("PORTKEY_API_KEY"))
    return False


def _backend_key_name(backend: str) -> str:
    """Return the env var expected for a known backend."""
    low = backend.lower()
    if low == "openrouter":
        return "OPENROUTER_API_KEY"
    if low == "portkey":
        return "PORTKEY_API_KEY"
    return "OPENAI_API_KEY"


def _str_arg(args: dict[str, Any], config: dict[str, str], key: str, default: str) -> str:
    """Resolve a string from call args, config, or default."""
    value = args.get(key, config.get(key, default))
    return str(value) if value is not None else default


def _int_arg(args: dict[str, Any], config: dict[str, str], key: str, default: int) -> int:
    """Resolve an integer from call args, config, or default."""
    return int(args.get(key, config.get(key, default)))


def _positive_int_arg(args: dict[str, Any], config: dict[str, str], key: str, default: int) -> int:
    """Resolve a positive integer."""
    value = _int_arg(args, config, key, default)
    if value <= 0:
        raise ValueError(f"{key} must be > 0, got {value!r}")
    return value


def _nonnegative_int_arg(
    args: dict[str, Any],
    config: dict[str, str],
    key: str,
    default: int,
) -> int:
    """Resolve a non-negative integer."""
    value = _int_arg(args, config, key, default)
    if value < 0:
        raise ValueError(f"{key} must be >= 0, got {value!r}")
    return value


def _optional_positive_int_arg(
    args: dict[str, Any],
    config: dict[str, str],
    key: str,
) -> int | None:
    """Resolve an optional positive integer."""
    raw = args.get(key, config.get(key))
    if raw in (None, ""):
        return None
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{key} must be > 0, got {value!r}")
    return value


def _positive_float_arg(
    args: dict[str, Any],
    config: dict[str, str],
    key: str,
    default: float,
) -> float:
    """Resolve a positive float."""
    value = float(args.get(key, config.get(key, default)))
    if value <= 0:
        raise ValueError(f"{key} must be > 0, got {value!r}")
    return value


def _optional_positive_float_arg(
    args: dict[str, Any],
    config: dict[str, str],
    key: str,
) -> float | None:
    """Resolve an optional positive float."""
    raw = args.get(key, config.get(key))
    if raw in (None, ""):
        return None
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{key} must be > 0, got {value!r}")
    return value


def _bool_config(config: dict[str, str], key: str, default: bool) -> bool:
    """Resolve a bool from string config."""
    raw = config.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _text_len(value: str | list[str]) -> int:
    """Return total character length of a haystack."""
    if isinstance(value, str):
        return len(value)
    return sum(len(item) for item in value)


def _trim(text: str, max_chars: int) -> str:
    """Trim output to the configured max chars."""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 34)] + "\n[truncated by rlm_query output cap]"


def _json_error(code: str, message: str, **extra: Any) -> str:
    """Serialize an error response."""
    payload = {"error": code, "message": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)
