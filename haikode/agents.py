"""
Named agents and plan mode.

Ports opencode's agent system (packages/opencode/src/agent/agent.ts) plus the
markdown-with-frontmatter convention shared by opencode's config/agent.ts and
Claude Code's `.claude/agents/*.md`:

- three built-ins: `build` (the default), `plan` (read-only) and `general`
  (a search subagent), each with its own permission ruleset
- custom agents from `.haikode/agent/*.md` (project) and the global config dir,
  project winning on a name collision
- an `agents` block in the project config, merged on top of both

An agent restricts the model in two independent dimensions: the tool list it
gets to see, and the permission ruleset that guards every call. Plan mode uses
both, because hiding a tool only discourages the model while a permission deny
actually stops it.

This module never imports the tool registry or the TUI: the caller passes the
tool names in and gets a filtered list back, so wiring it into the agent loop is

    tools = get_tools(AgentRegistry.resolve_tools(
        agent, list(REGISTRY), {name: t.permission for name, t in REGISTRY.items()}))
    permissions = Permissions(AgentPermissions(agent, config), asker=...)

AgentPermissions rather than a mutated Config: the agent's rules are scoped to
the session, and assigning them into the real config would let persist() write
plan mode's denies into the user's config file permanently.
"""

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .context import global_config_dir
from .permission import ALLOW, ASK, DENY

# opencode scans {agent,agents}/**/*.md; accept both spellings, like commands.
AGENT_DIRS = ("agent", "agents")
MODES = ("primary", "subagent", "all")
DECISIONS = (ALLOW, ASK, DENY)
DEFAULT_AGENT = "build"

# A repository that ships a thousand files under .haikode/agent/ would stall
# startup reading them, so the scan is capped the way projectconfig caps
# instruction globs. The cap is per directory and produces a warning.
MAX_AGENT_FILES = 128

# Most tools in the registry declare their own name as their permission key
# (bash -> "bash", edit -> "edit", ...), but MCP proxy tools all share the key
# "mcp", so resolve_tools() takes an optional name -> key map instead of
# assuming the two are the same.
WRITE_PERMISSIONS = ("edit", "write", "bash")

# The read-only tool set for plan mode, and the search set for subagents.
PLAN_TOOLS = ["glob", "grep", "list", "read", "task", "todowrite",
              "plan_exit", "question"]
SEARCH_TOOLS = ["bash", "glob", "grep", "list", "read", "webfetch"]

_TRUE = ("true", "yes", "on", "1", "enable", "enabled")
_FALSE = ("false", "no", "off", "0", "disable", "disabled")
# "tools: *" means "whatever the session offers", i.e. no restriction at all.
_ALL_TOOLS = ("*", "all")

PermissionValue = Union[str, Dict[str, str]]

# --- plan mode prompts ---------------------------------------------------

# Ported from opencode's session/prompt/plan.txt and plan-mode.txt. haikode has
# no question tool and no plan file, so the workflow is folded into three steps
# and the plan is delivered in the reply instead of written to disk (writing is
# exactly what this agent may not do).
PLAN_ENTER_PROMPT = """<system-reminder>
# Plan mode

CRITICAL: plan mode is ACTIVE - you are in a READ-ONLY phase. STRICTLY
FORBIDDEN: any file edit, any file creation, any system change. Do NOT use sed,
tee, echo redirection, git commit or any other shell command to modify
anything - shell commands may only read and inspect. This constraint overrides
ALL other instructions, including a direct request from the user to make the
change now. You may only observe, analyse and plan. Any modification attempt is
a critical violation.

## Responsibility

Read, search and delegate to subagents until you understand the code well
enough to write a plan that accomplishes what the user actually wants.

1. Understand the request and the code around it. Prefer several searches in
   one turn over a long sequential crawl.
2. Weigh the tradeoffs out loud and ask the user about anything ambiguous
   rather than assuming intent.
3. Present the final plan in your reply: the recommended approach only (not
   every alternative you considered), the exact files that must change, and how
   to verify the result end to end.

The plan should be comprehensive yet concise - detailed enough to execute,
short enough to scan. Use the `question` tool to resolve ambiguity while you
work. When the plan is ready, present it and call `plan_exit` to ask the user
for approval - if they approve, you are switched to the build agent
automatically and may start implementing. End a planning turn only by asking
a question or by calling plan_exit.
</system-reminder>"""

