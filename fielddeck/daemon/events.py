"""The in-process event bus.

Two kinds of consumer, deliberately different:

**Sinks** are synchronous and never drop.  The session recorder and the audit
log are sinks: losing an ESTOP record because a queue was full is not an
acceptable failure mode.

**Subscriptions** are bounded async queues that *do* drop, oldest first, and
count what they dropped.  The HMI and remote clients are subscriptions.  A
slow Claude client must never be able to stall a CAN reader.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

from fielddeck.common.events import Event, EventType
from fielddeck.common.logging import get_logger

__all__ = ["EventBus", "Subscription"]

_log = get_logger("fielddeck.daemon.events")


class Subscription:
    """A bounded, lossy view of the event stream."""

    def __init__(
        self,
        bus: EventBus,
        *,
        maxsize: int = 512,
        types: Iterable[EventType] | None = None,
        session_id: str | None = None,
    ) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._types = frozenset(types) if types else None
        self._session_id = session_id
        self._closed = False
        #: Events discarded because this consumer could not keep up.
        self.dropped = 0

    def wants(self, event: Event) -> bool:
        if self._types is not None and event.type not in self._types:
            return False
        return not (self._session_id is not None and event.session_id != self._session_id)

    def offer(self, event: Event) -> None:
        """Never blocks.  Drops the oldest event when the consumer lags."""
        if self._closed or not self.wants(event):
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:  # pragma: no cover - race, harmless
                pass
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - race, harmless
                self.dropped += 1

    async def get(self) -> Event:
        return await self._queue.get()

    def close(self) -> None:
        self._closed = True
        self._bus.unsubscribe(self)

    def __aiter__(self) -> AsyncIterator[Event]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[Event]:
        while not self._closed:
            yield await self._queue.get()

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class EventBus:
    """Fan-out for every FieldDeck event."""

    def __init__(self, *, history: int = 1000) -> None:
        self._subscriptions: list[Subscription] = []
        self._sinks: list[Callable[[Event], Any]] = []
        self._history: deque[Event] = deque(maxlen=history)
        self._published = 0

    # -- production --------------------------------------------------------

    def publish(self, event: Event) -> Event:
        """Synchronous and non-blocking.  Safe to call from anywhere."""
        self._history.append(event)
        self._published += 1
        for sink in list(self._sinks):
            try:
                sink(event)
            except Exception:
                _log.exception("event sink failed", extra={"event_type": str(event.type)})
        for subscription in list(self._subscriptions):
            subscription.offer(event)
        return event

    # -- consumption -------------------------------------------------------

    def add_sink(self, sink: Callable[[Event], Any]) -> Callable[[], None]:
        """Register a lossless consumer.  Returns its removal callable."""
        self._sinks.append(sink)

        def remove() -> None:
            if sink in self._sinks:
                self._sinks.remove(sink)

        return remove

    def subscribe(
        self,
        *,
        maxsize: int = 512,
        types: Iterable[EventType] | None = None,
        session_id: str | None = None,
    ) -> Subscription:
        subscription = Subscription(self, maxsize=maxsize, types=types, session_id=session_id)
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            self._subscriptions.remove(subscription)

    # -- introspection -----------------------------------------------------

    def recent(
        self,
        *,
        limit: int = 100,
        types: Iterable[EventType] | None = None,
        since_monotonic_ns: int | None = None,
    ) -> list[Event]:
        wanted = frozenset(types) if types else None
        out: list[Event] = []
        for event in reversed(self._history):
            if wanted is not None and event.type not in wanted:
                continue
            if since_monotonic_ns is not None and event.monotonic_ns < since_monotonic_ns:
                break
            out.append(event)
            if len(out) >= limit:
                break
        out.reverse()
        return out

    def stats(self) -> dict[str, int]:
        return {
            "published": self._published,
            "subscribers": len(self._subscriptions),
            "sinks": len(self._sinks),
            "dropped": sum(sub.dropped for sub in self._subscriptions),
            "history": len(self._history),
        }
