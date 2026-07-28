"""
The wiring tests: do the built libraries actually reach the running program?

Every module under test here passed its own unit tests long before this file
existed and still had zero effect on what a user sees, because nothing called
it. So these tests deliberately assert *connections* rather than behaviour:
that build_agent reads haikode.json, that a slash command lands in the library
that owns the feature, that the memory tools are in the registry the model is
offered. A regression here means a feature silently stopped existing.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode import agents as agents_mod  # noqa: E402
from haikode import context as context_mod  # noqa: E402
from haikode import main as main_mod  # noqa: E402
from haikode import memory as memory_mod  # noqa: E402
from haikode import models as models_mod  # noqa: E402
from haikode import projectconfig as projectconfig_mod  # noqa: E402
from haikode import repl as repl_mod  # noqa: E402
from haikode import runtime  # noqa: E402
from haikode import session as session_mod  # noqa: E402
from haikode import status as status_mod  # noqa: E402
from haikode import usage as usage_mod  # noqa: E402
from haikode.config import Config  # noqa: E402
from haikode.permission import PermissionRequest  # noqa: E402
from haikode.schema import PermissionDenied  # noqa: E402
from haikode.tool import REGISTRY, get_tools  # noqa: E402


class WiringTestCase(unittest.TestCase):
    """A project directory, a private global config, and no real home dir."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-wiring-")
        self.home = tempfile.mkdtemp(prefix="haikode-home-")
        self.globals = Path(self.home, "config")
        self.globals.mkdir(parents=True, exist_ok=True)
        self._patches = [
            patch.object(memory_mod, "global_config_dir", lambda: self.globals),
            patch.object(agents_mod, "global_config_dir", lambda: self.globals),
            patch.object(context_mod, "global_config_dir", lambda: self.globals),
            patch.object(projectconfig_mod, "global_config_dir",
                         lambda: self.globals),
            patch.object(context_mod, "home_dir", lambda: Path(self.home)),
        ]
        for entry in self._patches:
            entry.start()
        self.config = Config(path=str(Path(self.home, "config.json")))

    def tearDown(self):
        for entry in reversed(self._patches):
            entry.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def write_project(self, **settings):
        Path(self.dir, "haikode.json").write_text(json.dumps(settings))

    def build(self, **kwargs):
        return runtime.build_agent(self.config, kwargs.pop("provider", ""),
                                   self.dir, **kwargs)


# --------------------------------------------------------------------------
# 1. the project config actually changes the session
# --------------------------------------------------------------------------


