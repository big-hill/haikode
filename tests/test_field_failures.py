"""Regressions from the 447-message Haiku field session."""

import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from haikode import main as main_mod
from haikode import tui as tui_mod
from haikode import agent as agent_mod
from haikode import agents as agents_mod
from haikode import context as context_mod
from haikode import memory as memory_mod
from haikode import projectconfig as projectconfig_mod
from haikode.config import Config
from haikode.permission import Permissions
from haikode.providers.base import Provider
from haikode.providers.openai_compat import OpenAICompatProvider
from haikode.providers.subscription import ChatGPTSubscriptionProvider
from haikode.repl import REPL
from haikode.runtime import build_agent
from haikode.schema import CompletionChunk, Msg, ToolCall
from haikode.usage import UsageTracker, measure_context

Agent = agent_mod.Agent
MAX_STEPS_PROMPT = getattr(agent_mod, "MAX_STEPS_PROMPT", "")


class ScriptedProvider(Provider):
    name = "scripted"

    def __init__(self, turns):
        self.turns = list(turns)
        self.messages = []
        self.tools = []

    def stream(self, messages, tools, model, max_tokens):
        self.messages.append(list(messages))
        self.tools.append(list(tools))
        for chunk in self.turns.pop(0):
            yield chunk


def tool_turn():
    return [
        CompletionChunk(tool_call_delta={
            "index": 0, "id": "call_1", "name": "list", "arguments": "{}"}),
        CompletionChunk(stop_reason="tool_calls"),
    ]


def text_turn(text):
    return [CompletionChunk(text=text, stop_reason="stop")]


class TemporaryProject(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="haikode-field-")
        self.config_path = Path(self.root, "settings", "config.json")
        self.config_path.parent.mkdir()
        # Without this the memory tests read — and rebuild_index() writes —
        # the developer's real ~/.config/haikode/memory, and fail on any
        # machine that ever saved a user-scope memory.
        self.globals = Path(self.root, "globals")
        self.globals.mkdir()
        self._patches = [
            patch.object(memory_mod, "global_config_dir", lambda: self.globals),
            patch.object(agents_mod, "global_config_dir", lambda: self.globals),
            patch.object(context_mod, "global_config_dir", lambda: self.globals),
            patch.object(projectconfig_mod, "global_config_dir",
                         lambda: self.globals),
        ]
        for entry in self._patches:
            entry.start()

    def tearDown(self):
        for entry in self._patches:
            entry.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def config(self, **settings):
        self.config_path.write_text(json.dumps(settings))
        return Config(str(self.config_path))


