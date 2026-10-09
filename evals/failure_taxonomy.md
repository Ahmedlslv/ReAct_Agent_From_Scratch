# W02D2 — ReAct Failure Taxonomy
**Date:** 2026-06-07  
**Agent:** react-agent (claude-haiku, max_steps=8)  
**Score:** 19/20 passed

---

## Pass Rate by Category

| Category | Passed | Total | Rate |
|----------|--------|-------|------|
| single-tool | 5 | 5 | 100% |
| multi-hop | 4 | 5 | 80% |
| search | 5 | 5 | 100% |
| adversarial | 5 | 5 | 100% |

---

## Failure Mode Taxonomy

| Code | Definition | Count |
|------|-----------|-------|
| `FM-LOOP` | Agent repeated the same Thought+Action without progress | 0 |
| `FM-TOOL-ERR` | Tool returned an error (file not found, calc error, network) | 0 |
| `FM-HALLUC` | Agent made up a tool name or observation | 0 |
| `FM-PREMATURE` | Agent gave Answer too early without using needed tools | 0 |
| `FM-SEARCH` | Search returned no result or wrong result, derailed reasoning | 0 |
| `FM-MAXSTEP` | Hit max_steps without reaching an Answer | 1 |
| `FM-PARSE` | Agent output couldn't be parsed as Thought/Action/Answer | 0 |

---

## Failures by Mode

### `FM-MAXSTEP` — Hit max_steps without reaching an Answer

- **[B5]** `Compute 2^8, then compute 2^4, then tell me the ratio of the first result to the`
  - Expected: 256 / 16 = 16.0
  - Got: Max steps reached without a final answer.
  - Error: none


---

## Key Observations

1. **FM-MAXSTEP is the dominant failure mode.** The agent hit `max_steps=8` on multi-step math chains (B5: 2^8 → 2^4 → ratio). Root cause: model wrote `math.sqrt(x)` without `print()` inside `run_python`, got "no output" feedback, retried with variation, exhausted steps. Not a tool bug — a code generation habit.
2. **Adversarial and search tasks are robust.** 10/10 combined. The agent correctly handled missing files, nonsense queries, `sqrt(-1)` (returned complex number explanation), and DuckDuckGo failures (self-recovered with rephrased queries).
3. **Multi-hop is the weakest category (80%).** Chained math requiring multiple `run_python` calls in sequence exposes a latent issue: the agent sometimes forgets `print()` on intermediate steps, breaking the output capture loop.

## Top 3 Fixes

1. **Add `run_python` to the toolset** — replaces `calc`, handles arbitrary Python expressions including `print()` auto-wrapping for expression-mode inputs. Fixed B5 class failures. (Implemented in W02D4.)
2. **Improve `run_python` description** — instruct the agent to always use `print()` for multi-statement code, and show an example. Reduces FM-MAXSTEP on chained math.
3. **Increase `max_steps` for multi-hop tasks** — 8 steps is tight for 3-tool chains. 12 steps gives the agent room to recover from one bad intermediate step without hitting the ceiling.
