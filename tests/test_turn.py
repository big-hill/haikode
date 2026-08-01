"""
The turn lifecycle: does the DEFAULT front end actually write anything?

Every test here exists because an audit reproduced the opposite. The whole
lifecycle — mention expansion, session, checkpoint, persistence, snapshots —
used to live inside REPL.send(), so the curses TUI ran agent.run() directly
and left no session rows, no snapshots and no checkpoints behind. /undo, the
session counts and the resume dialog all described state the primary interface
had never created, `--continue` was erased by TUI startup, a prompt typed
during a run was silently discarded, and a failed write was swallowed.

So these assert connections and behaviour that only hold when both front ends
go through TurnController.run_turn().
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode import main as main_mod  # noqa: E402
from haikode import repl as repl_mod  # noqa: E402
from haikode import session as session_mod  # noqa: E402
from haikode import tui as tui_module  # noqa: E402
from haikode import turn as turn_mod  # noqa: E402
from haikode.config import Config  # noqa: E402
from haikode.schema import Msg  # noqa: E402
from haikode.turn import ASYNC, MODAL, SYNC, TURN, TurnController  # noqa: E402


class StubCtx:
    """Stands in for a ToolContext: the turn only reads modified_files."""

    def __init__(self):
        self.modified_files = {}
        self.read_files = set()
        self.todos = []


class StubAgent:
    """Stands in for Agent: streams a reply and reports one edited file."""

    def __init__(self, edited=None, reply="done", boom=None):
        self.messages = []
        self.ctx = StubCtx()
        self.tokens = {"input": 3, "output": 4}
        self.model = "stub-model"
        self.warnings = []
        self.registry = None
        self.calls = []
        self.aborted = False
        self.edited = edited
        self.reply = reply
        self.boom = boom

    def run(self, message, on_text=None, on_event=None):
        self.calls.append(message)
        self.messages.append(Msg(role="user", content=message))
        if self.boom is not None:
            raise self.boom
        self.messages.append(Msg(role="assistant", content=self.reply))
        if self.edited is not None:
            self.ctx.modified_files[str(self.edited)] = "before the run\n"
        if on_text:
            on_text(self.reply)
        return self.reply

    def abort(self):
        self.aborted = True

    def clear(self):
        self.messages = []

    def refresh_memory(self):
        pass


class TurnTestCase(unittest.TestCase):
    """A project directory and a private session database."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-turn-")
        self.home = tempfile.mkdtemp(prefix="haikode-turn-home-")
        self.db = Path(self.home, "sessions.db")
        self._patch = patch.object(session_mod, "default_db_path", lambda: self.db)
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def controller(self):
        controller = TurnController(cwd=self.dir, provider_name="p", model="m")
        self.addCleanup(controller.close)
        return controller

    def make_config(self):
        config = Config(path=str(Path(self.home, "config.json")))
        config.data["providers"] = {
            "p": {"base_url": "http://127.0.0.1:1", "model": "m",
                  "requires_key": False}}
        config.data["default_provider"] = "p"
        return config

    def make_repl(self, agent=None):
        """A real REPL whose agent is a stub, so send() runs the real path."""
        repl = repl_mod.REPL(self.make_config(), cwd=self.dir)
        self.addCleanup(repl.turn.close)
        repl.agent = agent if agent is not None else StubAgent()
        return repl

    def rows(self):
        store = session_mod.SessionStore()
        try:
            return store.list_sessions(cwd=self.dir)
        finally:
            store.close()

    def only_session(self):
        rows = self.rows()
        self.assertEqual(len(rows), 1, rows)
        store = session_mod.SessionStore()
        self.addCleanup(store.close)
        return store.load(rows[0]["id"])


# --------------------------------------------------------------------------
# 1. both front ends persist
# --------------------------------------------------------------------------


