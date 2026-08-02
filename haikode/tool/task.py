"""
task — delegate a scoped job to a sub-agent with a fresh context.

The sub-agent shares the provider, tools and permission state of the parent
but starts with an empty message history, so long searches don't pollute the
main conversation.
"""

from typing import Any, Dict

from .base import Tool, ToolContext, ToolResult, load_prompt

MAX_SUBAGENT_STEPS = 30

SUBAGENT_PROMPT = """You are a sub-agent working on one scoped task for another agent.

Work autonomously with the tools available. When you are done, reply with a
single final message that fully answers the task — the parent agent sees only
that message, not your tool calls. Be specific: include file paths, line
numbers, exact commands and concrete findings rather than summaries.
"""


class TaskTool(Tool):
    name = "task"
    description = load_prompt("task.txt")
    permission = "task"
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string",
                            "description": "A short (3-5 word) description of the task"},
            "prompt": {"type": "string",
                       "description": "The task for the agent to perform"},
            "subagent_type": {
                "type": "string",
                "description": "Which named subagent runs the task: "
                               "'general' (search, may run commands) or "
                               "'explore' (read-only locator). Custom "
                               "subagents from .haikode/agent/ work too. "
                               "Defaults to a generic subagent with the "
                               "caller's tools."},
        },
        "required": ["description", "prompt"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..agent import Agent  # late import: Agent imports the registry

        parent = getattr(ctx, "agent", None)
        if parent is None:
            raise RuntimeError("task tool requires an agent context")
        depth = getattr(ctx, "subagent_depth", 0)
        if depth >= 2:
            raise RuntimeError("task tool cannot nest more than 2 levels deep")

        label = args["description"]
        ctx.on_progress(f"task: {label}")

        # A named subagent brings its own tool list, permissions and prompt
        # through the same registry machinery an agent switch uses — plan
        # mode's prompt depends on `explore` existing here (issue #2). An
        # unknown name degrades to the generic subagent rather than failing
        # the call: the model is following instructions from a prompt that
        # may be newer or older than the local registry.
        type_name = str(args.get("subagent_type", "") or "").strip()
        registry = getattr(parent, "_registry", None)
        defn = None
        if type_name and registry is not None:
            candidate = registry.get(type_name)
            if candidate is not None and candidate.mode in ("subagent", "all"):
                defn = candidate

        sub = Agent(
            provider=parent.provider,
            model=parent.model,
            permissions=ctx.permissions,
            cwd=ctx.cwd,
            max_steps=MAX_SUBAGENT_STEPS,
            system_prompt=SUBAGENT_PROMPT,
            tool_names=[n for n in parent.tools if n != "task"],
            agent_name=defn.name if defn is not None else "",
            registry=registry if defn is not None else None,
        )
        sub.ctx.read_files = ctx.read_files
        sub.ctx.subagent_depth = depth + 1
        sub.ctx.aborted = ctx.aborted

        result = sub.run(args["prompt"], on_text=None,
                         on_event=lambda kind, payload: ctx.on_progress(
                             f"  task[{label}] {kind}: {payload}"
                             if kind == "tool" else ""))

        # Surface the sub-agent's file modifications to the parent for revert.
        ctx.modified_files.update(sub.ctx.modified_files)
        # Agent.run() absorbs its own ToolAborted so the sub-agent's history
        # stays well-formed, and it returns whatever text it had. Without this
        # the parent would record an interrupted delegation as a completed tool
        # call reading "(no result)" and take another provider step; the shared
        # abort Event is exactly what makes the check reliable here.
        ctx.check_abort()
        return ToolResult(title=label, output=result or "(no result)",
                          metadata={"steps": sub.steps_used})
