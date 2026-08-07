"""
Agent loop semantics, exercised with a stub provider (no network).

These cover what the old markdown-``` tool`` loop could not do at all:
several tool calls in one turn, results returned as `tool` messages tied to
their call id, and clean handling of malformed arguments.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode import agents as agents_mod  # noqa: E402
from haikode import context as context_mod  # noqa: E402
from haikode import memory as memory_mod  # noqa: E402
from haikode import prompt as prompt_mod  # noqa: E402
from haikode.agent import Agent, ProviderFailure, _CallAccumulator  # noqa: E402
from haikode.agents import AgentRegistry  # noqa: E402
from haikode.permission import PermissionRequest, Permissions  # noqa: E402
from haikode.providers.base import Provider, ProviderError  # noqa: E402
from haikode.schema import CompletionChunk, Msg, PermissionDenied  # noqa: E402


class ScriptedProvider(Provider):
    """Replays canned turns; records what the agent sent upstream."""

    name = "scripted"

    def __init__(self, turns):
        self.turns = list(turns)
        self.seen = []

    def stream(self, messages, tools, model, max_tokens):
        self.seen.append(list(messages))
        chunks = self.turns.pop(0) if self.turns else [
            CompletionChunk(text="done", stop_reason="stop")]
        for chunk in chunks:
            yield chunk


def call_chunks(index, call_id, name, arguments, split=1):
    """Emit a tool call, optionally split across several deltas."""
    out = [CompletionChunk(tool_call_delta={
        "index": index, "id": call_id, "name": name, "arguments": ""})]
    step = max(1, len(arguments) // split)
    for start in range(0, len(arguments), step):
        out.append(CompletionChunk(tool_call_delta={
            "index": index, "id": None, "name": None,
            "arguments": arguments[start:start + step]}))
    return out


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-agent-")
        Path(self.dir, "a.txt").write_text("alpha\n")
        Path(self.dir, "b.txt").write_text("beta\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def build(self, turns, **kwargs):
        return Agent(provider=ScriptedProvider(turns), model="test",
                     permissions=Permissions(auto_approve=True),
                     cwd=self.dir, **kwargs)


class TestAccumulator(unittest.TestCase):
    def test_reassembles_split_arguments(self):
        acc = _CallAccumulator()
        for chunk in call_chunks(0, "c1", "read", '{"filePath": "a.txt"}', split=5):
            acc.add(chunk.tool_call_delta)
        calls = acc.finish()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "read")
        self.assertEqual(calls[0].arguments, {"filePath": "a.txt"})

    def test_multiple_parallel_calls_keep_order(self):
        acc = _CallAccumulator()
        for chunk in call_chunks(1, "c2", "read", '{"filePath": "b.txt"}'):
            acc.add(chunk.tool_call_delta)
        for chunk in call_chunks(0, "c1", "read", '{"filePath": "a.txt"}'):
            acc.add(chunk.tool_call_delta)
        calls = acc.finish()
        self.assertEqual([c.id for c in calls], ["c1", "c2"])

    def test_empty_arguments_become_empty_dict(self):
        acc = _CallAccumulator()
        acc.add({"index": 0, "id": "c1", "name": "list", "arguments": ""})
        self.assertEqual(acc.finish()[0].arguments, {})

    def test_malformed_arguments_are_flagged_not_crashed(self):
        acc = _CallAccumulator()
        acc.add({"index": 0, "id": "c1", "name": "read",
                 "arguments": 'not json at all'})
        self.assertIn("__malformed__", acc.finish()[0].arguments)

    def test_arguments_wrapped_in_prose_are_recovered(self):
        acc = _CallAccumulator()
        acc.add({"index": 0, "id": "c1", "name": "read",
                 "arguments": 'Sure! {"filePath": "a.txt"} hope that helps'})
        self.assertEqual(acc.finish()[0].arguments, {"filePath": "a.txt"})


class TestLoop(AgentTestCase):
    def test_parallel_tool_calls_in_one_turn(self):
        turn = (call_chunks(0, "c1", "read", '{"filePath": "a.txt"}')
                + call_chunks(1, "c2", "read", '{"filePath": "b.txt"}')
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn, [CompletionChunk(text="both read", stop_reason="stop")]])

        names = []
        agent.run("go", on_event=lambda kind, payload:
                  names.append(payload["name"]) if kind == "tool" else None)

        self.assertEqual(names, ["read", "read"])
        tool_messages = [m for m in agent.messages if m.role == "tool"]
        self.assertEqual(len(tool_messages), 2)
        self.assertEqual({m.tool_call_id for m in tool_messages}, {"c1", "c2"})
        self.assertIn("alpha", tool_messages[0].content)
        self.assertIn("beta", tool_messages[1].content)

    def test_results_are_tool_role_not_user(self):
        """The old loop injected results as user messages, breaking tracking."""
        turn = (call_chunks(0, "c1", "list", "{}")
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn, [CompletionChunk(text="ok", stop_reason="stop")]])
        agent.run("go")
        roles = [m.role for m in agent.messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])

    def test_second_request_includes_the_tool_result(self):
        turn = (call_chunks(0, "c1", "read", '{"filePath": "a.txt"}')
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn, [CompletionChunk(text="ok", stop_reason="stop")]])
        agent.run("go")
        second_request = agent.provider.seen[1]
        self.assertTrue(any(m.role == "tool" and "alpha" in m.content
                            for m in second_request))

    def test_unknown_tool_reported_to_model(self):
        turn = (call_chunks(0, "c1", "nosuchtool", "{}")
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn, [CompletionChunk(text="ok", stop_reason="stop")]])
        agent.run("go")
        tool_message = [m for m in agent.messages if m.role == "tool"][0]
        self.assertIn("unknown tool", tool_message.content.lower())

    def test_tool_error_is_fed_back_not_raised(self):
        turn = (call_chunks(0, "c1", "read", '{"filePath": "missing.txt"}')
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn, [CompletionChunk(text="recovered", stop_reason="stop")]])
        result = agent.run("go")
        tool_message = [m for m in agent.messages if m.role == "tool"][0]
        self.assertIn("Error", tool_message.content)
        self.assertEqual(result, "recovered")

    def test_malformed_arguments_ask_model_to_retry(self):
        turn = ([CompletionChunk(tool_call_delta={
                    "index": 0, "id": "c1", "name": "read", "arguments": "{oops"})]
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn, [CompletionChunk(text="ok", stop_reason="stop")]])
        agent.run("go")
        tool_message = [m for m in agent.messages if m.role == "tool"][0]
        self.assertIn("not valid JSON", tool_message.content)

    def test_permission_denied_is_reported_not_fatal(self):
        turn = (call_chunks(0, "c1", "write",
                            '{"filePath": "new.txt", "content": "x"}')
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = Agent(provider=ScriptedProvider(
            [turn, [CompletionChunk(text="understood", stop_reason="stop")]]),
            model="test", permissions=Permissions(asker=lambda r: "reject"),
            cwd=self.dir)
        result = agent.run("go")
        tool_message = [m for m in agent.messages if m.role == "tool"][0]
        self.assertIn("rejected", tool_message.content.lower())
        self.assertEqual(result, "understood")
        self.assertFalse(Path(self.dir, "new.txt").exists())

    def test_step_limit_stops_the_loop(self):
        def endless():
            return (call_chunks(0, "c1", "list", "{}")
                    + [CompletionChunk(stop_reason="tool_calls")])
        handoff = [CompletionChunk(
            text="Step budget reached; file review remains.",
            stop_reason="stop")]
        agent = self.build([endless(), endless(), handoff], max_steps=3)
        result = agent.run("go")
        self.assertEqual(result, "Step budget reached; file review remains.")
        self.assertEqual(agent.steps_used, 3)

    def test_plain_answer_without_tools(self):
        agent = self.build([[CompletionChunk(text="42", stop_reason="stop")]])
        self.assertEqual(agent.run("what is 6*7?"), "42")

    def test_streamed_text_reaches_on_text(self):
        agent = self.build([[CompletionChunk(text="he"),
                             CompletionChunk(text="llo"),
                             CompletionChunk(stop_reason="stop")]])
        seen = []
        agent.run("hi", on_text=seen.append)
        self.assertEqual("".join(seen), "hello")

    def test_usage_is_accumulated(self):
        agent = self.build([[CompletionChunk(text="x", stop_reason="stop",
                                             usage={"input": 10, "output": 3})]])
        agent.run("hi")
        self.assertEqual(agent.tokens, {"input": 10, "output": 3})

    def test_abort_stops_between_tools(self):
        turn = (call_chunks(0, "c1", "list", "{}")
                + call_chunks(1, "c2", "list", "{}")
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn, [CompletionChunk(text="never", stop_reason="stop")]])

        def on_event(kind, payload):
            if kind == "tool":
                agent.abort()

        agent.run("go", on_event=on_event)
        tool_messages = [m for m in agent.messages if m.role == "tool"]
        self.assertTrue(any("Aborted" in m.content for m in tool_messages))
        # Every call must still be answered, or the next request is invalid.
        self.assertEqual({m.tool_call_id for m in tool_messages}, {"c1", "c2"})

    def test_abort_still_answers_every_pending_call(self):
        turn = (call_chunks(0, "c1", "list", "{}")
                + call_chunks(1, "c2", "list", "{}")
                + call_chunks(2, "c3", "list", "{}")
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn])
        agent.abort()
        agent.run("go")
        assistant = [m for m in agent.messages if m.role == "assistant"][0]
        answered = {m.tool_call_id for m in agent.messages if m.role == "tool"}
        self.assertEqual({c.id for c in assistant.tool_calls}, answered)

    def test_system_prompt_is_first_message_sent(self):
        agent = self.build([[CompletionChunk(text="ok", stop_reason="stop")]])
        agent.run("hi")
        first = agent.provider.seen[0][0]
        self.assertEqual(first.role, "system")
        self.assertIn("haikode", first.content)
        self.assertIn("Haiku", first.content)

    def test_tool_specs_are_offered_to_the_provider(self):
        """Regression: the old agent passed tools=[] and lost tool calling."""
        captured = {}

        class SpyProvider(ScriptedProvider):
            def stream(self, messages, tools, model, max_tokens):
                captured["tools"] = tools
                return super().stream(messages, tools, model, max_tokens)

        agent = Agent(provider=SpyProvider([[CompletionChunk(text="x", stop_reason="stop")]]),
                      model="test", permissions=Permissions(auto_approve=True),
                      cwd=self.dir)
        agent.run("hi")
        self.assertTrue(captured["tools"], "no tool specs were sent to the provider")
        self.assertIn("read", {spec.name for spec in captured["tools"]})

    def test_clear_resets_state(self):
        agent = self.build([[CompletionChunk(text="x", stop_reason="stop")]])
        agent.run("hi")
        agent.ctx.read_files.add("/tmp/x")
        agent.clear()
        self.assertEqual(agent.messages, [])
        self.assertEqual(agent.ctx.read_files, set())


class CompactionLatchAndAccounting(AgentTestCase):
    @staticmethod
    def _overflow_chunk(input_tokens=11):
        error = ProviderError(
            kind="context_overflow", message="prompt is too long",
            status=400, provider="scripted", model="m").as_dict()
        return CompletionChunk(stop_reason="error",
                               usage={"input": input_tokens, "output": 0,
                                      "error": error})

    def test_a_tool_loop_reuses_one_successful_summary(self):
        class SummaryProvider(Provider):
            name = "summary-count"

            def __init__(self):
                self.calls = 0

            def stream(self, messages, tools, model, max_tokens):
                self.calls += 1
                yield CompletionChunk(text="## Objective\n- keep the invariant")
                yield CompletionChunk(stop_reason="stop",
                                      usage={"input": 101, "output": 7})

        provider = SummaryProvider()
        agent = Agent(provider, "m", cwd=self.dir, context_window=20_000,
                      permissions=Permissions(auto_approve=True))
        original = [Msg(role="user" if index % 2 == 0 else "assistant",
                        content=("x" * 1200) + str(index))
                    for index in range(80)]
        agent.messages = original

        first = agent._messages_for_llm()
        for index in range(12):
            agent.messages.extend([
                Msg(role="user", content="new question %d" % index),
                Msg(role="assistant", content="new answer %d" % index),
            ])
            agent._messages_for_llm()

        self.assertEqual(1, provider.calls,
                         "new tail messages must not re-summarise the old fold")
        self.assertTrue(any((message.display or {}).get("summary")
                            for message in first))
        self.assertEqual(104, len(agent.messages),
                         "the raw transcript is the persistence source")
        self.assertEqual(101, agent.usage.hidden_session.input_tokens)
        self.assertEqual(7, agent.usage.hidden_session.output_tokens)
        self.assertEqual(108, agent.usage.session.total)
        self.assertEqual(0, agent.usage.latest.total,
                         "summary usage is not the next-request context size")

    def test_two_agents_never_share_a_cached_summary_or_its_usage(self):
        class SummaryProvider(Provider):
            name = "same-name"

            def __init__(self, text):
                self.text = text
                self.calls = 0

            def stream(self, messages, tools, model, max_tokens):
                self.calls += 1
                yield CompletionChunk(text=self.text)
                yield CompletionChunk(stop_reason="stop",
                                      usage={"input": 9, "output": 2})

        raw = [Msg(role="user" if index % 2 == 0 else "assistant",
                   content=("same" * 300) + str(index))
               for index in range(80)]
        first_provider = SummaryProvider("summary from first")
        first = Agent(first_provider, "m", cwd=self.dir,
                      context_window=20_000, tool_names=[])
        first.messages = raw
        first_view = first._messages_for_llm()

        second_provider = SummaryProvider("summary from second")
        second = Agent(second_provider, "m", cwd=self.dir,
                       context_window=20_000, tool_names=[])
        second.messages = raw
        second_view = second._messages_for_llm()

        self.assertEqual((first_provider.calls, second_provider.calls), (1, 1))
        self.assertIn("summary from first", first_view[1].content)
        self.assertIn("summary from second", second_view[1].content)
        self.assertEqual(second.usage.hidden_session.total, 11)

    def test_tool_schemas_do_not_inflate_the_token_scale(self):
        from haikode.context import message_tokens
        from haikode.usage import tool_specs_tokens

        class MeasuringProvider(Provider):
            name = "measuring"

            def stream(self, messages, tools, model, max_tokens):
                prompt = (sum(message_tokens(message) for message in messages)
                          + tool_specs_tokens(tools))
                yield CompletionChunk(text="done", stop_reason="stop",
                                      usage={"input": prompt, "output": 1})

        agent = Agent(MeasuringProvider(), "m", cwd=self.dir,
                      permissions=Permissions(auto_approve=True))
        agent.run("hello")
        self.assertAlmostEqual(1.0, agent.token_scale)

    def test_unchanged_tool_schemas_are_estimated_once(self):
        from haikode import agent as agent_mod

        agent = self.build([], tool_names=["read", "list"])
        with patch.object(agent_mod, "tool_specs_tokens",
                          wraps=agent_mod.tool_specs_tokens) as estimate:
            first = agent._tool_schema_tokens()
            self.assertEqual(agent._tool_schema_tokens(), first)
            self.assertEqual(estimate.call_count, 1)
            # Replacing the bundle (MCP refresh/agent switch) invalidates by
            # object identity without every caller having to remember a flag.
            agent.specs = list(agent.specs)
            self.assertEqual(agent._tool_schema_tokens(), first)
            self.assertEqual(estimate.call_count, 2)

    def test_context_overflow_forces_one_fold_and_one_retry(self):
        provider = ScriptedProvider([
            [self._overflow_chunk()],
            [CompletionChunk(text="## Objective\n- retain the constraint"),
             CompletionChunk(stop_reason="stop",
                             usage={"input": 5, "output": 2})],
            [CompletionChunk(text="recovered", stop_reason="stop",
                             usage={"input": 7, "output": 1})],
        ])
        agent = Agent(provider, "m", cwd=self.dir, context_window=1_000_000,
                      tool_names=[],
                      permissions=Permissions(auto_approve=True))
        agent.messages = [
            Msg(role="user" if index % 2 == 0 else "assistant",
                content="history %d" % index)
            for index in range(10)
        ]
        events = []
        streamed = []

        self.assertEqual(agent.run(
            "continue", on_text=streamed.append,
            on_event=lambda kind, payload: events.append((kind, payload))),
            "recovered")

        self.assertEqual(len(provider.seen), 3)  # failed main, summary, main
        self.assertEqual(streamed, ["recovered"])
        self.assertNotIn("error", [kind for kind, _ in events])
        self.assertIn("compaction", [kind for kind, _ in events])
        self.assertTrue(any((message.display or {}).get("summary")
                            for message in provider.seen[-1]))
        self.assertEqual(agent.tokens, {"input": 23, "output": 3})
        self.assertEqual(agent.usage.hidden_run.input_tokens, 5)
        self.assertEqual(agent.usage.hidden_run.output_tokens, 2)
        self.assertEqual(agent.usage.latest.input_tokens, 7)

    def test_context_overflow_after_partial_output_is_never_replayed(self):
        provider = ScriptedProvider([[
            CompletionChunk(text="partial"), self._overflow_chunk()]])
        agent = Agent(provider, "m", cwd=self.dir, context_window=1_000_000,
                      tool_names=[],
                      permissions=Permissions(auto_approve=True))
        events = []
        streamed = []

        with self.assertRaises(ProviderFailure):
            agent.run("continue", on_text=streamed.append,
                      on_event=lambda kind, payload:
                      events.append((kind, payload)))

        self.assertEqual(len(provider.seen), 1)
        self.assertEqual(streamed, ["partial"])
        self.assertEqual(events[-1][0], "error")

    def test_a_failed_turn_cannot_survive_in_the_latched_provider_tail(self):
        class SummaryProvider(Provider):
            name = "summary"

            def stream(self, messages, tools, model, max_tokens):
                yield CompletionChunk(text="## Objective\n- keep history")
                yield CompletionChunk(stop_reason="stop")

        agent = Agent(SummaryProvider(), "m", cwd=self.dir,
                      context_window=20_000, tool_names=[],
                      permissions=Permissions(auto_approve=True))
        agent.messages = [
            Msg(role="user" if index % 2 == 0 else "assistant",
                content="x" * 1200 + str(index))
            for index in range(80)
        ]
        agent._messages_for_llm()  # establish the separate summary latch

        failure = ProviderError(kind="auth", message="refused",
                                status=401).as_dict()
        agent.provider = ScriptedProvider([[
            CompletionChunk(stop_reason="error", usage={"error": failure})]])
        with self.assertRaises(ProviderFailure):
            agent.run("failed question")

        next_provider = ScriptedProvider([[
            CompletionChunk(text="ok", stop_reason="stop")]])
        agent.provider = next_provider
        self.assertEqual(agent.run("fresh question"), "ok")
        replay = "\n".join(message.content for message in next_provider.seen[0])
        self.assertNotIn("failed question", replay)
        self.assertIn("fresh question", replay)


class TestSubsetTools(AgentTestCase):
    def test_tool_names_limits_what_is_offered(self):
        agent = self.build([[CompletionChunk(text="x", stop_reason="stop")]],
                           tool_names=["read", "list"])
        self.assertEqual(set(agent.tools), {"read", "list"})
        agent.run("hi")
        self.assertEqual({s.name for s in agent.provider.seen and agent.specs},
                         {"read", "list"})


# --------------------------------------------------------------------------
# prompt assembly
# --------------------------------------------------------------------------


class IsolatedAgentTestCase(AgentTestCase):
    """An agent whose global config, memory and agent files live in a temp dir.

    Without this every test would read the developer's own ~/.config/haikode,
    so a stray global AGENTS.md or memory would change what the assertions see.
    """

    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp(prefix="haikode-home-")
        globals_dir = Path(self.home, "config")
        globals_dir.mkdir(parents=True, exist_ok=True)
        self._patches = [
            patch.object(memory_mod, "global_config_dir", lambda: globals_dir),
            patch.object(agents_mod, "global_config_dir", lambda: globals_dir),
            patch.object(context_mod, "global_config_dir", lambda: globals_dir),
            patch.object(context_mod, "home_dir", lambda: Path(self.home)),
        ]
        for entry in self._patches:
            entry.start()
        prompt_mod.clear_cache()

    def tearDown(self):
        for entry in reversed(self._patches):
            entry.stop()
        shutil.rmtree(self.home, ignore_errors=True)
        super().tearDown()


class TestPromptAssembly(IsolatedAgentTestCase):
    def _system_text(self, **kwargs):
        agent = self.build([[CompletionChunk(text="x", stop_reason="stop")]], **kwargs)
        return agent, agent._system_message().content

    def test_model_family_selects_the_prompt_variant(self):
        agent = Agent(provider=ScriptedProvider([]), model="claude-sonnet-5",
                      permissions=Permissions(auto_approve=True), cwd=self.dir)
        self.assertEqual(agent.prompt_variant(), "anthropic")
        content = agent._system_message().content
        marker = prompt_mod.load("anthropic").strip().splitlines()[0]
        self.assertIn(marker, content)
        self.assertNotIn(prompt_mod.load("gpt").strip().splitlines()[0], content)

    def test_a_different_model_selects_a_different_variant(self):
        agent = Agent(provider=ScriptedProvider([]), model="gpt-5.4",
                      permissions=Permissions(auto_approve=True), cwd=self.dir)
        self.assertEqual(agent.prompt_variant(), "gpt")
        self.assertIn(prompt_mod.load("gpt").strip().splitlines()[0],
                      agent._system_message().content)

    def test_switching_model_reselects_the_variant(self):
        agent = Agent(provider=ScriptedProvider([]), model="gpt-5.4",
                      permissions=Permissions(auto_approve=True), cwd=self.dir)
        agent._system_message()                       # prime the cache
        agent.set_model("claude-sonnet-5")
        self.assertIn(prompt_mod.load("anthropic").strip().splitlines()[0],
                      agent._system_message().content)

    def test_project_instructions_reach_the_system_message(self):
        Path(self.dir, "AGENTS.md").write_text("Never commit to main.\n")
        _, content = self._system_text()
        self.assertIn("Never commit to main.", content)

    def test_config_declared_instructions_reach_the_system_message(self):
        Path(self.dir, "docs").mkdir()
        Path(self.dir, "docs", "house.md").write_text("House rule: tabs.\n")
        agent = Agent(provider=ScriptedProvider([]), model="test",
                      permissions=Permissions(auto_approve=True), cwd=self.dir,
                      instructions=[Path(self.dir, "docs", "house.md")])
        self.assertIn("House rule: tabs.", agent._system_message().content)

    def test_memory_block_is_appended(self):
        store = memory_mod.MemoryStore(self.dir)
        store.write("The test suite runs with python3 -m unittest.",
                    name="how-to-test")
        _, content = self._system_text()
        self.assertIn("# Memory", content)
        self.assertIn("python3 -m unittest", content)

    def test_empty_memory_block_tells_the_model_the_store_exists(self):
        _, content = self._system_text()
        self.assertIn("# Memory", content)
        self.assertIn("No saved memories yet.", content)

    def test_written_memory_shows_up_without_rebuilding_the_agent(self):
        agent, before = self._system_text()
        self.assertIn("No saved memories yet.", before)
        memory_mod.MemoryStore(self.dir).write("Owner prefers Norwegian.",
                                               name="language")
        agent.refresh_memory()
        self.assertIn("Owner prefers Norwegian.", agent._system_message().content)

    def test_explicit_system_prompt_still_wins(self):
        """The task tool hands its sub-agent a prompt; it must be honoured."""
        agent = Agent(provider=ScriptedProvider([]), model="test",
                      permissions=Permissions(auto_approve=True), cwd=self.dir,
                      system_prompt="You are a sub-agent.")
        content = agent._system_message().content
        self.assertIn("You are a sub-agent.", content)
        self.assertNotIn(prompt_mod.load("default").strip().splitlines()[0], content)

    def test_prompt_assembly_is_cached_between_requests(self):
        agent, _ = self._system_text()
        with patch.object(agent.context, "instructions",
                          side_effect=AssertionError("re-read")) as spy:
            agent._system_message()
            spy.assert_not_called()


# --------------------------------------------------------------------------
# agents and plan mode
# --------------------------------------------------------------------------


class TestAgentSwitching(IsolatedAgentTestCase):
    def build_with_registry(self, turns=(), **kwargs):
        registry = AgentRegistry.load(self.dir)
        return Agent(provider=ScriptedProvider(list(turns)), model="test",
                     permissions=Permissions(asker=lambda r: "once"),
                     cwd=self.dir, registry=registry, **kwargs)

    def test_default_agent_is_build(self):
        agent = self.build_with_registry()
        self.assertEqual(agent.agent_name, "build")
        self.assertFalse(agent.plan_mode)

    def test_plan_mode_removes_the_write_tools(self):
        agent = self.build_with_registry()
        self.assertIn("edit", agent.tools)
        agent.switch_agent("plan")
        for name in ("edit", "write", "bash", "apply_patch"):
            self.assertNotIn(name, agent.tools, name)
        self.assertEqual({spec.name for spec in agent.specs}, set(agent.tools))

    def test_plan_mode_denies_a_write_through_permissions(self):
        """Belt and braces: the tool is gone *and* the permission is denied."""
        agent = self.build_with_registry()
        agent.switch_agent("plan")
        for key in ("edit", "write", "bash"):
            with self.assertRaises(PermissionDenied):
                agent.permissions.ask(PermissionRequest(key, ["x.txt"], "write x"))

    def test_plan_mode_reports_readonly(self):
        agent = self.build_with_registry()
        agent.switch_agent("plan")
        self.assertTrue(agent.plan_mode)

    def test_a_hallucinated_write_in_plan_mode_is_answered_not_executed(self):
        turn = (call_chunks(0, "c1", "write",
                            '{"filePath": "new.txt", "content": "x"}')
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build_with_registry(
            [turn, [CompletionChunk(text="understood", stop_reason="stop")]])
        agent.switch_agent("plan")
        agent.run("make the change now")
        tool_message = [m for m in agent.messages if m.role == "tool"][0]
        self.assertIn("not available", tool_message.content)
        self.assertFalse(Path(self.dir, "new.txt").exists())

    def test_session_grants_do_not_leak_into_plan_mode(self):
        agent = self.build_with_registry()
        agent.permissions.grant_always("bash", ["*"])
        agent.switch_agent("plan")
        with self.assertRaises(PermissionDenied):
            agent.permissions.ask(PermissionRequest("bash", ["rm -rf /"], "rm"))

    def test_grants_the_new_agent_allows_are_kept(self):
        agent = self.build_with_registry()
        agent.permissions.grant_always("webfetch", ["https://example.com/*"])
        agent.switch_agent("plan")
        agent.permissions.ask(PermissionRequest(
            "webfetch", ["https://example.com/x"], "fetch"))   # must not raise

    def test_an_agent_without_rules_keeps_the_callers_permission_object(self):
        """The front-end wires its asker onto the object it handed over."""
        permissions = Permissions(asker=lambda r: "once")
        agent = Agent(provider=ScriptedProvider([]), model="test",
                      permissions=permissions, cwd=self.dir,
                      registry=AgentRegistry.load(self.dir))
        self.assertIs(agent.permissions, permissions)
        self.assertIs(agent.ctx.permissions, permissions)

    def test_grants_survive_a_round_trip_through_plan_mode(self):
        agent = self.build_with_registry()
        agent.permissions.grant_always("write", ["notes.md"])
        agent.switch_agent("plan")
        agent.switch_agent("build")
        agent.permissions.ask(PermissionRequest("write", ["notes.md"], "write"))

    def test_repeated_switches_do_not_pile_up_grants(self):
        agent = self.build_with_registry()
        agent.permissions.grant_always("write", ["notes.md"])
        for _ in range(4):
            agent.switch_agent("plan")
            agent.switch_agent("build")
        self.assertEqual(agent.permissions._session_grants["write"], ["notes.md"])

    def test_the_asker_follows_the_agent_switch(self):
        seen = []
        agent = self.build_with_registry()
        agent.permissions.asker = lambda request: seen.append(request) or "once"
        agent.switch_agent("plan")
        agent.permissions.ask(PermissionRequest("webfetch", ["https://x"], "get"))
        self.assertEqual(len(seen), 1)

    def test_switch_agent_preserves_history(self):
        agent = self.build_with_registry(
            [[CompletionChunk(text="hello", stop_reason="stop")]])
        agent.run("hi")
        before = list(agent.messages)
        agent.switch_agent("plan")
        self.assertEqual([m.content for m in agent.messages],
                         [m.content for m in before])
        self.assertEqual([m.role for m in agent.messages],
                         [m.role for m in before])

    def test_switching_back_restores_the_tools(self):
        agent = self.build_with_registry()
        full = set(agent.tools)
        agent.switch_agent("plan")
        agent.switch_agent("build")
        self.assertEqual(set(agent.tools), full)

    def test_switching_back_restores_permissions(self):
        agent = self.build_with_registry()
        agent.switch_agent("plan")
        agent.switch_agent("build")
        agent.permissions.ask(PermissionRequest("edit", ["a.txt"], "edit"))

    def test_agent_switch_never_re_enables_a_disabled_tool(self):
        """The project's tools map is below the agent, not above it."""
        registry = AgentRegistry.load(self.dir)
        agent = Agent(provider=ScriptedProvider([]), model="test",
                      permissions=Permissions(auto_approve=True), cwd=self.dir,
                      registry=registry, tool_names=["read", "list", "edit"])
        agent.switch_agent("plan")
        agent.switch_agent("build")
        self.assertEqual(set(agent.tools), {"read", "list", "edit"})

    def test_an_agent_can_name_its_own_model(self):
        Path(self.dir, ".haikode", "agent").mkdir(parents=True)
        Path(self.dir, ".haikode", "agent", "cheap.md").write_text(
            "---\ndescription: A cheap agent\nmode: primary\n"
            "model: gpt-4o-mini\n---\nBe brief.\n")
        agent = self.build_with_registry()
        agent.switch_agent("cheap")
        self.assertEqual(agent.model, "gpt-4o-mini")
        agent.switch_agent("build")
        self.assertEqual(agent.model, "test")

    def test_a_model_assigned_directly_survives_an_agent_switch(self):
        """The desktop worker sets agent.model itself; a switch must respect it."""
        agent = self.build_with_registry()
        agent.model = "some-other-model"
        agent.switch_agent("plan")
        self.assertEqual(agent.model, "some-other-model")

    def test_switch_agent_returns_a_line_a_ui_can_show(self):
        """The TUI renders the return value straight into the transcript."""
        agent = self.build_with_registry()
        message = agent.switch_agent("plan")
        self.assertIsInstance(message, str)
        self.assertIn("plan", message)
        self.assertIn("read-only", message)
        self.assertLess(len(message), 80)
        self.assertEqual(agent.agent_def.name, "plan")

    def test_unknown_agent_raises_and_changes_nothing(self):
        agent = self.build_with_registry()
        tools = set(agent.tools)
        with self.assertRaises(KeyError):
            agent.switch_agent("nope")
        self.assertEqual(agent.agent_name, "build")
        self.assertEqual(set(agent.tools), tools)

    def test_plan_reminder_rides_the_next_user_message(self):
        agent = self.build_with_registry(
            [[CompletionChunk(text="ok", stop_reason="stop")]])
        agent.switch_agent("plan")
        agent.run("what would you change?")
        user = [m for m in agent.messages if m.role == "user"][0]
        self.assertIn("READ-ONLY", user.content)
        self.assertIn("what would you change?", user.content)

    def test_leaving_plan_mode_injects_the_build_switch_reminder(self):
        agent = self.build_with_registry(
            [[CompletionChunk(text="ok", stop_reason="stop")]])
        agent.switch_agent("plan")
        agent._pending_reminders = []          # drop the enter reminder
        agent.switch_agent("build")
        agent.run("go")
        user = [m for m in agent.messages if m.role == "user"][0]
        self.assertIn("no longer in read-only mode", user.content)

    def test_the_plan_prompt_reaches_the_system_message(self):
        agent = self.build_with_registry()
        agent.switch_agent("plan")
        content = agent._system_message().content
        self.assertIn(prompt_mod.plan_preamble().strip().splitlines()[0], content)
        # The built-in plan reminder must not replace the coding instructions.
        self.assertIn(prompt_mod.load("default").strip().splitlines()[0], content)

    def test_registry_is_loaded_lazily_when_none_is_given(self):
        agent = Agent(provider=ScriptedProvider([]), model="test",
                      permissions=Permissions(auto_approve=True), cwd=self.dir)
        self.assertIsNone(agent._registry)
        self.assertIn("plan", agent.registry.names())


