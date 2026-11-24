"""agentd: long-running autonomous coding agent for the Mistral API, controlled over Telegram.

Dependency order (no cycles):
    config -> runtime -> llm / tools -> loop -> tasks -> tg -> main
"""
