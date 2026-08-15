"""
The turn lifecycle, owned in exactly one place.

A *turn* is everything between the user pressing enter and the agent going
quiet: quick-capture, @-mention expansion, opening (or attaching to) the
durable session, taking a revert checkpoint, running the agent, persisting the
messages it produced and snapshotting the files it touched.

All of that used to live inside REPL.send(), so the curses TUI — the default
front end — called agent.run() directly and wrote nothing at all: no session
rows, no snapshots, no checkpoints. /undo, the session counts and the resume
dialog described state the primary interface never created. This module is the
one run_turn() every front end goes through, so that cannot drift again.

Nothing here prints and nothing here imports curses: callbacks go in, a
TurnResult comes out, and each front end decides what to show. Persistence
failures are reported rather than swallowed — a front end that cannot see them
cannot stop advertising an undo that will not work.
"""

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

# --- command execution classes -------------------------------------------
#
# A front end that owns the terminal (curses in raw mode) has exactly one
# thread it may not block, so it has to know what a command is going to do
# before it runs it. These four classes are that answer, and they live here
# rather than in tui.py so the REPL, the TUI and the tests agree on them.

SYNC = "sync"      # local and instant: safe to run on the UI thread
ASYNC = "async"    # network or a subprocess (the Haiku keystore): worker
MODAL = "modal"    # needs the user to type something: a front-end modal
TURN = "turn"      # expands into a prompt: goes through run_turn()

# Every one of these either uses the network, invokes a subprocess, rebuilds
# the agent (which re-resolves credentials), or asks the config layer for a key
# — and on Haiku a key lookup shells out to the BKeyStore helper, up to five
# seconds per provider.
ASYNC_COMMANDS = frozenset({
    "model", "models", "provider", "providers", "keys", "logout", "status",
    "update",
})
# /login prompts for a secret. In a curses front end stdin belongs to the
# screen, so the prompt would be invisible and the app would look hung.
MODAL_COMMANDS = frozenset({"login"})
# /init writes a config file and then runs a full agent pass; the agent pass
# must be a real turn, with the front end's streaming and permission modal.
TURN_COMMANDS = frozenset({"init"})


def command_name(line: str) -> str:
    """The bare command name in `line`: '/model gpt-4o' -> 'model'."""
    text = (line or "").strip()
    if text.startswith("/"):
        text = text[1:]
    return text.split(None, 1)[0].lower() if text.split() else ""


def command_mode(line: str, custom: bool = False) -> str:
    """How a front end must execute `line`: SYNC, ASYNC, MODAL or TURN.

    `custom` marks a user-defined command from a command file: those render to
    a prompt, so they are always a turn no matter what they are called.
    """
    if custom:
        return TURN
    name = command_name(line)
    if name in TURN_COMMANDS:
        return TURN
    if name in MODAL_COMMANDS:
        return MODAL
    if name in ASYNC_COMMANDS:
        return ASYNC
    return SYNC


# --- the result of one turn ----------------------------------------------


@dataclass
class TurnResult:
    """What a turn produced, in a form no front end has to guess at."""

    text: str = ""
    # Quick-capture confirmation. Non-empty means the line was a memory, not a
    # prompt, and the agent was never run.
    captured: str = ""
    attached: List[str] = field(default_factory=list)
    interrupted: bool = False
    error: str = ""
    session_id: str = ""
    checkpoint: Optional[int] = None
    persisted: bool = False
    # Non-empty when this conversation is not on disk. Front ends must show it
    # and must stop offering undo while it holds.
    persistence_error: str = ""

    @property
    def ran(self) -> bool:
        """True when the agent actually ran (i.e. this was not a capture)."""
        return not self.captured


def prepare_init(cwd: str) -> Tuple[str, str]:
    """(notice, prompt) for /init, without running anything.

    The config file comes first because it is instant and cannot fail halfway;
    the AGENTS.md pass is a full agent run and belongs in a turn, which is why
    this returns the prompt rather than sending it.
    """
    from .projectconfig import init_project_config
    try:
        notice = "wrote %s" % init_project_config(cwd)
    except FileExistsError as exc:
        notice = str(exc)
    except (OSError, ValueError) as exc:
        notice = "[error] %s" % exc
    try:
        from .commands import generate_agents_md_prompt
        prompt = generate_agents_md_prompt(cwd)
    except Exception:
        prompt = ("Analyse this project and write an AGENTS.md at its root describing "
                  "the build/test commands, code style and conventions a coding agent "
                  "needs to work here. Keep it under 40 lines.")
    return notice, prompt


