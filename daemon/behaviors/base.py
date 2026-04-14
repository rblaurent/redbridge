"""Behavior ABC + target types."""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from PIL import Image


class TargetKind(str, Enum):
    KEY = "key"
    DIAL_ROTATE = "dial_rotate"
    DIAL_PRESS = "dial_press"
    STRIP_REGION = "strip_region"


@dataclass(frozen=True)
class Target:
    kind: TargetKind
    index: int


# Stream Deck Plus pixel sizes.
KEY_SIZE: tuple[int, int] = (120, 120)
STRIP_REGION_SIZE: tuple[int, int] = (200, 100)


def size_for(kind: TargetKind) -> tuple[int, int]:
    if kind == TargetKind.KEY:
        return KEY_SIZE
    if kind == TargetKind.STRIP_REGION:
        return STRIP_REGION_SIZE
    return (0, 0)


class EventBus:
    """Minimal topic-based pub/sub. Real ingestion lands in step 8."""

    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[dict[str, Any]], None]]] = {}

    def subscribe(self, topic: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self._subs.setdefault(topic, []).append(handler)

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        for h in self._subs.get(topic, ()):
            try:
                h(payload)
            except Exception:
                pass


class Behavior(ABC):
    type_id: str = ""
    display_name: str = ""
    targets: set[TargetKind] = set()
    config_schema: dict[str, Any] = {"type": "object", "properties": {}}

    def __init__(self, target: Target, config: dict[str, Any], bus: EventBus) -> None:
        self.target = target
        self.config = config
        self.bus = bus

    def size(self) -> tuple[int, int]:
        return size_for(self.target.kind)

    def render(self) -> Image.Image | None:
        return None

    def on_press(self) -> None:
        pass

    def on_rotate(self, delta: int) -> None:
        pass

    def on_external_event(self, event: dict[str, Any]) -> None:
        pass

    def tick(self) -> bool:
        return False
