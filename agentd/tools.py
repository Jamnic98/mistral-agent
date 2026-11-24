"""Tools the model can call, plus the (best-effort) sandboxing around them."""
import os
import re
import subprocess
import sys

from .config import SECRET_KEYS, cfg, log

VENV = cfg.ws / ".venv"


def _tool(name, desc, props, req):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": req}}}


_S = {"type": "string"}
BASE_TOOLS = [
    _tool("list_files", "Recursively list files in the workspace.", {"subdir": _S}, []),
    _tool("read_file", "Read a text file.", {"path": _S}, ["path"]),
    _tool("write_file", "Create/overwrite a file with full contents.", {"path": _S, "content": _S}, ["path", "content"]),
    _tool("run_command", "Run a shell command in the workspace (120s timeout).", {"command": _S}, ["command"]),
]
WORKER_TOOLS = BASE_TOOLS + [_tool("finish_task", "Call when the task is done and tests pass.", {"summary": _S}, ["summary"])]
REVIEW_TOOLS = BASE_TOOLS + [_tool(
    "add_tasks", "Submit new backlog tasks. Pass an empty list if nothing is needed.",
    {"tasks": {"type": "array", "items": {"type": "object", "properties": {"title": _S, "description": _S},
                                          "required": ["title", "description"]}}}, ["tasks"])]

# A speed bump, not a sandbox. Real isolation comes from the systemd unit / a container.
DENY = re.compile(r"\b(sudo|mkfs|shutdown|reboot|curl|wget|ssh|scp|nc|ncat|printenv|env)\b|rm\s+-rf\s+(/|~|\*)"
                  r"|:\(\)\s*\{|>\s*/dev/sd|\.env|/proc|MISTRAL|TELEGRAM|\.\./")


def ensure_venv():
    """The generated project gets its own venv so `pip install` never touches the system or agentd itself."""
    py = VENV / "bin" / "python"
    if not py.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=False)
    if py.exists() and subprocess.run([str(py), "-c", "import pytest"], capture_output=True).returncode != 0:
        subprocess.run([str(py), "-m", "pip", "install", "-q", "pytest"], check=False)


def _tool_env():
    e = {k: v for k, v in os.environ.items() if k not in SECRET_KEYS}
    if (VENV / "bin").exists():
        e["PATH"] = f"{VENV / 'bin'}{os.pathsep}{e.get('PATH', '')}"
        e["VIRTUAL_ENV"] = str(VENV)
    return e


def safe_path(rel):
    p = (cfg.ws / rel).resolve()
    if p != cfg.ws and cfg.ws not in p.parents:
        raise ValueError("path escapes workspace")
    if cfg.meta in p.parents or p == cfg.meta:
        raise ValueError(".agent is off limits")
    return p


def tree(sub=""):
    root = safe_path(sub or ".")
    skip = {".git", ".agent", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(cfg.ws)
        if skip & set(rel.parts):
            continue
        if p.is_file():
            out.append(str(rel))
        if len(out) >= 300:
            out.append("... (truncated)")
            break
    return "\n".join(out) or "(empty)"


def sh(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, shell=True, cwd=cfg.ws, capture_output=True, text=True, timeout=timeout, env=_tool_env())
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
