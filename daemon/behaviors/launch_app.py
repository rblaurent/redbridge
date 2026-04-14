"""Launch an executable. Stub — filled in at step 9."""

from behaviors.base import Behavior, TargetKind
from registry import register


@register
class LaunchAppBehavior(Behavior):
    type_id = "launch_app"
    display_name = "Launch app"
    targets = {TargetKind.KEY}
    config_schema: dict = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}, "default": []},
            "icon_path": {"type": "string", "default": ""},
        },
        "required": ["path"],
    }
