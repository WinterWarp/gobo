"""OpenRouter inference via the OpenAI SDK. `thinking_level` maps to OpenRouter's
provider-agnostic `reasoning` parameter."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from openai import AsyncOpenAI

from .config import LLMConfig

log = logging.getLogger(__name__)

ToolHandler = Callable[[dict], Awaitable[str]]


@dataclass
class Toolbox:
    specs: list[dict] = field(default_factory=list)
    handlers: dict[str, ToolHandler] = field(default_factory=dict)

    def add(self, name: str, description: str, parameters: dict, handler: ToolHandler) -> None:
        self.specs.append(
            {
                "type": "function",
                "function": {"name": name, "description": description, "parameters": parameters},
            }
        )
        self.handlers[name] = handler


class LLM:
    def __init__(self, api_key: str, base_url: str = "https://openrouter.ai/api/v1"):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers={"X-Title": "gobo"},
        )

    def _extra_body(self, cfg: LLMConfig) -> dict:
        if cfg.thinking_level == "none":
            return {}
        return {"reasoning": {"effort": cfg.thinking_level}}

    async def text(self, cfg: LLMConfig, system: str, messages: list[dict]) -> str:
        """One-shot completion, no tools."""
        resp = await self.client.chat.completions.create(
            model=cfg.model,
            messages=[{"role": "system", "content": system}, *messages],
            extra_body=self._extra_body(cfg),
        )
        return (resp.choices[0].message.content or "").strip()

    async def tool_loop(
        self,
        cfg: LLMConfig,
        system: str,
        messages: list[dict],
        toolbox: Toolbox,
        max_rounds: int = 8,
    ) -> str:
        """Run a tool-calling loop until the model produces plain text."""
        convo: list[dict] = [{"role": "system", "content": system}, *messages]
        for _ in range(max_rounds):
            resp = await self.client.chat.completions.create(
                model=cfg.model,
                messages=convo,
                tools=toolbox.specs,
                extra_body=self._extra_body(cfg),
            )
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return (msg.content or "").strip()
            convo.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                }
            )
            for tc in msg.tool_calls:
                name = tc.function.name
                handler = toolbox.handlers.get(name)
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if handler is None:
                    result = f"error: unknown tool {name}"
                else:
                    try:
                        result = await handler(args)
                    except Exception as e:
                        log.exception("tool %s failed", name)
                        result = f"error: {e}"
                convo.append({"role": "tool", "tool_call_id": tc.id, "content": result})
        return "(I hit my tool-call budget — try again.)"