class StepBudgetRegressions(TemporaryProject):
    def test_last_step_is_a_tool_free_handoff_and_next_input_continues(self):
        provider = ScriptedProvider([
            tool_turn(),
            text_turn("Budget reached. The parser tests remain."),
            text_turn("Parser tests are now complete."),
        ])
        agent = Agent(provider, "test", cwd=self.root, max_steps=2,
                      tool_names=["list"],
                      permissions=Permissions(auto_approve=True))
        events = []

        result = agent.run("inspect it", on_event=lambda kind, data:
                           events.append((kind, data)))

        self.assertEqual(result, "Budget reached. The parser tests remain.")
        self.assertTrue(provider.tools[0])
        self.assertEqual(provider.tools[1], [])
        self.assertEqual(provider.messages[1][-1].content, MAX_STEPS_PROMPT)
        self.assertEqual(events[-1][0], "limit")
        self.assertTrue(events[-1][1]["continuable"])
        self.assertNotIn("[stopped after", result)

        self.assertEqual(agent.run("continue"),
                         "Parser tests are now complete.")
        self.assertTrue(provider.tools[2])

    def test_no_limit_is_the_default(self):
        config = self.config()
        self.assertIsNone(config.data["max_steps"])
        agent = build_agent(config, cwd=self.root)
        self.assertIsNone(agent.max_steps)
        self.assertIn("External edits take effect only after the user runs /reload",
                      agent._system_message().content)

    def test_reload_applies_an_external_edit_without_losing_history(self):
        config = self.config(max_steps=2)
        repl = REPL(config, cwd=self.root)
        self.addCleanup(repl.turn.close)
        repl.agent.messages = [Msg(role="user", content="earlier")]
        self.config_path.write_text(json.dumps({"max_steps": 7}))

        output = repl.handle_command("/reload")

        self.assertEqual(repl.agent.max_steps, 7)
        self.assertEqual([message.content for message in repl.agent.messages],
                         ["earlier"])
        self.assertIn("applied to this live session", output)
        self.assertIn(str(self.config_path), repl.handle_command("/config"))

    def test_bad_reload_keeps_the_live_snapshot(self):
        config = self.config(max_steps=3)
        repl = REPL(config, cwd=self.root)
        self.addCleanup(repl.turn.close)
        old = repl.agent
        self.config_path.write_text("{broken")

        output = repl.handle_command("/reload")

        self.assertIn("[error]", output)
        self.assertIs(repl.agent, old)
        self.assertEqual(repl.agent.max_steps, 3)

    def test_unusable_reload_rolls_back_config_and_agent(self):
        config = self.config(
            default_provider="local",
            providers={"local": {"base_url": "http://127.0.0.1:9/v1",
                                 "model": "m"}})
        repl = REPL(config, provider="local", cwd=self.root)
        self.addCleanup(repl.turn.close)
        old_agent = repl.agent
        old_data = config.data
        self.config_path.write_text(json.dumps({
            "default_provider": "local",
            "providers": {"local": {"base_url": 42, "model": "m"}},
        }))

        output = repl.handle_command("/reload")

        self.assertIn("[error]", output)
        self.assertIs(repl.agent, old_agent)
        self.assertIs(config.data, old_data)

    def test_unsupported_effort_reload_applies_with_a_warning(self):
        # An effort outside the model's whitelist must not be "unusable":
        # killing the reload (or the session) over it strands the user, so
        # the value is dropped with a visible warning instead.
        config = self.config(
            default_provider="chatgpt",
            providers={"chatgpt": {"model": "gpt-5.4",
                                   "reasoning_effort": "medium"}})
        repl = REPL(config, provider="chatgpt", cwd=self.root)
        self.addCleanup(repl.turn.close)
        self.config_path.write_text(json.dumps({
            "default_provider": "chatgpt",
            "providers": {"chatgpt": {"model": "gpt-5.4",
                                      "reasoning_effort": "max"}},
        }))

        output = repl.handle_command("/reload")

        self.assertNotIn("[error]", output)
        self.assertNotEqual(repl.agent.reasoning_effort, "max")
        self.assertTrue(any("reasoning effort" in w
                            for w in repl.warnings()), repl.warnings())