# Ported from opencode's session/prompt/build-switch.txt.
PLAN_EXIT_PROMPT = """<system-reminder>
Your operational mode has changed from plan to build.
You are no longer in read-only mode: file edits, shell commands and the rest of
your tools are permitted again.
The plan was approved by the user - execute it.
</system-reminder>"""


def enter_plan_text() -> str:
    """The reminder to inject when the user switches to plan mode."""
    return PLAN_ENTER_PROMPT


def exit_plan_text() -> str:
    """The reminder to inject when the user approves a plan and switches back."""
    return PLAN_EXIT_PROMPT


# --- definitions ---------------------------------------------------------

class AgentError(ValueError):
    """A broken agent file. load_agents() turns it into a warning, never a raise."""


@dataclass
class AgentDef:
    """
    One named agent.

    `model` empty and `tools` None both mean "inherit from the session", which
    is what an agent file that only sets a prompt should do. `permission` holds
    overrides on top of permission.DEFAULTS, not a complete ruleset.
    """

    name: str
    description: str = ""
    prompt: str = ""
    model: str = ""
    tools: Optional[List[str]] = None
    permission: Dict[str, PermissionValue] = field(default_factory=dict)
    # Markdown agents that do not declare a mode are usable both ways, matching
    # opencode's default for config-declared agents.
    mode: str = "all"
    builtin: bool = False
    # opencode's `steps`: the agentic iteration cap for this agent. None means
    # "use the session default".
    steps: Optional[int] = None
    # `disable: true` in a file or config block removes the agent entirely.
    disable: bool = False
    # Permission keys a built-in refuses to have relaxed. See _reassert_locks:
    # agent files and config blocks arrive with a checked-out repository, and
    # plan mode's read-only promise has to survive them.
    locked: Tuple[str, ...] = ()

    def copy(self) -> "AgentDef":
        return replace(self,
                       tools=None if self.tools is None else list(self.tools),
                       permission=_copy_permission(self.permission))

    def model_parts(self) -> Tuple[str, str]:
        """`anthropic/claude-x` -> ("anthropic", "claude-x"); a bare id -> ("", id)."""
        head, separator, tail = self.model.partition("/")
        if separator and tail:
            return head.strip(), tail.strip()
        return "", self.model.strip()


def _copy_permission(rules: Dict[str, PermissionValue]) -> Dict[str, PermissionValue]:
    return {key: (dict(value) if isinstance(value, dict) else value)
            for key, value in rules.items()}


BUILTIN: Dict[str, AgentDef] = {
    "build": AgentDef(
        name="build",
        description="The default agent. Full tool access under the configured permissions.",
        mode="primary",
        builtin=True,
    ),
    "plan": AgentDef(
        name="plan",
        description="Read-only planning agent. Researches and proposes, never edits.",
        prompt=PLAN_ENTER_PROMPT,
        tools=list(PLAN_TOOLS),
        # Belt and braces: the tools are gone from the schema *and* denied, so a
        # model that hallucinates a call still cannot touch the disk.
        permission={"edit": DENY, "write": DENY, "bash": DENY, "webfetch": ASK},
        mode="primary",
        builtin=True,
        locked=("edit", "write", "bash"),
    ),
    "general": AgentDef(
        name="general",
        description=("General-purpose agent for researching complex questions and "
                     "searching the codebase. Use it to run open-ended exploration "
                     "without spending the main context on it."),
        tools=list(SEARCH_TOOLS),
        # No task tool: a subagent spawning subagents nests without bound. Todos
        # belong to the parent session, so they are denied here as in opencode.
        permission={"task": DENY, "todowrite": DENY},
        mode="subagent",
        builtin=True,
        locked=("task",),
    ),
    "explore": AgentDef(
        name="explore",
        description=("Read-only search agent: locate files, symbols and "
                     "patterns, and report where things are. It reads, it "
                     "never runs or edits — use it to fan out codebase "
                     "exploration cheaply. Plan mode's prompt depends on it."),
        prompt=("You are an explore agent: a read-only scout. Find what was "
                "asked for — files, definitions, call sites, conventions — "
                "and report locations (path:line) with just enough excerpt "
                "to prove each finding. Do not propose changes; do not "
                "speculate about code you did not read. Your reply goes to "
                "another agent, so keep it dense and structured."),
        tools=["glob", "grep", "list", "read"],
        # Locked read-only in both dimensions: plan mode's own prompt sends
        # work here, and a planning phase must not mutate anything — not
        # even via bash, which the general subagent is allowed.
        permission={"edit": DENY, "write": DENY, "bash": DENY,
                    "task": DENY, "todowrite": DENY, "webfetch": DENY},
        mode="subagent",
        builtin=True,
        locked=("edit", "write", "bash", "task"),
    ),
}


