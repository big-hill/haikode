"""
The agent loop — native tool calling, parallel calls, proper tool-role messages.

This replaces the old markdown-``` tool`` convention. The model now receives
real tool schemas and emits real function calls, which is what modern models
are trained for: it can call several tools in one turn, and results go back
as `tool` messages tied to their call id.

The Agent is also where the pieces the rest of the package builds are actually
assembled, because the system message and the tool list are the only two
channels the model ever sees:

  * `prompt.build_system_prompt` picks the prompt variant for the model family
  * `ContextManager` supplies the environment block and the AGENTS.md chain,
    extended with the instruction files the project config declares
  * `MemoryStore.context_block()` appends what earlier sessions chose to save
  * `AgentRegistry` decides which tools exist at all and which permissions
    guard them, so plan mode is read-only in both dimensions at once
  * `UsageTracker` accumulates every provider usage payload, per run and per
    session, which is what the context meter reports

Everything above is pure local work: prompt assembly never touches the network.
"""

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .agents import (BUILTIN, DEFAULT_AGENT, AgentDef, AgentPermissions,
                     AgentRegistry, enter_plan_text, exit_plan_text,
                     is_readonly)
from .context import ContextManager, compact_history, message_tokens
from .memory import MemoryStore
from .permission import DENY, Permissions
from .prompt import build_system_prompt, select_variant
from .providers.base import ERROR_TEXT_MARKER, Provider, chunk_error
from .redact import redact
from .schema import CompletionChunk, Msg, PermissionDenied, ToolAborted, ToolCall
from .tool import REGISTRY, ToolContext, get_tools, tool_specs
from .tool.base import Tool, prompts_for_itself
from .usage import ContextState, UsageTracker, measure_context

DEFAULT_MAX_TOKENS = 8192

# The memory index rides in every request, so it is budgeted like the
# instruction files rather than dumped in whole.
MEMORY_CONTEXT_CHARS = 4000

PLAN_AGENT = "plan"

# What a tool call that never ran is answered with. Every provider validates
# that an assistant turn's calls are all answered, so a call left open by an
# interrupted run has to be closed with *something*.
INTERRUPTED_TOOL_RESULT = ("Error: this tool call was interrupted and never "
                           "produced a result.")

# Ported from opencode's runner/max-steps.ts. The last configured step is a
# handoff, not another chance to start work that the loop will abandon.
MAX_STEPS_PROMPT = """MAXIMUM STEPS REACHED

The configured step budget for this user turn has been reached. Tools are disabled until the next user input. Respond with text only.

State that the step budget was reached, summarize what was accomplished, list anything still unfinished, and recommend the next action. The user can continue in the same session with a new message, which starts a fresh step budget."""


class ProviderFailure(RuntimeError):
    """A provider round that failed, raised instead of being answered with.

    Providers never raise out of stream(): they end with a chunk carrying a
    structured error. That chunk used to be folded into the assistant message
    like any other text, which stored an auth failure as something the model
    said and replayed it next turn. The loop raises this instead, so a failed
    round leaves no assistant turn behind and a one-shot run can exit non-zero.
    """

    def __init__(self, error: Optional[Dict[str, Any]] = None):
        self.error: Dict[str, Any] = dict(error or {})
        self.kind = str(self.error.get("kind") or "unknown")
        self.retryable = bool(self.error.get("retryable"))
        super().__init__(str(self.error.get("message") or self.kind))


def provider_failure(chunk: CompletionChunk) -> Optional[Dict[str, Any]]:
    """The structured error a chunk carries, or None.

    usage["error"] is the authoritative channel. A chunk that only sets
    stop_reason="error" — an older provider, or one whose payload was lost — is
    still a failure and is given the same shape, so nothing that says "error"
    can reach the transcript as an answer.
    """
    structured = chunk_error(chunk)
    if structured:
        return dict(structured)
    if chunk.stop_reason != "error":
        return None
    message = (chunk.text or "").strip()
    if message.startswith(ERROR_TEXT_MARKER):
        message = message[len(ERROR_TEXT_MARKER):].strip()
    return {"kind": "unknown", "message": message or "provider stream failed",
            "retryable": False}


def _pairing_intact(messages: Sequence[Msg]) -> bool:
    """True when every tool call is answered by the tool messages right after it.

    Adjacency, not mere presence: Anthropic requires the tool_result blocks in
    the user turn immediately following the tool_use blocks, and the OpenAI
    dialect is only slightly more forgiving.
    """
    open_ids: List[str] = []
    for message in messages:
        if message.role == "tool":
            if message.tool_call_id not in open_ids:
                return False
            open_ids.remove(message.tool_call_id)
            continue
        if open_ids:
            return False
        if message.role == "assistant" and message.tool_calls:
            open_ids = [call.id for call in message.tool_calls]
    return not open_ids


def pair_tool_messages(messages: Sequence[Msg]) -> List[Msg]:
    """Repair a history whose tool calls and results do not line up.

    The agent loop itself always pairs them, but a history can reach a provider
    from somewhere else: a front-end copying the conversation onto a rebuilt
    agent while a tool is still running, a session restored from disk, a
    revert. One assistant turn with an unanswered call makes *every* later
    request 400, for the rest of the session, which is indistinguishable from
    the provider being down.

    Unanswered calls get a synthetic error result, orphaned results are
    dropped, and an already-consistent history is returned untouched so the
    normal path pays one linear scan and no allocation.
    """
    if _pairing_intact(messages):
        return messages if isinstance(messages, list) else list(messages)

    out: List[Msg] = []
    index, total = 0, len(messages)
    while index < total:
        message = messages[index]
        index += 1
        if message.role == "tool":
            continue                    # its call is gone; so must it be
        out.append(message)
        if not (message.role == "assistant" and message.tool_calls):
            continue
        wanted = [call.id for call in message.tool_calls]
        answered = set()
        while index < total and messages[index].role == "tool":
            reply = messages[index]
            index += 1
            if reply.tool_call_id in wanted and reply.tool_call_id not in answered:
                answered.add(reply.tool_call_id)
                out.append(reply)
        for call_id in wanted:
            if call_id not in answered:
                out.append(Msg(role="tool", tool_call_id=call_id,
                               content=INTERRUPTED_TOOL_RESULT))
    return out


