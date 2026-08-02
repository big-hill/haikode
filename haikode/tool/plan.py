"""
plan_exit — end plan mode by asking the user to approve the plan.

The plan-mode prompt has always instructed the model to finish with a
plan_exit call ("your turn should only end with either asking the user a
question or calling plan_exit"), and the tool did not exist: the model
called it, the call failed, and every planning session ended on a broken
promise (issue #1).

It rides the same seam the question tool uses — a permission request whose
metadata carries `kind: "question"` — so both front ends already know how
to render it, and a front end that predates questions degrades to
approve/reject. On approval the tool switches the live agent back to build
itself: `ctx.agent` is the running Agent, so no front-end cooperation is
required and the model's next step already has the build tool set.
"""

from typing import Any, Dict

from ..schema import PermissionDenied
from .base import Tool, ToolContext, ToolResult

APPROVE = "Approve — start building"
STAY = "Keep planning"

BUILD_AGENT = "build"


class PlanExitTool(Tool):
    name = "plan_exit"
    description = (
        "Signal that your plan is complete and ask the user to approve it. "
        "Call this exactly once, at the end of your planning turn, with a "
        "concise summary of the plan. If the user approves, you are switched "
        "to the build agent and should begin implementing; if not, continue "
        "planning. Do not use the question tool to ask 'is this plan okay?' — "
        "that is what this tool is for.")
    # Its own key, not "question": resolve_tools drops denied tools by this
    # key, and build denies plan_exit so a model outside plan mode is never
    # offered a plan to approve. The approval prompt itself still rides the
    # "question" key at ask time — it IS a question to the user.
    permission = "plan_exit"
    parameters = {
        "type": "object",
        "properties": {
            "plan": {"type": "string",
                     "description": "The plan being submitted for approval, "
                                    "as a short structured summary."},
        },
        "required": ["plan"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        agent = getattr(ctx, "agent", None)
        if agent is None or not getattr(agent, "plan_mode", False):
            # A model that reaches for plan approval outside plan mode is
            # confused, not dangerous; tell it so instead of erroring.
            return ToolResult(
                title="plan_exit",
                output="Not in plan mode — there is no plan to approve. "
                       "Continue with the user's request directly.")

        plan = str(args.get("plan", "")).strip()
        question = "The plan is ready. Approve it and switch to building?"
        if plan:
            question = "Plan:\n%s\n\n%s" % (plan, question)
        metadata: Dict[str, Any] = {
            "kind": "question",
            "questions": [{
                "question": question,
                "header": "Approve plan?",
                "options": [
                    {"label": APPROVE,
                     "description": "switch to the build agent and implement"},
                    {"label": STAY,
                     "description": "stay in plan mode and refine the plan"},
                ],
                "multiple": False,
            }],
            "answers": [],
        }
        approved = False
        try:
            ctx.ask("question", ["plan_exit"], question, metadata)
            answers = metadata.get("answers") or []
            first = answers[0] if answers else []
            if isinstance(first, str):
                first = [first]
            approved = any(str(item).strip().lower().startswith("approve")
                           for item in (first or []))
        except PermissionDenied:
            approved = False

        if not approved:
            return ToolResult(
                title="plan not approved",
                output="The user did not approve the plan. Stay in plan "
                       "mode: refine the plan, or ask what should change.",
                metadata={"approved": False})

        note = ""
        try:
            note = agent.switch_agent(BUILD_AGENT)
        except Exception as exc:
            return ToolResult(
                title="plan approved",
                output="The user approved the plan, but switching to the "
                       "build agent failed (%s). Ask the user to switch "
                       "manually with /build." % exc,
                metadata={"approved": True, "switched": False})
        return ToolResult(
            title="plan approved",
            output="The user approved the plan. You are now the build agent "
                   "(%s) — begin implementing it." % (note or "full tools"),
            metadata={"approved": True, "switched": True})


PLAN_EXIT_TOOL = PlanExitTool()

__all__ = ["PlanExitTool", "PLAN_EXIT_TOOL"]