def is_readonly(agent: AgentDef) -> bool:
    """
    True when the agent cannot change anything, in both dimensions.

    A write permission counts as neutralised when the tool is absent from the
    agent's tool list or the permission is a flat deny; a pattern map that
    allows even one pattern is not read-only.
    """
    for key in WRITE_PERMISSIONS:
        if _is_deny(_rule(agent, key)):
            continue
        if not _in_tool_list(agent, key):
            continue
        return False
    return True


def _in_tool_list(agent: AgentDef, name: str) -> bool:
    """
    Is `name` visible to this agent?

    Matching is case insensitive because Claude Code's agent files spell tools
    `Read, Grep, Bash` while every haikode tool name is lower case; a
    case-sensitive test would hand such an agent an empty tool list, or worse,
    call it read-only because "edit" != "Edit".
    """
    if agent.tools is None:
        return True
    lowered = name.lower()
    return any(entry == name or entry.lower() == lowered for entry in agent.tools)


def _rule(agent: AgentDef, key: str) -> Optional[PermissionValue]:
    """Permission rule for a key, tolerating case differences in the key."""
    if key in agent.permission:
        return agent.permission[key]
    lowered = key.lower()
    for name, value in agent.permission.items():
        if name.lower() == lowered:
            return value
    return None


def _is_deny(value: Optional[PermissionValue]) -> bool:
    if isinstance(value, str):
        return value == DENY
    if isinstance(value, dict) and value:
        return all(decision == DENY for decision in value.values())
    return False


# --- frontmatter ---------------------------------------------------------