class BothFrontEndsPersistATurn(TurnTestCase):
    """The blocker: only the fallback REPL was writing anything."""

    def _edited_file(self):
        path = Path(self.dir, "edited.txt")
        path.write_text("after the run\n")
        return path

    def test_run_turn_writes_messages_and_snapshots(self):
        edited = self._edited_file()
        controller = self.controller()
        agent = StubAgent(edited=edited)
        result = controller.run_turn(agent, "fix the parser")

        self.assertTrue(result.persisted)
        self.assertEqual(result.persistence_error, "")
        session = self.only_session()
        self.assertEqual([m.content for m in session.messages],
                         ["fix the parser", "done"])
        self.assertEqual(session.snapshots(result.checkpoint or 0),
                         {os.path.realpath(str(edited)): "before the run\n"})

    def test_the_tui_turn_path_persists_exactly_like_the_repl(self):
        """The TUI's worker body must go through the same controller."""
        edited = self._edited_file()
        ui = tui_module.TUI(lambda: None, config=None, cwd=self.dir,
                            turn=self.controller())
        agent = StubAgent(edited=edited)
        ui.agent = agent
        token = object()
        ui._run_token = token
        ui._run_agent("fix the parser", agent, token)
        ui._pump()

        session = self.only_session()
        self.assertEqual([m.content for m in session.messages],
                         ["fix the parser", "done"])
        self.assertTrue(session.snapshots())
        self.assertEqual(ui.turn.session.id, session.id)
        self.assertTrue(ui.turn.undo_available)

    def test_the_repl_send_path_persists_exactly_like_the_tui(self):
        edited = self._edited_file()
        repl = self.make_repl(StubAgent(edited=edited))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            repl.send("fix the parser")

        session = self.only_session()
        self.assertEqual([m.content for m in session.messages],
                         ["fix the parser", "done"])
        # And the revert point is real: undo restores the pre-run content.
        self.assertEqual(session.revert_last(),
                         [os.path.realpath(str(edited))])
        self.assertEqual(edited.read_text(), "before the run\n")
        self.assertTrue(repl.turn.undo_available)

    def test_a_second_turn_appends_rather_than_reopening(self):
        controller = self.controller()
        agent = StubAgent()
        controller.run_turn(agent, "one")
        controller.run_turn(agent, "two")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual([m.content for m in self.only_session().messages],
                         ["one", "done", "two", "done"])

    def test_mentions_are_expanded_and_reported_once(self):
        Path(self.dir, "notes.md").write_text("the parser drops commas")
        controller = self.controller()
        agent = StubAgent()
        seen = []
        result = controller.run_turn(agent, "look at @notes.md",
                                     on_attach=seen.append)
        self.assertIn("the parser drops commas", agent.calls[0])
        self.assertEqual(len(seen), 1)
        self.assertTrue(result.attached)

    def test_an_interrupted_turn_still_leaves_a_transcript(self):
        controller = self.controller()
        agent = StubAgent(boom=KeyboardInterrupt())
        result = controller.run_turn(agent, "long job")
        self.assertTrue(result.interrupted)
        self.assertTrue(agent.aborted)
        self.assertEqual([m.content for m in self.only_session().messages],
                         ["long job"])

    def test_a_failed_run_is_reported_not_raised(self):
        controller = self.controller()
        agent = StubAgent(boom=RuntimeError("provider exploded"))
        result = controller.run_turn(agent, "hello")
        self.assertIn("provider exploded", result.error)
        self.assertEqual([m.content for m in self.only_session().messages],
                         ["hello"])

    def test_a_turn_that_produced_nothing_leaves_no_empty_session(self):
        """A stub front end must not litter the user's session list."""
        controller = self.controller()

        class Nothing:
            messages = []

            def run(self, *_args, **_kwargs):
                raise AttributeError("no agent here")

        controller.run_turn(Nothing(), "hello")
        self.assertEqual(self.rows(), [])


# --------------------------------------------------------------------------
# 2. --continue survives TUI startup
# --------------------------------------------------------------------------


