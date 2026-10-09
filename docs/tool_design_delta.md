# W02D4 — Tool Design Refactor: Before / After

**Reference:** https://www.anthropic.com/engineering/writing-tools-for-agents  
**Date:** 2026-06-03

---

## Principles Applied

From the Anthropic guide, 5 principles were evaluated against `tools.py` (v1):

| Principle | v1 Status | Change in v2 |
|-----------|-----------|--------------|
| Right tool design (no redundancy) | ✗ `calc` + `run_python` overlap — agent confused which to use | Removed `calc`. `run_python` covers all math + more. |
| Naming / namespacing | ✗ `search` too generic; `file_read` inconsistent style | Renamed `search` → `web_search`. Clear, unambiguous. |
| Meaningful context from tools | ✗ Errors terse. No file metadata. No retry guidance. | Errors now actionable. `file_read` returns line count + truncation notice. |
| Token efficiency | ✗ `file_read` hard-caps silently. No agent hint to paginate. | `file_read` tells agent total size + how to request more. |
| Agent-facing descriptions | ✗ Docstrings written for developers, not agents. | Rewritten as explicit instructions: when to use, when NOT to use, what to do on failure. |

---

## Before / After: Tool Inventory

| v1 (`tools.py`) | v2 (`tools_v2.py`) | Change |
|-----------------|-------------------|--------|
| `calc(expression)` | *(removed)* | Redundant with `run_python`. Overlap caused agent to hesitate. |
| `file_read(path)` | `file_read(file_path, max_chars=3000)` | Renamed param, added `max_chars`, richer response. |
| `search(query)` | `web_search(query)` | Renamed. Clearer purpose. Better failure messages. |
| `run_python(code)` | `run_python(code)` | Improved error messages and description. |

**Tool count: 4 → 3** (removed redundancy)

---

## Before / After: Error Messages

### `file_read` — file not found

**v1:**
```
file_read error: file not found — 'secret.txt'
```

**v2:**
```
file_read error: file not found — 'secret.txt'
Files in '.': ab_comparison.md, ab_results.jsonl, agent.py, agent_reflective.py,
              eval_results.jsonl, failure_taxonomy.md, notes.txt, ...
Check the file name and try again.
```

→ Agent can now self-correct without a second blind attempt.

---

### `search` — no result

**v1:**
```
No direct result found for 'FastMCP'. Try rephrasing or using file_read for local data.
```

**v2:**
```
No result found for 'FastMCP'.
Suggestions:
  - Try a shorter or rephrased query
  - Use file_read if the information is in a local file
  - Answer from your own knowledge if you are confident
```

→ Three concrete recovery strategies instead of one vague hint.

---

### `run_python` — silent success with no output

**v1:**
```
Success (no stdout)
```

**v2:**
```
run_python ran successfully but produced no output.
If you expected a result, wrap it in print(), e.g. print(result).
```

→ Agent knows exactly what to fix without guessing.

---

## Before / After: `file_read` Response

### v1 — raw content, silent truncation at 2000 chars:
```
project: react-agent
status: building
lines: 12
...
```
*(truncated silently — agent doesn't know)*

### v2 — content + metadata + explicit truncation notice:
```
project: react-agent
status: building
lines: 12
...

[File metadata: 12 lines, 287 chars total]
```
*(no truncation here — file fits. If file were larger:)*
```
[TRUNCATED: showing first 3000 of 8500 chars.
 Call file_read with max_chars=8500 to read the full file.]
```

→ Agent knows file size upfront and can decide to request more if needed.

---

## Before / After: Tool Descriptions

### `search` (v1) — developer-facing:
```python
"""
Search for information using DuckDuckGo Instant Answer API (no key needed).
Falls back to a clear "no result" message if the API returns nothing useful.
For richer results, swap in a Brave/Serper API key.
"""
```

### `web_search` (v2) — agent-facing:
```python
"""
Search the web for factual information about a topic.

Use this when you need external knowledge not available in local files.
Prefer specific, focused queries over broad ones.

If the search returns no result, try:
  1. A shorter, simpler query
  2. A different angle on the same topic
  3. Acknowledging the limitation and answering from your own knowledge

Do NOT call web_search for math — use run_python instead.
Do NOT call web_search for local file contents — use file_read instead.
"""
```

→ Agent knows **when to use it**, **when NOT to use it**, and **what to do on failure**. No ambiguity.

---

## Predicted Hallucinated-Tool-Call Delta

| Failure | v1 risk | v2 risk | Reason |
|---------|---------|---------|--------|
| Agent calls `calc` for counting | High | Zero | `calc` removed |
| Agent calls `search` for math | Medium | Low | `web_search` description explicitly says "Do NOT use for math" |
| Agent calls unknown tool | Low | Lower | Error message now lists available tools + spelling check hint |
| Agent retries blind on file not found | Medium | Low | Error now lists directory contents |
| Agent stuck after search failure | Medium | Low | 3 concrete recovery strategies returned |
| Agent confused by silent truncation | Medium | Zero | Truncation notice + how to request more |

**Estimated reduction in hallucinated/wrong tool calls: ~40%** on the 20-task eval set.  
*(To verify: re-run `run_evals.py` with `tools_v2.py` swapped in and compare error observations in JSONL.)*

---

## How to Swap In v2

```python
# In agent.py and agent_reflective.py, change:
from tools import TOOLS, execute_tool

# To:
from tools_v2 import TOOLS, execute_tool
```

Also update the system prompt tool list in `SYSTEM_PROMPT`:
```
- web_search(query) — search the web for factual information
- file_read(file_path) — read a local file
- run_python(code) — execute Python for math, counting, data processing
```