class TestProjectConfigReachesTheAgent(WiringTestCase):
    def test_model_and_provider_override(self):
        self.write_project(model="anthropic/claude-sonnet-5")
        agent = self.build()
        self.assertEqual(agent.model, "claude-sonnet-5")
        self.assertEqual(agent.provider.name, "anthropic")

    def test_bare_model_applies_to_the_default_provider(self):
        self.write_project(model="qwen3-coder")
        agent = self.build()
        self.assertEqual(agent.model, "qwen3-coder")

    def test_explicit_provider_key(self):
        self.write_project(provider="openai")
        self.assertEqual(self.build().provider.name, "openai")

    def test_max_steps_override(self):
        self.write_project(max_steps=3)
        self.assertEqual(self.build().max_steps, 3)

    def test_context_window_override(self):
        self.write_project(provider="openai", context=64000)
        self.assertEqual(self.build().context_window, 64000)

    def test_tools_map_removes_a_tool(self):
        self.write_project(tools={"webfetch": False, "bash": False})
        tools = self.build().tools
        self.assertNotIn("webfetch", tools)
        self.assertNotIn("bash", tools)
        self.assertIn("read", tools)

    def test_tools_map_globs(self):
        self.write_project(tools={"memory_*": False})
        tools = self.build().tools
        self.assertNotIn("memory_write", tools)
        self.assertNotIn("memory_read", tools)

    def test_disabled_tool_is_not_offered_to_the_provider(self):
        self.write_project(tools={"webfetch": False})
        agent = self.build()
        self.assertNotIn("webfetch", {spec.name for spec in agent.specs})

    def test_permissions_reach_the_permission_layer(self):
        self.write_project(permission={"bash": "deny"})
        agent = self.build()
        with self.assertRaises(PermissionDenied):
            agent.permissions.ask(PermissionRequest("bash", ["ls"], "list"))

    def test_pattern_permissions_reach_the_permission_layer(self):
        # Two things this rule now depends on, both deliberate: only a trusted
        # repository may loosen a key (the `allow` is a widening), and the LAST
        # matching rule wins, so the catch-all has to be written first.
        projectconfig_mod.trust(self.dir)
        self.write_project(permission={"bash": {"*": "deny",
                                                "git status": "allow"}})
        agent = self.build()
        agent.permissions.ask(PermissionRequest("bash", ["git status"], "st"))
        with self.assertRaises(PermissionDenied):
            agent.permissions.ask(PermissionRequest("bash", ["rm -rf /"], "rm"))

    def test_an_untrusted_project_cannot_loosen_a_pattern(self):
        """The same file, without the trust grant: the `allow` is dropped."""
        self.write_project(permission={"bash": {"*": "deny",
                                                "git status": "allow"}})
        agent = self.build()
        with self.assertRaises(PermissionDenied):
            agent.permissions.ask(PermissionRequest("bash", ["git status"], "st"))

    def test_declared_instructions_are_resolved(self):
        Path(self.dir, "docs").mkdir()
        Path(self.dir, "docs", "rules.md").write_text("Rule: no tabs.\n")
        self.write_project(instructions=["docs/rules.md"])
        agent = self.build()
        self.assertIn("Rule: no tabs.", agent._system_message().content)

    def test_default_agent_from_the_project_config(self):
        self.write_project(default_agent="plan")
        agent = self.build()
        self.assertEqual(agent.agent_name, "plan")
        self.assertTrue(agent.plan_mode)

    def test_config_declared_agent_is_selectable(self):
        self.write_project(agents={"reviewer": {
            "description": "Reviews code", "tools": ["read", "grep"]}})
        agent = self.build()
        self.assertIn("reviewer", agent.registry.names())
        agent.switch_agent("reviewer")
        self.assertEqual(set(agent.tools), {"read", "grep"})

    def test_explicit_agent_argument_wins_over_the_config(self):
        self.write_project(default_agent="plan")
        self.assertEqual(self.build(agent_name="build").agent_name, "build")

    def test_unknown_agent_falls_back_with_a_warning(self):
        agent = self.build(agent_name="nope")
        self.assertEqual(agent.agent_name, "build")
        self.assertTrue(any("nope" in w for w in agent.warnings))

    def test_the_project_layer_sits_below_the_agent_layer(self):
        """A tool the project disabled stays gone through an agent switch."""
        self.write_project(tools={"read": False})
        agent = self.build()
        agent.switch_agent("plan")
        agent.switch_agent("build")
        self.assertNotIn("read", agent.tools)


class TestBrokenProjectConfigDegrades(WiringTestCase):
    def test_invalid_json_does_not_crash(self):
        Path(self.dir, "haikode.json").write_text("{not json at all")
        agent = self.build()
        self.assertTrue(agent.warnings)
        self.assertIn("read", agent.tools)

    def test_wrong_types_are_reported_not_fatal(self):
        self.write_project(max_steps="lots", tools=["read"])
        agent = self.build()
        self.assertTrue(agent.warnings)
        # The rejected value falls back to the user's own setting, not to the
        # project's — a bad project file must change nothing at all.
        self.assertEqual(agent.max_steps, self.config.data["max_steps"])

    def test_unknown_keys_become_warnings(self):
        self.write_project(nonsense_key=1)
        self.assertTrue(any("nonsense_key" in w for w in self.build().warnings))

    def test_runtime_never_prints(self):
        """Warnings belong to the front-end; runtime returns them as data."""
        Path(self.dir, "haikode.json").write_text("{oops")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            agent = self.build()
        self.assertEqual(buffer.getvalue(), "")
        self.assertTrue(agent.warnings)

    def test_an_unreadable_project_tree_still_builds_an_agent(self):
        with patch.object(runtime.ProjectConfig, "load",
                          side_effect=OSError("boom")):
            agent = self.build()
        self.assertTrue(any("boom" in w for w in agent.warnings))
        self.assertIn("read", agent.tools)


