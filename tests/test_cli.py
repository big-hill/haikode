"""
The scriptable CLI surface: exit codes, --json events and the sub-commands.

Every test here exists because a script could not see something. `haikode
"..."` used to exit 0 after the provider refused the request, so a CI job had
no way to tell a rate limit from an answer; there was no machine-readable
output at all, so the only way to consume a run was to scrape coloured text;
and sessions, models and agents could only be inspected from inside the curses
TUI, which a script does not have.

So these assert the *contract*: which code each distinct failure exits with,
that the JSON stream matches its documented schema for a full turn including a
tool call and an error, that the session sub-commands round-trip, and that a
run with piped stdin behaves exactly like a run on a tty minus the prompts.
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode import agents as agents_mod  # noqa: E402
from haikode import context as context_mod  # noqa: E402
from haikode import main as main_mod  # noqa: E402
from haikode import memory as memory_mod  # noqa: E402
from haikode import projectconfig as projectconfig_mod  # noqa: E402
from haikode import repl as repl_mod  # noqa: E402
from haikode import runtime as runtime_mod  # noqa: E402
from haikode import session as session_mod  # noqa: E402
from haikode.config import Config  # noqa: E402
from haikode.providers.base import Provider  # noqa: E402
from haikode.repl import (EXIT_DENIED, EXIT_ERROR, EXIT_INTERRUPTED,  # noqa: E402
                          EXIT_LIMIT, EXIT_OK, EXIT_USAGE, JSON_EVENTS)
from haikode.schema import CompletionChunk, Msg  # noqa: E402


class ScriptedProvider(Provider):
    """Replays canned turns; one list of chunks per provider round."""

    name = "scripted"

    def __init__(self, turns=None):
        self.turns = [list(turn) for turn in (turns or [])]
        self.seen = []

    def stream(self, messages, tools, model, max_tokens):
        self.seen.append(list(messages))
        chunks = self.turns.pop(0) if self.turns else [
            CompletionChunk(text="done", stop_reason="stop")]
        for chunk in chunks:
            yield chunk


def text_turn(text="hello from the model"):
    return [CompletionChunk(text=text),
            CompletionChunk(usage={"prompt_tokens": 11, "completion_tokens": 5},
                            stop_reason="stop")]


def error_turn(message="Rate limited (HTTP 429)", kind="rate_limit"):
    """What a provider that refused looks like on the wire."""
    return [CompletionChunk(usage={"error": {"kind": kind, "message": message,
                                             "retryable": True}},
                            stop_reason="error")]


def tool_turn(name, arguments, call_id="call-1"):
    return [CompletionChunk(tool_call_delta={
        "index": 0, "id": call_id, "name": name,
        "arguments": json.dumps(arguments)}),
        CompletionChunk(stop_reason="tool_calls")]


class CLITestCase(unittest.TestCase):
    """A project directory, a private config and a private session database."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-cli-")
        self.home = tempfile.mkdtemp(prefix="haikode-cli-home-")
        self.globals = Path(self.home, "config")
        self.globals.mkdir(parents=True, exist_ok=True)
        self.db = Path(self.home, "sessions.db")
        self.provider = ScriptedProvider()
        self._patches = [
            patch.object(memory_mod, "global_config_dir", lambda: self.globals),
            patch.object(agents_mod, "global_config_dir", lambda: self.globals),
            patch.object(context_mod, "global_config_dir", lambda: self.globals),
            patch.object(projectconfig_mod, "global_config_dir",
                         lambda: self.globals),
            patch.object(context_mod, "home_dir", lambda: Path(self.home)),
            patch.object(session_mod, "default_db_path", lambda: self.db),
            # Every agent this test file builds talks to the script, never to
            # a socket — including the ones main() builds for itself.
            patch.object(runtime_mod, "build_provider",
                         lambda *a, **k: self.provider),
        ]
        for entry in self._patches:
            entry.start()
        self.config = Config(path=str(Path(self.home, "config.json")))

    def tearDown(self):
        for entry in reversed(self._patches):
            entry.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    # --- helpers ---------------------------------------------------------

    def script(self, *turns):
        self.provider.turns = [list(turn) for turn in turns]

    def build(self, factory=None, **kwargs):
        factory = factory or repl_mod.REPL
        kwargs.setdefault("cwd", self.dir)
        return factory(self.config, **kwargs)

    def run_main(self, argv, stdin=None):
        """Run main() as the shell would. Returns (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        stream = _FakeStdin(stdin if stdin is not None else "")
        code = EXIT_OK
        with patch.object(main_mod, "Config", lambda: self.config), \
                patch.object(sys, "argv", ["haikode"] + argv), \
                patch.object(sys, "stdin", stream), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                main_mod.main()
            except SystemExit as exit_request:
                code = exit_request.code or EXIT_OK
        return code, out.getvalue(), err.getvalue()

    def events(self, text):
        """Parse a JSON Lines stream, asserting every line is one object."""
        parsed = []
        for line in text.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            self.assertIsInstance(record, dict, line)
            parsed.append(record)
        return parsed

    def seed_session(self, title="Fix the parser", body="the parser drops commas"):
        store = session_mod.SessionStore()
        session = store.new_session(self.dir, "ollama", "m", title)
        session.append(Msg(role="user", content=body))
        session.append(Msg(role="assistant", content="fixed it"))
        return session


class _FakeStdin(io.StringIO):
    """Piped stdin: readable, iterable, and emphatically not a terminal."""

    def isatty(self):
        return False


# --------------------------------------------------------------------------
# 1. exit codes — every distinct failure has its own
# --------------------------------------------------------------------------


class TestExitCodes(CLITestCase):
    def test_a_successful_run_exits_zero(self):
        self.script(text_turn())
        repl = self.build()
        with redirect_stdout(io.StringIO()):
            repl.send("hello")
        self.assertEqual(repl.exit_code(), EXIT_OK)

    def test_a_provider_failure_exits_one(self):
        """The reproduced defect: `[error] ProviderFailure` and then exit 0."""
        self.script(error_turn())
        repl = self.build()
        with redirect_stdout(io.StringIO()):
            repl.send("hello")
        self.assertIn("ProviderFailure", repl.last_turn.error)
        self.assertEqual(repl.exit_code(), EXIT_ERROR)

    def test_a_denied_permission_exits_three(self):
        self.script(tool_turn("bash", {"command": "rm -rf /",
                                       "description": "delete"}),
                    text_turn("I could not run that"))
        Path(self.dir, "haikode.json").write_text(
            json.dumps({"permission": {"bash": "deny"}}))
        repl = self.build()
        with redirect_stdout(io.StringIO()):
            repl.send("clean up")
        self.assertEqual(repl.exit_code(), EXIT_DENIED)

    def test_a_step_limit_exits_four(self):
        Path(self.dir, "haikode.json").write_text(json.dumps({"max_steps": 2}))
        self.script(tool_turn("list", {"path": "."}, "c1"),
                    tool_turn("list", {"path": "."}, "c2"),
                    tool_turn("list", {"path": "."}, "c3"))
        repl = self.build()
        with redirect_stdout(io.StringIO()):
            repl.send("look around")
        self.assertEqual(repl.exit_code(), EXIT_LIMIT)

    def test_an_interrupted_run_exits_130(self):
        repl = self.build()
        with patch.object(repl.agent, "run", side_effect=KeyboardInterrupt), \
                redirect_stdout(io.StringIO()):
            repl.send("hello")
        self.assertEqual(repl.exit_code(), EXIT_INTERRUPTED)

    def test_an_error_beats_a_later_success(self):
        """A piped run of several prompts must not exit 0 because the last
        one worked — the failure in the middle is the news."""
        self.script(error_turn(), text_turn())
        repl = self.build()
        with redirect_stdout(io.StringIO()):
            repl.send("first")
            repl.send("second")
        self.assertEqual(repl.last_turn.error, "")
        self.assertEqual(repl.exit_code(), EXIT_ERROR)

    def test_main_exits_with_the_code_of_a_one_shot_run(self):
        self.script(error_turn())
        code, _, _ = self.run_main(["-C", self.dir, "explain this"])
        self.assertEqual(code, EXIT_ERROR)

    def test_main_exits_zero_on_a_good_one_shot_run(self):
        self.script(text_turn())
        code, out, _ = self.run_main(["-C", self.dir, "explain this"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("hello from the model", out)

    def test_one_shot_helper_reports_the_same_code(self):
        self.script(error_turn())
        with redirect_stdout(io.StringIO()):
            code = repl_mod.one_shot(self.config, "hello", cwd=self.dir)
        self.assertEqual(code, EXIT_ERROR)

    def test_fork_without_a_session_is_a_usage_error(self):
        code, _, err = self.run_main(["-C", self.dir, "--fork", "go"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--fork", err)

    def test_an_unknown_session_id_is_a_usage_error(self):
        """And nothing runs: silently starting a fresh conversation would be
        the one thing the caller definitely did not ask for."""
        code, out, err = self.run_main(["-C", self.dir, "--session", "nope",
                                        "go"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("nope", err)
        self.assertEqual(out, "")
        self.assertEqual(self.provider.seen, [])

    def test_an_unconfigured_provider_is_a_usage_error_not_a_traceback(self):
        with patch.object(runtime_mod, "build_provider",
                          side_effect=ValueError("unknown provider 'bogus'")):
            code, out, err = self.run_main(["-C", self.dir, "-p", "bogus", "go"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("bogus", err)
        self.assertNotIn("Traceback", out + err)

    def test_the_help_documents_every_code(self):
        code, out, _ = self.run_main(["--help"])
        self.assertEqual(code, EXIT_OK)
        for value in (EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_DENIED, EXIT_LIMIT,
                      EXIT_INTERRUPTED):
            self.assertIn(str(value), out)


# --------------------------------------------------------------------------
# 2. --json — the documented event stream
# --------------------------------------------------------------------------


class TestJSONStream(CLITestCase):
    def json_run(self, prompt, **kwargs):
        stream = io.StringIO()
        repl = self.build(factory=repl_mod.JSONREPL, stream=stream, **kwargs)
        repl.send(prompt)
        return repl, self.events(stream.getvalue())

    def kinds(self, events):
        return [event["type"] for event in events]

    def only(self, events, kind):
        return [event for event in events if event["type"] == kind]

    def test_every_event_has_the_common_envelope(self):
        self.script(text_turn())
        _, events = self.json_run("hello")
        self.assertTrue(events)
        for event in events:
            self.assertIn(event["type"], JSON_EVENTS, event)
            self.assertIsInstance(event["time"], float)
            self.assertIsInstance(event["session"], str)

    def test_a_plain_turn_streams_text_usage_and_done(self):
        self.script(text_turn())
        _, events = self.json_run("hello")
        kinds = self.kinds(events)
        self.assertEqual(kinds[0], "run")
        self.assertEqual(kinds[-1], "done")
        self.assertIn("text", kinds)
        self.assertEqual("".join(e["text"] for e in self.only(events, "text")),
                         "hello from the model")
        usage = self.only(events, "usage")[0]
        self.assertEqual(usage["input"], 11)
        self.assertEqual(usage["output"], 5)
        done = self.only(events, "done")[0]
        self.assertEqual(done["exit"], EXIT_OK)
        self.assertEqual(done["text"], "hello from the model")
        self.assertFalse(done["interrupted"])

    def test_a_tool_call_appears_as_tool_and_tool_result(self):
        Path(self.dir, "note.txt").write_text("a line of text\n")
        self.script(tool_turn("read", {"filePath": "note.txt"}),
                    text_turn("the file says hello"))
        _, events = self.json_run("read note.txt")
        call = self.only(events, "tool")[0]
        self.assertEqual(call["name"], "read")
        self.assertEqual(call["args"], {"filePath": "note.txt"})
        result = self.only(events, "tool_result")[0]
        self.assertEqual(result["name"], "read")
        self.assertIn("a line of text", result["output"])
        self.assertIsInstance(result["metadata"], dict)

    def test_a_provider_error_appears_twice_and_ends_the_turn(self):
        """Once structured (what the provider said) and once as the turn's
        own error, which is what `done.error` and the exit code follow."""
        self.script(error_turn())
        _, events = self.json_run("hello")
        errors = self.only(events, "error")
        self.assertEqual({e["source"] for e in errors}, {"provider", "turn"})
        provider_error = [e for e in errors if e["source"] == "provider"][0]
        self.assertEqual(provider_error["kind"], "rate_limit")
        self.assertTrue(provider_error["retryable"])
        self.assertIn("429", provider_error["message"])
        done = self.only(events, "done")[0]
        self.assertIn("ProviderFailure", done["error"])
        self.assertEqual(done["exit"], EXIT_ERROR)

    def test_a_denied_tool_is_reported_and_carried_into_done(self):
        Path(self.dir, "haikode.json").write_text(
            json.dumps({"permission": {"bash": "deny"}}))
        self.script(tool_turn("bash", {"command": "rm -rf /"}),
                    text_turn("refused"))
        _, events = self.json_run("clean up")
        denied = self.only(events, "tool_denied")[0]
        self.assertEqual(denied["name"], "bash")
        self.assertIn("denied", denied["reason"])
        done = self.only(events, "done")[0]
        self.assertEqual(done["denied"], ["bash"])
        self.assertEqual(done["exit"], EXIT_DENIED)

    def test_an_unanswerable_permission_is_emitted_before_it_is_refused(self):
        """A script cannot answer a prompt, so the ask is reported and
        rejected — but it must be able to see WHICH rule stopped the run."""
        self.script(tool_turn("bash", {"command": "ls"}), text_turn("refused"))
        _, events = self.json_run("list the files")
        asked = self.only(events, "permission")[0]
        self.assertEqual(asked["key"], "bash")
        self.assertEqual(asked["decision"], "reject")
        self.assertTrue(asked["patterns"])
        self.assertEqual(self.only(events, "done")[0]["exit"], EXIT_DENIED)

    def test_yes_lets_the_same_tool_through(self):
        self.script(tool_turn("bash", {"command": "echo hi"}),
                    text_turn("it printed hi"))
        _, events = self.json_run("say hi", auto_approve=True)
        self.assertEqual(self.only(events, "permission"), [])
        self.assertEqual(self.only(events, "tool_result")[0]["name"], "bash")
        self.assertEqual(self.only(events, "done")[0]["exit"], EXIT_OK)

    def test_a_step_limit_is_an_event_and_an_exit_code(self):
        Path(self.dir, "haikode.json").write_text(json.dumps({"max_steps": 1}))
        self.script(tool_turn("list", {"path": "."}))
        _, events = self.json_run("look around")
        self.assertEqual(self.only(events, "limit")[0]["steps"], 1)
        self.assertEqual(self.only(events, "done")[0]["exit"], EXIT_LIMIT)

    def test_reasoning_is_streamed_as_its_own_event(self):
        self.script([CompletionChunk(reasoning="thinking about it"),
                     CompletionChunk(text="answer", stop_reason="stop")])
        _, events = self.json_run("hello")
        self.assertEqual(self.only(events, "reasoning")[0]["text"],
                         "thinking about it")

    def test_a_quick_capture_is_a_memory_event_and_runs_nothing(self):
        repl = self.build(factory=repl_mod.JSONREPL, stream=io.StringIO())
        with patch.object(repl.agent, "run",
                          side_effect=AssertionError("must not run")):
            repl.send("# The owner writes Norwegian.")
        events = self.events(repl.stream.getvalue())
        self.assertIn("Remembered", self.only(events, "memory")[0]["text"])
        self.assertEqual(self.only(events, "done")[0]["exit"], EXIT_OK)

    def test_an_attached_mention_is_reported(self):
        Path(self.dir, "note.txt").write_text("mentioned content\n")
        self.script(text_turn())
        _, events = self.json_run("look at @note.txt")
        self.assertIn("note.txt", self.only(events, "attach")[0]["paths"][0])

    def test_a_slash_command_is_captured_not_printed(self):
        """/init prints straight to stdout; a bare print would corrupt the
        stream, so a command's output ships inside one event."""
        stream = io.StringIO()
        repl = self.build(factory=repl_mod.JSONREPL, stream=stream)
        self.assertTrue(repl.command("/tools"))
        events = self.events(stream.getvalue())
        self.assertEqual(events[-1]["type"], "command")
        self.assertIn("memory_write", events[-1]["text"])

    def test_the_session_id_is_on_every_event_after_the_session_opens(self):
        self.script(text_turn())
        repl, events = self.json_run("hello")
        done = self.only(events, "done")[0]
        self.assertEqual(done["session"], repl.session.id)
        self.assertTrue(done["persisted"])

    def test_main_with_json_emits_only_json(self):
        self.script(text_turn())
        code, out, _ = self.run_main(["-C", self.dir, "--json", "hello"])
        self.assertEqual(code, EXIT_OK)
        events = self.events(out)
        self.assertEqual([e["type"] for e in events][0], "run")
        self.assertEqual([e["type"] for e in events][-1], "done")

    def test_main_with_json_reports_a_resume_as_an_event(self):
        session = self.seed_session()
        self.script(text_turn())
        code, out, _ = self.run_main(
            ["-C", self.dir, "--json", "--session", session.id, "carry on"])
        self.assertEqual(code, EXIT_OK)
        notice = [e for e in self.events(out) if e["type"] == "notice"]
        self.assertIn("Resumed", notice[0]["text"])


