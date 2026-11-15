"""
The bounded tool loop — the only place an LLM's tool request becomes a real call.

Each hop the model may ask for tools; we run them through `foodo_tools.execute`
(argument validation, allow-list, disk cache, never raises), append the JSON as a
`tool` message and ask again. On the final hop the tool surface is withdrawn, so the
model must answer in words: a loop that cannot end is a demo that cannot end.

`on_tool` fires per executed call so the caller can put it on the wire — the room
shows which database was read and how long it took, which is the honest way to prove
the agents are not making it up.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Callable, Dict, List, Optional

from . import foodo_tools
from . import llm as agent_llm

TOOL_RESULT_CHARS = 4000


def enabled() -> bool:
    """Tools are on unless switched off, and only when a provider can call them."""
    flag = (os.environ.get("LLM_TOOLS", "auto") or "auto").strip().lower()
    if flag in ("0", "off", "false", "no"):
        return False
    return agent_llm.has_provider()


def _parse_args(raw: Any) -> Dict[str, Any]:
    """The model's arguments arrive as a JSON string; a broken one is an empty dict."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_message(result: Dict[str, Any]) -> str:
    payload = result.get("data") if result.get("ok") else {"ok": False, "error": result.get("error")}
    return json.dumps(payload, ensure_ascii=False)[:TOOL_RESULT_CHARS]


async def run(
    messages: List[Dict],
    max_tokens: Optional[int] = None,
    temperature: float = 0.6,
    max_hops: Optional[int] = None,
    on_tool: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Return {"text", "calls", "source", "hops"} — never raises for a tool problem.

    `source` is "live" when a model answered, "unavailable" when no provider or the
    call failed, so a caller can fall back to template voice (and say so).
    """
    hops = max_hops if max_hops is not None else int(os.environ.get("LLM_TOOL_HOPS", "2"))
    hops = max(1, hops)
    convo = list(messages)
    calls: List[Dict[str, Any]] = []

    for hop in range(hops + 1):
        allow = hop < hops
        try:
            msg = await agent_llm.complete_with_tools(
                convo,
                tools=foodo_tools.TOOL_SPECS if allow else None,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except agent_llm.LLMError as exc:
            return {"text": "", "calls": calls, "source": "unavailable",
                    "error": exc.as_dict(), "hops": hop}

        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return {"text": msg.get("content") or "", "calls": calls,
                    "source": "live", "hops": hop}

        convo.append({"role": "assistant", "content": msg.get("content") or "",
                      "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            result = await asyncio.to_thread(foodo_tools.execute, name, _parse_args(fn.get("arguments")))
            calls.append(result)
            if on_tool is not None:
                try:
                    on_tool(result)
                except Exception:                      # a bad listener must not kill the turn
                    pass
            convo.append({"role": "tool",
                          "tool_call_id": call.get("id") or name,
                          "content": _tool_message(result)})

    return {"text": "", "calls": calls, "source": "live", "hops": hops}
