"""
ReAct Agent — Gemini API, plain Python, no agent framework.

The agent follows this cycle:
1. Send the question and conversation history to Gemini.
2. Parse the response for an Action or final Answer.
3. Execute the selected tool in Python.
4. Add the real tool result to the conversation.
5. Repeat until an answer is returned or max_steps is reached.

Actions currently use a text format:
    Action: file_read("data/notes.txt")

The model requests tools; this Python program executes them.
Text parsing depends on the model following the requested format.

Usage from the repository root:
    python -m agent.base "Use file_read to read data/notes.txt, then tell me the author."

ReAct paper:
    Yao et al., "ReAct: Synergizing Reasoning and Acting in Language Models"
    https://arxiv.org/abs/2210.03629
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# `from __future__ import annotations` enables the newer Python 3.10+ type hint
# syntax (e.g. `str | None`, `tuple[str, str]`) on Python 3.8 and 3.9 as well.
# It must be the very first import.
from __future__ import annotations

import re           # Regular expressions — used to parse the model's output
import os           # Reads API configuration from environment variables
import sys          # Access to command-line arguments (sys.argv)
from getpass import getpass
from google import genai
from google.genai import types, errors

# Import our tool registry and the single dispatch function.
# tools/ package re-exports the current v2 tools (web_search, file_read, run_python).
from tools import TOOLS, execute_tool

# ── System Prompt ─────────────────────────────────────────────────────────────
# The system prompt is the "instruction manual" given to the model before the
# conversation starts. It tells the model:
#   - what role to play (ReAct agent)
#   - what tools are available and when to use each
#   - what output format to follow (Thought / Action / Answer)
#
# IMPORTANT: The tool descriptions here are written for the *agent*, not a
# human developer. They say "use this for X, NOT for Y" so the model picks
# the right tool without guessing.

SYSTEM_PROMPT = """You are a ReAct agent. You solve problems by interleaving Thought, Action, and Observation steps.

You have access to these tools:
- web_search(query) — search the web for factual information; do NOT use for math or local files
- file_read(file_path) — read files inside the project's data folder; use paths such as data/notes.txt; do NOT use for web content or computation
- run_python(code) — run Python in a fresh isolated environment for math and text processing; it cannot access host files or the internet


For local data, use file_read first. Then include only the necessary text or
values directly in the Python code. Do not try to open host files from Python.

Each run_python call starts fresh. Variables, imports and temporary files do
not persist between calls.

A single expression returns its value. For statements, use print(result).
Example: Action: run_python("2 ** 10")
Example: Action: run_python("import math; print(math.sqrt(256))")

Actions accept one argument value. Do not include parameter names.
Correct: Action: file_read("data/notes.txt")
Incorrect: Action: file_read(file_path="data/notes.txt")
For questions about local files, call file_read and wait for its real
Observation before answering. Never invent file contents.

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

# ── Parser: extract Action ────────────────────────────────────────────────────


def parse_action(text: str) -> tuple[str, str] | None:
    """
    Parse the model's output to extract a tool call.

    The model outputs lines like:
        Action: web_search(What is the capital of France?)
        Action: file_read(notes.txt)
        Action: run_python(2 ** 10)

    This function uses a regex to extract:
        - tool_name  (e.g. "web_search")
        - argument   (e.g. "What is the capital of France?")

    Returns a (tool_name, argument) tuple, or None if no valid Action is found.
    The `re.MULTILINE` flag makes `$` match end-of-line rather than end-of-string,
    so we correctly capture single-line Action entries in a longer response.
    """
    match = re.search(r"Action:\s*(\w+)\((.+?)\)\s*$", text, re.MULTILINE)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return None


