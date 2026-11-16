"""Fan-out of bot change events to /api/events subscribers."""

from __future__ import annotations

from .logstore import Subscriber


class EventHub:
    def __init__(self) -> None:
        self.subscribers: set[Subscriber] = set()

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        self.subscribers.add(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self.subscribers.discard(sub)

    def publish(self, event: str, data: dict) -> None:
        for sub in list(self.subscribers):
            sub.push((event, data))
            if sub.closed:
                self.subscribers.discard(sub)

    def close_all(self) -> None:
        for sub in list(self.subscribers):
            sub.close()
        self.subscribers.clear()
