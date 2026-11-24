"""Mistral chat-completions client with retry/backoff and cost accounting."""
import time

import requests

from .config import cfg, log
from .runtime import check_limits, record_usage

API_URL = "https://api.mistral.ai/v1/chat/completions"


def flatten(content):
    if isinstance(content, list):  # reasoning models can return typed chunks
        return "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return content or ""


def chat(messages, tools=None):
    body = {"model": cfg.model, "messages": messages, "temperature": 0.3, "max_tokens": 8192}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    for attempt in range(10):
        check_limits()
        try:
            r = requests.post(API_URL, headers={"Authorization": f"Bearer {cfg.api_key}"}, json=body, timeout=600)
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
