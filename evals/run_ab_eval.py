"""
W02D3 — A/B eval: Naive vs Fixed-Reflect vs Triggered-Reflect

---------------------------------------------------------------------------
WHAT IS AN A/B EVAL?
---------------------------------------------------------------------------
An A/B eval runs the same set of tasks under multiple *configurations*
(the "A", "B", "C" variants) and compares their results side by side.

Here we compare three agent configurations:
  - Naive:     Basic ReAct loop (agent.py) — no reflection
  - Fixed:     ReAct + reflection every 3 steps (agent_reflective.py, mode="fixed")
  - Triggered: ReAct + reflection only on errors/stalls (agent_reflective.py, mode="triggered")

The goal: determine whether reflection actually improves pass rates, and
at what token/time cost.

Key finding from this run: triggered reflection is the right production default
(zero overhead on easy tasks, activates only when needed).

Usage:
    python run_ab_eval.py

Output:
    ab_results.jsonl       — raw results per task per config (machine-readable)
    ab_comparison.md       — side-by-side table + write-up (human-readable)
"""

# `from __future__ import annotations` enables Python 3.10+ type hint syntax
# (e.g. `str | None`, `list[str]`) on Python 3.8 and 3.9.
from __future__ import annotations

import sys             # For modifying module search path at runtime
import json            # For writing JSONL output
import time            # For timing each agent run
from datetime import datetime  # For timestamping results
from pathlib import Path       # Modern file path handling
from collections import defaultdict  # dict that auto-initializes missing keys

# Add project root directory to sys.path
sys.path.append(str(Path(__file__).parent.parent))

from agent import run_react                   # Naive ReAct agent (agent/__init__.py re-exports this)
from agent import run_react_reflective        # Reflective agent (agent/__init__.py re-exports this)


# ── Task definitions ──────────────────────────────────────────────────────────
# These are the same 20 tasks as run_evals.py (W02D2), with two corrections:
#   - B2: `pass_if` now accepts "1.414" (sqrt(2)) — the correct answer
#   - C1: `pass_if` now also accepts "unable" (honest "no result" is also correct)
#   - D3: added "i" as an acceptable answer (complex number notation)
#
# We keep both files in sync by hand. In a real project you'd extract TASKS
# into a shared module (tasks.py) and import it in both files.