# --------------------------------------------------------------------------
# usage accounting
# --------------------------------------------------------------------------


class TestUsageTracking(AgentTestCase):
    def test_usage_accumulates_across_runs(self):
        agent = self.build([
            [CompletionChunk(text="a", stop_reason="stop",
                             usage={"input": 10, "output": 3})],
            [CompletionChunk(text="b", stop_reason="stop",
                             usage={"input": 5, "output": 2})]])
        agent.run("one")
        agent.run("two")
        self.assertEqual(agent.usage.session.input_tokens, 15)
        self.assertEqual(agent.usage.session.output_tokens, 5)
        self.assertEqual(agent.tokens, {"input": 15, "output": 5})

    def test_run_counter_covers_only_the_current_turn(self):
        agent = self.build([
            [CompletionChunk(text="a", stop_reason="stop",
                             usage={"input": 10, "output": 3})],
            [CompletionChunk(text="b", stop_reason="stop",
                             usage={"input": 5, "output": 2})]])
        agent.run("one")
        agent.run("two")
        self.assertEqual(agent.usage.run.input_tokens, 5)
        self.assertEqual(agent.usage.session.input_tokens, 15)

    def test_provider_spellings_are_understood(self):
        agent = self.build([[CompletionChunk(
            text="a", stop_reason="stop",
            usage={"prompt_tokens": 7, "completion_tokens": 2,
                   "cache_read": 4, "reasoning": 9})]])
        agent.run("hi")
        self.assertEqual(agent.usage.session.cache_read, 4)
        self.assertEqual(agent.usage.session.reasoning_tokens, 9)

    def test_clear_resets_the_tracker(self):
        agent = self.build([[CompletionChunk(text="a", stop_reason="stop",
                                             usage={"input": 10, "output": 3})]])
        agent.run("hi")
        agent.clear()
        self.assertEqual(agent.usage.session.total, 0)

    def test_context_state_counts_the_system_message_and_the_tools(self):
        agent = self.build([[CompletionChunk(text="a", stop_reason="stop")]])
        state = agent.context_state()
        self.assertGreater(state.system, 0)
        self.assertGreater(state.tools, 0)
        self.assertEqual(state.window, agent.context_window)
        self.assertGreaterEqual(state.used, state.system + state.tools)

    def test_context_state_grows_with_the_conversation(self):
        agent = self.build([[CompletionChunk(text="a" * 400, stop_reason="stop")]])
        before = agent.context_state().used
        agent.run("tell me something long")
        self.assertGreater(agent.context_state().used, before)


