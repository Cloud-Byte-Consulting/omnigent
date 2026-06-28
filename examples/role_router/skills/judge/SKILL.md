---
name: judge
description: Deterministic two-tier verdict gate — heuristic tripwire floor + independent LLM review merged worst-of; turns blocking issues into fix-tasks and loops until clean (bounded by the hill-climb budget).
---

# judge — deterministic two-tier verdict gate

The implementer never clears its own work. Judge runs a HEURISTIC scan first
(objective signals — no LLM call), then dispatches an INDEPENDENT,
different-vendor reviewer for semantic depth. The final verdict is the
WORSE of the two — the LLM can worsen but cannot clear a hard tripwire.

## Procedure

### Tier 1 — heuristic scan (synchronous, no LLM)

Before touching the work output, scan the work-completed text for hard
failure signals. These are objective, non-overridable:

| Pattern (case-insensitive) | Label |
|---|---|
| `tests? (still )?fail(ing\|ed\|s)?` | tests failed |
| `build (failed\|broke\|broken\|is broken)` | build failed |
| `not (yet )?implemented` | not implemented |
| `(could ?n't\|unable to\|cannot\|can't\|was unable to) (complete\|finish\|resolve\|fix\|implement\|build\|run\|compile\|reproduce\|verify\|get \w+ to work)` | could not complete |
| `(uncaught\|unhandled) (exception\|error)\|stack ?trace\|traceback\|threw (an? )?(exception\|error)` | runtime error |
| `failed to (complete\|build\|run\|compile\|start\|load\|parse\|connect\|deploy\|install\|pass)` | operation failed |

If ANY pattern matches → record the matched labels as `hardTripwires[]`.
The heuristic verdict is **FAIL** (confidence 0.90) and cannot be overridden by
Tier 2. Skip to the merge step with an LLM verdict of FAIL as well — there is
no point reviewing broken work; re-dispatch the implementer first.

If no hard tripwires fire, score keyword coverage:

- Tokenise the original intent (drop stop-words, length ≤ 2).
- Check what fraction of those tokens appear anywhere in the work-completed text.
- coverage ≥ 0.70 → heuristic PASS (confidence 0.60 + coverage × 0.30, capped at 0.85)
- coverage ≥ 0.40 → heuristic PARTIAL (confidence 0.55)
- coverage < 0.40 → heuristic FAIL (confidence 0.50)

Soft signals (advisory only, never override the verdict):
- coverage < 0.50 → "low intent keyword coverage"
- work-completed text < 20 characters → "sparse work description"

### Tier 2 — LLM review (independent, different vendor)

Dispatch a **different-vendor** sub-agent as reviewer (Claude implemented it →
`codex` or `pi`; Codex → `claude_code` or `pi`; Pi → `claude_code` or
`codex`). Use a task-scoped title like `judge-<task_slug>`, not the raw vendor
name. Pass the diff/artifacts + original intent + acceptance contract only —
never the implementer's transcript or worktree:

```
sys_session_send(
  agent="<different-vendor>",
  title="judge-<task_slug>",
  args={
    purpose: "review",
    input: "<diff or artifact text>\n\n<original intent>\n\n<acceptance contract>",
    output: "<diff or artifact text ONLY — the changing part; feeds the semantic-convergence stop>",
    instructions: "
      You are Judge — a two-tier intent-verification gate. A heuristic tier has
      already produced a preliminary verdict; you are the semantic tier.

      PROCEDURE (follow in order, no skipping):

      1. INTENT SUMMARY: State in ONE sentence what the work actually did.

      2. GATHER EVIDENCE: Use read-only inspection tools (view, grep, glob, gh
         for PR/diff metadata) to inspect real artifacts — changed files, diff,
         recorded test/build output. Do NOT rely on the work-completed description
         alone. You are read-only; inspect files directly.

      3. VERDICT: PASS = work genuinely satisfies the request; PARTIAL = partially;
         FAIL = incomplete/off-target. Quote the original intent, list what was
         verified, explain any gap. Tag each evidence item:
         [verified-live] [artifact] [inferred].

      4. DOCS ASSESSMENT: Determine the documentation action required — one of:
         - 'none': internal fix/refactor/build change — no docs needed.
         - 'update': existing section needs updating → return suggestedFiles + suggestedSection.
         - 'create': new surface needs a doc → return proposedFile.
         - 'ask_user': new user-facing capability where even the file is uncertain →
           return askQuestion with 2-3 options.
         Err toward 'ask_user' over silence for any new user-facing capability.

      5. FINALIZE: Call judge_finalize with structured fields:
         verificationId, verdict, recommendation, confidence (0-1), riskLevel,
         reasoning, evidence[], gaps[], docsAction and its fields.
         Do NOT answer in prose — the gate only completes when judge_finalize runs.
         evidence[] must cite SPECIFIC files/lines/tests/tool-results, not generic claims.

      Hard floor (you may NOT override):
        Objective failures the heuristic flagged (failed tests, broken build,
        'not implemented') are a hard floor. You may always WORSEN a verdict or
        RAISE risk, but you cannot clear a hard tripwire to PASS.
    "
  }
)
```

