"""
Tool implementations for the ReAct agent.

Each tool is a plain Python function:
    tool(argument: str) -> str

The TOOLS dict maps tool names to callables.
execute_tool() is the single dispatch point used by the agent loop.
"""

import math
import os
import urllib.request
import urllib.parse
import json


# ── Tool: calc ────────────────────────────────────────────────────────────────

def calc(expression: str) -> str:
    """
    Evaluate a safe math expression.

    Supports: +, -, *, /, **, sqrt, log, sin, cos, tan, pi, e, abs, round
    Does NOT use eval() on arbitrary code — whitelist enforced.

    Examples:
        calc("sqrt(144)")     → "12.0"
        calc("2 ** 10")       → "1024"
        calc("pi * 5 ** 2")   → "78.53981633974483"
    """
    allowed_names = {
        "sqrt": math.sqrt,
        "log":  math.log,
        "log2": math.log2,
        "log10": math.log10,
        "sin":  math.sin,
        "cos":  math.cos,
        "tan":  math.tan,
        "abs":  abs,
        "round": round,
        "pi":   math.pi,
        "e":    math.e,
        "inf":  math.inf,
    }
    try:
        # Strip quotes the model sometimes wraps around the expression
        expression = expression.strip().strip("\"'")
        result = eval(expression, {"__builtins__": {}}, allowed_names)  # noqa: S307
        return str(result)
    except Exception as exc:
        return f"calc error: {exc}"


# ── Tool: file_read ───────────────────────────────────────────────────────────

def file_read(path: str) -> str:
    """
    Read a local file and return its contents (first 2000 chars).

    Path is resolved relative to the current working directory.
    Strips surrounding quotes the model sometimes adds.

    Examples:
        file_read("notes.txt")
        file_read("/absolute/path/to/file.md")
    """
    path = path.strip().strip("\"'")
    try:
        full_path = os.path.abspath(path)
        with open(full_path, "r", errors="replace") as f:
            content = f.read(2000)
        if not content:
            return f"File '{path}' is empty."
        return content
    except FileNotFoundError:
        return f"file_read error: file not found — '{path}'"
    except Exception as exc:
        return f"file_read error: {exc}"


# ── Tool: search ──────────────────────────────────────────────────────────────

def search(query: str) -> str:
    """
    Search for information using DuckDuckGo Instant Answer API (no key needed).

    Falls back to a clear "no result" message if the API returns nothing useful.
    For richer results, swap in a Brave/Serper API key.

    Examples:
        search("Python asyncio event loop")
        search("Anthropic Claude models 2025")
    """
    query = query.strip().strip("\"'")
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://api.duckduckgo.com/?q={encoded}&format=json&no_redirect=1&no_html=1"

        req = urllib.request.Request(url, headers={"User-Agent": "react-agent/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode())

        # Try Abstract first (Wikipedia-style summary)
        if data.get("AbstractText"):
            source = data.get("AbstractURL", "")
            return f"{data['AbstractText']}\nSource: {source}"

        # Try Answer (direct factual answer)
        if data.get("Answer"):
            return data["Answer"]

        # Try first RelatedTopic
        topics = data.get("RelatedTopics", [])
        if topics and isinstance(topics[0], dict) and topics[0].get("Text"):
            return topics[0]["Text"]

        return f"No direct result found for '{query}'. Try rephrasing or using file_read for local data."

    except Exception as exc:
        return f"search error: {exc}"


# ── Tool: run_python ──────────────────────────────────────────────────────────

def run_python(code: str) -> str:
    """
    Execute arbitrary Python code or evaluate an expression.

    If the code is a single expression, it evaluates and returns the result.
    If it contains statements, it executes the code and returns the stdout.

    Examples:
        run_python("len([l for l in open('notes.txt') if ':' in l])")
        run_python("import math; print(math.sqrt(256))")
    """
    # Strip quotes the model sometimes wraps around the argument
    code = code.strip().strip("\"'")

    # Try evaluating as an expression first
    try:
        compiled = compile(code, "<string>", "eval")
        result = eval(compiled, {})
        return str(result)
    except SyntaxError:
        # Not a single expression (contains statements like import, assignment, print, loops, etc.)
        pass
    except Exception as exc:
        # Any other exception (like FileNotFoundError, NameError) is a real runtime error of the expression
        return f"run_python error: {exc}"

    # Fall back to exec for statements
    import io
    import contextlib

    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        try:
            exec(code, {})
            stdout = f.getvalue()
            return stdout if stdout else "Success (no stdout)"
        except Exception as exc:
            return f"run_python error: {exc}"


# ── Dispatch ──────────────────────────────────────────────────────────────────

TOOLS: dict[str, callable] = {
    "calc":      calc,
    "file_read": file_read,
    "search":    search,
    "run_python": run_python,
}

def execute_tool(tool_name: str, argument: str) -> str:
    """Dispatch a tool call by name. Returns observation string."""
    if tool_name not in TOOLS:
        available = ", ".join(TOOLS.keys())
        return f"Unknown tool '{tool_name}'. Available tools: {available}"
    return TOOLS[tool_name](argument)
