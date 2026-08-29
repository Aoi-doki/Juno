"""The reasoning loop: assemble context, call Claude, run tools, repeat.

Two things here exist purely to keep the bill in single digits:

* **Tiering** — the routine model handles everything unless a turn looks hard,
  because most turns are "should I say something right now?" answered with
  *no*.
* **A budget ceiling** — when the month's spend crosses the configured cap the
  brain switches to a local Ollama model rather than quietly spending more.
  Degraded, but never a surprise invoice.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from anthropic import AsyncAnthropic

from juno import tools
from juno.config import Config
from juno.memory import Memory

log = logging.getLogger(__name__)

# USD per million tokens (input, output). Approximate and only used for the
# self-imposed budget cap, so drift here costs accuracy in a warning, not money.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (15.00, 75.00),
}

MAX_TOOL_ROUNDS = 6

PERSONA = """\
You are Juno — named for Juno Moneta, "Juno who warns", the aspect of the \
goddess who warned Rome of what was coming. That is your job: you watch what \
{user} is actually doing and you tell them the truth about it.

You are speaking aloud. Write for the ear, not the page: short sentences, no \
markdown, no lists, no headings, no emoji. Contractions. If a reply would run \
past about three sentences, cut it down — {user} can always ask for more.

Your character:
- Warm but not soft. You are on their side, which is exactly why you do not \
flatter them.
- Specific. "You've been on Reddit for forty minutes" beats "you seem \
distracted". Use the `activity` tool and quote real numbers rather than \
guessing at what they've been doing.
- Unbothered. If they push back or ignore you, you don't sulk or over-apologise. \
You note it and move on.
- Brief when nothing is wrong. Not every check-in needs a comment.

What you must not do:
- Never invent what they were doing. If the timeline is empty, say so.
- Never nag about the same thing twice in a row without new information.
- Never use the alarm urgency for ordinary distraction. It is for genuine \
problems only — a missed commitment, the middle of the night. Spending it \
cheaply is how you get uninstalled.
"""

PROACTIVITY_NOTE = """\
Your proactivity is set to {level} out of 10, where 0 speaks only when spoken \
to and 10 interrupts constantly. {guidance}
"""

_LEVEL_GUIDANCE = {
    range(0, 3): "Stay quiet. Only speak for things that are genuinely urgent.",
    range(3, 6): "Speak up for schedule items and real problems. Otherwise leave them alone.",
    range(6, 9): (
        "Hold them to what they said they'd do. Call out drift once you're confident it's "
        "drift and not a short break, and be direct about it."
    ),
    range(9, 11): (
        "Push hard. Challenge any gap between what they said they'd do and what they're "
        "doing. Still never rude for its own sake, and still silent when they're on track."
    ),
}


def _guidance_for(level: int) -> str:
    for span, text in _LEVEL_GUIDANCE.items():
        if level in span:
            return text
    return _LEVEL_GUIDANCE[range(6, 9)]


@dataclass(slots=True)
class Reply:
    text: str
    model: str
    tool_calls: list[str]


class Agent:
    def __init__(self, config: Config, memory: Memory, devices: Any) -> None:
        self.config = config
        self.memory = memory
        self.ctx = tools.Context(memory=memory, devices=devices, config=config)
        self._client = AsyncAnthropic(api_key=config.anthropic_key) if config.anthropic_key else None

    # --- context assembly ----------------------------------------------------

    def system_prompt(self) -> str:
        cfg = self.config
        parts = [
            PERSONA.format(user=cfg.user_name),
            PROACTIVITY_NOTE.format(
                level=cfg.proactivity.level, guidance=_guidance_for(cfg.proactivity.level)
            ),
        ]

        quiet_from, quiet_to = cfg.proactivity.quiet_hours
        parts.append(
            f"Quiet hours are {quiet_from:02d}:00 to {quiet_to:02d}:00. During them you do not "
            "start conversations at all, though you still answer if spoken to."
        )

        facts = self.memory.all_facts(limit=40)
        if facts:
            joined = "\n".join(f"- {f['subject']}: {f['body']}" for f in facts)
            parts.append(f"What you know about {cfg.user_name}:\n{joined}")

        now = datetime.now(timezone.utc).astimezone()
        parts.append(f"It is currently {now:%A %d %B, %H:%M}.")
        return "\n\n".join(parts)

    def _choose_model(self, messages: list[dict[str, Any]]) -> str:
        """Routine model unless the turn looks expensive to think about."""
        size = sum(len(json.dumps(m)) for m in messages)
        if size > self.config.models.escalate_over_chars:
            return self.config.models.escalation
        return self.config.models.routine

    def _over_budget(self) -> bool:
        month_start = time.time() - 30 * 86400
        return self.memory.spend_since(month_start) >= self.config.models.monthly_budget_usd

    def _record_spend(self, model: str, usage: Any) -> None:
        rate = next((v for k, v in PRICES.items() if model.startswith(k)), None)
        if rate is None or usage is None:
            return
        cost = (
            getattr(usage, "input_tokens", 0) * rate[0]
            + getattr(usage, "output_tokens", 0) * rate[1]
        ) / 1_000_000
        self.memory.record_spend(model, cost)

    # --- the loop ------------------------------------------------------------

    async def respond(self, user_text: str, *, history: int = 16) -> Reply:
        """One full turn, including any tool use, returning what to say."""
        if self._client is None:
            return Reply("My API key isn't set, so I can't think right now.", "none", [])

        if self._over_budget():
            log.warning("monthly budget reached; falling back to local model")
            text = await self._local_complete(user_text)
            return Reply(text, self.config.models.local_fallback_model, [])

        messages: list[dict[str, Any]] = []
        for row in self.memory.recent_turns(history):
            messages.append(
                {"role": "user" if row["role"] == "user" else "assistant", "content": row["text"]}
            )
        messages.append({"role": "user", "content": user_text})

        model = self._choose_model(messages)
        used: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            response = await self._client.messages.create(
                model=model,
                max_tokens=self.config.models.max_tokens,
                system=self.system_prompt(),
                tools=tools.definitions(),
                messages=messages,
            )
            self._record_spend(model, response.usage)

            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text").strip()
                return Reply(text, model, used)

            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                used.append(block.name)
                log.info("tool %s(%s)", block.name, block.input)
                output = await tools.dispatch(block.name, dict(block.input), self.ctx)
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
            messages.append({"role": "user", "content": results})

        # Ran out of rounds: better to say something honest than loop forever.
        return Reply("I got stuck working that out. Ask me again?", model, used)

    async def _local_complete(self, user_text: str) -> str:
        """Ollama fallback. No tools — it exists to stay useful when the budget
        is spent, not to be as capable."""
        url = f"{self.config.models.local_fallback_url}/api/chat"
        payload = {
            "model": self.config.models.local_fallback_model,
            "stream": False,
            "messages": [
                {"role": "system", "content": self.system_prompt()},
                {"role": "user", "content": user_text},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=120) as http:
                resp = await http.post(url, json=payload)
                resp.raise_for_status()
                return resp.json()["message"]["content"].strip()
        except Exception as exc:  # noqa: BLE001
            log.error("local fallback failed: %s", exc)
            return "I've hit my budget for the month and can't reach the local model either."