def parse_answer(text: str) -> str | None:
    """
    Check whether the model has produced a final answer.

    The model signals it is done by outputting:
        Answer: <the answer text here>

    `re.DOTALL` makes `.` match newlines too, so multi-line answers are captured
    in full (not just the first line).

    Returns the answer string, or None if not yet present.
    """
    match = re.search(r"Answer:\s*(.+)", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None

# ── ReAct Loop ────────────────────────────────────────────────────────────────


def run_react(
    question: str,
    max_steps: int = 10,
    verbose: bool = True,
) -> str:
    """
    Run the full ReAct loop for a given question.

    How it works:
      1. We start with system instructions and the user's question.
      2. We call the hosted Groq API (no local model).
         We pass `stop=["Observation:"]` so the model STOPS generating
         as soon as it would write "Observation:". This prevents the model from
         making up its own tool results.
      3. We parse the model's output:
         - If it contains "Answer:" → we're done, return it.
         - If it contains "Action:" → execute the named tool with the argument.
         - Otherwise → nudge the model to produce a valid Action.
      4. We append the model's output and the real observation to the message
         history, then loop back to step 2.
      5. If we hit max_steps without an Answer, we bail out gracefully.

    Parameters:
        question  — the task or question to solve
        max_steps — safety limit to prevent infinite loops (default: 10)
        verbose   — if True, print each step to the terminal

    Returns:
        The final answer string (or a timeout message).
    """

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        key = getpass("Paste your Gemini API key now: ").strip()
    if not key:
        raise ValueError("A Gemini API key is required.")

    model = os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash-lite"
    client = genai.Client(api_key=key)

    # Gemini receives system instructions through GenerateContentConfig.
    # Store the conversation history. Send system instructions separately.
    # Start the conversation with the user's question.
    messages = [
        {"role": "user", "parts": [{"text": question}]},
    ]

    step = 0  # Counter to enforce max_steps
    search_calls = 0
    search_limit = 3
    search_limit_message = (
        "The web search budget is exhausted. No further web searches "
        "are available for this task. Complete remaining work using "
        "other tools and existing observations. Clearly state what "
        "could not be verified."
    )

    print(f"\n{'='*60}")
    print(f"Question: {question}")
    print(f"Provider: Gemini | Model: {model}")
    print(f"{'='*60}\n")

    while step < max_steps:
        step += 1

        response = client.models.generate_content(
            model=model,
            contents=messages,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                stop_sequences=["Observation:"],
                max_output_tokens=1024,
            ),
        )

        assistant_text = (response.text or "").strip()

        if verbose:
            print(f"--- Step {step} ---")
            print(assistant_text)

        # ── Check for final answer ─────────────────────────────────────────────
        # If the model wrote "Answer: ...", we're done — return it immediately.
        answer = parse_answer(assistant_text)
        if answer:
            print(f"\n{'='*60}")
            print(f"Final Answer: {answer}")
            print(f"{'='*60}\n")
            return answer

        # ── Parse and execute the action ──────────────────────────────────────
        # Try to extract the tool call from the model's output.
        parsed = parse_action(assistant_text)
        if not parsed:
            # The model didn't produce a valid "Action: tool(arg)" line.
            # Return an error observation to nudge it back on track.
            observation = (
                "Error: your response must contain either "
                "Action: tool_name(argument) or Answer: your final answer. "
                "If the previous observation answers the question, "
                "respond with Answer: followed by the answer."
            )
        else:
            tool_name, argument = parsed

            if tool_name == "web_search" and search_calls >= search_limit:
                observation = search_limit_message
            else:
                if tool_name == "web_search":
                    search_calls += 1

                if verbose:
                    print(
                        f"\nObservation: [calling {tool_name}({argument!r})]"
                    )

                observation = execute_tool(tool_name, argument)

                if tool_name == "web_search" and search_calls == search_limit:
                    observation += f"\n\n{search_limit_message}"

        if verbose:
            print(f"Observation: {observation}\n")

        # ── Append to conversation history ────────────────────────────────────
        # The API is stateless — we maintain the full history ourselves.
        # We add two turns:
        #   1. The assistant's Thought + Action
        #   2. The user's "Observation: <result>" (which triggers the next step)
        #
        # Note: we prefix with "Observation:" in the user turn because the model
        # was trained to expect observations in this format. It also matches the
        # stop sequence we set, completing the Thought→Action→Observation cycle.
        messages.append(
            {"role": "model", "parts": [{"text": assistant_text}]}
        )
        messages.append(
            {"role": "user", "parts": [
                {"text": f"Observation: {observation}"}]}
        )

    # If we exit the loop without an answer, return a safe fallback.
    return "Max steps reached without a final answer."


# ── Entry point ───────────────────────────────────────────────────────────────
# When run directly (`python agent.py "..."`) this block executes.
# When imported as a module (e.g. in run_evals.py), it is skipped.

if __name__ == "__main__":
    # Join all command-line arguments into a single question string.
    # If no arguments are given, use a default demo question.
    question = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
        "What is the square root of 144, and what does the file data/notes.txt say on its first line?"
    )
    try:
        result = run_react(question)
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    if result == "Max steps reached without a final answer.":
        print(result, file=sys.stderr)
        sys.exit(1)