def _permission_keys() -> Dict[str, str]:
    """Tool name -> the permission key it asks under.

    Most tools ask under their own name, but not all (memory_read asks under
    "read"), and AgentRegistry.resolve_tools has to know the difference to drop
    a tool an agent denies.
    """
    return {name: (tool.permission or name) for name, tool in REGISTRY.items()}


def _rule_denies(rule: Any) -> bool:
    """True when a permission rule refuses every pattern it could match."""
    if isinstance(rule, str):
        return rule == DENY
    if isinstance(rule, dict):
        return bool(rule) and all(value == DENY for value in rule.values())
    return False


def _lookup(rules: Dict[str, Any], key: str) -> Any:
    return rules.get(key, rules.get(key.lower()))


def _denies(defn: AgentDef, key: str) -> bool:
    """True when this agent refuses `key` outright.

    Used to decide which session-scoped "always" grants may survive an agent
    switch. Permissions.decide() resolves a configured deny ahead of the
    grants, so a bash grant carried into plan mode is inert rather than an
    escape; dropping it as well keeps the grant list honest about what is
    actually in force, and does not depend on that ordering staying true.
    """
    return _rule_denies(_lookup(defn.permission, key))


def _denied_for_every_pattern(permissions: Optional[Permissions],
                              key: str) -> bool:
    """True when nothing under `key` could be allowed, whatever the pattern is.

    This is the only part of the pre-dispatch check that is safe to apply to a
    tool which asks for itself: the agent does not know that tool's pattern, so
    it may act on a rule that denies all of them and on nothing else. Anything
    looser would refuse actions the configuration allows — `{"*": "deny",
    "git *": "allow"}` denies most bash commands but not `git status`.

    Read through `describe()`, which reports the rules in evaluation order, so
    this cannot drift from what `Permissions.ask` will decide. A deny is
    absolute there, ahead of both auto-approve and session grants, so neither
    is consulted here either.
    """
    describe = getattr(permissions, "describe", None)
    if describe is None:
        return False                # unknown Permissions: leave it to the tool
    rows = [(glob, decision) for name, glob, decision, configured in describe()
            if name == key and configured]
    if not rows or any(decision != DENY for _, decision in rows):
        return False
    # A catch-all has to be among them, or a pattern that matches no rule at
    # all falls through to the key's default, which is never a deny.
    return any(set(glob) == {"*"} for glob, _ in rows)


def _declared_patterns(tool: Tool, args: Dict[str, Any],
                       ctx: ToolContext) -> List[str]:
    """What the tool says identifies this call, never silently widened to "*".

    A tool is free to declare "*" itself; what it must not get is "*" by
    omission, because one "always" answer to that grants the key outright.
    """
    try:
        declared = tool.permission_patterns(args, ctx)
    except Exception:
        declared = None
    patterns = [str(p) for p in (declared or []) if str(p)]
    return patterns or [tool.name or tool.permission]


class _CallAccumulator:
    """Reassembles tool calls that arrive as streamed fragments."""

    def __init__(self):
        self.calls: Dict[int, Dict[str, str]] = {}

    def add(self, delta: Dict):
        index = delta.get("index", 0)
        entry = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if delta.get("id"):
            entry["id"] = delta["id"]
        if delta.get("name"):
            entry["name"] = delta["name"]
        if delta.get("arguments"):
            entry["arguments"] += delta["arguments"]

    def finish(self) -> List[ToolCall]:
        out = []
        for index in sorted(self.calls):
            entry = self.calls[index]
            if not entry["name"]:
                continue
            raw = entry["arguments"].strip() or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                # Some models emit trailing prose or double-encode the object.
                try:
                    args = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
                except (ValueError, json.JSONDecodeError):
                    args = {"__malformed__": raw}
            if not isinstance(args, dict):
                args = {"value": args}
            out.append(ToolCall(id=entry["id"] or f"call_{index}",
                                name=entry["name"], arguments=args))
        return out