class TurnController:
    """Owns the session and the turn lifecycle for every front end.

    One instance is shared by the REPL and the TUI (main.py hands the same
    object to both), which is what makes `haikode --continue`, /undo and the
    session list describe the same conversation whichever front end is on
    screen.
    """

    def __init__(self, cwd: str = ".", provider_name: str = "",
                 model: str = "", store_factory: Optional[Callable[[], Any]] = None):
        self.cwd = str(cwd)
        self.provider_name = provider_name
        self.model = model
        self.session = None
        self._farewell_started = False
        # The model-written exit haiku and who wrote it. It is only composed
        # when the user explicitly asks with /farewell (or enables that exit
        # action); ordinary turns never spend a provider request on it.
        self.farewell_poem = None
        self.farewell_poet = ""
        # Short local display title for the terminal tab and session list.
        self.display_title = ""
        # Only an interactive front end turns this on (REPL loop, TUI).
        # It controls local title preparation only; it never calls a model.
        self.compose_farewell = False
        # Sticky: set by any failure that means this conversation is not on
        # disk, cleared only by a turn that persisted cleanly.
        self.persistence_error = ""
        self.last_checkpoint: Optional[int] = None
        self._store_factory = store_factory
        self._store = None
        # Turns run on the TUI's worker thread while /sessions reads the store
        # from the main one, so opening it is guarded.
        self._lock = threading.RLock()

    # --- the session -----------------------------------------------------

    def store(self):
        """The session database, opened once, or None when it is unavailable.

        A fresh SessionStore per call reopens the file and replays the schema
        every time, and the session picker asks on every keystroke.
        """
        with self._lock:
            if self._store is not None:
                return self._store
            try:
                if self._store_factory is not None:
                    self._store = self._store_factory()
                else:
                    from .session import SessionStore
                    self._store = SessionStore()
            except Exception as exc:      # sqlite3 missing, unwritable home, ...
                self._fail("sessions unavailable: %s" % exc)
                return None
            return self._store

    def close(self) -> None:
        with self._lock:
            store = self._store
            self._store = None
        if store is not None:
            try:
                store.close()
            except Exception:
                pass

    def open_session(self):
        """The durable session for this conversation, created on first use."""
        with self._lock:
            if self.session is not None:
                return self.session
            store = self.store()
            if store is None:
                return None
            try:
                self.session = store.new_session(self.cwd, self.provider_name,
                                                 self.model)
            except Exception as exc:
                self.session = None
                self._fail("could not open a session: %s" % exc)
            return self.session

    def adopt(self, session) -> None:
        """Continue an existing session (/resume, --continue, --session)."""
        with self._lock:
            self.session = session
            if session is not None:
                self.persistence_error = ""

    def reset(self) -> None:
        """Drop the session so the next turn starts a new one (/new).

        `persistence_error` deliberately survives: it describes the machine
        (no sqlite3, unwritable home), not the conversation, so clearing it
        here would silently re-advertise an undo that still cannot work.
        """
        with self._lock:
            self.session = None
            self.last_checkpoint = None

    @property
    def undo_available(self) -> bool:
        """Fail closed: no session, or a failed write, means no undo."""
        return self.session is not None and not self.persistence_error

    def persistence_notice(self) -> str:
        """One line for a front end to show while persistence is broken."""
        if not self.persistence_error:
            return ""
        return "session not saved - undo unavailable (%s)" % self.persistence_error

    def _fail(self, reason: str) -> None:
        self.persistence_error = reason

    # --- one turn --------------------------------------------------------

    def quick_capture(self, agent, line: str) -> str:
        """Claude Code's "#" convention: a leading # saves a memory.

        Returns the confirmation to display, or "" when the line is an
        ordinary prompt — so a caller can use it as "was this consumed?".
        """
        if not str(line or "").lstrip().startswith("#"):
            return ""
        from .memory import MemoryStore, parse_quick_capture
        parsed = parse_quick_capture(line)
        if parsed is None:
            return ""
        text, scope = parsed
        try:
            memory = MemoryStore(self.cwd).write(text, scope=scope)
        except OSError as exc:
            return "[error] could not save memory: %s" % exc
        try:
            agent.refresh_memory()
        except Exception:
            pass
        return "Remembered as '%s' (%s): %s" % (memory.name, memory.scope,
                                                memory.summary())

    def expand(self, message: str) -> Tuple[str, List[str]]:
        """@-mentions become attached file contents; failures leave the text."""
        try:
            from .commands import expand_mentions
            expanded, paths = expand_mentions(message, self.cwd)
            return expanded, list(paths)
        except Exception:
            return message, []

    def run_turn(self, agent, message: str,
                 on_text: Optional[Callable[[str], None]] = None,
                 on_event: Optional[Callable[[str, Any], None]] = None,
                 on_attach: Optional[Callable[[List[str]], None]] = None,
                 expand_mentions: bool = True) -> TurnResult:
        """Run one complete turn and return what it produced.

        The persistence half runs in a finally block on purpose: an
        interrupted or failed run still leaves messages the user can see, and
        the transcript and revert snapshots must describe what is on screen.
        """
        result = TurnResult()
        captured = self.quick_capture(agent, message)
        if captured:
            result.captured = captured
            result.persistence_error = self.persistence_error
            return result

        self._restore_context_checkpoint(agent)

        original = message
        if expand_mentions:
            message, result.attached = self.expand(message)
        if result.attached and on_attach is not None:
            try:
                on_attach(list(result.attached))
            except Exception:
                pass

        # Cleared before the run so the snapshot describes this turn only.
        try:
            agent.ctx.modified_files.clear()
        except Exception:
            pass

        try:
            result.text = agent.run(message, on_text=on_text, on_event=on_event)
        except KeyboardInterrupt:
            try:
                agent.abort()
            except Exception:
                pass
            result.interrupted = True
        except Exception as exc:
            # Name the provider in the headline: "ProviderFailure" alone
            # never said WHICH provider failed, and a session may use
            # several (subagents can pin their own).
            label = type(exc).__name__
            source = str(getattr(exc, "error", {}).get("provider", "")
                         if isinstance(getattr(exc, "error", None), dict)
                         else "")
            if source:
                label = "%s(%s)" % (label, source)
            result.error = "%s: %s" % (label, exc)
            try:
                from .failures import record_failure
                record_failure(label, str(exc),
                               getattr(exc, "error", None))
            except Exception:
                pass
        finally:
            self._persist(agent, original, result)
        if result.text and not result.error:
            self._prepare_farewell(agent)
        return result

    def _restore_context_checkpoint(self, agent) -> None:
        """Hydrate one agent from the session's latched provider projection.

        This is shared by REPL, TUI and desktop.  In particular, the desktop
        starts a fresh Python process for every Send, so an in-memory-only
        compaction latch would still buy a new summary on every user turn.
        Failure is deliberately non-fatal: the raw transcript remains the
        source of truth and can always be compacted again.
        """
        session = self.session
        session_id = str(getattr(session, "id", "") or "")
        if not session_id:
            return
        if str(getattr(agent, "_context_session_id", "") or "") == session_id:
            return
        try:
            checkpoint = session.load_context_checkpoint()
            if checkpoint is not None:
                history, raw_count = checkpoint
                agent.restore_context_checkpoint(history, raw_count)
        except Exception:
            return
        try:
            agent._context_session_id = session_id
        except Exception:
            pass

    def _prepare_farewell(self, agent) -> None:
        """Derive the interactive display title locally after the first turn.

        This used to launch a hidden main-model request for a cosmetic 3-5
        word title. A one-turn interactive session therefore made two provider
        calls, the second was absent from usage accounting, and it could race
        the user's next turn. Session.auto_title already has the subject; a
        deterministic local shortening is enough for a tab label.
        """
        if not self.compose_farewell:
            return
        if self._farewell_started:
            return
        self._farewell_started = True
        subject = ""
        session = self.session
        if session is not None:
            subject = str(getattr(session, "title", "") or "")
        if not subject:
            for message in list(getattr(agent, "messages", None) or []):
                if getattr(message, "role", "") == "user":
                    subject = str(getattr(message, "content", "") or "")
                    break
        words = " ".join(subject.split()).split()[:5]
        title = " ".join(words).strip(" \t\r\n.,:;!?\"'")[:48]
        if title:
            self.display_title = title

    def compose_farewell_now(self, agent) -> bool:
        """Write this session's exit haiku, on demand, with full context.

        The user asked for it by typing /farewell, so blocking briefly is
        the contract — and because it runs from the prompt, no turn is in
        flight and the stream has the pipe to itself. The material is the
        whole conversation's shape: the title and the user's own prompts.
        Returns True when a poem was composed; the curated collection
        covers False, as always.
        """
        from .schema import Msg
        from .status import validated_haiku
        subject = ""
        session = self.session
        if session is not None:
            subject = str(getattr(session, "title", "") or "")
            asked = [str(m.content or "") for m in
                     (getattr(session, "messages", None) or [])
                     if getattr(m, "role", "") == "user"][-4:]
            if asked:
                subject = "; ".join([subject] + [a[:80] for a in asked])[:400]
        prompt = (
            "Write exactly one haiku: three lines of roughly 5, 7 and 5 "
            "syllables. Subject: a calm, slightly wry farewell to this "
            "coding session%s. Technology-flavoured, English, lower case. "
            "Reply with the three lines only — no title, no quotes, no "
            "commentary." % (", which was about: %s" % subject
                             if subject else ""))
        try:
            parts = []
            for chunk in agent.provider.stream(
                    [Msg(role="user", content=prompt)], [],
                    agent.model, 96):
                if getattr(chunk, "usage", None):
                    try:
                        agent._record_hidden_usage(chunk.usage)
                    except Exception:
                        pass
                if getattr(chunk, "text", ""):
                    parts.append(chunk.text)
            poem = validated_haiku("".join(parts))
        except Exception:
            return False
        if not poem:
            return False
        self.farewell_poem = poem
        self.farewell_poet = str(agent.model or "")
        return True

    def _auto_rename(self, title: str) -> None:
        """Give the stored session the model's title — unless the user did.

        The stored title starts as the raw first prompt. Only that exact
        value is replaced, so a /rename the user typed always wins, and a
        second composer run cannot rename twice.
        """
        session = self.session
        if session is None:
            return
        try:
            current = str(getattr(session, "title", "") or "")
            raw_first = current == (session.messages[0].content or "")[:len(current)]                 if getattr(session, "messages", None) else True
            if raw_first and current != title:
                session.rename(title)
        except Exception:
            pass

    def _persist(self, agent, title_hint: str, result: TurnResult) -> None:
        """Write the turn to the session, opening one only if there is anything
        to write.

        The checkpoint is taken here rather than before the run: `_seq` only
        moves when a message is appended, and nothing appends during a run, so
        this is the same revert point — and it means a turn that produced
        nothing (a front end handing us a stub agent, a run that died before
        the first message) leaves no empty session behind.
        """
        # Agent.messages is a stable list once run() returns. Copying the whole
        # lossless transcript here made persistence O(total history) before it
        # wrote only the small new tail; keep the sequence and index that tail
        # directly. Non-list test doubles still degrade safely to a tuple.
        messages = getattr(agent, "messages", None) or ()
        if not hasattr(messages, "__getitem__"):
            messages = tuple(messages)
        modified = dict(getattr(getattr(agent, "ctx", None), "modified_files",
                                None) or {})
        session = self.session
        baseline = len(getattr(session, "messages", ()) or ()) if session else 0
        if session is None and not messages and not modified:
            result.persistence_error = self.persistence_error
            return
        if session is None:
            session = self.open_session()
        if session is None:
            result.persistence_error = self.persistence_error
            return

        trouble = ""
        try:
            result.checkpoint = session.checkpoint()
            self.last_checkpoint = result.checkpoint
            session.auto_title(title_hint)
        except Exception as exc:
            trouble = "checkpoint failed: %s" % exc
        try:
            for index in range(baseline, len(messages)):
                session.append(messages[index])
            from .session import capture_modified
            capture_modified(session, getattr(agent, "ctx", None))
        except Exception as exc:
            trouble = trouble or "session not saved: %s" % exc
        else:
            result.persisted = not trouble

        # A context checkpoint is an optimisation, not the transcript.  A
        # failed checkpoint write must not claim the raw turn or undo data was
        # lost; leave it dirty so a later clean turn can retry the small upsert.
        if not trouble:
            try:
                checkpoint = agent.context_checkpoint()
                if checkpoint is not None:
                    history, raw_count = checkpoint
                    if session.save_context_checkpoint(history, raw_count):
                        agent._llm_checkpoint_dirty = False
                agent._context_session_id = str(getattr(session, "id", "") or "")
            except Exception:
                pass

        result.session_id = getattr(session, "id", "") or ""
        # Sticky until a turn writes cleanly: a front end must keep warning
        # for as long as the conversation is not really on disk.
        self.persistence_error = trouble
        result.persistence_error = trouble
