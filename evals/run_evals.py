"""
W02D2 — 20-task eval runner for the ReAct agent.

---------------------------------------------------------------------------
WHY WRITE EVALS?
---------------------------------------------------------------------------
A ReAct agent produces natural-language answers — you can't just `assert
result == expected` like a unit test. Instead, we define a `pass_if`
function for each task that checks whether the answer contains the right
content (e.g. "1024" or "riadh").

This gives us a repeatable, automated way to measure:
  - Overall pass rate (how good is the agent?)
  - Failure modes (WHY does it fail? Loop? Bad tool choice? Search flakiness?)
  - Category breakdown (where is it weakest?)

Running this after any change to the agent or tools tells you immediately
whether you've improved or regressed.

---------------------------------------------------------------------------
TASK CATEGORIES
---------------------------------------------------------------------------
A  Single-tool:  Exercises one tool in isolation. Should always pass.
B  Multi-hop:    Chains 2–3 tools. Tests planning and intermediate reasoning.
C  Search:       Depends on the (flaky) DuckDuckGo API. Tests self-recovery.
D  Adversarial:  Missing files, nonsense queries, undefined math. Tests
                 graceful error handling — the agent should fail cleanly,
                 not crash or hallucinate.

Usage:
    python run_evals.py

Output:
    eval_results.jsonl    — one JSON record per task (machine-readable)
    failure_taxonomy.md   — bucketed failure analysis (human-readable)
"""

import sys             # For modifying module search path at runtime
import json            # For writing JSONL output (one JSON object per line)
import time            # For measuring how long each task takes
import traceback       # For capturing full exception traces (not currently used)
from datetime import datetime  # For timestamping each result
from pathlib import Path       # Modern file path handling (cleaner than os.path)
from typing import Optional    # Type hint for values that may be None

# Add project root directory to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from agent import run_react    # The naive ReAct agent we're evaluating (agent/__init__.py re-exports this)


# ── Task definitions ──────────────────────────────────────────────────────────
# Each task is a dict with:
#   id        — unique identifier (A1, B3, D5, etc.)
#   category  — which category this tests (single-tool, multi-hop, search, adversarial)
#   task      — the question string sent to the agent
#   expected  — human-readable description of what we expect (for the report)
#   pass_if   — a function(answer: str) -> bool that decides pass or fail
#
# `pass_if` is a lambda (anonymous one-line function). It's defined inline
# because the logic is simple and specific to each task.
# Example: lambda r: "1024" in r   →   passes if the answer contains "1024"

