"""Offline checks run by default; real containers require explicit opt-in."""

import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import python_sandbox as sandbox


class FakeProcess:
    """A Docker CLI stand-in; no Python snippet is executed."""

    def __init__(self, output=b"", returncode=0):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(output)
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class SandboxUnitTests(unittest.TestCase):
    def test_container_policy(self):
        command = sandbox._create_command("test-container")

        for option, value in [
            ("--network", "none"),
            ("--user", "65534:65534"),
            ("--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges=true"),
            ("--memory", "256m"),
            ("--memory-swap", "256m"),
            ("--cpus", "1"),
            ("--pids-limit", "32"),
            ("--pull", "never"),
        ]:
            self.assertEqual(command[command.index(option) + 1], value)

        self.assertIn("--read-only", command)

        for forbidden in (
            "--volume", "-v", "--mount", "--privileged",
            "--env", "-e", "--env-file", "--use-api-socket",
        ):
            self.assertNotIn(forbidden, command)

    def test_input_limits_do_not_call_docker(self):
        with patch.object(sandbox.subprocess, "run") as docker:
            self.assertIn("non-empty", sandbox.run_in_sandbox(" "))
            self.assertIn(
                "exceeds",
                sandbox.run_in_sandbox("x" * 20_000),
            )
            docker.assert_not_called()

    def test_missing_docker_never_executes_locally(self):
        with patch.object(
            sandbox.subprocess,
            "run",
            side_effect=FileNotFoundError,
        ), patch.object(sandbox, "_execute") as execute:
            self.assertIn(
                "Docker CLI not found",
                sandbox.run_in_sandbox("print(42)"),
            )
            execute.assert_not_called()

    def test_cleanup_after_execution_failure(self):
        completed = subprocess.CompletedProcess([], 0, "", "")

        with patch.object(
            sandbox.subprocess,
            "run",
            return_value=completed,
        ) as docker, patch.object(
            sandbox,
            "_execute",
            side_effect=sandbox.SandboxError("test timeout"),
        ):
            self.assertIn(
                "test timeout",
                sandbox.run_in_sandbox("while True: pass"),
            )

        command = docker.call_args_list[-1].args[0]
        self.assertEqual(command[:3], ["docker", "rm", "--force"])

    def test_cleanup_failure_is_reported(self):
        with patch.object(
            sandbox.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess(
                    [], 1, "", "daemon unavailable"
                ),
            ],
        ), patch.object(
            sandbox,
            "_execute",
            return_value=(0, "42"),
        ):
            self.assertIn(
                "cleanup failed",
                sandbox.run_in_sandbox("42"),
            )

    def test_stream_returns_output(self):
        process = FakeProcess(b"42\n")

        with patch.object(
            sandbox.subprocess,
            "Popen",
            return_value=process,
        ):
            self.assertEqual(
                sandbox._execute("test", b"print(42)"),
                (0, "42"),
            )

    def test_stream_limits_output(self):
        process = FakeProcess(
            b"x" * (sandbox.MAX_OUTPUT_BYTES + 1)
        )

        with patch.object(
            sandbox.subprocess,
            "Popen",
            return_value=process,
        ):
            with self.assertRaisesRegex(
                sandbox.SandboxError,
                "output exceeded",
            ):
                sandbox._execute("test", b"print('x')")

    def test_stream_times_out(self):
        process = FakeProcess(returncode=None)

        with patch.object(
            sandbox.subprocess,
            "Popen",
            return_value=process,
        ), patch.object(
            sandbox,
            "EXECUTION_TIMEOUT",
            0.01,
        ):
            with self.assertRaisesRegex(
                sandbox.SandboxError,
                "timed out",
            ):
                sandbox._execute("test", b"while True: pass")

        self.assertEqual(process.returncode, -9)


@unittest.skipUnless(
    os.environ.get("RUN_SANDBOX_INTEGRATION") == "1",
    "Set RUN_SANDBOX_INTEGRATION=1 to use real Docker containers.",
)
class SandboxIntegrationTests(unittest.TestCase):
    def test_expression_and_print(self):
        self.assertEqual(
            sandbox.run_in_sandbox("2 ** 10"),
            "1024",
        )
        self.assertEqual(
            sandbox.run_in_sandbox("print(42)"),
            "42",
        )
        self.assertEqual(
            sandbox.run_in_sandbox(
                "import math; print(math.sqrt(256))"
            ),
            "16.0",
        )

    def test_host_file_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host-only.txt"
            path.write_text(
                "host-only sentinel",
                encoding="utf-8",
            )

            result = sandbox.run_in_sandbox(
                "from pathlib import Path; "
                f"print(Path({str(path)!r}).exists())"
            )
            self.assertEqual(result, "False")

    def test_host_environment_is_not_forwarded(self):
        with patch.dict(
            os.environ,
            {"SANDBOX_HOST_SECRET": "host-only"},
        ):
            result = sandbox.run_in_sandbox(
                "import os; "
                "print(os.getenv('SANDBOX_HOST_SECRET', 'absent'))"
            )

        self.assertEqual(result, "absent")

    def test_root_filesystem_is_read_only(self):
        result = sandbox.run_in_sandbox(
            "import os; "
            "print(bool(os.statvfs('/').f_flag & os.ST_RDONLY))"
        )
        self.assertEqual(result, "True")

    def test_external_network_is_unavailable(self):
        result = sandbox.run_in_sandbox(
            "import socket\n"
            "try:\n"
            "    socket.create_connection("
            "('1.1.1.1', 80), timeout=1).close()\n"
            "    print('connected')\n"
            "except OSError:\n"
            "    print('blocked')\n"
        )
        self.assertEqual(result, "blocked")

    def test_output_limit(self):
        self.assertIn(
            "output exceeded",
            sandbox.run_in_sandbox("print('x' * 100000)"),
        )

    def test_timeout_removes_the_container(self):
        token = "integration-" + os.urandom(8).hex()

        with patch.object(sandbox.uuid, "uuid4") as identifier:
            identifier.return_value.hex = token
            result = sandbox.run_in_sandbox("while True: pass")

        self.assertIn("timed out", result)

        inspection = subprocess.run(
            [
                "docker", "container", "inspect",
                f"react-python-{token}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(inspection.returncode, 0)
        self.assertIn("no such", inspection.stderr.lower())


if __name__ == "__main__":
    unittest.main()
