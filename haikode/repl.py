"""
Plain-terminal REPL — the fallback front-end when the curses TUI cannot run
(dumb terminal, tiny window, piped stdin) and the engine behind one-shot runs.

Everything the TUI does is available here too: streaming, tool activity,
permission prompts, slash commands, @-file references, sessions and undo.

It is also the command layer the TUI dispatches into (main.py hands it
`on_command`), so every command added here shows up in the TUI palette as
well. Commands delegate: this module decides what to *show*, the library
modules decide what a thing means. A command that reimplements a library is a
bug, because the TUI reaches the same library through a different door.

JSONREPL at the bottom is the same object with a different renderer: it writes
one JSON object per line instead of coloured text, which is what makes haikode
drivable from a script or another program. Both classes run the identical
TurnController turn, so a scripted run writes the same session rows, takes the
same checkpoints and obeys the same permissions as a run in the TUI.
"""

import io
import json
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config
from .permission import Permissions
from .runtime import build_agent, provider_status
from .turn import TurnController, TurnResult

# --- exit codes ----------------------------------------------------------
#
# A script must be able to tell "the model answered" from "the provider
# refused", and those two used to be the same zero. The codes are part of the
# CLI's contract: they are documented in --help and in the README, and
# main.py exits with exactly what exit_code() returns.

EXIT_OK = 0            # the turn finished and the model answered
EXIT_ERROR = 1         # the provider or the agent failed (ProviderFailure, ...)
EXIT_USAGE = 2         # bad arguments, or an id/name that does not exist
EXIT_DENIED = 3        # a tool call was refused by the permission layer
EXIT_LIMIT = 4         # the agent hit its step limit without finishing
EXIT_INTERRUPTED = 130 # Ctrl-C, the shell's convention for SIGINT

# Worst-outcome ordering for a run of several turns (piped stdin): later
# entries beat earlier ones, so a process that failed once never exits 0.
# EXIT_USAGE is absent on purpose — it is decided before any turn runs.
_SEVERITY = (EXIT_OK, EXIT_LIMIT, EXIT_DENIED, EXIT_INTERRUPTED, EXIT_ERROR)

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

# How many transcript messages a /compact keeps verbatim.
COMPACT_KEEP = 10


def find_session(store, ref: str):
    """(session, error) for an id or a unique id prefix.

    /sessions, the resume dialog and the JSON events all show eight
    characters, so a user who pastes back what they were shown has to be
    understood. An ambiguous prefix is an error rather than a guess: picking
    the newest match would silently rename or delete the wrong conversation.
    """
    if not str(ref or "").strip():
        return None, "no session id given"
    ref = str(ref).strip()
    try:
        session = store.load(ref)
        if session is not None:
            return session, ""
        rows = store.list_sessions(limit=100000, include_archived=True)
    except Exception as exc:
        return None, str(exc)
    matches = [row["id"] for row in rows if row["id"].startswith(ref)]
    if len(matches) == 1:
        return store.load(matches[0]), ""
    if matches:
        return None, ("ambiguous session id '%s' (%d sessions match)"
                      % (ref, len(matches)))
    return None, "no session %s" % ref


def copy_session(store, session, cwd: str = "", provider: str = "",
                 model: str = ""):
    """A new session holding the same transcript — opencode's session.fork.

    Only the messages are copied. The file snapshots stay with the session
    that took them, so /undo in a fork reverts nothing the fork did not do
    itself, and the original is left exactly as it was. `cwd`/`provider`/
    `model` describe where the copy will *continue*, defaulting to wherever
    the original was.
    """
    forked = store.new_session(cwd or session.cwd, provider or session.provider,
                               model or session.model, session.title or "")
    for message in session.messages:
        forked.append(message)
    return forked


def _color_enabled() -> bool:
    return sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _color_enabled() else text


def _summarize_args(name: str, args: dict) -> str:
    """One-line tool call summary, like opencode's transcript."""
    if name in ("read", "write", "edit"):
        return str(args.get("filePath", ""))
    if name == "bash":
        return str(args.get("description") or args.get("command", ""))
    if name in ("grep", "glob"):
        pattern = args.get("pattern", "")
        include = args.get("include")
        return f"{pattern}" + (f"  ({include})" if include else "")
    if name == "list":
        return str(args.get("path", "."))
    if name == "webfetch":
        return str(args.get("url", ""))
    if name == "task":
        return str(args.get("description", ""))
    if name == "todowrite":
        todos = args.get("todos") or []
        return f"{len(todos)} items"
    if name == "memory_write":
        return str(args.get("name") or args.get("text", ""))[:60]
    if name == "memory_read":
        return str(args.get("query", "")) or "(all)"
    if name == "apply_patch":
        return f"{len(str(args.get('patchText', '')).splitlines())} patch lines"
    return ", ".join(f"{k}={v!r}"[:40] for k, v in list(args.items())[:2])


def print_diff(diff: str, limit: int = 40):
    lines = diff.splitlines()
    for line in lines[:limit]:
        if line.startswith("+++") or line.startswith("---"):
            print("  " + _c(line, DIM))
        elif line.startswith("+"):
            print("  " + _c(line, GREEN))
        elif line.startswith("-"):
            print("  " + _c(line, RED))
        elif line.startswith("@@"):
            print("  " + _c(line, CYAN))
        else:
            print("  " + line)
    if len(lines) > limit:
        print("  " + _c(f"… +{len(lines) - limit} more diff lines", DIM))


def _ask_questions(request) -> str:
    """The model's multiple-choice questions, on a plain terminal.

    Fills request.metadata["answers"] in place — the contract the question
    tool documents — and returns "once". Every question was a dead end
    before this existed: the tool asked, no front end answered, and the
    model was told "Unanswered" after burning the turn.
    """
    metadata = request.metadata or {}
    answers: List[Any] = []
    for question in metadata.get("questions") or []:
        print()
        print(_c("┌ " + str(question.get("question", "")), BOLD + CYAN))
        options = question.get("options") or []
        for index, option in enumerate(options, 1):
            description = option.get("description", "")
            print("  %2d. %s%s" % (index, option["label"],
                                   _c("  — " + description, DIM)
                                   if description else ""))
        hint = ("numbers or text, comma-separated"
                if question.get("multiple") else "a number, or free text")
        print(_c("└ answer (%s; empty skips): " % hint, DIM), end="")
        try:
            raw = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            answers.append([])
            continue
        chosen: List[str] = []
        for part in (p.strip() for p in raw.split(",")):
            if not part:
                continue
            if part.isdigit() and 1 <= int(part) <= len(options):
                chosen.append(options[int(part) - 1]["label"])
            else:
                chosen.append(part)
        if not question.get("multiple"):
            chosen = chosen[:1]
        answers.append(chosen)
    metadata["answers"] = answers
    return "once"


