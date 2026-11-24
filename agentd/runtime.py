"""Shared mutable runtime state: persistence, spend tracking, pause/stop, limits, notifications."""
import json
import threading
import time

from .config import cfg, log

LOCK = threading.RLock()
PAUSED = threading.Event()
STOP_EVT = threading.Event()
INBOX = []                  # owner messages to inject into the running episode
CURRENT = {"task": None}    # for /status
STATE_REF = {}              # {"s": state dict}, set by main
START = time.time()
_hb = {"last": time.time(), "milestone": 0}
_sink = [None]


class Stop(Exception):
    """Raised to unwind the agent: budget, deadline, or /stop."""


# ---- notifications (sink is wired to Telegram in main; keeps this module dependency-free)
def set_notifier(fn):
    _sink[0] = fn


def notify(text):
    if _sink[0]:
        _sink[0](text)


# ---- persistence
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


spend = load_json(cfg.spend_f, {"usd": 0.0, "in": 0, "out": 0, "calls": 0})
_hb["milestone"] = int(spend["usd"] // 25)

# The deadline is fixed on the very first run, so service restarts don't reset the clock
RUN = load_json(cfg.run_f, None)
if RUN is None:
    RUN = {"deadline": time.time() + cfg.hours * 3600}
    save_json(cfg.run_f, RUN)


def record_usage(u):
    with LOCK:
        pi, po = u.get("prompt_tokens", 0), u.get("completion_tokens", 0)
        spend["in"] += pi
        spend["out"] += po
        spend["calls"] += 1
        # Conservative: bills all input at full price (ignores cache discount)
        spend["usd"] += (pi * cfg.price_in + po * cfg.price_out) / 1e6
        save_json(cfg.spend_f, spend)
    m = int(spend["usd"] // 25)
    if m > _hb["milestone"]:
        _hb["milestone"] = m
        notify(f"💸 Spend milestone: ${spend['usd']:.2f} of ${cfg.budget:.0f}")


def status_text():
    s = STATE_REF.get("s") or {"tasks": []}
    c = {k: sum(t["status"] == k for t in s["tasks"]) for k in ("done", "pending", "failed")}
    left = max(RUN["deadline"] - time.time(), 0) / 3600
    return (f"{'⏸ PAUSED' if PAUSED.is_set() else '🟢 running'}\n"
            f"Task: {CURRENT['task'] or '(planning/reviewing)'}\n"
            f"Tasks: {c['done']} done, {c['pending']} pending, {c['failed']} failed\n"
            f"Spend: ${spend['usd']:.2f} / ${cfg.budget:.0f} | {spend['calls']} calls | "
            f"{spend['in']:,} in / {spend['out']:,} out\n"
            f"Uptime {(time.time() - START) / 3600:.1f}h | {left:.1f}h until deadline")


def check_limits():
    """Called before every API call."""
    if STOP_EVT.is_set():
        raise Stop("Stopped via Telegram")
    if PAUSED.is_set():
        t0 = time.time()
        while PAUSED.is_set() and not STOP_EVT.is_set():
            time.sleep(2)
        RUN["deadline"] += time.time() - t0  # pause time doesn't eat the deadline
        save_json(cfg.run_f, RUN)
        if STOP_EVT.is_set():
            raise Stop("Stopped via Telegram")
    if spend["usd"] >= cfg.budget:
        raise Stop(f"Budget reached: ${spend['usd']:.2f}")
    if time.time() > RUN["deadline"]:
        raise Stop("Deadline reached")
    if time.time() - _hb["last"] > cfg.heartbeat_min * 60:
        _hb["last"] = time.time()
        notify("💓 " + status_text())
