"""Check whether the configured Bedrock Mantle model supports native tool calling.

Run from the backend directory (reads BEDROCK_API_KEY and model settings from .env):

    python -m app.llm.probe                 # local
    docker compose exec -T backend python -m app.llm.probe

Exit code 0 = native tool calling works; set LEAVE_AGENT_NATIVE_TOOLS=true to use it.
"""

from __future__ import annotations

import json
import sys
import time

from app.core.config import Settings
from app.infrastructure.llm.mantle import MantleLLMGateway

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_leave_balance",
            "description": "Get the authenticated employee's leave balance.",
            "parameters": {
                "type": "object",
                "properties": {"leave_type": {"type": "string", "enum": ["CASUAL", "SICK", "EARNED"]}},
                "additionalProperties": False,
            },
        },
    }
]
SYSTEM = "You are an HR leave assistant. Use tools for every fact. Never guess balances."


def main() -> int:
    settings = Settings()
    if not settings.bedrock_api_key:
        print("FAIL  BEDROCK_API_KEY is not set (looked in the environment and .env).")
        return 2
    gateway = MantleLLMGateway(settings)
    print(f"Model: {settings.standard_model_id} at {settings.bedrock_openai_base_url}")

    started = time.monotonic()
    try:
        text = gateway.complete("standard", system="Reply with the single word: ready", user="ping")
        print(f"PASS  plain completion ({time.monotonic() - started:.1f}s): {text[:60]!r}")
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print(f"FAIL  plain completion: {exc}")
        return 2

    user = {"role": "user", "content": "How many casual leave days do I have?"}
    started = time.monotonic()
    try:
        first = gateway.complete_with_tools("standard", system=SYSTEM, messages=[user], tools=TOOLS)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  tool-calling request rejected: {exc}")
        print("      Keep LEAVE_AGENT_NATIVE_TOOLS=false (JSON protocol).")
        return 1
    if not first.tool_calls:
        print(f"FAIL  model answered without calling the tool: {first.content!r}")
        print("      Keep LEAVE_AGENT_NATIVE_TOOLS=false (JSON protocol).")
        return 1
    call = first.tool_calls[0]
    print(f"PASS  tool call ({time.monotonic() - started:.1f}s): {call.name}({json.dumps(call.arguments)})")

    messages = [
        user,
        {
            "role": "assistant",
            "content": first.content or "",
            "tool_calls": [{
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }],
        },
        {
            "role": "tool",
            "tool_call_id": call.id,
            "content": json.dumps({"balances": [{"leave_type": "CASUAL", "available_days": "4"}]}),
        },
    ]
    started = time.monotonic()
    try:
        second = gateway.complete_with_tools("standard", system=SYSTEM, messages=messages, tools=TOOLS)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  follow-up after tool result rejected: {exc}")
        return 1
    if not second.content or "4" not in second.content:
        print(f"FAIL  follow-up did not use the tool result: content={second.content!r} calls={second.tool_calls}")
        return 1
    print(f"PASS  answer from tool result ({time.monotonic() - started:.1f}s): {second.content[:120]!r}")
    print("\nNative tool calling works. Set LEAVE_AGENT_NATIVE_TOOLS=true in .env and restart the backend.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