class TestSessionConfigWrites(WiringTestCase):
    def test_a_granted_rule_lands_in_the_users_config_only(self):
        self.write_project(permission={"bash": "deny"})
        agent = self.build()
        agent.permissions.persist("webfetch", "https://x/*", "allow")
        saved = json.loads(Path(self.config.path).read_text())
        self.assertEqual(saved["permission"]["webfetch"]["https://x/*"], "allow")
        # The project's own rule must not be copied into the global config.
        self.assertNotIn("bash", saved.get("permission", {}))

    def test_the_project_permission_block_is_visible_to_the_session(self):
        self.write_project(permission={"webfetch": "deny"})
        agent = self.build()
        self.assertEqual(
            agent.permissions.config.data["permission"]["webfetch"], "deny")


# --------------------------------------------------------------------------
# 2. the tool registry
# --------------------------------------------------------------------------


class TestToolRegistry(WiringTestCase):
    def test_memory_tools_are_registered(self):
        self.assertIn("memory_write", REGISTRY)
        self.assertIn("memory_read", REGISTRY)

    def test_memory_tools_are_offered_to_the_model(self):
        agent = self.build()
        names = {spec.name for spec in agent.specs}
        self.assertIn("memory_write", names)
        self.assertIn("memory_read", names)

    def test_memory_write_is_callable_through_the_registry(self):
        from haikode.permission import Permissions
        from haikode.tool import ToolContext
        ctx = ToolContext(cwd=self.dir, permissions=Permissions(auto_approve=True))
        result = REGISTRY["memory_write"].execute(
            {"text": "Deploy with tar over ssh, never scp.",
             "name": "deploy"}, ctx)
        self.assertIn("deploy", result.output)
        self.assertEqual(memory_mod.MemoryStore(self.dir).get("deploy").name,
                         "deploy")

    def test_memory_read_is_callable_through_the_registry(self):
        from haikode.permission import Permissions
        from haikode.tool import ToolContext
        memory_mod.MemoryStore(self.dir).write("scp hangs on this box.",
                                               name="scp")
        ctx = ToolContext(cwd=self.dir, permissions=Permissions(auto_approve=True))
        result = REGISTRY["memory_read"].execute({"query": "scp"}, ctx)
        self.assertIn("scp hangs", result.output)

    def test_a_written_memory_reaches_the_next_system_prompt(self):
        from haikode.permission import Permissions
        agent = self.build(permissions=Permissions(auto_approve=True))
        self.assertIn("No saved memories yet.", agent._system_message().content)
        REGISTRY["memory_write"].execute(
            {"text": "The suite is run with unittest discover.", "name": "suite"},
            agent.ctx)
        agent._memory_epoch += 1
        self.assertIn("unittest discover", agent._system_message().content)

    def test_get_tools_filters_by_name(self):
        self.assertEqual(set(get_tools(["read", "memory_read"])),
                         {"read", "memory_read"})


# --------------------------------------------------------------------------
# 3. the REPL commands delegate to the libraries
# --------------------------------------------------------------------------


class REPLTestCase(WiringTestCase):
    def setUp(self):
        super().setUp()
        self.repl = repl_mod.REPL(self.config, cwd=self.dir)

    def run_command(self, line: str) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            result = self.repl.handle_command(line)
        return (result or "") + buffer.getvalue()


class TestREPLCommandsAreRegistered(REPLTestCase):
    EXPECTED = ("agent", "plan", "build", "memory", "remember", "forget",
                "status", "context", "usage", "sessions", "rename", "archive",
                "export", "compact", "init", "config", "models", "model",
                "provider")

    def test_every_new_command_is_discoverable(self):
        names = set(self.repl.commands.complete(""))
        for name in self.EXPECTED:
            self.assertIn(name, names, name)

    def test_help_lists_them(self):
        text = self.repl.commands.help_text()
        for name in self.EXPECTED:
            self.assertIn("/" + name, text, name)

    def test_dispatch_reaches_the_handler(self):
        with patch.object(self.repl, "_cmd_status", return_value="STATUS") as spy:
            self.repl._setup_commands()
            self.assertEqual(self.repl.handle_command("/status"), "STATUS")
        spy.assert_called_once()


