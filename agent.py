#!/usr/bin/env python3
"""
Long-running autonomous coding agent for the Mistral API, with Telegram control.

Flow:  PLAN -> (WORK on task -> run tests -> self-fix -> git commit)* -> REVIEW -> new tasks -> repeat
Stops when: budget spent, deadline hit, /stop from Telegram, or reviewer finds nothing left (unless --endless).
Resumable: state lives in <workspace>/.agent/state.json. Just rerun the same command.

Setup:
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env     # then fill in your keys
    python agent.py --goal "Build a CLI task manager with SQLite, full tests, and docs" --endless

RUN THIS IN A DOCKER CONTAINER OR VM. The model executes shell commands.
The denylist below is a speed bump, not a sandbox.
"""
import argparse
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import requests
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

API_URL = "https://api.mistral.ai/v1/chat/completions"
SECRET_KEYS = ("MISTRAL_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def env(name, default):
    return type(default)(os.getenv(name, default))


# ----------------------------------------------------------------------------- config
ap = argparse.ArgumentParser()
ap.add_argument("--goal", help="What to build (required on first run)")
ap.add_argument("--workspace", default=env("WORKSPACE", "./project"))
ap.add_argument("--model", default=env("MISTRAL_MODEL", "mistral-large-4"))
ap.add_argument("--budget", type=float, default=env("BUDGET_USD", 250.0), help="USD hard stop")
ap.add_argument("--price-in", type=float, default=env("PRICE_IN", 1.36), help="USD per 1M input tokens")
ap.add_argument("--price-out", type=float, default=env("PRICE_OUT", 4.18), help="USD per 1M output tokens")
ap.add_argument("--hours", type=float, default=env("HOURS", 140.0), help="Wall-clock limit")
ap.add_argument("--max-turns", type=int, default=40, help="Tool-call turns per agent episode")
ap.add_argument("--context-tokens", type=int, default=80_000, help="Approx context cap before old output is trimmed")
ap.add_argument("--test-cmd", default="python -m pytest -x -q")
ap.add_argument("--endless", action="store_true", help="Keep inventing improvements until the budget is gone")
ap.add_argument("--tasks-per-review", type=int, default=8)
ap.add_argument("--heartbeat-min", type=float, default=env("HEARTBEAT_MIN", 30.0), help="Telegram status ping interval")
args = ap.parse_args()

WS = Path(args.workspace).resolve()
META = WS / ".agent"
META.mkdir(parents=True, exist_ok=True)
STATE_F, SPEND_F = META / "state.json", META / "spend.json"
START = time.time()
DEADLINE = time.time() + args.hours * 3600
API_KEY = os.environ.get("MISTRAL_API_KEY")
if not API_KEY:
    sys.exit("Set MISTRAL_API_KEY (in .env or environment)")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(META / "agent.log")],
)
log = logging.info
logging.getLogger("urllib3").setLevel(logging.WARNING)  # urllib3 can log URLs containing the bot token

LOCK = threading.RLock()
PAUSED = threading.Event()
STOP_EVT = threading.Event()
INBOX = []  # owner messages to inject into the running episode
CURRENT = {"task": None}
STATE_REF = {}
HB = {"last": time.time(), "milestone": 0}


class Stop(Exception):
    pass


# ----------------------------------------------------------------------------- persistence
def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def save_json(p, obj):
    with LOCK:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=2))
        tmp.replace(p)


