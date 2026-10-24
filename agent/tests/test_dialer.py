"""Tests for the HTTP dial helper client."""

from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request

import pytest
from telephone_agent.dialer import Dialer, DialerError, mask_number


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


def _http_error(status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://helper.invalid/call",
        status,
        "error",
        hdrs=None,
        fp=io.BytesIO(body),
    )


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


def _header(request: urllib.request.Request, name: str) -> str:
    for key, value in request.headers.items():
        if key.lower() == name.lower():
            return value
    raise AssertionError(f"header {name!r} not found in {request.headers!r}")


def test_health_uses_get_and_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(
        monkeypatch,
        [FakeResponse(b'{"status":"ok"}')],
    )
    dialer = Dialer("http://helper:8080", "test-token")

    assert dialer.health() == {"status": "ok"}

    request, timeout = calls[0]
    assert request.full_url == "http://helper:8080/health"
    assert request.get_method() == "GET"
    assert _header(request, "Authorization") == "Bearer test-token"
    assert _header(request, "Accept") == "application/json"
    assert timeout == 10.0


def test_place_call_posts_json_and_an_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = _install(monkeypatch, [FakeResponse(b'{"status":"dialing"}')])
    dialer = Dialer("http://helper:8080", "test-token")
    number = "+15555550100"

    with caplog.at_level(logging.INFO, logger="telephone_agent.dialer"):
        result = dialer.place_call(number)

    assert result == {"status": "dialing"}
    request, _ = calls[0]
    assert request.full_url == "http://helper:8080/call"
    assert request.get_method() == "POST"
    assert _header(request, "Content-Type") == "application/json"
    assert json.loads(request.data or b"{}") == {"number": number}
    assert _header(request, "Idempotency-Key")
    assert number not in caplog.text
    assert "0100" in caplog.text
    assert "test-token" not in repr(dialer)


def test_place_call_retries_5xx_with_same_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(
        monkeypatch,
        [_http_error(503, b"busy"), FakeResponse(b'{"status":"dialing"}')],
    )
    dialer = Dialer("http://helper:8080", "test-token", retries=2, backoff_s=0.0)

    assert dialer.place_call("+15555550100") == {"status": "dialing"}
    assert len(calls) == 2
    first = calls[0][0]
    second = calls[1][0]
    assert _header(first, "Idempotency-Key") == _header(second, "Idempotency-Key")


def test_place_call_does_not_retry_4xx(monkeypatch: pytest.MonkeyPatch) -> None:
    number = "+15555550100"
    calls = _install(
        monkeypatch,
        [_http_error(403, f'{{"error":"not allowed to call {number}"}}'.encode())],
    )
    dialer = Dialer("http://helper:8080", "test-token", retries=5, backoff_s=0.0)

    with pytest.raises(DialerError) as info:
        dialer.place_call(number)

    assert len(calls) == 1
    error = info.value
    assert error.status == 403
    assert number not in str(error)
    assert number not in error.body
    assert "0100" in str(error)


def test_connection_errors_retry_then_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    url_error = urllib.error.URLError("connection refused")
    calls = _install(monkeypatch, [url_error, url_error, url_error])
    dialer = Dialer("http://helper:8080", "test-token", retries=2, backoff_s=0.0)

    with pytest.raises(DialerError, match="cannot reach dial helper") as info:
        dialer.health()

    assert len(calls) == 3
    assert info.value.status is None
    assert info.value.body == ""


def test_5xx_retries_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(
        monkeypatch,
        [
            _http_error(500, b"one"),
            _http_error(500, b"two"),
            _http_error(500, b"three"),
        ],
    )
    dialer = Dialer("http://helper:8080", "test-token", retries=2, backoff_s=0.0)

    with pytest.raises(DialerError) as info:
        dialer.health()

    assert len(calls) == 3
    assert info.value.status == 500
    assert info.value.body == "three"


def test_5xx_statuses_returned_by_a_custom_opener_are_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install(
        monkeypatch,
        [FakeResponse(b"busy", status=503), FakeResponse(b"{}")],
    )
    dialer = Dialer("http://helper:8080", "test-token", retries=1, backoff_s=0.0)

    assert dialer.health() == {}
    assert len(calls) == 2


def test_4xx_is_not_retried_for_health(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, [_http_error(401, b"unauthorized")])
    dialer = Dialer("http://helper:8080", "test-token", retries=5, backoff_s=0.0)

    with pytest.raises(DialerError) as info:
        dialer.health()

    assert len(calls) == 1
    assert info.value.status == 401
    assert info.value.body == "unauthorized"


