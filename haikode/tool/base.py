"""
Tool base types. Mirrors opencode's tool contract: every tool has a name,
a description (the model reads it), a JSON-Schema parameter spec, and an
execute() that returns a title + output + metadata.

Two contracts live here that the agent depends on:

  * `Tool.permission` names the key the agent enforces *before* dispatch, and
    `Tool.permission_patterns` says what identifies the specific action. A
    tool may also ask for itself — see `prompts_for_itself` — in which case
    the agent leaves the prompt to it and enforces only what holds for every
    pattern.
  * Cancellation is a `threading.Event` reached through `ToolContext.aborted`.
    The bool spelling still works, and assigning one context's flag to another
    shares the event by reference, which is how a sub-agent inherits it.
"""

import inspect
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..permission import PermissionRequest
from ..schema import ToolAborted


@dataclass
class ToolResult:
    title: str                                  # short label for the UI
    output: str                                 # what the model sees
    metadata: Dict[str, Any] = field(default_factory=dict)


class _AbortFlag:
    """A boolean that also carries the Event it was read from.

    `ToolContext.aborted` used to be a plain bool, and the task tool inherits
    cancellation with a single assignment (`sub.ctx.aborted = ctx.aborted`).
    Handing back this flag makes that assignment share the *event* by
    reference, so an abort raised on the parent reaches a sub-agent that is
    already streaming, while `if ctx.aborted:` reads exactly as before.
    """

    def __init__(self, event: threading.Event):
        self.event = event

    def __bool__(self) -> bool:
        return self.event.is_set()

    def __eq__(self, other) -> bool:
        if isinstance(other, _AbortFlag):
            return bool(self) == bool(other)
        if isinstance(other, (bool, int)):
            return bool(self) == bool(other)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(bool(self))

    def __repr__(self) -> str:
        return repr(bool(self))


class ToolContext:
    """Per-run state shared by all tools."""

    def __init__(self, cwd: str = ".", permissions=None, session=None,
                 on_progress=None, abort: Optional[threading.Event] = None):
        self.cwd = str(Path(cwd).resolve())
        self.permissions = permissions
        self.session = session
        self.on_progress = on_progress or (lambda text: None)
        # One handle for the whole run: tools poll it, the provider waits on
        # it, sub-agents adopt it. A bool could only ever be polled.
        self._abort = abort if isinstance(abort, threading.Event) else threading.Event()
        # True once this context adopted somebody else's event. A borrower
        # must never clear a cancellation it does not own — a sub-agent
        # starting a run would otherwise undo the abort that is chasing it.
        self._abort_shared = False
        # Permission requests already resolved inside the current tool call,
        # or None outside one. See ask().
        self._resolved: Optional[set] = None
        # Files the model has read this session — edit/write require this
        # (same guard opencode uses to stop blind overwrites).
        self.read_files: set = set()
        # Files touched this run, for session revert (phase 6).
        self.modified_files: Dict[str, Optional[str]] = {}
        self.todos: List[Dict[str, str]] = []
        # Live activity counters the footer reads: running subagents and
        # shells. Shared by reference into subagent contexts (task.py), so
        # the root context sees the whole tree's activity.
        self.activity: Dict[str, int] = {"agents": 0, "shells": 0}
        # When each counter last rose from zero, so the footer can say how
        # long the work has been running — a 15-minute compile with no
        # provider stream open reads as a dead session without it.
        self.activity_since: Dict[str, float] = {}
        self.activity_lock = threading.Lock()

    def bump_activity(self, key: str, delta: int) -> None:
        """Adjust one live counter; never below zero, never raising."""
        try:
            with self.activity_lock:
                before = self.activity.get(key, 0)
                now = max(0, before + delta)
                self.activity[key] = now
                if now > 0 and before <= 0:
                    self.activity_since[key] = time.monotonic()
                elif now <= 0:
                    self.activity_since.pop(key, None)
        except Exception:
            pass

    # --- cancellation ---------------------------------------------------

    @property
    def abort_event(self) -> threading.Event:
        """The cancellation handle, to hand to providers and sub-agents."""
        return self._abort

    @property
    def abort_shared(self) -> bool:
        """True when this context borrowed its event from another context."""
        return self._abort_shared

    @property
    def aborted(self) -> _AbortFlag:
        return _AbortFlag(self._abort)

    @aborted.setter
    def aborted(self, value: Any) -> None:
        """Set or clear the flag, or adopt another context's event.

        A bool is the historical spelling and keeps working. A flag (or a bare
        Event) is shared by reference instead, so `sub.ctx.aborted =
        ctx.aborted` gives the sub-agent the parent's cancellation rather than
        a snapshot of it.
        """
        if isinstance(value, _AbortFlag):
            self._abort, self._abort_shared = value.event, True
            return
        if isinstance(value, threading.Event):
            self._abort, self._abort_shared = value, True
            return
        if value:
            self._abort.set()
        else:
            self._abort.clear()

    def check_abort(self):
        if self._abort.is_set():
            raise ToolAborted("aborted by user")

    # --- permissions ----------------------------------------------------

    @contextmanager
    def tool_call(self) -> Iterator[None]:
        """Scope one tool call, so a permission is asked at most once in it.

        Opened by the agent around the pre-dispatch check and execute(). Left
        closed when a tool is driven directly (tests, other front-ends), where
        every ask is a fresh one.
        """
        previous = self._resolved
        self._resolved = set()
        try:
            yield
        finally:
            self._resolved = previous

    def ask(self, key: str, patterns: List[str], title: str,
            metadata: Optional[Dict] = None, always: Optional[List[str]] = None):
        """Put one permission request to the user, once per tool call.

        The agent enforces a tool's declared permission before dispatch, so a
        tool that asks for the same thing again would prompt twice. The memo is
        keyed on the patterns as well as the key: a *narrower* request (a path
        outside the working directory, a second file) is a different action and
        is still put to the user.
        """
        if self.permissions is None:
            return
        identity = (key, tuple(str(p) for p in (patterns or ["*"])))
        if self._resolved is not None and identity in self._resolved:
            return
        self.permissions.ask(
            PermissionRequest(key, patterns, title, metadata, always=always))
        if self._resolved is not None:
            self._resolved.add(identity)

    def resolve(self, path: str) -> Path:
        """
        Absolute, fully normalised path relative to the session cwd.

        Normalising matters: read/edit guards and revert snapshots key off this
        string, so /var/... and /private/var/... (or a/../b) must not produce
        two different keys for the same file.
        """
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            p = Path(self.cwd) / p
        try:
            return p.resolve()
        except OSError:
            return Path(os.path.normpath(str(p)))

    def relative(self, path) -> str:
        try:
            return str(Path(path).relative_to(self.cwd))
        except ValueError:
            return str(path)

    def record_original(self, path: Path):
        """Remember a file's pre-edit content once, so a run can be reverted."""
        key = str(path)
        if key in self.modified_files:
            return
        try:
            self.modified_files[key] = path.read_text(errors="replace") if path.exists() else None
        except OSError:
            self.modified_files[key] = None


