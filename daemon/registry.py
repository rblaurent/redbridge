"""Behavior registry. Modules register themselves via @register on import."""

from __future__ import annotations

import importlib
import sys
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


def reload_behaviors() -> None:
    """Clear registry, reload all behavior modules, re-populate via @register."""
    BEHAVIORS.clear()
    if "gfx" in sys.modules:
        importlib.reload(sys.modules["gfx"])
    mods = [
        name for name in list(sys.modules)
        if name.startswith("behaviors.") and name != "behaviors.base"
    ]
    for name in mods:
        importlib.reload(sys.modules[name])
    importlib.reload(sys.modules["behaviors"])