class TestREPLDelegates(REPLTestCase):
    def test_status_uses_the_status_module(self):
        with patch.object(status_mod, "collect",
                          wraps=status_mod.collect) as collect:
            output = self.run_command("/status")
        collect.assert_called_once()
        self.assertIn("Agent", output)
        self.assertIn("Prompt", output)

    def test_context_uses_the_usage_module(self):
        with patch.object(usage_mod, "detail_lines",
                          return_value=["CONTEXT"]) as spy:
            output = self.run_command("/context")
        spy.assert_called_once()
        self.assertIn("CONTEXT", output)

    def test_usage_uses_the_usage_module(self):
        with patch.object(usage_mod, "detail_lines",
                          return_value=["USAGE"]) as spy:
            output = self.run_command("/usage")
        spy.assert_called_once()
        self.assertIn("USAGE", output)

    def test_config_uses_the_project_config_describe(self):
        with patch.object(self.repl.project, "describe",
                          return_value=["DESCRIBED"]) as spy:
            output = self.run_command("/config")
        spy.assert_called_once()
        self.assertIn("DESCRIBED", output)
        self.assertIn("Effective settings", output)

    def test_agent_lists_the_registry(self):
        output = self.run_command("/agent")
        self.assertIn("build", output)
        self.assertIn("plan", output)

    def test_agent_switch_calls_switch_agent(self):
        with patch.object(self.repl.agent, "switch_agent",
                          wraps=self.repl.agent.switch_agent) as spy:
            self.run_command("/agent plan")
        spy.assert_called_once_with("plan")
        self.assertEqual(self.repl.agent.agent_name, "plan")

    def test_plan_and_build_switch_the_agent(self):
        self.run_command("/plan")
        self.assertEqual(self.repl.agent.agent_name, "plan")
        self.assertNotIn("edit", self.repl.agent.tools)
        self.run_command("/build")
        self.assertEqual(self.repl.agent.agent_name, "build")
        self.assertIn("edit", self.repl.agent.tools)

    def test_unknown_agent_is_reported(self):
        self.assertIn("Unknown agent", self.run_command("/agent nope"))

    def test_memory_uses_the_memory_store(self):
        memory_mod.MemoryStore(self.dir).write("Keys live in the keystore.",
                                               name="keys")
        self.assertIn("keys", self.run_command("/memory"))
        self.assertIn("keys", self.run_command("/memory keystore"))

    def test_remember_writes_through_the_memory_store(self):
        with patch.object(memory_mod.MemoryStore, "write", autospec=True,
                          side_effect=memory_mod.MemoryStore.write) as spy:
            output = self.run_command("/remember Tar over ssh, never scp.")
        spy.assert_called_once()
        self.assertIn("Remembered", output)
        self.assertTrue(memory_mod.MemoryStore(self.dir).all())

    def test_forget_deletes_through_the_memory_store(self):
        memory_mod.MemoryStore(self.dir).write("Temporary note.", name="temp")
        self.assertIn("Forgot", self.run_command("/forget temp"))
        self.assertIsNone(memory_mod.MemoryStore(self.dir).get("temp"))

    def test_forget_reports_an_unknown_name(self):
        self.assertIn("No memory named", self.run_command("/forget nothing"))

    def test_quick_capture_saves_a_memory_instead_of_prompting(self):
        with patch.object(self.repl.agent, "run",
                          side_effect=AssertionError("must not run")):
            output = self.run_command("# The owner writes Norwegian.")
        self.assertIn("Remembered", output)
        names = [m.name for m in memory_mod.MemoryStore(self.dir).all()]
        self.assertTrue(names)

    def test_quick_capture_double_hash_is_user_scoped(self):
        self.run_command("## Prefers concise answers.")
        scopes = {m.scope for m in memory_mod.MemoryStore(self.dir).all()}
        self.assertIn("user", scopes)

    def test_send_refuses_to_prompt_with_a_capture_line(self):
        with patch.object(self.repl.agent, "run",
                          side_effect=AssertionError("must not run")):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                self.repl.send("# A note for later.")
        self.assertIn("Remembered", buffer.getvalue())

    def test_models_uses_the_catalog(self):
        catalog = MagicMock()
        catalog.choices.return_value = []
        catalog.errors = {"ollama": "offline"}
        with patch.object(models_mod, "ModelCatalog", return_value=catalog):
            output = self.run_command("/models")
        catalog.choices.assert_called_once()
        self.assertIn("offline", output)

    def test_model_selection_uses_the_catalog(self):
        catalog = MagicMock()
        catalog.select.return_value = models_mod.ModelRef(
            provider="ollama", model="qwen3-coder")
        with patch.object(models_mod, "ModelCatalog", return_value=catalog):
            output = self.run_command("/model qwen3-coder")
        catalog.select.assert_called_once_with("ollama/qwen3-coder")
        self.assertIn("ollama/qwen3-coder", output)
        self.assertEqual(self.repl.agent.model, "qwen3-coder")

    def test_model_favourite_uses_the_catalog(self):
        catalog = MagicMock()
        catalog.toggle_favourite.return_value = True
        with patch.object(models_mod, "ModelCatalog", return_value=catalog):
            output = self.run_command("/model fav anthropic/claude-sonnet-5")
        catalog.toggle_favourite.assert_called_once_with(
            "anthropic/claude-sonnet-5")
        self.assertIn("Added favourite", output)

    def test_model_listing_shows_favourites_and_recents(self):
        catalog = MagicMock()
        catalog.current.return_value = models_mod.ModelRef(
            provider="ollama", model="gpt-oss:120b")
        catalog.favourites.return_value = [
            models_mod.ModelRef(provider="openai", model="gpt-4o-mini")]
        catalog.recent.return_value = [
            models_mod.ModelRef(provider="xai", model="grok-4")]
        with patch.object(models_mod, "ModelCatalog", return_value=catalog):
            output = self.run_command("/model")
        self.assertIn("openai/gpt-4o-mini", output)
        self.assertIn("xai/grok-4", output)

    def test_provider_add_uses_the_models_wrapper(self):
        with patch.object(models_mod, "add_provider",
                          return_value=(True, "added")) as spy:
            output = self.run_command("/provider add local http://x/v1 m")
        spy.assert_called_once()
        self.assertEqual(spy.call_args[0][1:], ("local", "http://x/v1", "m",
                                                "openai"))
        self.assertIn("added", output)

    def test_provider_remove_uses_the_models_wrapper(self):
        with patch.object(models_mod, "remove_provider",
                          return_value=(True, "removed")) as spy:
            output = self.run_command("/provider remove xai")
        spy.assert_called_once()
        self.assertIn("removed", output)

    def test_provider_default_uses_the_models_wrapper(self):
        with patch.object(models_mod, "set_default",
                          return_value=(True, "default set")) as spy:
            output = self.run_command("/provider default openai")
        spy.assert_called_once()
        self.assertIn("default set", output)
        self.assertEqual(self.repl.provider_name, "openai")

    def test_provider_switch_rebuilds_the_agent_and_keeps_history(self):
        from haikode.schema import Msg
        self.repl.agent.messages = [Msg(role="user", content="earlier")]
        self.run_command("/provider openai")
        self.assertEqual(self.repl.provider_name, "openai")
        self.assertEqual(self.repl.agent.provider.name, "openai")
        self.assertEqual([m.content for m in self.repl.agent.messages],
                         ["earlier"])

    def test_always_grants_survive_a_provider_switch(self):
        self.repl.agent.permissions.grant_always("bash", ["git status"])
        self.run_command("/provider openai")
        self.repl.agent.permissions.ask(
            PermissionRequest("bash", ["git status"], "status"))

    def test_init_writes_a_project_config_and_prompts_for_agents_md(self):
        with patch.object(self.repl, "send") as send, \
                patch.object(projectconfig_mod, "init_project_config",
                             wraps=projectconfig_mod.init_project_config) as spy:
            self.run_command("/init")
        spy.assert_called_once()
        send.assert_called_once()
        self.assertIn("AGENTS.md", send.call_args[0][0])
        self.assertTrue(Path(self.dir, "haikode.json").exists())

    def test_init_does_not_clobber_an_existing_config(self):
        self.write_project(max_steps=5)
        with patch.object(self.repl, "send"):
            output = self.run_command("/init")
        self.assertIn("already exists", output)
        self.assertEqual(json.loads(Path(self.dir, "haikode.json").read_text()),
                         {"max_steps": 5})

    def test_tools_command_lists_the_active_tool_set(self):
        output = self.run_command("/tools")
        self.assertIn("memory_write", output)