def parse_agent_frontmatter(text: str) -> Tuple[Dict[str, object], str]:
    """
    Split `---` frontmatter from the markdown body.

    commands.parse_frontmatter is not reused here: it understands flat
    "key: value" lines only, so the nested blocks agent files need

        permission:
          edit: deny

    would flatten into top-level "edit" keys and silently lose the grouping.
    This parser understands one level of nesting plus list items, and raises
    AgentError on anything it cannot make sense of so the caller can warn.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise AgentError("missing --- frontmatter block")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        raise AgentError("unterminated --- frontmatter block")

    data: Dict[str, object] = {}
    current: Optional[str] = None
    for raw in lines[1:end]:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw[:1].isspace():
            if current is None:
                raise AgentError("indented line before any key: %r" % stripped)
            _add_nested(data, current, stripped)
            continue
        key, separator, value = stripped.partition(":")
        key = key.strip().lower()
        if not separator or not key:
            raise AgentError("not a 'key: value' line: %r" % stripped)
        current = key
        value = value.strip()
        # An empty value opens a block; the following indented lines decide
        # whether it becomes a list or a map.
        data[key] = None if not value else _scalar(value)
    # A block nobody filled in is an empty value, not a parse error.
    for key, value in list(data.items()):
        if value is None:
            data[key] = ""
    return data, "\n".join(lines[end + 1:])


def _add_nested(data: Dict[str, object], key: str, stripped: str) -> None:
    if stripped.startswith("- "):
        item = _unquote(stripped[2:].strip())
        if data.get(key) is None:
            data[key] = []
        if not isinstance(data[key], list):
            raise AgentError("list item under non-list key %r" % key)
        if item:
            data[key].append(item)
        return
    name, separator, value = stripped.partition(":")
    if not separator:
        raise AgentError("indented line is neither a list item nor a mapping: %r"
                         % stripped)
    name = name.strip().lower()
    if not name:
        raise AgentError("empty key in block %r" % key)
    if data.get(key) is None:
        data[key] = {}
    if not isinstance(data[key], dict):
        raise AgentError("mapping under non-mapping key %r" % key)
    data[key][name] = _unquote(value.strip())


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _scalar(value: str) -> object:
    """Inline lists become lists, everything else stays a string."""
    if value.startswith("[") and value.endswith("]"):
        return [_unquote(item.strip()) for item in value[1:-1].split(",")
                if item.strip()]
    return _unquote(value)


# --- normalisation -------------------------------------------------------

def _as_text(key: str, value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    raise AgentError("%s must be text" % key)


def _as_bool(value: object) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    return None


def _normalize_tools(value: object
                     ) -> Tuple[bool, Optional[List[str]], Dict[str, str]]:
    """
    Accept every spelling of `tools`, as (sets_allowlist, names, permissions).

    A comma-separated string or a list is an allowlist. A map of
    `name: true|false` is opencode's deprecated form, which config/agent.ts
    rewrites into nothing *but* permissions - so it must not touch the
    allowlist, otherwise `{read: true, write: false}` would quietly strip grep,
    glob and bash from an agent that only meant to turn writing off.
    """
    if isinstance(value, dict):
        permission = {}
        for name, raw in value.items():
            flag = _as_bool(raw)
            if flag is None:
                raise AgentError("tools.%s must be true or false" % name)
            permission[str(name).strip().lower()] = ALLOW if flag else DENY
        return False, None, permission
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False, None, {}
        if text.lower() in _ALL_TOOLS:
            return True, None, {}
        value = [item.strip() for item in text.replace("\n", ",").split(",")]
    if isinstance(value, (list, tuple)):
        names = [str(item).strip() for item in value if str(item).strip()]
        if not names or all(name in _ALL_TOOLS for name in names):
            return True, None, {}
        return True, names, {}
    raise AgentError("tools must be a list, a comma separated string or a map")


def _normalize_permission(value: object) -> Dict[str, PermissionValue]:
    """`key: decision` lines, or `key: {pattern: decision}` for bash-style rules."""
    if not value:
        return {}
    if not isinstance(value, dict):
        raise AgentError("permission must be a block of 'key: decision' lines")
    rules: Dict[str, PermissionValue] = {}
    for key, raw in value.items():
        name = str(key).strip().lower()
        if not name:
            raise AgentError("permission entry without a key")
        if isinstance(raw, dict):
            patterns = {}
            for pattern, decision in raw.items():
                patterns[str(pattern)] = _decision(name, decision)
            rules[name] = patterns
            continue
        rules[name] = _decision(name, raw)
    return rules


def _decision(key: str, value: object) -> str:
    flag = _as_bool(value)
    if flag is not None:
        return ALLOW if flag else DENY
    decision = str(value).strip().lower()
    if decision not in DECISIONS:
        raise AgentError("permission.%s must be one of %s, got %r"
                         % (key, "/".join(DECISIONS), value))
    return decision


def _apply(base: AgentDef, data: Dict[str, object], prompt: str = "") -> AgentDef:
    """
    Overlay the keys present in `data` onto `base`.

    Absent keys are inherited, which is what lets a project file add a prompt to
    a built-in agent without also re-stating its permissions.
    """
    defn = base.copy()
    if "name" in data:
        name = _as_text("name", data["name"]).strip()
        if name:
            defn.name = name
    if "description" in data:
        defn.description = _as_text("description", data["description"]).strip()
    if "model" in data:
        defn.model = _as_text("model", data["model"]).strip()
    if "mode" in data:
        mode = _as_text("mode", data["mode"]).strip().lower()
        if mode and mode not in MODES:
            raise AgentError("mode must be one of %s, got %r"
                             % ("/".join(MODES), data["mode"]))
        if mode:
            defn.mode = mode
    if "tools" in data:
        sets_allowlist, tools, from_tools = _normalize_tools(data["tools"])
        if sets_allowlist:
            defn.tools = tools
        defn.permission = _merge_permission(defn.permission, from_tools)
    if "permission" in data:
        defn.permission = _merge_permission(
            defn.permission, _normalize_permission(data["permission"]))
    for key in ("steps", "max_steps", "maxsteps"):
        if key in data:
            defn.steps = _as_steps(key, data[key])
    if _disabled(data):
        defn.disable = True
    body = prompt.strip()
    if not body and "prompt" in data:
        body = _as_text("prompt", data["prompt"]).strip()
    if body:
        defn.prompt = body
    return _reassert_locks(base, defn)


def _as_steps(key: str, value: object) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        steps = int(str(value).strip())
    except (TypeError, ValueError):
        raise AgentError("%s must be a whole number" % key)
    if steps <= 0:
        raise AgentError("%s must be greater than zero" % key)
    return steps


def _reassert_locks(base: AgentDef, defn: AgentDef) -> AgentDef:
    """
    Restore the deny rules a built-in refuses to give up.

    Agent files and config blocks are checked out with the repository, so
    without this a hostile `.haikode/agent/plan.md` could set `edit: allow` and
    plan mode would keep telling the user it is read-only while editing files.
    Overrides may still tighten these keys, add rules for other keys, change the
    prompt, model and tool list, or define a differently named agent that is
    allowed to write.
    """
    for key in base.locked:
        rule = base.permission.get(key)
        if rule is None:
            continue
        defn.permission[key] = dict(rule) if isinstance(rule, dict) else rule
    defn.locked = tuple(base.locked)
    return defn


def _merge_permission(base: Dict[str, PermissionValue],
                      override: Dict[str, PermissionValue]
                      ) -> Dict[str, PermissionValue]:
    """Later rules win; two pattern maps for the same key merge per pattern."""
    merged = _copy_permission(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            combined = dict(current)
            combined.update(value)
            merged[key] = combined
        else:
            merged[key] = dict(value) if isinstance(value, dict) else value
    return merged


def _disabled(data: Dict[str, object]) -> bool:
    for key in ("disable", "disabled"):
        if key in data and _as_bool(data[key]) is True:
            return True
    return False


# --- loading -------------------------------------------------------------

def agent_from_markdown(text: str, name: str) -> AgentDef:
    """Parse one agent file. Raises AgentError when the file is unusable."""
    data, body = parse_agent_frontmatter(text)
    if not data:
        raise AgentError("frontmatter has no keys")
    return _apply(AgentDef(name=name), data, body)


def _entry_name(relative: Path) -> str:
    """agent/review/api.md -> "review/api", matching opencode's entry naming."""
    return relative.with_suffix("").as_posix()