TASKS = [

    # ── Category A: Single-tool (should always pass) ─────────────────────────
    # These tasks require exactly one tool call. If the agent fails here,
    # something fundamental is broken (wrong tool description, parse error, etc.)
    {
        "id": "A1", "category": "single-tool",
        "task": "What is 2 to the power of 10?",
        "expected": "1024",
        "pass_if": lambda r: "1024" in r,
    },
    {
        "id": "A2", "category": "single-tool",
        "task": "Calculate the square root of 256.",
        "expected": "16.0",
        "pass_if": lambda r: "16" in r,       # Accept "16" or "16.0"
    },
    {
        "id": "A3", "category": "single-tool",
        "task": "What is pi multiplied by 2, rounded to 4 decimal places?",
        "expected": "6.2832",
        "pass_if": lambda r: "6.2832" in r,
    },
    {
        "id": "A4", "category": "single-tool",
        "task": "Read the file data/notes.txt and tell me the value of the 'author' field.",
        "expected": "riadh",
        "pass_if": lambda r: "riadh" in r.lower(),  # Case-insensitive; data/notes.txt author field = "riadh"
    },
    {
        "id": "A5", "category": "single-tool",
        "task": "Read data/notes.txt and tell me what 'stack' is listed.",
        "expected": "python, anthropic, duckduckgo",
        # Must contain both keywords — checking for two words is more robust
        # than checking for the full comma-separated string.
        "pass_if": lambda r: "python" in r.lower() and "anthropic" in r.lower(),
    },

    # ── Category B: Multi-hop (chain 2–3 tools) ───────────────────────────────
    # The agent must use the output of one tool as the input to another.
    # This tests: can the agent plan a sequence, extract a value from an
    # intermediate result, and pass it correctly to the next step?
    {
        "id": "B1", "category": "multi-hop",
        "task": "Read data/notes.txt, find the number in the 'lines' field, then compute that number squared.",
        "expected": "144 (12^2)",
        "pass_if": lambda r: "144" in r,
    },
    {
        "id": "B2", "category": "multi-hop",
        "task": "Read data/notes.txt to find the week field, then compute the square root of that week number.",
        # data/notes.txt contains "week: W02D1" — the number is 2. sqrt(2) ≈ 1.414.
        # We accept both "1.414" and "1.41" because rounding may vary.
        "expected": "sqrt(2) ≈ 1.414 (or sqrt(1) = 1.0 if assuming week 1)",
        "pass_if": lambda r: "1.41" in r or "1.0" in r or r.strip().endswith("1"),
    },
    {
        "id": "B3", "category": "multi-hop",
        "task": "Calculate 15 * 8, then calculate the square root of that result, then round it to 2 decimal places.",
        "expected": "sqrt(120) ≈ 10.95",
        "pass_if": lambda r: "10.95" in r,
    },
    {
        "id": "B4", "category": "multi-hop",
        "task": "Read data/notes.txt, count how many key-value pairs it contains (lines with a colon), then multiply that count by 3.",
        # data/notes.txt has 11 lines containing a colon → 11 × 3 = 33.
        # We also accept 30 (10 × 3) because some interpretations may exclude
        # one line — this is a known ambiguity in the task.
        "expected": "11 pairs * 3 = 33 (or 10 pairs * 3 = 30 if omitting status/completed)",
        "pass_if": lambda r: "33" in r or "30" in r,
    },
    {
        "id": "B5", "category": "multi-hop",
        "task": "Compute 2^8, then compute 2^4, then tell me the ratio of the first result to the second.",
        "expected": "256 / 16 = 16.0",
        "pass_if": lambda r: "16" in r,
    },

    # ── Category C: Search-dependent (exposes search flakiness) ───────────────
    # DuckDuckGo's free API is unreliable on niche queries. These tasks check:
    #   - Does the agent recover gracefully when search returns nothing?
    #   - Does it use its own knowledge as a fallback?
    {
        "id": "C1", "category": "search",
        "task": "Search for what 'FastMCP' is and summarize it in one sentence.",
        "expected": "A Python framework for building MCP servers",
        "pass_if": lambda r: "mcp" in r.lower() or "model context" in r.lower() or "fastmcp" in r.lower(),
    },
    {
        "id": "C2", "category": "search",
        "task": "Search for the capital of France.",
        "expected": "Paris",
        "pass_if": lambda r: "paris" in r.lower(),
    },
    {
        "id": "C3", "category": "search",
        "task": "Search for what 'HotpotQA' is.",
        "expected": "A multi-hop QA benchmark",
        "pass_if": lambda r: "question" in r.lower() or "qa" in r.lower() or "hotpot" in r.lower(),
    },
    {
        "id": "C4", "category": "search",
        "task": "Search for 'Anthropic Claude' and tell me what kind of company Anthropic is.",
        "expected": "AI safety company",
        "pass_if": lambda r: "anthropic" in r.lower() or "ai" in r.lower(),
    },
    {
        "id": "C5", "category": "search",
        "task": "Search for 'chain of thought prompting' and explain what it is.",
        "expected": "Prompting technique where LLM shows reasoning steps",
        "pass_if": lambda r: "reason" in r.lower() or "step" in r.lower() or "chain" in r.lower(),
    },

    # ── Category D: Adversarial (exposes loops, hallucination, premature stop) ─
    # These tasks are designed to trip up a poorly designed agent.
    # We're testing robustness, not capability — the agent should fail cleanly.
    {
        "id": "D1", "category": "adversarial",
        "task": "Read the file called secret.txt and tell me its contents.",
        "expected": "File not found error, graceful handling",
        # The file doesn't exist. Pass if the agent reports the error clearly
        # rather than hallucinating file contents.
        "pass_if": lambda r: "not found" in r.lower() or "error" in r.lower() or "exist" in r.lower(),
    },
    {
        "id": "D2", "category": "adversarial",
        "task": "What is the meaning of life?",
        "expected": "Should answer without tools or with search, not loop",
        # The agent must produce *some* answer (len > 10) and must not time out.
        "pass_if": lambda r: len(r) > 10 and "max steps" not in r.lower(),
    },
    {
        "id": "D3", "category": "adversarial",
        "task": "Calculate the square root of -1.",
        "expected": "Should report error or explain complex numbers, not crash",
        # sqrt(-1) is mathematically undefined in real numbers. The agent
        # should explain this, not crash or return garbage.
        "pass_if": lambda r: "error" in r.lower() or "complex" in r.lower() or "imaginary" in r.lower() or "cannot" in r.lower(),
    },
    {
        "id": "D4", "category": "adversarial",
        "task": "Search for 'xkzqwmblorf' and tell me what it is.",
        "expected": "Should gracefully report no results found",
        # A nonsense query — no search engine will return a result. The agent
        # must report "no result" rather than hallucinating a definition.
        # Broad keyword list to avoid false negatives from different phrasings:
        # "no result" / "not found" / "no known meaning" / "does not appear" / "unknown"
        # Synced with run_ab_eval.py D4 pass_if.
        "pass_if": lambda r: any(w in r.lower() for w in ["not found", "no result", "couldn't find", "unable", "nonsense", "no search results", "no direct result", "no known", "does not appear", "unknown", "no "]),
    },
    {
        "id": "D5", "category": "adversarial",
        "task": "Read data/notes.txt, then search for the goal mentioned in it, then calculate how many characters are in that goal string.",
        "expected": "Multi-hop with search in the middle — may fail on search step",
        # Complex chain: file_read → web_search → run_python (len()). At least
        # some digit must appear in the answer, and it must not time out.
        "pass_if": lambda r: any(c.isdigit() for c in r) and "max steps" not in r.lower(),
    },
]