spend = load_json(SPEND_F, {"usd": 0.0, "in": 0, "out": 0, "calls": 0})
HB["milestone"] = int(spend["usd"] // 25)


def record_usage(u):
    with LOCK:
        pi, po = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        spend["in"] += pi
        spend["out"] += po
        spend["calls"] += 1
        # Conservative: bills all input at full price (ignores cache discount)
        spend["usd"] += (pi * args.price_in + po * args.price_out) / 1e6
        save_json(SPEND_F, spend)
    m = int(spend["usd"] // 25)
    if m > HB["milestone"]:
        HB["milestone"] = m
        notify(f"💸 Spend milestone: ${spend['usd']:.2f} of ${args.budget:.0f}")


def check_limits():
    global DEADLINE
    if STOP_EVT.is_set():
        raise Stop("Stopped via Telegram")
    if PAUSED.is_set():
        t0 = time.time()
        while PAUSED.is_set() and not STOP_EVT.is_set():
            time.sleep(2)
        DEADLINE += time.time() - t0  # pause time doesn't eat the deadline
        if STOP_EVT.is_set():
            raise Stop("Stopped via Telegram")
    if spend["usd"] >= args.budget:
        raise Stop(f"Budget reached: ${spend['usd']:.2f}")
    if time.time() > DEADLINE:
        raise Stop("Deadline reached")
    if time.time() - HB["last"] > args.heartbeat_min * 60:
        HB["last"] = time.time()
        notify("💓 " + status_text())


# ----------------------------------------------------------------------------- telegram
class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id or "").strip()
        self.base = f"https://api.telegram.org/bot{token}"
        self.enabled = bool(token)

    def call(self, method, http_timeout=15, **params):
        try:
            return requests.post(f"{self.base}/{method}", json=params, timeout=http_timeout).json()
        except Exception:  # never log the exception: it can contain the token in the URL
            return {"ok": False}

    def send(self, text, chat_id=None):
        cid = chat_id or self.chat_id
        if not (self.enabled and cid):
            return
        for i in range(0, max(len(text), 1), 3900):
            self.call("sendMessage", chat_id=cid, text=text[i:i + 3900], disable_web_page_preview=True)

    def start(self):
        if not self.enabled:
            log("Telegram disabled (no TELEGRAM_BOT_TOKEN)")
            return
        self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in COMMANDS])
        threading.Thread(target=self._poll, daemon=True).start()

    def _poll(self):
        # Discard anything sent while we were offline so stale /stop etc. don't fire
        res = self.call("getUpdates", offset=-1)
        offset = (res["result"][-1]["update_id"] + 1) if res.get("result") else None
        while True:
            try:
                res = self.call("getUpdates", http_timeout=45, timeout=30, offset=offset, allowed_updates=["message"])
                if not res.get("ok"):
                    time.sleep(5)
                    continue
                for u in res["result"]:
                    offset = u["update_id"] + 1
                    m = u.get("message") or {}
                    text, cid = (m.get("text") or "").strip(), str((m.get("chat") or {}).get("id", ""))
                    if not text:
                        continue
                    if not self.chat_id:
                        self.send(f"Your chat ID is {cid}\nPut TELEGRAM_CHAT_ID={cid} in .env and restart the agent.", cid)
                    elif cid == self.chat_id:  # ONLY the owner can command the agent
                        try:
                            handle_message(text)
                        except Exception as e:
                            self.send(f"⚠️ command error: {e!r}")
                    else:
                        log(f"ignored Telegram message from unauthorized chat {cid}")
            except Exception:
                time.sleep(5)


