# ReAct Agent from Scratch

A Python implementation of the ReAct loop using the Gemini API:

Question → Model response → Tool execution → Observation → Final answer

The project uses no agent framework and runs the model through a hosted API.

## Current features

- Configurable Gemini model.
- Web search through DuckDuckGo Instant Answer.
- Local file reading.
- Python calculations and data processing.
- Conversation history containing real tool results.
- A maximum step limit.
- Terminal output showing actions and observations.

## Setup

Requires Python 3.10 or newer. Tested with Python 3.13.7 on Windows.

```powershell
git clone https://github.com/Ahmedlslv/ReAct_Agent_From_Scratch.git
cd ReAct_Agent_From_Scratch
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Set the model for the current PowerShell session:

```powershell
$env:GEMINI_MODEL = "gemini-3.5-flash-lite"
```

The basic agent reads `GEMINI_API_KEY` from the environment. If it is missing,
the agent prompts for the key with hidden input. Never commit API keys.

API model availability and free-tier quotas depend on your project.

## Run

From the repository root:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m agent.base "Say hello. Do not use any tools."
```

Read a local file:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m agent.base "Use file_read to read data/notes.txt, then tell me the author."
```

The included file contains `author: riadh`.

## Architecture

1. Initialize conversation history with the user's question.
2. Send the history and system instructions to Gemini.
3. Parse the response for an `Answer:` or an `Action:`.
4. Execute the selected tool in Python.
5. Append the model response and real tool observation to the history.
6. Repeat until an answer is returned or the step limit is reached.

Tool calls currently use a text format:

```text
Action: file_read("data/notes.txt")
```

The Python program executes tools; the model does not directly access local files.

## Manual validation

- Greeting: returned a final answer without using tools.
- File reading: retrieved the included file and identified its author.
- Multi-tool task: searched for Einstein, recovered from a Python syntax
  error, calculated an age of 76, and finished in six steps.

The Einstein search did not retrieve his exact birth and death dates.
The model supplied those from its existing knowledge, so that run demonstrated
tool execution and recovery rather than complete source verification.

## Limitations

- Text parsing depends on the model following the requested format.
- DuckDuckGo Instant Answer provides limited coverage and may return no result.
- Python execution uses unrestricted eval/exec and is not sandboxed.
- File access is not yet restricted to the data folder.
- API quotas and temporary failures can interrupt runs.
- The reflection module still uses Anthropic.
- Included evaluation reports describe an earlier implementation and have
  not been reproduced with this Gemini baseline.
- This baseline has manual validation but no automated tests.

## Planned improvements

- Structured tool calls with validated arguments.
- Controlled calculation tools instead of unrestricted Python execution.
- Restricted file access.
- Clear API failure handling and bounded retries.
- Evaluations with verifiable expected outcomes.
- A short terminal demo.
