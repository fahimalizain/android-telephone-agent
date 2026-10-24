"""Tests for the echo and HTTP conversation brains."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest
from telephone_agent.brain import (
    DEFAULT_SYSTEM_PROMPT,
    BrainError,
    EchoBrain,
    HttpBrain,
    build_brain,
)
from telephone_agent.config import BrainConfig


class FakeResponse:
    def __init__(self, body: bytes = b"{}", status: int = 200) -> None:
        self.body = body
        self.status = status

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None


def _install(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[object],
) -> list[tuple[urllib.request.Request, float | None]]:
    calls: list[tuple[urllib.request.Request, float | None]] = []
    pending = list(responses)

    def fake_urlopen(request: urllib.request.Request, timeout: float | None = None) -> object:
        calls.append((request, timeout))
        if not pending:
            raise AssertionError("urlopen called more times than expected")
        response = pending.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


def _header(request: urllib.request.Request, name: str) -> str | None:
    for key, value in request.headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _completion(content: str) -> FakeResponse:
    return FakeResponse(
        json.dumps({"choices": [{"message": {"role": "assistant", "content": content}}]}).encode()
    )


def test_echo_brain_echoes_short_text() -> None:
    brain = EchoBrain()
    assert brain.reply("  hello there  ") == "I heard: hello there"


def test_echo_brain_handles_empty_text() -> None:
    assert EchoBrain().reply("   ") == "I did not hear anything."


def test_echo_brain_truncates_long_text() -> None:
    reply = EchoBrain().reply("word " * 200)
    assert len(reply) <= len("I heard: ") + 200
    assert reply.endswith("...")


def test_echo_reset_is_a_noop() -> None:
    brain = EchoBrain()
    assert brain.reply("one") == "I heard: one"
    brain.reset()
    assert brain.reply("two") == "I heard: two"


def test_http_brain_posts_chat_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, [_completion("Hello from the model")])
    brain = HttpBrain(
        base_url="https://llm.example.invalid/v1/",
        model="test-model",
        api_key="test-key",
        system_prompt="Be brief.",
        max_tokens=50,
        timeout_s=9.0,
    )

    assert brain.reply("Hello") == "Hello from the model"

    request, timeout = calls[0]
    assert request.full_url == "https://llm.example.invalid/v1/chat/completions"
    assert request.get_method() == "POST"
    assert _header(request, "Authorization") == "Bearer test-key"
    assert _header(request, "Content-Type") == "application/json"
    assert timeout == 9.0
    payload = json.loads(request.data or b"{}")
    assert payload["model"] == "test-model"
    assert payload["max_tokens"] == 50
    assert payload["stream"] is False
    assert payload["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Hello"},
    ]


def test_http_brain_keeps_history_and_reset_clears_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(
        monkeypatch,
        [_completion("first reply"), _completion("second reply"), _completion("third reply")],
    )
    brain = HttpBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    brain.reply("turn one")
    brain.reply("turn two")

    second = json.loads(calls[1][0].data or b"{}")
    assert [message["role"] for message in second["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert second["messages"][1]["content"] == "turn one"
    assert second["messages"][2]["content"] == "first reply"

    brain.reset()
    brain.reply("turn three")

    third = json.loads(calls[2][0].data or b"{}")
    assert [message["role"] for message in third["messages"]] == ["system", "user"]
    assert third["messages"][1]["content"] == "turn three"


def test_http_brain_uses_default_prompt_when_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch, [_completion("ok")])
    brain = HttpBrain(
        base_url="http://127.0.0.1:11434/v1",
        model="local",
        system_prompt="   ",
    )

    brain.reply("hi")

    payload = json.loads(calls[0][0].data or b"{}")
    assert payload["messages"][0] == {
        "role": "system",
        "content": DEFAULT_SYSTEM_PROMPT,
    }


def test_http_brain_omits_authorization_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch, [_completion("ok")])
    brain = HttpBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    brain.reply("hi")

    assert _header(calls[0][0], "Authorization") is None


def test_http_brain_does_not_repr_api_key() -> None:
    brain = HttpBrain(
        base_url="http://127.0.0.1:11434/v1",
        model="local",
        api_key="super-secret-key",
    )

    assert "super-secret-key" not in repr(brain)


def test_http_error_becomes_brain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    error = urllib.error.HTTPError(
        "http://127.0.0.1:11434/v1/chat/completions",
        401,
        "unauthorized",
        hdrs=None,
        fp=io.BytesIO(b'{"error":"bad key"}'),
    )
    calls = _install(monkeypatch, [error])
    brain = HttpBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    with pytest.raises(BrainError) as info:
        brain.reply("hi")

    assert len(calls) == 1
    assert info.value.status == 401
    assert "bad key" in info.value.body


def test_connection_error_becomes_brain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [urllib.error.URLError("connection refused")])
    brain = HttpBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    with pytest.raises(BrainError, match="cannot reach brain endpoint"):
        brain.reply("hi")


def test_invalid_json_becomes_brain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [FakeResponse(b"not json")])
    brain = HttpBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    with pytest.raises(BrainError, match="invalid JSON") as info:
        brain.reply("hi")

    assert info.value.status == 200


def test_missing_choices_becomes_brain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [FakeResponse(b'{"choices": []}')])
    brain = HttpBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    with pytest.raises(BrainError, match="schema"):
        brain.reply("hi")


def test_non_string_content_becomes_brain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(
        monkeypatch,
        [FakeResponse(b'{"choices":[{"message":{"content":42}}]}')],
    )
    brain = HttpBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    with pytest.raises(BrainError, match="no assistant content"):
        brain.reply("hi")


def test_empty_body_becomes_brain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [FakeResponse(b"")])
    brain = HttpBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    with pytest.raises(BrainError, match="empty response"):
        brain.reply("hi")


def test_http_brain_rejects_empty_transcript() -> None:
    brain = HttpBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    with pytest.raises(BrainError, match="empty transcript"):
        brain.reply("  ")


def test_build_brain_factory() -> None:
    assert isinstance(build_brain(BrainConfig(kind="echo")), EchoBrain)

    http = build_brain(
        BrainConfig(
            kind="http",
            base_url="http://127.0.0.1:11434/v1",
            model="local",
        )
    )
    assert isinstance(http, HttpBrain)

    with pytest.raises(BrainError, match="unsupported brain kind"):
        build_brain(BrainConfig(kind="other"))


def test_http_brain_validation() -> None:
    with pytest.raises(ValueError, match="base_url"):
        HttpBrain(base_url="not-a-url", model="local")
    with pytest.raises(ValueError, match="model"):
        HttpBrain(base_url="http://127.0.0.1:11434/v1", model=" ")
    with pytest.raises(ValueError, match="max_tokens"):
        HttpBrain(base_url="http://127.0.0.1:11434/v1", model="local", max_tokens=0)
    with pytest.raises(ValueError, match="timeout_s"):
        HttpBrain(base_url="http://127.0.0.1:11434/v1", model="local", timeout_s=0)