class ContinueSurvivesStartup(TurnTestCase):
    """`haikode --continue` resumed, then TUI startup rebuilt and erased it."""

    def setUp(self):
        super().setUp()
        self.config = Config(path=str(Path(self.home, "config.json")))
        self.config.data["providers"] = {
            "p": {"base_url": "http://127.0.0.1:1", "model": "m",
                  "requires_key": False}}
        self.config.data["default_provider"] = "p"

    def _seed(self):
        store = session_mod.SessionStore()
        self.addCleanup(store.close)
        session = store.new_session(self.dir, "p", "m", "Earlier work")
        session.append(Msg(role="user", content="earlier question"))
        return session

    def _resumed_repl(self):
        args = main_mod.build_parser().parse_args(["--continue"])
        with redirect_stdout(io.StringIO()):
            return main_mod.build_repl(self.config, args, self.dir)

    def test_the_tui_is_handed_the_resumed_agent_and_session(self):
        session = self._seed()
        repl = self._resumed_repl()
        captured = {}
        with patch.object(tui_module, "run_tui", lambda **kw: captured.update(kw)):
            main_mod._start_tui(repl, self.config, self.dir)
        self.assertIs(captured["agent"], repl.agent)
        self.assertIs(captured["turn"], repl.turn)
        self.assertEqual(captured["turn"].session.id, session.id)

    def test_startup_adopts_that_agent_instead_of_rebuilding_it(self):
        self._seed()
        repl = self._resumed_repl()
        captured = {}
        with patch.object(tui_module, "run_tui", lambda **kw: captured.update(kw)):
            main_mod._start_tui(repl, self.config, self.dir)
        ui = tui_module.TUI(captured["agent_factory"], self.config, self.dir,
                            on_command=captured["on_command"],
                            agent=captured["agent"], turn=captured["turn"])
        # This is the call _attach makes; before the fix it went straight to
        # the factory, which runs new_conversation() and wipes the resumption.
        agent = ui._startup_agent()
        self.assertIs(agent, repl.agent)
        self.assertEqual([m.content for m in agent.messages],
                         ["earlier question"])

    def test_the_command_layer_is_reachable_for_classification(self):
        """The TUI feature-detects the registry behind on_command: without it
        the palette lists no slash commands and every custom command falls
        back to the blocking send() path."""
        self._seed()
        repl = self._resumed_repl()
        captured = {}
        with patch.object(tui_module, "run_tui", lambda **kw: captured.update(kw)):
            main_mod._start_tui(repl, self.config, self.dir)
        ui = tui_module.TUI(captured["agent_factory"], self.config, self.dir,
                            on_command=captured["on_command"])
        self.assertIs(ui._command_registry(), repl.commands)
        self.assertIsNotNone(ui._build_palette().get("cmd.undo"))

    def test_the_factory_is_still_what_new_uses(self):
        self._seed()
        repl = self._resumed_repl()
        captured = {}
        with patch.object(tui_module, "run_tui", lambda **kw: captured.update(kw)):
            main_mod._start_tui(repl, self.config, self.dir)
        self.assertEqual(captured["agent_factory"]().messages, [])

    def test_a_turn_after_resuming_appends_to_the_resumed_session(self):
        session = self._seed()
        repl = self._resumed_repl()
        repl.agent = StubAgent()
        repl.agent.messages = list(session.messages)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            repl.send("and now this")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual([m.content for m in self.only_session().messages],
                         ["earlier question", "and now this", "done"])


# --------------------------------------------------------------------------
# 3. a persistence failure is visible
# --------------------------------------------------------------------------


class BrokenStore:
    def __init__(self, *_args, **_kwargs):
        raise RuntimeError("no sqlite3 here")


class PersistenceFailureIsVisible(TurnTestCase):
    """Swallowing this is how both front ends offered an undo that could not
    work; the user only found out when nothing was reverted."""

    def test_a_store_that_cannot_open_marks_the_controller(self):
        controller = self.controller()
        with patch.object(session_mod, "SessionStore", BrokenStore):
            result = controller.run_turn(StubAgent(), "hello")
        self.assertIn("no sqlite3 here", result.persistence_error)
        self.assertFalse(result.persisted)
        self.assertFalse(controller.undo_available)
        self.assertIn("undo unavailable", controller.persistence_notice())

    def test_a_failed_append_marks_the_controller(self):
        controller = self.controller()
        with patch.object(session_mod.Session, "append",
                          side_effect=OSError("disk full")):
            result = controller.run_turn(StubAgent(), "hello")
        self.assertIn("disk full", result.persistence_error)
        self.assertFalse(controller.undo_available)

    def test_a_clean_turn_clears_it_again(self):
        controller = self.controller()
        with patch.object(session_mod.Session, "append",
                          side_effect=OSError("disk full")):
            controller.run_turn(StubAgent(), "hello")
        self.assertTrue(controller.persistence_error)
        controller.run_turn(StubAgent(), "again")
        self.assertEqual(controller.persistence_error, "")
        self.assertTrue(controller.undo_available)

    def test_the_repl_says_so_instead_of_staying_silent(self):
        repl = self.make_repl()
        stderr = io.StringIO()
        with patch.object(session_mod, "SessionStore", BrokenStore):
            with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                repl.send("hello")
        self.assertIn("undo unavailable", stderr.getvalue())

    def test_the_repl_help_stops_advertising_undo(self):
        repl = self.make_repl()
        self.assertNotIn("/undo is unavailable", repl._cmd_help(""))
        repl.turn.persistence_error = "session not saved: disk full"
        self.assertIn("/undo is unavailable", repl._cmd_help(""))

    def test_the_repl_refuses_to_pretend_undo_works(self):
        repl = self.make_repl()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            repl.send("hello")
        self.assertNotIn("undo unavailable", repl._cmd_undo(""))
        repl.turn.persistence_error = "session not saved: disk full"
        self.assertIn("undo unavailable", repl._cmd_undo(""))

    def test_the_tui_shows_it_in_the_transcript_and_the_footer(self):
        ui = tui_module.TUI(lambda: None, config=None, cwd=self.dir,
                            turn=self.controller())
        agent = StubAgent()
        ui.agent = agent
        token = object()
        ui._run_token = token
        with patch.object(session_mod, "SessionStore", BrokenStore):
            ui._run_agent("hello", agent, token)
            ui._pump()
        errors = [e.text for e in ui.transcript.entries if e.kind == "error"]
        self.assertTrue(any("undo unavailable" in text for text in errors), errors)
        self.assertIn("undo unavailable", ui.turn.persistence_notice())

    def test_the_tui_stops_offering_undo_while_it_holds(self):
        controller = self.controller()
        ui = tui_module.TUI(lambda: None, config=None, cwd=self.dir,
                            turn=controller)
        controller.run_turn(StubAgent(), "hello")
        self.assertTrue(ui._undo_available())
        controller.persistence_error = "session not saved: disk full"
        self.assertFalse(ui._undo_available())

    def test_the_notice_is_only_repeated_when_it_changes(self):
        ui = tui_module.TUI(lambda: None, config=None, cwd=self.dir,
                            turn=self.controller())
        ui.turn.persistence_error = "disk full"
        ui._announce_persistence()
        ui._announce_persistence()
        self.assertEqual(len([e for e in ui.transcript.entries
                              if e.kind == "error"]), 1)


