"""Run untrusted Python snippets in disposable, restricted Linux containers.

The Docker daemon and runtime image are trusted. No host paths are mounted.
This module never executes snippet code in the host Python interpreter.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid

IMAGE = os.environ.get("REACT_SANDBOX_IMAGE") or "python:3.13-slim"
EXECUTION_TIMEOUT = 5.0
DOCKER_TIMEOUT = 10.0
MAX_CODE_BYTES = 16_384
MAX_OUTPUT_BYTES = 16_384

# This runner executes INSIDE the container. A single expression returns its
# value; statements return their printed output. print(42) therefore returns 42.
_RUNNER = """
import ast
import sys

source = sys.stdin.read()
tree = ast.parse(source, filename="<agent>", mode="exec")
scope = {"__name__": "__main__"}

if len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr):
    expression = ast.Expression(tree.body[0].value)
    result = eval(compile(expression, "<agent>", "eval"), scope)
    if result is not None:
        print(result)
else:
    exec(compile(tree, "<agent>", "exec"), scope)
"""


class SandboxError(Exception):
    """Execution stopped because a sandbox limit or I/O check failed."""


def _create_command(name: str) -> list[str]:
    """Security settings are controlled by this program, never by the model."""
    return [
        "docker", "create", "--interactive", "--rm",
        "--name", name,
        "--label", "react-python-sandbox=true",
        "--pull", "never",
        "--network", "none",
        "--read-only",
        "--user", "65534:65534",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--memory", "256m",
        "--memory-swap", "256m",
        "--cpus", "1",
        "--pids-limit", "32",
        "--shm-size", "8m",
        "--ulimit", "nofile=64:64",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=8m,mode=1777",
        "--workdir", "/tmp",
        "--log-driver", "none",
        "--init",
        "--entrypoint", "python",
        IMAGE, "-I", "-S", "-B", "-u", "-c", _RUNNER,
    ]


def _execute(name: str, payload: bytes) -> tuple[int, str]:
    """Stream input/output without buffering unlimited output on the host."""
    process = subprocess.Popen(
        ["docker", "start", "--attach", "--interactive", name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        shell=False,
    )
    output = bytearray()
    output_exceeded = threading.Event()
    input_sent = threading.Event()
    read_failed = threading.Event()

    def send_input() -> None:
        try:
            with process.stdin:
                remaining = memoryview(payload)
                while remaining:
                    written = process.stdin.write(remaining)
                    if not written:
                        return
                    remaining = remaining[written:]
            input_sent.set()
        except (OSError, ValueError):
            # The exit code/output or input_sent check reports the failure.
            pass

    def read_output() -> None:
        try:
            while chunk := process.stdout.read(4096):
                available = MAX_OUTPUT_BYTES - len(output)
                output.extend(chunk[:available])
                if len(chunk) > available:
                    output_exceeded.set()
                # Keep draining until the controller stops the process.
        except (OSError, ValueError):
            read_failed.set()

    writer = threading.Thread(target=send_input, daemon=True)
    reader = threading.Thread(target=read_output, daemon=True)
    writer.start()
    reader.start()
    deadline = time.monotonic() + EXECUTION_TIMEOUT
    problem = None

    try:
        while process.poll() is None:
            if output_exceeded.is_set():
                problem = f"output exceeded {MAX_OUTPUT_BYTES} bytes."
                break
            if read_failed.is_set():
                problem = "could not read Docker output."
                break
            if time.monotonic() >= deadline:
                problem = (
                    f"execution timed out after {EXECUTION_TIMEOUT:g} seconds."
                )
                break
            time.sleep(0.02)
    finally:
        # Stopping this CLI is insufficient to stop the container. The caller
        # also force-removes the named container in its own finally block.
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        writer.join(timeout=1)
        reader.join(timeout=1)
        if not reader.is_alive():
            process.stdout.close()

    if problem:
        raise SandboxError(problem)
    if output_exceeded.is_set():
        raise SandboxError(f"output exceeded {MAX_OUTPUT_BYTES} bytes.")
    if reader.is_alive() or writer.is_alive() or read_failed.is_set():
        raise SandboxError("Docker I/O did not finish cleanly.")
    if process.returncode == 0 and not input_sent.is_set():
        raise SandboxError("the complete snippet could not be sent to Docker.")

    return (
        process.returncode,
        output.decode("utf-8", errors="replace").strip(),
    )


def _remove_container(name: str) -> str | None:
    """Return a warning when cleanup cannot be confirmed."""
    try:
        result = subprocess.run(
            ["docker", "rm", "--force", name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCKER_TIMEOUT,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return f"cleanup could not be confirmed for container {name}."

    # --rm may already have removed the container after normal execution.
    if result.returncode and "no such container" not in result.stderr.lower():
        return (
            f"cleanup failed for container {name}: "
            f"{result.stderr.strip()[:500]}"
        )
    return None


def run_in_sandbox(source: str) -> str:
    """Return a snippet's output or an actionable error, never a local fallback.

    The execution deadline includes container start/attachment. Container
    creation and cleanup each have a separate Docker operation timeout.
    Each call has fresh state and can use only its own temporary filesystem.
    """
    if not isinstance(source, str) or not source.strip():
        return "run_python error: provide a non-empty Python snippet."

    try:
        payload = source.encode("utf-8")
    except UnicodeEncodeError:
        return "run_python error: code must be valid UTF-8 text."

    if len(payload) > MAX_CODE_BYTES:
        return f"run_python error: code exceeds {MAX_CODE_BYTES} bytes."

    name = f"react-python-{uuid.uuid4().hex}"
    cleanup_needed = True
    cleanup_problem = None
    result = ""

    try:
        created = subprocess.run(
            _create_command(name),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCKER_TIMEOUT,
            shell=False,
        )

        if created.returncode:
            result = (
                "run_python error: Docker could not create the container. "
                "Check Docker Desktop and pull the configured image.\n"
                + created.stderr.strip()[:2000]
            )
        else:
            exit_code, output = _execute(name, payload)
            if exit_code:
                result = (
                    f"run_python error: execution failed (exit {exit_code}).\n"
                    f"{output}"
                )
            else:
                result = (
                    output
                    or "Python completed without output. Use print(result)."
                )

    except FileNotFoundError:
        cleanup_needed = False
        result = (
            "run_python error: Docker CLI not found. Install Docker Desktop."
        )
    except SandboxError as exc:
        result = f"run_python error: {exc}"
    except subprocess.TimeoutExpired:
        result = "run_python error: a Docker operation timed out."
    except OSError as exc:
        result = f"run_python error: could not run Docker: {exc}"
    finally:
        if cleanup_needed:
            cleanup_problem = _remove_container(name)

    if cleanup_problem:
        return f"run_python error: {cleanup_problem}\n{result}"
    return result