class UnreadableCredentialsAreNotASignedOutUser(unittest.TestCase):
    """From the field: "not signed in to chatgpt" with valid tokens on disk.

    _read() swallowed every failure and returned {}, so a moment when the
    file could not be read looked exactly like never having logged in. The
    advice that came with it was actively harmful: `login` would have
    overwritten working credentials.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="haikode-oauth-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = Path(self.root, "oauth.json")

    def _store(self):
        from haikode.oauth import OAuthStore
        return OAuthStore(str(self.path))

    def test_a_missing_file_still_says_not_signed_in(self):
        from haikode.oauth import OAuthError, access_token
        with self.assertRaises(OAuthError) as caught:
            access_token("chatgpt", self._store())
        self.assertIn("Not signed in", str(caught.exception))

    def test_an_unreadable_file_says_so_and_does_not_advise_a_relogin(self):
        from haikode.oauth import OAuthError, access_token
        self.path.write_text("{ this is not json")
        with self.assertRaises(OAuthError) as caught:
            access_token("chatgpt", self._store())
        message = str(caught.exception)
        self.assertIn("Could not read", message)
        self.assertIn("nothing was changed", message)
        self.assertNotIn("Not signed in", message)

    def test_a_read_that_recovers_on_retry_is_not_an_error_at_all(self):
        import time as _time
        store = self._store()
        self.path.write_text("{ broken")
        real_open = Path.open
        calls = {"n": 0}

        def flaky(self_path, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("transient")
            return real_open(self_path, *args, **kwargs)

        good = json.dumps({"chatgpt": {"access": "a", "refresh": "r",
                                       "expires": int(
                                           (_time.time() + 3600) * 1000)}})
        self.path.write_text(good)
        with patch.object(Path, "open", flaky):
            tokens = store.get("chatgpt")
        self.assertEqual("a", tokens.get("access"))
        self.assertEqual("", store.read_error)
        self.assertGreater(calls["n"], 1, "the retry never happened")


class TheStoreKeepsItsOwnBackups(unittest.TestCase):
    """Two defects destroyed a live store in one day.

    Both times the conversations survived only because someone had taken a
    copy by hand. The store now takes verified, rotating snapshots itself.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="haikode-bak-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.db = Path(self.root, "sessions.db")

    def _store(self):
        from haikode.session import SessionStore
        store = SessionStore(self.db)
        self.addCleanup(store.close)
        return store

    def _snapshots(self):
        return sorted(p.name for p in Path(self.root).glob("sessions.db.bak*"))

    def test_opening_a_populated_store_leaves_a_verified_snapshot(self):
        first = self._store()
        first.new_session("/p", "zen", "m")
        first.close()
        self._store().connect()

        self.assertIn("sessions.db.bak1", self._snapshots())
        backup = sqlite3.connect(str(Path(self.root, "sessions.db.bak1")))
        self.addCleanup(backup.close)
        self.assertEqual("ok",
                         backup.execute("PRAGMA quick_check").fetchone()[0])
        self.assertEqual(
            1, backup.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])

    def test_snapshots_rotate_instead_of_overwriting_one_slot(self):
        from haikode.session import BACKUP_GENERATIONS, SessionStore
        for _ in range(BACKUP_GENERATIONS + 2):
            store = SessionStore(self.db)
            store.new_session("/p", "zen", "m")
            store.close()
        self.assertEqual(
            ["sessions.db.bak%d" % n
             for n in range(1, BACKUP_GENERATIONS + 1)],
            self._snapshots())

    def test_a_corrupt_store_never_overwrites_a_good_snapshot(self):
        store = self._store()
        store.new_session("/p", "zen", "m")
        store.close()
        self._store().connect()          # snapshot taken while healthy
        good = Path(self.root, "sessions.db.bak1")
        keep = good.read_bytes()

        with open(self.db, "r+b") as handle:   # scribble over the page data
            handle.seek(4096)
            handle.write(b"\xff" * 8192)

        from haikode.session import SessionStore
        try:
            SessionStore(self.db).connect()
        except Exception:
            pass

        self.assertEqual(keep, good.read_bytes(),
                         "a corrupt store overwrote the last good snapshot")


class AFailedTurnLeavesNoTrace(TemporaryProject):
    """From the field: the same question stored twice, 50 seconds apart.

    A request that failed left the user's message standing alone. The model
    saw the question again on the retry, /resume replayed a conversation
    where the user apparently asked twice and was ignored once, and
    session_history showed the same. The error must not become an assistant
    message either — that would replay to the provider as words the model
    never said — so the whole exchange is rolled back.
    """

    def _failing_repl(self):
        config = self.config(
            default_provider="x",
            providers={"x": {"base_url": "http://127.0.0.1:9/v1",
                             "model": "m"}})
        repl = REPL(config, provider="x", cwd=self.root)
        self.addCleanup(repl.turn.close)
        return repl

    def test_resending_after_a_failure_does_not_duplicate_the_question(self):
        repl = self._failing_repl()
        repl.send("find the bug")
        repl.send("find the bug")

        self.assertEqual([], repl.agent.messages)
        session = repl.turn.session
        stored = list(session.messages) if session is not None else []
        self.assertEqual([], [m for m in stored if m.role == "user"])

    def test_the_error_is_never_stored_as_something_the_model_said(self):
        repl = self._failing_repl()
        repl.send("find the bug")
        self.assertEqual(
            [], [m for m in repl.agent.messages if m.role == "assistant"])