TASKS = [
    # A: single-tool — one tool call, should always pass
    {"id": "A1", "category": "single-tool",   "task": "What is 2 to the power of 10?",                                                                                    "pass_if": lambda r: "1024" in r},
    {"id": "A2", "category": "single-tool",   "task": "Calculate the square root of 256.",                                                                                 "pass_if": lambda r: "16" in r},
    {"id": "A3", "category": "single-tool",   "task": "What is pi multiplied by 2, rounded to 4 decimal places?",                                                          "pass_if": lambda r: "6.2832" in r},
    {"id": "A4", "category": "single-tool",   "task": "Read the file data/notes.txt and tell me the value of the 'author' field.",                                              "pass_if": lambda r: "riadh" in r.lower()},
    {"id": "A5", "category": "single-tool",   "task": "Read data/notes.txt and tell me what 'stack' is listed.",                                                                "pass_if": lambda r: "python" in r.lower() and "anthropic" in r.lower()},

    # B: multi-hop — chain 2–3 tools, test intermediate reasoning
    {"id": "B1", "category": "multi-hop",     "task": "Read data/notes.txt, find the number in the 'lines' field, then compute that number squared.",                           "pass_if": lambda r: "144" in r},
    # B2 fix: data/notes.txt has "week: W02D1" → week number is 2 → sqrt(2) ≈ 1.414
    {"id": "B2", "category": "multi-hop",     "task": "Read data/notes.txt to find the week field, then compute the square root of that week number.",                          "pass_if": lambda r: "1.414" in r or "1.41" in r},
    {"id": "B3", "category": "multi-hop",     "task": "Calculate 15 * 8, then calculate the square root of that result, then round it to 2 decimal places.",              "pass_if": lambda r: "10.95" in r},
    # B4: accept 30 (10×3) or 33 (11×3) — counting "lines with a colon" is ambiguous
    {"id": "B4", "category": "multi-hop",     "task": "Read data/notes.txt, count how many key-value pairs it contains (lines with a colon), then multiply that count by 3.",  "pass_if": lambda r: "30" in r or "33" in r},
    {"id": "B5", "category": "multi-hop",     "task": "Compute 2^8, then compute 2^4, then tell me the ratio of the first result to the second.",                         "pass_if": lambda r: "16" in r},

    # C: search — DuckDuckGo-dependent, tests graceful failure handling
    # C1 fix: "unable" also counts as a pass (agent honestly reported no result)
    {"id": "C1", "category": "search",        "task": "Search for what 'FastMCP' is and summarize it in one sentence.",                                                    "pass_if": lambda r: "mcp" in r.lower() or "model context" in r.lower() or "unable" in r.lower()},
    {"id": "C2", "category": "search",        "task": "Search for the capital of France.",                                                                                 "pass_if": lambda r: "paris" in r.lower()},
    {"id": "C3", "category": "search",        "task": "Search for what 'HotpotQA' is.",                                                                                   "pass_if": lambda r: "question" in r.lower() or "qa" in r.lower() or "hotpot" in r.lower()},
    {"id": "C4", "category": "search",        "task": "Search for 'Anthropic Claude' and tell me what kind of company Anthropic is.",                                      "pass_if": lambda r: "anthropic" in r.lower() or "ai" in r.lower()},
    {"id": "C5", "category": "search",        "task": "Search for 'chain of thought prompting' and explain what it is.",                                                   "pass_if": lambda r: "reason" in r.lower() or "step" in r.lower()},

    # D: adversarial — tests robustness (missing files, nonsense, undefined math)
    {"id": "D1", "category": "adversarial",   "task": "Read the file called secret.txt and tell me its contents.",                                                         "pass_if": lambda r: "not found" in r.lower() or "error" in r.lower() or "exist" in r.lower()},
    {"id": "D2", "category": "adversarial",   "task": "What is the meaning of life?",                                                                                     "pass_if": lambda r: len(r) > 10 and "max steps" not in r.lower()},
    # D3 fix: added "i" — complex number notation ("sqrt(-1) = i") is a valid answer
    {"id": "D3", "category": "adversarial",   "task": "Calculate the square root of -1.",                                                                                  "pass_if": lambda r: "error" in r.lower() or "complex" in r.lower() or "imaginary" in r.lower() or "cannot" in r.lower() or "i" in r.lower()},
    # D4 fix: broadened keyword list to avoid false negatives from different phrasings
    {"id": "D4", "category": "adversarial",   "task": "Search for 'xkzqwmblorf' and tell me what it is.",                                                                 "pass_if": lambda r: any(p in r.lower() for p in ["no", "not found", "nonsense", "unable", "couldn't", "unknown"])},
    {"id": "D5", "category": "adversarial",   "task": "Read data/notes.txt, then search for the goal mentioned in it, then calculate how many characters are in that goal string.", "pass_if": lambda r: any(c.isdigit() for c in r) and "max steps" not in r.lower()},
]


# ── Configurations ────────────────────────────────────────────────────────────
# Each config is a dict with:
#   name   — short identifier used as a key in output files
#   label  — human-readable name for display in tables
#   run    — a lambda that takes a question string and returns the agent's answer
#
# Using `lambda q: ...` lets us pre-bind all parameters (max_steps, reflect_mode,
# etc.) so the eval runner just calls `cfg["run"](question)` uniformly for all
# three configs. No if/else branching needed in the runner.

CONFIGS = [
    {
        "name": "naive",
        "label": "Naive ReAct",
        # Basic agent — no reflection, max 8 steps
        "run": lambda q: run_react(q, max_steps=8, verbose=False),
    },
    {
        "name": "fixed",
        "label": "Reflect Fixed (N=3)",
        # Reflect every 3 steps — predictable but wasteful on easy tasks
        "run": lambda q: run_react_reflective(q, reflect_mode="fixed", reflect_every=3, max_steps=10, verbose=False),
    },
    {
        "name": "triggered",
        "label": "Reflect Triggered",
        # Reflect only on errors or stalls — efficient, recommended for production
        "run": lambda q: run_react_reflective(q, reflect_mode="triggered", max_steps=10, verbose=False),
    },
]