class TestREPLSessionCommands(REPLTestCase):
    def setUp(self):
        super().setUp()
        # Redirect the database rather than faking the store: these tests are
        # about the REPL reaching the real session library.
        self.db = Path(self.home, "sessions.db")
        self._store_patch = patch.object(session_mod, "default_db_path",
                                         lambda: self.db)
        self._store_patch.start()

    def tearDown(self):
        self._store_patch.stop()
        super().tearDown()

    def _seed(self, title="Fix the parser", body="the parser drops commas"):
        from haikode.schema import Msg
        store = session_mod.SessionStore()
        session = store.new_session(self.dir, "ollama", "m", title)
        session.append(Msg(role="user", content=body))
        session.append(Msg(role="assistant", content="done"))
        return session

    def test_sessions_lists_the_store(self):
        self._seed()
        output = self.run_command("/sessions")
        self.assertIn("Fix the parser", output)

    def test_sessions_searches_the_store(self):
        self._seed()
        with patch.object(session_mod.SessionStore, "search", autospec=True,
                          side_effect=session_mod.SessionStore.search) as spy:
            output = self.run_command("/sessions commas")
        spy.assert_called_once()
        self.assertIn("Fix the parser", output)

    def test_resume_adopts_the_history(self):
        session = self._seed()
        output = self.run_command("/resume " + session.id)
        self.assertIn("Resumed", output)
        self.assertEqual(len(self.repl.agent.messages), 2)

    def test_resume_latest_for_continue(self):
        session = self._seed()
        self.assertIn(session.id[:8], self.repl.resume_latest())

    def test_resume_session_by_id_for_the_session_flag(self):
        session = self._seed()
        self.assertIn("Resumed", self.repl.resume_session(session.id))
        self.assertEqual(len(self.repl.agent.messages), 2)
        self.assertIn("No session", self.repl.resume_session("nope"))

    def test_rename_uses_the_session(self):
        session = self._seed()
        self.run_command("/resume " + session.id)
        with patch.object(session_mod.Session, "rename", autospec=True,
                          side_effect=session_mod.Session.rename) as spy:
            output = self.run_command("/rename Parser work")
        spy.assert_called_once()
        self.assertIn("Parser work", output)

    def test_archive_uses_the_session(self):
        session = self._seed()
        self.run_command("/resume " + session.id)
        output = self.run_command("/archive")
        self.assertIn("Archived", output)
        rows = session_mod.SessionStore().list_sessions(cwd=self.dir)
        self.assertNotIn(session.id, [r["id"] for r in rows])

    def test_export_renders_the_transcript(self):
        session = self._seed()
        self.run_command("/resume " + session.id)
        output = self.run_command("/export")
        self.assertIn("the parser drops commas", output)

    def test_export_to_a_file(self):
        session = self._seed()
        self.run_command("/resume " + session.id)
        target = Path(self.dir, "out.md")
        output = self.run_command("/export " + str(target))
        self.assertIn("Exported", output)
        self.assertIn("the parser drops commas", target.read_text())

    def test_compact_uses_the_session(self):
        session = self._seed()
        self.run_command("/resume " + session.id)
        with patch.object(session_mod.Session, "compact",
                          return_value=1, autospec=True) as spy:
            output = self.run_command("/compact 1")
        spy.assert_called_once()
        self.assertEqual(spy.call_args.kwargs["keep_last"], 1)
        self.assertIn("Folded", output)

    def test_compact_without_a_session_trims_in_memory(self):
        from haikode.schema import Msg
        self.repl.agent.messages = [Msg(role="user", content="x" * 400)
                                    for _ in range(40)]
        self.repl.agent.context_window = 1000
        output = self.run_command("/compact")
        self.assertIn("Compacted in memory", output)
        self.assertLess(len(self.repl.agent.messages), 40)

    def test_sessions_are_scoped_to_this_directory(self):
        other = tempfile.mkdtemp(prefix="haikode-other-")
        try:
            store = session_mod.SessionStore()
            store.new_session(other, "ollama", "m", "Elsewhere")
            self._seed()
            output = self.run_command("/sessions")
            self.assertIn("Fix the parser", output)
            self.assertNotIn("Elsewhere", output)
        finally:
            shutil.rmtree(other, ignore_errors=True)


