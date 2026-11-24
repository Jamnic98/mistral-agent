"""Entry point: first-run setup, then the PLAN -> WORK -> REVIEW loop until stop."""
import sys
import traceback
from pathlib import Path

from .config import cfg, log
from .runtime import (CURRENT, STATE_REF, Stop, check_limits, load_json, notify,
                      save_json, spend)
from .tasks import do_task, git_commit, plan_tasks
from .tg import start_telegram
from .tools import ensure_venv, sh


def _goal():
    if cfg.goal:
        return cfg.goal
    if cfg.goal_file:
        return Path(cfg.goal_file).read_text().strip()
    return None


def run():
    state = load_json(cfg.state_f, None)
    if state is None:
        goal = _goal()
        if not goal:
            sys.exit("--goal (or --goal-file) required on first run")
        state = {"goal": goal, "tasks": [], "empty_reviews": 0}
        if not (cfg.ws / ".git").exists():
            sh("git init -q")
        (cfg.ws / ".gitignore").write_text(".agent/\n__pycache__/\n.venv/\n.pytest_cache/\n")
        ensure_venv()
    state.setdefault("directives", [])
    state.setdefault("untriaged", [])
    STATE_REF["s"] = state
    start_telegram()
    notify(f"🚀 Agent {'resumed' if state['tasks'] else 'started'}\nGoal: {state['goal'][:500]}\n"
           f"Model: {cfg.model} | budget ${cfg.budget:.0f} | {cfg.hours:.0f}h\n/help for commands")
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
                save_json(cfg.state_f, state)
                if not n and state["empty_reviews"] >= (50 if cfg.endless else 2):
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
            save_json(cfg.state_f, state)
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
        save_json(cfg.state_f, state)
        git_commit("agent stopped: checkpoint")
        final = f"Final: ${spend['usd']:.2f} | {spend['in']:,} in / {spend['out']:,} out | {spend['calls']} calls"
        log(final)
        notify("📊 " + final)
