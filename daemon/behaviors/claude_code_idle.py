"""Claude Code idle villager. Stub — filled in at step 8."""

from behaviors.base import Behavior, TargetKind
from registry import register


@register
class ClaudeCodeIdleBehavior(Behavior):
    type_id = "claude_code_idle"
    display_name = "Claude Code idle"
    targets = {TargetKind.KEY}
    config_schema: dict = {
        "type": "object",
        "properties": {
            "waiting_states": {
                "type": "array",
                "items": {"type": "string"},
                "default": ["idle_prompt", "permission_prompt"],
            }
        },
    }
