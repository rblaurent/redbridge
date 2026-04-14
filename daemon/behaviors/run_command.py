"""Run a shell command. Stub — filled in at step 9."""

from behaviors.base import Behavior, TargetKind
from registry import register


@register
class RunCommandBehavior(Behavior):
    type_id = "run_command"
    display_name = "Run command"
    targets = {TargetKind.KEY, TargetKind.DIAL_PRESS}
    config_schema: dict = {
        "type": "object",
        "properties": {
            "command": {"type": "string"},
            "icon_path": {"type": "string", "default": ""},
            "ok_color": {"type": "string", "default": "#00aa00"},
            "fail_color": {"type": "string", "default": "#aa0000"},
        },
        "required": ["command"],
    }
