# CTF Agent

Local CTF solver swarm driven by chat web sessions. The system no longer uses
LLM APIs or CTFd APIs; challenges are read from local task folders and candidate
flags are verified by the operator in the terminal.

## How It Works

```text
task/<challenge>/README.md
  -> local task poller
  -> webchat coordinator
  -> ChallengeSwarm
  -> enabled webchat solvers
  -> one Docker sandbox per solver
  -> terminal flag verification
```

Each solver runs commands through an isolated Docker sandbox with CTF tools. The
chat web automation layer is intentionally separated from the solver logic; DOM
selectors live in `backend/chatweb/` and can be filled in for ChatGPT web UI.

## 1. Prerequisites

- Python 3.14+
- `uv`
- Docker Desktop running with Linux containers
- Chrome installed
- Browser profile already logged into the chat web providers you enable

Build the sandbox image once:

```powershell
docker build -f sandbox\Dockerfile.sandbox -t ctf-sandbox .
```

If Docker reports it cannot connect to `dockerDesktopLinuxEngine`, start Docker
Desktop and wait until `docker info` works.

## 2. Install Dependencies

```powershell
uv sync
```

## 3. Configure `.env`

Create your local `.env`:

```powershell
copy .env.example .env
```

Important values:

```env
# Folder containing local tasks.
TASKS_DIR=task

# Runtime/generated folder. The agent writes normalized challenge folders here.
LOCAL_CHALLENGES_DIR=challenges-local

# Existing browser profile that is already logged in.
WEBCHAT_BROWSER_USER_DATA_DIR=C:\Users\<you>\AppData\Local\Google\Chrome\User Data
WEBCHAT_BROWSER_PROFILE=Default

# Keep false while developing selectors.
WEBCHAT_HEADLESS=false

# Terminal mode.
# false = normal Rich UI; true = debug logs.
TERMINAL_DEBUG=false

# Max tasks solved in parallel.
# Total containers ~= MAX_CONCURRENT_CHALLENGES * enabled model count.
MAX_CONCURRENT_CHALLENGES=1

# Per-container Docker limits.
CONTAINER_MEMORY_LIMIT=16g
CONTAINER_CPU_LIMIT=2.0
```

Model toggles:

```env
ENABLE_CHATGPT_O3_MEDIUM=true
ENABLE_CHATGPT_GPT55_HIGH=true
ENABLE_CHATGPT_GPT54_HIGH=true
```

Disable models to reduce browser sessions and Docker containers.

## 4. Login Browser Profile

Open Chrome with the same browser profile configured in `.env`, then log into
ChatGPT in that Chrome window.

First check these two values in `.env`:

```env
WEBCHAT_BROWSER_USER_DATA_DIR=...
WEBCHAT_BROWSER_PROFILE=...
```

Then open Chrome with those values:

```powershell
chrome.exe --user-data-dir="<WEBCHAT_BROWSER_USER_DATA_DIR>" --profile-directory="<WEBCHAT_BROWSER_PROFILE>"
```

For the default project-local profile:

```powershell
chrome.exe --user-data-dir=".chrome-profile" --profile-directory="Default"
```

If `.chrome-profile` does not exist yet, Chrome will create a new empty profile
there. Log into ChatGPT once in that new profile before running the agent.

That example matches:

```env
WEBCHAT_BROWSER_USER_DATA_DIR=.chrome-profile
WEBCHAT_BROWSER_PROFILE=Default
```

After logging in, close all Chrome windows using that profile before running the
agent. ChromeDriver cannot reliably attach to the same profile while another Chrome
process is already using it.

## 5. Task Folder Layout

Each task is a subfolder under `TASKS_DIR` and must contain `README.md`.

```text
task/
  baby-rev/
    README.md       # challenge statement
    chall.bin       # copied to /challenge/distfiles/chall.bin
    notes/
      data.txt      # copied to /challenge/distfiles/notes/data.txt
```

`README.md` becomes the prompt description. Every other file or directory
under the task folder is mirrored into the runtime `distfiles/` directory inside each solver container,
except agent state files such as `SOLVED.txt` and `WRITEUP.md`.

