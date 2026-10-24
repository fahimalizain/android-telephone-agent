"""Poll the dial helper and answer inbound calls with the voice agent.

The helper on Phone A is the signaling path. It exposes ``GET /state`` and
``POST /answer``; Bluetooth HFP carries only audio, so there is no ringing
event or caller ID in user space. This module polls the helper once
per :attr:`InboundConfig.poll_interval_s`, answers a ringing call, and then runs
exactly one voice session at a time.

The watcher is deliberately small and dependency-injected so it can be tested
without HTTP, audio, or a phone:

* it never answers a call that is already ``offhook`` (the user may have
  answered manually);
* it waits for ``idle`` after a session before re-arming, so a call that ends
  remotely cannot cause a second session;
* helper failures are logged and retried with backoff rather than terminating
  the daemon;
* a stop event interrupts the wait between calls and is passed to the session
  so SIGTERM ends an active call.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

from .config import InboundConfig
from .dialer import DialerError

__all__ = ["InboundWatcher"]

logger = logging.getLogger(__name__)

_WAIT_SLICE_S = 0.25
_MAX_BACKOFF_S = 30.0


class InboundDialer(Protocol):
    """The helper operations the watcher needs."""

    def state(self) -> dict[str, object]: ...

    def answer(self) -> dict[str, object]: ...


class InboundSession(Protocol):
    """One call session, as built by the injected factory."""

    def run(self) -> object: ...


class InboundWatcher:
    """Watch for ringing calls, answer them, and run one session per call.

    Args:
        dialer: Helper client with ``state()`` and ``answer()``.
        session_factory: Builds one fresh session per answered call. It may
            take no arguments or accept the watcher's stop event as one
            positional or ``stop_event`` keyword argument.
        stop_event: Set by SIGTERM/SIGINT or a test to stop the watcher.
        config: Polling and answer-delay settings. Defaults to
            :class:`InboundConfig`.
        clock: Monotonic clock seam, accepted for callers that want
            deterministic time in tests.
        sleep: Sleep seam for deterministic waits in tests.
        once: Handle at most one call, then return.
        poll_interval_s: Optional override for ``config.poll_interval_s``.
        answer_delay_s: Optional override for ``config.answer_delay_s``.
    """

    def __init__(
        self,
        config: InboundConfig | None = None,
        *,
        dialer: InboundDialer,
        session_factory: Callable[..., InboundSession],
        stop_event: threading.Event,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        once: bool = False,
        poll_interval_s: float | None = None,
        answer_delay_s: float | None = None,
    ) -> None:
        self._dialer = dialer
        self._session_factory = session_factory
        self._stop = stop_event
        self._clock = clock
        self._sleep = sleep
        self._once = once

        settings = config or InboundConfig()
        self._poll_interval = (
            settings.poll_interval_s if poll_interval_s is None else poll_interval_s
        )
        self._answer_delay = settings.answer_delay_s if answer_delay_s is None else answer_delay_s

    def run(self) -> int:
        """Poll until stopped and return the number of answered calls.

        With ``once=True`` the loop returns after the first answered call. A
        helper error never escapes: it is logged and retried with bounded
        exponential backoff.
        """

        handled = 0
        failures = 0
        while not self._stop.is_set():
            try:
                state = self._dialer.state()
                failures = 0
            except DialerError as exc:
                failures += 1
                delay = self._backoff(failures)
                logger.warning(
                    "cannot read the inbound call state: %s; retrying in %.1f s",
                    exc,
                    delay,
                )
                self._wait(delay)
                continue

            phase = state.get("state")
            if phase == "ringing":
                if self._handle_ringing():
                    handled += 1
                    if self._once:
                        break
                self._wait(self._poll_interval)
            elif phase == "unknown":
                logger.warning("dial helper cannot read the phone call state; retrying")
                self._wait(self._poll_interval)
            else:
                # ``idle`` is the normal case. An ``offhook`` call was not
                # answered by us, so never start a session for it; just wait
                # until the helper reports idle again.
                self._wait(self._poll_interval)

        return handled

    def _handle_ringing(self) -> bool:
        """Answer one ringing call and run its session.

        Returns ``True`` when the helper accepted the answer request, even if
        the subsequent session failed. A call that stops ringing, was answered
        manually, or could not be answered produces ``False`` and no session.
        """

        if self._answer_delay > 0:
            if not self._wait(self._answer_delay):
                return False
            # The caller may have hung up or the user may have answered while
            # we waited. Re-check before asking the helper to answer.
            try:
                phase = self._dialer.state().get("state")
            except DialerError as exc:
                logger.warning("cannot re-check the ringing call before answering: %s", exc)
                return False
            if phase != "ringing":
                logger.info("call is no longer ringing (%s); not answering", phase)
                return False

        try:
            self._dialer.answer()
        except DialerError as exc:
            if exc.status == 409:
                logger.info("call stopped ringing before it could be answered")
            else:
                logger.warning("cannot answer the inbound call: %s", exc)
            return False

        logger.info("inbound call answered; opening the voice session")
        try:
            session = self._new_session()
            session.run()
        except Exception:
            logger.warning(
                "inbound voice session failed; the helper will be asked to end the call",
                exc_info=True,
            )

        if not self._once and not self._stop.is_set():
            self._wait_until_idle()
        return True

    def _new_session(self) -> InboundSession:
        """Build one session, supporting factories with or without a stop arg."""

        factory = self._session_factory
        try:
            parameters = inspect.signature(factory).parameters.values()
        except (TypeError, ValueError):
            return factory(self._stop)

        positional = False
        keyword = False
        for parameter in parameters:
            if parameter.name == "stop_event" and parameter.kind in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }:
                keyword = True
            elif parameter.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            }:
                positional = True

        if keyword:
            return factory(stop_event=self._stop)
        if positional:
            return factory(self._stop)
        return factory()

    def _wait_until_idle(self) -> None:
        """Poll until the helper reports ``idle`` or the stop event is set."""

        failures = 0
        while not self._stop.is_set():
            try:
                phase = self._dialer.state().get("state")
                failures = 0
            except DialerError as exc:
                failures += 1
                delay = self._backoff(failures)
                logger.warning(
                    "cannot confirm that the call ended: %s; retrying in %.1f s",
                    exc,
                    delay,
                )
                self._wait(delay)
                continue

            if phase == "idle":
                return
            self._wait(self._poll_interval)

    def _backoff(self, failures: int) -> float:
        exponent = max(failures - 1, 0)
        return min(max(self._poll_interval, 1.0) * (2**exponent), _MAX_BACKOFF_S)

    def _wait(self, seconds: float) -> bool:
        """Sleep for *seconds* in short slices, stopping early on the event.

        Returns ``True`` when the full delay elapsed, or ``False`` when the
        stop event was set. The injected clock lets tests advance time without
        real sleeping; an unchanged clock still makes progress in slices.
        """

        remaining = seconds
        while remaining > 0 and not self._stop.is_set():
            chunk = min(remaining, _WAIT_SLICE_S)
            before = self._clock()
            self._sleep(chunk)
            advanced = self._clock() - before
            remaining -= advanced if advanced > 0 else chunk
        return not self._stop.is_set()
