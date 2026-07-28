"""
What the agent loop must enforce on behalf of every tool.

Three reproduced defects are pinned here:

* `Tool.permission` used to enforce nothing. Tools asked for themselves, and
  the two that did not — todowrite and task — ran under any configuration,
  including `todowrite: deny`. The check now happens in the agent, before
  dispatch, and these tests drive it through the real registry.
* `Agent.abort()` set a boolean nobody could wait on: the provider had no
  handle at all, so a stalled read ran to its own timeout, and a sub-agent got
  a snapshot of the flag rather than the flag.
* A provider failure arrived as a chunk and was appended as the assistant's
  answer, which is then replayed to the model next turn as its own words.
"""

import json
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode.agent import Agent, ProviderFailure  # noqa: E402
from haikode.permission import Permissions  # noqa: E402
from haikode.providers.base import (Provider, ProviderError,  # noqa: E402
                                    error_chunk)
from haikode.schema import CompletionChunk  # noqa: E402
from haikode.tool import ToolContext, ToolResult  # noqa: E402
from haikode.tool.base import Tool  # noqa: E402


class FakeConfig:
    """Config stand-in: Permissions only ever reads `.data`."""

    def __init__(self, permission=None):
        self.data: Dict[str, Any] = {"permission": dict(permission or {})}

    def save(self):
        return True


class ScriptedProvider(Provider):
    """Replays canned turns and counts the rounds it was asked for."""

    name = "scripted"

    def __init__(self, turns=()):
        self.turns = [list(turn) for turn in turns]
        self.rounds = 0
        self.abort = None               # every real provider has this slot

    def stream(self, messages, tools, model, max_tokens):
        self.rounds += 1
        chunks = self.turns.pop(0) if self.turns else [
            CompletionChunk(text="done", stop_reason="stop")]
        for chunk in chunks:
            yield chunk


class StallingProvider(ScriptedProvider):
    """Replays its turns, then blocks the way a real stream blocks.

    net's read loop waits for bytes with the abort handle in hand and the
    dialects end the stream when it trips (`except Aborted: return`). Without a
    handle there is nothing to wait on but the clock, which is exactly the
    defect: the agent's own check only runs once a chunk has arrived.
    """

    def __init__(self, turns=(), seconds: float = 5.0):
        super().__init__(turns)
        self.seconds = seconds
        self.stalled = threading.Event()
        self.waited = 0.0

    def stream(self, messages, tools, model, max_tokens):
        if self.turns:
            yield from super().stream(messages, tools, model, max_tokens)
            return
        self.rounds += 1
        self.stalled.set()
        started = time.monotonic()
        handle = getattr(self, "abort", None)
        if handle is not None:
            handle.wait(self.seconds)
        else:
            time.sleep(self.seconds)
        self.waited = time.monotonic() - started


def tool_call(call_id: str, name: str, arguments: Dict[str, Any]):
    """One turn: a single tool call, fully formed."""
    return [CompletionChunk(tool_call_delta={
        "index": 0, "id": call_id, "name": name,
        "arguments": json.dumps(arguments)}),
        CompletionChunk(stop_reason="tool_calls")]


def text_turn(text: str):
    return [CompletionChunk(text=text, stop_reason="stop")]


class ScopedTool(Tool):
    """A tool whose calls differ in scope, declared rather than asked for."""

    name = "scoped"
    permission = "scoped"
    asks_own_permission = False
    parameters = {"type": "object",
                  "properties": {"target": {"type": "string"}},
                  "required": ["target"]}

    def __init__(self):
        self.ran = []

    def permission_patterns(self, args, ctx):
        return ["scoped:%s" % args.get("target", "")]

    def execute(self, args, ctx) -> ToolResult:
        self.ran.append(args.get("target"))
        return ToolResult(title="scoped", output="ran %s" % args.get("target"))


class SilentTool(Tool):
    """Claims to ask for itself and never does — the shape of the defect."""

    name = "silent"
    permission = "bash"
    asks_own_permission = True
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.ran = 0

    def execute(self, args, ctx) -> ToolResult:
        self.ran += 1
        return ToolResult(title="silent", output="ran")