class SessionHistoryIsReachable(unittest.TestCase):
    """Asked three times in the field: "what did we work on last session?"

    Sessions were on disk, listable and loadable — nothing exposed them to
    the model, so it answered "I cannot see the previous session" every time.
    """

    def setUp(self):
        self.sandbox = tempfile.mkdtemp(prefix="haikode-hist-")
        self._patch = patch.dict(os.environ,
                                 {"HAIKODE_CONFIG_DIR": self.sandbox})
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(shutil.rmtree, self.sandbox, True)

    def _store_with_a_past_session(self):
        from haikode.session import SessionStore
        store = SessionStore()
        session = store.new_session("/work/proj", "chatgpt", "gpt-5.6-sol")
        session.append(Msg(role="user", content="port OnlyOffice to Haiku"))
        session.append(Msg(role="assistant", tool_calls=[
            ToolCall(id="c1", name="grep", arguments={})]))
        session.append(Msg(role="tool", tool_call_id="c1", content="x" * 5000))
        session.append(Msg(role="assistant",
                           content="Startup reaches app:ready."))
        store.close()
        return session.id

    def _tool(self):
        from haikode.tool import REGISTRY
        return REGISTRY["session_history"]

    def _ctx(self):
        from haikode.tool.base import ToolContext
        return ToolContext(cwd="/work/proj",
                           permissions=Permissions(auto_approve=True))

    def test_the_model_has_the_tool_and_is_told_it_exists(self):
        from haikode.prompt import capability_guidance
        from haikode.tool import REGISTRY
        self.assertIn("session_history", REGISTRY)
        guidance = capability_guidance(("session_history", "read"))
        self.assertIn("session_history", guidance)
        self.assertIn("previous", guidance.lower())

    def test_recent_sessions_are_listed_for_this_workspace(self):
        session_id = self._store_with_a_past_session()
        result = self._tool().execute({}, self._ctx())
        self.assertIn(session_id, result.output)
        self.assertIn("4 messages", result.output)

    def test_a_transcript_reads_back_without_the_tool_output(self):
        session_id = self._store_with_a_past_session()
        result = self._tool().execute({"session_id": session_id}, self._ctx())
        self.assertIn("port OnlyOffice to Haiku", result.output)
        self.assertIn("Startup reaches app:ready", result.output)
        self.assertIn("[ran: grep]", result.output)
        self.assertNotIn("x" * 100, result.output)

    def test_an_empty_workspace_says_so_and_offers_the_wider_search(self):
        result = self._tool().execute({}, self._ctx())
        self.assertIn("No earlier sessions", result.output)
        self.assertIn("all_projects", result.output)

    def test_the_current_session_is_not_listed_as_earlier_work(self):
        from haikode.session import SessionStore
        from haikode.tool.base import ToolContext
        store = SessionStore()
        self.addCleanup(store.close)
        live = store.new_session("/work/proj", "chatgpt", "gpt-5.6-sol")
        live.append(Msg(role="user", content="what did we do last time"))
        ctx = ToolContext(cwd="/work/proj", session=live,
                          permissions=Permissions(auto_approve=True))

        output = self._tool().execute({}, ctx).output

        self.assertNotIn(live.id, output)

    def test_an_unknown_id_is_an_error_not_an_empty_transcript(self):
        with self.assertRaises(ValueError) as caught:
            self._tool().execute({"session_id": "ses_nope"}, self._ctx())
        self.assertIn("without arguments", str(caught.exception))


class TestsCannotReachTheRealStore(unittest.TestCase):
    """The suite must not write sessions into the user's own store.

    A run on the Haiku machine left 96 test sessions in a picker holding
    125 — every forgotten patch of global_config_dir landed in
    ~/config/settings/haikode.
    """

    def test_the_sandbox_is_active_and_is_not_the_users_directory(self):
        import os
        from haikode.context import global_config_dir
        from haikode.session import default_db_path

        sandbox = os.environ.get("HAIKODE_CONFIG_DIR")
        self.assertTrue(sandbox, "tests/__init__.py did not set the sandbox")
        self.assertEqual(str(global_config_dir()),
                         str(Path(sandbox).expanduser()))
        self.assertTrue(str(default_db_path()).startswith(str(Path(sandbox))))

        for real in (Path.home() / ".config" / "haikode",
                     Path.home() / "config" / "settings" / "haikode"):
            self.assertNotEqual(global_config_dir().resolve(), real.resolve())

    def test_a_default_session_store_lands_in_the_sandbox(self):
        from haikode.session import SessionStore
        store = SessionStore()
        self.addCleanup(store.close)
        session = store.new_session(".", "zen", "m")
        self.assertTrue(str(store.path).startswith(
            str(Path(os.environ["HAIKODE_CONFIG_DIR"]))))
        self.assertTrue(session.id.startswith("ses_"))