# --------------------------------------------------------------------------
# 4. enter during a run queues instead of discarding
# --------------------------------------------------------------------------


class EnterDuringARunQueues(TurnTestCase):
    """The buffer was cleared before the "still working" check ran, so a
    follow-up typed during a run simply vanished."""

    def make_tui(self):
        ui = tui_module.TUI(lambda: None, config=None, cwd=self.dir,
                            turn=self.controller())
        ui.agent = StubAgent()
        return ui

    def test_the_typed_text_is_kept(self):
        ui = self.make_tui()
        ui.running = True
        ui.buffer = "and also rename the file"
        ui.cursor = len(ui.buffer)
        ui._on_enter()
        self.assertEqual(ui.queued, ["and also rename the file"])
        self.assertEqual(ui.buffer, "")

    def test_the_queue_is_visible(self):
        """In the band above the prompt, not in the transcript.

        It used to be written into the transcript, where the next tool call
        scrolled it away while the user was still waiting for it.
        """
        ui = self.make_tui()
        ui.running = True
        ui.buffer = "and also rename the file"
        ui._on_enter()
        pinned = " ".join(line.text for line in ui._pinned_queue_lines(60))
        self.assertIn("and also rename the file", pinned)
        self.assertIn("queued", ui.status_hint)

    def test_it_is_sent_when_the_run_completes(self):
        ui = self.make_tui()
        sent = []
        ui._submit = sent.append
        ui.running = True
        ui.queued = ["the follow-up"]
        token = object()
        ui._run_token = token
        ui._queue.put(("done", None, token))
        ui._pump()
        self.assertEqual(sent, ["the follow-up"])
        self.assertEqual(ui.queued, [])

    def test_interrupting_drops_the_queue(self):
        ui = self.make_tui()
        ui.running = True
        ui.queued = ["the follow-up"]
        ui._interrupt()
        self.assertEqual(ui.queued, [])
        self.assertTrue(any("discarded" in e.text
                            for e in ui.transcript.entries))

    def test_a_new_session_drops_the_queue(self):
        ui = self.make_tui()
        ui.agent_factory = lambda: StubAgent()
        ui.queued = ["the follow-up"]
        ui._new_session()
        self.assertEqual(ui.queued, [])

    def test_a_slash_command_typed_mid_run_still_dispatches(self):
        ui = self.make_tui()
        seen = []
        ui.on_command = lambda line: seen.append(line) or "ok"
        ui.running = True
        ui.buffer = "/tools"
        ui._on_enter()
        self.assertEqual(seen, ["/tools"])
        self.assertEqual(ui.queued, [])