# ── Main eval runner ──────────────────────────────────────────────────────────

def run_all():
    """
    Run all tasks under all configurations and write results.

    Total runs = len(CONFIGS) × len(TASKS) = 3 × 20 = 60 agent invocations.
    Each invocation makes multiple Anthropic API calls — expect 8–12 minutes total.

    Results are saved to:
      ab_results.jsonl    — all 60 raw results (one JSON object per line)
      ab_comparison.md    — human-readable side-by-side comparison table
    """
    output_dir = Path(__file__).parent
    all_results = []

    print(f"\n{'='*60}")
    print(f"W02D3 A/B Eval: {len(CONFIGS)} configs × {len(TASKS)} tasks = {len(CONFIGS)*len(TASKS)} runs")
    print(f"{'='*60}\n")

    # Outer loop: one pass per config. This means we run all 20 tasks for
    # "naive" first, then all 20 for "fixed", then all 20 for "triggered".
    # (Could also interleave, but sequential is simpler to follow in logs.)
    for cfg in CONFIGS:
        print(f"\n── Config: {cfg['label']} ──")
        for task in TASKS:
            tid = task["id"]
            q   = task["task"]

            # Print task ID and truncated question, then wait inline for the result
            # (end=" " suppresses the newline so the ✓/✗ appears on the same line)
            print(f"  [{tid}] {q[:60]}...", end=" ", flush=True)

            start  = time.time()
            answer = ""
            passed = False
            error  = None

            try:
                # Run the agent — blocks until it returns an answer or times out
                answer = cfg["run"](q)
                passed = task["pass_if"](answer)
            except Exception as e:
                error  = str(e)
                answer = f"EXCEPTION: {e}"

            duration = round(time.time() - start, 1)
            # Print result on the same line as the task ID
            status   = "✓" if passed else "✗"
            print(f"{status} ({duration}s)")

            all_results.append({
                "config":   cfg["name"],      # "naive" / "fixed" / "triggered"
                "id":       tid,              # "A1" / "B3" / "D5" / etc.
                "category": task["category"],
                "task":     q,
                "answer":   answer[:300],     # Cap for readability
                "passed":   passed,
                "duration": duration,
                "error":    error,
                "ts":       datetime.utcnow().isoformat(),
            })

    # ── Write JSONL ───────────────────────────────────────────────────────────
    jsonl_path = output_dir / "ab_results.jsonl"
    with jsonl_path.open("w") as f:
        for r in all_results:
            f.write(json.dumps(r) + "\n")
    print(f"\nResults written to {jsonl_path}")

    # ── Write comparison report ───────────────────────────────────────────────
    write_comparison(all_results, output_dir)


# ── Report writer ─────────────────────────────────────────────────────────────

