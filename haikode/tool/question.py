"""
question — let the model ask the user a structured multiple-choice question.

Ported from `packages/opencode/src/tool/question.ts`. opencode has a dedicated
Question service with its own event stream and a Deferred the TUI resolves;
haikode's tool layer has no UI at all, so the question rides the one seam that
already reaches the front-end: the permission asker.

The contract for a front-end
----------------------------
`ctx.ask("question", ...)` is called with metadata:

    {"questions": [{"question": ..., "header": ..., "options": [...],
                    "multiple": bool}],
     "answers": []}

A question-aware asker fills `request.metadata["answers"]` in place with one
entry per question (a label, or a list of labels) and then returns "once".
The metadata dict is the same object the tool holds, so the tool reads the
answers straight back out.

Degradation, which is the whole point of doing it this way:

- an asker that has never heard of questions just approves or rejects; it
  writes nothing, and the tool reports every question as "Unanswered"
- a headless run has no asker at all, the permission layer denies, and the
  tool *still* returns "Unanswered" instead of raising
- nothing here ever waits on a condition variable, so no front-end can hang
  the agent loop by ignoring the request

The model is told in the description to pick a sensible default when an answer
comes back unanswered, so an unanswered question costs a turn, not a deadlock.
"""

from typing import Any, Dict, List

from ..schema import PermissionDenied
from .base import Tool, ToolContext, ToolResult, load_prompt

MAX_QUESTIONS = 5
MAX_OPTIONS = 12
UNANSWERED = "Unanswered"


def _clean_options(raw: Any) -> List[Dict[str, str]]:
    options: List[Dict[str, str]] = []
    for item in (raw or [])[:MAX_OPTIONS]:
        if isinstance(item, str):
            options.append({"label": item, "description": ""})
            continue
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        if not label:
            continue
        options.append({"label": label,
                        "description": str(item.get("description", "")).strip()})
    return options


def _normalise(raw: Any) -> List[Dict[str, Any]]:
    """Accept the model's questions leniently; drop anything unusable."""
    questions: List[Dict[str, Any]] = []
    for item in (raw or [])[:MAX_QUESTIONS]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("question", "")).strip()
        if not text:
            continue
        header = str(item.get("header", "")).strip() or text[:30]
        questions.append({
            "question": text,
            "header": header,
            "options": _clean_options(item.get("options")),
            "multiple": bool(item.get("multiple")),
        })
    return questions


def _selected(answer: Any) -> List[str]:
    """One asker answer -> list of labels. Tolerates a bare string."""
    if answer is None:
        return []
    if isinstance(answer, str):
        text = answer.strip()
        return [text] if text else []
    if isinstance(answer, (list, tuple, set)):
        out = []
        for item in answer:
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    text = str(answer).strip()
    return [text] if text else []


def _collect(metadata: Dict[str, Any], count: int) -> List[List[str]]:
    """Read whatever the asker wrote back, in whatever shape it used."""
    raw = metadata.get("answers")
    if raw is None:
        raw = metadata.get("answer")
    if raw is None:
        return [[] for _ in range(count)]

    # A single question answered with a bare string or a flat list of labels.
    if isinstance(raw, str):
        raw = [raw]
    elif isinstance(raw, (list, tuple)):
        raw = list(raw)
        if count == 1 and raw and all(isinstance(item, str) for item in raw):
            raw = [raw]
    else:
        raw = [raw]

    answers = [_selected(raw[i]) if i < len(raw) else [] for i in range(count)]
    return answers


class QuestionTool(Tool):
    name = "question"
    description = load_prompt("question.txt")
    permission = "question"
    parameters = {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "Questions to ask (at most %d)" % MAX_QUESTIONS,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string",
                                     "description": "Complete question"},
                        "header": {"type": "string",
                                   "description": "Very short label (max 30 chars)"},
                        "options": {
                            "type": "array",
                            "description": "Available choices",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string",
                                              "description": "Display text (1-5 words, concise)"},
                                    "description": {"type": "string",
                                                    "description": "Explanation of choice"},
                                },
                                "required": ["label", "description"],
                            },
                        },
                        "multiple": {"type": "boolean",
                                     "description": "Allow selecting multiple choices"},
                    },
                    "required": ["question", "header", "options"],
                },
            },
        },
        "required": ["questions"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        questions = _normalise(args.get("questions"))
        if not questions:
            raise ValueError("questions is required: pass at least one "
                             "{question, header, options} object")

        # The metadata dict is the channel: we keep the reference, the asker
        # writes "answers" into it, we read them back after ctx.ask returns.
        metadata: Dict[str, Any] = {
            "questions": questions,
            "answers": [],
            "kind": "question",
        }
        headers = [q["header"] for q in questions]

        dismissed = False
        try:
            ctx.ask("question", headers, questions[0]["question"], metadata)
        except PermissionDenied:
            # No asker, a deny rule, or the user dismissed the prompt. None of
            # those should kill the turn — the model just learns nothing.
            dismissed = True

        answers = ([[] for _ in questions] if dismissed
                   else _collect(metadata, len(questions)))

        formatted = ", ".join(
            '"%s"="%s"' % (question["question"],
                           ", ".join(answer) if answer else UNANSWERED)
            for question, answer in zip(questions, answers))

        answered = sum(1 for answer in answers if answer)
        if answered:
            output = ("User has answered your questions: %s. You can now "
                      "continue with the user's answers in mind." % formatted)
        else:
            output = ("The user did not answer: %s. Do not ask again — choose "
                      "a sensible default, state which default you chose, and "
                      "continue." % formatted)

        return ToolResult(
            title="Asked %d question%s" % (len(questions),
                                           "" if len(questions) == 1 else "s"),
            output=output,
            metadata={"answers": answers, "questions": questions,
                      "answered": answered, "dismissed": dismissed})


QUESTION_TOOL = QuestionTool()

__all__ = ["QuestionTool", "QUESTION_TOOL"]