# --------------------------------------------------------------------------
# 5. commands reach the right execution mode
# --------------------------------------------------------------------------


class CommandsAreClassified(unittest.TestCase):
    def test_local_commands_are_synchronous(self):
        for line in ("/tools", "/todos", "/memory", "/undo", "/export out.md",
                     "/compact", "/permissions"):
            self.assertEqual(turn_mod.command_mode(line), SYNC, line)

    def test_network_and_keystore_commands_are_asynchronous(self):
        for line in ("/models", "/model openai/gpt-4o", "/provider openai",
                     "/keys", "/logout openai", "/status"):
            self.assertEqual(turn_mod.command_mode(line), ASYNC, line)

    def test_login_needs_a_modal(self):
        self.assertEqual(turn_mod.command_mode("/login"), MODAL)
        self.assertEqual(turn_mod.command_mode("/login openai"), MODAL)

    def test_init_and_custom_commands_are_turns(self):
        self.assertEqual(turn_mod.command_mode("/init"), TURN)
        self.assertEqual(turn_mod.command_mode("/review", custom=True), TURN)

    def test_the_name_is_taken_from_the_line(self):
        self.assertEqual(turn_mod.command_name("/model  openai/gpt-4o"), "model")
        self.assertEqual(turn_mod.command_name(""), "")


class TUIDispatchesEachClassCorrectly(TurnTestCase):
    """curses owns the only UI thread: a command that blocks it freezes the
    app, and one that reads stdin is invisible while it does."""

    def make_tui(self):
        ui = tui_module.TUI(lambda: None, config=None, cwd=self.dir,
                            turn=self.controller())
        ui.agent = StubAgent()
        self.calls = []
        ui.on_command = lambda line: self.calls.append(line) or "ok"
        self.async_jobs = []
        ui._run_async = lambda label, work, done=None: self.async_jobs.append(
            (label, work, done))
        self.submitted = []
        ui._submit = self.submitted.append
        return ui

    def test_a_sync_command_runs_inline(self):
        ui = self.make_tui()
        ui._dispatch_command("/tools")
        self.assertEqual(self.calls, ["/tools"])
        self.assertEqual(self.async_jobs, [])

    def test_a_keystore_command_goes_to_a_worker(self):
        ui = self.make_tui()
        ui._dispatch_command("/keys")
        self.assertEqual(self.calls, [])          # not on the curses thread
        self.assertEqual(len(self.async_jobs), 1)
        label, work, _done = self.async_jobs[0]
        self.assertEqual(label, "keys")
        self.assertEqual(work(), "ok")            # the worker does the work
        self.assertEqual(self.calls, ["/keys"])

    def test_login_opens_a_modal_and_never_touches_the_command_layer(self):
        ui = self.make_tui()
        ui._dispatch_command("/login openai")
        self.assertIsNotNone(ui.dialog)
        self.assertEqual(ui.dialog.name, "login")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.async_jobs, [])

    def test_the_login_modal_masks_the_key(self):
        ui = self.make_tui()
        ui._dispatch_command("/login openai")
        field = [f for f in ui.dialog.fields if f.name == "key"][0]
        field.value = "sk-secret"
        view = tui_module.form_view(ui.dialog, 50, 12, tui_module.Glyphs(False))
        rendered = "\n".join("".join(text for text, _ in row) for row in view.rows)
        self.assertNotIn("sk-secret", rendered)
        self.assertIn("*********", rendered)
        # ...and the value the submit handler reads is still the real one.
        self.assertEqual(ui.dialog.values()["key"], "sk-secret")

    def test_saving_a_key_happens_off_the_curses_thread(self):
        ui = self.make_tui()
        ui._dispatch_command("/login openai")
        stored = {}
        ui.config = type("C", (), {"set_api_key": lambda _self, name, key:
                                   stored.setdefault(name, key) and "keystore"})()
        for field in ui.dialog.fields:
            field.value = "openai" if field.name == "provider" else "sk-secret"
        ui._save_login(ui.dialog)
        self.assertEqual(stored, {})              # nothing written yet
        self.assertEqual(len(self.async_jobs), 1)
        self.async_jobs[0][1]()
        self.assertEqual(stored, {"openai": "sk-secret"})

    def test_init_becomes_a_turn_rather_than_a_blocking_send(self):
        ui = self.make_tui()
        ui._dispatch_command("/init")
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.submitted), 1)
        self.assertIn("AGENTS.md", self.submitted[0])
        self.assertTrue(Path(self.dir, "haikode.json").exists())

    def test_a_custom_command_becomes_a_turn(self):
        commands = Path(self.dir, ".haikode", "commands")
        commands.mkdir(parents=True)
        Path(commands, "review.md").write_text("Review $ARGUMENTS carefully.")
        ui = self.make_tui()
        from haikode.commands import CommandRegistry
        registry = CommandRegistry(self.dir)
        ui.on_command = lambda line: self.calls.append(line) or "ok"
        ui._command_registry = lambda: registry
        ui._dispatch_command("/review the parser")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.submitted, ["Review the parser carefully."])

    def test_a_turn_command_typed_mid_run_is_queued_not_dropped(self):
        ui = self.make_tui()
        ui.running = True
        ui._dispatch_command("/init")
        self.assertEqual(self.submitted, [])
        self.assertEqual(len(ui.queued), 1)
        self.assertIn("AGENTS.md", ui.queued[0])


