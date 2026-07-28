"""
skill — load one packaged instruction set by name.

Port of `packages/opencode/src/tool/skill.ts`. Discovery, parsing and the
output block live in haikode/skills.py; this file is the thin tool wrapper:
resolve the name, ask permission for that one skill, hand back its body.

Two deliberate differences from opencode:

- an unknown name is a tool *error* naming the available skills, not a fatal
  one. opencode dies on it; here the model reads the list and retries, which
  costs a turn instead of the session.
- the registry is re-scanned once before giving up, so a skill written during
  the session is found without a restart. The happy path stays on the cache.
"""

from typing import Any, Dict, Sequence

from .. import skills
from .base import Tool, ToolContext, ToolResult, load_prompt

FALLBACK_DESCRIPTION = (
    "Load a specialized skill by name when the task matches one of the skills "
    "listed in the system prompt. The skill's full instructions are returned.")


class SkillTool(Tool):
    name = "skill"
    description = load_prompt("skill.txt") or FALLBACK_DESCRIPTION
    permission = "skill"
    # execute() asks under its own key with the concrete skill name, so the
    # agent must not also ask centrally for the bare tool name.
    asks_own_permission = True
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "The name of the skill, exactly as listed "
                                    "under 'Available skills'"},
        },
        "required": ["name"],
    }

    def permission_patterns(self, args: Dict[str, Any],
                            ctx: ToolContext) -> Sequence[str]:
        """The skill being loaded, so a rule or grant names one skill."""
        name = str(args.get("name") or "").strip()
        return [name] if name else [self.name]

    def _resolve(self, name: str, ctx: ToolContext):
        registry = skills.load(ctx.cwd)
        skill = registry.get(name)
        if skill is not None:
            return skill, registry
        # Miss: the cache may predate the skill. One rescan, then give up.
        registry = skills.load(ctx.cwd, refresh=True)
        return registry.get(name), registry

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip()
        if not name:
            raise ValueError("name is required: pass the name of a skill "
                             "listed in the system prompt")

        skill, registry = self._resolve(name, ctx)
        if skill is None:
            available = ", ".join(registry.names()) or "none"
            raise ValueError('Unknown skill "%s". Available skills: %s'
                             % (name, available))

        ctx.ask("skill", [skill.name], "Load skill: %s" % skill.name,
                {"skill": skill.name, "location": str(skill.path)},
                always=[skill.name])
        ctx.check_abort()

        return ToolResult(
            title="Loaded skill: %s" % skill.name,
            output=skill.instructions(),
            metadata={"name": skill.name,
                      "directory": str(skill.directory),
                      "location": str(skill.path),
                      "truncated": skill.truncated})


SKILL_TOOL = SkillTool()

__all__ = ["SkillTool", "SKILL_TOOL"]
