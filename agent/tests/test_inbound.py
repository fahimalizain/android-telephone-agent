"""Tests for the inbound call watcher, without HTTP, audio, or a phone."""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable

from telephone_agent.config import InboundConfig
from telephone_agent.dialer import DialerError
from telephone_agent.inbound import InboundWatcher


class FakeTime:
    """Deterministic clock and sleeper; records every requested delay."""

    def __init__(self, stop_event: threading.Event) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._stop = stop_event

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self._stop.is_set():
            return


class FakeDialer:
    """Return a scripted state sequence, then set the stop event on idle."""

    def __init__(
        self,
        states: list[str],
        stop_event: threading.Event,
        *,
        answer_error: DialerError | None = None,
    ) -> None:
        self.states = deque(states)
        self.stop_event = stop_event
        self.answer_error = answer_error
        self.state_calls = 0
        self.answers = 0

    def state(self) -> dict[str, object]:
        self.state_calls += 1
        if self.states:
            return {"state": self.states.popleft()}
        self.stop_event.set()
        return {"state": "idle"}

    def answer(self) -> dict[str, object]:
        self.answers += 1
        if self.answer_error is not None:
            raise self.answer_error
        return {"state": "answering"}


class FlakyDialer:
    """Fail a fixed number of times, then report idle and stop the watcher."""

    def __init__(self, failures: int, stop_event: threading.Event) -> None:
        self.failures = failures
        self.stop_event = stop_event
        self.calls = 0

    def state(self) -> dict[str, object]:
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise DialerError("cannot reach dial helper")
        self.stop_event.set()
        return {"state": "idle"}

    def answer(self) -> dict[str, object]:
        raise AssertionError("answer must not be called")


class FakeSession:
    def __init__(self, stop_event: threading.Event | None = None) -> None:
        self.stop_event = stop_event
        self.runs = 0
        self.on_run: Callable[[FakeSession], None] | None = None

    def run(self) -> None:
        self.runs += 1
        if self.on_run is not None:
            self.on_run(self)


def _watcher(
    *,
    dialer: object,
    stop_event: threading.Event,
    factory: Callable[..., FakeSession],
    answer_delay_s: float = 0.0,
    once: bool = False,
) -> tuple[InboundWatcher, FakeTime]:
    time_source = FakeTime(stop_event)
    watcher = InboundWatcher(
        dialer=dialer,  # type: ignore[arg-type]
        session_factory=factory,
        stop_event=stop_event,
        config=InboundConfig(poll_interval_s=1.0, answer_delay_s=answer_delay_s),
        clock=time_source.clock,
        sleep=time_source.sleep,
        once=once,
    )
    return watcher, time_source


def test_ringing_call_is_answered_and_runs_one_session() -> None:
    stop_event = threading.Event()
    dialer = FakeDialer(["ringing"], stop_event)
    sessions: list[FakeSession] = []
    seen_stop: list[threading.Event] = []

    def factory(stop_event: threading.Event) -> FakeSession:
        seen_stop.append(stop_event)
        session = FakeSession(stop_event)
        sessions.append(session)
        return session

    watcher, _time = _watcher(
        dialer=dialer,
        stop_event=stop_event,
        factory=factory,
        once=True,
    )

    assert watcher.run() == 1
    assert dialer.answers == 1
    assert len(sessions) == 1
    assert sessions[0].runs == 1
    assert seen_stop == [stop_event]
    assert sessions[0].stop_event is stop_event


def test_manual_offhook_call_is_not_answered_or_run() -> None:
    stop_event = threading.Event()
    dialer = FakeDialer(["offhook"], stop_event)
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    watcher, _time = _watcher(dialer=dialer, stop_event=stop_event, factory=factory)

    assert watcher.run() == 0
    assert dialer.answers == 0
    assert sessions == []


def test_answer_409_does_not_start_a_session() -> None:
    stop_event = threading.Event()
    dialer = FakeDialer(
        ["ringing"],
        stop_event,
        answer_error=DialerError("no ringing call", status=409, body="no ringing call"),
    )
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    watcher, _time = _watcher(dialer=dialer, stop_event=stop_event, factory=factory)

    assert watcher.run() == 0
    assert dialer.answers == 1
    assert sessions == []


