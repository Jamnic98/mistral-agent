"""Telegram bot: owner-only commands, long-polling, message handling."""
import os
import threading
import time

import requests

from .config import cfg, log
from .runtime import (INBOX, LOCK, PAUSED, STATE_REF, STOP_EVT, notify, save_json,
                      set_notifier, status_text)
from .tasks import add_task
from .tools import tree

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
        lines = cfg.log_f.read_text(errors="replace").splitlines()[-25:]
        notify("\n".join(l[:200] for l in lines)[-3800:])
    elif cmd == "/tree":
        notify(tree()[:3800])
    elif cmd == "/stop":
        STOP_EVT.set()
        notify("🛑 Stopping after the current API call. State is saved; rerun to resume.")
    elif cmd.startswith("/"):
        notify("Unknown command. /help")
    else:
        with LOCK:
            s["directives"].append(text)
            s["untriaged"].append(text)
            INBOX.append(text)
            save_json(cfg.state_f, s)
        notify("📨 Got it. Passed to the running agent now; I'll turn it into backlog tasks after the current task.")


def start_telegram():
    tg = Telegram(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
    set_notifier(tg.send)
    tg.start()
    return tg
