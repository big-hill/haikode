import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from haikode import config as config_module
from haikode.config import Config
from haikode.providers import base as provider_base
from haikode.schema import CompletionChunk
from haikode import configtool, desktop_worker, runtime, session
from haikode.main import provider_command


class ScriptedProvider:
    """Provider stub that replays canned chunk lists, one list per round."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    def stream(self, messages, tools, model, max_tokens):
        self.calls.append(list(messages))
        chunks = (self.rounds.pop(0) if self.rounds
                  else [CompletionChunk(text="done", stop_reason="stop")])
        for chunk in chunks:
            yield chunk


def tool_call_chunks(call_id, name, arguments):
    """The streamed fragments a provider emits for one native tool call."""
    return [
        CompletionChunk(tool_call_delta={"index": 0, "id": call_id,
                                         "name": name}),
        CompletionChunk(tool_call_delta={"index": 0,
                                         "arguments": json.dumps(arguments)}),
        CompletionChunk(stop_reason="tool_calls"),
    ]


def worker_frames(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def frame_events(text):
    return [frame["event"] for frame in worker_frames(text)]


def frames_named(text, event):
    return [frame for frame in worker_frames(text) if frame["event"] == event]


class ConfigAndWorkerTests(unittest.TestCase):
    def test_keystore_can_be_disabled_for_isolated_checks(self):
        with (patch.dict(os.environ, {"HAI_DISABLE_KEYSTORE": "1"}),
              patch.object(config_module.shutil, "which",
                           return_value="/tmp/hai-keystore")):
            self.assertIsNone(config_module._keystore_bin())

    def test_required_provider_profiles_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
        providers = config.data["providers"]
        for name in ("openai", "anthropic", "ollama", "ollama-local",
                     "chatgpt", "supergrok"):
            self.assertIn(name, providers)
        self.assertFalse(providers["ollama-local"]["requires_key"])
        self.assertEqual(providers["chatgpt"]["oauth_provider"], "chatgpt")
        self.assertEqual(providers["chatgpt"]["dialect"], "chatgpt")
        self.assertEqual(providers["supergrok"]["oauth_provider"], "supergrok")
        self.assertEqual(providers["supergrok"]["dialect"], "supergrok")
        self.assertNotIn("opencode", providers)

    def test_desktop_worker_emits_valid_ndjson(self):
        env = os.environ.copy()
        env["HAI_DESKTOP_TEST_REPLY"] = "hei æøå"
        process = subprocess.run(
            [sys.executable, "-m", "haikode.desktop_worker", "--provider", "zen"],
            input="test prompt",
            text=True,
            capture_output=True,
            env=env,
            timeout=10,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        frames = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([frame["event"] for frame in frames],
                         ["started", "delta", "completed"])
        self.assertEqual(frames[1]["text"], "hei æøå")
        self.assertTrue(all(frame["v"] == 1 for frame in frames))

    def test_desktop_worker_accepts_duplex_prompt_frame(self):
        env = os.environ.copy()
        env["HAI_DESKTOP_TEST_REPLY"] = "framed-ok"
        env["HAI_FRAMED_STDIN"] = "1"
        prompt = "multiline\nprompt"
        framed = f"{len(prompt.encode('utf-8'))}\n{prompt}"
        process = subprocess.run(
            [sys.executable, "-m", "haikode.desktop_worker", "--provider", "zen"],
            input=framed, text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        frames = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(frames[1], {
            "v": 1, "event": "delta", "text": "framed-ok"})

    def test_desktop_worker_permission_roundtrip(self):
        class DuplexInput:
            buffer = BytesIO(b"permission\tper_test\tonce\n")

        output = StringIO()
        with (patch.object(desktop_worker.sys, "stdin", DuplexInput()),
              redirect_stdout(output)):
            response = desktop_worker._await_permission({
                "id": "per_test",
                "permission": "bash",
                "patterns": ["make test"],
            })

        self.assertEqual(response, "once")
        frame = json.loads(output.getvalue())
        self.assertEqual(frame, {
            "v": 1,
            "event": "permission",
            "id": "per_test",
            # `text` is what the pre-agent desktop binary renders; it must keep
            # its exact shape so a mixed-age pairing still shows something.
            "text": "bash: make test",
            "permission": "bash",
            "patterns": ["make test"],
        })

    def test_desktop_worker_controlled_duplex_permission(self):
        env = os.environ.copy()
        env["HAI_DESKTOP_TEST_REPLY"] = "approval-ok"
        env["HAI_DESKTOP_TEST_PERMISSION"] = "once"
        env["HAI_FRAMED_STDIN"] = "1"
        prompt = "permission prompt"
        framed = (f"{len(prompt.encode('utf-8'))}\n{prompt}"
                  "permission\tper_desktop_smoke\tonce\n")
        process = subprocess.run(
            [sys.executable, "-m", "haikode.desktop_worker", "--provider", "zen"],
            input=framed, text=True, capture_output=True, env=env, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        frames = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual([frame["event"] for frame in frames],
                         ["started", "permission", "delta", "completed"])
        self.assertEqual(frames[2]["text"], "approval-ok:once")

    # --- the desktop app runs the real agent -----------------------------

    @staticmethod
    def _started_session_id(text):
        for frame in worker_frames(text):
            if frame.get("event") == "started":
                return frame["session"]
        raise AssertionError("no started frame emitted")

    @staticmethod
    def _worker_env(directory, provider, stdin_bytes=b""):
        """Patches that let desktop_worker.run() drive a real Agent offline."""
        config = Config(os.path.join(directory, "config.json"))
        config.data["default_provider"] = "ollama-local"

        class DuplexInput:
            buffer = BytesIO(stdin_bytes)

        return config, (
            patch.object(desktop_worker, "Config", return_value=config),
            patch.object(runtime, "build_provider", return_value=provider),
            patch.object(session, "default_db_path",
                         return_value=Path(directory) / "sessions.db"),
            patch.object(desktop_worker.sys, "stdin", DuplexInput()),
        )

    def _stay_put(self):
        """run(directory=...) chdirs the process; put the suite back after."""
        self.addCleanup(os.chdir, os.getcwd())

    def test_desktop_worker_streams_tool_frames_from_the_agent(self):
        """The GUI must see the same tool activity the CLI does."""
        self._stay_put()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "hello.txt").write_text("hi")
            provider = ScriptedProvider([
                tool_call_chunks("call_1", "list", {"path": directory}),
                [CompletionChunk(text="one file", stop_reason="stop")],
            ])
            _, patches = self._worker_env(directory, provider)
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    redirect_stdout(output):
                # --directory is what the desktop app always passes, and it
                # has to be passed here too: a path outside the working
                # directory is an external_directory question now, so
                # without it the run stops on a permission frame.
                self.assertEqual(
                    desktop_worker.run("what is here?", directory=directory), 0)

            frames = worker_frames(output.getvalue())
            self.assertEqual([f["event"] for f in frames],
                             ["started", "info", "usage", "tool", "tool_result",
                              "usage", "delta", "usage", "completed"])
            self.assertEqual(frames[3]["name"], "list")
            self.assertEqual(frames[3]["title"], directory)
            self.assertEqual(frames[4]["name"], "list")
            self.assertIn("hello.txt", frames[4]["output"])
            self.assertEqual(frames[6]["text"], "one file")
            self.assertEqual(frames[8]["steps"], 2)

    def test_desktop_worker_reports_the_agent_model_and_context(self):
        """The app's header and meter come from `info` and `usage` frames."""
        self._stay_put()
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                [CompletionChunk(text="hello", stop_reason="stop")]])
            _, patches = self._worker_env(directory, provider)
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    redirect_stdout(output):
                self.assertEqual(
                    desktop_worker.run("hi", directory=directory), 0)

            info = frames_named(output.getvalue(), "info")[0]
            self.assertEqual(info["agent"], "build")
            self.assertEqual(info["provider"], "ollama-local")
            self.assertTrue(info["model"])
            self.assertEqual(info["directory"], os.getcwd())
            self.assertIn("bash", info["tools"])
            self.assertGreater(info["window"], 0)

            usage = frames_named(output.getvalue(), "usage")[-1]
            # The numbers stay numbers so the BStatusBar can be driven from
            # `percent`; `context`/`summary` are the pre-formatted labels.
            self.assertEqual(usage["window"], info["window"])
            self.assertGreater(usage["used"], 0)
            self.assertGreater(usage["percent"], 0.0)
            self.assertIn("%", usage["context"])
            self.assertIn("in /", usage["summary"])
            self.assertEqual(usage["tokens"], {"input": 0, "output": 0})

    def test_desktop_worker_emits_every_frame_kind_for_a_stubbed_agent(self):
        """One run, every frame the desktop protocol defines."""
        self._stay_put()
        marker = "hai-desktop-all-frames"
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                [CompletionChunk(reasoning="planning the work"),
                 *tool_call_chunks("call_1", "todowrite", {"todos": [
                     {"content": "read the file", "status": "completed"},
                     {"content": "run the check", "status": "in_progress"}]})],
                tool_call_chunks("call_2", "bash", {"command": f"echo {marker}"}),
                tool_call_chunks("call_3", "read", {
                    "filePath": os.path.join(directory, "absent.txt")}),
                [CompletionChunk(text="all done", stop_reason="stop")],
            ])
            _, patches = self._worker_env(
                directory, provider, b"permission\tper_1\tonce\n")
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    redirect_stdout(output):
                self.assertEqual(
                    desktop_worker.run("do the plan", directory=directory), 0)

            text = output.getvalue()
            self.assertEqual(frame_events(text), [
                "started", "info", "usage",
                "reasoning", "tool", "tool_result", "todos", "usage",
                "tool", "permission", "tool_result", "usage",
                "tool", "tool_error", "usage",
                "delta", "usage", "completed"])

            self.assertEqual(frames_named(text, "reasoning")[0]["text"],
                             "planning the work")
            todos = frames_named(text, "todos")[0]
            self.assertEqual(todos["text"],
                             "[x] read the file\n[>] run the check")
            self.assertEqual(todos["summary"], "1/2 done")
            self.assertEqual(frames_named(text, "permission")[0]["command"],
                             f"echo {marker}")
            self.assertIn(marker, frames_named(text, "tool_result")[1]["output"])
            failed = frames_named(text, "tool_error")[0]
            self.assertEqual(failed["name"], "read")
            # `kind` is a string so the C++ side can switch on it without
            # needing to read a JSON boolean.
            self.assertEqual(failed["kind"], "failed")
            self.assertFalse(failed["denied"])
            self.assertIn("absent.txt", failed["error"])
            self.assertEqual(frames_named(text, "delta")[0]["text"], "all done")

    def test_desktop_worker_answers_even_when_sessions_are_unavailable(self):
        """A broken session database costs undo, never the answer.

        The worker used to open the store itself and let the failure escape as
        the run's error, so an unwritable home turned every desktop prompt into
        "[Error] disk I/O error".
        """
        self._stay_put()
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                [CompletionChunk(text="still here", stop_reason="stop")]])
            _, patches = self._worker_env(directory, provider)
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    patch.object(session, "SessionStore",
                                 side_effect=OSError("disk I/O error")), \
                    redirect_stdout(output):
                self.assertEqual(
                    desktop_worker.run("hi", directory=directory), 0)

            text = output.getvalue()
            self.assertEqual(frame_events(text)[-1], "completed")
            self.assertEqual(frames_named(text, "delta")[0]["text"], "still here")
            notices = [frame["text"] for frame in frames_named(text, "status")]
            self.assertTrue(any("undo unavailable" in notice
                                for notice in notices), notices)

    def test_desktop_worker_expands_mentions_like_the_other_front_ends(self):
        """@file is context in the desktop app too, and the title stays clean."""
        self._stay_put()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "notes.md").write_text("gravity is a habit")
            provider = ScriptedProvider([
                [CompletionChunk(text="read it", stop_reason="stop")]])
            _, patches = self._worker_env(directory, provider)
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3]:
                with redirect_stdout(output):
                    self.assertEqual(desktop_worker.run(
                        "summarise @notes.md please", directory=directory), 0)
                session_id = self._started_session_id(output.getvalue())
                store = session.SessionStore()
                restored = store.load(session_id)
                store.close()

            sent = provider.calls[0][-1].content
            self.assertIn("gravity is a habit", sent)
            attached = [frame["text"]
                        for frame in frames_named(output.getvalue(), "status")]
            self.assertTrue(any("notes.md" in note for note in attached),
                            attached)
            # The title is the prompt the user typed, not the file it pulled in.
            self.assertEqual(restored.title, "summarise @notes.md please")

    def test_desktop_worker_quick_capture_never_reaches_the_provider(self):
        """A leading # saves a memory here exactly as it does in the REPL."""
        self._stay_put()
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                [CompletionChunk(text="unreachable", stop_reason="stop")]])
            _, patches = self._worker_env(directory, provider)
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    redirect_stdout(output):
                self.assertEqual(desktop_worker.run(
                    "#the deploy script needs sudo", directory=directory), 0)

            self.assertEqual(provider.calls, [])
            text = output.getvalue()
            self.assertEqual(frames_named(text, "completed")[0]["finish"],
                             "capture")
            notices = [frame["text"] for frame in frames_named(text, "status")]
            self.assertTrue(any("Remembered" in notice for notice in notices),
                            notices)

    def test_desktop_worker_permission_resolves_from_stdin(self):
        """A real tool permission blocks the run until the GUI answers."""
        marker = "hai-desktop-permission-ok"
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                tool_call_chunks("call_1", "bash", {"command": f"echo {marker}"}),
                [CompletionChunk(text="ran it", stop_reason="stop")],
            ])
            _, patches = self._worker_env(
                directory, provider, b"permission\tper_1\tonce\n")
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    redirect_stdout(output):
                self.assertEqual(desktop_worker.run("run echo"), 0)

            text = output.getvalue()
            self.assertEqual(frame_events(text),
                             ["started", "info", "usage", "tool", "permission",
                              "tool_result", "usage", "delta", "usage",
                              "completed"])
            permission = frames_named(text, "permission")[0]
            self.assertEqual(permission["id"], "per_1")
            self.assertEqual(permission["permission"], "bash")
            self.assertEqual(permission["title"], f"Run: echo {marker}")
            self.assertEqual(permission["command"], f"echo {marker}")
            # The answer really released the tool rather than denying it.
            self.assertIn(marker, frames_named(text, "tool_result")[0]["output"])

    def test_desktop_worker_permission_rejection_denies_the_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                tool_call_chunks("call_1", "bash", {"command": "echo nope"}),
                [CompletionChunk(text="declined", stop_reason="stop")],
            ])
            _, patches = self._worker_env(
                directory, provider, b"permission\tper_1\treject\n")
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    redirect_stdout(output):
                self.assertEqual(desktop_worker.run("run echo"), 0)

            frames = worker_frames(output.getvalue())
            denied = [f for f in frames if f["event"] == "tool_error"]
            self.assertEqual(len(denied), 1)
            self.assertTrue(denied[0]["denied"])
            self.assertEqual(denied[0]["kind"], "denied")
            self.assertEqual(denied[0]["name"], "bash")

    def test_desktop_worker_persists_agent_turns_across_runs(self):
        """The id from `started` keeps the next turn on the same transcript.

        Two processes, one conversation: the transcript, the revert point and
        the file snapshot all come out of the same TurnController the REPL and
        the TUI use, so /undo describes what the desktop app actually did.
        """
        self._stay_put()
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                tool_call_chunks("call_1", "list", {"path": directory}),
                [CompletionChunk(text="reply-1", stop_reason="stop")],
                tool_call_chunks("call_2", "write", {"filePath": "notes.txt",
                                                     "content": "written"}),
                [CompletionChunk(text="reply-2", stop_reason="stop")],
            ])
            _, patches = self._worker_env(
                directory, provider, b"permission\tper_1\tonce\n")
            first_out, second_out = StringIO(), StringIO()
            with patches[0], patches[1], patches[2], patches[3]:
                with redirect_stdout(first_out):
                    self.assertEqual(
                        desktop_worker.run("first", directory=directory), 0)
                session_id = self._started_session_id(first_out.getvalue())
                with redirect_stdout(second_out):
                    self.assertEqual(desktop_worker.run(
                        "second", directory=directory,
                        session_name=session_id), 0)
                self.assertEqual(self._started_session_id(second_out.getvalue()),
                                 session_id)

                store = session.SessionStore()
                restored = store.load(session_id)
                checkpoint = restored.last_checkpoint()
                snapshots = restored.snapshots()
                store.close()

            written = os.path.realpath(os.path.join(directory, "notes.txt"))
            self.assertEqual(Path(written).read_text(), "written")

        # The tool call and its result survive the process boundary; dropping
        # either half makes providers reject the next request.
        self.assertEqual([m.role for m in restored.messages],
                         ["user", "assistant", "tool", "assistant",
                          "user", "assistant", "tool", "assistant"])
        self.assertEqual(restored.messages[1].tool_calls[0].name, "list")
        self.assertEqual(restored.messages[2].tool_call_id, "call_1")
        self.assertEqual(restored.title, "first")
        # The second turn opened its own revert point, after the first turn's
        # four messages, and the file it created was snapshotted against it.
        self.assertEqual(checkpoint, 4)
        self.assertEqual(snapshots, {written: None})
        # ...and the app is told which session the turn actually landed in.
        self.assertEqual(
            frames_named(second_out.getvalue(), "completed")[0]["session"],
            session_id)

        # ... and the third provider round was primed with that whole history.
        replayed = [(m.role, m.content) for m in provider.calls[2]]
        self.assertEqual(replayed[0][0], "system")
        self.assertEqual(replayed[-2:], [("assistant", "reply-1"),
                                         ("user", "second")])

    def test_desktop_worker_provider_error_becomes_an_error_frame(self):
        """A transport failure must not reach the app as assistant text."""
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                [CompletionChunk(text="\n[stream error] connection refused",
                                 stop_reason="error")],
            ])
            _, patches = self._worker_env(directory, provider)
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    redirect_stdout(output):
                self.assertEqual(desktop_worker.run("hi"), 1)

            # Every stdout line parsed as a frame: no traceback leaked into the
            # protocol stream, and exactly one error frame is reported.
            text = output.getvalue()
            self.assertEqual(frame_events(text),
                             ["started", "info", "usage", "error"])
            failure = frames_named(text, "error")[0]
            self.assertEqual(failure["message"], "connection refused")
            self.assertEqual(failure["kind"], "unknown")
            self.assertFalse(failure["retryable"])

    def test_desktop_worker_forwards_the_structured_provider_error(self):
        """The app switches on `kind`, so it must not sniff for a text marker.

        The frame is providers.base.ProviderError.as_dict(): an auth failure
        can point the user at Settings, a rate limit cannot.
        """
        with tempfile.TemporaryDirectory() as directory:
            failure = provider_base.ProviderError(
                kind="auth", message="key rejected", retryable=False,
                status=401, provider="ollama-local", model="qwen3",
                body='{"error":{"message":"bad key"}}')
            provider = ScriptedProvider([[provider_base.error_chunk(failure)]])
            _, patches = self._worker_env(directory, provider)
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    redirect_stdout(output):
                self.assertEqual(desktop_worker.run("hi"), 1)

            text = output.getvalue()
            self.assertEqual(len(frames_named(text, "error")), 1)
            frame = frames_named(text, "error")[0]
            self.assertEqual(frame["kind"], "auth")
            self.assertEqual(frame["message"], "key rejected")
            self.assertEqual(frame["status"], 401)
            self.assertEqual(frame["provider"], "ollama-local")
            self.assertFalse(frame["retryable"])
            self.assertIn("bad key", frame["body"])
            # The "[stream error]" compatibility line is not an answer.
            self.assertEqual(frames_named(text, "delta"), [])

    def test_desktop_worker_silent_provider_becomes_an_error_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([[CompletionChunk(stop_reason="stop")]])
            _, patches = self._worker_env(directory, provider)
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    redirect_stdout(output):
                self.assertEqual(desktop_worker.run("hi"), 1)
            self.assertEqual(frame_events(output.getvalue()),
                             ["started", "info", "usage", "error"])

    def test_desktop_worker_refuses_to_fork_a_named_session(self):
        """Adversarial-review finding: a named session that cannot load must
        refuse the run, never answer with a blank history under a new id —
        that silent fork lost the whole conversation without a word."""
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                [CompletionChunk(text="reply", stop_reason="stop")]])
            _, patches = self._worker_env(directory, provider)
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    redirect_stdout(output):
                self.assertEqual(
                    desktop_worker.run("hi", session_name="ses_doesnotexist"),
                    1)
            frames = worker_frames(output.getvalue())
            self.assertEqual("error", frames[0]["event"])
            self.assertEqual("session", frames[0]["kind"])
            self.assertIn("ses_doesnotexist", frames[0]["message"])

    def test_desktop_worker_an_empty_session_name_starts_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = ScriptedProvider([
                [CompletionChunk(text="reply", stop_reason="stop")]])
            _, patches = self._worker_env(directory, provider)
            output = StringIO()
            with patches[0], patches[1], patches[2], patches[3], \
                    redirect_stdout(output):
                self.assertEqual(
                    desktop_worker.run("hi", session_name=""), 0)
            started = worker_frames(output.getvalue())[0]
            self.assertEqual("started", started["event"])
            self.assertTrue(started["session"])

    def test_desktop_worker_maps_agent_events_onto_frames(self):
        """Every Agent.on_event kind has a frame; unknown kinds stay silent."""
        output = StringIO()
        with redirect_stdout(output):
            desktop_worker._emit_agent_event("reasoning", "thinking about it")
            desktop_worker._emit_agent_event(
                "tool", {"name": "edit", "args": {"filePath": "/tmp/a.py"}})
            desktop_worker._emit_agent_event("tool_result", {
                "name": "edit", "title": "a.py", "output": "Edited a.py",
                "metadata": {"diff": "--- a\n+++ b\n+new\n", "exit": 0}})
            desktop_worker._emit_agent_event(
                "tool_error", {"name": "bash", "error": "boom"})
            desktop_worker._emit_agent_event("tool_result", {
                "name": "todowrite", "title": "1 todo",
                "metadata": {"todos": [{"content": "ship it",
                                        "status": "pending"}]}})
            desktop_worker._emit_agent_event("error", {
                "kind": "rate_limit", "message": "slow down", "retryable": True})
            desktop_worker._emit_agent_event("limit", {"steps": 40})
            desktop_worker._emit_agent_event("something_new", {"name": "x"})

        frames = worker_frames(output.getvalue())
        self.assertEqual([f["event"] for f in frames],
                         ["reasoning", "tool", "tool_result", "tool_error",
                          "tool_result", "todos", "error", "status"])
        self.assertEqual(frames[1]["title"], "/tmp/a.py")
        self.assertIn("+new", frames[2]["diff"])
        self.assertEqual(frames[2]["exit"], 0)
        self.assertEqual(frames[5]["text"], "[ ] ship it")
        self.assertEqual(frames[6]["kind"], "rate_limit")
        self.assertTrue(frames[6]["retryable"])

    def test_desktop_worker_clips_oversized_frame_payloads(self):
        """A huge diff must not stall the window looper on the pipe."""
        output = StringIO()
        with redirect_stdout(output):
            desktop_worker._emit_agent_event("tool_result", {
                "name": "write", "title": "big.txt",
                "output": "x" * 50000,
                "metadata": {"diff": "+" * 50000}})
        frame = worker_frames(output.getvalue())[0]
        self.assertLess(len(frame["diff"]), desktop_worker.MAX_FRAME_TEXT + 200)
        self.assertIn("truncated", frame["diff"])
        self.assertLess(len(frame["output"]), 2300)

    def test_configtool_accepts_key_on_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            output = StringIO()
            # Without the disabled keystore this stores "secret-value" in the
            # real Haiku keyring of whoever runs the suite, where it then
            # shadows their actual key for the openai provider.
            with (patch.object(configtool, "Config", return_value=config),
                  patch.object(config_module, "_keystore_bin",
                               return_value=None),
                  patch.object(sys, "stdin", StringIO("secret-value")),
                  redirect_stdout(output)):
                self.assertEqual(configtool.main(["set-key-stdin", "openai"]), 0)
                self.assertNotIn("secret-value", output.getvalue())
                self.assertEqual(config.get_api_key("openai"), "secret-value")

    def test_configtool_set_model_is_one_atomic_save(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            config.data.setdefault("providers", {})["chatgpt"] = {
                "dialect": "chatgpt", "model": "gpt-5.6-terra"}
            output = StringIO()
            with (patch.object(configtool, "Config", return_value=config),
                  redirect_stdout(output)):
                self.assertEqual(
                    configtool.main(["set-model", "chatgpt", "gpt-5.6-sol"]),
                    0)
                self.assertEqual(
                    configtool.main(["set-model", "nowhere", "x"]), 1)
            self.assertEqual("gpt-5.6-sol",
                             config.data["providers"]["chatgpt"]["model"])
            self.assertEqual("chatgpt", config.data["default_provider"])

    def test_configtool_efforts_is_model_aware(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            config.data.setdefault("providers", {})["chatgpt"] = {
                "dialect": "chatgpt", "model": "gpt-5.6-sol"}
            output = StringIO()
            with (patch.object(configtool, "Config", return_value=config),
                  redirect_stdout(output)):
                self.assertEqual(configtool.main(["efforts", "chatgpt"]), 0)
            listed = output.getvalue().split()
            self.assertIn("max", listed)
            self.assertIn("xhigh", listed)

    def test_configtool_set_effort_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            config.data.setdefault("providers", {})["chatgpt"] = {
                "dialect": "chatgpt", "model": "gpt-5.6-sol"}
            output = StringIO()
            with (patch.object(configtool, "Config", return_value=config),
                  redirect_stdout(output)):
                self.assertEqual(
                    configtool.main(["set-effort", "chatgpt", "xhigh"]), 0)
            self.assertEqual(
                "xhigh",
                config.data["providers"]["chatgpt"]["reasoning_effort"])

    def test_custom_provider_management(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(provider_command([
                    "add", "studio-ollama", "--base-url",
                    "http://ollama.tailnet:11434/v1", "--model", "qwen3",
                    "--no-key"], config=config), 0)
                self.assertEqual(provider_command(
                    ["default", "studio-ollama"], config=config), 0)
                self.assertEqual(provider_command(["list"], config=config), 0)
            self.assertIn("studio-ollama", output.getvalue())
            self.assertFalse(
                config.data["providers"]["studio-ollama"]["requires_key"])
            self.assertEqual(config.data["default_provider"], "studio-ollama")
            with redirect_stdout(StringIO()):
                self.assertEqual(provider_command(
                    ["remove", "studio-ollama"], config=config), 0)
            self.assertNotIn("studio-ollama", config.data["providers"])

    def test_configtool_add_provider_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            output = StringIO()
            with (patch.object(configtool, "Config", return_value=config),
                  redirect_stdout(output)):
                result = configtool.main([
                    "add-provider", "lan-model", "openai",
                    "http://10.0.0.5:11434/v1", "qwen3", "false"])
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue().strip(), "ok")
            self.assertFalse(config.data["providers"]["lan-model"]["requires_key"])
            fresh = Config(os.path.join(directory, "fresh.json"))
            self.assertNotIn("lan-model", fresh.data["providers"])

    def test_subscription_oauth_start_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            pending = {
                "provider": "chatgpt",
                "verification_uri": "https://auth.example/device",
                "verification_uri_complete": "https://auth.example/device",
                "user_code": "TEST-CODE",
            }
            with (patch.object(configtool, "begin_device_authorization",
                               return_value=pending) as begin,
                  patch.object(configtool, "spawn_background_completion")
                  as spawn):
                ok, result = configtool.start_oauth(config, "chatgpt")
            self.assertTrue(ok)
            self.assertEqual(result["method"], "auto")
            self.assertEqual(result["url"], "https://auth.example/device")
            self.assertIn("TEST-CODE", result["instructions"])
            self.assertIn("locally on Haiku", result["instructions"])
            begin.assert_called_once_with("chatgpt")
            self.assertEqual(spawn.call_args.args[0], "chatgpt")


if __name__ == "__main__":
    unittest.main()