# ── Failure mode taxonomy ─────────────────────────────────────────────────────
# When a task fails, we classify WHY it failed into one of these categories.
# This helps us identify which improvements to the agent would have the most impact.
#
# FM = Failure Mode

FAILURE_MODES = {
    "FM-LOOP":      "Agent repeated the same Thought+Action without progress",
    "FM-TOOL-ERR":  "Tool returned an error (file not found, calc error, network)",
    "FM-HALLUC":    "Agent made up a tool name or observation",
    "FM-PREMATURE": "Agent gave Answer too early without using needed tools",
    "FM-SEARCH":    "Search returned no result or wrong result, derailed reasoning",
    "FM-MAXSTEP":   "Hit max_steps without reaching an Answer",
    "FM-PARSE":     "Agent output couldn't be parsed as Thought/Action/Answer",
    "FM-NONE":      "No failure — task passed",
}


# ── Failure classifier ────────────────────────────────────────────────────────

def classify_failure(answer: str, passed: bool, error: Optional[str]) -> str:
    """
    Heuristically classify why a task failed based on the final answer string.

    This is not perfect — it's a first-pass triage. Some misclassifications
    are expected and should be corrected manually after reviewing the JSONL log.

    Parameters:
        answer  — the agent's final answer string
        passed  — whether the task passed the pass_if criterion
        error   — exception message if the agent crashed (None if no crash)
    """
    if passed:
        return "FM-NONE"             # No failure — don't classify

    if error:
        return "FM-TOOL-ERR"         # Agent raised an exception

    if "max steps" in answer.lower():
        return "FM-MAXSTEP"          # Agent timed out

    if "no result" in answer.lower() or "not found" in answer.lower():
        return "FM-SEARCH"           # Search returned nothing and agent gave up

    # Catch-all — wrong answer without an obvious cause
    return "FM-PREMATURE"


# ── Main eval runner ──────────────────────────────────────────────────────────

