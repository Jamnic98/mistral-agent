# mistral-agent

Long-running autonomous coding agent for the Mistral API, with Telegram control.

Flow: `PLAN -> (WORK on task -> run tests -> self-fix -> git commit)* -> REVIEW -> new tasks -> repeat`.
Stops when: budget spent, deadline hit, `/stop` from Telegram, or the reviewer finds nothing left (unless `--endless`).
Resumable: state lives in `<workspace>/.agent/state.json`. Just rerun the same command.

The code lives in the `agentd` package (`python -m agentd`):

```
agentd/
  config.py    CLI args, .env, paths, logging (parsed once at import)
  runtime.py   shared state: persistence, spend tracking, pause/stop, limits, notifications
  llm.py       Mistral chat-completions client with retry/backoff and cost accounting
  tools.py     model tools + best-effort sandboxing (denylist, workspace path checks, venv)
  loop.py      episode loop: context trimming, owner-message injection, tool-call turns
  tasks.py     prompts, backlog management, per-task worker episodes
  tg.py        Telegram bot: owner-only commands, long-polling
  main.py      entry point: first-run setup, then the PLAN -> WORK -> REVIEW loop
```

## Setup

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then fill in your keys
python -m agentd --goal "Build a CLI task manager with SQLite, full tests, and docs" --endless
```

> **RUN THIS IN A DOCKER CONTAINER OR VM.** The model executes shell commands.
> The denylist in `tools.py` is a speed bump, not a sandbox.
