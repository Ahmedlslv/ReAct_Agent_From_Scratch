# ReAct Agent from Scratch

A small AI agent built with plain Python and the Gemini API. It can read project data, look up information, and run Python calculations in disposable Docker containers.

The agent loop, tool dispatch, conversation history, and sandbox controller are implemented directly, without LangChain or another agent framework. This project explores how agents work and how to test their behavior.

**Status:** working learning and portfolio project. The Python sandbox has automated unit and Docker integration tests. Agent evaluation and reliability are still being improved.

## What it can do

| Tool         | Purpose                                                      | Where it runs                                  |
| ------------ | ------------------------------------------------------------ | ---------------------------------------------- |
| `web_search` | Retrieve factual summaries through DuckDuckGo Instant Answer | Host Python process, with internet access      |
| `file_read`  | Read text files inside the project's `data/` directory       | Host Python process, with a directory boundary |
| `run_python` | Calculate values and process text using Python               | A fresh, restricted Docker container           |

The basic agent uses Gemini. An optional reflection experiment uses Anthropic.

## How it works

[ReAct](https://arxiv.org/abs/2210.03629) combines reasoning with actions and observations. This implementation follows a simple loop:

```text
Question -> Gemini -> Action -> Tool -> Observation
               ^                           |
               +---------------------------+
               |
               +-> Final answer
```

1. Send the question, instructions, and conversation history to Gemini.
2. Parse a requested tool action or a final answer.
3. Execute the tool and collect its real output.
4. Add the action and observation to the history.
5. Continue until the agent answers or reaches its step limit.

Tool requests currently use a text format:

```text
Action: run_python("2 ** 10")
```

The basic agent allows up to 10 model steps and 3 web search attempts per task. Terminal output shows the requested actions and actual tool observations.

## Requirements

- Python 3.10 or newer. Development and local testing use Python 3.13 on Windows.
- A Gemini API key and access to the configured model.
- Docker Desktop running Linux containers for `run_python` and Docker integration tests.
- Internet access for Gemini, web search, and the initial Docker image download.

Docker is only required for Python tool execution. The agent loop, Gemini requests, and the other tools run in the host Python environment.

## Setup

The commands below use PowerShell. Run them from the repository root after cloning.

### 1. Install Python dependencies

```powershell
git clone https://github.com/Ahmedlslv/ReAct_Agent_From_Scratch.git
cd ReAct_Agent_From_Scratch
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Using the virtual environment's Python directly means activation is optional.

### 2. Prepare Docker

Start Docker Desktop and check the engine type:

```powershell
docker info --format '{{.OSType}}'
```

The output should be `linux`. Download the sandbox image once:

```powershell
docker pull python:3.13-slim
```

The sandbox uses the locally cached image and does not pull images during tool execution. Each call creates a new container; the image remains available for later calls.

For a reproducible image version, optionally pin the local image digest for this PowerShell session:

```powershell
$env:REACT_SANDBOX_IMAGE = (
    docker image inspect python:3.13-slim --format '{{index .RepoDigests 0}}'
).Trim()
```

### 3. Configure Gemini

Create a key using the [Gemini API key guide](https://ai.google.dev/gemini-api/docs/api-key). When you run the basic agent, it reads `GEMINI_API_KEY` or prompts for the key with hidden input if the variable is missing.

Never commit API keys. This project does not automatically load a `.env` file.

The current code defaults to `gemini-3.5-flash-lite`. You can choose another model available to your Google project:

```powershell
$env:GEMINI_MODEL = "YOUR_AVAILABLE_MODEL_ID"
```

Replace the placeholder with a model ID from the [Gemini model documentation](https://ai.google.dev/gemini-api/docs/models). Model availability, API quotas, and charges depend on your project and model.

## Run the agent

Start with a request that does not need tools:

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m agent.base "Say hello. Do not use any tools."
```

Read the included sample file:

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m agent.base "Use file_read to read data/notes.txt, then tell me the author."
```

Try a complete file-to-calculation task:

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m agent.base "Use file_read to read data/notes.txt. Find the numeric value of its lines field, not the number of physical lines. Then use run_python to square that value. Report the value and its square."
```

The supplied file has `lines: 12`, so the expected square is **144**. In the terminal, check that the agent actually calls `file_read`, then `run_python`, and receives the expected observations before answering. A correct final number alone does not prove the tools were used.

### Check the Python tool without Gemini

```powershell
.\.venv\Scripts\python.exe -B -c "from tools import execute_tool; print(execute_tool('run_python', '2 ** 10'))"
```

Expected output: `1024`. This uses Docker but does not call a model API.

## Python sandbox

Every `run_python` call follows this lifecycle:

```text
Validate input -> Create container -> Execute code -> Collect output -> Remove container
```

The container uses the computer's CPU and memory. Gemini itself runs through Google's hosted API; it does not run inside this container.

The sandbox configuration includes:

| Control                            | Current setting                                     |
| ---------------------------------- | --------------------------------------------------- |
| External network                   | Disabled                                            |
| Host directories and Docker socket | Not mounted                                         |
| Host environment variables         | Not forwarded into the container                    |
| Root filesystem                    | Read-only                                           |
| User                               | Non-root, UID/GID `65534:65534`                     |
| Linux capabilities                 | All dropped; privilege escalation disabled          |
| Memory                             | 256 MiB, with no additional swap allowance          |
| CPU quota                          | Equivalent to one CPU                               |
| Process limit                      | 32                                                  |
| Writable temporary directory       | An 8 MiB `/tmp` tmpfs                               |
| Code input / captured output       | 16 KiB each                                         |
| Execution deadline                 | 5 seconds, including container start and attachment |

Container creation and cleanup each have a separate 10-second Docker operation timeout. Cleanup is attempted after success, execution errors, output limits, and timeouts. If Docker is unavailable, the tool returns an error; it never falls back to running the snippet on the host.

A single expression returns its value. For statements, use `print()`:

```python
2 ** 10
```

```python
import math
print(math.sqrt(256))
```

Each call starts fresh: variables, imports, and temporary files do not persist. The default image provides Python and its standard library. Packages installed in the host virtual environment are not automatically available inside the container.

To process a local file, the agent must first use `file_read`, then include the necessary values or text in its Python snippet. Python in the container cannot open the host's project files.

This is a restricted execution prototype. It trusts the Docker daemon and runtime image, and it is not a complete security boundary for a public service accepting hostile code. See the [Docker runtime reference](https://docs.docker.com/reference/cli/docker/container/run/) for the underlying controls.

## Tests

### Offline unit tests

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_python_sandbox.py -v
```

This runs 8 unit tests and skips 7 Docker integration tests. It requires neither Docker nor an API key.

### Real Docker integration tests

With Docker running and the image downloaded:

```powershell
$env:RUN_SANDBOX_INTEGRATION = "1"
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_python_sandbox.py -v
Remove-Item Env:RUN_SANDBOX_INTEGRATION
```

The 15 tests cover container policy, missing Docker, input and output limits, execution results, host file and environment isolation, blocked external networking, a read-only root filesystem, timeout behavior, and cleanup.

All 15 passed in local Windows/Docker validation. These checks validate the sandbox behaviors they exercise; they do not establish the quality of the full agent or prove security against every attack. The Gemini examples above are separate live API checks.

## Configuration

| Variable                  | Purpose                                      | Default                        |
| ------------------------- | -------------------------------------------- | ------------------------------ |
| `GEMINI_API_KEY`          | Basic agent's Gemini credential              | Hidden prompt when absent      |
| `GEMINI_MODEL`            | Basic agent's model ID                       |
| `gemini-3.5-flash-lite`   |
| `REACT_SANDBOX_IMAGE`     | Locally available Python image or digest     | `python:3.13-slim`             |
| `RUN_SANDBOX_INTEGRATION` | Enable real Docker integration tests         | Disabled; set to `1` to enable |
| `ANTHROPIC_API_KEY`       | Credential for the optional reflection agent | Required for that experiment   |

PowerShell `$env:` settings apply to the current session and programs launched from it.

## Reflection and evaluation experiments

The optional reflection agent adds periodic or error-triggered reflection steps:

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m agent.reflective triggered "Use run_python to calculate the square root of 144."
```

It currently uses Anthropic and requires its own API key. The basic agent uses Gemini, so comparing these implementations directly does not isolate the effect of reflection.

The `evals/` directory contains task suites, evaluation runners, and saved reports. The current graders include broad keyword checks, and existing reports describe earlier runs. Treat them as exploratory artifacts rather than a verified score for the current implementation. Running evaluation scripts makes model API calls and may overwrite saved results.

## Project structure

```text
agent/
  __init__.py             Public agent entry points
  base.py                 Gemini ReAct loop
  reflective.py           Anthropic reflection experiment
tools/
  __init__.py             Active tool exports
  v2.py                   Tool registry and implementations
  python_sandbox.py       Docker execution and cleanup
  v1.py                   Historical tool implementation
tests/
  test_python_sandbox.py  Offline and real-container tests
data/
  notes.txt               Sample data for agent tasks
evals/                    Evaluation runners and historical reports
requirements.txt          Python dependencies
README.md                 Project documentation
```

The active agents use `tools/v2.py`. The historical `tools/v1.py` is not the current sandbox implementation.

## Known limitations

- Text-based action parsing depends on the model following the format and is fragile for complex or multiline arguments.
- DuckDuckGo Instant Answer has limited coverage and may return no useful result.
- API errors and rate limits can interrupt runs; bounded retries and clearer error handling are planned.
- Tool observations are included in subsequent model API requests. Use sample data that is appropriate to send to the selected provider.
- Abruptly terminating the controller or losing Docker access can prevent cleanup. Cleanup failures are reported when the program can detect them.
- Full agent regression tests and stronger outcome-based evaluation are still needed.

## Troubleshooting

**Docker connection error:** start Docker Desktop and wait until the Linux engine is ready. Closing the dashboard window usually leaves the engine running; quitting Docker Desktop stops it.

**Image unavailable:** run `docker pull python:3.13-slim`, or pull the image configured in `REACT_SANDBOX_IMAGE`.

**File access denied:** `file_read` accepts files inside the project's `data/` directory. The Python container has no host file mounts.

**Authentication, model, or quota error:** check the API key, the model ID, and the limits of the Google project associated with the key.

**Possible leftover container:** inspect sandbox containers before taking any cleanup action:

```powershell
docker ps -a --filter label=react-python-sandbox=true
```

## Next improvements

- [x] Implement the ReAct loop and tool dispatch from scratch.
- [x] Keep real tool observations in conversation history.
- [x] Move Python execution into disposable Docker containers.
- [x] Add sandbox unit and integration tests.
- [ ] Use structured tool calls with validated arguments.
- [ ] Add offline tests for the agent loop and failure paths.
- [ ] Handle API failures with bounded retries.
- [ ] Improve evaluation graders and save complete execution traces.
- [ ] Compare reflection strategies using the same model and budgets.
- [ ] Add automated checks and a short reproducible demo.