class DefaultsStayLive(TemporaryProject):
    """Saving must not freeze shipped defaults into the user's file.

    The field bug: max_steps 20 was the old default, some save() wrote it out
    as if the user had chosen it, and the later change to "no limit" could
    never reach the machine. Every turn kept stopping after 20 steps.
    """

    def test_saving_does_not_write_defaults_into_the_file(self):
        config = self.config(default_provider="zen",
                             providers={"zen": {"model": "m"}})
        self.assertIsNone(config.data.get("max_steps"))

        config.data["default_provider"] = "chatgpt"
        config.save()

        on_disk = json.loads(self.config_path.read_text())
        self.assertNotIn("max_steps", on_disk)
        self.assertEqual(on_disk["default_provider"], "chatgpt")
        self.assertEqual(on_disk["providers"]["zen"]["model"], "m")

    def test_a_deliberate_non_default_value_survives(self):
        config = self.config(max_steps=5, providers={"zen": {"model": "m"}})
        config.save()
        self.assertEqual(json.loads(self.config_path.read_text())["max_steps"],
                         5)
        self.assertEqual(Config(str(self.config_path)).data["max_steps"], 5)

    def test_a_frozen_old_default_no_longer_outlives_a_save(self):
        # The exact repro: a file carrying the retired default.
        self.config_path.write_text(json.dumps({"max_steps": 20}))
        config = Config(str(self.config_path))
        self.assertEqual(config.data["max_steps"], 20)  # explicit: still honoured

        config.data["max_steps"] = None                 # user clears it
        config.save()

        self.assertNotIn("max_steps",
                         json.loads(self.config_path.read_text()))
        self.assertIsNone(Config(str(self.config_path)).data["max_steps"])


class YoloWiring(TemporaryProject):
    """--yolo and /yolo must reach the objects that actually gate things."""

    def _repl(self, **kwargs):
        config = self.config(default_provider="zen",
                             providers={"zen": {"model": "m"}})
        repl = REPL(config, provider="zen", cwd=self.root, **kwargs)
        self.addCleanup(repl.turn.close)
        return repl

    def test_the_cli_flag_reaches_the_agents_permissions(self):
        args = main_mod.build_parser().parse_args(["--yolo", "-p", "zen"])
        self.assertTrue(args.yolo)
        repl = self._repl(yolo=True)
        self.assertTrue(repl.permissions.yolo)
        self.assertTrue(repl.agent.permissions.yolo)
        self.assertEqual(repl.agent.permissions.decide("bash", "anything"),
                         "allow")

    def test_the_command_toggles_the_live_session_both_ways(self):
        repl = self._repl()
        self.assertFalse(repl.agent.permissions.yolo)

        on = repl.handle_command("/yolo")
        self.assertTrue(repl.agent.permissions.yolo)
        self.assertIn("yolo ON", on)

        off = repl.handle_command("/yolo")
        self.assertFalse(repl.agent.permissions.yolo)
        self.assertIn("off", off)

    def test_yolo_trusts_project_command_files(self):
        repl = self._repl(yolo=True)
        self.assertIs(repl.commands.trusted, True)

    def test_the_footer_shows_yolo_and_never_drops_it_first(self):
        line = tui_mod.build_status("prov", "dir", 0, 0, 80,
                                    tui_mod.Glyphs(False), yolo=True)
        self.assertIn("YOLO", line)
        narrow = tui_mod.build_status("prov", "dir", 0, 0, 24,
                                      tui_mod.Glyphs(False), yolo=True)
        self.assertIn("YOLO", narrow)


class ResponsesToolEncodingRegressions(TemporaryProject):
    """From the OnlyOffice field session: todo JSON came back as user text.

    The Responses payload flattened tool results into user messages and
    dropped the assistant's own function calls entirely, so the model saw
    the "user" pasting todo JSON it had no memory of requesting — and spent
    the whole step budget answering it.
    """

    def test_tool_traffic_uses_function_items_not_user_messages(self):
        captured = {}

        def events(url, payload, **kwargs):
            captured.update(payload)
            yield {"type": "response.completed",
                   "response": {"usage": {"input_tokens": 1,
                                          "output_tokens": 1}}}

        provider = ChatGPTSubscriptionProvider(object())
        history = [
            Msg(role="user", content="rydd opp i loggene"),
            Msg(role="assistant", tool_calls=[
                ToolCall(id="call_1", name="todowrite",
                         arguments={"todos": [{"text": "les logg"}]})]),
            Msg(role="tool", tool_call_id="call_1",
                content='{"todos": [{"text": "les logg"}]}'),
        ]
        with patch.object(provider, "_headers", return_value={}), \
                patch("haikode.providers.subscription.sse_json_events",
                      events):
            list(provider.stream(history, [], "gpt-5.6-sol", 100))

        items = captured["input"]
        types = [item.get("type") for item in items]
        self.assertIn("function_call", types)
        self.assertIn("function_call_output", types)
        output = next(item for item in items
                      if item.get("type") == "function_call_output")
        self.assertEqual(output["call_id"], "call_1")
        call = next(item for item in items
                    if item.get("type") == "function_call")
        self.assertEqual(call["call_id"], "call_1")
        self.assertEqual(call["name"], "todowrite")
        user_texts = [part["text"] for item in items
                      if item.get("role") == "user"
                      for part in item.get("content", [])]
        self.assertEqual(user_texts, ["rydd opp i loggene"])


