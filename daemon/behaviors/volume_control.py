"""System volume control. Stub — filled in at step 9."""

from behaviors.base import Behavior, TargetKind
from registry import register


@register
class VolumeControlBehavior(Behavior):
    type_id = "volume_control"
    display_name = "Volume control"
    targets = {TargetKind.DIAL_ROTATE, TargetKind.DIAL_PRESS, TargetKind.STRIP_REGION}
    config_schema: dict = {
        "type": "object",
        "properties": {
            "device": {"type": "string", "default": "default"},
        },
    }
