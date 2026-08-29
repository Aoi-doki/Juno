"""Engine adapters: message conversion, tool schemas, failure handling.

The conversions are the risky part. A tool-result batching mistake or a
swallowed exception here shows up as Juno mysteriously going quiet, not as a
stack trace.
"""

from __future__ import annotations

import json

import httpx
import pytest

from juno.config import DEFAULT_ENGINES, EngineSpec, ModelConfig
from juno.engines import (
    AnthropicEngine,
    EngineError,
    OpenAICompatEngine,
    ToolCall,
    _messages_to_anthropic,
    _tools_to_openai,
    build_engine,
)

TOOLS = [
    {
        "name": "activity",
        "description": "What they have been doing.",
        "input_schema": {"type": "object", "properties": {"minutes": {"type": "integer"}}},
    }
]


class TestToolSchemaConversion:
    def test_anthropic_shape_becomes_openai_function(self):
        converted = _tools_to_openai(TOOLS)[0]
        assert converted["type"] == "function"
        assert converted["function"]["name"] == "activity"
        assert converted["function"]["parameters"] == TOOLS[0]["input_schema"]

    def test_empty_list_stays_empty(self):
        assert _tools_to_openai([]) == []


class TestNeutralToAnthropic:
    def test_plain_messages_pass_through(self):
        out = _messages_to_anthropic([{"role": "user", "content": "hello"}])
        assert out == [{"role": "user", "content": "hello"}]

    def test_assistant_tool_calls_become_tool_use_blocks(self):
        out = _messages_to_anthropic(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "activity", "arguments": '{"minutes": 60}'},
                        }
                    ],
                }
            ]
        )
        block = out[0]["content"][0]
        assert block["type"] == "tool_use"
        assert block["name"] == "activity"
        assert block["input"] == {"minutes": 60}

    def test_parallel_tool_results_batch_into_one_user_message(self):
        """Splitting them across messages is accepted but teaches the model to
        stop calling tools in parallel."""
        out = _messages_to_anthropic(
            [
                {"role": "tool", "tool_call_id": "a", "content": "first"},
                {"role": "tool", "tool_call_id": "b", "content": "second"},
            ]
        )
        assert len(out) == 1
        assert out[0]["role"] == "user"
        assert [b["tool_use_id"] for b in out[0]["content"]] == ["a", "b"]

    def test_results_flush_before_the_next_message(self):
        out = _messages_to_anthropic(
            [
                {"role": "tool", "tool_call_id": "a", "content": "first"},
                {"role": "user", "content": "and now this"},
            ]
        )
        assert [m["role"] for m in out] == ["user", "user"]
        assert out[1]["content"] == "and now this"

    def test_assistant_text_is_kept_alongside_tool_calls(self):
        out = _messages_to_anthropic(
            [
                {
                    "role": "assistant",
                    "content": "Let me check.",
                    "tool_calls": [
                        {"id": "1", "function": {"name": "activity", "arguments": "{}"}}
                    ],
                }
            ]
        )
        assert out[0]["content"][0] == {"type": "text", "text": "Let me check."}

    def test_malformed_arguments_do_not_raise(self):
        out = _messages_to_anthropic(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "1", "function": {"name": "activity", "arguments": "not json"}}
                    ],
                }
            ]
        )
        assert out[0]["content"][0]["input"] == {}


