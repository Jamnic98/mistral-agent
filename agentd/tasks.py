"""Prompts, backlog management and per-task worker episodes."""
import json

from .config import cfg, log
from .loop import agent_loop
from .runtime import LOCK, notify, save_json
from .tools import REVIEW_TOOLS, WORKER_TOOLS, sh, tree

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


def add_task(state, title, desc, front=False):
    with LOCK:
        t = {"id": max([x["id"] for x in state["tasks"]], default=0) + 1,
             "title": title, "description": desc, "status": "pending", "attempts": 0}
        if front:
            idx = next((i for i, x in enumerate(state["tasks"]) if x["status"] == "pending"), len(state["tasks"]))
            state["tasks"].insert(idx, t)
        else:
            state["tasks"].append(t)
        save_json(cfg.state_f, state)
    return t


def directive_block(state):
    d = state.get("directives", [])[-15:]
    return ("\n\nOWNER DIRECTIVES (from Telegram, highest priority):\n- " + "\n- ".join(d)) if d else ""


def git_commit(msg):
    sh("git add -A && git -c user.name=agent -c user.email=agent@local commit -qm " + json.dumps(msg[:70]))


def run_tests():
    rc, out = sh(cfg.test_cmd, timeout=600)
    return rc in (0, 5), out  # 5 = pytest "no tests collected"


def do_task(state, t):
    notes = (cfg.ws / "NOTES.md").read_text()[:6000] if (cfg.ws / "NOTES.md").exists() else "(none)"
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
            f"Create up to {cfg.tasks_per_review} tasks.\n\nFILES:\n{tree()}")
    res = agent_loop(REVIEW_SYS, user, REVIEW_TOOLS, "add_tasks")
    new = (res or {}).get("tasks", [])[: cfg.tasks_per_review]
    if triage:
        if not new:  # never lose an owner request: fall back to one raw task each
            new = [{"title": r[:80], "description": r} for r in reqs]
        with LOCK:
            state["untriaged"] = [r for r in state["untriaged"] if r not in reqs]
    for t in (reversed(new) if triage else new):
        add_task(state, t["title"], t["description"], front=triage)
    if new:
        notify(f"📋 {mode}: added {len(new)} task(s)\n" + "\n".join(f"• {t['title']}" for t in new[:10]))
    log(f"{mode} added {len(new)} tasks")
    return len(new)
