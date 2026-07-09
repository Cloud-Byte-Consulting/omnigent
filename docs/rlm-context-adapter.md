# Bounded RLM context adapter

`omnigent.context_adapters.rlm.RLMContextAdapter` is the optional boundary
between an Omnigent session and a Recursive Language Model worker. Omnigent
continues to own the parent `task_ref`, `session_id`, model allow-list, token
and cost budgets, timeout, fallback, and result verification.

The adapter backend is injected as an async callable. This is intentional:
`rlm-context-agent` currently installs agent/command assets and does not expose
a stable network service. A deployment may supply an in-process, subprocess,
or remote backend only after it implements the typed request/result contract.

Before dispatch, document content is deterministically truncated to the
parent's character budget. The worker always receives `content_trust` set to
`untrusted` and an empty capability set, so retrieved text cannot acquire tool
authority. A result is accepted only when:

- parent identity is unchanged;
- the model is explicitly allowed;
- token, cost, and output budgets are satisfied; and
- every non-empty summary has exact, in-bounds citations to dispatched text.

Timeouts, backend errors, and rejected results use the injected primary-model
fallback. Cancellation propagates instead of starting fallback work. The
fallback receives the same bounded request and a stable reason code, which the
session runtime can record with its normal model-usage and audit events.

This module does not persist an independent conversation, call tools, resolve
policy `ASK` decisions, or choose models. Those remain Omnigent responsibilities.