## 6. Run All Local Tasks

Use config values from `.env`:

```powershell
uv run ctf-solve
```

Equivalent explicit form:

```powershell
uv run ctf-solve --tasks-dir task
```

## 7. Run One Task

```powershell
uv run ctf-solve --challenge task\baby-rev
```

## 8. CLI Options

| Option | Meaning | Default/source |
|---|---|---|
| `--image TEXT` | Docker sandbox image name. | `SANDBOX_IMAGE` / `ctf-sandbox` |
| `--models TEXT` | Solver model spec. Can be passed multiple times. Overrides enabled default models. | Enabled model toggles |
| `--challenge TEXT` | Run one task directory instead of coordinator mode. | Not set |
| `--tasks-dir TEXT` | Folder containing local tasks. | `TASKS_DIR` / `task` |
| `--challenges-dir TEXT` | Runtime normalized challenge folder. | `LOCAL_CHALLENGES_DIR` / `challenges-local` |
| `--coordinator-model TEXT` | Model name used by coordinator web provider. | Provider default |
| `--max-challenges INTEGER` | Temporary override for `MAX_CONCURRENT_CHALLENGES`. | `.env` / config |
| `--msg-port INTEGER` | Local operator message HTTP port. `0` means auto-pick. | `0` |
| `-v`, `--verbose` | Enable debug logging and bypass normal terminal UI mode. | `false` |

Examples:

```powershell
# Override max parallel tasks just for this run.
uv run ctf-solve --max-challenges 2 -v

# Run only one model.
uv run ctf-solve --models chatweb/chatgpt/gpt-5.5-high -v

# Run a different task root.
uv run ctf-solve --tasks-dir task-test -v
```

Normal terminal UI is used when `TERMINAL_DEBUG=false` and `-v` is not passed.
In this mode, single-task runs show a live solver table with:

- `State`: current solver state such as `starting`, `running`, `tool`, `verifying`, `error`, or `flag_found`.
- `Current action`: the exact phase the solver is in, for example opening chat web, waiting for model response, running `bash`, or operator flag verification.
- `Action time`: how long the current action has been running.
- `Total time`: how long that model has been active for this task.
- `Steps`: number of tool steps executed by that solver.
- `Detail`: command/path/URL/flag candidate or short result summary for the current action.

Debug mode is enabled by either:

```powershell
uv run ctf-solve -v
```

or:

```env
TERMINAL_DEBUG=true
```

## 9. Docker Container Count

Each solver gets one container.

```text
container count ~= active tasks * enabled models
```

Examples:

```text
MAX_CONCURRENT_CHALLENGES=1 and 3 models enabled -> up to 3 containers
MAX_CONCURRENT_CHALLENGES=2 and 3 models enabled -> up to 6 containers
```

## 10. Flag Verification

When a solver finds a candidate, the process pauses and asks:

```text
========================================================================
Pending flag #1
Challenge: baby-rev
Model: chatweb/chatgpt/gpt-5.5-high
FLAG: flag{...}
========================================================================
is that correct (y/n):
```

- `y`: write `task/<challenge>/SOLVED.txt`, create `task/<challenge>/WRITEUP.md` from the winning solver trace if missing, and stop the swarm.
- `n`: mark candidate wrong and resume solving.

If multiple tasks/models find flags at the same time, candidates are queued and
the terminal asks for one verification at a time.

`WRITEUP.md` includes metadata, flag, winning model, trace path, solver method,
key tool calls, important output, and any solver-created `solve`/`exploit`
script captured in the trace.

## 11. Current Chat Web DOM Status

The chat web architecture is wired, but some provider DOM selectors are still
templates or best-effort selectors in `backend/chatweb/`:

- `select_model()`
- `send_message()`
- `wait_for_response()`
- `upload_files()`

Until those methods are implemented for the target web UI, the app can prepare
tasks, start Docker, and open Chrome, but actual chat send/read/upload will
raise `NotImplementedError`.
