# role_router — Omnigent supervisor port

This is the Omnigent config image for the role-router CO, porting the JS/Node
`copilot-role-router` orchestration to a pure-config Omnigent supervisor.

## What this is

`config.yaml` is a single-file Omnigent supervisor that expresses role-router's
model: a CO that decomposes user requests, delegates to vendor-diverse specialist
sub-agents, and enforces a code-enforced Judge hill-climb gate before accepting work.

role-router (JS) had no programmatic callers — only manifest/docs/persona references.
Phase 2 consolidation replaces its runtime with this config image running on the
Omnigent engine, which already owns the multi-agent engine (supervisor YAML, recursive
sub-agents, persistent async worker sessions, cross-review skill, fanout skill,
runner-side guardrails.policies).

## Sub-agents and vendor diversity

| Agent    | Model                  | Harness       | Role in roles.mjs    |
|----------|------------------------|---------------|----------------------|
| recon    | gemini-3.5-flash       | pi            | read-only discovery  |
| medic    | claude-opus-4-8        | claude-sdk    | diagnosis + plan     |
| engineer | gpt-5.5                | openai-agents | implementation       |
| qa       | gpt-5.4                | openai-agents | independent verify   |
| judge    | gemini-3.1-pro-preview | pi            | two-tier verdict     |
| scribe   | gpt-5.4                | openai-agents | recording + docs     |

Vendor diversity (Anthropic / OpenAI / Gemini) between CO, QA, and Judge is the
point — keep it when re-mapping model names to a specific provider's catalog.

## Two pieces ported from role-router code

1. **Sticky hill-climb budget** (`HILLCLIMB_DEFAULTS` from `repeatable-actions.mjs`):
   `maxRounds=3`, `maxFlatRounds=2`, `minConfidenceDelta=0.05`. Expressed as the
   `hillclimb_budget` guardrails policy in `config.yaml`. The function exists at
   `omnigent.inner.nessie.policies.hillclimb_budget` in `policies.py`.

2. **Deterministic two-tier Judge** (`intentHeuristic` + `mergeVerdicts` from
   `repeatable-actions.mjs`): the hard-tripwire floor (failed tests, broken build,
   not implemented) and anti-gaming verdict merge (worse-of + cannot-clear-tripwire +
   evidence-free PASS -> PARTIAL) are expressed in the judge sub-agent prompt. The
   runner-side policy enforces the sticky halt.

## Skills referenced

The CO prompt references three existing skills from polly's skills directory:

- `examples/polly/skills/investigate/SKILL.md` — delegated read-only investigation
- `examples/polly/skills/fanout/SKILL.md` — parallel implementation in worktrees
- `examples/polly/skills/cross-review/SKILL.md` — independent diff review (the Judge gate)

The empty `skills/judge/` stub is reserved for a role_router-specific judge skill
if the cross-review skill needs to be forked for non-diff verdict flows.

## What is incomplete / needs attention

- Model names use Copilot CLI naming from `roles.mjs` (e.g., `gemini-3.5-flash`,
  `gpt-5.5`, `claude-opus-4-8`). Harnesses that cannot select cross-vendor models
  should map to the nearest native equivalent — keep the vendor-diversity intent
  (Anthropic / OpenAI / Gemini). Check `sys_list_models` at runtime.
- The `hillclimb_budget` policy intercepts `sys_session_send` calls with
  `args.purpose == "review"`. The judge sub-agent dispatches must pass `intent`
  and `confidence`/`gaps` in `args` for plateau detection to work; without them
  the policy still enforces the round budget but cannot detect plateaus.
