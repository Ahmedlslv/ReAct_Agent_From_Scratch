"""
ReAct Agent with Reflection — two modes:

  Mode A: FIXED — reflect every N steps (default N=3)
  Mode B: TRIGGERED — reflect only on tool error or stall (same action repeated)

---------------------------------------------------------------------------
WHAT IS REFLECTION?
---------------------------------------------------------------------------
A plain ReAct agent (agent.py) just keeps looping Thought → Action → Observation.
If it gets stuck in a loop or goes down the wrong path, it has no mechanism to
notice and change strategy.

Reflection adds a periodic "pause and self-critique" step. The model is asked:
  - Are you making progress?
  - Have you seen any errors?
  - Should you try a different approach?

This is inspired by the Reflexion paper (Shinn et al. 2023), which showed that
LLMs that critique their own intermediate steps outperform those that don't on
multi-step reasoning tasks.

---------------------------------------------------------------------------
TWO REFLECTION MODES
---------------------------------------------------------------------------
Fixed:     Inject a reflection prompt every N steps regardless of what happened.
           Simple, predictable, but wastes tokens on easy tasks that are
           already working correctly.

Triggered: Inject a reflection prompt ONLY when something goes wrong:
           - The last observation contained an error
           - The agent is "stalling" (repeating the same action twice in a row)
           More efficient — zero overhead on smooth runs, fires on genuine problems.

Verdict: Triggered is the right production default.

Reference: Yao et al. ReAct (2023) + Shinn et al. Reflexion (2023)

Usage:
    from agent_reflective import run_react_reflective

    # Fixed interval (reflect every 3 steps)
    run_react_reflective(question, reflect_mode="fixed", reflect_every=3)

    # Error/stall triggered (recommended)
    run_react_reflective(question, reflect_mode="triggered")
"""

# ── Imports ───────────────────────────────────────────────────────────────────
from __future__ import annotations  # Enables newer type hint syntax on Python <3.10

import re           # Regular expressions — for parsing Thought/Action/Answer/Reflect
import sys          # Command-line argument access
import anthropic    # Anthropic Python SDK — sends requests to Claude
from tools import execute_tool  # Single dispatch function for all tools


# ── Prompts ───────────────────────────────────────────────────────────────────

# The main system prompt — identical to agent.py.
# Tells the model what role to play and what format to follow.
SYSTEM_PROMPT = """You are a ReAct agent. You solve problems by interleaving Thought, Action, and Observation steps.

You have access to these tools:
- web_search(query) — search the web for factual information; do NOT use for math or local files
- run_python(code) — execute Python for any math, counting, data processing, or string operations
- file_read(file_path) — read a local file; do NOT use for web content or computation

Always follow this exact format for each step:

Thought: <your reasoning about what to do next>
Action: <tool_name>(<argument>)

After each Action, you will receive an Observation. Use it to continue reasoning.

When you have the final answer, output:
Thought: I now have the final answer.
Answer: <your final answer>

Do not skip steps. Do not call multiple tools in one Action.
Do not make up observations — wait for real ones.
"""

# The reflection prompt — injected mid-conversation to trigger self-critique.
# It asks the model to look back at what it has done and decide whether to
# change strategy before continuing.
#
# Note: we ask for a "Reflect:" prefix so we can detect and log it separately,
# and then immediately ask for the next Thought + Action (or a final Answer).
# This keeps the conversation flowing without adding a separate API call.
REFLECT_PROMPT = """Before continuing, pause and reflect on your trajectory so far.

Review what you have done:
- What was the original question?
- What tools have you called and what did they return?
- Are you making real progress toward the answer, or repeating yourself?
- Have you hit any errors? What caused them?
- Is your current approach the best one, or should you try something different?

After reflecting, decide:
- If you are on track: continue with the next Thought + Action.
- If you are stuck or repeating: change strategy. Try a different tool, a different argument, or reason from what you already know.
- If you already have enough information: produce the Answer now.

Output your reflection as:
Reflect: <your self-critique and revised plan>

Then immediately continue with:
Thought: <next step>
Action: <tool_name>(<argument>)

OR if you have the answer:
Thought: I now have the final answer.
Answer: <your final answer>
"""


