"""CLI args, .env, paths and logging. Parsed once at import time (this is a single-purpose daemon)."""
import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

# Stripped from the environment of every command the model runs
SECRET_KEYS = ("MISTRAL_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def _env(name, default):
    return type(default)(os.getenv(name, default))


def parse(argv=None):
    ap = argparse.ArgumentParser(prog="agentd", description="Autonomous coding agent (Mistral + Telegram)")
    ap.add_argument("--goal", help="What to build (first run only)")
    ap.add_argument("--goal-file", help="Read the goal from a file (first run only)")
    ap.add_argument("--workspace", default=_env("WORKSPACE", "./project"))
    ap.add_argument("--model", default=_env("MISTRAL_MODEL", "mistral-large-4"))
    ap.add_argument("--budget", type=float, default=_env("BUDGET_USD", 250.0), help="USD hard stop")
    ap.add_argument("--price-in", type=float, default=_env("PRICE_IN", 1.36), help="USD per 1M input tokens")
    ap.add_argument("--price-out", type=float, default=_env("PRICE_OUT", 4.18), help="USD per 1M output tokens")
    ap.add_argument("--hours", type=float, default=_env("HOURS", 140.0),
                    help="Wall-clock limit, fixed at first run and persisted across restarts")
    ap.add_argument("--max-turns", type=int, default=40, help="Tool-call turns per agent episode")
    ap.add_argument("--context-tokens", type=int, default=80_000, help="Approx context cap before old output is trimmed")
    ap.add_argument("--test-cmd", default="python -m pytest -x -q")
    ap.add_argument("--endless", action="store_true", help="Keep inventing improvements until the budget is gone")
    ap.add_argument("--tasks-per-review", type=int, default=8)
    ap.add_argument("--heartbeat-min", type=float, default=_env("HEARTBEAT_MIN", 30.0), help="Telegram status ping interval")
    return ap.parse_args(argv)


cfg = parse()
cfg.ws = Path(cfg.workspace).resolve()
cfg.meta = cfg.ws / ".agent"
cfg.meta.mkdir(parents=True, exist_ok=True)
cfg.state_f = cfg.meta / "state.json"
cfg.spend_f = cfg.meta / "spend.json"
cfg.run_f = cfg.meta / "run.json"
cfg.log_f = cfg.meta / "agent.log"
cfg.api_key = os.environ.get("MISTRAL_API_KEY") or sys.exit("Set MISTRAL_API_KEY (in .env or the environment)")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(cfg.log_f)],
)
logging.getLogger("urllib3").setLevel(logging.WARNING)  # urllib3 can log URLs containing the bot token
log = logging.info