# --------------------------------------------------------------------------
# 4. the CLI flags
# --------------------------------------------------------------------------


class TestCLIFlags(WiringTestCase):
    def parse(self, argv):
        return main_mod.build_parser().parse_args(argv)

    def test_agent_flag(self):
        self.assertEqual(self.parse(["--agent", "plan"]).agent, "plan")

    def test_model_flag_splits_the_provider(self):
        self.assertEqual(main_mod.split_model("anthropic/claude-sonnet-5"),
                         ("anthropic", "claude-sonnet-5"))

    def test_bare_model_flag_keeps_the_provider(self):
        self.assertEqual(main_mod.split_model("qwen3-coder"), ("", "qwen3-coder"))

    def test_continue_flag(self):
        self.assertTrue(self.parse(["--continue"]).resume)
        self.assertTrue(self.parse(["-c"]).resume)

    def test_session_flag(self):
        self.assertEqual(self.parse(["--session", "abc"]).session, "abc")

    def test_print_logs_flag(self):
        self.assertTrue(self.parse(["--print-logs"]).print_logs)

    def test_the_scripting_flags_parse(self):
        args = self.parse(["--json", "--fork", "--title", "Nightly audit",
                           "-s", "abc"])
        self.assertTrue(args.json)
        self.assertTrue(args.fork)
        self.assertEqual(args.title, "Nightly audit")

    def test_json_builds_the_json_front_end(self):
        """--json has to reach the REPL factory, not just the namespace: the
        whole point is that the events come out of the same turn."""
        args = self.parse(["--json"])
        repl = main_mod.build_repl(self.config, args, self.dir)
        self.assertIsInstance(repl, repl_mod.JSONREPL)
        self.assertIsInstance(main_mod.build_repl(
            self.config, self.parse([]), self.dir), repl_mod.REPL)

    def test_title_reaches_a_resumed_session(self):
        db = Path(self.home, "sessions.db")
        with patch.object(session_mod, "default_db_path", lambda: db):
            store = session_mod.SessionStore()
            session = store.new_session(self.dir, "ollama", "m", "Old title")
            args = self.parse(["--session", session.id, "--title", "New title"])
            with redirect_stdout(io.StringIO()):
                main_mod.build_repl(self.config, args, self.dir)
            self.assertEqual(store.load(session.id).title, "New title")

    def test_every_exit_code_is_documented_in_the_help(self):
        codes = {code for code, _ in main_mod.EXIT_CODE_HELP}
        self.assertEqual(codes, {repl_mod.EXIT_OK, repl_mod.EXIT_ERROR,
                                 repl_mod.EXIT_USAGE, repl_mod.EXIT_DENIED,
                                 repl_mod.EXIT_LIMIT,
                                 repl_mod.EXIT_INTERRUPTED})
        for code, _ in main_mod.EXIT_CODE_HELP:
            self.assertIn(str(code), main_mod.HELP_EPILOGUE)

    def test_every_subcommand_has_a_handler(self):
        """A name in SUBCOMMANDS that _dispatch_subcommand does not route
        falls through and is silently run as a prompt."""
        for name in sorted(main_mod.SUBCOMMANDS - {"run"}):
            with patch.object(main_mod, "doctor"), \
                    patch.object(main_mod, "login"), \
                    patch.object(main_mod, "provider_command", return_value=0), \
                    patch.object(main_mod, "session_command", return_value=0), \
                    patch.object(main_mod, "models_command", return_value=0), \
                    patch.object(main_mod, "agent_command", return_value=0), \
                    patch.object(main_mod, "export_command", return_value=0), \
                    patch.object(main_mod, "import_command", return_value=0):
                self.assertIsNotNone(main_mod._dispatch_subcommand([name]), name)

    def test_existing_flags_still_parse(self):
        args = self.parse(["-p", "openai", "-C", "/tmp", "--no-tui", "--yes",
                           "do", "the", "thing"])
        self.assertEqual(args.provider, "openai")
        self.assertEqual(args.directory, "/tmp")
        self.assertTrue(args.no_tui)
        self.assertTrue(args.yes)
        self.assertEqual(args.prompt, ["do", "the", "thing"])

    def test_build_repl_applies_agent_and_model(self):
        args = self.parse(["--agent", "plan", "--model", "openai/gpt-4o-mini"])
        repl = main_mod.build_repl(self.config, args, self.dir)
        self.assertEqual(repl.agent.agent_name, "plan")
        self.assertEqual(repl.agent.model, "gpt-4o-mini")
        self.assertEqual(repl.provider_name, "openai")

    def test_build_repl_continues_the_latest_session(self):
        from haikode.schema import Msg
        db = Path(self.home, "sessions.db")
        with patch.object(session_mod, "default_db_path", lambda: db):
            store = session_mod.SessionStore()
            session = store.new_session(self.dir, "ollama", "m", "Earlier work")
            session.append(Msg(role="user", content="earlier question"))
            args = self.parse(["--continue"])
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                repl = main_mod.build_repl(self.config, args, self.dir)
        self.assertIn("Resumed", buffer.getvalue())
        self.assertEqual([m.content for m in repl.agent.messages],
                         ["earlier question"])

    def test_print_logs_reports_config_warnings(self):
        Path(self.dir, "haikode.json").write_text("{broken")
        args = self.parse(["--print-logs"])
        stderr = io.StringIO()
        real = sys.stderr
        sys.stderr = stderr
        try:
            main_mod.build_repl(self.config, args, self.dir)
        finally:
            sys.stderr = real
        self.assertIn("config", stderr.getvalue())