class MemoryAndTodoRegressions(TemporaryProject):
    def agent(self):
        return Agent(ScriptedProvider([]), "gpt-5.6-sol", cwd=self.root,
                     tool_names=["memory_write", "memory_read", "todowrite"],
                     permissions=Permissions(auto_approve=True))

    def test_empty_session_teaches_selective_memory_and_task_tracking(self):
        content = self.agent()._system_message().content

        self.assertIn("No saved memories yet.", content)
        self.assertIn("Use memory_write", content)
        self.assertIn("Do not write a memory every turn", content)
        self.assertIn("current-task progress", content)
        self.assertIn("three or more meaningful steps", content)
        self.assertIn("Skip it for simple tasks", content)

    def test_a_new_agent_reads_what_an_earlier_session_saved(self):
        first = self.agent()
        first.memory.write("The owner prefers Norwegian.", name="language")

        second = self.agent()

        self.assertIn("The owner prefers Norwegian.",
                      second._system_message().content)

    def test_memory_command_shows_the_editable_locations_even_when_empty(self):
        config = self.config()
        repl = REPL(config, cwd=self.root)
        self.addCleanup(repl.turn.close)

        output = repl.handle_command("/memory")

        self.assertIn("Editable project memories:", output)
        self.assertIn("Editable user memories:", output)


class ReasoningAndContextRegressions(TemporaryProject):
    def test_reasoning_effort_reaches_the_chatgpt_request(self):
        captured = {}

        def events(_url, payload, **_kwargs):
            captured.update(payload)
            yield {"type": "response.completed",
                   "response": {"usage": {"input_tokens": 10,
                                          "output_tokens": 2}}}

        provider = ChatGPTSubscriptionProvider(object())
        provider.set_reasoning_effort("max", "gpt-5.6-sol")
        with patch.object(provider, "_headers", return_value={}), \
                patch("haikode.providers.subscription.sse_json_events", events):
            list(provider.stream([], [], "gpt-5.6-sol", 100))

        self.assertEqual(captured["reasoning"]["effort"], "max")
        self.assertEqual(captured["tool_choice"], "none")

    def test_effort_has_cli_and_live_session_controls(self):
        self.assertEqual(main_mod.build_parser().parse_args(
            ["--effort", "xhigh"]).effort, "xhigh")
        # Parsing alone is not wiring: the flag must reach the REPL override
        # that build_agent applies, or --effort silently becomes a no-op —
        # the exact class of defect this file exists to catch.
        cli_config = self.config(
            default_provider="chatgpt",
            providers={"chatgpt": {"model": "gpt-5.6-sol"}})
        cli_args = main_mod.build_parser().parse_args(
            ["--effort", "xhigh", "-p", "chatgpt"])
        cli_repl = main_mod.build_repl(cli_config, cli_args, self.root,
                                       report=lambda *_: None)
        self.addCleanup(cli_repl.turn.close)
        self.assertEqual(cli_repl.reasoning_effort_override, "xhigh")
        self.assertEqual(cli_repl.agent.reasoning_effort, "xhigh")
        config = self.config(
            default_provider="chatgpt",
            providers={"chatgpt": {"model": "gpt-5.6-sol",
                                   "reasoning_effort": "medium"}})
        repl = REPL(config, provider="chatgpt", cwd=self.root)
        self.addCleanup(repl.turn.close)

        output = repl.handle_command("/effort high")

        self.assertEqual(repl.agent.reasoning_effort, "high")
        self.assertIn("live session", output)

    def test_chatgpt_gpt56_uses_the_backend_profile_not_stale_config(self):
        config = self.config(
            default_provider="chatgpt",
            providers={"chatgpt": {"model": "gpt-5.6-sol",
                                   "context": 200000}})

        agent = build_agent(config, "chatgpt", self.root)

        self.assertEqual(agent.context_window, 500000)
        self.assertEqual(agent.context_source, "ChatGPT backend profile")
        repl = REPL(config, provider="chatgpt", cwd=self.root)
        self.addCleanup(repl.turn.close)
        self.assertIn("Window source: ChatGPT backend profile",
                      repl.handle_command("/context"))

    def test_legacy_provider_keeps_the_configured_context_fallback(self):
        class StreamOnlyProvider:
            def stream(self, messages, tools, model, max_tokens):
                yield CompletionChunk(text="ok", stop_reason="stop")

        config = self.config(
            default_provider="ollama-local",
            providers={"ollama-local": {"model": "fixture",
                                        "context": 32100}})
        with patch("haikode.runtime.build_provider",
                   return_value=StreamOnlyProvider()):
            agent = build_agent(config, cwd=self.root)

        self.assertEqual(agent.context_window, 32100)
        self.assertEqual(agent.context_source, "configuration")

    def test_context_meter_prefers_the_latest_provider_count(self):
        class Counted:
            context_window = 500000
            messages = [Msg(role="user", content="x" * 40000)]
            specs = []
            system_prompt = "y" * 40000

            def __init__(self):
                self.usage = UsageTracker()
                self.usage.record({"input": 100, "output": 10,
                                   "reasoning": 5, "cache_read": 20})

        self.assertEqual(measure_context(Counted()).used, 135)

    def test_openai_usage_does_not_double_count_cache_or_reasoning(self):
        usage = OpenAICompatProvider._usage({
            "prompt_tokens": 100,
            "prompt_tokens_details": {"cached_tokens": 40},
            "completion_tokens": 20,
            "completion_tokens_details": {"reasoning_tokens": 5},
        })
        self.assertEqual(usage, {"input": 60, "output": 15,
                                 "cache_read": 40, "reasoning": 5})


