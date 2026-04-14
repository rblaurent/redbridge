"""Default unassigned behavior — renders black, does nothing."""

from __future__ import annotations

from PIL import Image

from behaviors.base import Behavior, TargetKind
from registry import register


@register
class EmptyBehavior(Behavior):
    type_id = "empty"
    display_name = "Empty"
    targets = {
        TargetKind.KEY,
        TargetKind.DIAL_ROTATE,
        TargetKind.DIAL_PRESS,
        TargetKind.STRIP_REGION,
    }
    config_schema = {"type": "object", "properties": {}}

    def render(self) -> Image.Image | None:
        w, h = self.size()
        if w == 0 or h == 0:
            return None
        return Image.new("RGB", (w, h), (0, 0, 0))
