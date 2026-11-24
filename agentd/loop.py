"""Episode loop: context trimming, owner-message injection, tool-call turns."""
import json

from .config import cfg, log
from .llm import chat
from .runtime import INBOX, LOCK
from .tools import run_tool


def est_tokens(messages):
    return sum(len(str(m.get("content") or "")) + len(json.dumps(m.get("tool_calls") or "")) for m in messages) // 4


def compact(messages, keep_last=12):
    """Shrink old tool outputs and old file writes; keeps message pairing intact."""
    if est_tokens(messages) < cfg.context_tokens:
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


def agent_loop(system, user, tools, terminal):
    """Runs until `terminal` tool is called. Returns its args dict, or None on failure."""
    with LOCK:
        INBOX.clear()  # the episode prompt already contains current directives
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for turn in range(cfg.max_turns):
        compact(messages)
        if est_tokens(messages) > cfg.context_tokens * 1.5:
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