def test_answer_delay_rechecks_before_answering() -> None:
    stop_event = threading.Event()
    dialer = FakeDialer(["ringing", "offhook"], stop_event)
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    watcher, time_source = _watcher(
        dialer=dialer,
        stop_event=stop_event,
        factory=factory,
        answer_delay_s=2.0,
    )

    assert watcher.run() == 0
    assert dialer.answers == 0
    assert sessions == []
    assert sum(time_source.sleeps) >= 2.0


def test_helper_failures_are_retried_without_exiting() -> None:
    stop_event = threading.Event()
    dialer = FlakyDialer(failures=2, stop_event=stop_event)

    def factory() -> FakeSession:
        raise AssertionError("a session must not start without a ringing call")

    watcher, time_source = _watcher(dialer=dialer, stop_event=stop_event, factory=factory)

    assert watcher.run() == 0
    assert dialer.calls == 3
    assert time_source.sleeps
    assert all(delay > 0 for delay in time_source.sleeps)


def test_once_handles_at_most_one_call() -> None:
    stop_event = threading.Event()
    dialer = FakeDialer(["ringing", "ringing", "ringing"], stop_event)
    sessions: list[FakeSession] = []

    def factory(stop_event: threading.Event) -> FakeSession:
        session = FakeSession(stop_event)
        sessions.append(session)
        return session

    watcher, _time = _watcher(
        dialer=dialer,
        stop_event=stop_event,
        factory=factory,
        once=True,
    )

    assert watcher.run() == 1
    assert dialer.answers == 1
    assert len(sessions) == 1


def test_stop_event_ends_an_active_session_and_is_passed_through() -> None:
    stop_event = threading.Event()
    dialer = FakeDialer(["ringing"], stop_event)
    seen_stop: list[threading.Event] = []

    def factory(stop_event: threading.Event) -> FakeSession:
        seen_stop.append(stop_event)
        session = FakeSession(stop_event)

        def interrupt(_session: FakeSession) -> None:
            # SIGTERM arrives while the call is active.
            stop_event.set()

        session.on_run = interrupt
        return session

    watcher, _time = _watcher(dialer=dialer, stop_event=stop_event, factory=factory)

    assert watcher.run() == 1
    assert seen_stop == [stop_event]
    assert stop_event.is_set()


def test_duplicate_sessions_are_not_started_while_the_call_never_goes_idle() -> None:
    stop_event = threading.Event()
    dialer = FakeDialer(["ringing", "ringing", "ringing"], stop_event)
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    watcher, time_source = _watcher(dialer=dialer, stop_event=stop_event, factory=factory)

    assert watcher.run() == 1
    assert dialer.answers == 1
    assert len(sessions) == 1
    assert time_source.sleeps


def test_busy_executor_does_not_answer_the_ringing_call() -> None:
    stop_event = threading.Event()
    dialer = FakeDialer(["ringing"], stop_event)
    sessions: list[FakeSession] = []

    def factory() -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    watcher = InboundWatcher(
        dialer=dialer,
        session_factory=factory,
        stop_event=stop_event,
        config=InboundConfig(poll_interval_s=1.0),
        once=True,
        can_accept=lambda: False,
    )

    assert watcher.run() == 0
    assert dialer.answers == 0
    assert sessions == []


def test_can_accept_is_rechecked_after_the_answer_delay() -> None:
    stop_event = threading.Event()
    dialer = FakeDialer(["ringing", "ringing"], stop_event)
    sessions: list[FakeSession] = []
    checks = 0

    def can_accept() -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    def factory() -> FakeSession:
        session = FakeSession()
        sessions.append(session)
        return session

    time_source = FakeTime(stop_event)
    watcher = InboundWatcher(
        dialer=dialer,
        session_factory=factory,
        stop_event=stop_event,
        config=InboundConfig(poll_interval_s=1.0, answer_delay_s=1.0),
        clock=time_source.clock,
        sleep=time_source.sleep,
        can_accept=can_accept,
    )

    assert watcher.run() == 0
    assert checks >= 2
    assert dialer.answers == 0
    assert sessions == []