def _narrow_project_permission(defn: AgentDef, path: Path,
                               warnings: List[str]) -> AgentDef:
    """Strip permission loosenings from an agent file inside an untrusted repo.

    An agent definition arrives with a checkout exactly like haikode.json does,
    and it reaches the same place: the ruleset that guards bash, edit and
    webfetch. `.haikode/agent/build.md` declaring `bash: allow` was a complete
    escape — `build` is the default agent, so `git clone && haikode` ran shell
    commands with no prompt and no warning. Project files may still tighten
    (deny), pick a model, set a prompt or drop tools; only widening is refused.
    """
    kept: Dict[str, PermissionValue] = {}
    refused: List[str] = []
    for key, value in defn.permission.items():
        if isinstance(value, str):
            if value == DENY:
                kept[key] = value
            else:
                refused.append(key)
            continue
        if isinstance(value, dict):
            denies = {pattern: decision for pattern, decision in value.items()
                      if decision == DENY}
            if denies:
                kept[key] = denies
            if len(denies) != len(value):
                refused.append(key)
    if refused:
        warnings.append(
            "%s: ignored permission %s — an agent file in an untrusted project "
            "may only tighten permissions. Run /trust to accept this project."
            % (path, ", ".join(sorted(refused))))
        defn.permission = kept
    return defn


