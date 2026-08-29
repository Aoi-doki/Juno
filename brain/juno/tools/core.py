"""The tools available in Phase 0-1: memory, the activity timeline, and
speaking to a specific device.

Later phases add their own modules (desktop control, smart home) and import
cleanly on top — nothing here needs to change to accommodate them.
"""

from __future__ import annotations

import time

from juno.protocol import speak as speak_frame
from juno.tools import Context, tool


@tool(
    "remember",
    "Store a durable fact about the user — a preference, an ongoing project, a "
    "person, a routine. Use this whenever they tell you something that will "
    "still be true tomorrow. Restating an existing subject updates it.",
    {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Short identifying phrase, e.g. 'sleep target' or 'sister'. "
                "Reusing a subject overwrites the old value.",
            },
            "body": {"type": "string", "description": "The fact itself, in one or two sentences."},
        },
        "required": ["subject", "body"],
    },
)
def remember(ctx: Context, subject: str, body: str) -> str:
    ctx.memory.remember(subject, body, source="conversation")
    return f"remembered: {subject}"


@tool(
    "recall",
    "Search stored facts about the user. Call this before claiming you do not "
    "know something about them.",
    {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Keywords to search for."}},
        "required": ["query"],
    },
)
def recall(ctx: Context, query: str) -> str:
    rows = ctx.memory.search_facts(query)
    if not rows:
        return "no matching facts"
    return "\n".join(f"- {r['subject']}: {r['body']}" for r in rows)


@tool(
    "forget",
    "Delete a stored fact by its exact subject. Only use when the user asks you to.",
    {
        "type": "object",
        "properties": {"subject": {"type": "string"}},
        "required": ["subject"],
    },
)
def forget(ctx: Context, subject: str) -> str:
    return f"forgotten: {subject}" if ctx.memory.forget(subject) else f"no fact named {subject!r}"


@tool(
    "activity",
    "What the user has actually been doing, from screen, phone and presence "
    "events. Use this for any question about their recent time, and before "
    "commenting on how they are spending it — never guess.",
    {
        "type": "object",
        "properties": {
            "minutes": {
                "type": "integer",
                "description": "How far back to look. Default 60.",
                "minimum": 1,
                "maximum": 1440,
            }
        },
    },
)
def activity(ctx: Context, minutes: int = 60) -> str:
    return ctx.memory.digest(since=time.time() - minutes * 60)


@tool(
    "devices",
    "List the devices currently connected and what each can do. Use this to "
    "check whether the user is reachable before trying to say something to them.",
    {"type": "object", "properties": {}},
)
def devices(ctx: Context) -> str:
    live = ctx.devices.all()
    if not live:
        return "no devices connected"
    return "\n".join(
        f"- {d.device_id} ({d.kind}): {', '.join(sorted(d.capabilities)) or 'no capabilities'}"
        f"{'  [nearest]' if d.device_id == ctx.devices.nearest_id() else ''}"
        for d in live
    )


@tool(
    "say_on",
    "Speak on a specific device rather than replying in the current "
    "conversation. Use this to reach the user somewhere else — for example "
    "speaking on their phone when they have walked away from the laptop.",
    {
        "type": "object",
        "properties": {
            "device_id": {"type": "string", "description": "From the `devices` tool."},
            "text": {"type": "string"},
            "urgency": {
                "type": "string",
                "enum": ["normal", "alarm"],
                "description": "'alarm' takes over the screen and overrides Do Not Disturb. "
                "Reserve it for genuine problems — using it for ordinary nagging "
                "trains the user to ignore it.",
            },
        },
        "required": ["device_id", "text"],
    },
)
async def say_on(ctx: Context, device_id: str, text: str, urgency: str = "normal") -> str:
    device = ctx.devices.get(device_id)
    if device is None:
        return f"error: {device_id} is not connected"
    if "speak" not in device.capabilities:
        return f"error: {device_id} cannot speak"
    await device.send(speak_frame(text, urgency=urgency))
    ctx.memory.add_turn("juno", text, device_id=device_id)
    return f"spoken on {device_id}"