TG = Telegram(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
notify = TG.send

COMMANDS = [
    ("status", "Current task, spend, progress"),
    ("tasks", "Show the backlog"),
    ("add", "/add <text> queue a task at the end"),
    ("next", "/next <text> queue a task at the front"),
    ("pause", "Pause between API calls"),
    ("resume", "Resume"),
    ("log", "Recent log lines"),
    ("tree", "Workspace file list"),
    ("stop", "Stop gracefully (resumable)"),
    ("help", "List commands"),
]


def status_text():
    s = STATE_REF.get("s") or {"tasks": []}
    c = {k: sum(t["status"] == k for t in s["tasks"]) for k in ("done", "pending", "failed")}
    left = max(DEADLINE - time.time(), 0) / 3600
    return (f"{'⏸ PAUSED' if PAUSED.is_set() else '🟢 running'}\n"
            f"Task: {CURRENT['task'] or '(planning/reviewing)'}\n"
            f"Tasks: {c['done']} done, {c['pending']} pending, {c['failed']} failed\n"
            f"Spend: ${spend['usd']:.2f} / ${args.budget:.0f} | {spend['calls']} calls | "
            f"{spend['in']:,} in / {spend['out']:,} out\n"
            f"Uptime {(time.time() - START) / 3600:.1f}h | {left:.1f}h until deadline")


def handle_message(text):
    s = STATE_REF.get("s")
    if s is None:
        return notify("Agent still starting up, try again in a moment.")
    cmd, _, rest = text.partition(" ")
    cmd, rest = cmd.lower().split("@")[0], rest.strip()
    if cmd in ("/start", "/help"):
        notify("Commands:\n" + "\n".join(f"/{c} - {d}" for c, d in COMMANDS) +
               "\n\nAny plain text is treated as guidance/requests: it goes to the running agent "
               "immediately and gets triaged into the backlog after the current task.")
    elif cmd == "/status":
        notify(status_text())
    elif cmd == "/tasks":
        pend = [t for t in s["tasks"] if t["status"] == "pending"]
        lines = [f"#{t['id']} {t['title']}" for t in pend[:15]]
        notify(f"{len(pend)} pending:\n" + ("\n".join(lines) or "(none)"))
    elif cmd in ("/add", "/next"):
        if not rest:
            return notify(f"Usage: {cmd} <task description>")
        t = add_task(s, rest[:80], rest, front=(cmd == "/next"))
        notify(f"Queued #{t['id']} {'at the front' if cmd == '/next' else 'at the end'}.")
    elif cmd == "/pause":
        PAUSED.set()
        notify("⏸ Pausing before the next API call.")
    elif cmd == "/resume":
        PAUSED.clear()
        notify("▶️ Resumed.")
    elif cmd == "/log":
        lines = (META / "agent.log").read_text(errors="replace").splitlines()[-25:]
        notify("\n".join(l[:200] for l in lines)[-3800:])
    elif cmd == "/tree":
        notify(tree()[:3800])
    elif cmd == "/stop":
        STOP_EVT.set()
        notify("🛑 Stopping after the current API call. State is saved; rerun the script to resume.")
    elif cmd.startswith("/"):
        notify("Unknown command. /help")
    else:
        with LOCK:
            s["directives"].append(text)
            s["untriaged"].append(text)
            INBOX.append(text)
            save_json(STATE_F, s)
        notify("📨 Got it. Passed to the running agent now; I'll turn it into backlog tasks after the current task.")


# ----------------------------------------------------------------------------- LLM client
def flatten(content):
    if isinstance(content, list):  # reasoning models can return typed chunks
        return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return content or ""


def chat(messages, tools=None):
    body = {"model": args.model, "messages": messages, "temperature": 0.3, "max_tokens": 8192}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    for attempt in range(10):
        check_limits()
        try:
            r = requests.post(API_URL, headers={"Authorization": f"Bearer {API_KEY}"}, json=body, timeout=600)
            if r.status_code in (429, 500, 502, 503, 504):
                wait = min(2 ** attempt * 2, 120)
                log(f"HTTP {r.status_code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"API error {r.status_code}: {r.text[:500]}")
            data = r.json()
            record_usage(data.get("usage", {}))
            m = data["choices"][0]["message"]
            out = {"role": "assistant", "content": flatten(m.get("content"))}
            if m.get("tool_calls"):
                out["tool_calls"] = m["tool_calls"]
            return out
        except (requests.Timeout, requests.ConnectionError) as e:
            wait = min(2 ** attempt * 2, 120)
            log(f"Network error {type(e).__name__}, retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("API unreachable after retries")


# ----------------------------------------------------------------------------- tools
def tool(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}


S = {"type": "string"}
BASE_TOOLS = [
    tool("list_files", "Recursively list files in the workspace.", {"subdir": S}, []),
    tool("read_file", "Read a text file.", {"path": S}, ["path"]),
    tool("write_file", "Create/overwrite a file with full contents.", {"path": S, "content": S}, ["path", "content"]),
    tool("run_command", "Run a shell command in the workspace (120s timeout).", {"command": S}, ["command"]),
]
WORKER_TOOLS = BASE_TOOLS + [tool("finish_task", "Call when the task is done and tests pass.", {"summary": S}, ["summary"])]
REVIEW_TOOLS = BASE_TOOLS + [tool(
    "add_tasks", "Submit new backlog tasks. Pass an empty list if nothing is needed.",
    {"tasks": {"type": "array", "items": {"type": "object", "properties": {"title": S, "description": S},
                                          "required": ["title", "description"]}}}, ["tasks"])]

DENY = re.compile(r"\b(sudo|mkfs|shutdown|reboot|curl|wget|ssh|scp|nc|ncat|printenv|env)\b|rm\s+-rf\s+(/|~|\*)"
                  r"|:\(\)\s*\{|>\s*/dev/sd|\.env|/proc|MISTRAL|TELEGRAM|\.\./")


def safe_path(rel):
    p = (WS / rel).resolve()
    if p != WS and WS not in p.parents:
        raise ValueError("path escapes workspace")
    if META in p.parents or p == META:
        raise ValueError(".agent is off limits")
    return p


def tree(sub=""):
    root = safe_path(sub or ".")
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(WS)
        if any(part in {".git", ".agent", "__pycache__", ".venv", "node_modules", ".pytest_cache"} for part in rel.parts):
            continue
        if p.is_file():
            out.append(str(rel))
        if len(out) >= 300:
            out.append("... (truncated)")
            break
    return "\n".join(out) or "(empty)"


def sh(cmd, timeout=120):
    clean_env = {k: v for k, v in os.environ.items() if k not in SECRET_KEYS}
    try:
        r = subprocess.run(cmd, shell=True, cwd=WS, capture_output=True, text=True, timeout=timeout, env=clean_env)
        out = f"[exit {r.returncode}]\n{r.stdout}{r.stderr}"
        return r.returncode, (out if len(out) < 8000 else out[:4000] + "\n...[truncated]...\n" + out[-4000:])
    except subprocess.TimeoutExpired:
        return 124, f"[timeout after {timeout}s]"


def run_tool(name, a):
    try:
        if name == "list_files":
            return tree(a.get("subdir", ""))
        if name == "read_file":
            t = safe_path(a["path"]).read_text(errors="replace")
            return t if len(t) < 30000 else t[:30000] + "\n...[truncated]"
        if name == "write_file":
            p = safe_path(a["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(a["content"])
            return f"wrote {a['path']} ({len(a['content'])} chars)"
        if name == "run_command":
            if DENY.search(a["command"]):
                return "BLOCKED: command not allowed"
            return sh(a["command"])[1]
        return f"unknown tool {name}"
    except Exception as e:
        return f"ERROR: {e!r}"


# ----------------------------------------------------------------------------- context mgmt
def est_tokens(messages):
    return sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "")) for m in messages) // 4


def compact(messages, keep_last=12):
    """Shrink old tool outputs and old file writes; keeps message pairing intact."""
    if est_tokens(messages) < args.context_tokens:
        return
    for m in messages[2:-keep_last]:
        if m["role"] == "tool" and len(m["content"]) > 300:
            m["content"] = m["content"][:200] + "\n...[old output trimmed]"
        for tc in m.get("tool_calls") or []:
            fn = tc["function"]
            if fn["name"] == "write_file" and len(fn["arguments"]) > 400:
                try:
                    a = json.loads(fn["arguments"])
                    a["content"] = "[omitted: see file on disk]"
                    fn["arguments"] = json.dumps(a)
                except Exception:
                    pass


def inject_owner_messages(messages):
    """Deliver Telegram messages mid-episode. Appended to the last tool result because some
    Mistral endpoints reject a 'user' message directly after a 'tool' message."""
    with LOCK:
        pending, INBOX[:] = list(INBOX), []
    if not pending:
        return
    note = "\n\n[MESSAGE FROM PROJECT OWNER via Telegram, high priority, take into account:]\n" + "\n".join(pending)
    if messages[-1]["role"] == "tool":
        messages[-1]["content"] += note
    else:
        messages.append({"role": "user", "content": note.strip()})
    log(f"  injected owner message(s): {pending!r}"[:200])


# ----------------------------------------------------------------------------- agent loop
def agent_loop(system, user, tools, terminal):
    """Runs until `terminal` tool is called. Returns its args dict, or None on failure."""
    with LOCK:
        INBOX.clear()  # the episode prompt already contains current directives
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for turn in range(args.max_turns):
        compact(messages)
        if est_tokens(messages) > args.context_tokens * 1.5:
            log("context too large, abandoning episode")
            return None
        inject_owner_messages(messages)
        msg = chat(messages, tools)
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            messages.append({"role": "user", "content": f"Keep going with tools. Call {terminal} when finished."})
            continue
        done = None
        for c in calls:
            fn = c["function"]
            try:
                a = fn["arguments"] if isinstance(fn["arguments"], dict) else json.loads(fn["arguments"] or "{}")
            except json.JSONDecodeError:
                result = "ERROR: invalid JSON arguments"
            else:
                if fn["name"] == terminal:
                    done, result = a, "ok"
                else:
                    result = run_tool(fn["name"], a)
                    log(f"  [{turn}] {fn['name']} {str(a)[:90]!r} -> {result[:70]!r}")
            messages.append({"role": "tool", "tool_call_id": c["id"], "name": fn["name"], "content": result})
        if done is not None:
            return done
    return None


# ----------------------------------------------------------------------------- prompts
WORKER_SYS = """You are a senior engineer working autonomously in a sandboxed workspace.
Rules: write complete, working files (never placeholders). Write tests alongside code and run them.
Use run_command to execute code and fix your own errors. Keep NOTES.md updated with architecture
decisions and conventions. Use `python` and `pip` normally. Call finish_task only when the task is
complete and tests pass. If you receive a message from the project owner, treat it as high priority."""

REVIEW_SYS = """You are a staff engineer reviewing an autonomously-built codebase and managing the backlog.
Inspect files, run the tests, and look for: bugs, missing features vs the goal, weak test coverage,
missing error handling, performance issues, docs gaps, refactors. Then call add_tasks with
concrete, independently-implementable tasks (each completable in one focused session).
Owner directives (from Telegram) always take priority over your own ideas.
Modes: INITIAL PLAN = ordered backlog to build from scratch. REVIEW = improvements to the existing code.
TRIAGE = convert the owner's new requests into concrete tasks (one or more per request)."""


# ----------------------------------------------------------------------------- orchestration
def add_task(state, title, desc, front=False):
    with LOCK:
        t = {"id": max([x["id"] for x in state["tasks"]], default=0) + 1,
             "title": title, "description": desc, "status": "pending", "attempts": 0}
        if front:
            idx = next((i for i, x in enumerate(state["tasks"]) if x["status"] == "pending"), len(state["tasks"]))
            state["tasks"].insert(idx, t)
        else:
            state["tasks"].append(t)
        save_json(STATE_F, state)
    return t


def directive_block(state):
    d = state.get("directives", [])[-15:]
    return ("\n\nOWNER DIRECTIVES (from Telegram, highest priority):\n- " + "\n- ".join(d)) if d else ""


def git_commit(msg):
    sh("git add -A && git -c user.name=agent -c user.email=agent@local commit -qm " + json.dumps(msg[:70]))


def run_tests():
    rc, out = sh(args.test_cmd, timeout=600)
    return rc in (0, 5), out  # 5 = pytest "no tests collected"


def do_task(state, t):
    notes = (WS / "NOTES.md").read_text()[:6000] if (WS / "NOTES.md").exists() else "(none)"
    ctx = (f"PROJECT GOAL:\n{state['goal']}{directive_block(state)}\n\nTASK #{t['id']}: {t['title']}\n"
           f"{t['description']}\n\nCURRENT FILES:\n{tree()}\n\nNOTES.md:\n{notes}")
    res = agent_loop(WORKER_SYS, ctx, WORKER_TOOLS, "finish_task")
    if res is None:
        return False, "agent did not finish (turn/context limit)"
    summary = res.get("summary", "")
    for fix in range(4):
        ok, out = run_tests()
        if ok:
            return True, summary
        log(f"  tests failing, fix attempt {fix + 1}")
        r2 = agent_loop(WORKER_SYS, f"{ctx}\n\nThe test suite is FAILING after your work:\n{out}\n\nFix it.",
                        WORKER_TOOLS, "finish_task")
        summary = (r2 or {}).get("summary", summary)
    return False, f"tests still failing after fixes. Last output:\n{out[-600:]}"


def plan_tasks(state, mode):
    """mode: 'INITIAL PLAN' | 'REVIEW' | 'TRIAGE'. Returns number of tasks added."""
    triage = mode == "TRIAGE"
    reqs = list(state["untriaged"]) if triage else []
    done = [t["title"] for t in state["tasks"] if t["status"] == "done"][-40:]
    failed = [t["title"] for t in state["tasks"] if t["status"] == "failed"]
    pending = [t["title"] for t in state["tasks"] if t["status"] == "pending"]
    user = (f"PROJECT GOAL:\n{state['goal']}{directive_block(state)}\n\nMode: {mode}\n"
            f"{'NEW OWNER REQUESTS TO TRIAGE: ' + json.dumps(reqs) if triage else ''}\n"
            f"Completed: {done}\nPending: {pending}\nFailed (retry or redesign): {failed}\n"
            f"Create up to {args.tasks_per_review} tasks.\n\nFILES:\n{tree()}")
    res = agent_loop(REVIEW_SYS, user, REVIEW_TOOLS, "add_tasks")
    new = (res or {}).get("tasks", [])[: args.tasks_per_review]
    if triage:
        if not new:  # never lose an owner request: fall back to one raw task each
            new = [{"title": r[:80], "description": r} for r in reqs]
        with LOCK:
            state["untriaged"] = [r for r in state["untriaged"] if r not in reqs]
    for t in (reversed(new) if triage else new):
        add_task(state, t["title"], t["description"], front=triage)
    if new:
        notify(f"🗂 {mode}: added {len(new)} task(s)\n" + "\n".join(f"• {t['title']}" for t in new[:10]))
    log(f"{mode} added {len(new)} tasks")
    return len(new)


def main():
    state = load_json(STATE_F, None)
    if state is None:
        if not args.goal:
            sys.exit("--goal required on first run")
        state = {"goal": args.goal, "tasks": [], "empty_reviews": 0}
        if not (WS / ".git").exists():
            sh("git init -q")
        (WS / ".gitignore").write_text(".agent/\n__pycache__/\n.venv/\n.pytest_cache/\n")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pytest"], check=False)
    state.setdefault("directives", [])
    state.setdefault("untriaged", [])
    STATE_REF["s"] = state
    TG.start()
    notify(f"🚀 Agent {'resumed' if state['tasks'] else 'started'}\nGoal: {state['goal'][:500]}\n"
           f"Model: {args.model} | budget ${args.budget:.0f} | {args.hours:.0f}h\n/help for commands")
    try:
        while True:
            check_limits()
            CURRENT["task"] = None
            if state["untriaged"]:
                plan_tasks(state, "TRIAGE")
                continue
            pending = [t for t in state["tasks"] if t["status"] == "pending"]
            if not pending:
                n = plan_tasks(state, "REVIEW" if state["tasks"] else "INITIAL PLAN")
                state["empty_reviews"] = 0 if n else state["empty_reviews"] + 1
                save_json(STATE_F, state)
                if not n and state["empty_reviews"] >= (50 if args.endless else 2):
                    notify("🏁 Reviewer reports nothing left to do.")
                    break
                continue
            t = pending[0]
            t["attempts"] += 1
            CURRENT["task"] = f"#{t['id']} {t['title']}"
            log(f"=== Task {t['id']}/{len(state['tasks'])}: {t['title']} (attempt {t['attempts']}) | ${spend['usd']:.2f}")
            notify(f"▶️ Task {t['id']}/{len(state['tasks'])}: {t['title']}")
            try:
                ok, summary = do_task(state, t)
            except Stop:
                t["attempts"] -= 1  # interrupted, not failed
                raise
            t["status"] = "done" if ok else ("failed" if t["attempts"] >= 3 else "pending")
            git_commit(f"task {t['id']}: {t['title']} [{t['status']}]")
            save_json(STATE_F, state)
            if ok:
                notify(f"✅ Task {t['id']} done: {t['title']}\n{summary[:600]}\n💰 ${spend['usd']:.2f}")
            else:
                retry = "will retry" if t["status"] == "pending" else "giving up on it"
                notify(f"❌ Task {t['id']} failed ({retry}): {t['title']}\n{summary[:600]}")
    except Stop as e:
        log(f"STOPPED: {e}")
        notify(f"⏹ Stopped: {e}")
    except KeyboardInterrupt:
        log("Interrupted; state saved, rerun to resume.")
        notify("⏹ Interrupted locally; state saved.")
    except Exception:
        tb = traceback.format_exc()
        log(tb)
        notify(f"💥 Agent crashed:\n{tb[-1500:]}")
        raise
    finally:
        save_json(STATE_F, state)
        git_commit("agent stopped: checkpoint")
        final = f"Final: ${spend['usd']:.2f} | {spend['in']:,} in / {spend['out']:,} out | {spend['calls']} calls"
        log(final)
        notify("📊 " + final)


if __name__ == "__main__":
    main()