class EchoTool(Tool):
    """Asks for itself with the same identity the agent checks."""

    name = "echo"
    permission = "echo"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.ran = 0

    def execute(self, args, ctx) -> ToolResult:
        ctx.ask("echo", ["echo"], "Run the echo tool")
        self.ran += 1
        return ToolResult(title="echo", output="ran")


class EnforcementTestCase(unittest.TestCase):
    def setUp(self):
        self.prompts = []
        self.answers = ["once"]

    def asker(self, request):
        self.prompts.append(request)
        return self.answers[min(len(self.prompts) - 1, len(self.answers) - 1)]

    def build(self, turns=(), permission=None, asker=None, provider=None,
              tools=None, **kwargs):
        agent = Agent(provider=provider or ScriptedProvider(turns),
                      model="test",
                      permissions=Permissions(config=FakeConfig(permission),
                                              asker=asker),
                      cwd=str(Path(__file__).resolve().parent.parent),
                      **kwargs)
        for tool in tools or []:
            agent.tools[tool.name] = tool
        return agent

    @staticmethod
    def tool_messages(agent):
        return [m for m in agent.messages if m.role == "tool"]


class DeclaredPermissionsAreEnforced(EnforcementTestCase):
    def test_denied_todowrite_leaves_the_list_alone(self):
        """Reproduced: `todowrite: deny` and the todo list was replaced anyway."""
        agent = self.build([tool_call("c1", "todowrite",
                                      {"todos": [{"content": "x",
                                                  "status": "pending"}]}),
                            text_turn("ok")],
                           permission={"todowrite": "deny"})
        agent.run("go")
        self.assertEqual(agent.ctx.todos, [])
        self.assertIn("rejected", self.tool_messages(agent)[0].content)

    def test_denied_task_never_spawns_a_sub_agent(self):
        agent = self.build([tool_call("c1", "task", {"description": "d",
                                                     "prompt": "p"}),
                            text_turn("ok")],
                           permission={"task": "deny"})
        agent.run("go")
        # A sub-agent shares the provider, so it would show up as a round.
        self.assertEqual(agent.provider.rounds, 2)
        self.assertIn("rejected", self.tool_messages(agent)[0].content)

    def test_a_denied_key_stops_a_tool_that_forgot_to_ask(self):
        """The check must not depend on the tool remembering to make it."""
        silent = SilentTool()
        agent = self.build([tool_call("c1", "silent", {}), text_turn("ok")],
                           permission={"bash": {"*": "deny"}}, tools=[silent])
        agent.run("go")
        self.assertEqual(silent.ran, 0)
        self.assertIn("rejected", self.tool_messages(agent)[0].content)

    def test_a_rule_that_denies_some_patterns_does_not_deny_the_tool(self):
        """The pre-dispatch check may only act on a deny that covers everything.

        `{"*": "allow", "rm *": "deny"}` denies one command shape, and the tool
        is the only thing that knows which shape this call is.
        """
        silent = SilentTool()
        agent = self.build([tool_call("c1", "silent", {}), text_turn("ok")],
                           permission={"bash": {"*": "allow", "rm *": "deny"}},
                           tools=[silent])
        agent.run("go")
        self.assertEqual(silent.ran, 1)

    def test_headless_ask_is_refused_rather_than_run(self):
        """No asker means no approval; the tool must not run regardless."""
        agent = self.build([tool_call("c1", "todowrite", {"todos": []}),
                            text_turn("ok")],
                           permission={"todowrite": "ask"})
        agent.run("go")
        self.assertIn("rejected", self.tool_messages(agent)[0].content)

    def test_a_denied_tool_is_reported_as_denied_not_as_an_error(self):
        events = []
        agent = self.build([tool_call("c1", "todowrite", {"todos": []}),
                            text_turn("ok")],
                           permission={"todowrite": "deny"})
        agent.run("go", on_event=lambda kind, payload: events.append(kind))
        self.assertIn("tool_denied", events)
        self.assertNotIn("tool_error", events)


