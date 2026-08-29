"""Tools added by Phases 2, 4 and 6: acting on devices, the schedule, the plan,
and the house.

Registered by importing this module; see ``juno.tools.__init__``.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from juno.protocol import Capability, command
from juno.tools import Context, tool


@tool(
    "set_plan",
    "Record what the user says they are working on now, and for how long. Call "
    "this whenever they state an intention — it is what lets you notice later "
    "that they have drifted off it.",
    {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "What they said they'd be doing."},
            "minutes": {
                "type": "integer",
                "description": "How long they said, if they said. Omit if they didn't.",
            },
        },
        "required": ["task"],
    },
)
def set_plan(ctx: Context, task: str, minutes: int | None = None) -> str:
    body = task if minutes is None else f"{task} (for about {minutes} minutes)"
    started = datetime.now(timezone.utc).astimezone().strftime("%H:%M")
    ctx.memory.remember("current task", f"{body}; started {started}", source="stated")
    return f"noted: {body}"


@tool(
    "clear_plan",
    "Forget what they said they were working on — they finished it, or changed "
    "their mind. Stops you nagging them about a task that is over.",
    {"type": "object", "properties": {}},
)
def clear_plan(ctx: Context) -> str:
    return "cleared" if ctx.memory.forget("current task") else "there was no stated plan"


@tool(
    "snooze",
    "Accept a 'not now'. Use this when they push back on something you raised, "
    "rather than simply dropping it — it will come back, and repeated snoozes "
    "on the same subject come back sooner.",
    {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "What to hold off on. Use the same wording you raised it with, "
                "or 'check-in' for a general 'leave me alone'.",
            }
        },
        "required": ["subject"],
    },
)
def snooze(ctx: Context, subject: str) -> str:
    scheduler = getattr(ctx, "scheduler", None)
    if scheduler is None:
        return "nothing scheduled to snooze"
    granted = scheduler.gate.snooze(subject, time.monotonic())
    return f"holding off on {subject} for {int(granted / 60)} minutes"


@tool(
    "schedule",
    "What is coming up on their calendar. Use this before commenting on how "
    "they are spending time — a long stretch in one app matters differently "
    "when nothing is booked.",
    {
        "type": "object",
        "properties": {
            "hours": {"type": "integer", "description": "How far ahead. Default 12.", "minimum": 1}
        },
    },
)
async def schedule(ctx: Context, hours: int = 12) -> str:
    calendar = getattr(ctx, "calendar", None)
    if calendar is None or not calendar.configured:
        return "no calendar is configured"
    now = datetime.now(timezone.utc).astimezone()
    events = await calendar.upcoming(within_hours=hours, now=now)
    if not events:
        return f"nothing in the next {hours} hours"
    return "\n".join(f"- {e.describe(now)}" for e in events)


@tool(
    "notify",
    "Put a notification on a device's screen. Quieter than speaking — use it "
    "when what you have to say is useful but not worth interrupting for.",
    {
        "type": "object",
        "properties": {
            "device_id": {"type": "string"},
            "summary": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["device_id", "summary"],
    },
)
async def notify(ctx: Context, device_id: str, summary: str, body: str = "") -> str:
    device = ctx.devices.get(device_id)
    if device is None:
        return f"error: {device_id} is not connected"
    if "control" not in device.capabilities:
        return f"error: {device_id} cannot show notifications"
    await device.send(
        command(Capability.CONTROL, action="notify", summary=summary, body=body)
    )
    return f"notified on {device_id}"


@tool(
    "launch_app",
    "Open an application on one of their devices.",
    {
        "type": "object",
        "properties": {
            "device_id": {"type": "string"},
            "application": {
                "type": "string",
                "description": "Desktop entry name or executable, e.g. 'firefox'.",
            },
        },
        "required": ["device_id", "application"],
    },
)
async def launch_app(ctx: Context, device_id: str, application: str) -> str:
    device = ctx.devices.get(device_id)
    if device is None:
        return f"error: {device_id} is not connected"
    if "control" not in device.capabilities:
        return f"error: {device_id} cannot launch applications"
    await device.send(
        command(Capability.CONTROL, action="launch", application=application)
    )
    return f"launching {application} on {device_id}"


@tool(
    "list_devices_home",
    "List the smart home devices and their current states.",
    {
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "description": "Optional substring to match against entity id or name.",
            }
        },
    },
)
async def list_devices_home(ctx: Context, filter: str = "") -> str:  # noqa: A002
    ha = getattr(ctx, "home", None)
    if ha is None or not ha.configured:
        return "Home Assistant is not configured"
    try:
        entities = await ha.states()
    except Exception as exc:  # noqa: BLE001
        return f"could not reach Home Assistant: {exc}"

    needle = filter.lower()
    if needle:
        entities = [e for e in entities if needle in e.entity_id.lower() or needle in e.name.lower()]
    if not entities:
        return "no matching devices"
    # Cap the listing: a full HA install has hundreds of entities and sending
    # all of them would cost more than the answer is worth.
    shown = entities[:60]
    lines = [e.describe() for e in shown]
    if len(entities) > len(shown):
        lines.append(f"… and {len(entities) - len(shown)} more; narrow it with a filter")
    return "\n".join(lines)


@tool(
    "control_home",
    "Turn a smart home device on or off, or call another Home Assistant "
    "service on it. Check `list_devices_home` first for the exact entity id.",
    {
        "type": "object",
        "properties": {
            "entity_id": {"type": "string", "description": "e.g. light.kitchen"},
            "service": {
                "type": "string",
                "description": "turn_on, turn_off, toggle, or another service in the "
                "entity's domain.",
            },
            "confirmed": {
                "type": "boolean",
                "description": "Required for locks, covers, alarms and vacuums. Set only "
                "after the user has clearly asked for that specific action.",
            },
        },
        "required": ["entity_id", "service"],
    },
)
async def control_home(
    ctx: Context, entity_id: str, service: str, confirmed: bool = False
) -> str:
    ha = getattr(ctx, "home", None)
    if ha is None or not ha.configured:
        return "Home Assistant is not configured"
    if ha.is_forbidden(entity_id):
        return f"{entity_id} is on the forbidden list and cannot be controlled"
    if ha.is_guarded(entity_id) and not confirmed:
        return (
            f"{entity_id} is a lock, cover, alarm or vacuum — ask them to confirm in "
            "plain words first, then call this again with confirmed=true"
        )

    domain = entity_id.split(".", 1)[0]
    try:
        return await ha.call(domain, service, entity_id)
    except Exception as exc:  # noqa: BLE001
        return f"could not control {entity_id}: {exc}"