Emit the `sys_session_send` call in the SAME turn — do not end a turn having
only announced "I'll dispatch Judge". End your turn; collect the structured
report with `sys_read_inbox` when it returns.

### Merge — anti-gaming worst-of

```
merged.verdict  = worse of (heuristic.verdict, llm.verdict)
merged.riskLevel = max of (heuristic.riskLevel, llm.riskLevel)
merged.evidence  = union(heuristic.evidence, llm.evidence, hardTripwire citations)
merged.gaps      = union(heuristic.gaps, llm.gaps)

if hardTripwires.length > 0:
    merged.verdict = FAIL  # non-overridable floor, confidence bumped to ≥ 0.90

if merged.verdict == PASS and llm.evidence contains no concrete citation
   (no file path, line ref, test name, diff ref, commit hash, PR number):
    merged.verdict = PARTIAL  # evidence-free PASS downgraded
```

Verdict ranking for "worse-of": FAIL > PARTIAL > PASS.

### Hill-climb budget (persisted per intent, sticky)

The round counter is keyed by a stable djb2 hash of the normalised intent
(`runKey`). The cap is CODE-ENFORCED — it cannot be talked past.

Budget defaults (from `HILLCLIMB_DEFAULTS`):

| Setting | Value |
|---|---|
| `maxRounds` | 3 rework cycles before forced human escalation |
| `maxFlatRounds` | 2 consecutive non-improving rounds before plateau escalation |
| `minConfidenceDelta` | 0.05 confidence gain required to count as real improvement |

"Improvement" = gaps[] shrank OR confidence rose ≥ `minConfidenceDelta`.
Raw confidence drift alone does not count.

Stop conditions (priority order):

| Directive | Trigger | Action |
|---|---|---|
| `STOP_ACCEPT` | verdict accepted (PASS, high confidence) | Done — mark task ready. |
| `STOP_REVIEW` | Judge requests human review | Escalate; do not auto-refine. |
| `STOP_BUDGET` | `round > maxRounds` | Escalate to user with specifics; mark terminal. |
| `STOP_PLATEAU` | `flatRounds >= maxFlatRounds` | Escalate to user with specifics; mark terminal. |
| `STOP_CONVERGED` | consecutive `output`s stop changing in meaning (opt-in embedder) | Accept the result — it has settled; stop refining. |
| `CONTINUE_REFINE` | otherwise | Route `gaps[]` back to the implementer; re-verify. |

**Sticky terminal**: once a run is halted (`STOP_BUDGET` or `STOP_PLATEAU`),
it stays halted. Re-verifying the same intent key does not reset the cap — only
a new intent (new `runKey`) starts fresh. A noncompliant orchestrator cannot
cycle through verdicts to escape the budget.

### Routing gaps to the implementer

For each **blocking** gap: add a fix-task to the registry scoped to the same
worktree, then send the concrete fixes back to the SAME implementer conversation
via `sys_session_send` — reuse the original `agent` + `title` (or `session_id`)
with `purpose: "implement"`. A new title would spawn a fresh worker with no
memory of the task. Then loop to Tier 1.

Non-blocking issues go in the registry as follow-ups; they do not block.

When gates are green AND merged verdict is PASS with zero blocking gaps: mark
the task ready in the registry and leave it for the human.

## Notes

- Judge requires a reviewer from a DIFFERENT vendor than the implementer. If only
  one vendor is available, you CANNOT run independent cross-vendor review — say
  so explicitly and pull in the human at the plan gate.
- Give the reviewer ONLY diff + contract + original intent. Never the
  implementer's transcript or worktree. Cross-vendor independence is the point.
- Hard tripwires fire on the WORK-COMPLETED TEXT, not the diff. Pattern-match
  the summary/description the implementer produced, not the code itself — the
  code is what the LLM tier inspects.
- The heuristic is fast and cheap; it runs even on hard-tripwire hits so the
  merged evidence record is complete.
- Confidence from LLMs is noisy and uncalibrated. Improvement is measured by
  concrete deltas (fewer gaps, or confidence gain ≥ `minConfidenceDelta`),
  not raw confidence drift alone.
