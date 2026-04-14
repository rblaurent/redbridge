"""Behavior registry. Modules register themselves via @register on import."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from behaviors.base import Behavior

BEHAVIORS: dict[str, type["Behavior"]] = {}


def register(cls: type["Behavior"]) -> type["Behavior"]:
    if not cls.type_id:
        raise ValueError(f"{cls.__name__} must set type_id")
    if cls.type_id in BEHAVIORS:
        raise ValueError(f"duplicate behavior type_id: {cls.type_id}")
    BEHAVIORS[cls.type_id] = cls
    return cls


def get(type_id: str) -> type["Behavior"] | None:
    return BEHAVIORS.get(type_id)


def all_behaviors() -> dict[str, type["Behavior"]]:
    return dict(BEHAVIORS)