def test_non_2xx_response_from_custom_opener_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, [FakeResponse(b"nope", status=418)])
    dialer = Dialer("http://helper:8080", "test-token", retries=0)

    with pytest.raises(DialerError) as info:
        dialer.health()

    assert info.value.status == 418
    assert info.value.body == "nope"


def test_invalid_json_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [FakeResponse(b"not json")])
    dialer = Dialer("http://helper:8080", "test-token")

    with pytest.raises(DialerError, match="invalid JSON") as info:
        dialer.health()

    assert info.value.status == 200


def test_non_object_json_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [FakeResponse(b"[1, 2, 3]")])
    dialer = Dialer("http://helper:8080", "test-token")

    with pytest.raises(DialerError, match="expected an object"):
        dialer.health()


def test_empty_response_is_an_empty_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [FakeResponse(b"")])
    dialer = Dialer("http://helper:8080", "test-token")

    assert dialer.health() == {}


def test_hangup_posts_to_hangup(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, [FakeResponse(b"{}")])
    dialer = Dialer("http://helper:8080", "test-token")

    assert dialer.hangup() == {}

    request, _ = calls[0]
    assert request.full_url == "http://helper:8080/hangup"
    assert request.get_method() == "POST"
    assert json.loads(request.data or b"{}") == {}


def test_state_uses_get_and_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, [FakeResponse(b'{"state":"ringing"}')])
    dialer = Dialer("http://helper:8080", "test-token")

    assert dialer.state() == {"state": "ringing"}

    request, timeout = calls[0]
    assert request.full_url == "http://helper:8080/state"
    assert request.get_method() == "GET"
    assert _header(request, "Authorization") == "Bearer test-token"
    assert _header(request, "Accept") == "application/json"
    assert timeout == 10.0


@pytest.mark.parametrize("state", ["idle", "ringing", "offhook", "unknown"])
def test_state_accepts_every_documented_phase(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    _install(monkeypatch, [FakeResponse(json.dumps({"state": state}).encode())])
    dialer = Dialer("http://helper:8080", "test-token")

    assert dialer.state() == {"state": state}


@pytest.mark.parametrize(
    "body",
    [
        b"{}",
        b'{"state":null}',
        b'{"state":1}',
        b'{"state":"busy"}',
        b'{"state":"RINGING"}',
    ],
)
def test_state_rejects_malformed_payloads(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    _install(monkeypatch, [FakeResponse(body)])
    dialer = Dialer("http://helper:8080", "test-token")

    with pytest.raises(DialerError, match="invalid call state") as info:
        dialer.state()

    assert info.value.status == 200
    assert info.value.body


def test_answer_posts_an_empty_body(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, [FakeResponse(b'{"state":"answering"}', status=202)])
    dialer = Dialer("http://helper:8080", "test-token")

    assert dialer.answer() == {"state": "answering"}

    request, _ = calls[0]
    assert request.full_url == "http://helper:8080/answer"
    assert request.get_method() == "POST"
    assert _header(request, "Authorization") == "Bearer test-token"
    assert _header(request, "Content-Type") == "application/json"
    assert json.loads(request.data or b"{}") == {}


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (409, b'{"error":"no ringing call"}'),
        (403, b'{"error":"answer permission denied"}'),
    ],
)
def test_answer_4xx_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    body: bytes,
) -> None:
    calls = _install(monkeypatch, [_http_error(status, body)])
    dialer = Dialer("http://helper:8080", "test-token", retries=5, backoff_s=0.0)

    with pytest.raises(DialerError) as info:
        dialer.answer()

    assert len(calls) == 1
    assert info.value.status == status
    assert info.value.body == body.decode()


def test_answer_500_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(
        monkeypatch,
        [_http_error(500, b'{"error":"answer failed"}'), FakeResponse(b'{"state":"answering"}')],
    )
    dialer = Dialer("http://helper:8080", "test-token", retries=1, backoff_s=0.0)

    assert dialer.answer() == {"state": "answering"}
    assert len(calls) == 2


def test_timeout_is_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install(monkeypatch, [FakeResponse()])
    dialer = Dialer("http://helper:8080", "test-token", timeout_s=3.5)

    dialer.health()

    assert calls[0][1] == 3.5


@pytest.mark.parametrize(
    "number, expected",
    [
        ("+15555550100", "*******0100"),
        ("+91123", "*1123"),
        ("1234", "****"),
        ("12", "**"),
        ("", "<none>"),
    ],
)
def test_mask_number(number: str, expected: str) -> None:
    assert mask_number(number) == expected


def test_dialer_rejects_empty_token() -> None:
    with pytest.raises(ValueError, match="token"):
        Dialer("http://helper:8080", " ")


def test_dialer_rejects_relative_url() -> None:
    with pytest.raises(ValueError, match="absolute http"):
        Dialer("helper:8080", "test-token")