def terminal_asker(request) -> str:
    """Permission prompt for a plain terminal. Returns once|always|reject."""
    metadata = request.metadata or {}
    if metadata.get("kind") == "question":
        return _ask_questions(request)
    print()
    print(_c(f"┌ Permission required: {request.title}", BOLD + YELLOW))
    if metadata.get("diff"):
        print_diff(metadata["diff"], limit=24)
    elif metadata.get("command"):
        print("  " + _c(metadata["command"], BOLD))
        if metadata.get("workdir"):
            print("  " + _c(f"in {metadata['workdir']}", DIM))
    elif metadata.get("url"):
        print("  " + metadata["url"])
    print(_c("└ [o]nce  [a]lways  [r]eject (default: reject)", DIM))
    try:
        answer = input("  > ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return "reject"
    if answer.startswith("o") or answer == "y":
        return "once"
    if answer.startswith("a"):
        return "always"
    return "reject"


class REPL:
    def __init__(self, config: Config, provider: str = "", cwd: str = ".",
                 auto_approve: bool = False, agent_name: str = "",
                 model: str = "", print_logs: bool = False,
                 reasoning_effort: str = "", yolo: bool = False):
        self.config = config
        self.cwd = cwd
        self.provider_name = provider or config.data.get("default_provider", "ollama")
        self.agent_name = agent_name
        self.model_override = model
        self.reasoning_effort_override = reasoning_effort
        self.print_logs = print_logs
        self.permissions = Permissions(
            config=config,
            asker=terminal_asker if sys.stdin.isatty() else None,
            auto_approve=auto_approve or yolo,
            yolo=yolo)
        # The turn lifecycle (sessions, checkpoints, persistence) lives in one
        # place so the TUI runs the same one; main.py hands this very object to
        # the TUI, which is why both front-ends see one conversation.
        self.turn = TurnController(cwd=cwd, provider_name=self.provider_name)
        self.commands = None
        self.agent = None
        self.project = None
        self._show_reasoning = False
        self._reported_persistence = ""
        self.last_turn = None
        # Per-turn bookkeeping for exit_code(). A denial and a step limit are
        # not exceptions — the agent reports them as events and returns
        # normally — so the only place they can be seen is the event stream.
        self._denied: List[str] = []
        self._limited = False
        self._worst = EXIT_OK
        # --title: applied as soon as there is a session to apply it to.
        self.pending_title = ""
        self._build_agent()
        self._setup_commands()
        if print_logs:
            self.report_warnings()

    # The durable session is the controller's; the REPL's commands read it
    # through here so a session opened by either front-end is the same one.
    @property
    def session(self):
        return self.turn.session

    @session.setter
    def session(self, value):
        self.turn.adopt(value)

    @property
    def _last_checkpoint(self):
        return self.turn.last_checkpoint

    # --- setup ---------------------------------------------------------

    def _build_agent(self):
        """Build the agent for the current provider/agent/model selection.

        The permission object is shared across rebuilds so that "always" grants
        made earlier in the session survive switching provider or model.
        """
        self.agent = build_agent(self.config, self.provider_name, self.cwd,
                                 permissions=self.permissions,
                                 agent_name=self.agent_name,
                                 model=self.model_override,
                                 reasoning_effort=self.reasoning_effort_override)
        self.project = self.agent.project
        self.agent_name = self.agent.agent_name
        self.model = self.agent.model
        self._sync_turn()

    def _sync_turn(self):
        """Keep the controller's idea of provider/model current.

        Only read when a *new* session row is created, but a stale value there
        mislabels the session in /sessions and in the resume dialog.
        """
        self.turn.provider_name = self.provider_name
        self.turn.model = getattr(self.agent, "model", "") or ""

    def _rebuild_agent(self):
        """Rebuild after a settings change, carrying the conversation over.

        The history is provider-agnostic (it is only ever a list of Msg), so
        switching model or provider mid-session keeps the transcript intact —
        which is the whole point of switching mid-session.
        """
        old = self.agent
        self._build_agent()
        if old is None:
            return
        self.agent.messages = list(old.messages)
        self.agent.tokens = dict(old.tokens)
        self.agent.usage = old.usage
        # The carried tracker's last exchange was measured under the old
        # provider/model; against the new window it misreads (120k observed
        # on a 32k window shows 366%). Estimate until the next real usage.
        self.agent.usage.invalidate_latest()
        self.agent.ctx.read_files = set(old.ctx.read_files)
        self.agent.ctx.todos = list(old.ctx.todos)
        self.agent.ctx.modified_files.update(old.ctx.modified_files)

    def warnings(self) -> List[str]:
        """Configuration problems the user should know about, if any."""
        return list(getattr(self.agent, "warnings", []))

    def report_warnings(self) -> None:
        for warning in self.warnings():
            print(_c(f"[config] {warning}", YELLOW), file=sys.stderr)

    def _setup_commands(self):
        try:
            from .commands import CommandRegistry
        except ImportError:
            self.commands = None
            return
        registry = CommandRegistry(
            self.cwd, trusted=True if self.permissions.yolo else None)
        for name, handler, help_text in self._builtins():
            registry.register(name, handler, help_text)
        self.commands = registry

    def _open_session(self):
        return self.turn.open_session()

    def _store(self):
        """The session store, or None when sqlite3 is unavailable."""
        return self.turn.store()

    def _memory(self):
        from .memory import MemoryStore
        return MemoryStore(self.cwd)

    # --- event rendering -------------------------------------------------

    def _on_text(self, text: str):
        print(text, end="", flush=True)

    def _on_event(self, kind: str, payload):
        if kind == "reasoning":
            if self._show_reasoning:
                print(_c(payload, DIM), end="", flush=True)
            return
        if kind == "tool":
            summary = _summarize_args(payload["name"], payload["args"])
            print(f"\n{_c('⏺', CYAN)} {_c(payload['name'], BOLD)}  {summary}", flush=True)
            return
        if kind == "tool_result":
            metadata = payload.get("metadata") or {}
            if metadata.get("diff"):
                print_diff(metadata["diff"])
            else:
                output = (payload.get("output") or "").rstrip()
                if output:
                    lines = output.splitlines()
                    for line in lines[:8]:
                        print("  " + _c(line[:200], DIM))
                    if len(lines) > 8:
                        print("  " + _c(f"… +{len(lines) - 8} lines", DIM))
            return
        if kind == "tool_denied":
            print(_c(f"  ✗ denied: {payload['reason']}", RED))
            return
        if kind == "tool_error":
            print(_c(f"  ✗ {payload['name']}: {payload['error']}", RED))
            return
        if kind == "limit":
            print(_c("  [step budget reached after %s steps; send 'continue' "
                     "for a fresh budget. /config shows the active value; "
                     "external edits need /reload]" % payload["steps"], YELLOW))

    # --- running ----------------------------------------------------------

    def quick_capture(self, line: str) -> Optional[str]:
        """Claude Code's "#" convention: a leading # saves a memory.

        Returns the confirmation to display, or None when the line is not a
        capture — so a caller can use it as "was this line consumed?".
        """
        return self.turn.quick_capture(self.agent, line) or None

    def _report_persistence(self) -> None:
        """Say once, and again whenever it changes, that nothing is being saved.

        Swallowing this is how the REPL used to offer /undo for a conversation
        that was never written: the user only found out when undo did nothing.
        """
        notice = self.turn.persistence_notice()
        if notice and notice != self._reported_persistence:
            print(_c("  [%s]" % notice, YELLOW), file=sys.stderr)
        self._reported_persistence = notice

    def _on_attach(self, paths: List[str]) -> None:
        print(_c(f"  [attached: {', '.join(paths)}]", DIM))

    def _event(self, kind: str, payload) -> None:
        """Record what the exit code depends on, then render.

        Bookkeeping lives here rather than in `_on_event` so that a front-end
        which replaces the renderer (JSONREPL) cannot accidentally drop it: a
        denied permission that no longer moves the exit code is a scripted run
        that reports success after refusing to do the work.
        """
        if kind == "tool_denied":
            self._denied.append(str((payload or {}).get("name") or ""))
        elif kind == "limit":
            self._limited = True
        self._on_event(kind, payload)

    def _run(self, message: str) -> TurnResult:
        """One turn through the shared controller, plus the per-turn bookkeeping.

        Every front-end path (`send`, JSONREPL.send) goes through here so the
        exit code, the pending --title and the turn lifecycle stay in step.
        """
        self._denied = []
        self._limited = False
        result = self.last_turn = self.turn.run_turn(
            self.agent, message, on_text=self._on_text, on_event=self._event,
            on_attach=self._on_attach,
            expand_mentions=self.commands is not None)
        self._apply_pending_title()
        code = self.turn_exit_code(result, bool(self._denied), self._limited)
        if _SEVERITY.index(code) > _SEVERITY.index(self._worst):
            self._worst = code
        return result

    def set_title(self, title: str) -> None:
        """--title / `/rename` before there is a session to rename.

        A new conversation has no session row until the first turn persists,
        so a title asked for on the command line has to wait for one.
        """
        self.pending_title = (title or "").strip()
        self._apply_pending_title()

    def _apply_pending_title(self) -> None:
        session = self.turn.session
        if not self.pending_title or session is None:
            return
        try:
            session.rename(self.pending_title)
        except Exception:
            return      # a failed rename is already reported as a persistence problem
        self.pending_title = ""

    def send(self, message: str) -> str:
        """Run one turn and show it. `last_turn` keeps the structured result so
        a one-shot run can exit non-zero when the turn actually failed."""
        result = self._run(message)
        if result.captured:
            print(result.captured)
            return ""
        if result.interrupted:
            print(_c("\n[interrupted]", YELLOW))
            self._report_persistence()
            return ""
        print()
        if result.error:
            print(_c(f"[error] {result.error}", RED))
        self._report_persistence()
        return result.text

    @staticmethod
    def turn_exit_code(result: Optional[TurnResult],
                       denied: bool = False, limited: bool = False) -> int:
        """The code one turn's outcome deserves.

        Precedence is worst-first: a run that died tells a script less than
        nothing about whether the work happened, a run the user stopped is not
        a failure of the model, and a refusal is not the same as a step limit.
        """
        if result is None:
            return EXIT_OK
        if result.error:
            return EXIT_ERROR
        if result.interrupted:
            return EXIT_INTERRUPTED
        if denied:
            return EXIT_DENIED
        if limited:
            return EXIT_LIMIT
        return EXIT_OK

    def exit_code(self) -> int:
        """The worst outcome this process has seen, for `sys.exit()`.

        Worst rather than last: a piped run of several prompts where the third
        failed must not exit 0 because the fourth succeeded. See the EXIT_*
        constants at the top of this module for what each code means.
        """
        code = self.turn_exit_code(self.last_turn, bool(self._denied),
                                   self._limited)
        return code if _SEVERITY.index(code) > _SEVERITY.index(self._worst) \
            else self._worst

    # --- commands ----------------------------------------------------------

    def _builtins(self):
        return [
            ("help", self._cmd_help, "show this help"),
            ("exit", self._cmd_exit, "quit"),
            ("quit", self._cmd_exit, "quit"),
            ("clear", self._cmd_clear, "start a new conversation"),
            ("new", self._cmd_clear, "start a new conversation"),
            ("agent", self._cmd_agent, "list or switch agent"),
            ("plan", self._cmd_plan, "switch to the read-only plan agent"),
            ("build", self._cmd_build, "leave plan mode, back to build"),
            ("model", self._cmd_model, "show or set the model"),
            ("models", self._cmd_models, "list models the providers offer"),
            ("provider", self._cmd_provider, "switch provider, or add/remove/default"),
            ("login", self._cmd_login, "store an API key / sign in"),
            ("logout", self._cmd_logout, "remove stored credentials"),
            ("keys", self._cmd_keys, "show credential status"),
            ("tools", self._cmd_tools, "list available tools"),
            ("mcp", self._cmd_mcp, "list MCP servers and their tools"),
            ("farewell", self._cmd_farewell,
             "toggle the model-written exit haiku (default on)"),
            ("permissions", self._cmd_permissions, "show permission rules"),
            ("reasoning", self._cmd_reasoning, "toggle reasoning display"),
            ("effort", self._cmd_effort, "show or set model reasoning effort"),
            ("steer", self._cmd_steer, "show, edit or drop pending steering"),
            ("yolo", self._cmd_yolo, "toggle bypassing every permission gate"),
            ("status", self._cmd_status, "show the current setup"),
            ("config", self._cmd_config, "show effective settings and their source"),
            ("reload", self._cmd_reload, "apply config-file edits to this session"),
            ("context", self._cmd_context, "show context window usage"),
            ("usage", self._cmd_usage, "show token usage for this session"),
            ("memory", self._cmd_memory, "list or search saved memories"),
            ("remember", self._cmd_remember, "save a memory"),
            ("forget", self._cmd_forget, "delete a memory by name"),
            ("sessions", self._cmd_sessions, "list or search saved sessions"),
            ("resume", self._cmd_resume, "resume a session by id"),
            ("fork", self._cmd_fork, "continue in a copy, leaving this one intact"),
            ("rename", self._cmd_rename, "rename the current session"),
            ("archive", self._cmd_archive, "archive the current session"),
            ("export", self._cmd_export, "export the transcript"),
            ("compact", self._cmd_compact, "fold old messages into a summary"),
            ("undo", self._cmd_undo, "revert file changes from the last run"),
            ("todos", self._cmd_todos, "show the current todo list"),
            ("cost", self._cmd_cost, "show token usage"),
            ("init", self._cmd_init, "write AGENTS.md and haikode.json"),
        ]

    def _cmd_help(self, arg):
        text = (self.commands.help_text() if self.commands
                else "\n".join(f"/{n:<12} {h}" for n, _, h in self._builtins()))
        # The table is static, so say here that one of its entries cannot work
        # rather than let the list keep advertising it.
        if self.turn.persistence_error:
            text += "\n" + _c("  /undo is unavailable: "
                              + self.turn.persistence_notice(), YELLOW)
        return text

    def _cmd_exit(self, arg):
        print(self._farewell())
        raise SystemExit(0)

    def _farewell(self) -> str:
        """The exit haiku plus how to come back to this conversation."""
        from .status import farewell
        session = getattr(self.turn, "session", None)
        return farewell(str(getattr(session, "id", "") or ""),
                        poem=getattr(self.turn, "farewell_poem", None),
                        poet=getattr(self.turn, "farewell_poet", ""))

    def new_conversation(self) -> None:
        """Drop the conversation, keeping the provider/model/agent selection.

        Separate from _build_agent(): starting over must not re-read the
        project config or re-resolve the provider, and the front-ends need a
        way to say "empty this" that is not "rebuild everything".
        """
        self.agent.clear()
        self.turn.reset()

    def _cmd_clear(self, arg):
        self.new_conversation()
        return "New conversation."

    # --- agents ---

    def _cmd_agent(self, arg):
        registry = self.agent.registry
        if not arg:
            lines = []
            for defn in registry.primary():
                marker = "* " if defn.name == self.agent.agent_name else "  "
                lines.append(f"{marker}{defn.name:<12} {defn.description}")
            subagents = [d.name for d in registry.subagents()]
            if subagents:
                lines.append("  subagents: " + ", ".join(subagents))
            for warning in registry.warnings:
                lines.append(_c(f"  warning: {warning}", YELLOW))
            return "\n".join(lines)
        return self._switch_agent(arg.strip())

    def _switch_agent(self, name: str) -> str:
        try:
            message = self.agent.switch_agent(name)
        except KeyError:
            known = ", ".join(d.name for d in self.agent.registry.primary())
            return f"Unknown agent '{name}'. Available: {known}"
        self.agent_name = self.agent.agent_name
        self.model = self.agent.model
        self._sync_turn()
        return f"{message}\n  tools: {', '.join(sorted(self.agent.tools))}"

    def _cmd_plan(self, arg):
        return self._switch_agent("plan")

    def _cmd_build(self, arg):
        return self._switch_agent("build")

    # --- models and providers ---

    def _catalog(self):
        # The real Config, never the merged session view: a model choice is the
        # user's and belongs in the user's own config file.
        from .models import ModelCatalog
        return ModelCatalog(self.config)

    def _cmd_model(self, arg):
        catalog = self._catalog()
        if not arg:
            current = catalog.current()
            lines = [f"Model: {current.id if current else self.model or '(provider default)'}"]
            favourites = catalog.favourites()
            if favourites:
                lines.append("Favourites: " + ", ".join(r.id for r in favourites))
            recent = catalog.recent()
            if recent:
                lines.append("Recent:     " + ", ".join(r.id for r in recent))
            lines.append(_c("/model <provider/model> to switch, "
                            "/model fav [id] to (un)favourite", DIM))
            return "\n".join(lines)

        parts = arg.split(None, 1)
        if parts[0] in ("fav", "favourite", "favorite"):
            target = parts[1].strip() if len(parts) > 1 else self._current_model_id()
            if not target:
                return "Nothing to favourite."
            now = catalog.toggle_favourite(self._qualify(target))
            return f"{'Added' if now else 'Removed'} favourite: {self._qualify(target)}"

        ref = self._qualify(arg.strip())
        try:
            selected = catalog.select(ref)
        except (KeyError, ValueError) as e:
            return f"[error] {e}"
        provider_changed = selected.provider != self.provider_name
        previous, previous_override = self.provider_name, self.model_override
        self.provider_name = selected.provider
        self.model_override = ""
        note = ""
        if provider_changed:
            try:
                self._rebuild_agent()
            except Exception as e:
                self.provider_name = previous
                self.model_override = previous_override
                return f"[error] model unchanged: {e}"
        else:
            note = self.agent.set_model(selected.model)
        self.model = self.agent.model
        self._sync_turn()
        suffix = f" — {note}" if note else ""
        return f"Model → {selected.id} (saved){suffix}"

    def _current_model_id(self) -> str:
        return f"{self.provider_name}/{self.model}" if self.model else ""

    def _qualify(self, value: str) -> str:
        """Let the user type a bare model id and mean the current provider."""
        return value if "/" in value else f"{self.provider_name}/{value}"

    def _cmd_models(self, arg):
        catalog = self._catalog()
        refs = catalog.models(arg.strip() or None) if arg.strip() else catalog.choices()
        if not refs:
            errors = "; ".join(f"{name}: {why}"
                               for name, why in catalog.errors.items())
            return f"[error] {errors or 'no models returned'}"
        current = self._current_model_id()
        lines, category = [], None
        for ref in refs:
            if ref.category != category:
                category = ref.category
                lines.append(_c(f"  {category}", BOLD))
            marker = "* " if ref.id == current else "  "
            free = " (free)" if ref.free else ""
            lines.append(f"  {marker}{ref.id}{free}")
        for name, why in catalog.errors.items():
            lines.append(_c(f"  warning: {name}: {why}", YELLOW))
        lines.append(_c(f"({len(refs)} models — /model <id> to switch)", DIM))
        return "\n".join(lines)

    def _cmd_provider(self, arg):
        providers = self.config.data.get("providers", {})
        if not arg:
            return "\n".join(
                ("* " if n == self.provider_name else "  ") +
                f"{n:<12} {provider_status(self.config, n)}" for n in providers)

        parts = arg.split()
        action = parts[0]
        if action in ("add", "remove", "default"):
            return self._provider_admin(action, parts[1:])
        if action not in providers:
            return f"Unknown provider '{action}'. Available: {', '.join(providers)}"
        previous, previous_override = self.provider_name, self.model_override
        self.provider_name = action
        self.model_override = ""
        try:
            self._rebuild_agent()
        except Exception as e:
            # A failed build must not strand provider_name pointing at a
            # provider the still-live agent was never built for.
            self.provider_name, self.model_override = previous, previous_override
            return f"[error] provider unchanged: {e}"
        return f"Provider → {action} ({provider_status(self.config, action)})"

    def _provider_admin(self, action: str, args: List[str]) -> str:
        from . import models as models_mod
        if action == "add":
            if len(args) < 2:
                return ("Usage: /provider add <name> <base-url> [model] "
                        "[openai|anthropic]")
            name, base_url = args[0], args[1]
            model = args[2] if len(args) > 2 else ""
            dialect = args[3] if len(args) > 3 else "openai"
            ok, message = models_mod.add_provider(
                self.config, name, base_url, model, dialect, update=True)
            return message if ok else f"[error] {message}"
        if action == "remove":
            if not args:
                return "Usage: /provider remove <name>"
            ok, message = models_mod.remove_provider(self.config, args[0])
            if ok and args[0] == self.provider_name:
                self.provider_name = self.config.data.get("default_provider", "")
                self._rebuild_agent()
            return message if ok else f"[error] {message}"
        if not args:
            return "Usage: /provider default <name>"
        ok, message = models_mod.set_default(self.config, args[0])
        if ok:
            self.provider_name = args[0]
            self._rebuild_agent()
        return message if ok else f"[error] {message}"

    def _cmd_login(self, arg):
        from .auth import interactive_login
        if interactive_login(self.config, arg):
            self._rebuild_agent()
            return "Signed in."
        return "Not signed in."

    def _cmd_logout(self, arg):
        if not arg:
            return "Usage: /logout <provider>"
        self.config.clear_api_key(arg)
        # Removing the file is not enough: the live provider holds what it
        # read, so without this the session keeps working as the account it
        # was just logged out of.
        invalidate = getattr(getattr(self.agent, "provider", None),
                             "invalidate_auth", None)
        if callable(invalidate):
            invalidate()
        return f"Credentials for {arg} removed."

    def _cmd_keys(self, arg):
        return "\n".join(f"  {n:<12} {provider_status(self.config, n)}"
                         for n in self.config.data.get("providers", {}))

    def _cmd_tools(self, arg):
        return "\n".join(f"  {name:<12} {tool.description.splitlines()[0][:60]}"
                         for name, tool in sorted(self.agent.tools.items()))

    def _cmd_farewell(self, arg):
        """Turn the model-written exit haiku on or off, persistently.

        Off means no provider call is spent on poetry; the curated
        collection still says goodbye, because that part is free.
        """
        choice = (arg or "").strip().lower()
        if choice not in ("", "on", "off"):
            return "Usage: /farewell [on|off]"
        if choice:
            enabled = choice == "on"
            self.config.data["farewell_haiku"] = enabled
            try:
                self.config.save()
            except OSError as exc:
                return f"[error] could not save: {exc}"
            self.turn.compose_farewell = enabled and sys.stdin.isatty()
        enabled = self.config.data.get("farewell_haiku", True)
        return ("model-written exit haiku: %s  (the curated collection "
                "answers either way)" % ("on" if enabled else "off"))

    def _cmd_mcp(self, arg):
        """Configured MCP servers: connection state, tools, warnings."""
        from .skills import mcp_report, mcp_warnings
        manager = getattr(self.agent.ctx, "mcp", None)
        lines = [mcp_report(manager)]
        for warning in mcp_warnings(manager):
            lines.append(_c("  warning: %s" % warning, YELLOW))
        return "\n".join(lines)

    def _cmd_permissions(self, arg):
        """The rules in force, in the order they are evaluated.

        Read through Permissions.describe() rather than re-parsing the config
        block here. Which shapes a rule may take is that module's business (a
        flat decision, an ordered object, a list of pairs), and this second
        reader both crashed on the list form and could disagree with what
        ask() decides. Order is the whole point now: the LAST matching rule
        wins, so a catch-all printed after a specific rule is the one in force.
        Session grants are listed apart because they are not rules — they only
        upgrade an ask, and a configured deny still beats them.
        """
        permissions = self.agent.permissions
        lines = []
        for key, pattern, decision, configured in permissions.describe():
            suffix = "" if configured else "  (default)"
            lines.append(f"  {key:<12} {decision:<6} {pattern}{suffix}")
        grants = permissions.session_grants()
        if grants:
            lines.append("  -- granted for this session only --")
            for key in sorted(grants):
                for glob in grants[key]:
                    lines.append(f"  {key:<12} {'allow':<6} {glob}")
        return "\n".join(lines)

    def _cmd_reasoning(self, arg):
        self._show_reasoning = not self._show_reasoning
        return f"Reasoning display {'on' if self._show_reasoning else 'off'}."

    def _cmd_effort(self, arg):
        choices = tuple(self.agent.reasoning_efforts())
        if not choices:
            return (f"Reasoning effort is not controllable through "
                    f"{self.provider_name}.")
        value = arg.strip().lower()
        if not value:
            return ("Reasoning effort: %s (choices: %s)"
                    % (self.agent.reasoning_effort or "(provider default)",
                       ", ".join(choices)))
        if value == "next":
            current = self.agent.reasoning_effort
            index = choices.index(current) if current in choices else -1
            value = choices[(index + 1) % len(choices)]
        try:
            applied = self.agent.set_reasoning_effort(value)
        except ValueError as exc:
            return f"[error] {exc}"
        self.reasoning_effort_override = applied
        return (f"Reasoning effort -> {applied} for this live session. "
                "Set providers.<name>.reasoning_effort in config.json and "
                "run /reload to make it the session default.")

    # --- status, config, usage ---

    def _cmd_status(self, arg):
        from . import status
        info = status.collect(self.config, self.provider_name, self.cwd,
                              self.agent.tools)
        lines = status.detail_lines(info)
        lines.append(f"Agent          {self.agent.agent_name}"
                     + (" (read-only)" if self.agent.plan_mode else ""))
        lines.append(f"Prompt         {self.agent.prompt_variant()}")
        if self.agent.reasoning_effort:
            lines.append(f"Effort         {self.agent.reasoning_effort}")
        memories = self._safe(lambda: len(self._memory().all()), 0)
        lines.append(f"Memories       {memories}")
        if self.project is not None and self.project.sources:
            lines.append("Project config " + ", ".join(str(p) for p in
                                                       self.project.sources))
        if self.turn.persistence_error:
            lines.append(_c("Sessions       " + self.turn.persistence_notice(),
                            YELLOW))
        for warning in self.warnings():
            lines.append(_c(f"Warning        {warning}", YELLOW))
        return "\n".join(lines)

    def _cmd_config(self, arg):
        if self.project is None:
            return "No project configuration loaded."
        lines = list(self.project.describe())
        lines.append("")
        lines.append("Effective settings:")
        lines.append(f"  provider      {self.provider_name}")
        lines.append(f"  model         {self.agent.model}")
        lines.append(f"  agent         {self.agent.agent_name}")
        limit = self.agent.max_steps if self.agent.max_steps is not None else "unlimited"
        lines.append(f"  max_steps     {limit}")
        lines.append(f"  effort        {self.agent.reasoning_effort or '(provider default)'}")
        lines.append(f"  context       {self.agent.context_window}"
                     f" ({self.agent.context_source})")
        lines.append(f"  tools         {', '.join(sorted(self.agent.tools))}")
        lines.append("")
        lines.append(f"Loaded from {self.config.path}. External edits are "
                     "snapshots: run /reload to apply them to this live session, "
                     "or restart haikode.")
        return "\n".join(lines)

    def _cmd_reload(self, arg):
        old = self.agent
        old_data = self.config.data
        old_permissions_config = getattr(self.permissions, "config", None)
        before = (old.max_steps, old.model, old.reasoning_effort,
                  old.context_window)
        try:
            changed = self.config.reload()
            self._rebuild_agent()
        except Exception as exc:
            self.config.data = old_data
            self.agent = old
            self.permissions.config = old_permissions_config
            return f"[error] {exc}"
        after = (self.agent.max_steps, self.agent.model,
                 self.agent.reasoning_effort, self.agent.context_window)
        global_note = "global file changed" if changed else "global file unchanged"
        return ("Reloaded %s and project configuration (%s); applied to this "
                "live session while retaining %d messages. "
                "max_steps/model/effort/context: %s -> %s"
                % (self.config.path, global_note, len(self.agent.messages),
                   "/".join(str(value if value is not None else "unlimited")
                            for value in before),
                   "/".join(str(value if value is not None else "unlimited")
                            for value in after)))

    def _cmd_steer(self, arg):
        """Inspect and change what the running turn will be told next."""
        agent = self.agent
        pending = agent.pending_steering()
        parts = (arg or "").split(None, 2)
        verb = parts[0].lower() if parts else ""

        if not verb:
            if not pending:
                return ("Nothing pending. Type while a turn is running and it "
                        "reaches the model at its next step.")
            lines = ["  %d  %s" % (index + 1, text.splitlines()[0][:70])
                     for index, text in enumerate(pending)]
            lines.append(_c("/steer edit <n> <text> · /steer drop <n> · "
                            "/steer clear", DIM))
            return "\n".join(lines)

        if verb == "clear":
            return "Dropped %d pending message(s)." % agent.clear_steering()

        if verb in ("edit", "drop"):
            if len(parts) < 2 or not parts[1].isdigit():
                return "Usage: /steer %s <n>%s" % (
                    verb, " <text>" if verb == "edit" else "")
            index = int(parts[1]) - 1
            text = parts[2] if verb == "edit" and len(parts) > 2 else ""
            if verb == "edit" and not text:
                return "Usage: /steer edit <n> <text>"
            if not agent.edit_steering(index, text):
                return "No pending message %s." % parts[1]
            return ("Replaced message %s." if verb == "edit"
                    else "Dropped message %s.") % parts[1]

        return "Usage: /steer [edit <n> <text> | drop <n> | clear]"

    def _cmd_yolo(self, arg):
        """Turn every gate off, or back on. Session-only: never persisted."""
        word = arg.strip().lower()
        if word in ("on", "off"):
            state = word == "on"
        elif not word:
            state = not self.permissions.yolo
        else:
            return "Usage: /yolo [on|off]"
        self.permissions.yolo = state
        self.permissions.auto_approve = state or self.permissions.auto_approve
        agent_perms = getattr(self.agent, "permissions", None)
        if agent_perms is not None and agent_perms is not self.permissions:
            agent_perms.yolo = state
            agent_perms.auto_approve = state or agent_perms.auto_approve
        if self.commands is not None:
            self.commands.trusted = True if state else None
            self.commands.load_custom(self.cwd)
        if not state:
            return "yolo off - permission rules apply again"
        return _c("yolo ON - no prompts, no deny rules, no repo trust check. "
                  "This session only.", YELLOW)

    def _cmd_context(self, arg):
        from .usage import context_bar, detail_lines
        state = self.agent.context_state()
        lines = [context_bar(state, width=24),
                 "Window source: %s" % self.agent.context_source]
        lines.extend(detail_lines(self.agent.usage, state))
        return "\n".join(lines)

    def _cmd_usage(self, arg):
        from .usage import detail_lines
        return "\n".join(detail_lines(self.agent.usage, self.agent.context_state()))

    def _cmd_cost(self, arg):
        from .usage import summary_line
        return summary_line(self.agent.usage, self.agent.context_state())

    # --- memory ---

    def _cmd_memory(self, arg):
        store = self._memory()
        query = arg.strip()
        memories = store.search(query) if query else store.all()
        if not memories:
            note = f"No memories match '{query}'." if query else "No memories saved."
            return (note + "\nEditable project memories: %s\n"
                    "Editable user memories: %s"
                    % (store.project_dir, store.global_dir))
        lines = [f"  {m.name:<28} {m.scope:<7} {m.summary()}" for m in memories]
        for warning in store.warnings:
            lines.append(_c(f"  warning: {warning}", YELLOW))
        lines.append(_c("Project files: %s" % store.project_dir, DIM))
        lines.append(_c("User files:    %s" % store.global_dir, DIM))
        lines.append(_c("Edit the .md files directly, or use /remember and /forget.",
                        DIM))
        return "\n".join(lines)

    def _cmd_remember(self, arg):
        if not arg.strip():
            return "Usage: /remember <text>   (prefix with 'user:' for a global memory)"
        captured = self.quick_capture("# " + arg.strip())
        return captured or "[error] nothing to remember"

    def _cmd_forget(self, arg):
        if not arg.strip():
            return "Usage: /forget <name>"
        store = self._memory()
        if store.delete(arg.strip()):
            self.agent.refresh_memory()
            return f"Forgot '{arg.strip()}'."
        names = ", ".join(m.name for m in store.all()) or "(none)"
        return f"No memory named '{arg.strip()}'. Known: {names}"

    # --- sessions ---

    def _cmd_sessions(self, arg):
        store = self._store()
        if store is None:
            return "[error] sessions unavailable (sqlite3 missing)"
        query = arg.strip()
        try:
            rows = (store.search(query) if query
                    else store.list_sessions(cwd=self.cwd))
        except Exception as e:
            return f"[error] {e}"
        if not rows:
            return f"No sessions match '{query}'." if query else "No saved sessions."
        lines = []
        for row in rows:
            marker = "* " if self.session is not None and row["id"] == self.session.id else "  "
            flag = " [archived]" if row.get("archived") else ""
            # The id in full: this is the list a user copies an id out of, and
            # ids are time-prefixed, so every eight-character form is the same.
            line = (f"{marker}{row['id']}  {row.get('message_count', 0):>3} msgs  "
                    f"{(row.get('title') or '')[:48]}{flag}")
            if row.get("snippet"):
                line += "\n" + _c(f"      {row['snippet']}", DIM)
            lines.append(line)
        lines.append(_c("(/resume <id> to continue)", DIM))
        return "\n".join(lines)

    def _cmd_resume(self, arg):
        if not arg:
            return "Usage: /resume <session-id>"
        return self.resume_session(arg)

    def resume_session(self, session_id: str) -> str:
        """Continue a stored session by id or unique prefix (/resume, --session)."""
        store = self._store()
        if store is None:
            return "[error] sessions unavailable (sqlite3 missing)"
        session, error = find_session(store, session_id)
        if session is None:
            return f"No session {session_id}" if error.startswith("no session") \
                else f"[error] {error}"
        return self.adopt_session(session)

    def adopt_session(self, session) -> str:
        """Continue an existing session in this REPL."""
        self.turn.adopt(session)
        # Replayed wholesale so tool calls stay paired with their results:
        # a provider rejects an assistant turn whose calls were never answered.
        self.agent.messages = list(session.messages)
        # The id in full: it is what `--session` and `haikode session …` take,
        # and the eight-character form is the same for every session opened
        # this decade (ids are time-prefixed), so it cannot be pasted back.
        return f"Resumed {session.id} ({len(session.messages)} messages)"

    def resume_latest(self) -> str:
        """Adopt the most recent session for this directory (--continue)."""
        store = self._store()
        if store is None:
            return "[error] sessions unavailable (sqlite3 missing)"
        try:
            rows = store.list_sessions(limit=1, cwd=self.cwd)
            if not rows:
                return "No session to continue."
            session = store.load(rows[0]["id"])
        except Exception as e:
            return f"[error] {e}"
        if session is None:
            return "No session to continue."
        return self.adopt_session(session)

    def _cmd_fork(self, arg):
        if arg.strip():
            resumed = self.resume_session(arg.strip())
            if resumed.startswith("[error]") or resumed.startswith("No session"):
                return resumed
        return self.fork_session()

    def fork_session(self) -> str:
        """Continue in a copy of the current session (`--fork`, `/fork`).

        The copy continues *here* — this directory, this provider — which is
        not necessarily where the original ran.
        """
        session = self.session
        if session is None:
            return "Nothing to fork — no session is open."
        store = self._store()
        if store is None:
            return "[error] sessions unavailable (sqlite3 missing)"
        try:
            forked = copy_session(store, session, cwd=self.cwd,
                                  provider=self.provider_name,
                                  model=self.turn.model)
        except Exception as e:
            return f"[error] {e}"
        self.turn.adopt(forked)
        self.turn.last_checkpoint = None
        self.agent.messages = list(forked.messages)
        return (f"Forked {session.id} → {forked.id} "
                f"({len(forked.messages)} messages)")

    def _cmd_rename(self, arg):
        session = self.session or self._open_session()
        if session is None:
            return "No session to rename."
        if not arg.strip():
            return f"Session {session.id[:8]}: {session.title or '(untitled)'}"
        try:
            return f"Renamed to '{session.rename(arg.strip())}'."
        except Exception as e:
            return f"[error] {e}"

    def _cmd_archive(self, arg):
        store = self._store()
        session = self.session
        if arg.strip() and store is not None:
            session = store.load(arg.strip())
        if session is None:
            return "No session to archive."
        try:
            session.archive()
        except Exception as e:
            return f"[error] {e}"
        if session is self.session:
            self.turn.reset()
            self.agent.clear()
        return f"Archived {session.id[:8]}."

    def _cmd_export(self, arg):
        session = self.session
        if session is None:
            return "No session to export."
        target, _, fmt = arg.strip().partition(" ")
        fmt = fmt.strip() or ("json" if target.endswith(".json") else "markdown")
        try:
            text = session.export(fmt)
        except Exception as e:
            return f"[error] {e}"
        if not target:
            return text
        try:
            path = Path(target).expanduser()
            path.write_text(text)
        except OSError as e:
            return f"[error] {e}"
        return f"Exported {len(text)} bytes to {path}"

    def _cmd_compact(self, arg):
        keep = COMPACT_KEEP
        if arg.strip().isdigit():
            keep = max(1, int(arg.strip()))
        session = self.session
        if session is None:
            # No durable session to fold into, so trim in place instead. The
            # reserve is deliberately far below the request-time default: the
            # user asked for room, not for the usual margin.
            from .context import compact_history
            before = len(self.agent.messages)
            self.agent.messages = compact_history(
                self.agent.messages, self.agent.context_window, reserve=0.25,
                provider=self.agent.provider, model=self.agent.model)
            return f"Compacted in memory: {before} → {len(self.agent.messages)} messages."
        try:
            # Without a provider this folds the turns away behind a notice
            # instead of summarising them, which is the opposite of what
            # someone typing /compact is asking for.
            folded = session.compact(keep_last=keep,
                                     provider=self.agent.provider,
                                     model=self.agent.model)
        except Exception as e:
            return f"[error] {e}"
        if not folded:
            return "Nothing to compact."
        self.agent.messages = list(session.messages)
        return f"Folded {folded} messages into a summary."

    def _cmd_undo(self, arg):
        session = self.session
        if session is None:
            return "No session to undo."
        if not self.turn.undo_available:
            # Fail closed: the snapshots this would revert to were never
            # written, so "nothing to undo" would be a lie.
            return "[error] " + self.turn.persistence_notice()
        try:
            restored = session.revert_last()
        except Exception as e:
            return f"[error] {e}"
        if not restored:
            return "Nothing to undo."
        self.agent.messages = list(session.messages)
        return "Reverted:\n" + "\n".join(f"  {p}" for p in restored)

    def _cmd_todos(self, arg):
        todos = self.agent.ctx.todos
        if not todos:
            return "No todos."
        marks = {"completed": "x", "in_progress": ">", "cancelled": "-"}
        return "\n".join(f"  [{marks.get(t['status'], ' ')}] {t['content']}" for t in todos)

    def _cmd_init(self, arg):
        """Write haikode.json, then have the model write AGENTS.md.

        Both halves come from turn.prepare_init() so the TUI can run the same
        /init without calling this blocking path from its curses thread.
        """
        from .turn import prepare_init
        notice, prompt = prepare_init(self.cwd)
        print(_c("  " + notice, RED if notice.startswith("[error]") else DIM))
        self.send(prompt)
        return ""

    # --- dispatch ---------------------------------------------------------

    @staticmethod
    def _safe(call, fallback):
        try:
            return call()
        except Exception:
            return fallback

    def handle_command(self, line: str) -> Optional[str]:
        """Returns display text, or None if the line was not a command."""
        captured = self.quick_capture(line)
        if captured is not None:
            return captured
        if not line.startswith("/"):
            return None
        if self.commands is not None:
            kind, value = self.commands.dispatch(line, self.cwd)
            if kind == "builtin":
                return value or ""
            if kind == "prompt":
                self.send(value)
                return ""
            return f"Unknown command: {value}. Try /help"
        # Fallback when commands.py is unavailable
        name, _, arg = line[1:].partition(" ")
        for candidate, handler, _help in self._builtins():
            if candidate == name:
                return handler(arg.strip())
        return f"Unknown command: {name}. Try /help"

    # --- loop -------------------------------------------------------------

    def run(self):
        self.turn.compose_farewell = (sys.stdin.isatty() and
                                      self.config.data.get("farewell_haiku",
                                                           True))
        if sys.stdout.isatty():
            from .status import startup_haiku, terminal_title
            sys.stdout.write(terminal_title(
                "haikode — %s" % Path(self.cwd).name))
            for line in startup_haiku():
                print(_c(("    " if line.startswith("—") else "  ") + line,
                         DIM))
            print()
        print(_c("haikode", BOLD) + " — AI coding agent for Haiku OS")
        print(f"Provider: {self.provider_name}  Model: {self.model or '(default)'}  "
              f"Agent: {self.agent.agent_name}  Tools: {len(self.agent.tools)}")
        print(_c("/help for commands, @file to attach a file, # to remember, "
                 "Ctrl-C to interrupt\n", DIM))

        while True:
            try:
                line = input(_c("> ", BOLD + CYAN)).strip()
            except EOFError:
                print()
                print(self._farewell())
                return
            except KeyboardInterrupt:
                print("\n(use /exit to quit)")
                continue
            if not line:
                continue
            try:
                handled = self.handle_command(line)
            except SystemExit:
                return
            if handled is not None:
                if handled:
                    print(handled)
                continue
            self.send(line)


# --- the machine-readable front end --------------------------------------
#
# The schema below is the contract `--json` promises. It is duplicated in the
# README and in `haikode --help`; keep the three in step.
#
# Output is JSON Lines: exactly one JSON object per line, flushed as it
# happens, so a reader can consume it with `for line in proc.stdout`.
#
# Every event carries:
#   type     str    the event kind, one of the names below
#   time     float  unix seconds, when the event was emitted
#   session  str    the durable session id, "" until a session is opened
#
# Per-kind fields:
#   run          prompt, provider, model, agent, cwd     a turn is starting
#   notice       text                                    startup/resume message
#   attach       paths[]                                 @-mentions expanded
#   memory       text                                    a "#" quick capture
#   text         text                                    a chunk of the answer
#   reasoning    text                                    a chunk of reasoning
#   tool         name, args{}                            the model called a tool
#   tool_result  name, title, output, metadata{}         the tool returned
#   tool_denied  name, reason                            the permission layer refused
#   tool_error   name, error                             the tool raised
#   permission   key, title, patterns[], decision        asked, and how it was answered
#   limit        steps                                   the step limit was hit
#   error        source, message, kind, retryable        source: provider|turn
#   usage        input, output, reasoning, cache_read,
#                cache_write, total, cost                tokens for the turn
#   command      command, text                           a slash command's output
#   done         text, interrupted, error, denied[],
#                limited, persisted, exit                always last in a turn
#
# `done.exit` is the exit code this turn alone deserves; the process exits with
# the worst code across every turn it ran.

JSON_EVENTS = ("run", "notice", "attach", "memory", "text", "reasoning", "tool",
               "tool_result", "tool_denied", "tool_error", "permission",
               "limit", "error", "usage", "command", "done")


class JSONREPL(REPL):
    """`--json`: the same turn, rendered as JSON Lines instead of text.

    Only the renderer differs. Sessions, checkpoints, permissions, @-mentions
    and slash commands are the base class's, which is the point: a scripted
    run must not be a second, subtly different agent.
    """

    def __init__(self, *args, stream=None, **kwargs):
        # Bound before super().__init__ so nothing can emit to a stream that
        # does not exist yet, and captured as an object so redirect_stdout()
        # around a command cannot swallow the event stream.
        self.stream = stream if stream is not None else sys.stdout
        super().__init__(*args, **kwargs)
        # A permission nobody can answer is still worth reporting: a script
        # has to be able to see WHICH rule stopped the run, not merely that a
        # tool failed. Emitting and then rejecting keeps the headless default
        # (deny) exactly as it was — this asker never approves anything.
        self.permissions.asker = self._ask_permission

    # --- emitting --------------------------------------------------------

    def emit(self, event: str, **fields: Any) -> Dict[str, Any]:
        session = self.turn.session
        record: Dict[str, Any] = {
            "type": event,
            "time": round(time.time(), 3),
            "session": getattr(session, "id", "") or "",
        }
        record.update(fields)
        try:
            self.stream.write(json.dumps(record, default=str) + "\n")
            self.stream.flush()
        except (OSError, ValueError):
            # A closed pipe (`haikode --json … | head`) is not a reason to
            # abandon the turn: the work is already done and the session write
            # still has to happen.
            pass
        return record

    def _ask_permission(self, request) -> str:
        self.emit("permission", key=request.key, title=request.title,
                  patterns=list(request.patterns), decision="reject")
        return "reject"

    # --- the renderer the base class calls -------------------------------

    def _on_text(self, text: str) -> None:
        self.emit("text", text=text)

    def _on_attach(self, paths: List[str]) -> None:
        self.emit("attach", paths=list(paths))

    def _on_event(self, kind: str, payload) -> None:
        if kind == "reasoning":
            self.emit("reasoning", text=str(payload))
        elif kind == "tool":
            self.emit("tool", name=payload.get("name", ""),
                      args=payload.get("args") or {})
        elif kind == "tool_result":
            self.emit("tool_result", name=payload.get("name", ""),
                      title=payload.get("title", ""),
                      output=payload.get("output", ""),
                      metadata=payload.get("metadata") or {})
        elif kind == "tool_denied":
            self.emit("tool_denied", name=payload.get("name", ""),
                      reason=payload.get("reason", ""))
        elif kind == "tool_error":
            self.emit("tool_error", name=payload.get("name", ""),
                      error=payload.get("error", ""))
        elif kind == "limit":
            self.emit("limit", steps=payload.get("steps", 0))
        elif kind == "error":
            failure = payload if isinstance(payload, dict) else {"message": str(payload)}
            self.emit("error", source="provider",
                      message=str(failure.get("message") or ""),
                      kind=str(failure.get("kind") or "unknown"),
                      retryable=bool(failure.get("retryable")))

    def _report_persistence(self) -> None:
        notice = self.turn.persistence_notice()
        if notice and notice != self._reported_persistence:
            self.emit("notice", text=notice)
        self._reported_persistence = notice

    # --- a turn ----------------------------------------------------------

    def send(self, message: str) -> str:
        self.emit("run", prompt=message, provider=self.provider_name,
                  model=getattr(self.agent, "model", "") or "",
                  agent=getattr(self.agent, "agent_name", "") or "",
                  cwd=self.cwd)
        result = self._run(message)
        if result.captured:
            self.emit("memory", text=result.captured)
        else:
            self._emit_usage()
            if result.error:
                # Emitted next to the structured provider error rather than
                # instead of it: this one is what actually ended the turn.
                self.emit("error", source="turn", message=result.error,
                          kind="turn", retryable=False)
        self._report_persistence()
        self.emit("done", text=result.text, interrupted=result.interrupted,
                  error=result.error, denied=list(self._denied),
                  limited=self._limited, persisted=result.persisted,
                  exit=self.turn_exit_code(result, bool(self._denied),
                                           self._limited))
        return result.text

    def _emit_usage(self) -> None:
        tracker = getattr(self.agent, "usage", None)
        usage = getattr(tracker, "run", None)
        if usage is None:
            return
        self.emit("usage", input=usage.input_tokens, output=usage.output_tokens,
                  reasoning=usage.reasoning_tokens, cache_read=usage.cache_read,
                  cache_write=usage.cache_write, total=usage.total,
                  cost=usage.cost)

    # --- the stdin loop --------------------------------------------------

    def command(self, line: str) -> bool:
        """Run `line` as a slash command, emitting whatever it printed.

        Commands print (/init writes a notice, the fallback path prints
        directly), and a bare print into the event stream would corrupt it —
        so their stdout is captured and shipped as one `command` event.

        A command that runs a turn (/init) emits that turn's events first and
        its own `command` event after: the capture only ends when the handler
        returns, while the turn writes to the real stream as it happens.
        """
        if not line.startswith("/"):
            return False
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            text = self.handle_command(line)
        body = ((text or "") + buffer.getvalue()).rstrip()
        self.emit("command", command=line, text=body)
        return True

    def run(self) -> None:
        """One prompt per line of stdin — the scripting loop.

        No banner and no prompt string: everything this writes is an event, so
        the reader on the other end of the pipe never has to skip anything.
        """
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                if not self.command(line):
                    self.send(line)
            except SystemExit:          # /exit
                return
            except KeyboardInterrupt:
                self.emit("done", text="", interrupted=True, error="",
                          denied=[], limited=False, persisted=False,
                          exit=EXIT_INTERRUPTED)
                self._worst = EXIT_INTERRUPTED
                return


def one_shot(config: Config, prompt: str, provider: str = "",
             cwd: str = ".", auto_approve: bool = False,
             agent_name: str = "", model: str = "",
             print_logs: bool = False, as_json: bool = False,
             yolo: bool = False) -> int:
    """Run one prompt and return the exit code it earned. See EXIT_* above."""
    factory = JSONREPL if as_json else REPL
    repl = factory(config, provider=provider, cwd=cwd,
                   auto_approve=auto_approve, yolo=yolo,
                   agent_name=agent_name,
                   model=model, print_logs=print_logs)
    repl.send(prompt)
    return repl.exit_code()