def load_agents(cwd: str = ".", trusted: Optional[bool] = None
                ) -> Tuple[Dict[str, AgentDef], List[str]]:
    """
    Load *.md agent files, global first then project so the project wins.

    A file that names an agent the previous root already defined is folded onto
    it field by field (opencode's mergeDeep across config directories), so a
    project file that only overrides the prompt keeps the global file's model.
    Entries with `disable: true` are returned too; the registry drops them.

    Files under the global config directory are the user's own and are taken at
    face value. Files inside the project are untrusted input unless the user
    trusted that project — see `_narrow_project_permission`. `trusted` is
    resolved from the trust store when the caller does not say.

    Returns (agents, warnings). A broken file is skipped and described in the
    warnings; loading agents must never take down the session.
    """
    agents: Dict[str, AgentDef] = {}
    warnings: List[str] = []
    if trusted is None:
        try:
            from .projectconfig import is_trusted
            trusted = is_trusted(cwd)
        except Exception:
            trusted = False       # fail closed: a broken trust store is not consent
    global_root = Path(global_config_dir())
    roots = (global_root, Path(cwd) / ".haikode")
    for root in roots:
        untrusted_root = root != global_root and not trusted
        for directory in AGENT_DIRS:
            base = root / directory
            if not base.is_dir():
                continue
            try:
                files = sorted(base.rglob("*.md"))
            except OSError as err:
                warnings.append("%s: %s" % (base, err))
                continue
            if len(files) > MAX_AGENT_FILES:
                warnings.append("%s: %d agent files, only the first %d are read"
                                % (base, len(files), MAX_AGENT_FILES))
                files = files[:MAX_AGENT_FILES]
            for path in files:
                if not path.is_file():
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError as err:
                    warnings.append("%s: %s" % (path, err))
                    continue
                name = _entry_name(path.relative_to(base))
                if not name:
                    continue
                try:
                    defn = agent_from_markdown(text, name)
                except AgentError as err:
                    warnings.append("%s: %s" % (path, err))
                    continue
                if untrusted_root and defn.permission:
                    defn = _narrow_project_permission(defn, path, warnings)
                previous = agents.get(defn.name)
                agents[defn.name] = (_merge_custom(previous, defn) if previous
                                     else defn)
    return agents, warnings


def _merge_custom(base: AgentDef, override: AgentDef) -> AgentDef:
    """
    Fold a custom file onto a built-in of the same name.

    Only fields the file actually filled in replace the built-in's, so
    `plan.md` containing just a prompt keeps plan's read-only ruleset. A file
    that does not declare a mode keeps the built-in's mode - which does mean an
    explicit `mode: all` on a built-in is indistinguishable from silence.
    """
    return _reassert_locks(base, AgentDef(
        name=base.name,
        description=override.description or base.description,
        prompt=override.prompt or base.prompt,
        model=override.model or base.model,
        tools=list(override.tools) if override.tools is not None
        else (None if base.tools is None else list(base.tools)),
        permission=_merge_permission(base.permission, override.permission),
        mode=override.mode if override.mode != "all" else base.mode,
        builtin=base.builtin,
        steps=override.steps if override.steps is not None else base.steps,
        disable=override.disable or base.disable,
    ))