class AsyncCommandsAreCancellable(TurnTestCase):
    def make_tui(self):
        ui = tui_module.TUI(lambda: None, config=None, cwd=self.dir,
                            turn=self.controller())
        ui.agent = StubAgent()
        return ui

    def test_a_result_reaches_the_main_thread(self):
        ui = self.make_tui()
        done = []
        ui._run_async("keys", lambda: "the answer", done.append)
        for _ in range(200):
            ui._pump()
            if done:
                break
            import time
            time.sleep(0.01)
        self.assertEqual(done, ["the answer"])
        self.assertEqual(ui._busy_label, "")

    def test_a_cancelled_result_is_dropped(self):
        ui = self.make_tui()
        done = []
        ui._run_async("keys", lambda: "the answer", done.append)
        ui._cancel_async()
        for _ in range(20):
            ui._pump()
            import time
            time.sleep(0.005)
        self.assertEqual(done, [])
        self.assertEqual(ui.status_hint, "cancelled")

    def test_a_failing_worker_becomes_a_transcript_error(self):
        ui = self.make_tui()

        def boom():
            raise RuntimeError("keystore timed out")

        ui._run_async("keys", boom)
        for _ in range(200):
            ui._pump()
            if any(e.kind == "error" for e in ui.transcript.entries):
                break
            import time
            time.sleep(0.01)
        self.assertTrue(any("keystore timed out" in e.text
                            for e in ui.transcript.entries))


class ProviderDialogDoesNotBlock(TurnTestCase):
    """catalog.providers() can run a five-second keystore probe per provider."""

    def test_the_dialog_opens_before_the_catalogue_is_read(self):
        ui = tui_module.TUI(lambda: None, config=None, cwd=self.dir,
                            turn=self.controller())
        loaded = []

        class SlowCatalog:
            def providers(self_inner):
                loaded.append(True)
                return [{"name": "openai", "auth": "key", "model": "gpt-4o"}]

        ui._catalog = lambda: SlowCatalog()
        ui._load_providers_async = lambda catalog: None   # never fires here
        ui._open_providers()
        self.assertIsNotNone(ui.dialog)
        self.assertEqual(ui.dialog.name, "providers")
        self.assertEqual(loaded, [])

    def test_picking_a_provider_rebuilds_off_the_curses_thread(self):
        """The rebuild re-resolves credentials — a keystore subprocess."""
        ui = tui_module.TUI(lambda: None, config=None, cwd=self.dir,
                            turn=self.controller())
        calls, jobs = [], []
        ui.on_command = lambda line: calls.append(line) or "ok"
        ui._run_async = lambda label, work, done=None: jobs.append((label, work,
                                                                    done))
        ui._reprovision("openai")
        self.assertEqual(calls, [])
        self.assertEqual(len(jobs), 1)
        jobs[0][1]()
        self.assertEqual(calls, ["/provider openai"])

    def test_the_rows_arrive_through_the_dialog_queue(self):
        ui = tui_module.TUI(lambda: None, config=None, cwd=self.dir,
                            turn=self.controller())

        class Catalog:
            def providers(self_inner):
                return [{"name": "openai", "auth": "key", "model": "gpt-4o"}]

        ui._catalog = lambda: Catalog()
        ui._open_providers()
        for _ in range(200):
            ui._pump()
            if ui.dialog.select.items:
                break
            import time
            time.sleep(0.01)
        self.assertTrue(any("openai" in (item.title or "")
                            for item in ui.dialog.select.items))


if __name__ == "__main__":
    unittest.main()