class TestOpenAICompatEngine:
    @pytest.fixture(autouse=True)
    def _patch_client(self, monkeypatch):
        self.captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            self.captured["url"] = str(request.url)
            self.captured["headers"] = dict(request.headers)
            self.captured["body"] = json.loads(request.content)
            return self.response

        transport = httpx.MockTransport(handler)
        real = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        self.response = httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "All good.", "tool_calls": None}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    async def test_plain_text_reply(self):
        engine = OpenAICompatEngine("gemini", "http://x/v1", "gemini-2.5-flash", "k")
        reply = await engine.complete("system", [{"role": "user", "content": "hi"}], TOOLS)

        assert reply.text == "All good."
        assert reply.tool_calls == []
        assert reply.input_tokens == 100
        assert reply.output_tokens == 10

    async def test_system_prompt_is_prepended_as_a_message(self):
        engine = OpenAICompatEngine("gemini", "http://x/v1", "m", "k")
        await engine.complete("be brief", [{"role": "user", "content": "hi"}], [])

        assert self.captured["body"]["messages"][0] == {"role": "system", "content": "be brief"}

    async def test_the_api_key_travels_as_a_bearer_token(self):
        engine = OpenAICompatEngine("gemini", "http://x/v1", "m", "secret")
        await engine.complete("s", [], [])
        assert self.captured["headers"]["authorization"] == "Bearer secret"

    async def test_no_key_means_no_auth_header(self):
        """Ollama needs no key, and sending an empty bearer confuses some servers."""
        engine = OpenAICompatEngine("local", "http://x/v1", "qwen3:4b")
        await engine.complete("s", [], [])
        assert "authorization" not in self.captured["headers"]

    async def test_tool_calls_are_parsed(self):
        self.response = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {
                                        "name": "activity",
                                        "arguments": '{"minutes": 90}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )
        engine = OpenAICompatEngine("gemini", "http://x/v1", "m", "k")
        reply = await engine.complete("s", [], TOOLS)

        assert reply.wants_tools
        assert reply.tool_calls[0] == ToolCall("call_1", "activity", {"minutes": 90})
        assert reply.text == ""

    async def test_one_unparseable_tool_call_does_not_lose_the_others(self):
        """A small model that emits bad JSON should cost that call, not the turn."""
        self.response = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {"id": "a", "function": {"name": "activity", "arguments": "{oops"}},
                                {"id": "b", "function": {"name": "devices", "arguments": "{}"}},
                            ],
                        }
                    }
                ]
            },
        )
        engine = OpenAICompatEngine("local", "http://x/v1", "m")
        reply = await engine.complete("s", [], TOOLS)

        assert [c.name for c in reply.tool_calls] == ["devices"]

    async def test_an_http_error_becomes_an_engine_error(self):
        self.response = httpx.Response(429, text="rate limited")
        engine = OpenAICompatEngine("gemini", "http://x/v1", "m", "k")

        with pytest.raises(EngineError) as caught:
            await engine.complete("s", [], [])
        assert "429" in str(caught.value)

    async def test_a_response_with_no_choices_is_an_engine_error(self):
        self.response = httpx.Response(200, json={"choices": []})
        engine = OpenAICompatEngine("gemini", "http://x/v1", "m", "k")

        with pytest.raises(EngineError):
            await engine.complete("s", [], [])


class TestAvailability:
    def test_openai_engine_needs_a_url_and_model_but_not_a_key(self):
        assert OpenAICompatEngine("local", "http://x/v1", "qwen3:4b").available is True
        assert OpenAICompatEngine("broken", "", "qwen3:4b").available is False

    def test_anthropic_engine_needs_a_key(self):
        assert AnthropicEngine("claude", "claude-haiku-4-5", "").available is False
        assert AnthropicEngine("claude", "claude-haiku-4-5", "sk-ant-x").available is True


class TestBuildEngine:
    def test_builds_an_openai_engine(self):
        engine = build_engine("gemini", DEFAULT_ENGINES["gemini"])
        assert isinstance(engine, OpenAICompatEngine)
        assert "generativelanguage.googleapis.com" in engine.base_url

    def test_builds_an_anthropic_engine(self):
        assert isinstance(build_engine("claude", DEFAULT_ENGINES["claude"]), AnthropicEngine)

    def test_an_unknown_kind_is_rejected_loudly(self):
        with pytest.raises(ValueError):
            build_engine("weird", EngineSpec(kind="telepathy", model="x"))


class TestRoleDefaults:
    def test_sensitive_paths_default_to_local(self):
        """Check-ins carry the activity timeline, so the default keeps them home."""
        models = ModelConfig()
        assert models.checkin == "local"
        assert models.fallback == "local"

    def test_conversation_defaults_to_the_fast_hosted_engine(self):
        assert ModelConfig().conversation == "gemini"

    def test_api_keys_come_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "from-env")
        assert DEFAULT_ENGINES["gemini"].api_key == "from-env"

    def test_an_engine_with_no_key_env_has_no_key(self):
        assert DEFAULT_ENGINES["local"].api_key == ""