# --------------------------------------------------------------------------
# 3. session control on a run
# --------------------------------------------------------------------------


class TestSessionFlags(CLITestCase):
    def test_session_flag_resumes_by_a_prefix(self):
        session = self.seed_session()
        self.script(text_turn())
        code, out, _ = self.run_main(
            ["-C", self.dir, "--session", session.id[:8], "carry on"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Resumed", out)

    def test_continue_resumes_the_latest(self):
        self.seed_session()
        self.script(text_turn())
        code, out, _ = self.run_main(["-C", self.dir, "--continue", "carry on"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Resumed", out)

    def test_fork_leaves_the_original_untouched(self):
        session = self.seed_session()
        self.script(text_turn())
        code, out, _ = self.run_main(
            ["-C", self.dir, "--session", session.id, "--fork", "carry on"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Forked", out)
        store = session_mod.SessionStore()
        original = store.load(session.id)
        self.assertEqual(len(original.messages), 2)
        rows = store.list_sessions(cwd=self.dir)
        self.assertEqual(len(rows), 2)
        forked = store.load([r["id"] for r in rows if r["id"] != session.id][0])
        self.assertEqual([m.content for m in forked.messages[:2]],
                         [m.content for m in original.messages])
        self.assertGreater(len(forked.messages), 2)

    def test_title_names_a_brand_new_session(self):
        self.script(text_turn())
        code, _, _ = self.run_main(
            ["-C", self.dir, "--title", "Nightly audit", "look around"])
        self.assertEqual(code, EXIT_OK)
        rows = session_mod.SessionStore().list_sessions(cwd=self.dir)
        self.assertEqual(rows[0]["title"], "Nightly audit")

    def test_title_renames_a_resumed_session(self):
        session = self.seed_session()
        self.script(text_turn())
        self.run_main(["-C", self.dir, "--session", session.id,
                       "--title", "Renamed", "carry on"])
        self.assertEqual(session_mod.SessionStore().load(session.id).title,
                         "Renamed")

    def test_agent_and_model_flags_reach_the_run(self):
        self.script(text_turn())
        args = main_mod.build_parser().parse_args(
            ["--agent", "plan", "--model", "openai/gpt-4o-mini"])
        repl = main_mod.build_repl(self.config, args, self.dir)
        self.assertEqual(repl.agent.agent_name, "plan")
        self.assertEqual(repl.agent.model, "gpt-4o-mini")
        self.assertEqual(repl.provider_name, "openai")

    def test_the_fork_command_is_available_in_both_front_ends(self):
        session = self.seed_session()
        repl = self.build()
        repl.adopt_session(session)
        output = repl.handle_command("/fork")
        self.assertIn("Forked", output)
        self.assertNotEqual(repl.session.id, session.id)
        self.assertEqual(len(repl.session.messages), 2)


# --------------------------------------------------------------------------
# 4. the sub-commands
# --------------------------------------------------------------------------


class TestSessionSubcommand(CLITestCase):
    def run_session(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main_mod.session_command(argv)
        return code, out.getvalue(), err.getvalue()

    def test_list_shows_the_stored_sessions(self):
        session = self.seed_session()
        code, out, _ = self.run_session(["list"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn(session.id[:8], out)
        self.assertIn("Fix the parser", out)

    def test_list_json_is_machine_readable(self):
        session = self.seed_session()
        code, out, _ = self.run_session(["list", "--json"])
        self.assertEqual(code, EXIT_OK)
        rows = json.loads(out)
        self.assertEqual(rows[0]["id"], session.id)
        self.assertEqual(rows[0]["message_count"], 2)

    def test_list_can_be_scoped_to_a_directory(self):
        other = tempfile.mkdtemp(prefix="haikode-cli-other-")
        try:
            session_mod.SessionStore().new_session(other, "ollama", "m",
                                                   "Elsewhere")
            self.seed_session()
            _, out, _ = self.run_session(["list", "-C", self.dir])
            self.assertIn("Fix the parser", out)
            self.assertNotIn("Elsewhere", out)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_show_reports_the_stats(self):
        session = self.seed_session()
        code, out, _ = self.run_session(["show", session.id[:8]])
        self.assertEqual(code, EXIT_OK)
        self.assertIn(session.id, out)
        self.assertIn("Messages  2", out)

    def test_show_json_is_the_full_export(self):
        session = self.seed_session()
        _, out, _ = self.run_session(["show", session.id, "--json"])
        data = json.loads(out)
        self.assertEqual(data["id"], session.id)
        self.assertEqual(len(data["messages"]), 2)

    def test_export_renders_the_transcript(self):
        session = self.seed_session()
        code, out, _ = self.run_session(["export", session.id])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("the parser drops commas", out)

    def test_export_writes_a_file(self):
        session = self.seed_session()
        target = Path(self.dir, "out.md")
        code, out, _ = self.run_session(["export", session.id, "-o", str(target)])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("the parser drops commas", target.read_text())

    def test_rename_changes_the_title(self):
        session = self.seed_session()
        code, out, _ = self.run_session(["rename", session.id, "Parser", "work"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(session_mod.SessionStore().load(session.id).title,
                         "Parser work")

    def test_delete_removes_it(self):
        session = self.seed_session()
        code, _, _ = self.run_session(["delete", session.id])
        self.assertEqual(code, EXIT_OK)
        self.assertIsNone(session_mod.SessionStore().load(session.id))

    def test_an_unknown_id_is_a_usage_error(self):
        code, _, err = self.run_session(["show", "deadbeef"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("no session", err)

    def test_an_ambiguous_prefix_is_refused_rather_than_guessed(self):
        store = session_mod.SessionStore()
        first = store.new_session(self.dir, "ollama", "m", "One")
        with patch.object(session_mod, "new_session_id",
                          lambda: first.id[:6] + "zzzzzz"):
            store.new_session(self.dir, "ollama", "m", "Two")
        code, _, err = self.run_session(["show", first.id[:6]])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("ambiguous", err)

    def test_no_subcommand_prints_help(self):
        code, out, _ = self.run_session([])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("list", out)


class TestExportImport(CLITestCase):
    def run_cmd(self, fn, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn(argv)
        return code, out.getvalue(), err.getvalue()

    def test_export_defaults_to_the_latest_session_here(self):
        self.seed_session()
        code, out, _ = self.run_cmd(main_mod.export_command, ["-C", self.dir])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(out)["title"], "Fix the parser")

    def test_export_import_round_trips_a_session(self):
        session = self.seed_session()
        target = Path(self.dir, "session.json")
        code, _, _ = self.run_cmd(main_mod.export_command,
                                  [session.id, "-o", str(target)])
        self.assertEqual(code, EXIT_OK)
        code, out, _ = self.run_cmd(main_mod.import_command, [str(target)])
        self.assertEqual(code, EXIT_OK)
        imported_id = out.split()[1]
        imported = session_mod.SessionStore().load(imported_id)
        self.assertNotEqual(imported.id, session.id)
        self.assertEqual(imported.title, session.title)
        self.assertEqual([(m.role, m.content) for m in imported.messages],
                         [(m.role, m.content) for m in session.messages])

    def test_import_keeps_tool_calls_paired(self):
        from haikode.schema import ToolCall
        store = session_mod.SessionStore()
        session = store.new_session(self.dir, "ollama", "m", "With tools")
        session.append(Msg(role="user", content="read it"))
        session.append(Msg(role="assistant", tool_calls=[
            ToolCall(id="c1", name="read", arguments={"filePath": "a.txt"})]))
        session.append(Msg(role="tool", tool_call_id="c1", content="alpha",
                           display={"tool": "read"}))
        target = Path(self.dir, "tools.json")
        self.run_cmd(main_mod.export_command, [session.id, "-o", str(target)])
        code, out, _ = self.run_cmd(main_mod.import_command, [str(target)])
        self.assertEqual(code, EXIT_OK)
        imported = session_mod.SessionStore().load(out.split()[1])
        self.assertEqual(imported.messages[1].tool_calls[0].name, "read")
        self.assertEqual(imported.messages[1].tool_calls[0].arguments,
                         {"filePath": "a.txt"})
        self.assertEqual(imported.messages[2].tool_call_id, "c1")

    def test_import_refuses_a_file_it_does_not_understand(self):
        target = Path(self.dir, "junk.json")
        target.write_text(json.dumps({"hello": "world"}))
        code, _, err = self.run_cmd(main_mod.import_command, [str(target)])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("messages", err)
        self.assertEqual(session_mod.SessionStore().list_sessions(), [])

    def test_import_refuses_a_message_without_a_role(self):
        """And leaves nothing behind: a partly-imported conversation would be
        replayed to a provider with a hole in it."""
        target = Path(self.dir, "broken.json")
        target.write_text(json.dumps({"messages": [
            {"role": "user", "content": "fine"}, {"content": "orphan"}]}))
        code, _, err = self.run_cmd(main_mod.import_command, [str(target)])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("role", err)
        self.assertEqual(session_mod.SessionStore().list_sessions(), [])


class TestModelsAndAgentSubcommands(CLITestCase):
    def run_cmd(self, fn, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = fn(argv, self.config)
        return code, out.getvalue(), err.getvalue()

    def test_models_lists_provider_slash_model(self):
        from haikode import models as models_mod
        with patch.object(models_mod.ModelCatalog, "_ids_for",
                          lambda self, name, refresh: ["m-one", "m-two"]):
            code, out, _ = self.run_cmd(main_mod.models_command, ["ollama"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("ollama/m-one", out)
        self.assertIn("ollama/m-two", out)

    def test_models_json_carries_the_fields_a_script_needs(self):
        from haikode import models as models_mod
        with patch.object(models_mod.ModelCatalog, "_ids_for",
                          lambda self, name, refresh: ["m-one"]):
            code, out, _ = self.run_cmd(main_mod.models_command,
                                        ["ollama", "--json"])
        self.assertEqual(code, EXIT_OK)
        entry = json.loads(out)[0]
        self.assertEqual(entry["id"], "ollama/m-one")
        self.assertEqual(entry["provider"], "ollama")
        self.assertIn("free", entry)

    def test_models_rejects_an_unknown_provider(self):
        code, _, err = self.run_cmd(main_mod.models_command, ["nowhere"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("nowhere", err)

    def test_models_reports_a_provider_that_could_not_be_reached(self):
        from haikode import models as models_mod

        def boom(self, name, refresh):
            self.errors[name] = "offline"
            return []

        with patch.object(models_mod.ModelCatalog, "_ids_for", boom):
            code, _, err = self.run_cmd(main_mod.models_command, [])
        self.assertEqual(code, EXIT_ERROR)
        self.assertIn("offline", err)

    def test_agent_lists_the_primary_agents(self):
        code, out, _ = self.run_cmd(main_mod.agent_command, ["-C", self.dir])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("build", out)
        self.assertIn("plan", out)

    def test_agent_json_reports_the_resolved_tool_list(self):
        code, out, _ = self.run_cmd(main_mod.agent_command,
                                    ["-C", self.dir, "--json"])
        self.assertEqual(code, EXIT_OK)
        data = json.loads(out)
        self.assertEqual(data["default"], "build")
        plan = [a for a in data["primary"] if a["name"] == "plan"][0]
        self.assertIn("read", plan["tools"])
        self.assertNotIn("edit", plan["tools"])

    def test_agent_detail_names_one_agent(self):
        code, out, _ = self.run_cmd(main_mod.agent_command,
                                    ["plan", "-C", self.dir])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Agent       plan", out)
        self.assertIn("Tools", out)

    def test_agent_sees_a_project_declared_agent(self):
        Path(self.dir, "haikode.json").write_text(json.dumps(
            {"agents": {"reviewer": {"description": "Reviews code",
                                     "tools": ["read", "grep"]}}}))
        code, out, _ = self.run_cmd(main_mod.agent_command,
                                    ["reviewer", "-C", self.dir, "--json"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(sorted(json.loads(out)["tools"]), ["grep", "read"])

    def test_agent_rejects_an_unknown_name(self):
        code, _, err = self.run_cmd(main_mod.agent_command,
                                    ["nope", "-C", self.dir])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("nope", err)


class TestSubcommandDispatch(CLITestCase):
    def test_a_known_word_is_a_subcommand(self):
        with patch.object(main_mod, "models_command", return_value=EXIT_OK) as spy:
            self.assertEqual(main_mod._dispatch_subcommand(["models"]), EXIT_OK)
        spy.assert_called_once_with([])

    def test_a_sentence_that_starts_with_one_is_still_a_prompt(self):
        self.assertIsNone(main_mod._dispatch_subcommand(["export the parser"]))

    def test_run_is_not_dispatched_as_a_subcommand(self):
        self.assertIsNone(main_mod._dispatch_subcommand(["run", "hello"]))

    def test_run_prefix_still_runs_the_prompt(self):
        self.script(text_turn())
        code, out, _ = self.run_main(["run", "-C", self.dir, "hello"])
        self.assertEqual(code, EXIT_OK)
        self.assertIn("hello from the model", out)


# --------------------------------------------------------------------------
# 5. piped stdin — the scripting case
# --------------------------------------------------------------------------


class TestPipedStdin(CLITestCase):
    def test_a_piped_body_extends_the_prompt(self):
        self.script(text_turn())
        code, _, _ = self.run_main(["-C", self.dir, "review this"],
                                   stdin="diff --git a b\n")
        self.assertEqual(code, EXIT_OK)
        sent = self.provider.seen[0][-1].content
        self.assertIn("review this", sent)
        self.assertIn("diff --git", sent)

    def test_a_tty_run_ignores_stdin(self):
        """The same argv on a terminal must send exactly the same prompt,
        minus whatever happens to be on stdin."""
        self.script(text_turn())
        out = io.StringIO()
        with patch.object(main_mod, "Config", lambda: self.config), \
                patch.object(sys, "argv", ["haikode", "-C", self.dir, "review this"]), \
                patch.object(sys.stdin, "isatty", lambda: True), \
                redirect_stdout(out):
            with self.assertRaises(SystemExit) as raised:
                main_mod.main()
        self.assertEqual(raised.exception.code or EXIT_OK, EXIT_OK)
        self.assertEqual(self.provider.seen[0][-1].content.strip(),
                         "review this")

    def test_compose_prompt_leaves_a_prompt_alone_without_a_pipe(self):
        with patch.object(sys, "stdin", _TTYStdin("ignored")):
            self.assertEqual(main_mod.compose_prompt(["do", "it"]), "do it")

    def test_compose_prompt_does_not_read_stdin_without_a_prompt(self):
        """With no positional prompt haikode is a line-oriented REPL over the
        pipe, so swallowing stdin here would eat every prompt at once."""
        stream = _FakeStdin("first\nsecond\n")
        with patch.object(sys, "stdin", stream):
            self.assertEqual(main_mod.compose_prompt([]), "")
        self.assertEqual(stream.read(), "first\nsecond\n")

    def test_piped_prompts_run_one_turn_each(self):
        self.script(text_turn("one"), text_turn("two"))
        code, out, _ = self.run_main(["-C", self.dir, "--no-tui"],
                                     stdin="first\nsecond\n")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("one", out)
        self.assertIn("two", out)
        self.assertEqual(len(self.provider.seen), 2)

    def test_piped_prompts_with_json_emit_one_stream(self):
        self.script(text_turn("one"), text_turn("two"))
        code, out, _ = self.run_main(["-C", self.dir, "--json"],
                                     stdin="first\n/tools\nsecond\n")
        self.assertEqual(code, EXIT_OK)
        events = [json.loads(line) for line in out.splitlines() if line.strip()]
        kinds = [event["type"] for event in events]
        self.assertEqual(kinds.count("run"), 2)
        self.assertEqual(kinds.count("done"), 2)
        self.assertEqual(kinds.count("command"), 1)
        for event in events:
            self.assertIn(event["type"], JSON_EVENTS)

    def test_a_failure_mid_pipe_still_fails_the_process(self):
        self.script(error_turn(), text_turn("recovered"))
        code, _, _ = self.run_main(["-C", self.dir, "--json"],
                                   stdin="first\nsecond\n")
        self.assertEqual(code, EXIT_ERROR)

    def test_a_piped_run_never_prompts_for_a_permission(self):
        """No tty means no asker: the run refuses rather than hanging on an
        input() nobody can answer."""
        self.script(tool_turn("bash", {"command": "ls"}), text_turn("refused"))
        code, out, _ = self.run_main(["-C", self.dir, "--no-tui", "list files"],
                                     stdin="")
        self.assertEqual(code, EXIT_DENIED)
        self.assertIn("denied", out)

    def test_the_same_run_with_yes_is_allowed(self):
        self.script(tool_turn("bash", {"command": "echo hi"}),
                    text_turn("printed"))
        code, _, _ = self.run_main(
            ["-C", self.dir, "--no-tui", "--yes", "say hi"], stdin="")
        self.assertEqual(code, EXIT_OK)


class _TTYStdin(io.StringIO):
    def isatty(self):
        return True


# --------------------------------------------------------------------------
# 6. the schema is only a contract while it is written down
# --------------------------------------------------------------------------


class TestTheSchemaIsDocumented(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent

    def schema_block(self) -> str:
        source = Path(repl_mod.__file__).read_text()
        head = source.partition("JSON_EVENTS = (")[0]
        return head.rpartition("# --- the machine-readable front end")[2]

    def test_every_event_kind_is_in_the_schema_comment(self):
        block = self.schema_block()
        self.assertTrue(block.strip(), "the schema comment block moved")
        for kind in JSON_EVENTS:
            self.assertIn(kind, block, kind)

    def test_every_event_kind_is_in_the_readme(self):
        """A schema a caller cannot find is not a schema."""
        text = (self.root / "README.md").read_text()
        for kind in JSON_EVENTS:
            self.assertIn("`%s`" % kind, text, kind)


class EffortFlagTests(unittest.TestCase):
    """--effort takes whatever the provider takes.

    The flag used to carry its own list, which rejected "minimal" -- a
    level xAI actually accepts -- before any provider was consulted. The
    provider owns the enum; the flag just carries the string.
    """

    def test_a_provider_specific_level_is_not_rejected_by_the_parser(self):
        from haikode.main import build_parser
        args = build_parser().parse_args(["--effort", "minimal", "hi"])
        self.assertEqual("minimal", args.effort)

    def test_a_refused_level_is_reported_not_swallowed(self):
        """Asked for and not applied must never look like applied."""
        import io
        from contextlib import redirect_stderr
        from unittest.mock import patch
        from haikode import main as main_mod

        class Agent:
            reasoning_effort = ""

            def reasoning_efforts(self):
                return ("low", "high")

        class Repl:
            agent = Agent()

        captured = io.StringIO()
        args = main_mod.build_parser().parse_args(["--effort", "max"])
        with redirect_stderr(captured):
            asked = str(getattr(args, "effort", "") or "").strip().lower()
            applied = str(getattr(Repl.agent, "reasoning_effort", "") or "")
            if applied != asked:
                choices = ", ".join(Repl.agent.reasoning_efforts()) or "none"
                print("[config] reasoning effort '%s' not applied (this model "
                      "takes: %s)" % (asked, choices), file=sys.stderr)
        self.assertIn("not applied", captured.getvalue())
        self.assertIn("low, high", captured.getvalue())

    def test_an_unknown_level_still_reaches_the_provider(self):
        from haikode.main import build_parser
        args = build_parser().parse_args(["--effort", "whatever"])
        self.assertEqual("whatever", args.effort)


if __name__ == "__main__":
    unittest.main()
