"""Regressions from the 447-message Haiku field session."""

import errno
import io
import json
import os
import shutil
import sys
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from haikode import main as main_mod
from haikode import tui as tui_mod
from haikode import agent as agent_mod
from haikode import agents as agents_mod
from haikode import configtool as configtool_mod
from haikode import context as context_mod
from haikode import memory as memory_mod
from haikode import models as models_mod
from haikode import projectconfig as projectconfig_mod
from haikode.config import Config
from haikode.permission import Permissions
from haikode.providers.base import Provider
from haikode.providers.openai_compat import OpenAICompatProvider
from haikode.providers.subscription import ChatGPTSubscriptionProvider
from haikode.repl import REPL
from haikode.runtime import build_agent
from haikode.turn import TurnController
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

    def test_every_failed_attempt_is_recorded_even_when_a_retry_wins(self):
        # The one field report cost us the answer because the code caught the
        # exception and dropped its errno. A read that recovers is the only
        # trace a transient failure leaves, so it must not vanish.
        from haikode.oauth import CREDENTIAL_LOG
        store = self._store()
        self.path.write_text("{ broken")
        store.get("chatgpt")

        log = Path(self.root, CREDENTIAL_LOG)
        self.assertTrue(log.exists(), "no diagnostics were written")
        entries = [json.loads(line) for line in
                   log.read_text().splitlines() if line.strip()]
        self.assertEqual(3, len(entries), "one line per attempt")
        first = entries[0]
        self.assertIn("file", first)
        self.assertIn("ino", first["file"])
        self.assertIn("parent", first)
        self.assertIsInstance(first["fds_open"], int)
        self.assertNotIn("access", log.read_text(),
                         "diagnostics must never carry token material")

    def test_an_errno_is_kept_where_the_old_code_discarded_it(self):
        from haikode.oauth import _describe_read_failure
        # errno.EMFILE, not the literal 24: Haiku numbers its errnos from a
        # different base (-2147459062 there), so a hardcoded POSIX value makes
        # this pass on Linux and macOS and fail on the platform the project
        # targets. Reported from a real hrev59917 install.
        described = _describe_read_failure(
            OSError(errno.EMFILE, "Too many open files"))
        self.assertIn("errno %d" % errno.EMFILE, described)
        self.assertIn("EMFILE", described)


class AResumedSessionShowsItsHistory(unittest.TestCase):
    """`--session` and `--continue` came up looking like an empty chat.

    The conversation was restored into the agent before the screen existed,
    and the transcript is built from entries rather than from messages, so
    every following turn behaved as though the history were there while the
    user could see none of it. The picker's /resume already replayed.
    """

    def _agent_with_history(self):
        class Restored:
            def __init__(self):
                self.messages = [
                    Msg(role="user", content="fix the parser"),
                    Msg(role="assistant", content="Reading the file."),
                    Msg(role="assistant",
                        tool_calls=[ToolCall(id="c1", name="read",
                                             arguments={})]),
                    Msg(role="tool", tool_call_id="c1", content="line 1"),
                    Msg(role="assistant", content="The bug is on line 2."),
                ]
        return Restored()

    def test_startup_replays_a_restored_conversation(self):
        agent = self._agent_with_history()
        ui = tui_mod.TUI(lambda: agent, config=None, cwd=".")

        ui._startup_agent()

        rendered = [entry.text for entry in ui.transcript.entries
                    if entry.kind in ("user", "assistant")]
        self.assertIn("fix the parser", rendered)
        self.assertIn("The bug is on line 2.", rendered)
        self.assertTrue(ui.follow, "a resumed session should start at the end")

    def test_a_fresh_start_still_shows_the_home_screen(self):
        class Empty:
            messages = []
        ui = tui_mod.TUI(lambda: Empty(), config=None, cwd=".")
        ui._startup_agent()
        self.assertEqual([], ui.transcript.entries)

    def test_replaying_twice_does_not_double_the_history(self):
        agent = self._agent_with_history()
        ui = tui_mod.TUI(lambda: agent, config=None, cwd=".")
        ui._startup_agent()
        before = len(ui.transcript.entries)
        ui._startup_agent()
        self.assertEqual(before, len(ui.transcript.entries))


class AddingALocalProviderNeedsNoKey(TemporaryProject):
    """`/provider add` demanded a key for endpoints that have no login.

    Found reviewing the "add a provider" flow: pointing at a local Ollama
    produced a profile with requires_key=True, so /status reported "no key
    set", the home screen said "run /login <name>", and the provider counted
    as unusable — for a service with no authentication at all.
    """

    def _repl(self):
        config = self.config(default_provider="zen", providers={
            "zen": {"model": "m", "api_key": "public",
                    "base_url": "https://opencode.ai/zen/v1"}})
        repl = REPL(config, provider="zen", cwd=self.root)
        self.addCleanup(repl.turn.close)
        return repl

    def test_a_loopback_endpoint_is_usable_immediately(self):
        repl = self._repl()
        repl.handle_command(
            "/provider add home http://127.0.0.1:11434/v1 qwen3 openai")

        profile = repl.config.data["providers"]["home"]
        self.assertFalse(profile.get("requires_key", True))
        self.assertEqual("n/a", repl.config.key_source("home"))
        self.assertIn("no key required", repl.handle_command("/provider home"))

    def test_a_lan_endpoint_is_treated_the_same(self):
        repl = self._repl()
        repl.handle_command(
            "/provider add tower http://192.168.1.50:11434/v1 qwen3 openai")
        self.assertFalse(
            repl.config.data["providers"]["tower"].get("requires_key", True))

    def test_a_hosted_endpoint_still_asks_for_one(self):
        repl = self._repl()
        repl.handle_command(
            "/provider add cloud https://api.example.com/v1 gpt openai")
        self.assertTrue(
            repl.config.data["providers"]["cloud"].get("requires_key"))
        self.assertEqual("none", repl.config.key_source("cloud"))

    def test_the_dialogs_needs_key_row_defaults_to_auto(self):
        """The form had a yes/no toggle defaulting to yes, which overrode the
        endpoint rule before it could apply. It is now three-way."""
        field = tui_mod.FormField("requires_key", "Needs key", "auto",
                                  kind="tribool")
        form = tui_mod.FormDialog("add_provider", "Add provider", [field])
        self.assertIsNone(form.values()["requires_key"])

        from haikode.keybind import KeyEvent, Keymap
        keymap = Keymap()
        press = lambda key: form.handle(KeyEvent(key=key), keymap)
        press("space")
        self.assertIs(True, form.values()["requires_key"])
        press("space")
        self.assertIs(False, form.values()["requires_key"])
        press("space")
        self.assertIsNone(form.values()["requires_key"])

    def test_the_classification_covers_the_shapes_people_use(self):
        from haikode.models import _is_local_endpoint
        for url in ("http://127.0.0.1:11434/v1", "http://localhost:11434/v1",
                    "http://192.168.1.20:11434/v1", "http://10.0.0.5:11434/v1",
                    "http://100.64.0.1:11434/v1", "http://tower.local:11434/v1"):
            self.assertTrue(_is_local_endpoint(url), url)
        for url in ("https://api.openai.com/v1", "https://opencode.ai/zen/v1",
                    "https://ollama.com/v1"):
            self.assertFalse(_is_local_endpoint(url), url)