# ── Parsers ───────────────────────────────────────────────────────────────────
# These mirror agent.py's parsers. We duplicate them here so this file is
# self-contained (importable without depending on agent.py).

def parse_action(text: str) -> tuple[str, str] | None:
    """
    Extract (tool_name, argument) from a line like:
        Action: web_search(capital of France)
    Returns None if no valid Action line is found.
    """
    match = re.search(r"Action:\s*(\w+)\((.+?)\)\s*$", text, re.MULTILINE)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return None


def parse_answer(text: str) -> str | None:
    """
    Extract the final answer from a line like:
        Answer: Paris is the capital of France.
    re.DOTALL allows the answer to span multiple lines.
    Returns None if not present.
    """
    match = re.search(r"Answer:\s*(.+)", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def parse_reflect_and_continue(text: str) -> tuple[str | None, str | None, tuple | None]:
    """
    Parse a reflection response that may contain:
        Reflect: <critique text>
        Thought: <next reasoning>
        Action: <tool(arg)>    ← OR →    Answer: <final>

    Returns a 3-tuple: (reflect_text, answer, action)
    Any element may be None if not present in the response.
    """
    reflect = None
    # Match everything after "Reflect:" up to the next "Thought:" or end of string
    rm = re.search(r"Reflect:\s*(.+?)(?=Thought:|$)", text, re.DOTALL)
    if rm:
        reflect = rm.group(1).strip()

    answer = parse_answer(text)
    action = parse_action(text)
    return reflect, answer, action


# ── Stall detection ───────────────────────────────────────────────────────────

def is_stalled(history: list[tuple[str, str]], window: int = 2) -> bool:
    """
    Detect if the agent is repeating the same action without progress.

    `history` is a list of (tool_name, argument) tuples — one entry per
    action the agent has taken so far.

    We look at the last `window` entries. If they are all identical
    (same tool AND same argument), the agent is stuck in a loop.

    Example — stalled:
        history = [("web_search", "FastMCP"), ("web_search", "FastMCP")]
        is_stalled(history, window=2) → True

    Example — not stalled:
        history = [("web_search", "FastMCP"), ("web_search", "MCP server")]
        is_stalled(history, window=2) → False
    """
    if len(history) < window:
        return False                    # Not enough history to judge
    recent = history[-window:]          # Last `window` actions
    return len(set(recent)) == 1        # True if all are identical (set collapses duplicates)


# ── Core loop ─────────────────────────────────────────────────────────────────

def run_react_reflective(
    question: str,
    reflect_mode: str = "fixed",   # "fixed" or "triggered"
    reflect_every: int = 3,         # Only used in fixed mode: reflect every N steps
    max_steps: int = 10,            # Safety cap to prevent infinite loops
    verbose: bool = True,
) -> str:
    """
    ReAct loop with optional mid-loop reflection.

    This extends the basic agent.py loop with one addition: before (or instead
    of) a normal step, we may inject the REFLECT_PROMPT to make the model
    critique its progress and potentially change strategy.

    reflect_mode="fixed":
        Reflect at steps 3, 6, 9, ... (every `reflect_every` steps).
        Predictable but wasteful — reflections on easy tasks add no value.

    reflect_mode="triggered":
        Reflect only when something bad happened (tool error or stall).
        Efficient — zero overhead on smooth runs.

    Returns the final answer string, or a timeout message if max_steps is hit.
    """
    client = anthropic.Anthropic()

    # Start with the user's question
    messages = [{"role": "user", "content": question}]

    step = 0
    action_history: list[tuple[str, str]] = []  # Track (tool, arg) pairs for stall detection
    reflect_count = 0                            # How many times we've reflected (for logging)

    if verbose:
        print(f"\n{'='*60}")
        print(f"Question: {question}")
        # Show which mode we're using and, for fixed mode, the interval
        print(f"Mode: reflect_{reflect_mode}" + (f" (N={reflect_every})" if reflect_mode == "fixed" else ""))
        print(f"{'='*60}\n")

    while step < max_steps:
        step += 1

        # ── Step 1: Decide whether to reflect ────────────────────────────────
        # We check this BEFORE the normal step so we can inject reflection
        # into the conversation history before the next LLM call.
        should_reflect = False

        if reflect_mode == "fixed":
            # Reflect at steps 3, 6, 9, ...
            # We skip step 1 (nothing to reflect on yet — no actions taken).
            # `(step - 1) % reflect_every == 0` triggers at step 1, 4, 7, ...
            # so we add `step > 1` to skip the first one.
            should_reflect = (step > 1) and ((step - 1) % reflect_every == 0)

        elif reflect_mode == "triggered":
            # Look at the last message in the conversation.
            # If it's an Observation that contains error indicators, trigger reflection.
            last_user = messages[-1]["content"] if messages else ""
            tool_errored = (
                "error" in last_user.lower() or
                "not found" in last_user.lower() or
                "no result" in last_user.lower()
            ) and last_user.startswith("Observation:")

            # Also check for stalling (same action repeated twice in a row)
            stalled = is_stalled(action_history, window=2)

            should_reflect = tool_errored or stalled

        # ── Step 2: Inject reflection if needed ──────────────────────────────
        if should_reflect:
            reflect_count += 1
            if verbose:
                print(f"--- [REFLECT #{reflect_count}] ---")

            # Add the reflection prompt as a user turn — this asks the model to
            # pause and critique itself before continuing.
            messages.append({"role": "user", "content": REFLECT_PROMPT})

            # Make a separate (smaller) LLM call for the reflection.
            # max_tokens=512 is enough for a short self-critique + next action.
            reflect_response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=messages,
                stop_sequences=["Observation:"],  # Same stop sequence as normal steps
            )
            reflect_text = reflect_response.content[0].text.strip()

            if verbose:
                print(reflect_text)

            # Parse the reflection response — it may contain a Reflect note,
            # a next Action, or even a final Answer (if the model decides it
            # already has enough information).
            reflect_note, answer, action = parse_reflect_and_continue(reflect_text)

            # Add the model's reflection response to history
            messages.append({"role": "assistant", "content": reflect_text})

            # If the model produced a final answer during reflection — we're done
            if answer:
                if verbose:
                    print(f"\n{'='*60}\nFinal Answer (post-reflect): {answer}\n{'='*60}\n")
                return answer

            # If the model produced an action during reflection, execute it
            # and skip the normal step below (use `continue` to go back to top).
            if action:
                tool_name, argument = action
                observation = execute_tool(tool_name, argument)
                action_history.append((tool_name, argument))
                if verbose:
                    print(f"Observation: {observation}\n")
                messages.append({"role": "user", "content": f"Observation: {observation}"})
                continue  # Go back to the top of the while loop

            # If reflection produced neither an answer nor an action, nudge the
            # model to keep going with the normal step.
            messages.append({"role": "user", "content": "Continue."})

        # ── Step 3: Normal ReAct step ─────────────────────────────────────────
        # This is the same as agent.py — call the model, parse Thought/Action/Answer.
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=messages,
            stop_sequences=["Observation:"],
        )
        assistant_text = response.content[0].text.strip()

        if verbose:
            print(f"--- Step {step} ---")
            print(assistant_text)

        # Check for a final answer
        answer = parse_answer(assistant_text)
        if answer:
            if verbose:
                print(f"\n{'='*60}\nFinal Answer: {answer}\n{'='*60}\n")
            return answer

        # Parse and execute the action
        parsed = parse_action(assistant_text)
        if not parsed:
            observation = "Error: no valid Action found. Use format: Action: tool_name(argument)"
        else:
            tool_name, argument = parsed
            # Record this action for stall detection
            action_history.append((tool_name, argument))
            observation = execute_tool(tool_name, argument)

        if verbose:
            print(f"Observation: {observation}\n")

        # Append to conversation history
        messages.append({"role": "assistant", "content": assistant_text})
        messages.append({"role": "user",      "content": f"Observation: {observation}"})

    return "Max steps reached without a final answer."


# ── Entry point ───────────────────────────────────────────────────────────────
# Run from the command line:
#   python agent_reflective.py fixed "Read notes.txt and summarize the goal."
#   python agent_reflective.py triggered "Search for FastMCP and explain it."
#
# First argument: mode ("fixed" or "triggered")
# Remaining arguments: the question

if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "fixed"
    question = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else (
        "Read data/notes.txt, count how many key-value pairs it contains (lines with a colon), then multiply that count by 3."
    )
    run_react_reflective(question, reflect_mode=mode, verbose=True)
