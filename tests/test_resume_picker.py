"""Resume must restore routing as well as the lossless transcript."""
import sqlite3
from unittest.mock import Mock, patch

from tests.test_cli import CLITestCase
from haikode import main as main_mod
from haikode import session as session_mod
from haikode import tui as tui_mod
from haikode.schema import Msg


class ResumeRouting(CLITestCase):
    def test_resume_restores_provider_model_and_agent_without_saving_defaults(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        session = repl.turn.store().new_session(self.dir, "ollama", "saved-model")
        session.set_route("ollama", "saved-model", "plan")
        session.append(Msg(role="user", content="keep this"))
        before = repr(self.config.data)
        result = repl.adopt_session(session)
        self.assertIn("Resumed", result)
        self.assertEqual(repl.agent.model, "saved-model")
        self.assertEqual(repl.agent.agent_name, "plan")
        self.assertEqual(repl.provider_name, "ollama")
        self.assertEqual(repl.agent.messages[0].content, "keep this")
        self.assertEqual(repr(self.config.data), before)

    def test_resume_missing_provider_keeps_current_conversation(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        old = repl.agent
        old.messages = [Msg(role="user", content="current")]
        session = repl.turn.store().new_session(self.dir, "removed", "old-model")
        result = repl.adopt_session(session)
        self.assertTrue(result.startswith("[error]"), result)
        self.assertIs(repl.agent, old)
        self.assertIsNone(repl.session)

    def test_turn_records_actual_route_for_next_resume(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        repl.agent.switch_agent("plan")
        repl.agent.set_model("last-used-model")
        repl.send("hello")
        loaded = repl.turn.store().load(repl.session.id)
        self.assertEqual(loaded.model, "last-used-model")
        self.assertEqual(loaded.agent_name, "plan")

    def test_picker_flag_is_distinct_from_continue(self):
        args = main_mod.build_parser().parse_args(["--resume"])
        self.assertTrue(args.resume_picker)
        self.assertFalse(args.resume)

    def test_picker_refuses_noninteractive_stdin_before_building_agent(self):
        with patch.object(main_mod, "build_repl") as build:
            code, _, err = self.run_main(["--resume"])
        self.assertEqual(code, 2)
        self.assertIn("terminal", err)
        build.assert_not_called()

    def test_explicit_startup_model_overrides_saved_route(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        session = repl.turn.store().new_session(self.dir, "ollama", "old")
        args = main_mod.build_parser().parse_args(
            ["--session", session.id, "--model", "ollama/new"])
        resumed = main_mod.build_repl(self.config, args, self.dir, report=lambda _: None)
        self.addCleanup(resumed.turn.close)
        self.assertEqual(resumed.agent.model, "new")
        # Explicit options apply once; future selections use their own route.
        resumed.adopt_session(session)
        self.assertEqual(resumed.agent.model, "old")

    def test_plain_picker_reprompts_and_restores_selection(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        session = repl.turn.store().new_session(self.dir, "ollama", "picked")
        with patch("builtins.input", side_effect=["bad", "0", "1"]):
            self.assertTrue(main_mod.pick_session_plain(repl))
        self.assertEqual(repl.session.id, session.id)
        self.assertEqual(repl.agent.model, "picked")

    def test_resume_clears_session_grants(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        repl.permissions.grant_always("bash", ["*"])
        session = repl.turn.store().new_session(self.dir, "ollama", "picked")
        repl.adopt_session(session)
        self.assertEqual(repl.permissions.session_grants(), {})

    def test_tui_resume_replaces_empty_history_and_agent_label(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        repl.agent.messages = [Msg(role="user", content="previous")]
        bridge = main_mod.CommandBridge(repl, tui_mod.REPROVISION_COMMANDS)
        ui = tui_mod.TUI(lambda: repl.agent, self.config, cwd=self.dir,
                         agent=repl.agent, turn=repl.turn, on_command=bridge)
        ui.agent_name = "plan"
        session = repl.turn.store().new_session(self.dir, "ollama", "picked")
        session.set_route("ollama", "picked", "build")
        result = bridge("/resume " + session.id)
        ui._finish_resume(result)
        self.assertIs(ui.agent, repl.agent)
        self.assertEqual(ui.agent.messages, [])
        self.assertEqual(ui.agent_name, "build")

    def test_old_database_migration_preserves_rows_and_old_reader(self):
        with sqlite3.connect(str(self.db)) as conn:
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
                         "cwd TEXT, provider TEXT, model TEXT, created REAL, "
                         "updated REAL, archived INTEGER DEFAULT 0)")
            conn.execute("INSERT INTO sessions VALUES ('legacy', 'title', ?, "
                         "'ollama', 'old', 1, 1, 0)", (self.dir,))
        store = session_mod.SessionStore(self.db)
        self.addCleanup(store.close)
        session = store.load("legacy")
        self.assertEqual(session.agent_name, "")
        session.set_route("ollama", "new", "plan")
        self.assertEqual(store.load("legacy").agent_name, "plan")
        with sqlite3.connect(str(self.db)) as conn:
            self.assertEqual(conn.execute(
                "SELECT title, provider, model FROM sessions WHERE id = 'legacy'"
            ).fetchone(), ("title", "ollama", "new"))

    def test_failed_resume_does_not_poison_new_conversation_factory(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        bridge = main_mod.CommandBridge(repl, tui_mod.REPROVISION_COMMANDS)
        result = bridge("/resume missing")
        self.assertFalse(result.startswith("Resumed "))
        self.assertFalse(bridge.reprovisioned)

    def test_provider_only_override_uses_its_own_default_model(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        session = repl.turn.store().new_session(self.dir, "ollama", "saved")
        self.config.data["providers"]["other"] = {
            "model": "other-default", "requires_key": False,
            "base_url": "http://127.0.0.1:1/v1"}
        repl.resume_overrides = {"provider": "other"}
        repl.adopt_session(session)
        self.assertEqual(repl.provider_name, "other")
        self.assertEqual(repl.agent.model, "other-default")

    def test_resume_disposes_replaced_mcp_and_lsp(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        managers = [Mock(), Mock()]
        repl.agent.ctx.mcp, repl.agent.ctx.lsp = managers
        session = repl.turn.store().new_session(self.dir, "ollama", "picked")
        repl.adopt_session(session)
        for manager in managers:
            manager.shutdown_all.assert_called_once_with()

    def test_tui_fork_adopts_selected_history_and_model(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        session = repl.turn.store().new_session(self.dir, "ollama", "fork-model")
        session.append(Msg(role="user", content="selected history"))
        bridge = main_mod.CommandBridge(repl, tui_mod.REPROVISION_COMMANDS)
        ui = tui_mod.TUI(lambda: repl.agent, self.config, cwd=self.dir,
                         agent=repl.agent, turn=repl.turn, on_command=bridge)
        result = bridge("/fork " + session.id)
        self.assertTrue(bridge.reprovisioned)
        ui._finish_command("/fork " + session.id, result)
        self.assertIs(ui.agent, repl.agent)
        self.assertEqual(ui.agent.model, "fork-model")
        self.assertEqual(ui.agent.messages[0].content, "selected history")
        self.assertNotEqual(repl.session.id, session.id)

    def test_failed_continue_never_runs_a_fresh_prompt(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        repl.turn.store().new_session(self.dir, "removed", "old")
        code, _, _ = self.run_main(["-C", self.dir, "--continue", "do work"])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.provider.seen, [])

    def test_failed_preload_is_reported_without_traceback_or_prompt(self):
        with patch.object(session_mod.SessionStore, "list_sessions",
                          side_effect=sqlite3.OperationalError("database is locked")):
            code, _, err = self.run_main(["--continue", "do work"])
        self.assertNotEqual(code, 0)
        self.assertIn("cannot read saved session", err)
        self.assertEqual(self.provider.seen, [])

    def test_failed_fork_by_id_preserves_current_agent_and_session(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        repl.send("original")
        old_agent, old_session = repl.agent, repl.session
        target = repl.turn.store().new_session(self.dir, "ollama", "other")
        with patch("haikode.repl.copy_session", side_effect=OSError("disk full")):
            result = repl._cmd_fork(target.id)
        self.assertTrue(result.startswith("[error]"))
        self.assertIs(repl.agent, old_agent)
        self.assertIs(repl.session, old_session)

    def test_failed_cli_fork_does_not_send_to_original(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        target = repl.turn.store().new_session(self.dir, "ollama", "other")
        with patch("haikode.repl.copy_session", side_effect=OSError("disk full")):
            code, _, err = self.run_main(
                ["--session", target.id, "--fork", "do work"])
        self.assertNotEqual(code, 0)
        self.assertIn("disk full", err)
        self.assertEqual(self.provider.seen, [])

    def test_failed_fork_route_write_does_not_adopt_fork(self):
        repl = self.build()
        self.addCleanup(repl.turn.close)
        repl.send("original")
        original = repl.session
        forked = repl.turn.store().new_session(self.dir, "ollama", "other")
        with patch("haikode.repl.copy_session", return_value=forked), \
                patch.object(forked, "set_route", side_effect=OSError("disk full")):
            result = repl.fork_session()
        self.assertTrue(result.startswith("[error]"))
        self.assertIs(repl.session, original)
