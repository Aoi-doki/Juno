"""Tool registry.

A tool is a plain Python function plus a JSON schema. ``@tool`` registers both,
so adding a capability means writing one function — the Anthropic tool
definitions and the dispatch table are derived, never hand-maintained in
parallel.

Handlers receive a ``Context`` carrying whatever they need to touch: memory, the
live device registry, config. Keeping that in one object means a new tool never
has to change the agent loop's signature.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for type checkers
    from juno.config import Config
    from juno.memory import Memory
    from juno.orchestrator import DeviceRegistry


@dataclass(slots=True)
class Context:
    memory: "Memory"
    devices: "DeviceRegistry"
    config: "Config"


Handler = Callable[..., Any | Awaitable[Any]]

_REGISTRY: dict[str, tuple[Handler, dict[str, Any]]] = {}


def tool(name: str, description: str, schema: dict[str, Any]) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        if name in _REGISTRY:
            raise ValueError(f"tool {name!r} is already registered")
        _REGISTRY[name] = (
            fn,
            {"name": name, "description": description, "input_schema": schema},
        )
        return fn

    return register


def definitions() -> list[dict[str, Any]]:
    """Tool schemas in the shape the Anthropic API expects."""
    return [spec for _, spec in _REGISTRY.values()]


async def dispatch(name: str, args: dict[str, Any], ctx: Context) -> str:
    """Run a tool and return its result as text for the model.

    Errors are returned rather than raised: a tool that blows up should let the
    model apologise or try something else, not kill the whole turn.
    """
    entry = _REGISTRY.get(name)
    if entry is None:
        return f"error: no such tool {name!r}"
    fn, _ = entry
    try:
        out = fn(ctx=ctx, **args)
        if inspect.isawaitable(out):
            out = await out
        return str(out)
    except TypeError as exc:
        return f"error: bad arguments for {name}: {exc}"
    except Exception as exc:  # noqa: BLE001 - surfaced to the model deliberately
        return f"error: {name} failed: {exc}"


from juno.tools import core as _core  # noqa: E402,F401  (import registers the tools)