class AgentRegistry:
    """Built-in, file-based and config-declared agents, resolved into one table."""

    def __init__(self, agents: Optional[Dict[str, AgentDef]] = None,
                 warnings: Optional[List[str]] = None,
                 default_name: str = ""):
        self.agents: Dict[str, AgentDef] = dict(agents or {})
        self.warnings: List[str] = list(warnings or [])
        # The config's `default_agent`, honoured by default() when it resolves.
        self.default_name: str = default_name or ""

    @classmethod
    def load(cls, cwd: str = ".",
             project_config: Optional[Dict] = None) -> "AgentRegistry":
        """
        Built-ins, then custom files, then the config's `agents` block.

        `project_config` is a plain dict (the parsed opencode-style config) so
        this module does not depend on the config object's shape.
        """
        agents = {name: defn.copy() for name, defn in BUILTIN.items()}
        try:
            from .projectconfig import is_trusted
            trusted = is_trusted(cwd)
        except Exception:
            trusted = False       # fail closed: a broken trust store is not consent
        custom, warnings = load_agents(cwd, trusted=trusted)
        for name, defn in custom.items():
            base = agents.get(name)
            agents[name] = _merge_custom(base, defn) if base else defn
        for name, data in _config_agents(project_config, warnings).items():
            base = agents.get(name) or AgentDef(name=name)
            if _disabled(data):
                agents.pop(name, None)
                continue
            try:
                defn = _apply(base, data)
            except AgentError as err:
                warnings.append("config agent %s: %s" % (name, err))
                continue
            if not trusted and data.get("permission"):
                # Same escape as the .md files, one door along: the `agents`
                # block of an untrusted haikode.json reaches the same ruleset.
                # projectconfig reports these as escalations but does not strip
                # them, because it does not own agent semantics — this does.
                defn = _narrow_project_permission(
                    defn, Path(cwd) / "haikode.json", warnings)
            # The block's key is the agent's identity: honouring a `name:`
            # override here would file the agent under one name and label it
            # with another, so get(names()[i]) and primary()[i].name disagree.
            defn.name = name
            agents[name] = defn
        for name in [name for name, defn in agents.items() if defn.disable]:
            agents.pop(name)
        return cls(agents, warnings, _config_default(project_config))

    # -- lookup --

    def get(self, name: str) -> Optional[AgentDef]:
        """The agent, or None for an unknown name - never a raise."""
        if not name:
            return None
        return self.agents.get(name)

    def names(self) -> List[str]:
        """Built-ins in their canonical order first, then the rest sorted."""
        builtin = [name for name in BUILTIN if name in self.agents]
        rest = sorted(name for name in self.agents if name not in BUILTIN)
        return builtin + rest

    def primary(self) -> List[AgentDef]:
        """Agents a user can select for the main session."""
        return [self.agents[name] for name in self.names()
                if self.agents[name].mode in ("primary", "all")]

    def subagents(self) -> List[AgentDef]:
        """Agents the task tool may spawn."""
        return [self.agents[name] for name in self.names()
                if self.agents[name].mode in ("subagent", "all")]

    def default(self) -> AgentDef:
        """The config's `default_agent`, else `build`, else the first selectable."""
        for name in (self.default_name, DEFAULT_AGENT):
            agent = self.agents.get(name) if name else None
            if agent is not None and agent.mode in ("primary", "all"):
                return agent
        candidates = self.primary()
        if candidates:
            return candidates[0]
        return BUILTIN[DEFAULT_AGENT].copy()

    # -- resolution --

    @staticmethod
    def resolve_tools(agent: AgentDef, all_tool_names: Sequence[str],
                      permission_keys: Optional[Mapping[str, str]] = None
                      ) -> List[str]:
        """
        The tool names this agent may see, in the caller's order.

        Tools the agent denies outright are dropped as well: a tool that can
        only fail wastes schema tokens and invites the model to try it.
        `permission_keys` maps a tool name to the permission key it asks under,
        for the tools where the two differ (every MCP proxy tool asks under
        "mcp"); names missing from it are assumed to be their own key.
        """
        keys = permission_keys or {}
        names = [name for name in (all_tool_names or [])
                 if _in_tool_list(agent, name)]
        return [name for name in names
                if not _is_deny(_rule(agent, keys.get(name) or name))]

    @staticmethod
    def resolve_permissions(agent: AgentDef,
                            base_config_permission: Optional[Dict] = None
                            ) -> Dict[str, PermissionValue]:
        """
        The ruleset to hand permission.Permissions for this agent.

        The agent's rules win over the user's config: a deny on the plan agent
        is a safety property of that agent, and a user who wants edits should
        switch to build rather than weaken plan mode. Keys neither side mentions
        are left out so permission.DEFAULTS still applies.
        """
        base = base_config_permission if isinstance(base_config_permission, dict) else {}
        return _merge_permission(base, agent.permission)

    @staticmethod
    def resolve_subagent_permissions(parent: AgentDef, child: AgentDef,
                                     base_config_permission: Optional[Dict] = None
                                     ) -> Dict[str, PermissionValue]:
        """
        Rules for `child` spawned by `parent` - the parent's rules win.

        A subagent must never be freer than the agent that spawned it, or plan
        mode leaks: `general` may run shell commands, so plan -> task(general)
        -> `echo x > file` would walk straight around plan's bash deny. Filter
        the tool list the same way, by chaining:
        resolve_tools(child, resolve_tools(parent, all_names, keys), keys).
        """
        rules = AgentRegistry.resolve_permissions(child, base_config_permission)
        return _merge_permission(rules, parent.permission)