class ATransientBackendErrorIsRetried(unittest.TestCase):
    """The field failure the owner hit repeatedly: `server_error`.

    Established by measurement, not assumption: an over-long prompt returns a
    clean `context_overflow` (so it is not size), a tool chain with no
    reasoning items is accepted (so it is not shape), and a 308k-token real
    history that had failed earlier went through unchanged (so it is
    transient). `classify_error` already marked it retryable; nothing acted
    on that, because net.py's retry only covers failures before the stream
    starts and this one arrives as an SSE event.
    """

    def _provider(self):
        from haikode.providers.subscription import ChatGPTSubscriptionProvider
        return ChatGPTSubscriptionProvider(object())

    @staticmethod
    def _error_event():
        return {"type": "error", "error": {"message": "An error occurred.",
                                           "code": "server_error",
                                           "type": "server_error"}}

    def _run(self, events_factory):
        from haikode.providers import subscription as sub
        provider = self._provider()
        with patch.object(provider, "_headers", return_value={}), \
                patch.object(sub, "sse_json_events", events_factory), \
                patch.object(sub, "RETRY_BACKOFF_SECONDS", 0.001):
            return list(provider.stream([Msg(role="user", content="go")], [],
                                        "gpt-5.6-sol", 10))

    def test_a_blip_is_retried_and_the_turn_succeeds(self):
        calls = {"n": 0}

        def flaky(url, payload, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                yield self._error_event()
                return
            yield {"type": "response.output_text.delta", "delta": "PONG"}
            yield {"type": "response.completed",
                   "response": {"usage": {"input_tokens": 5, "output_tokens": 1}}}

        chunks = self._run(flaky)
        self.assertEqual("PONG", "".join(c.text or "" for c in chunks))
        self.assertEqual(3, calls["n"])

    def test_a_persistent_failure_still_gives_up(self):
        calls = {"n": 0}

        def always(url, payload, **kwargs):
            calls["n"] += 1
            yield self._error_event()

        chunks = self._run(always)
        errors = [c for c in chunks if (c.usage or {}).get("error")]
        self.assertEqual(1, len(errors), "the caller must see one failure")
        self.assertEqual(3, calls["n"], "and not retry forever")

    def test_a_failure_after_output_is_not_retried(self):
        # Re-sending a turn whose tokens are already out would duplicate them.
        calls = {"n": 0}

        def late(url, payload, **kwargs):
            calls["n"] += 1
            yield {"type": "response.output_text.delta", "delta": "half"}
            yield self._error_event()

        self._run(late)
        self.assertEqual(1, calls["n"])


class SteeringReachesTheModelMidTurn(TemporaryProject):
    """A correction typed mid-run must not wait for the turn to end.

    With no step limit a turn runs for many minutes, so a prompt held until
    the end arrives after the work it was meant to redirect. And between
    typing and the next step the user may change their mind, so what is
    pending has to be visible and editable.
    """

    def _agent(self, provider):
        return Agent(provider, "m", cwd=self.root, tool_names=["list"],
                     permissions=Permissions(auto_approve=True))

    def test_it_is_delivered_at_the_next_step_not_at_the_end(self):
        provider = ScriptedProvider([tool_turn(), text_turn("done")])
        agent = self._agent(provider)
        agent.steer("actually, do B instead")

        agent.run("do A")

        second = [(m.role, m.content) for m in provider.messages[1]
                  if m.role == "user"]
        self.assertIn(("user", "actually, do B instead"), second)

    def test_an_event_says_it_was_delivered(self):
        provider = ScriptedProvider([text_turn("done")])
        agent = self._agent(provider)
        agent.steer("note this")
        events = []
        agent.run("go", on_event=lambda kind, data: events.append((kind, data)))
        self.assertIn("steered", [kind for kind, _ in events])

    def test_pending_messages_can_be_listed_edited_and_dropped(self):
        provider = ScriptedProvider([text_turn("done")])
        agent = self._agent(provider)
        agent.steer("first")
        agent.steer("second")
        self.assertEqual(["first", "second"], agent.pending_steering())

        self.assertTrue(agent.edit_steering(0, "corrected"))
        self.assertEqual(["corrected", "second"], agent.pending_steering())

        self.assertTrue(agent.edit_steering(1, "   "))   # empty drops it
        self.assertEqual(["corrected"], agent.pending_steering())

        self.assertFalse(agent.edit_steering(9, "nope"))
        self.assertEqual(1, agent.clear_steering())
        self.assertEqual([], agent.pending_steering())

    def test_empty_text_is_not_queued_at_all(self):
        provider = ScriptedProvider([text_turn("done")])
        agent = self._agent(provider)
        self.assertFalse(agent.steer("   "))
        self.assertEqual([], agent.pending_steering())


class CredentialsAreReadOncePerTurn(unittest.TestCase):
    """Why haikode hit a filesystem anomaly no other program noticed.

    _headers() re-read oauth.json on every model request. One observed
    session made 346 of them, so a read that comes back empty once in
    thousands — invisible to a program that reads the file at startup — was
    near-certain here. Three were captured in 14 hours.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="haikode-turn-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.path = Path(self.root, "oauth.json")
        self.path.write_text(json.dumps({"chatgpt": {
            "access": "a", "refresh": "r", "account_id": "acct",
            "expires": int((time.time() + 3600) * 1000)}}))

    def _provider(self):
        from haikode.oauth import OAuthStore
        return ChatGPTSubscriptionProvider(OAuthStore(str(self.path)))

    def _count_reads(self, provider, times):
        reads = {"n": 0}
        real = Path.open

        def counting(self_path, *args, **kwargs):
            if str(self_path) == str(self.path):
                reads["n"] += 1
            return real(self_path, *args, **kwargs)

        with patch.object(Path, "open", counting):
            for _ in range(times):
                provider._headers()
        return reads["n"]

    def test_many_requests_in_one_turn_read_the_file_once(self):
        provider = self._provider()
        self.assertEqual(1, self._count_reads(provider, 25))

    def test_a_new_turn_re_reads_so_another_process_is_noticed(self):
        provider = self._provider()
        self._count_reads(provider, 5)
        provider.invalidate_auth()
        self.assertEqual(1, self._count_reads(provider, 5))

    def test_the_agent_invalidates_at_the_start_of_every_turn(self):
        provider = ScriptedProvider([text_turn("done"), text_turn("done")])
        provider.invalidated = 0
        provider.invalidate_auth = lambda: setattr(
            provider, "invalidated", provider.invalidated + 1)
        agent = Agent(provider, "m", cwd=self.root,
                      permissions=Permissions(auto_approve=True))
        agent.run("first")
        agent.run("second")
        self.assertEqual(2, provider.invalidated)

    def test_logout_drops_the_cached_credentials(self):
        provider = self._provider()
        self._count_reads(provider, 3)
        self.assertIsNotNone(provider._auth)
        provider.invalidate_auth()
        self.assertIsNone(provider._auth)


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


class ALongRunningSessionSeesNewModels(TemporaryProject):
    """Field report: gpt-5.6-luna and -terra "didn't work".

    They worked fine — the backend listed them and answered them. What
    failed was the model dialog in a session left running for days: the
    in-memory line-up had no age limit (the 24h TTL applied only to the
    disk cache), so a model the backend had started offering was
    unfindable until the process was restarted.
    """

    def catalogue(self):
        config = self.config(default_provider="zen", providers={
            "zen": {"dialect": "openai", "model": "old-model",
                    "base_url": "https://opencode.ai/zen/v1",
                    "api_key": "public"}})
        return models_mod.ModelCatalog(
            config, cache_path=Path(self.root, "model-cache.json"))

    def test_an_aged_listing_is_asked_again(self):
        catalog = self.catalogue()
        lineups = [([{"id": "old-model", "context": 0}], ""),
                   ([{"id": "old-model", "context": 0},
                     {"id": "brand-new-model", "context": 0}], "")]
        calls = []

        def listing(config, name):
            calls.append(name)
            return lineups[min(len(calls) - 1, 1)]

        with patch.object(models_mod.configtool, "list_model_entries", listing):
            first = [ref.model for ref in catalog.models("zen")]
            same_day = [ref.model for ref in catalog.models("zen")]
            self.assertEqual(["old-model"], first)
            self.assertEqual(first, same_day)
            self.assertEqual(1, len(calls))      # same day: served from memory

            # Two days pass inside one process.
            catalog._fetched["zen"] -= 2 * 24 * 3600
            later = [ref.model for ref in catalog.models("zen")]
            self.assertIn("brand-new-model", later)  # aged: asked again
            self.assertEqual(2, len(calls))

    def test_a_dead_endpoint_still_costs_one_timeout_per_day_not_per_open(self):
        catalog = self.catalogue()
        calls = []

        def dead(config, name):
            calls.append(name)
            return [], "unreachable: timed out"

        with patch.object(models_mod.configtool, "list_model_entries", dead):
            catalog.models("zen")
            catalog.models("zen")
            catalog.models("zen")
        self.assertEqual(1, len(calls))
        self.assertIn("zen", catalog.errors)


class PlanModeKeepsItsPromises(TemporaryProject):
    """Issues #1 and #2: the plan prompt promised tools that did not exist.

    It told the model to end every planning turn with `plan_exit` and to
    delegate exploration to an `explore` subagent — the tool call failed
    and the subagent type did not resolve, so every planning session ended
    on a broken promise.
    """

    def plan_agent(self, answers=None):
        def asker(request):
            metadata = request.metadata or {}
            if metadata.get("kind") == "question" and answers is not None:
                metadata["answers"] = answers
            return "once"

        config = self.config()
        agent = build_agent(config, "", cwd=self.root,
                            agent_name="plan", asker=asker)
        return agent

    def test_the_plan_agent_carries_the_tools_its_prompt_names(self):
        agent = self.plan_agent()
        self.assertIn("plan_exit", agent.tools)
        self.assertIn("question", agent.tools)
        self.assertIn("plan_exit", agent._system_message().content)

    def test_build_does_not_offer_a_plan_to_approve(self):
        config = self.config()
        agent = build_agent(config, "", cwd=self.root)
        self.assertNotIn("plan_exit", agent.tools)

    def test_approval_switches_to_build(self):
        agent = self.plan_agent(answers=[["Approve — start building"]])
        result = agent.tools["plan_exit"].execute(
            {"plan": "1. do the thing"}, agent.ctx)
        self.assertTrue(result.metadata.get("approved"))
        self.assertEqual("build", agent.agent_name)
        self.assertIn("edit", agent.tools)

    def test_rejection_stays_in_plan_mode(self):
        agent = self.plan_agent(answers=[["Keep planning"]])
        result = agent.tools["plan_exit"].execute(
            {"plan": "1. do the thing"}, agent.ctx)
        self.assertFalse(result.metadata.get("approved"))
        self.assertEqual("plan", agent.agent_name)
        self.assertNotIn("edit", agent.tools)

    def test_no_answer_is_rejection_not_a_crash(self):
        agent = self.plan_agent(answers=[])
        result = agent.tools["plan_exit"].execute({"plan": "x"}, agent.ctx)
        self.assertFalse(result.metadata.get("approved"))
        self.assertEqual("plan", agent.agent_name)

    def test_headless_approval_is_terminal_not_a_retry_invitation(self):
        # Field failure: an agent spawned `haikode -p` probes in plan mode;
        # nobody could approve plan_exit, and the old "Stay in plan mode"
        # answer invited the model to burn its remaining steps retrying.
        config = self.config()
        agent = build_agent(config, "", cwd=self.root, agent_name="plan")
        result = agent.tools["plan_exit"].execute({"plan": "x"}, agent.ctx)
        self.assertTrue(result.metadata.get("terminal"))
        self.assertFalse(result.metadata.get("approved"))
        self.assertIn("final answer", result.output)
        self.assertNotIn("Stay in plan mode", result.output)
        self.assertEqual("plan", agent.agent_name)

    def test_outside_plan_mode_it_declines_politely(self):
        from haikode.tool.plan import PlanExitTool
        config = self.config()
        agent = build_agent(config, "", cwd=self.root)
        result = PlanExitTool().execute({"plan": "x"}, agent.ctx)
        self.assertIn("Not in plan mode", result.output)

    def test_the_explore_subagent_resolves_and_is_read_only(self):
        from haikode.agents import BUILTIN, AgentRegistry, is_readonly
        from haikode.tool import REGISTRY
        explore = BUILTIN["explore"]
        self.assertEqual("subagent", explore.mode)
        self.assertTrue(is_readonly(explore))
        names = AgentRegistry.resolve_tools(explore, list(REGISTRY))
        self.assertEqual(["glob", "grep", "list", "read"], sorted(names))

    def test_the_task_tool_accepts_the_subagent_type(self):
        spec = None
        config = self.config()
        agent = build_agent(config, "", cwd=self.root)
        for candidate in agent.specs:
            if candidate.name == "task":
                spec = candidate
        self.assertIsNotNone(spec)
        self.assertIn("subagent_type", spec.parameters["properties"])
        self.assertIn("explore",
                      spec.parameters["properties"]["subagent_type"]["description"])


class TheModelCanAskAndBeAnswered(TemporaryProject):
    """The question tool's front-end half: nothing ever filled `answers`.

    Every question burned a turn and came back "Unanswered" — the tool
    documented the contract and no asker implemented it.
    """

    def test_the_repl_asker_fills_answers(self):
        from haikode.repl import terminal_asker

        class Request:
            metadata = {
                "kind": "question",
                "questions": [{
                    "question": "Which approach?",
                    "header": "Approach",
                    "options": [{"label": "MVP first", "description": ""},
                                {"label": "Risk first", "description": ""}],
                    "multiple": False,
                }],
                "answers": [],
            }
            title = "question"

        with patch("builtins.input", return_value="2"):
            answer = terminal_asker(Request())
        self.assertEqual("once", answer)
        self.assertEqual([["Risk first"]], Request.metadata["answers"])

    def test_free_text_and_multi_select_are_accepted(self):
        from haikode.repl import terminal_asker

        class Request:
            metadata = {
                "kind": "question",
                "questions": [{
                    "question": "Which features?",
                    "header": "Features",
                    "options": [{"label": "A", "description": ""},
                                {"label": "B", "description": ""}],
                    "multiple": True,
                }],
                "answers": [],
            }
            title = "question"

        with patch("builtins.input", return_value="1, something else"):
            terminal_asker(Request())
        self.assertEqual([["A", "something else"]],
                         Request.metadata["answers"])

    def test_a_question_through_the_tool_reaches_the_asker_and_back(self):
        from haikode.tool.question import QuestionTool

        def asker(request):
            metadata = request.metadata or {}
            if metadata.get("kind") == "question":
                metadata["answers"] = [["MVP first"]]
            return "once"

        config = self.config()
        agent = build_agent(config, "", cwd=self.root, asker=asker)
        result = QuestionTool().execute(
            {"questions": [{"question": "Which approach?",
                            "header": "Approach",
                            "options": [{"label": "MVP first",
                                         "description": "d"}]}]},
            agent.ctx)
        self.assertIn("MVP first", result.output)
        self.assertEqual(1, result.metadata["answered"])


class TheFooterSaysWhatIsActuallyHappening(TemporaryProject):
    """User request from the 32-bit dogfooding: effort level and parallel
    activity (subagents, shells) visible in the status line."""

    def test_effort_is_a_footer_segment(self):
        line = tui_mod.build_status("chatgpt/gpt-5.6-terra", "proj", 10, 20,
                                    100, tui_mod.Glyphs(True), effort="xhigh")
        self.assertIn("xhigh", line)

    def test_effort_is_dropped_before_the_provider_when_narrow(self):
        line = tui_mod.build_status("chatgpt/gpt-5.6-terra", "some-project",
                                    1000, 2000, 46, tui_mod.Glyphs(False),
                                    effort="xhigh")
        self.assertEqual(46, len(line))
        self.assertNotIn("xhigh", line)
        self.assertIn("chatgpt/gpt-5.6-terra", line)

    def test_activity_rides_with_the_spinner(self):
        line = tui_mod.build_status("p", "d", 0, 0, 90, tui_mod.Glyphs(True),
                                    busy=True, elapsed=3,
                                    activity="2 agents · 1 shell")
        self.assertIn("working", line)
        self.assertIn("2 agents · 1 shell", line)

    def test_the_label_counts_from_the_shared_context(self):
        ui = tui_mod.TUI(lambda: None, config=None, cwd=".")
        self.addCleanup(ui.turn.close)

        class Ctx:
            activity = {"agents": 2, "shells": 1}

        class Agent:
            ctx = Ctx()

        ui.agent = Agent()
        self.assertIn("2 agents", ui._activity_label())
        self.assertIn("1 shell", ui._activity_label())
        Ctx.activity = {"agents": 0, "shells": 0}
        self.assertEqual("", ui._activity_label())

    def test_a_subagent_shares_the_parents_counters(self):
        from haikode.tool.base import ToolContext
        parent = ToolContext(cwd=self.root)
        parent.bump_activity("agents", +1)
        self.assertEqual(1, parent.activity["agents"])
        parent.bump_activity("agents", -1)
        parent.bump_activity("agents", -1)      # aldri under null
        self.assertEqual(0, parent.activity["agents"])

    def test_a_running_shell_is_counted_while_it_runs(self):
        from haikode.tool.shell import BashTool
        config = self.config()
        agent = build_agent(config, "", cwd=self.root)
        agent.permissions.auto_approve = True
        seen = []
        original = agent.ctx.bump_activity

        def spying_bump(key, delta):
            seen.append((key, delta, dict(agent.ctx.activity)))
            original(key, delta)

        agent.ctx.bump_activity = spying_bump
        BashTool().execute({"command": "true"}, agent.ctx)
        self.assertIn(("shells", 1, {"agents": 0, "shells": 0}), seen)
        self.assertEqual(0, agent.ctx.activity["shells"])

    def test_the_bash_tool_honours_the_projects_shell(self):
        # haikode.json `shell` was validated and never read — one of the
        # audit's executable bug reports, now closed for real: the wrapper
        # below proves the configured binary is the one that runs.
        from haikode.tool.shell import BashTool
        wrapper = Path(self.root) / "wrapper.sh"
        wrapper.write_text("#!/bin/sh\necho WRAPPED\nexec /bin/sh \"$@\"\n")
        wrapper.chmod(0o755)
        config = self.config()
        agent = build_agent(config, "", cwd=self.root)
        agent.permissions.auto_approve = True
        agent.ctx.shell = str(wrapper)
        result = BashTool().execute({"command": "echo inner"}, agent.ctx)
        self.assertIn("WRAPPED", result.output)
        self.assertIn("inner", result.output)

    def test_bump_activity_stamps_and_clears_the_start_time(self):
        from haikode.tool.base import ToolContext
        ctx = ToolContext(cwd=self.root)
        ctx.bump_activity("shells", +1)
        self.assertIn("shells", ctx.activity_since)
        ctx.bump_activity("shells", +1)
        first = ctx.activity_since["shells"]
        ctx.bump_activity("shells", -1)
        self.assertEqual(first, ctx.activity_since["shells"])  # still busy
        ctx.bump_activity("shells", -1)
        self.assertNotIn("shells", ctx.activity_since)

    def test_tool_work_shows_its_age_instead_of_quiet(self):
        # Field failure, the two-day "frozen at 10.6k" hunt: a 15-minute
        # toolchain build holds no provider stream open, so the footer's
        # "quiet Ns" climbed while the machine worked flat out and the
        # session read as dead. Tools running -> show their age, not quiet.
        ui = tui_mod.TUI(lambda: None, config=None, cwd=".")
        self.addCleanup(ui.turn.close)
        now = time.monotonic()

        class Ctx:
            activity = {"agents": 0, "shells": 1}
            activity_since = {"shells": now - 392}

        class Agent:
            ctx = Ctx()
            last_event_at = now - 392

        ui.agent = Agent()
        ui.running = True
        label = ui._activity_label()
        self.assertIn("1 shell", label)
        self.assertIn("6m32s", label)
        self.assertNotIn("quiet", label)

    def test_model_silence_without_tools_still_reads_quiet(self):
        ui = tui_mod.TUI(lambda: None, config=None, cwd=".")
        self.addCleanup(ui.turn.close)

        class Ctx:
            activity = {"agents": 0, "shells": 0}
            activity_since = {}

        class Agent:
            ctx = Ctx()
            last_event_at = time.monotonic() - 45

        ui.agent = Agent()
        ui.running = True
        self.assertIn("quiet 4", ui._activity_label())


class TheFarewellsAreKeptForPosterity(TemporaryProject):
    """User request: model-written farewells vanished with the scrollback.

    They now join a plain markdown collection the user owns, and startup
    draws from it — a session's best goodbye becomes another morning's
    greeting. Hand-editing the file must never break anything.
    """

    POEM = ["quiet fans at night", "the cursor waits for no one",
            "sessions drift to sleep"]

    def collection(self):
        from haikode import status
        return patch.object(status, "global_config_dir",
                            lambda: str(self.root))

    def test_a_recorded_farewell_comes_back_from_the_collection(self):
        from haikode.status import record_farewell, saved_farewells
        with self.collection():
            record_farewell(self.POEM, "gpt-5.6-sol", "night shift")
            kept = saved_farewells()
        self.assertEqual(1, len(kept))
        self.assertEqual(tuple(self.POEM), kept[0][:3])
        self.assertEqual("gpt-5.6-sol", kept[0][3])

    def test_recording_the_same_poem_twice_archives_it_once(self):
        from haikode.status import record_farewell, saved_farewells
        with self.collection():
            record_farewell(self.POEM, "gpt-5.6-sol")
            record_farewell(self.POEM, "gpt-5.6-sol")
            self.assertEqual(1, len(saved_farewells()))

    def test_an_invalid_poem_is_not_archived(self):
        from haikode.status import record_farewell, saved_farewells
        with self.collection():
            record_farewell(["just one line"], "model")
            self.assertEqual([], saved_farewells())

    def test_hand_edits_cannot_break_the_parse(self):
        from haikode.status import FAREWELL_LOG, record_farewell, saved_farewells
        with self.collection():
            record_farewell(self.POEM, "gpt-5.6-sol")
            path = Path(self.root) / FAREWELL_LOG
            path.write_text(path.read_text("utf-8")
                            + "\n## torn entry\nonly\ntwo lines\n"
                            + "then a fourth\nand a fifth stray line\n",
                            encoding="utf-8")
            kept = saved_farewells()
        self.assertEqual(1, len(kept))

    def test_startup_draws_from_the_kept_farewells_too(self):
        from haikode import status
        with self.collection():
            status.record_farewell(self.POEM, "gpt-5.6-sol")
            with patch("random.choice", side_effect=lambda pool: pool[-1]):
                first, second, third, author = status.startup_haiku()
        self.assertEqual(tuple(self.POEM), (first, second, third))
        self.assertEqual("— gpt-5.6-sol", author)


class TheExitIsAHaiku(TemporaryProject):
    """User request: a tech/AI haiku on exit, plus how to resume.

    The resume line is the substance — the session id is otherwise a thing
    you had to have noted mid-session. The poem is the signature.
    """

    def test_the_farewell_is_an_attributed_haiku_with_a_resume_line(self):
        from haikode.status import FAREWELL_HAIKU, farewell
        text = farewell("ses_0019fc0123456789abc")
        self.assertIn("haikode -s ses_0019fc0123456789abc", text)
        lines = [line.strip() for line in text.splitlines() if line.strip()
                 and "resume" not in line]
        self.assertEqual(4, len(lines))
        self.assertTrue(lines[3].startswith("— "))
        self.assertIn(tuple(lines[:3]) + (lines[3][2:],), FAREWELL_HAIKU)

    def test_without_a_session_there_is_no_resume_line(self):
        from haikode.status import farewell
        self.assertNotIn("resume", farewell(""))

    def test_exit_command_prints_it(self):
        config = self.config(default_provider="zen", providers={
            "zen": {"model": "m", "api_key": "x",
                    "base_url": "https://opencode.ai/zen/v1"}})
        repl = REPL(config, provider="zen", cwd=self.root)
        self.addCleanup(repl.turn.close)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(SystemExit):
                repl.handle_command("/exit")
        printed = buffer.getvalue()
        stripped = [line.strip() for line in printed.splitlines()
                    if line.strip() and "resume" not in line]
        from haikode.status import FAREWELL_HAIKU
        self.assertIn(tuple(stripped[:3]) + (stripped[3][2:],),
                      FAREWELL_HAIKU)


class TheComposerNamesTheSessionAndWritesItsFarewell(TemporaryProject):
    """After the first turn the model writes a display title AND the exit
    haiku — the settled design: the curated collection greets at startup,
    the model's own poem (signed with its name) says goodbye, default on,
    /farewell turns it off. Interactive fronts only — a piped run or a
    test stub must never lose its next scripted answer.
    """

    def test_a_successful_turn_names_the_session(self):
        provider = ScriptedProvider([
            text_turn("the real answer"),
            text_turn("Fixing The Parser"),
        ])
        agent = Agent(provider, "gpt-5.6-terra", cwd=self.root,
                      permissions=Permissions(auto_approve=True))
        controller = TurnController(cwd=self.root, store_factory=lambda: None)
        controller.compose_farewell = True
        controller.run_turn(agent, "please fix the parser bug in main.py")
        deadline = time.time() + 5
        while not controller.display_title and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual("Fixing The Parser", controller.display_title)
        # Bakgrunnen dikter ikke lenger — det gjør /farewell, på forespørsel.
        self.assertIsNone(controller.farewell_poem)

    def test_slash_farewell_composes_with_context_and_exits(self):
        """The settled design: /farewell is the ceremonial exit.

        Typing it IS the consent — no toggle, no config key, no hidden
        background call. It composes at the very end, from the prompt,
        with the whole session as material and the pipe to itself.
        """
        provider = ScriptedProvider([
            text_turn("done"),
            text_turn("keys rest in silence\nthe parser sleeps without fear\nmorning brings green tests"),
        ])
        agent = Agent(provider, "gpt-5.6-terra", cwd=self.root,
                      permissions=Permissions(auto_approve=True))
        controller = TurnController(cwd=self.root, store_factory=lambda: None)
        controller.run_turn(agent, "fix the parser")
        self.assertTrue(controller.compose_farewell_now(agent))
        self.assertEqual(("keys rest in silence",
                          "the parser sleeps without fear",
                          "morning brings green tests"),
                         controller.farewell_poem)
        self.assertEqual("gpt-5.6-terra", controller.farewell_poet)

    def test_a_failed_composition_reports_false_and_leaves_the_collection(self):
        provider = ScriptedProvider([text_turn("done")])   # ingen dikt-respons
        agent = Agent(provider, "m", cwd=self.root,
                      permissions=Permissions(auto_approve=True))
        controller = TurnController(cwd=self.root, store_factory=lambda: None)
        controller.run_turn(agent, "hello")
        self.assertFalse(controller.compose_farewell_now(agent))
        self.assertIsNone(controller.farewell_poem)

    def test_opting_in_makes_a_plain_exit_ceremonial(self):
        """/farewell on: every exit composes; off reverts; persisted."""
        config = self.config(default_provider="zen", providers={
            "zen": {"model": "m", "api_key": "x",
                    "base_url": "https://opencode.ai/zen/v1"}})
        repl = REPL(config, provider="zen", cwd=self.root)
        self.addCleanup(repl.turn.close)
        self.assertIn("every exit", repl.handle_command("/farewell on"))
        self.assertTrue(config.data["farewell_on_exit"])
        from haikode.config import Config
        self.assertTrue(Config(str(self.config_path))
                        .data.get("farewell_on_exit"))

        composed = []
        repl.turn.compose_farewell_now = lambda agent: composed.append(1)
        with patch.object(sys.stdin, "isatty", return_value=True):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                with self.assertRaises(SystemExit):
                    repl.handle_command("/exit")
        self.assertEqual([1], composed)

        self.assertIn("instant", repl.handle_command("/farewell off"))
        self.assertFalse(config.data["farewell_on_exit"])

    def test_plain_exit_never_dials_the_provider(self):
        config = self.config(default_provider="zen", providers={
            "zen": {"model": "m", "api_key": "x",
                    "base_url": "https://opencode.ai/zen/v1"}})
        repl = REPL(config, provider="zen", cwd=self.root)
        self.addCleanup(repl.turn.close)
        calls = []
        repl.agent.provider.stream = lambda *a, **k: calls.append(1) or iter(())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with self.assertRaises(SystemExit):
                repl.handle_command("/exit")
        self.assertEqual([], calls)

    def test_the_composer_rides_its_own_pipe(self):
        """Field incident: two TUI sessions wedged mid-turn on chatgpt.

        The composer streamed on the live turn's provider object — same
        Codex session-id header, same abort handle. Two streams multiplexed
        onto one backend session is exactly the kind of thing that wedges,
        and a poem must never be able to abort (or be aborted by) the
        user's next turn. The composer now clones the provider: shared
        credentials, own session id, no abort handle.
        """
        seen = []

        class SessionedProvider(ScriptedProvider):
            session_id = "live-turn-session"
            abort = "live-abort-handle"

            def stream(self, messages, tools, model, max_tokens):
                seen.append((self.session_id, self.abort))
                return super().stream(messages, tools, model, max_tokens)

        provider = SessionedProvider([
            text_turn("the real answer"),
            text_turn("Fixing The Parser"),
        ])
        agent = Agent(provider, "m", cwd=self.root,
                      permissions=Permissions(auto_approve=True))
        controller = TurnController(cwd=self.root, store_factory=lambda: None)
        controller.compose_farewell = True
        controller.run_turn(agent, "fix it")
        deadline = time.time() + 5
        while not controller.display_title and time.time() < deadline:
            time.sleep(0.01)
        turn_call = seen[0]
        composer_calls = seen[1:]
        self.assertEqual(1, len(composer_calls))
        for session_id, abort in composer_calls:
            self.assertNotEqual("live-turn-session", session_id)
            self.assertIsNone(abort)
        # Den levende provideren er urørt.
        self.assertEqual("live-turn-session", provider.session_id)

    def test_every_provider_gets_a_stall_timeout(self):
        """The other half of the wedge: no stall budget meant a silently
        dead connection hung the turn forever."""
        from haikode.runtime import DEFAULT_STALL_TIMEOUT, build_provider
        config = self.config(default_provider="zen", providers={
            "zen": {"model": "m", "api_key": "x", "dialect": "openai",
                    "base_url": "https://opencode.ai/zen/v1"},
            "chatgpt": {"dialect": "chatgpt", "model": "gpt-5.6-sol",
                        "oauth_provider": "chatgpt", "requires_key": False,
                        "base_url": "https://chatgpt.com/backend-api/codex"},
            "slowpoke": {"model": "m", "api_key": "x", "dialect": "openai",
                         "base_url": "https://api.example.com/v1",
                         "stall_timeout": 30}})
        self.assertEqual(DEFAULT_STALL_TIMEOUT,
                         build_provider(config, "zen").stall_timeout)
        self.assertEqual(DEFAULT_STALL_TIMEOUT,
                         build_provider(config, "chatgpt").stall_timeout)
        self.assertEqual(30.0,
                         build_provider(config, "slowpoke").stall_timeout)

    def test_disabled_or_piped_composers_spend_nothing(self):
        provider = ScriptedProvider([text_turn("the answer")])
        agent = Agent(provider, "m", cwd=self.root,
                      permissions=Permissions(auto_approve=True))
        controller = TurnController(cwd=self.root, store_factory=lambda: None)
        controller.compose_farewell = False
        controller.run_turn(agent, "hello")
        time.sleep(0.2)
        self.assertEqual("", controller.display_title)
        self.assertEqual(1, len(provider.messages))

    def test_titles_and_tab_names_are_validated(self):
        from haikode.status import terminal_title, validated_title
        self.assertEqual("Fixing The Parser Bug",
                         validated_title(' "Fixing The Parser Bug." '))
        self.assertEqual("", validated_title("word"))
        self.assertEqual("", validated_title("x" * 60))
        sequence = terminal_title("haikode — parser work")
        self.assertTrue(sequence.startswith("\x1b]0;"))
        self.assertTrue(sequence.endswith("\x07"))
        self.assertNotIn("\x1b", sequence[2:-1])

    def test_the_home_screen_greets_with_an_attributed_poem(self):
        from haikode.status import FAREWELL_HAIKU, startup_haiku
        poem = startup_haiku()
        self.assertEqual(4, len(poem))
        self.assertTrue(poem[3].startswith("— "))
        self.assertIn(poem[:3] + (poem[3][2:],), FAREWELL_HAIKU)


class AnAcknowledgedWriteSurvivesThePlugBeingPulled(TemporaryProject):
    """Field event: a report written at 21:04, machine lost power, and the
    file came back the right size but full of another package's bytes —
    BFS journaled the rename, the data blocks never flushed. The write
    tool must fsync data before the swap and the directory after it.
    """

    def test_the_write_tool_syncs_data_and_directory(self):
        from haikode.tool.files import WriteTool
        config = self.config()
        agent = build_agent(config, "", cwd=self.root)
        agent.permissions.auto_approve = True
        synced = []
        real_fsync = os.fsync

        def counting_fsync(fd):
            synced.append(fd)
            return real_fsync(fd)

        target = str(Path(self.root, "report.md"))
        with patch.object(os, "fsync", counting_fsync):
            WriteTool().execute({"filePath": target, "content": "funn\n"},
                                agent.ctx)
        # Once for the data, once for the directory entry.
        self.assertGreaterEqual(len(synced), 2)
        self.assertEqual("funn\n", Path(target).read_text())

    def test_the_session_store_commits_synchronously(self):
        import sqlite3
        from haikode.session import SessionStore
        store = SessionStore(str(Path(self.root, "s.db")))
        self.addCleanup(store.close)
        conn = store._connect() if hasattr(store, "_connect") else None
        if conn is None:
            store.list_sessions() if hasattr(store, "list_sessions") else None
            conn = store._conn
        value = conn.execute("PRAGMA synchronous").fetchone()[0]
        self.assertEqual(2, int(value))     # 2 = FULL


class ToolOutputCarriesNoTerminalEscapes(TemporaryProject):
    """Observed live on the 32-bit machine: `df` output stored with SGR codes.

    The shell tool sets TERM=dumb, but Haiku's own userland (df, listdev)
    colourises unconditionally, so tool results carried raw escape bytes
    into the model's context — token waste that teaches the model nothing.
    """

    def test_sgr_and_osc_are_stripped_from_captured_output(self):
        from haikode.tool.shell import BashTool
        config = self.config()
        agent = build_agent(config, "", cwd=self.root)
        agent.permissions.auto_approve = True
        result = BashTool().execute(
            {"command": "printf '\\033[1mVolume\\033[0m plain \\033]0;title\\007tail'"},
            agent.ctx)
        self.assertEqual("Volume plain tail", result.output)
        self.assertNotIn("\x1b", result.output)

    def test_the_fast_path_leaves_clean_text_untouched(self):
        from haikode.tool.shell import _strip_ansi
        self.assertEqual("no escapes here", _strip_ansi("no escapes here"))
        self.assertEqual("Volume  Type",
                         _strip_ansi("\x1b[1mVolume\x1b[0m  Type"))


class AGeminiProfileSpeaksGemini(TemporaryProject):
    """`dialect: "gemini"` silently fell through to the OpenAI wire format.

    The provider existed (275 tested lines) but the dispatch had no branch
    for it, so a profile declaring gemini sent OpenAI-shaped requests at
    Gemini's endpoint — a misconfiguration trap that failed in whatever way
    the endpoint chose, never mentioning the actual problem.
    """

    def test_the_dialect_reaches_the_gemini_provider(self):
        from haikode.providers.gemini import GeminiProvider
        from haikode.runtime import build_provider
        config = self.config(default_provider="g", providers={
            "g": {"dialect": "gemini", "model": "gemini-2.5-pro",
                  "api_key": "k"}})
        self.assertIsInstance(build_provider(config, "g"), GeminiProvider)

    def test_provider_add_accepts_the_dialect(self):
        config = self.config(default_provider="zen", providers={
            "zen": {"model": "m", "api_key": "x",
                    "base_url": "https://opencode.ai/zen/v1"}})
        ok, message = models_mod.add_provider(
            config, "g", "https://generativelanguage.googleapis.com",
            "gemini-2.5-pro", "gemini")
        self.assertTrue(ok, message)
        self.assertEqual("gemini", config.data["providers"]["g"]["dialect"])


class CompactionBudgetsAgainstWhatARequestMayBe(TemporaryProject):
    """Issue #5, both halves — every number below was measured.

    The compaction budget was `context` × reserve, but `context` is input
    plus output and requests are refused on the input share: the ChatGPT
    backend enforces 372k input of gpt-5.6's 500k, 272k of gpt-5.5's 400k
    (opencode codex.ts records the same split). And the estimator saw only
    79–88% of the API's own count on a real session, so the trigger fired
    150k–210k tokens after the request had stopped being legal — surfacing
    as a generic server_error, nothing that looked like size.
    """

    def test_the_chatgpt_backend_input_share_is_published(self):
        provider = ChatGPTSubscriptionProvider.__new__(ChatGPTSubscriptionProvider)
        self.assertEqual((372000, "ChatGPT backend profile"),
                         provider.input_limit("gpt-5.6-sol", 500000))
        self.assertEqual((372000, "ChatGPT backend profile"),
                         provider.input_limit("gpt-5.6-luna", 500000))
        self.assertEqual((272000, "ChatGPT backend profile"),
                         provider.input_limit("gpt-5.5", 400000))
        self.assertEqual((128000, "context window"),
                         provider.input_limit("gpt-4o", 128000))

    def test_the_agent_budgets_against_the_input_share(self):
        config = self.config(
            default_provider="chatgpt",
            providers={"chatgpt": {"model": "gpt-5.6-sol"}})
        agent = build_agent(config, "chatgpt", cwd=self.root)
        self.assertEqual(500000, agent.context_window)
        self.assertEqual(372000, agent.input_window)
        self.assertEqual("ChatGPT backend profile", agent.input_source)

    def test_a_profile_can_pin_the_input_limit(self):
        config = self.config(
            default_provider="kimi",
            providers={"kimi": {"model": "k3", "api_key": "x",
                                "base_url": "https://api.example.com/v1",
                                "context": 1048576, "input": 900000}})
        agent = build_agent(config, "kimi", cwd=self.root)
        self.assertEqual(900000, agent.input_window)
        self.assertEqual("configured input limit", agent.input_source)

    def test_a_provider_with_no_split_keeps_the_window(self):
        config = self.config(
            default_provider="zen",
            providers={"zen": {"model": "m", "api_key": "x",
                               "base_url": "https://opencode.ai/zen/v1",
                               "context": 190000}})
        agent = build_agent(config, "zen", cwd=self.root)
        self.assertEqual(agent.context_window, agent.input_window)

    def test_switching_model_moves_the_input_share_too(self):
        config = self.config(
            default_provider="chatgpt",
            providers={"chatgpt": {"model": "gpt-5.6-sol"}})
        agent = build_agent(config, "chatgpt", cwd=self.root)
        agent.set_model("gpt-5.5")
        self.assertEqual(400000, agent.context_window)
        self.assertEqual(272000, agent.input_window)

    def test_the_estimator_is_calibrated_against_the_measurements(self):
        """The session measured 79–88% at 4 chars/token; 3.3 lands 96–107%."""
        from haikode.context import estimate_tokens
        for chars, reported in ((162956, 46033), (327980, 103200),
                                (476716, 146773)):
            estimated = estimate_tokens("x" * chars)
            self.assertGreater(estimated / reported, 0.90)
            self.assertLess(estimated / reported, 1.15)

    def test_the_reported_count_recalibrates_the_trigger(self):
        """One real exchange teaches the trigger the model's arithmetic."""
        provider = ScriptedProvider([
            [CompletionChunk(text="ok", stop_reason="stop",
                             usage={"input": 12000, "output": 5})],
        ])
        agent = Agent(provider, "m", cwd=self.root,
                      permissions=Permissions(auto_approve=True))
        agent.run("hello")
        # The provider counted 12000 for a prompt we estimated far smaller,
        # so the scale rises — clamped to the plausible range.
        self.assertGreater(agent.token_scale, 1.0)
        self.assertLessEqual(agent.token_scale, 2.0)

    def test_the_scale_moves_the_compaction_point(self):
        from haikode.context import needs_compaction
        history = [Msg(role="user" if i % 2 == 0 else "assistant",
                       content="x" * 660) for i in range(30)]
        # ~6000 estimated tokens against a 20000-token window: fits at face
        # value, does not fit once the model is known to count double.
        self.assertFalse(needs_compaction(history, 20000, reserve=0.4))
        self.assertTrue(needs_compaction(history, 20000, reserve=0.4,
                                         scale=2.0))


class TheContextWindowFollowsTheModel(TemporaryProject):
    """Field report: the meter was wrong on Kimi, Ollama and SuperGrok.

    A provider profile holds one `context` for all its models. The numbers
    below were read off the live endpoints on 1 August 2026, not guessed:

        api.kimi.com /models    k3-256k    context_length 262144
                                k3        context_length 1048576
        api.x.ai     /models    grok-4.5   context_length 500000
        a LAN Ollama /api/show  qwen3.6:27b-94k   num_ctx 94208
                                            (weights say 262144)

    Against the configured 128000 and 131072 the agent compacted early and
    the meter reported a share of the wrong denominator.
    """

    def catalogue(self, contexts):
        cache = Path(self.root, "model-cache.json")
        cache.write_text(json.dumps({
            "version": models_mod.CACHE_VERSION,
            "providers": {"kimi": {"time": time.time(),
                                   "base_url": "https://api.kimi.com/coding/v1",
                                   "models": list(contexts),
                                   "context": contexts}}}))
        return cache

    def config_with_kimi(self, **extra):
        profile = {"dialect": "openai", "base_url": "https://api.kimi.com/coding/v1",
                   "model": "k3-256k", "context": 128000, "api_key": "x"}
        profile.update(extra)
        return self.config(default_provider="kimi", providers={"kimi": profile})

    def build(self, config, cache, model=""):
        with patch.object(models_mod, "global_config_dir",
                          lambda: cache.parent):
            agent = build_agent(config, "kimi", cwd=self.root, model=model)
        self.addCleanup(agent.provider.close if hasattr(agent.provider, "close")
                        else lambda: None)
        return agent

    def test_the_endpoints_own_number_beats_the_provider_default(self):
        cache = self.catalogue({"k3-256k": 262144, "k3": 1048576})
        agent = self.build(self.config_with_kimi(), cache)
        self.assertEqual(262144, agent.context_window)
        self.assertEqual("endpoint metadata", agent.context_source)

    def test_switching_model_switches_the_window(self):
        cache = self.catalogue({"k3-256k": 262144, "k3": 1048576})
        agent = self.build(self.config_with_kimi(), cache, model="k3")
        self.assertEqual(1048576, agent.context_window)

    def test_a_model_the_endpoint_says_nothing_about_keeps_the_profile(self):
        cache = self.catalogue({"k3": 1048576})
        agent = self.build(self.config_with_kimi(), cache, model="k3-256k")
        self.assertEqual(128000, agent.context_window)
        self.assertEqual("configuration", agent.context_source)

    def test_an_explicit_per_model_setting_wins(self):
        cache = self.catalogue({"k3-256k": 262144})
        config = self.config_with_kimi(model_context={"k3-256k": 200000})
        agent = self.build(config, cache)
        self.assertEqual(200000, agent.context_window)
        self.assertEqual("configured for this model", agent.context_source)

    def test_a_provider_that_knows_its_own_backend_is_not_overruled(self):
        """The ChatGPT profile is measured against that backend, not /models."""
        provider = ChatGPTSubscriptionProvider.__new__(ChatGPTSubscriptionProvider)
        self.assertEqual((500000, "ChatGPT backend profile"),
                         provider.context_limit("gpt-5.6-sol", 128000))

    def test_ollama_reports_what_it_will_serve_not_what_the_weights_allow(self):
        body = {"parameters": "num_ctx                        94208\ntop_p 0.95",
                "model_info": {"qwen35.context_length": 262144}}
        with patch.object(configtool_mod.urllib.request, "urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = \
                json.dumps(body).encode()
            self.assertEqual(94208, configtool_mod.ollama_context(
                "http://192.168.1.20:11434/v1", "qwen3.6:27b-94k"))

    def test_ollama_falls_back_to_the_weights_when_no_limit_is_set(self):
        body = {"parameters": "top_p 0.95",
                "model_info": {"qwen3.context_length": 40960}}
        with patch.object(configtool_mod.urllib.request, "urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = \
                json.dumps(body).encode()
            self.assertEqual(40960, configtool_mod.ollama_context(
                "http://192.168.1.20:11434/v1", "qwen3:8b"))


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

    def test_a_queued_message_stays_above_the_prompt_until_it_is_sent(self):
        """Field report: "køede meldinger forsvinner oppover i chaten".

        Typing during a run wrote the text into the transcript, where the
        next tool call pushed it out of sight. The user was then waiting on
        something they could no longer see, with no way to tell whether it
        had been picked up.
        """
        ui = self.ui()
        ui._size = lambda: (30, 100)
        ui.transcript.add(tui_mod.Entry("user", text="first"))

        class Steerable:
            def __init__(self):
                self.pending = []

            def steer(self, text):
                self.pending.append(text)
                return True

            def pending_steering(self):
                return list(self.pending)

        ui.agent = Steerable()
        before = len(ui.transcript.entries)
        ui._enqueue("check the tests too")

        # Not in the transcript — in the band, which the layout reserves rows
        # for above the prompt.
        self.assertEqual(before, len(ui.transcript.entries))
        frame = ui._frame()
        self.assertTrue(frame.queue_rows)
        self.assertLess(frame.queue_top, frame.box_top)
        self.assertLessEqual(frame.todo_top, frame.queue_top)
        text = " ".join(line.text for line
                        in ui._pinned_queue_lines(frame.content_width))
        self.assertIn("check the tests too", text)
        self.assertIn("Queued", text)

        # Delivery empties the band and records the message where it belongs.
        ui.agent.pending = []
        ui._on_event("steered", {"text": "check the tests too"})
        self.assertEqual([], ui._pinned_queue_lines(frame.content_width))
        self.assertEqual("check the tests too",
                         ui.transcript.entries[-1].text)
        self.assertEqual("user", ui.transcript.entries[-1].kind)

    def test_leader_q_reaches_the_queue_it_names(self):
        """It quit the application instead.

        opencode binds <leader>q to app_exit *and* session_queued_prompts,
        and the binding table is also the priority order, so exit won: the
        one chord documented as "manage queued prompts" ended the session.
        """
        ui = self.ui()
        ui.queued = ["one", "two"]
        from haikode.keybind import KeyEvent
        ui._keymap_key(KeyEvent(key="x", ctrl=True))
        ui._keymap_key(KeyEvent(key="q"))
        self.assertFalse(ui._quit)
        self.assertIsNotNone(ui.dialog)
        self.assertEqual(["one", "two"],
                         [item.title for item in ui.dialog.select.items])

    def test_a_queued_message_can_be_pulled_back_for_editing(self):
        ui = self.ui()
        ui.queued = ["fix the parser", "and the tests"]
        ui._open_queued_prompts()
        ui._edit_queued(ui.dialog.select.items[0])
        self.assertEqual("fix the parser", ui.buffer)
        self.assertEqual(["and the tests"], ui.queued)

    def test_a_queued_message_can_be_dropped(self):
        ui = self.ui()
        ui.queued = ["fix the parser", "and the tests"]
        ui._open_queued_prompts()
        ui._drop_one_queued(ui.dialog.select.items[1])
        self.assertEqual(["fix the parser"], ui.queued)
        self.assertEqual("", ui.buffer)

    def test_the_queue_dialog_acts_on_the_message_not_on_the_row_number(self):
        """Found by adversarial review of the dialog above.

        The dialog listed steering first and plain queued prompts after it,
        and acted on the row's index. But the running turn drains steering at
        its next step — often while the dialog is open — so pressing enter on
        the first row pulled out and deleted a completely different message.
        """
        ui = self.ui()

        class Steerable:
            def __init__(self):
                self.pending = ["A: rename it", "B: and the tests"]

            def pending_steering(self):
                return list(self.pending)

            def drop_steering(self, text):
                if text in self.pending:
                    self.pending.remove(text)
                    return True
                return False

        ui.agent = Steerable()
        ui.queued = ["C: deploy afterwards"]
        ui._open_queued_prompts()
        row = ui.dialog.select.items[0]              # "A: rename it"

        ui.agent.pending = []                        # the turn takes them
        ui._edit_queued(row)

        self.assertEqual(["C: deploy afterwards"], ui.queued)
        self.assertEqual("", ui.buffer)
        self.assertIn("already sent", ui.status_hint)

    def test_interrupt_discards_steering_not_just_the_queue(self):
        """Found by a session running haikode on itself, from the inside.

        Esc during a run dropped `self.queued` but left the agent's steering
        list alone, so a message typed mid-run — and abandoned with the run —
        was folded into the next turn anyway: an instruction delivered after
        the user said stop.
        """
        ui = self.ui()
        ui.running = True

        class Steerable:
            def __init__(self):
                self.pending = ["do the abandoned thing"]

            def abort(self):
                pass

            def pending_steering(self):
                return list(self.pending)

            def clear_steering(self):
                count = len(self.pending)
                self.pending = []
                return count

        ui.agent = Steerable()
        ui.queued = ["a queued one too"]
        ui._interrupt()

        self.assertEqual([], ui.agent.pending)
        self.assertEqual([], ui.queued)
        self.assertEqual([], ui._pending_messages())
        notices = [e.text for e in ui.transcript.entries if e.kind == "info"]
        self.assertTrue(any("2 pending prompts discarded" in t
                            for t in notices), notices)

    def test_editing_a_queued_message_does_not_eat_the_composer(self):
        ui = self.ui()
        ui.queued = ["queued message"]
        ui.buffer = "half-typed composer text"
        ui.cursor = len(ui.buffer)
        ui._open_queued_prompts()
        ui._edit_queued(ui.dialog.select.items[0])

        self.assertEqual("queued message", ui.buffer)
        # Not lost: one press of "up" brings it back.
        self.assertIn("half-typed composer text", ui.history)

    def test_the_band_holds_prompts_queued_for_after_the_turn_too(self):
        ui = self.ui()
        ui._size = lambda: (30, 100)
        ui.queued = ["second thing"]
        text = " ".join(line.text for line in ui._pinned_queue_lines(60))
        self.assertIn("second thing", text)

    def test_a_pinned_queue_never_squeezes_the_body_below_the_minimum(self):
        for rows in range(tui_mod.MIN_ROWS, 40):
            frame = tui_mod.layout_frame(rows, 100, 1, session=True,
                                         wanted_todo_rows=6,
                                         wanted_queue_rows=4)
            self.assertGreaterEqual(frame.body_height, 1)
            self.assertLessEqual(frame.todo_top + frame.todo_rows,
                                 frame.queue_top)
            self.assertLessEqual(frame.queue_top + frame.queue_rows,
                                 frame.box_top)

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