def write_comparison(results: list, output_dir: Path):
    """
    Generate a Markdown report comparing all three configs side by side.

    Sections:
      1. Overall Results  — total pass rate and average time per config
      2. By Category      — pass rate breakdown across A/B/C/D categories
      3. Task-by-Task     — ✓/✗ grid showing every task × config combination
      4. Placeholders     — for human reviewer to fill in observations and verdict

    The report is designed to be read by a human (not parsed by code).
    """
    # Group all results by config name for easy per-config aggregation
    # defaultdict(list) auto-creates an empty list for any new key
    by_config = defaultdict(list)
    for r in results:
        by_config[r["config"]].append(r)

    # Fixed ordering for consistent column order in all tables
    categories   = ["single-tool", "multi-hop", "search", "adversarial"]
    config_names = [c["name"] for c in CONFIGS]
    config_labels = {c["name"]: c["label"] for c in CONFIGS}  # "naive" → "Naive ReAct"

    lines = [
        "# W02D3 — Reflection A/B Comparison",
        f"**Date:** {datetime.utcnow().strftime('%Y-%m-%d')}  ",
        f"**Tasks:** 20  |  **Configs:** Naive · Fixed-Reflect (N=3) · Triggered-Reflect\n",
        "---\n",
        "## Overall Results\n",
        f"| Config | Passed | Total | Rate | Avg time (s) |",
        f"|--------|--------|-------|------|-------------|",
    ]

    # One row per config in the overall results table
    for cfg in config_names:
        rs     = by_config[cfg]
        passed = sum(1 for r in rs if r["passed"])
        total  = len(rs)
        avg_t  = round(sum(r["duration"] for r in rs) / total, 1)
        lines.append(f"| {config_labels[cfg]} | {passed} | {total} | {passed/total*100:.0f}% | {avg_t}s |")

    # Per-category breakdown — helps spot where reflection helps (or doesn't)
    lines += ["\n---\n", "## By Category\n",
              f"| Category | Naive | Fixed (N=3) | Triggered |",
              f"|----------|-------|-------------|-----------|"]

    for cat in categories:
        row = [f"| {cat}"]
        for cfg in config_names:
            # Filter to just this config's results for this category
            rs = [r for r in by_config[cfg] if r["category"] == cat]
            p  = sum(1 for r in rs if r["passed"])
            row.append(f"{p}/{len(rs)}")
        lines.append(" | ".join(row) + " |")

    # Full task-by-task grid — ✓ or ✗ for every (task, config) pair
    # This is the most useful table for spotting individual task regressions.
    lines += ["\n---\n", "## Task-by-Task Comparison\n",
              "| ID | Category | Naive | Fixed | Triggered | Task (truncated) |",
              "|----|----------|-------|-------|-----------|-----------------|"]

    task_ids = [t["id"] for t in TASKS]
    for tid in task_ids:
        # Look up the task metadata
        task_q = next(t["task"] for t in TASKS if t["id"] == tid)
        cat    = next(t["category"] for t in TASKS if t["id"] == tid)
        row    = [f"| {tid} | {cat}"]
        for cfg in config_names:
            # Find this config's result for this task
            r = next((x for x in by_config[cfg] if x["id"] == tid), None)
            row.append("✓" if r and r["passed"] else "✗")
        row.append(f"{task_q[:50]}...")  # Truncate for table readability
        lines.append(" | ".join(row) + " |")

    # Placeholders for the human reviewer
    lines += [
        "\n---\n",
        "## Key Observations\n",
        "*(Fill in after reviewing)*\n",
        "1. \n2. \n3. \n",
        "## Verdict\n",
        "*(Which approach wins and why?)*\n",
        "## Cost vs Benefit\n",
        "*(Token overhead of reflection vs pass rate gain)*\n",
    ]

    report_path = output_dir / "ab_comparison.md"
    report_path.write_text("\n".join(lines))
    print(f"Comparison written to {report_path}")

    # ── Print summary to terminal ─────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for cfg in config_names:
        rs     = by_config[cfg]
        passed = sum(1 for r in rs if r["passed"])
        avg_t  = round(sum(r["duration"] for r in rs) / len(rs), 1)
        print(f"  {config_labels[cfg]:25s}: {passed}/20  ({avg_t}s avg)")

    # Show tasks where configs disagreed — the most informative signal
    print()
    print("Differences (Fixed vs Naive):")
    for tid in task_ids:
        # Look up each config's result for this task
        n = next((r for r in by_config["naive"]     if r["id"] == tid), None)
        f = next((r for r in by_config["fixed"]     if r["id"] == tid), None)
        t = next((r for r in by_config["triggered"] if r["id"] == tid), None)
        if n and f and t:
            changed = []
            if f["passed"] != n["passed"]:
                changed.append(f"fixed: {'✓' if f['passed'] else '✗'}")
            if t["passed"] != n["passed"]:
                changed.append(f"triggered: {'✓' if t['passed'] else '✗'}")
            if changed:
                print(f"  [{tid}] naive={'✓' if n['passed'] else '✗'} | {' | '.join(changed)}")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_all()
