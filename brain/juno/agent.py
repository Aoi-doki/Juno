"""The reasoning loop: assemble context, call an engine, run tools, repeat.

Which engine depends on the *role* of the turn, not on how hard it looks.
Conversation and check-ins have genuinely different requirements — one is
latency-sensitive and low-volume, the other is high-volume and carries your
screen contents — so they are routed separately and can run on different
backends entirely. See ``config.ModelConfig``.

Every engine is addressed through the same neutral message format, so nothing
below knows or cares whether it is talking to Gemini, Ollama or Claude.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from juno import tools
from juno.config import Config
from juno.engines import Engine, EngineError, EngineReply, build_engine
from juno.memory import Memory

log = logging.getLogger(__name__)

# USD per million tokens (input, output), for the spend figure on /health.
# Only meaningful for paid engines; free ones record nothing.
PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
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
        self._engines: dict[str, Engine] = {}

        for name, spec in config.models.engines.items():
            try:
                self._engines[name] = build_engine(name, spec, config.models.max_tokens)
            except ValueError as exc:
                log.error("skipping engine %s: %s", name, exc)

        live = [n for n, e in self._engines.items() if e.available]
        log.info("engines available: %s", ", ".join(live) or "none")

    # --- engine selection ----------------------------------------------------

    def engine_for(self, role: str) -> Engine | None:
        """The engine for a role, falling back when it isn't usable.

        An engine with no API key is *unavailable* rather than an error, so a
        config listing Claude without a key quietly routes elsewhere instead of
        failing at the worst moment.
        """
        wanted = getattr(self.config.models, role, None) or self.config.models.conversation
        engine = self._engines.get(wanted)
        if engine is not None and engine.available:
            return engine

        if engine is not None:
            log.warning("engine %r for role %r is not usable; falling back", wanted, role)
        fallback = self._engines.get(self.config.models.fallback)
        if fallback is not None and fallback.available:
            return fallback
        return next((e for e in self._engines.values() if e.available), None)

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

    def _record_spend(self, reply: EngineReply) -> None:
        rate = next((v for k, v in PRICES.items() if reply.model.startswith(k)), None)
        if rate is None:
            return  # a free engine; nothing to account for
        cost = (reply.input_tokens * rate[0] + reply.output_tokens * rate[1]) / 1_000_000
        self.memory.record_spend(reply.model, cost)

    # --- the loop ------------------------------------------------------------

    async def respond(
        self, user_text: str, *, history: int = 16, role: str = "conversation"
    ) -> Reply:
        """One full turn, including any tool use, returning what to say."""
        engine = self.engine_for(role)
        if engine is None:
            return Reply("I've got no thinking engine configured right now.", "none", [])

        messages: list[dict[str, Any]] = []
        for row in self.memory.recent_turns(history):
            messages.append(
                {"role": "user" if row["role"] == "user" else "assistant", "content": row["text"]}
            )
        messages.append({"role": "user", "content": user_text})

        system = self.system_prompt()
        definitions = tools.definitions()
        used: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            try:
                reply = await engine.complete(system, messages, definitions)
            except EngineError as exc:
                log.error("%s", exc)
                fallback = self._engines.get(self.config.models.fallback)
                if fallback is not None and fallback is not engine and fallback.available:
                    log.info("retrying on %s", fallback.name)
                    engine = fallback
                    continue
                return Reply("I couldn't reach my thinking engine.", engine.model, used)

            self._record_spend(reply)

            if not reply.wants_tools:
                return Reply(reply.text, reply.model, used)

            messages.append(
                {
                    "role": "assistant",
                    "content": reply.text,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments),
                            },
                        }
                        for call in reply.tool_calls
                    ],
                }
            )
            for call in reply.tool_calls:
                used.append(call.name)
                log.info("tool %s(%s)", call.name, call.arguments)
                output = await tools.dispatch(call.name, call.arguments, self.ctx)
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": output}
                )

        # Ran out of rounds: better to say something honest than loop forever.
        return Reply("I got stuck working that out. Ask me again?", engine.model, used)

    def spend_30d(self) -> float:
        return self.memory.spend_since(time.time() - 30 * 86400)