class Agent:
    def __init__(self, provider: Provider, model: str,
                 permissions: Optional[Permissions] = None,
                 cwd: str = ".",
                 max_steps: Optional[int] = None,
                 max_tokens: int = DEFAULT_MAX_TOKENS,
                 context_window: int = 128000,
                 context_source: str = "configuration",
                 context_default: int = 0,
                 input_override: int = 0,
                 system_prompt: Optional[str] = None,
                 tool_names: Optional[List[str]] = None,
                 agent_name: str = "",
                 registry: Optional[AgentRegistry] = None,
                 project: Any = None,
                 instructions: Optional[Sequence[Path]] = None,
                 warnings: Optional[Sequence[str]] = None,
                 abort_event: Optional[threading.Event] = None):
        self.provider = provider
        self.model = model
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.context_window = context_window
        self.context_source = context_source
        # What the configuration alone would give: context_limit's fallback
        # when a later set_model lands on a model without a known profile.
        self._context_default = context_default or context_window
        # What one prompt may be. `context` is input plus output; requests
        # are refused on the input share, so compaction budgets against this
        # and not the window (issue #5). An `input` figure in the provider
        # profile is the user's own word and outranks the provider's.
        self._input_override = max(0, int(input_override or 0))
        self.input_window, self.input_source = self._derive_input_limit()
        # Local estimate vs what the provider last reported for this
        # conversation: >1 means the estimator runs low here. Applied to the
        # compaction trigger so it fires on the model's arithmetic, not ours.
        self.token_scale = 1.0
        self.messages: List[Msg] = []
        self.steps_used = 0
        self.cost = 0.0
        self.tokens = {"input": 0, "output": 0}
        # monotonic() of the last streamed event, for stream-health display.
        self.last_event_at = 0.0
        self.usage = UsageTracker()
        # Prompts typed while a turn is running; folded in at the next step.
        self._steering: List[str] = []
        self._steer_lock = threading.Lock()

        self.cwd = cwd
        # What the project config left enabled. Every agent filters this list
        # rather than the whole registry, so switching back to build cannot
        # resurrect a tool the project turned off.
        self.base_tool_names: List[str] = (list(tool_names) if tool_names is not None
                                           else list(REGISTRY))
        self._base_max_steps = max_steps
        self._base_model = model
        self._agent_model_applied = False

        self.permissions = permissions or Permissions()
        self._base_permissions = self.permissions

        # `abort_event` lets a builder create the handle before the provider
        # and give both halves the same object; without one the agent makes its
        # own. Either way this agent may clear it when a top-level run starts,
        # so a sub-agent inherits by adopting its parent's context flag
        # instead (see ToolContext.aborted).
        self.ctx = ToolContext(cwd=cwd, permissions=self.permissions,
                               abort=abort_event)
        self.ctx.agent = self

        self.context = ContextManager(cwd)
        self.memory = MemoryStore(cwd)
        self.project = project
        self.instruction_paths: List[Path] = [Path(p) for p in (instructions or [])]
        self.warnings: List[str] = list(warnings or [])

        # None means "derive from the model family and the agent"; a string is
        # a caller's explicit override (the task tool hands its sub-agent one).
        self.system_prompt = system_prompt
        self._prompt_cache: Optional[Any] = None
        # Bumped whenever a memory is written, so the next request sees it.
        self._memory_epoch = 0
        # Mode-change reminders, folded into the next user message.
        self._pending_reminders: List[str] = []

        self._registry = registry
        # Set by runtime.build_agent: builds a sibling provider by profile
        # name, for subagent definitions that pin a different provider's
        # model. None in bare/test construction — the task tool fails loudly
        # rather than silently reviewing on the wrong model.
        self.provider_factory = None
        self.agent_name = agent_name or (registry.default().name if registry
                                         else DEFAULT_AGENT)
        # No registry and no named agent is the sub-agent case (the task tool):
        # it inherits its tools and permissions from its parent, so loading the
        # agent table per sub-agent would be paid for nothing.
        self._apply_agent(None if registry is None and not agent_name
                          else self.registry.get(self.agent_name))

    # --- agents ---------------------------------------------------------

    @property
    def registry(self) -> AgentRegistry:
        """The agent table, loaded on first use.

        Lazy because loading scans two directories: the sub-agents the task
        tool spawns never look at it, and paying for the scan per sub-agent
        would be a per-tool-call cost.
        """
        if self._registry is None:
            project = getattr(self.project, "data", None)
            self._registry = AgentRegistry.load(self.cwd, project)
            self.warnings.extend(self._registry.warnings)
        return self._registry

    def _apply_agent(self, defn: Optional[AgentDef]) -> None:
        """Point tools, permissions, prompt and limits at `defn`.

        Never touches self.messages: an agent switch changes what the model may
        do next, not what it already did, and rewriting the history would break
        the tool_call/tool pairing the providers validate.
        """
        self.agent_def = defn
        names = self.base_tool_names
        if defn is not None:
            names = AgentRegistry.resolve_tools(defn, names, _permission_keys())
        # plan_exit is opt-in by name: it exists to end plan mode, and an
        # agent that merely inherits "everything" (build, a bare Agent, a
        # custom agent with no tools list) must not be offered a plan to
        # approve. Only an agent whose own tool list names it gets it.
        if defn is None or not defn.tools or "plan_exit" not in [
                str(name).lower() for name in defn.tools]:
            names = [name for name in names if name != "plan_exit"]
        self.tools = get_tools(names)
        self._merge_mcp_tools()
        self.specs = tool_specs(self.tools)

        self.permissions = self._permissions_for(defn)
        self.ctx.permissions = self.permissions

        self.max_steps = (defn.steps if defn is not None and defn.steps
                          else self._base_max_steps)
        if defn is not None:
            self._apply_agent_model(defn)
        self._prompt_cache = None

    def _apply_agent_model(self, defn: AgentDef) -> None:
        """Honour an agent's `model:` field where this session can.

        A bare model id is swapped in directly. A provider-qualified id for a
        different provider needs a new client, which only the front-end can
        build, so it becomes a warning instead of a silent no-op. Leaving an
        agent that named a model restores the session's own model; an agent
        that never named one must not touch it, or a front-end that assigned
        `agent.model` directly would see its choice reverted by a switch.
        """
        provider, model = defn.model_parts()
        if not model:
            if self._agent_model_applied:
                self.model = self._base_model
                self._agent_model_applied = False
            return
        current = str(getattr(self.provider, "name", "") or "")
        if provider and provider != current:
            note = (f"agent '{defn.name}' wants {provider}/{model}; "
                    f"switch provider with /provider {provider} to use it")
            if note not in self.warnings:
                self.warnings.append(note)
            return
        self.model = model
        self._agent_model_applied = True

    def _permissions_for(self, defn: Optional[AgentDef]) -> Permissions:
        """A Permissions carrying this agent's overlay.

        An agent with no rules of its own gets the caller's own object back,
        so "always" grants and a front-end's asker survive; an agent that does
        have rules gets a fresh object over an AgentPermissions rather than
        over the real Config, because persist() writing plan mode's denies
        into the user's config file would outlive the session that asked.
        """
        base = self._base_permissions
        current = getattr(self, "permissions", None) or base
        # A front-end wires its asker onto whatever object was live when it
        # looked (TUI._wire_permissions assigns agent.permissions.asker). When
        # the session *started* on an agent that carries its own rules -- which
        # is what `default_agent: plan` or `-a plan` produces -- that object is
        # an overlay, the base never learns about the asker, and the next
        # switch builds an overlay with asker=None. Every ASK from then on
        # fails with "no interactive session is available" even though the user
        # is sitting right there. The asker is a capability of the front-end,
        # not of the agent, so it belongs on the base.
        if current.asker is not None and base.asker is not current.asker:
            base.asker = current.asker
        if defn is None or not defn.permission:
            target = base
        else:
            target = Permissions(config=AgentPermissions(defn, base.config),
                                 asker=base.asker,
                                 auto_approve=base.auto_approve,
                                 yolo=base.yolo)
        if target is not current:
            self._carry_grants(current, target, defn)
        return target

    @staticmethod
    def _carry_grants(source: Permissions, target: Permissions,
                      defn: Optional[AgentDef]) -> None:
        """Move session "always" grants across an agent switch.

        Grants for a key the new agent denies are dropped, so a bash grant made
        in build mode cannot follow the session into plan mode. See _denies.
        """
        existing = getattr(target, "_session_grants", {})
        for key, patterns in dict(getattr(source, "_session_grants", {})).items():
            if defn is not None and _denies(defn, key):
                continue
            fresh = [p for p in patterns if p not in existing.get(key, [])]
            if fresh:
                target.grant_always(key, fresh)

    def attach_mcp(self, manager: Any) -> None:
        """Adopt an MCPManager: its tools join this agent's set.

        Called by runtime.build_agent(). The set is re-read at every turn
        (see run()), because servers connect in the background and their
        tools should appear when they are ready rather than never.
        """
        self.ctx.mcp = manager
        self._merge_mcp_tools()
        self.specs = tool_specs(self.tools)

    def _merge_mcp_tools(self) -> None:
        """Fold the MCP manager's current offering into self.tools.

        Names are namespaced (mcp_<server>_<tool>), so a remote tool can
        never shadow a built-in; on the freak collision the built-in wins,
        because the model's trust in `read` must not be transferable to
        somebody else's server.
        """
        manager = getattr(self.ctx, "mcp", None)
        if manager is None:
            return
        stale = getattr(self, "_mcp_tool_names", set())
        for name in stale:
            self.tools.pop(name, None)
        fresh = set()
        try:
            offered = manager.agent_tools()
        except Exception:
            offered = []
        for tool in offered:
            if tool.name in self.tools:
                continue
            self.tools[tool.name] = tool
            fresh.add(tool.name)
        self._mcp_tool_names = fresh

    def _refresh_mcp_tools(self) -> None:
        """Pick up servers that finished connecting since the last turn."""
        if getattr(self.ctx, "mcp", None) is None:
            return
        before = getattr(self, "_mcp_tool_names", set())
        self._merge_mcp_tools()
        if self._mcp_tool_names != before:
            self.specs = tool_specs(self.tools)

    def switch_agent(self, name: str) -> str:
        """Swap prompt, tools and permissions mid-session.

        Returns a one-line summary for the front-end to display; the agent
        itself is on `.agent_def` afterwards. Raises KeyError for an unknown
        name, leaving the session exactly as it was.
        """
        defn = self.registry.get(name)
        if defn is None:
            raise KeyError(f"unknown agent '{name}'")
        previous = self.agent_name
        self.agent_name = defn.name
        self._apply_agent(defn)
        if previous != defn.name:
            reminder = self._mode_reminder(previous, defn.name)
            if reminder:
                self._pending_reminders.append(reminder)
        return f"agent → {defn.name}" + (" (read-only)" if is_readonly(defn) else "")

    @staticmethod
    def _mode_reminder(previous: str, current: str) -> str:
        """The synthetic reminder opencode injects around a plan-mode switch."""
        if current == PLAN_AGENT:
            return enter_plan_text()
        if previous == PLAN_AGENT:
            return exit_plan_text()
        return ""

    @property
    def plan_mode(self) -> bool:
        """True when the active agent cannot change anything."""
        if self.agent_def is None:
            return False
        return is_readonly(self.agent_def)

    def set_model(self, model: str) -> str:
        """Switch model mid-session without disturbing the history.

        The prompt variant is keyed on the model id, so this also re-selects
        the system prompt on the next request. Everything the provider keyed
        on the old model must follow: the stored reasoning effort is
        revalidated (an effort whitelisted for one model would otherwise ride
        along and fail every request on the next), the context window is
        re-derived so the meter and compaction use the new model's real
        limit, and the meter's last observed exchange is dropped.

        Returns a short user-facing note describing those side effects, or ""
        when only the id changed.
        """
        self.model = model
        self._base_model = model
        self._agent_model_applied = False
        notes = []
        effort = str(getattr(self.provider, "reasoning_effort", "") or "")
        setter = getattr(self.provider, "set_reasoning_effort", None)
        if effort and callable(setter):
            try:
                setter(effort, model)
            except ValueError:
                try:
                    self.provider.reasoning_effort = ""
                except Exception:
                    pass
                notes.append("reasoning effort '%s' cleared: not supported "
                             "by %s" % (effort, model))
        limit = getattr(self.provider, "context_limit", None)
        if callable(limit):
            try:
                window, source = limit(model, self._context_default)
            except Exception:
                window, source = self.context_window, self.context_source
            if window != self.context_window:
                notes.append("context window %sk (%s)"
                             % (window // 1000, source))
            self.context_window, self.context_source = window, source
        # The input share follows the model just as the window does; a new
        # conversation mix also means the old estimator correction is stale.
        self.input_window, self.input_source = self._derive_input_limit()
        self.token_scale = 1.0
        self.usage.invalidate_latest()
        return "; ".join(notes)

    def _derive_input_limit(self) -> Tuple[int, str]:
        """What one prompt may be: profile override, provider, or window."""
        if self._input_override:
            return (min(self._input_override, self.context_window),
                    "configured input limit")
        limit = getattr(self.provider, "input_limit", None)
        if callable(limit):
            try:
                value, source = limit(self.model, self.context_window)
                value = int(value or 0)
                if 0 < value <= self.context_window:
                    return value, str(source)
            except Exception:
                pass
        return self.context_window, "context window"

    def _observe_reported_usage(self, usage: Dict[str, Any],
                                estimated: int) -> None:
        """Recalibrate the estimator against what the model just counted.

        The estimator is tuned for the average session (context.py); this
        conversation's mix of code, prose and tool output can still run it
        low or high — measured 79–88% on one real session. The provider's
        own count for the prompt we just sent is ground truth, so the ratio
        replaces guesswork. Clamped: a single absurd reading (a truncated
        payload, a provider bug) must not swing the trigger by more than the
        plausible range of estimator error.
        """
        try:
            reported = int(usage.get("input", 0)) + int(usage.get("cache_read", 0))
        except (TypeError, ValueError):
            return
        if reported <= 0 or estimated <= 0:
            return
        self.token_scale = min(2.0, max(0.5, reported / estimated))

    # --- prompt assembly ----------------------------------------------

    def _agent_prompt(self) -> str:
        """The agent-supplied prompt that replaces the model-family variant.

        A built-in's own `prompt` is a mode reminder (plan's read-only
        briefing), not a replacement system prompt — prompt.select_prompt
        already appends the plan preamble, and swapping the whole prompt out
        would drop every coding instruction with it. A user's own agent file
        overrides that text, and then it is meant literally.
        """
        if self.system_prompt is not None:
            return self.system_prompt
        defn = self.agent_def
        if defn is None or not defn.prompt:
            return ""
        canned = BUILTIN.get(defn.name)
        if canned is not None and defn.prompt == canned.prompt:
            return ""
        return defn.prompt

    def _instructions(self) -> str:
        """AGENTS.md and friends, plus whatever the project config declares."""
        try:
            return self.context.instructions(self.instruction_paths)
        except OSError:
            return ""

    def refresh_memory(self) -> None:
        """Re-read the memory index into the next system prompt.

        Called by front-ends after a memory is written outside the agent loop
        (the REPL's "#" capture), which the tool path cannot observe.
        """
        self._memory_epoch += 1

    def _memory_block(self) -> str:
        try:
            block = self.memory.context_block(limit_chars=MEMORY_CONTEXT_CHARS)
        except OSError:
            return ""
        if ("memory_read" not in self.tools
                and "memory_write" not in self.tools
                and block == "# Memory\n\nNo saved memories yet."):
            # "No saved memories yet." exists to make the feature
            # discoverable — for an agent that has no memory tools (plan,
            # subagents) it is pure noise. Saved facts still flow to them.
            return ""
        return block

    def _assemble_system_text(self) -> str:
        text = build_system_prompt(
            model=self.model,
            agent=self.agent_name,
            instructions=self._instructions(),
            environment=self.context.environment_block(),
            agent_prompt=self._agent_prompt(),
            tool_names=tuple(self.tools))
        skills = self._skills_block()
        if skills:
            text = f"{text}\n\n{skills}"
        memory = self._memory_block()
        return f"{text}\n\n{memory}" if memory else text

    def _skills_block(self) -> str:
        """The skill catalogue, when the skill tool is on offer.

        Without this the model was handed the `skill` tool and never told
        which skills exist — it could only guess a name. Bounded inside
        prompt_block(), and suppressed entirely for agents whose tool set
        excludes `skill`, so a plan agent pays nothing for it.
        """
        if "skill" not in self.tools:
            return ""
        try:
            from .skills import prompt_block
            return prompt_block(self.cwd, permissions=self.permissions)
        except Exception:
            return ""

    def _system_message(self) -> Msg:
        """The one place the system prompt is assembled.

        Cached against everything that can change it, because this runs on
        every provider round *and* every time a front-end measures the context
        meter, and it reads a handful of files each time it does not.
        """
        key = (self.model, self.agent_name, self._memory_epoch,
               self.system_prompt)
        if self._prompt_cache is not None and self._prompt_cache[0] == key:
            return Msg(role="system", content=self._prompt_cache[1])
        text = self._assemble_system_text()
        self._prompt_cache = (key, text)
        return Msg(role="system", content=text)

    def prompt_variant(self) -> str:
        """Which prompt file the current model resolves to, for /status."""
        return select_variant(self.model)

    def reasoning_efforts(self) -> Sequence[str]:
        """Effort values the live provider accepts for the current model."""
        values = self.provider.reasoning_efforts(self.model)
        return tuple(str(value) for value in values)

    @property
    def reasoning_effort(self) -> str:
        """The effort that will be sent on the next request, when supported."""
        return str(getattr(self.provider, "reasoning_effort", "") or "")

    def set_reasoning_effort(self, effort: str) -> str:
        """Change reasoning effort for subsequent rounds in this live session."""
        return self.provider.set_reasoning_effort(effort, self.model)

    def _messages_for_llm(self, reminder: Optional[str] = None,
                          on_event: Optional[Callable] = None) -> List[Msg]:
        # Paired before compaction so the token budget is computed over the
        # messages that will actually be sent.
        #
        # The provider goes in because without it compaction can only drop the
        # oldest turns and leave a note saying it did. Everything the user
        # decided early in a long session — constraints, rejected approaches,
        # what a file turned out to contain — vanished silently at exactly the
        # point the conversation got long enough to need it. With a provider it
        # writes a summary instead; if that call fails it still falls back to
        # dropping, so a summariser outage degrades rather than breaks.
        notify = None
        if on_event is not None:
            notify = lambda: on_event(  # noqa: E731 - one-shot closure
                "compaction", {"text": "compacting the conversation"})
        history = compact_history(pair_tool_messages(self.messages),
                                  self.input_window,
                                  provider=self.provider, model=self.model,
                                  scale=self.token_scale, notify=notify)
        messages = [self._system_message()] + history
        if reminder:
            messages.append(Msg(role="assistant", content=reminder))
        return messages

    # --- usage -----------------------------------------------------------

    def context_state(self) -> ContextState:
        """What the next request would cost, for the front-ends' context meter."""
        return measure_context(self)

    # --- one provider round -------------------------------------------

    def _bind_abort(self) -> None:
        """Hand the provider this run's cancellation handle.

        Providers take an `abort` (a threading.Event or a callable) and pass it
        to net's read loop, which is the only place a stalled stream can be
        interrupted — the loop below sees nothing until a chunk arrives. Bound
        per step rather than once, because a sub-agent shares its parent's
        provider and adopts the parent's event only after it is constructed.
        """
        if getattr(self.provider, "abort", None) is not self.ctx.abort_event:
            try:
                self.provider.abort = self.ctx.abort_event
            except AttributeError:      # a provider with no such slot
                pass

    _REASONING_REJECTED = ("thinking", "signature", "redacted_thinking")

    def _forget_reasoning(self, failure: "ProviderFailure") -> bool:
        """Strip stored reasoning blocks if that is what the provider refused.

        Returns whether anything was dropped, so the caller only retries on a
        failure this can actually fix. Deliberately narrow: a provider outage
        must not be mistaken for a poisoned block and cost a second request.
        """
        error = getattr(failure, "error", None) or {}
        haystack = " ".join(str(error.get(key) or "")
                            for key in ("message", "body")).lower()
        if error.get("status") != 400:
            return False
        if not any(word in haystack for word in self._REASONING_REJECTED):
            return False
        dropped = False
        for message in self.messages:
            if getattr(message, "reasoning", None):
                message.reasoning = {}
                dropped = True
        return dropped

    def _step(self, on_text: Optional[Callable], on_event: Optional[Callable],
              final_step: bool = False):
        accumulator = _CallAccumulator()
        text_parts: List[str] = []
        reasoning_blocks: List[dict] = []
        stop_reason = None

        self._bind_abort()
        self.last_event_at = time.monotonic()
        messages = self._messages_for_llm(
            MAX_STEPS_PROMPT if final_step else None, on_event=on_event)
        # What we think this prompt weighs; the response's usage says what it
        # actually weighed, and the ratio recalibrates the estimator.
        estimated_prompt = sum(message_tokens(m) for m in messages)
        specs = [] if final_step else self.specs
        stream = self.provider.stream(messages, specs,
                                      self.model, self.max_tokens)
        try:
            for chunk in stream:
                self.ctx.check_abort()
                # The stream's pulse, for the footer: a link that died
                # silently used to be indistinguishable from a model
                # thinking. Now the screen can say how long the line has
                # been quiet.
                self.last_event_at = time.monotonic()
                failure = provider_failure(chunk)
                if failure is not None:
                    # Before on_text and before the history append, both
                    # deliberately: a provider error rendered as an answer is
                    # one the model is told it wrote, and it argues with it
                    # next turn.
                    if on_event:
                        on_event("error", failure)
                    raise ProviderFailure(failure)
                if chunk.text:
                    text_parts.append(chunk.text)
                    if on_text:
                        on_text(chunk.text)
                if chunk.reasoning and on_event:
                    on_event("reasoning", chunk.reasoning)
                if chunk.reasoning_block:
                    # Opaque and never shown: the screen already had the
                    # readable copy through `reasoning`. This is the one the
                    # provider needs handed back next request.
                    reasoning_blocks.append(chunk.reasoning_block)
                if chunk.tool_call_delta:
                    accumulator.add(chunk.tool_call_delta)
                if chunk.usage:
                    # One parse for both counters: the tracker knows every
                    # spelling of a usage payload, self.tokens is the flat pair
                    # the UIs read.
                    delta = self.usage.record(chunk.usage)
                    # Cache reads count: they are tokens the model actually
                    # consumed. Excluding them made the footer plateau at the
                    # first prompt's size on a well-cached backend, which a
                    # user read — twice — as the session having stopped.
                    self.tokens["input"] += delta.input_tokens + delta.cache_read
                    self.tokens["output"] += delta.output_tokens
                    self._observe_reported_usage(chunk.usage, estimated_prompt)
                if chunk.stop_reason:
                    stop_reason = chunk.stop_reason
        finally:
            # Leaving the stream to the GC leaks the socket and net's pump
            # thread for as long as the traceback holds this frame alive, which
            # is precisely the abort case.
            closer = getattr(stream, "close", None)
            if closer is not None:
                closer()

        # A provider that returns cleanly on abort (they all do — net raises
        # Aborted and the dialects swallow it) would otherwise leave an empty
        # assistant turn behind as if the model had answered with silence.
        self.ctx.check_abort()

        text = "".join(text_parts)
        # A provider should obey the empty tool list, but a compatibility
        # endpoint may still replay a tool call. Never persist an unanswered call
        # in the handoff turn: it would poison every later request in the session.
        calls = [] if final_step else accumulator.finish()
        # Tagged with what produced them: a signature is only valid to the
        # dialect and model that issued it, so a later provider or model
        # switch can tell these are not its own to replay.
        reasoning = {"dialect": getattr(self.provider, "name", ""),
                     "model": self.model,
                     "blocks": reasoning_blocks} if reasoning_blocks else {}
        self.messages.append(Msg(role="assistant", content=text,
                                 tool_calls=calls, reasoning=reasoning))
        return text, calls, stop_reason

    # --- tool execution -------------------------------------------------

    def _authorize(self, tool: Tool, call: ToolCall) -> None:
        """Enforce the tool's declared permission before it can run.

        opencode asks inside each tool, and a tool that forgets to (todowrite
        and task both did) simply is not guarded — `todowrite: deny` in the
        config did nothing at all. The check therefore lives here, where no
        tool can skip it.

        A tool that asks for itself keeps its own prompt: it names the file,
        command or URL, which this cannot, and asking here as well would put
        the question twice. For those, only a rule that denies every pattern is
        applied here, so a denied key stays denied even if such a tool ever
        stops asking. `ToolContext.ask` remembers what this resolved, so a tool
        that asks for the same scope again is not a second prompt; asking for
        *additional* scope (external_directory, a second file) still is.
        """
        key = tool.permission
        if not key:
            return
        if prompts_for_itself(tool):
            # The context's object, not self.permissions: it is the one the
            # tool's own ask will consult, and the two must not disagree.
            if _denied_for_every_pattern(self.ctx.permissions, key):
                raise PermissionDenied(f"{key} denied by configuration")
            return
        patterns = _declared_patterns(tool, call.arguments, self.ctx)
        self.ctx.ask(key, patterns, f"Run the {tool.name} tool",
                     {"tool": tool.name, "args": call.arguments},
                     always=patterns)

    def _run_tool(self, call: ToolCall, on_event: Optional[Callable]) -> Msg:
        tool = self.tools.get(call.name)
        if tool is None:
            return Msg(role="tool", tool_call_id=call.id,
                       content=self._unknown_tool_message(call.name))

        if "__malformed__" in call.arguments:
            return Msg(role="tool", tool_call_id=call.id,
                       content=("Error: arguments were not valid JSON. "
                                "Call the tool again with a well-formed JSON object."))

        if on_event:
            on_event("tool", {"name": call.name, "args": call.arguments})

        try:
            with self.ctx.tool_call():
                self._authorize(tool, call)
                result = tool.execute(call.arguments, self.ctx)
            if call.name == "memory_write":
                # The next request must see what was just remembered.
                self._memory_epoch += 1
            # Every tool result passes through here on its way to the model,
            # the transcript and sessions.db, so this is the one place that can
            # promise a credential never reaches any of the three. Doing it
            # inside the shell tool covered `bash` alone: `read .env` and
            # `grep -r KEY` handed the key over untouched, and it was still
            # sitting in the database afterwards.
            output = redact(result.output, heuristic=False)
            message = Msg(role="tool", tool_call_id=call.id, content=output,
                          display={"tool": call.name, "title": result.title,
                                   **result.metadata})
            if on_event:
                on_event("tool_result",
                         {"name": call.name, "title": result.title,
                          "metadata": result.metadata, "output": output})
            return message
        except PermissionDenied as e:
            if on_event:
                on_event("tool_denied", {"name": call.name, "reason": str(e)})
            return Msg(role="tool", tool_call_id=call.id,
                       content=f"The user rejected this action: {e}",
                       display={"tool": call.name, "denied": True})
        except ToolAborted:
            raise
        except Exception as e:
            if on_event:
                on_event("tool_error", {"name": call.name, "error": str(e)})
            return Msg(role="tool", tool_call_id=call.id,
                       content=f"Error: {e}",
                       display={"tool": call.name, "error": str(e)})

    def _unknown_tool_message(self, name: str) -> str:
        """Say *why* a tool is missing when the active agent withheld it.

        A model in plan mode that calls `edit` and is told only "unknown tool"
        will keep trying; told that the plan agent has no edit tool, it stops.
        """
        if name in REGISTRY:
            return (f"Error: the '{name}' tool is not available to the "
                    f"'{self.agent_name}' agent in this session. Available: "
                    f"{', '.join(sorted(self.tools))}.")
        return f"Error: unknown tool '{name}'"

    # --- public API ------------------------------------------------------

    def steer(self, text: str) -> bool:
        """Add to what the model is told, without stopping what it is doing.

        A prompt typed mid-run used to wait for the whole turn to finish, and
        with no step limit a turn can run for many minutes — by which time the
        correction is about work already done. This delivers it at the next
        step instead, which is the point where the model chooses what to do
        next anyway. Returns False for empty text so callers can fall through.
        """
        if not (text or "").strip():
            return False
        with self._steer_lock:
            self._steering.append(text)
        return True

    def pending_steering(self) -> List[str]:
        """What is waiting to be handed to the model, in delivery order."""
        with self._steer_lock:
            return list(self._steering)

    def edit_steering(self, index: int, text: str) -> bool:
        """Replace one pending message; empty text drops it.

        Between typing and the next step there is a window — often minutes,
        since a step can be a long tool call — in which the user changes their
        mind about what they just typed. Without this the only options were to
        let a stale instruction through or abort the whole turn.
        """
        with self._steer_lock:
            if not 0 <= index < len(self._steering):
                return False
            if (text or "").strip():
                self._steering[index] = text
            else:
                del self._steering[index]
            return True

    def drop_steering(self, text: str) -> bool:
        """Remove one pending message by its exact text. Atomic.

        Indices are the wrong handle for this. A front end lists the queue,
        the user reads it, and somewhere in those seconds the running turn
        reaches a step boundary and drains everything — so by the time a key
        is pressed, index 0 is a different message, or gone. Matching on the
        text means the worst case is "it was already sent", which is the
        truth, rather than dropping something the user never chose.
        """
        with self._steer_lock:
            try:
                self._steering.remove(text)
            except ValueError:
                return False
            return True

    def clear_steering(self) -> int:
        """Drop everything pending. Returns how many were discarded."""
        with self._steer_lock:
            count = len(self._steering)
            self._steering = []
            return count

    def _drain_steering(self, on_event: Optional[Callable]) -> None:
        """Fold anything steered in since the last step into the history."""
        with self._steer_lock:
            pending, self._steering = self._steering, []
        for text in pending:
            self.messages.append(Msg(role="user", content=text))
            if on_event:
                on_event("steered", {"text": text})

    def run(self, user_message: str, on_text: Optional[Callable] = None,
            on_event: Optional[Callable] = None) -> str:
        """Run until the model stops calling tools. Returns the final text."""
        self.usage.start_run()
        # MCP servers connect in the background; a turn boundary is where
        # their tools join (or a stand-in is replaced by the real thing).
        self._refresh_mcp_tools()
        # A turn is the boundary at which credentials are re-checked: within
        # one, the provider may reuse what it read, so a long turn does not
        # re-read the token file once per model request.
        invalidate = getattr(self.provider, "invalidate_auth", None)
        if callable(invalidate):
            try:
                invalidate()
            except Exception:
                pass
        self.messages.append(Msg(role="user", content=self._compose(user_message)))
        # A sub-agent borrows its parent's handle; clearing it here would undo
        # the abort that is on its way to us.
        if not (self.ctx.abort_shared or getattr(self.ctx, "subagent_depth", 0)):
            self.ctx.aborted = False
        final = ""
        self.steps_used = 0

        while self.max_steps is None or self.steps_used < self.max_steps:
            self._drain_steering(on_event)
            self.steps_used += 1
            final_step = (self.max_steps is not None
                          and self.steps_used >= self.max_steps)
            try:
                try:
                    text, calls, stop_reason = self._step(
                        on_text, on_event, final_step=final_step)
                except ProviderFailure as failure:
                    # The one failure the session can repair itself. A
                    # replayed reasoning block the provider refuses lives in
                    # the history, so the same 400 comes back on every later
                    # request — the session is bricked from the inside, which
                    # is exactly the shape pair_tool_messages exists to
                    # prevent for tool calls. Drop the blocks and try once.
                    if not self._forget_reasoning(failure):
                        raise
                    if on_event:
                        on_event("info", "provider refused the stored "
                                         "reasoning blocks; dropped them and "
                                         "retried")
                    text, calls, stop_reason = self._step(
                        on_text, on_event, final_step=final_step)
            except ToolAborted:
                self.messages.append(Msg(role="user", content="[interrupted by user]"))
                return final
            except Exception:
                # Roll the turn back. A failed request used to leave the
                # user's message standing alone: the model saw the question
                # again on the next attempt, the session stored it twice, and
                # /resume replayed a conversation where the user apparently
                # asked twice and was ignored once. The error itself must not
                # become an assistant message — that would replay to the
                # provider as words the model never said (see
                # tests/test_enforcement.py ProviderErrorsAreNotAnswers) —
                # so the failed exchange leaves no trace instead, which is
                # already how partially streamed text is treated.
                if self.messages and self.messages[-1].role == "user":
                    self.messages.pop()
                raise

            if text:
                final = text
            if final_step:
                if on_event:
                    on_event("limit", {"steps": self.steps_used,
                                       "continuable": True})
                return final
            if not calls:
                return final

            # Every tool_call must get a matching tool message, even when the
            # user interrupts: providers reject a history where an assistant
            # turn has calls that were never answered.
            aborted = False
            for call in calls:
                if aborted or self.ctx.aborted:
                    aborted = True
                    self.messages.append(Msg(role="tool", tool_call_id=call.id,
                                             content="Aborted by user."))
                    continue
                try:
                    self.messages.append(self._run_tool(call, on_event))
                except ToolAborted:
                    aborted = True
                    self.messages.append(Msg(role="tool", tool_call_id=call.id,
                                             content="Aborted by user."))
            if aborted:
                return final

        return final

    def _compose(self, user_message: str) -> str:
        """Fold any pending mode reminder into the next user turn.

        opencode delivers these as an extra part on the user message rather
        than as a message of their own, and so does this: an unanswered user
        message of pure reminder text is a history shape some providers reject.
        """
        if not self._pending_reminders:
            return user_message
        parts = self._pending_reminders + [user_message]
        self._pending_reminders = []
        return "\n\n".join(part for part in parts if part)

    @property
    def abort_event(self) -> threading.Event:
        """The one cancellation handle: tools, the provider, and sub-agents.

        Read through the context rather than stored, because a sub-agent
        replaces its own event with its parent's after construction.
        """
        return self.ctx.abort_event

    def abort(self):
        """Cancel the run — including a provider stalled waiting for bytes."""
        self.ctx.aborted = True

    def clear(self):
        self.messages = []
        self.ctx.read_files.clear()
        self.ctx.todos = []
        self.usage.reset()
        self._pending_reminders = []
        self._prompt_cache = None
