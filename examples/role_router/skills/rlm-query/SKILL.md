---
name: rlm-query
description: Opt-in long-context RLM query for dense haystacks that normal context handling or compaction cannot cover.
---

# rlm-query

Use `rlm_query` only when the task needs dense access over a large haystack that will not fit normal context handling, or when the user explicitly asks for the RLM long-context path. It is not a session mode and it is not a default retrieval step.

## When To Use

Use `rlm_query` when all of these are true:

- The important evidence is inside a provided corpus, diff bundle, transcript, or source slice.
- The haystack is too large or semantically dense for ordinary prompt context or a short targeted search.
- The question can be stated as one focused answerable query.

Do not use it for ordinary web search, short file inspection, routine summarization, or tasks that require filesystem mutation.

## Call Shape

Call the tool with a focused question and the smallest useful haystack:

```json
{
  "haystack": "<long corpus or list of document strings>",
  "question": "<specific question>",
  "max_depth": 1,
  "max_subcalls": 8
}
```

Keep `max_depth` at `1` unless the user explicitly approves deeper recursion. Keep `max_subcalls` at or below the configured cap. The role-router config runs this through the Docker RLM environment by default and disables both `custom_tools` and `custom_sub_tools`.

## Governance Contract

`rlm_query` is bounded by two runner policies:

- `rlm_subcall_bounds` caps direct RLM invocations per turn and rejects oversized `max_subcalls`.
- `rlm_cost_plan` estimates token exposure before the tool runs, ASKs at warning thresholds, and DENYs above hard caps.

RLM executes model-written Python inside its REPL. The Docker environment is the current containment floor; full kernel-level coverage depends on CLO-10 ActPlane. Do not ask RLM to run commands, modify files, use secrets, or reach external systems.

## Result Handling

Treat the result as evidence, not a final answer by itself. Cite that it came from `rlm_query`, reconcile it with any directly inspected artifacts, and escalate if the tool returns a missing dependency, missing model key, cap, timeout, or sandbox error.