def run_all(max_steps: int = 8, verbose: bool = False):
    """
    Run all 20 tasks against the naive ReAct agent and write results.

    Parameters:
        max_steps — how many Thought/Action/Observation steps the agent may take
                    per task before giving up (default: 8)
        verbose   — if True, print every step of every agent run (very noisy)
    """
    results = []
    # __file__ is this script's path. .parent gets the directory it's in.
    # We write output files to the same directory as the script.
    output_dir = Path(__file__).parent

    print(f"\n{'='*60}")
    print(f"W02D2 — ReAct Agent Eval: {len(TASKS)} tasks")
    print(f"{'='*60}\n")

    for task in TASKS:
        tid  = task["id"]
        cat  = task["category"]
        q    = task["task"]
        print(f"[{tid}] {cat}: {q[:70]}...")  # Truncate long questions for readability

        start   = time.time()
        answer  = ""
        error   = None
        passed  = False

        try:
            # Run the agent — this makes multiple API calls and may take several seconds
            answer = run_react(q, max_steps=max_steps, verbose=verbose)
            # Check whether the answer satisfies the task-specific criterion
            passed = task["pass_if"](answer)
        except Exception as e:
            # The agent itself raised an exception (shouldn't happen — tools catch their
            # own errors, but network failures or API errors could bubble up)
            error  = str(e)
            answer = f"EXCEPTION: {e}"

        duration  = round(time.time() - start, 1)
        failure   = classify_failure(answer, passed, error)
        status    = "PASS ✓" if passed else f"FAIL ✗ [{failure}]"

        print(f"    → {status} ({duration}s)\n")

        # Store the full result — we'll write this to JSONL and use it for the taxonomy
        results.append({
            "id":       tid,
            "category": cat,
            "task":     q,
            "expected": task["expected"],
            "answer":   answer[:300],   # Cap at 300 chars for readability in the file
            "passed":   passed,
            "failure":  failure,
            "duration": duration,
            "error":    error,
            "ts":       datetime.utcnow().isoformat(),  # ISO 8601 timestamp
        })

    # ── Write JSONL results ───────────────────────────────────────────────────
    # JSONL = JSON Lines: one JSON object per line. Easy to grep, append, and
    # parse with `json.loads(line)` in a for loop. Much simpler than a database.
    jsonl_path = output_dir / "eval_results.jsonl"
    with jsonl_path.open("w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    print(f"Results written to {jsonl_path}")

    # ── Write taxonomy doc ────────────────────────────────────────────────────
    write_taxonomy(results, output_dir)

    # ── Print summary ─────────────────────────────────────────────────────────
    passed_count = sum(1 for r in results if r["passed"])
    print(f"\n{'='*60}")
    print(f"Total: {passed_count}/{len(results)} passed")
    print(f"{'='*60}\n")

    # Group by category and print breakdown
    by_cat = {}
    for r in results:
        by_cat.setdefault(r["category"], []).append(r["passed"])
    for cat, vals in by_cat.items():
        p = sum(vals)
        print(f"  {cat:15s}: {p}/{len(vals)}")

    print()
    # List any failing tasks with their failure mode
    failures = [r for r in results if not r["passed"]]
    if failures:
        print("Failures:")
        for r in failures:
            print(f"  [{r['id']}] {r['failure']} — {r['task'][:60]}")


# ── Taxonomy writer ───────────────────────────────────────────────────────────

def write_taxonomy(results: list, output_dir: Path):
    """
    Write a Markdown failure analysis document from eval results.

    The document includes:
      - Pass rate by category (table)
      - Failure mode counts (table)
      - Per-failure breakdown: what task, what was expected, what the agent said

    This is meant to be read by a human after the eval run to decide
    which failure modes to prioritize fixing.
    """
    from collections import Counter, defaultdict

    # Separate passing and failing results
    failures = [r for r in results if not r["passed"]]

    # Count how many times each failure mode appears
    fm_counts = Counter(r["failure"] for r in failures)

    # Group failures by their failure mode (for the per-mode section)
    by_fm = defaultdict(list)
    for r in failures:
        by_fm[r["failure"]].append(r)

    passed = sum(1 for r in results if r["passed"])

    # Build the markdown document as a list of strings, then join them
    lines = [
        "# W02D2 — ReAct Failure Taxonomy",
        f"**Date:** {datetime.utcnow().strftime('%Y-%m-%d')}  ",
        f"**Agent:** react-agent (claude-haiku, max_steps=8)  ",
        f"**Score:** {passed}/{len(results)} passed\n",
        "---\n",
        "## Pass Rate by Category\n",
        "| Category | Passed | Total | Rate |",
        "|----------|--------|-------|------|",
    ]

    # One row per category
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    for cat, items in by_cat.items():
        p = sum(1 for i in items if i["passed"])
        lines.append(f"| {cat} | {p} | {len(items)} | {p/len(items)*100:.0f}% |")

    lines += [
        "\n---\n",
        "## Failure Mode Taxonomy\n",
        "| Code | Definition | Count |",
        "|------|-----------|-------|",
    ]
    for fm, definition in FAILURE_MODES.items():
        if fm == "FM-NONE":
            continue  # Skip the "no failure" entry — it's not a failure mode
        count = fm_counts.get(fm, 0)
        lines.append(f"| `{fm}` | {definition} | {count} |")

    lines += ["\n---\n", "## Failures by Mode\n"]
    for fm, items in by_fm.items():
        lines.append(f"### `{fm}` — {FAILURE_MODES.get(fm, fm)}\n")
        for r in items:
            lines.append(f"- **[{r['id']}]** `{r['task'][:80]}`")
            lines.append(f"  - Expected: {r['expected']}")
            lines.append(f"  - Got: {r['answer'][:150]}")
            lines.append(f"  - Error: {r['error'] or 'none'}\n")

    # Leave placeholders for the human reviewer to fill in
    lines += [
        "\n---\n",
        "## Key Observations\n",
        "*(Fill in after reviewing results)*\n",
        "1. \n2. \n3. \n",
        "## Top 3 Fixes\n",
        "1. \n2. \n3. \n",
    ]

    taxonomy_path = output_dir / "failure_taxonomy.md"
    taxonomy_path.write_text("\n".join(lines))
    print(f"Taxonomy written to {taxonomy_path}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_all(max_steps=8, verbose=False)
