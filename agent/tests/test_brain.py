"""Tests for the echo and HTTP conversation brains."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest
from telephone_agent.actions import BrainActionAdapter, Consult, HangUp, Speak
from telephone_agent.brain import (
    DEFAULT_ACTION_SYSTEM_PROMPT,
    DEFAULT_SYSTEM_PROMPT,
    BrainError,
    EchoBrain,
    HttpBrain,
    HttpDeliberativeBrain,
    _parse_action,
    build_brain,
    build_deliberative_brain,
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


def test_parse_action_accepts_raw_speak_json() -> None:
    assert _parse_action('{"action": "speak", "text": "Hello there."}') == Speak("Hello there.")


@pytest.mark.parametrize(
    "content",
    [
        '```\n{"action": "speak", "text": "Fenced."}\n```',
        '```json\n{"action": "speak", "text": "Fenced."}\n```',
    ],
)
def test_parse_action_accepts_fenced_json(content: str) -> None:
    assert _parse_action(content) == Speak("Fenced.")


def test_parse_action_accepts_json_surrounded_by_prose() -> None:
    content = 'Here is my choice: {"action": "speak", "text": "Sure."} Thanks.'
    assert _parse_action(content) == Speak("Sure.")


def test_parse_action_falls_back_to_plain_text() -> None:
    assert _parse_action("  A plain spoken reply.  ") == Speak("A plain spoken reply.")


def test_parse_action_accepts_consult_with_timeout() -> None:
    action = _parse_action('{"action": "consult", "question": "Which booking?", "timeout_s": 4.5}')
    assert action == Consult(question="Which booking?", timeout_s=4.5)


def test_parse_action_accepts_consult_without_timeout() -> None:
    action = _parse_action('{"action": "consult", "question": "Which booking?"}')
    assert action == Consult(question="Which booking?")


def test_parse_action_defaults_hangup_reason() -> None:
    assert _parse_action('{"action": "hangup"}') == HangUp()


def test_parse_action_accepts_hangup_reason() -> None:
    assert _parse_action('{"action": "hangup", "reason": "caller_requested"}') == HangUp(
        reason="caller_requested"
    )


def test_parse_action_rejects_unknown_action() -> None:
    with pytest.raises(BrainError, match="unknown action"):
        _parse_action('{"action": "sing"}')


def test_parse_action_rejects_json_without_action() -> None:
    with pytest.raises(BrainError, match="string 'action'"):
        _parse_action('{"text": "Hello."}')


@pytest.mark.parametrize(
    "content, message",
    [
        ('{"action": "speak"}', "non-empty 'text'"),
        ('{"action": "speak", "text": "   "}', "non-empty 'text'"),
        ('{"action": "consult"}', "non-empty 'question'"),
        ('{"action": "consult", "question": ""}', "non-empty 'question'"),
    ],
)
def test_parse_action_rejects_empty_fields(content: str, message: str) -> None:
    with pytest.raises(BrainError, match=message):
        _parse_action(content)


def test_parse_action_truncates_long_speech() -> None:
    action = _parse_action(json.dumps({"action": "speak", "text": "x" * 1_200}))

    assert isinstance(action, Speak)
    assert len(action.text) == 1_000
    assert action.text.endswith("...")


def test_parse_action_rejects_long_question() -> None:
    content = json.dumps({"action": "consult", "question": "q" * 501})

    with pytest.raises(BrainError, match="500 characters"):
        _parse_action(content)


@pytest.mark.parametrize("timeout_s", [0, -1, "soon", True])
def test_parse_action_rejects_invalid_consult_timeout(timeout_s: object) -> None:
    content = json.dumps(
        {"action": "consult", "question": "Which booking?", "timeout_s": timeout_s}
    )

    with pytest.raises(BrainError, match="timeout_s"):
        _parse_action(content)


def test_http_deliberative_decide_posts_json_action(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, [_completion('{"action":"speak","text":"Hello."}')])
    brain = HttpDeliberativeBrain(
        base_url="https://llm.example.invalid/v1/",
        model="test-model",
        api_key="test-key",
        max_tokens=50,
        timeout_s=9.0,
    )

    assert brain.decide("Hi") == Speak("Hello.")

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
        {"role": "system", "content": DEFAULT_ACTION_SYSTEM_PROMPT},
        {"role": "user", "content": "Hi"},
    ]


def test_http_deliberative_keeps_history_across_decide_and_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_content = '{"action":"consult","question":"Which room?"}'
    calls = _install(
        monkeypatch,
        [
            _completion(first_content),
            _completion('{"action":"speak","text":"Room four."}'),
            _completion('{"action":"hangup","reason":"completed"}'),
        ],
    )
    brain = HttpDeliberativeBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    assert brain.decide("Where is the meeting?") == Consult(question="Which room?")
    assert brain.resume("Room four") == Speak("Room four.")

    second = json.loads(calls[1][0].data or b"{}")
    assert second["messages"] == [
        {"role": "system", "content": DEFAULT_ACTION_SYSTEM_PROMPT},
        {"role": "user", "content": "Where is the meeting?"},
        {"role": "assistant", "content": first_content},
        {"role": "user", "content": "The requester answered: Room four"},
    ]

    brain.reset()
    assert brain.decide("Anything else?") == HangUp()

    third = json.loads(calls[2][0].data or b"{}")
    assert third["messages"] == [
        {"role": "system", "content": DEFAULT_ACTION_SYSTEM_PROMPT},
        {"role": "user", "content": "Anything else?"},
    ]


def test_http_deliberative_uses_configured_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, [_completion("A plain spoken reply.")])
    brain = HttpDeliberativeBrain(
        base_url="http://127.0.0.1:11434/v1",
        model="local",
        system_prompt="Use the custom action prompt.",
    )

    assert brain.decide("Hi") == Speak("A plain spoken reply.")

    payload = json.loads(calls[0][0].data or b"{}")
    assert payload["messages"][0] == {
        "role": "system",
        "content": "Use the custom action prompt.",
    }


def test_http_deliberative_consult_timeout_makes_no_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(monkeypatch, [])
    brain = HttpDeliberativeBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    assert brain.on_consult_timeout() == Speak(
        "I'm sorry, I don't have that information right now."
    )
    assert calls == []


def test_http_deliberative_rejects_empty_transcript() -> None:
    brain = HttpDeliberativeBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    with pytest.raises(BrainError, match="empty transcript"):
        brain.decide("  ")


def test_http_deliberative_parse_failure_is_a_brain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [_completion('{"action":"dance"}')])
    brain = HttpDeliberativeBrain(base_url="http://127.0.0.1:11434/v1", model="local")

    with pytest.raises(BrainError, match="unknown action"):
        brain.decide("Hi")


def test_http_deliberative_does_not_repr_api_key() -> None:
    brain = HttpDeliberativeBrain(
        base_url="http://127.0.0.1:11434/v1",
        model="local",
        api_key="super-secret-key",
    )

    assert "super-secret-key" not in repr(brain)


def test_http_deliberative_validation() -> None:
    with pytest.raises(ValueError, match="base_url"):
        HttpDeliberativeBrain(base_url="not-a-url", model="local")
    with pytest.raises(ValueError, match="model"):
        HttpDeliberativeBrain(base_url="http://127.0.0.1:11434/v1", model=" ")
    with pytest.raises(ValueError, match="max_tokens"):
        HttpDeliberativeBrain(
            base_url="http://127.0.0.1:11434/v1",
            model="local",
            max_tokens=0,
        )
    with pytest.raises(ValueError, match="timeout_s"):
        HttpDeliberativeBrain(
            base_url="http://127.0.0.1:11434/v1",
            model="local",
            timeout_s=0,
        )


def test_build_deliberative_brain_text_mode() -> None:
    brain = build_deliberative_brain(BrainConfig(kind="echo"))

    assert isinstance(brain, BrainActionAdapter)
    assert brain.decide("hello") == Speak("I heard: hello")


def test_build_deliberative_brain_json_mode() -> None:
    brain = build_deliberative_brain(
        BrainConfig(
            kind="http",
            base_url="http://127.0.0.1:11434/v1",
            model="local",
            action_mode="json",
        )
    )

    assert isinstance(brain, HttpDeliberativeBrain)
    assert brain.base_url == "http://127.0.0.1:11434/v1"
    assert brain.model == "local"
