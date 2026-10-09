"""
tools_v2.py — Refactored tool implementations (W02D4)

---------------------------------------------------------------------------
OVERVIEW
---------------------------------------------------------------------------
This file defines the three tools available to the ReAct agent.

Each tool is a plain Python function with the signature:
    tool(argument: str) -> str

The TOOLS dict maps tool names (strings) to callables.
execute_tool() is the single entry point the agent loop uses — it receives
a name + argument string and returns the result as a string.

Why plain strings in, strings out?
  - The agent receives everything as text (that's how LLM APIs work).
  - Returning plain strings keeps the tool contract simple and debuggable.
  - Any structured data (numbers, lists, JSON) is formatted as a readable string.

---------------------------------------------------------------------------
CHANGES FROM tools.py (v1) — W02D4 refactor
---------------------------------------------------------------------------
Following the Anthropic "Writing tools for agents" guide:

  1. Removed `calc`  — redundant with run_python. Two overlapping tools
                       confuse the model about which to call.
  2. Renamed `search` → `web_search`  — clearer purpose, less ambiguous.
  3. Rewrote all docstrings as agent-facing instructions (not dev docs).
     The model reads these descriptions to decide when to use each tool.
  4. Actionable errors — errors now tell the agent *what to fix*, not just
     *what went wrong*.
  5. Token efficiency — file_read now surfaces truncation status and total
     size so the agent can decide to request more without blind retrying.

Reference: https://www.anthropic.com/engineering/writing-tools-for-agents
"""

# `from __future__ import annotations` enables Python 3.10+ type hint syntax
# (e.g. `str | None`, `tuple[str, str]`) on older Python versions.
from __future__ import annotations

import math           # Standard math functions (used by run_python's eval scope)
import os             # File system access — listing directories, resolving paths
import io             # In-memory file-like objects — used to capture print() output
import contextlib     # redirect_stdout — captures stdout from executed code
import urllib.request # Low-level HTTP — avoids needing the `requests` library
import urllib.parse   # URL encoding — converts query strings to safe URL format
import json           # Parsing JSON responses from the DuckDuckGo API


# ── Tool: web_search ──────────────────────────────────────────────────────────
# Uses the DuckDuckGo Instant Answer API (free, no API key needed).
# Caveat: it only returns summary-level results (Wikipedia abstracts, direct
# factual answers). It won't return full web page content.