# --------------------------------------------------------------------------
# the invariant every provider validates
# --------------------------------------------------------------------------


class TestToolMessagePairing(AgentTestCase):
    def _pairs(self, agent):
        requested = [c.id for m in agent.messages if m.role == "assistant"
                     for c in m.tool_calls]
        answered = [m.tool_call_id for m in agent.messages if m.role == "tool"]
        return requested, answered

    def test_exactly_one_tool_message_per_call(self):
        turn = (call_chunks(0, "c1", "list", "{}")
                + call_chunks(1, "c2", "read", '{"filePath": "a.txt"}')
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn, [CompletionChunk(text="ok", stop_reason="stop")]])
        agent.run("go")
        requested, answered = self._pairs(agent)
        self.assertEqual(sorted(requested), sorted(answered))
        self.assertEqual(len(answered), len(set(answered)))

    def test_exactly_one_tool_message_per_call_on_abort(self):
        turn = (call_chunks(0, "c1", "list", "{}")
                + call_chunks(1, "c2", "list", "{}")
                + call_chunks(2, "c3", "list", "{}")
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn])

        def on_event(kind, payload):
            if kind == "tool":
                agent.abort()

        agent.run("go", on_event=on_event)
        requested, answered = self._pairs(agent)
        self.assertEqual(sorted(requested), sorted(answered))
        self.assertEqual(len(answered), len(set(answered)))

    def test_exactly_one_tool_message_per_call_when_a_tool_is_missing(self):
        turn = (call_chunks(0, "c1", "nosuchtool", "{}")
                + call_chunks(1, "c2", "list", "{}")
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn, [CompletionChunk(text="ok", stop_reason="stop")]])
        agent.run("go")
        requested, answered = self._pairs(agent)
        self.assertEqual(sorted(requested), sorted(answered))

    def test_model_switch_mid_session_leaves_the_pairing_intact(self):
        turn = (call_chunks(0, "c1", "list", "{}")
                + [CompletionChunk(stop_reason="tool_calls")])
        agent = self.build([turn, [CompletionChunk(text="ok", stop_reason="stop")],
                            [CompletionChunk(text="ok2", stop_reason="stop")]])
        agent.run("go")
        agent.set_model("claude-sonnet-5")
        agent.run("again")
        requested, answered = self._pairs(agent)
        self.assertEqual(sorted(requested), sorted(answered))
        self.assertEqual(agent.model, "claude-sonnet-5")