class PermissionScopeIsDeclaredNotGuessed(EnforcementTestCase):
    def test_a_narrow_pattern_cannot_be_blanket_granted(self):
        """"always" grants what the user saw, not the key.

        With patterns defaulting to ["*"] the first approval would authorise
        every later call under that key for the rest of the session.
        """
        scoped = ScopedTool()
        self.answers = ["always"]
        agent = self.build([tool_call("c1", "scoped", {"target": "alpha"}),
                            tool_call("c2", "scoped", {"target": "beta"}),
                            text_turn("ok")],
                           asker=self.asker, tools=[scoped])
        agent.run("go")
        self.assertEqual(scoped.ran, ["alpha", "beta"])
        self.assertEqual([p.patterns for p in self.prompts],
                         [["scoped:alpha"], ["scoped:beta"]])

    def test_the_same_scope_twice_is_granted_without_a_second_prompt(self):
        scoped = ScopedTool()
        self.answers = ["always"]
        agent = self.build([tool_call("c1", "scoped", {"target": "alpha"}),
                            tool_call("c2", "scoped", {"target": "alpha"}),
                            text_turn("ok")],
                           asker=self.asker, tools=[scoped])
        agent.run("go")
        self.assertEqual(len(self.prompts), 1)

    def test_an_always_answer_does_not_carry_to_another_tool(self):
        """The default pattern is the tool's name, so a grant stays there."""

        class OtherTool(SilentTool):
            name = "other"
            permission = "silent_key"
            asks_own_permission = False

        first, second = SilentTool(), OtherTool()
        first.permission = "silent_key"
        first.asks_own_permission = False
        self.answers = ["always"]
        agent = self.build([tool_call("c1", "silent", {}),
                            tool_call("c2", "other", {}),
                            text_turn("ok")],
                           asker=self.asker, tools=[first, second])
        agent.run("go")
        self.assertEqual([p.patterns for p in self.prompts],
                         [["silent"], ["other"]])


class ToolsThatAskAreNotAskedForTwice(EnforcementTestCase):
    def test_bash_prompts_once_and_names_the_command(self):
        agent = self.build([tool_call("c1", "bash", {"command": "echo hi"}),
                            text_turn("ok")],
                           permission={"bash": "ask"}, asker=self.asker)
        agent.run("go")
        self.assertEqual(len(self.prompts), 1)
        self.assertIn("echo hi", self.prompts[0].title)

    def test_an_identical_request_inside_one_call_prompts_once(self):
        """The central check and a tool asking for the same scope are one ask."""
        echo = EchoTool()
        echo.asks_own_permission = False        # force both to fire
        agent = self.build([tool_call("c1", "echo", {}), text_turn("ok")],
                           asker=self.asker, tools=[echo])
        agent.run("go")
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(echo.ran, 1)

    def test_additional_scope_is_still_put_to_the_user(self):
        """A second, different request in the same call is a different action."""

        class ExternalTool(EchoTool):
            name = "external"

            def execute(self, args, ctx) -> ToolResult:
                ctx.ask("external_directory", ["/elsewhere/*"], "Write outside")
                self.ran += 1
                return ToolResult(title="external", output="ran")

        tool = ExternalTool()
        tool.asks_own_permission = False
        agent = self.build([tool_call("c1", "external", {}), text_turn("ok")],
                           asker=self.asker, tools=[tool])
        agent.run("go")
        self.assertEqual([p.key for p in self.prompts],
                         ["echo", "external_directory"])

    def test_the_memo_does_not_outlive_one_tool_call(self):
        """Two calls to the same tool are two actions, even in one turn."""
        echo = EchoTool()
        echo.asks_own_permission = False
        agent = self.build([tool_call("c1", "echo", {}),
                            tool_call("c2", "echo", {}),
                            text_turn("ok")],
                           asker=self.asker, tools=[echo])
        agent.run("go")
        self.assertEqual(len(self.prompts), 2)

    def test_a_tool_driven_directly_still_asks_every_time(self):
        """Outside the agent there is no call scope and no memo."""
        ctx = ToolContext(cwd=".", permissions=Permissions(
            config=FakeConfig({"echo": "ask"}), asker=self.asker))
        EchoTool().execute({}, ctx)
        EchoTool().execute({}, ctx)
        self.assertEqual(len(self.prompts), 2)