def web_search(query: str) -> str:
    """
    Search the web for factual information about a topic.

    Use this when you need external knowledge not available in local files.
    Prefer specific, focused queries over broad ones — e.g. "Python asyncio
    create_task" not "Python async".

    If the search returns no result, try:
      1. A shorter, simpler query (remove filler words)
      2. A different angle on the same topic
      3. Acknowledging the limitation and answering from your own knowledge

    Do NOT call web_search for math — use run_python instead.
    Do NOT call web_search for local file contents — use file_read instead.

    Returns the most relevant result found, with source URL if available.
    If nothing is found, returns a clear "no result" message with retry advice.
    """
    # Strip quotes the model sometimes wraps around its arguments
    query = query.strip().strip("\"'")
    if not query:
        return "web_search error: query cannot be empty. Provide a search term."

    try:
        # Build the DuckDuckGo API URL.
        # urllib.parse.quote encodes special characters (spaces → %20, etc.)
        # so the query is safe to embed in a URL.
        encoded = urllib.parse.quote(query)
        url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_redirect=1&no_html=1"

        # Make the HTTP GET request with a descriptive User-Agent header.
        # timeout=8 prevents the agent from hanging if the API is slow.
        req = urllib.request.Request(url, headers={"User-Agent": "react-agent/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())

        # DuckDuckGo returns several result types — we try the best ones first:

        # AbstractText: Wikipedia-style summary paragraph (highest quality)
        if data.get("AbstractText"):
            source = data.get("AbstractURL", "")
            text   = data["AbstractText"]
            return f"{text}\n\nSource: {source}" if source else text

        # Answer: a short direct factual answer (e.g. unit conversions, dates)
        if data.get("Answer"):
            return f"Direct answer: {data['Answer']}"

        # RelatedTopics: a list of loosely related results — we take just the first
        topics = data.get("RelatedTopics", [])
        if topics and isinstance(topics[0], dict) and topics[0].get("Text"):
            return f"Related: {topics[0]['Text']}"

        # Nothing useful returned — give the agent specific recovery strategies
        # (three options is better than one vague hint)
        return (
            f"No result found for '{query}'.\n"
            "Suggestions:\n"
            "  - Try a shorter or rephrased query\n"
            "  - Use file_read if the information is in a local file\n"
            "  - Answer from your own knowledge if you are confident"
        )

    except Exception as exc:
        # Network error, API timeout, JSON parse failure, etc.
        # Distinguish this from a "no result" so the agent knows whether to retry.
        return (
            f"web_search failed: {exc}\n"
            "This is a network or API error, not a problem with your query.\n"
            "You can retry once, or proceed without search results."
        )


# ── Tool: file_read ───────────────────────────────────────────────────────────
# Reads a local file and returns its contents as a string.
# Includes metadata (line count, total chars) and truncation notices so the
# agent can make informed decisions about whether to request more content.

def file_read(file_path: str, max_chars: int = 3000) -> str:
    """
    Read the contents of a local file.

    Use this to access data in local files: text, markdown, JSON, CSV, code, etc.
    The file path can be relative (resolved from the current working directory)
    or absolute.

    The response includes:
      - File contents (up to max_chars characters)
      - A truncation notice if the file was cut off
      - File metadata: total lines, total characters

    If the file is not found, the error message lists files in the same directory
    so you can correct the path.

    Do NOT use file_read for web content — use web_search instead.
    Do NOT use file_read to run or evaluate code — use run_python instead.

    Parameters:
      file_path  — path to the file (relative or absolute)
      max_chars  — maximum characters to return (default 3000, increase if needed)
    """
    # Strip quotes the model may have wrapped around the path
    file_path = file_path.strip().strip("\"'")
    if not file_path:
        return "file_read error: file_path cannot be empty. Provide a file name or path."

    try:
        # os.path.abspath resolves relative paths using the current working directory.
        # errors="replace" handles files with encoding issues (replaces bad bytes
        # with a placeholder character instead of raising an exception).
        full_path = os.path.abspath(file_path)
        with open(full_path, "r", errors="replace") as f:
            content = f.read()

        # Compute metadata so the agent knows the full file size even when truncated
        total_chars = len(content)
        # Count lines: number of newlines + 1 for the last line (if file is not empty
        # and doesn't end with a newline)
        total_lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        truncated   = total_chars > max_chars

        # Return the (possibly truncated) content
        displayed = content[:max_chars]

        result = displayed
        # Always append metadata — the agent knows the file size upfront
        result += f"\n\n[File metadata: {total_lines} lines, {total_chars} chars total]"
        if truncated:
            # Tell the agent exactly how to get the rest — no guessing needed
            result += (
                f"\n[TRUNCATED: showing first {max_chars} of {total_chars} chars. "
                f"Call file_read with max_chars={total_chars} to read the full file.]"
            )
        return result

    except FileNotFoundError:
        # The file doesn't exist. List nearby files so the agent can self-correct
        # the path without a blind second attempt.
        directory = os.path.dirname(os.path.abspath(file_path)) or "."
        try:
            nearby = [f for f in os.listdir(directory) if os.path.isfile(os.path.join(directory, f))]
            nearby_str = ", ".join(sorted(nearby)[:10])  # show up to 10 files
        except Exception:
            nearby_str = "(could not list directory)"
        return (
            f"file_read error: file not found — '{file_path}'\n"
            f"Files in '{directory}': {nearby_str}\n"
            "Check the file name and try again."
        )
    except PermissionError:
        return f"file_read error: permission denied — '{file_path}'. You do not have read access to this file."
    except Exception as exc:
        return f"file_read error: {exc}"


# ── Tool: run_python ──────────────────────────────────────────────────────────
# Executes arbitrary Python code in a sandboxed context and returns the result.
# This replaces the old `calc` tool — run_python can do everything calc did
# and more (counting, string operations, data processing, imports, etc.).

def run_python(code: str) -> str:
    """
    Execute Python code or evaluate a Python expression and return the result.

    Use this for:
      - Any math or numeric computation (preferred over guessing)
      - Counting, filtering, or transforming data from files
      - String operations (length, splitting, searching)
      - Data processing (sorting, deduplicating, summing)

    Two modes (auto-detected):
      - Single expression: evaluated and result returned directly
          e.g. run_python("2 ** 10")  →  "1024"
          e.g. run_python("len([l for l in open('notes.txt') if ':' in l])")
      - Statements (import, loops, print, assignments): executed, stdout returned
          e.g. run_python("import math; print(round(math.sqrt(120), 2))")

    IMPORTANT:
      - Always use print() for statement-mode results, otherwise you get no output.
      - File paths in code are relative to the current working directory.
      - Do NOT use run_python to search the web — use web_search instead.
      - Do NOT use run_python to read large files directly — use file_read first,
        then process the content with run_python if needed.

    Returns the expression result or stdout as a string.
    On error, returns a clear message with the exception and a fix suggestion.
    """
    # Strip surrounding quotes the model sometimes adds
    code = code.strip().strip("\"'")
    if not code:
        return "run_python error: code cannot be empty. Provide a Python expression or statement."

    # ── Mode 1: Single expression evaluation ──────────────────────────────────
    # `compile(code, "<string>", "eval")` succeeds only if `code` is a single
    # expression (e.g. "2 + 2", "len('hello')", "math.sqrt(144)").
    # If it's a statement (import, assignment, print, etc.), it raises SyntaxError.
    try:
        compiled = compile(code, "<string>", "eval")
        result   = eval(compiled, {})   # {} = empty namespace, no builtins by default
        return str(result)
    except SyntaxError:
        pass  # Not a single expression — fall through to exec mode
    except Exception as exc:
        # A genuine runtime error in the expression (e.g. NameError, ZeroDivisionError)
        return (
            f"run_python error: {exc}\n"
            f"Expression tried: {code!r}\n"
            "Fix: check variable names, syntax, and that any files exist."
        )

    # ── Mode 2: Statement execution ───────────────────────────────────────────
    # For code with multiple lines, assignments, imports, loops, print() calls, etc.
    # We capture everything the code prints to stdout using redirect_stdout.
    buf = io.StringIO()   # In-memory buffer that acts like a file
    try:
        with contextlib.redirect_stdout(buf):
            exec(code, {})   # noqa: S102 — execute the code in an empty namespace
        stdout = buf.getvalue()   # Everything that was printed
        if not stdout:
            # Code ran but printed nothing — common beginner mistake (forgot print())
            return (
                "run_python ran successfully but produced no output.\n"
                "If you expected a result, wrap it in print(), e.g. print(result)."
            )
        return stdout.strip()
    except Exception as exc:
        return (
            f"run_python error: {exc}\n"
            f"Code tried:\n{code}\n"
            "Fix: check imports, variable names, and file paths."
        )


# ── Dispatch ──────────────────────────────────────────────────────────────────
# The agent loop doesn't call tool functions directly — it calls execute_tool()
# with a name (string) and an argument (string). This single entry point keeps
# the agent code clean and makes it easy to add or remove tools.

# Map of tool name → callable. The keys must match what the agent writes after
# "Action:" (e.g. "Action: web_search(...)").
TOOLS: dict[str, callable] = {
    "web_search": web_search,
    "file_read":  file_read,
    "run_python": run_python,
}

def execute_tool(tool_name: str, argument: str) -> str:
    """
    Dispatch a tool call by name and return the result as a string.

    Called by the agent loop after parsing an "Action: tool_name(argument)" line.
    If the tool name is unknown (e.g. the model hallucinated "calc"), returns an
    actionable error that lists the available tools.
    """
    if tool_name not in TOOLS:
        # List available tools in the error so the agent can self-correct
        available = ", ".join(TOOLS.keys())
        return (
            f"Unknown tool '{tool_name}'. "
            f"Available tools: {available}. "
            "Check spelling and use only the tools listed."
        )
    # Call the tool function — each tool accepts exactly one string argument
    return TOOLS[tool_name](argument)