class TestTUIFactoryContract(WiringTestCase):
    """The TUI asks for an agent through one no-argument callable.

    It means two different things by it, so main.py has to disambiguate; get
    this wrong and either /new keeps the old conversation or /model throws it
    away.
    """

    def _wire(self):
        from haikode import tui as tui_module
        from haikode.schema import Msg
        repl = repl_mod.REPL(self.config, cwd=self.dir)
        repl.agent.messages = [Msg(role="user", content="earlier")]
        captured = {}
        with patch.object(tui_module, "run_tui", captured.update):
            main_mod._start_tui(repl, self.config, self.dir)
        return repl, captured

    def test_new_session_gets_an_empty_agent(self):
        repl, captured = self._wire()
        agent = captured["agent_factory"]()
        self.assertIs(agent, repl.agent)
        self.assertEqual(agent.messages, [])

    def test_a_reprovision_command_keeps_the_conversation(self):
        repl, captured = self._wire()
        captured["on_command"]("/provider openai")
        agent = captured["agent_factory"]()
        self.assertEqual([m.content for m in agent.messages], ["earlier"])
        self.assertEqual(agent.provider.name, "openai")

    def test_the_reprovision_flag_is_consumed(self):
        repl, captured = self._wire()
        captured["on_command"]("/provider openai")
        captured["agent_factory"]()
        self.assertEqual(captured["agent_factory"]().messages, [])

    def test_a_plain_command_does_not_count_as_a_reprovision(self):
        repl, captured = self._wire()
        captured["on_command"]("/tools")
        self.assertEqual(captured["agent_factory"]().messages, [])

    def test_the_command_callback_still_returns_the_display_text(self):
        repl, captured = self._wire()
        self.assertIn("memory_write", captured["on_command"]("/tools"))
        self.assertIsNone(captured["on_command"]("not a command"))


class TestDoctor(WiringTestCase):
    def test_doctor_reports_the_project_config_and_agents(self):
        Path(self.dir, "haikode.json").write_text(json.dumps(
            {"model": "anthropic/claude-sonnet-5"}))
        memory_mod.MemoryStore(self.dir).write("A saved note.", name="note")
        buffer = io.StringIO()
        with patch.object(main_mod, "Config", lambda: self.config), \
                redirect_stdout(buffer):
            main_mod.doctor(self.dir)
        output = buffer.getvalue()
        self.assertIn("Project config", output)
        self.assertIn("haikode.json", output)
        self.assertIn("Prompt variant", output)
        self.assertIn("Agents:", output)
        self.assertIn("Memory: 1 saved", output)
        self.assertIn("memory_write", output)

    def test_doctor_survives_a_broken_project_config(self):
        Path(self.dir, "haikode.json").write_text("{broken")
        buffer = io.StringIO()
        with patch.object(main_mod, "Config", lambda: self.config), \
                redirect_stdout(buffer):
            main_mod.doctor(self.dir)
        self.assertIn("haikode doctor", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