class AbortReachesEverything(EnforcementTestCase):
    def test_abort_interrupts_a_stalled_provider(self):
        provider = StallingProvider(seconds=5.0)
        agent = self.build(provider=provider)
        timer = threading.Timer(0.05, agent.abort)
        timer.start()
        try:
            started = time.monotonic()
            agent.run("go")
            elapsed = time.monotonic() - started
        finally:
            timer.cancel()
        self.assertLess(elapsed, 1.0)
        self.assertLess(provider.waited, 1.0)
        # An aborted round is not an answer: nothing may be stored as one.
        self.assertFalse([m for m in agent.messages if m.role == "assistant"])

    def test_the_provider_is_given_the_agents_own_handle(self):
        provider = ScriptedProvider([text_turn("ok")])
        agent = self.build(provider=provider)
        agent.run("go")
        self.assertIs(provider.abort, agent.abort_event)
        self.assertFalse(provider.abort.is_set())
        agent.abort()
        self.assertTrue(provider.abort.is_set())

    def test_abort_reaches_a_running_sub_agent(self):
        provider = StallingProvider(
            [tool_call("c1", "task", {"description": "dig", "prompt": "p"})],
            seconds=5.0)
        agent = self.build(provider=provider)
        finished = threading.Event()

        def go():
            agent.run("go")
            finished.set()

        worker = threading.Thread(target=go, daemon=True)
        worker.start()
        self.assertTrue(provider.stalled.wait(2.0), "sub-agent never streamed")
        agent.abort()
        self.assertTrue(finished.wait(2.0),
                        "abort did not reach the sub-agent's provider")
        worker.join(1.0)

    def test_a_sub_agent_cannot_clear_its_parents_abort(self):
        """Starting a run resets the flag; a borrowed one is not ours to reset."""
        parent = self.build()
        sub = self.build()
        sub.ctx.aborted = parent.ctx.aborted
        parent.abort()
        self.assertTrue(sub.ctx.aborted)
        sub.ctx.aborted = False
        self.assertFalse(parent.ctx.aborted)    # a bool still writes through

    def test_the_flag_still_reads_as_a_bool(self):
        agent = self.build()
        self.assertFalse(agent.ctx.aborted)
        self.assertEqual(agent.ctx.aborted, False)
        agent.abort()
        self.assertTrue(agent.ctx.aborted)
        self.assertEqual(agent.ctx.aborted, True)

    def test_a_fresh_run_clears_a_previous_abort(self):
        agent = self.build([text_turn("ok")])
        agent.abort()
        self.assertEqual(agent.run("go"), "ok")


class ProviderErrorsAreNotAnswers(EnforcementTestCase):
    @staticmethod
    def failure(kind="auth", message="Authentication failed (ollama)"):
        return error_chunk(ProviderError(kind=kind, message=message,
                                         status=401, provider="ollama"))

    def test_a_structured_error_is_raised_not_stored(self):
        agent = self.build([[self.failure()]])
        with self.assertRaises(ProviderFailure) as caught:
            agent.run("go")
        self.assertEqual(caught.exception.kind, "auth")
        self.assertIn("Authentication failed", str(caught.exception))
        self.assertFalse([m for m in agent.messages if m.role == "assistant"])

    def test_the_error_marker_never_reaches_on_text(self):
        agent = self.build([[self.failure()]])
        streamed = []
        with self.assertRaises(ProviderFailure):
            agent.run("go", on_text=streamed.append)
        self.assertEqual(streamed, [])

    def test_the_failure_is_reported_as_an_error_event(self):
        events = []
        agent = self.build([[self.failure()]])
        with self.assertRaises(ProviderFailure):
            agent.run("go", on_event=lambda kind, payload:
                      events.append((kind, payload)))
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["kind"], "auth")

    def test_a_bare_error_stop_reason_is_a_failure_too(self):
        """Older providers only set stop_reason; that is still not an answer."""
        agent = self.build([[CompletionChunk(text="\n[stream error] refused",
                                             stop_reason="error")]])
        with self.assertRaises(ProviderFailure) as caught:
            agent.run("go")
        self.assertEqual(str(caught.exception), "refused")
        self.assertFalse([m for m in agent.messages if m.role == "assistant"])

    def test_text_streamed_before_the_error_is_not_kept(self):
        agent = self.build([[CompletionChunk(text="I think "), self.failure()]])
        with self.assertRaises(ProviderFailure):
            agent.run("go")
        self.assertFalse([m for m in agent.messages if m.role == "assistant"])


if __name__ == "__main__":
    unittest.main()