class TestCrossProviderSubagents(AgentTestCase):
    """An agent definition may pin `model: provider/id`; the task tool must
    run the sub-agent on that provider — or fail loudly. Nothing here is
    vendor-specific: any configured profile name works, which is the point.
    """

    class Sibling(ScriptedProvider):
        name = "elsewhere"

        def __init__(self, turns):
            super().__init__(turns)
            self.models = []

        def stream(self, messages, tools, model, max_tokens):
            self.models.append(model)
            return super().stream(messages, tools, model, max_tokens)

    def parent_with(self, defn_model, factory=None):
        from haikode.agents import AgentDef
        registry = AgentRegistry.load(self.dir)
        registry.agents["qa"] = AgentDef(name="qa", description="pinned QA",
                                         mode="subagent", model=defn_model)
        parent = Agent(provider=self.Sibling([]), model="parent-model",
                       permissions=Permissions(auto_approve=True),
                       cwd=self.dir, registry=registry)
        parent.provider.name = "scripted"      # the session's own provider
        parent.provider_factory = factory
        return parent

    def run_task(self, parent, **extra):
        from haikode.tool.task import TaskTool
        args = {"description": "qa", "prompt": "review this",
                "subagent_type": "qa"}
        args.update(extra)
        return TaskTool().execute(args, parent.ctx)

    def test_a_pinned_provider_model_runs_on_the_sibling_client(self):
        sibling = self.Sibling([[CompletionChunk(text="verdict",
                                                 stop_reason="stop")]])
        built = []

        def factory(name):
            built.append(name)
            return sibling

        parent = self.parent_with("elsewhere/model-x", factory)
        self.run_task(parent)
        self.assertEqual(["elsewhere"], built)
        self.assertEqual(["model-x"], sibling.models)
        self.assertEqual([], parent.provider.seen)   # parent's client unused

    def test_a_bare_model_id_stays_on_the_parents_provider(self):
        parent = self.parent_with("model-y")
        parent.provider.turns = [[CompletionChunk(text="ok",
                                                  stop_reason="stop")]]
        self.run_task(parent)
        self.assertEqual(["model-y"], parent.provider.models)

    def test_no_factory_fails_loudly_not_silently_on_the_wrong_model(self):
        parent = self.parent_with("elsewhere/model-z")
        with self.assertRaises(RuntimeError) as caught:
            self.run_task(parent)
        self.assertIn("elsewhere", str(caught.exception))
        self.assertIn("qa", str(caught.exception))

    def test_an_unavailable_provider_names_itself_in_the_failure(self):
        def factory(name):
            raise KeyError("no profile %r" % name)

        parent = self.parent_with("elsewhere/model-z", factory)
        with self.assertRaises(RuntimeError) as caught:
            self.run_task(parent)
        self.assertIn("not available", str(caught.exception))

    def test_the_calls_model_argument_beats_the_definition(self):
        sibling = self.Sibling([[CompletionChunk(text="verdict",
                                                 stop_reason="stop")]])
        parent = self.parent_with("elsewhere/model-x", lambda name: sibling)
        self.run_task(parent, model="elsewhere/model-call")
        self.assertEqual(["model-call"], sibling.models)

    def test_the_calls_model_needs_no_subagent_type(self):
        sibling = self.Sibling([[CompletionChunk(text="ok",
                                                 stop_reason="stop")]])
        built = []

        def factory(name):
            built.append(name)
            return sibling

        parent = self.parent_with("", factory)
        from haikode.tool.task import TaskTool
        TaskTool().execute({"description": "qa", "prompt": "look",
                            "model": "elsewhere/model-q"}, parent.ctx)
        self.assertEqual(["elsewhere"], built)
        self.assertEqual(["model-q"], sibling.models)

    def test_a_bare_call_model_stays_on_the_parents_provider(self):
        parent = self.parent_with("")
        parent.provider.turns = [[CompletionChunk(text="ok",
                                                  stop_reason="stop")]]
        self.run_task(parent, model="model-bare")
        self.assertEqual(["model-bare"], parent.provider.models)


if __name__ == "__main__":
    unittest.main()