class TUIRegressions(unittest.TestCase):
    def ui(self):
        ui = tui_mod.TUI(lambda: None, config=None, cwd=".")
        self.addCleanup(ui.turn.close)
        return ui

    def test_raw_haiku_pageup_sequence_scrolls_instead_of_being_dropped(self):
        ui = self.ui()
        with patch.object(ui, "_peek_key", side_effect=["[", "5", "~"]), \
                patch.object(ui, "_page_up") as page_up:
            ui._handle_key(27)
        page_up.assert_called_once_with()

    def test_configured_line_scroll_binding_is_dispatched(self):
        ui = self.ui()
        with patch.object(ui, "_line_up") as line_up:
            consumed = ui._keymap_key(
                tui_mod.keybind.KeyEvent(key="y", ctrl=True, alt=True))
        self.assertTrue(consumed)
        line_up.assert_called_once_with()

    def test_reload_cannot_replace_the_agent_during_an_active_turn(self):
        ui = self.ui()
        ui.running = True
        calls = []
        ui.on_command = lambda line: calls.append(line) or "reloaded"

        ui._dispatch_command("/reload")

        self.assertEqual(calls, [])
        self.assertIn("not reloaded", ui.transcript.entries[-1].text)

    def test_transcript_and_composer_share_the_same_column(self):
        ui = self.ui()
        ui._size = lambda: (30, 100)
        home = ui._frame()
        self.assertGreater(home.box_left, 2)
        ui.transcript.add(tui_mod.Entry("assistant", text="word " * 80))
        frame = ui._frame()
        lines = ui._view_lines()
        self.assertTrue(lines)
        self.assertEqual(frame.box_left, 2)
        self.assertEqual(frame.box_width, 96)
        self.assertLessEqual(max(len(line.text) for line in lines),
                             frame.content_width)

        positions = []
        with patch.object(ui, "_addstr",
                          side_effect=lambda _y, x, *_args: positions.append(x)), \
                patch.object(ui, "_draw_prompt_box"), \
                patch.object(ui, "_draw_hint_row"):
            ui._draw_transcript(frame)
        self.assertTrue(positions)
        self.assertEqual(set(positions), {frame.box_left + 2})

    def test_footer_does_not_repeat_the_prompt_context_meter(self):
        ui = self.ui()
        ui._size = lambda: (30, 100)
        captured = {}

        def status_line(*_args, **kwargs):
            captured.update(kwargs)
            return ""

        with patch.object(tui_mod, "build_status", status_line), \
                patch.object(ui, "_addstr"):
            ui._draw_footer(ui._frame())
        self.assertEqual(captured["context"], "")


if __name__ == "__main__":
    unittest.main()