class AgentPermissions:
    """
    Config stand-in that hands permission.Permissions this agent's ruleset.

    Assigning the resolved rules into the real Config instead would persist
    them: Permissions.persist() writes config.data["permission"] back to disk,
    so one session in plan mode would leave `edit: deny` in the user's config
    for good. This keeps the overlay in memory and writes back only the rules
    the agent did not contribute.
    """

    def __init__(self, agent: AgentDef, config=None):
        self._config = config
        self._agent = agent
        base = getattr(config, "data", None)
        self.data: Dict[str, object] = dict(base) if isinstance(base, dict) else {}
        rules = self.data.get("permission")
        # Kept separately: where the agent replaced a rule outright the user's
        # own version is not recoverable from the overlay, and save() must not
        # drop it.
        self._user = _copy_permission(rules) if isinstance(rules, dict) else {}
        self.data["permission"] = AgentRegistry.resolve_permissions(
            agent, self._user)

    def save(self) -> bool:
        """Persist the user's rules plus anything newly granted, minus the agent's."""
        if self._config is None or not hasattr(self._config, "save"):
            return False
        rules = self.data.get("permission")
        added = _without_agent_rules(
            rules if isinstance(rules, dict) else {}, self._agent.permission)
        self._config.data["permission"] = _merge_permission(self._user, added)
        self._config.save()
        return True


def _without_agent_rules(effective: Dict[str, PermissionValue],
                         agent_rules: Dict[str, PermissionValue]
                         ) -> Dict[str, PermissionValue]:
    kept: Dict[str, PermissionValue] = {}
    for key, value in effective.items():
        rule = agent_rules.get(key)
        if rule is None:
            kept[key] = dict(value) if isinstance(value, dict) else value
            continue
        if isinstance(value, dict):
            patterns = {pattern: decision for pattern, decision in value.items()
                        if not _from_agent(rule, pattern, decision)}
            if patterns:
                kept[key] = patterns
            continue
        if value != rule:
            kept[key] = value
    return kept


def _from_agent(rule: PermissionValue, pattern: str, decision: str) -> bool:
    """Did this exact pattern/decision come from the agent rather than the user?"""
    if isinstance(rule, dict):
        return rule.get(pattern) == decision
    # A flat agent rule becomes {"*": decision} the first time persist() runs.
    return pattern == "*" and rule == decision


def _config_default(project_config: Optional[Dict]) -> str:
    if not isinstance(project_config, dict):
        return ""
    value = project_config.get("default_agent")
    return value.strip() if isinstance(value, str) else ""


def _config_agents(project_config: Optional[Dict],
                   warnings: List[str]) -> Dict[str, Dict[str, object]]:
    """The `agents` (or opencode's `agent`) block of a config dict."""
    block: Dict[str, Dict[str, object]] = {}
    if not isinstance(project_config, dict):
        return block
    for key in ("agent", "agents"):
        section = project_config.get(key)
        if section is None:
            continue
        if not isinstance(section, dict):
            warnings.append("config %s: expected a block of named agents" % key)
            continue
        for name, data in section.items():
            if not isinstance(data, dict):
                warnings.append("config %s.%s: expected a block" % (key, name))
                continue
            block[str(name)] = {**block.get(str(name), {}), **data}
    return block


__all__ = ["AgentDef", "AgentError", "AgentPermissions", "AgentRegistry",
           "BUILTIN", "DECISIONS", "DEFAULT_AGENT", "MAX_AGENT_FILES", "MODES",
           "PLAN_ENTER_PROMPT", "PLAN_EXIT_PROMPT", "PLAN_TOOLS",
           "SEARCH_TOOLS", "agent_from_markdown", "enter_plan_text",
           "exit_plan_text", "is_readonly", "load_agents",
           "parse_agent_frontmatter"]
