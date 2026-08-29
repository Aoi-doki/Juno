"""Where thinking happens, behind one interface.

Three backends, one protocol:

``OpenAICompatEngine``  Gemini, Ollama, Cerebras, OpenRouter — anything speaking
                        OpenAI's ``/chat/completions``. One adapter covers all
                        of them because they all copied the same shape.
``AnthropicEngine``     Claude, for when quality matters more than the bill.

Messages travel in a **neutral format** modelled on OpenAI's, since that is what
most backends want natively; the Anthropic engine converts on the way in and
out. Keeping the neutral format at the boundary means the agent loop never
learns which backend it is talking to, and adding a fourth is one class.

The engines are addressed by *role* rather than by name — see
``Config.models.roles``. That is what lets the sensitive path (check-ins, which
carry your screen contents) run locally while ordinary conversation goes to a
faster hosted model.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 180.0


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class EngineReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class EngineError(Exception):
    """A backend failed in a way the caller should surface, not retry blindly."""


class Engine(Protocol):
    name: str
    model: str

    @property
    def available(self) -> bool: ...

    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> EngineReply: ...


# --- OpenAI-compatible -------------------------------------------------------


def _tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic-style tool definitions into OpenAI's function shape.

    Tools are declared once in Anthropic's format because that is what the
    ``@tool`` decorator produces; this is the only place that knows the
    difference.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


class OpenAICompatEngine:
    """Any backend speaking OpenAI's chat-completions API.

    Verified shapes:
      Gemini  ``https://generativelanguage.googleapis.com/v1beta/openai/``
      Ollama  ``http://127.0.0.1:11434/v1``
    """

    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str = "",
        max_tokens: int = 1024,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens

    @property
    def available(self) -> bool:
        # Ollama needs no key, so a blank one is not proof of misconfiguration —
        # only a missing base URL is.
        return bool(self.base_url and self.model)

    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> EngineReply:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if tools:
            payload["tools"] = _tools_to_openai(tools)

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as http:
                response = await http.post(
                    f"{self.base_url}/chat/completions", json=payload, headers=headers
                )
                if response.status_code >= 400:
                    raise EngineError(
                        f"{self.name} returned {response.status_code}: {response.text[:300]}"
                    )
                data = response.json()
        except httpx.HTTPError as exc:
            raise EngineError(f"{self.name} unreachable: {exc}") from exc

        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise EngineError(f"{self.name} returned no choices: {data}") from exc

        calls: list[ToolCall] = []
        for raw in choice.get("tool_calls") or []:
            function = raw.get("function") or {}
            # Arguments arrive as a JSON *string*. A model that emits malformed
            # JSON should lose that one tool call, not the whole turn.
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                log.warning("%s emitted unparseable arguments for %s",
                            self.name, function.get("name"))
                continue
            if not isinstance(arguments, dict):
                continue
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or function.get("name", "call")),
                    name=str(function.get("name", "")),
                    arguments=arguments,
                )
            )

        usage = data.get("usage") or {}
        return EngineReply(
            text=(choice.get("content") or "").strip(),
            tool_calls=calls,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            model=self.model,
        )


# --- Anthropic ---------------------------------------------------------------


def _messages_to_anthropic(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Neutral (OpenAI-shaped) messages into Anthropic's content blocks.

    The fiddly part is tool results: OpenAI sends one ``tool`` message per
    result, Anthropic wants them batched into a single ``user`` message.
    Splitting them across messages is accepted by the API but teaches the model
    to stop calling tools in parallel.
    """
    out: list[dict[str, Any]] = []
    pending_results: list[dict[str, Any]] = []

    def flush() -> None:
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for message in messages:
        role = message.get("role")

        if role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id", ""),
                    "content": str(message.get("content", "")),
                }
            )
            continue

        flush()

        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            for call in message["tool_calls"]:
                function = call.get("function", {})
                try:
                    arguments = json.loads(function.get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", ""),
                        "name": function.get("name", ""),
                        "input": arguments,
                    }
                )
            out.append({"role": "assistant", "content": blocks})
            continue

        out.append({"role": role, "content": message.get("content", "")})

    flush()
    return out


class AnthropicEngine:
    def __init__(self, name: str, model: str, api_key: str, max_tokens: int = 1024) -> None:
        self.name = name
        self.model = model
        self.max_tokens = max_tokens
        self._api_key = api_key
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _get_client(self):
        if self._client is None:
            from anthropic import AsyncAnthropic  # imported late: optional dependency

            self._client = AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> EngineReply:
        if not self.available:
            raise EngineError(f"{self.name} has no API key")
        try:
            response = await self._get_client().messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=tools,
                messages=_messages_to_anthropic(messages),
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises a family of errors
            raise EngineError(f"{self.name} failed: {exc}") from exc

        text = "".join(b.text for b in response.content if b.type == "text").strip()
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in response.content
            if b.type == "tool_use"
        ]
        return EngineReply(
            text=text,
            tool_calls=calls,
            input_tokens=getattr(response.usage, "input_tokens", 0),
            output_tokens=getattr(response.usage, "output_tokens", 0),
            model=self.model,
        )


# --- construction ------------------------------------------------------------


def build_engine(name: str, spec: Any, max_tokens: int = 1024) -> Engine:
    """One configured engine from its config entry."""
    if spec.kind == "anthropic":
        return AnthropicEngine(name, spec.model, spec.api_key, max_tokens)
    if spec.kind == "openai":
        return OpenAICompatEngine(name, spec.base_url, spec.model, spec.api_key, max_tokens)
    raise ValueError(f"unknown engine kind {spec.kind!r} for {name!r}")