class Tool:
    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {}
    permission: str = ""          # permission key, "" = never asks

    #: Does execute() put `permission` to the user itself, naming the concrete
    #: action? None means "work it out from the source" (prompts_for_itself).
    #: Setting it explicitly is preferred; the detection only exists so that a
    #: tool cannot lose its prompt by staying silent.
    asks_own_permission: Optional[bool] = None

    def permission_patterns(self, args: Dict[str, Any],
                            ctx: ToolContext) -> Sequence[str]:
        """What identifies this specific action, for the pre-dispatch check.

        The default is the tool's own name and deliberately not "*": patterns
        are what an "always" answer is remembered as, and a "*" grant is a
        standing authorisation for every future call under that key. A tool
        whose calls differ in scope — a path, a command, a URL — overrides this
        so the rules in `permission.<key>` and any grant apply to the action
        the user actually saw.
        """
        return [self.name or self.permission or "*"]

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise NotImplementedError


# Matches `ctx.ask("bash", ...)` and friends: the key a tool asks under.
_ASK_CALL = re.compile(r"""\.ask\(\s*['"]([A-Za-z_][A-Za-z_0-9]*)['"]""")
_SELF_ASK_CACHE: Dict[Tuple[type, str], bool] = {}


def prompts_for_itself(tool: Tool) -> bool:
    """True when this tool asks the user about its own permission key.

    The agent enforces every declared permission before dispatch. Prompting
    centrally for a tool that already asks would put the question twice, and
    the central one is the worse of the two: it names the tool rather than the
    file, command or URL, and an "always" answer to it would authorise every
    later call. `Tool.asks_own_permission` states the answer outright; where it
    is unset the tool's own source is read for an `ask("<key>"` call. That
    detection can only ever add a prompt, never remove one — a source that
    cannot be read, or one that asks through a helper, answers False and the
    agent asks.
    """
    declared = getattr(tool, "asks_own_permission", None)
    if declared is not None:
        return bool(declared)
    key = tool.permission
    if not key:
        return False
    cache_key = (type(tool), key)
    if cache_key not in _SELF_ASK_CACHE:
        try:
            source = inspect.getsource(type(tool))
        except (OSError, TypeError):
            source = ""
        _SELF_ASK_CACHE[cache_key] = key in set(_ASK_CALL.findall(source))
    return _SELF_ASK_CACHE[cache_key]


def load_prompt(filename: str) -> str:
    """Read a tool description shipped alongside the code."""
    path = Path(__file__).parent / "prompts" / filename
    try:
        return path.read_text().strip()
    except OSError:
        return ""
