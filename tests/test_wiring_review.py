"""
Regressions found reviewing the wiring round.

Each class here pins one defect that shipped as "wired and verified": a stranded
permission asker, an unpaired tool history reaching the provider, warnings that
were collected and never shown, and an agent picker listing a different table
than the one the switch resolves against.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode import repl as repl_mod  # noqa: E402
from haikode import runtime, tui  # noqa: E402
from haikode.agent import (INTERRUPTED_TOOL_RESULT, Agent,  # noqa: E402
                           pair_tool_messages)
from haikode.agents import AgentRegistry  # noqa: E402
from haikode.config import Config  # noqa: E402
from haikode.permission import (PermissionDenied, PermissionRequest,  # noqa: E402
                                Permissions)
from haikode.providers.base import Provider  # noqa: E402
from haikode.schema import CompletionChunk, Msg, ToolCall  # noqa: E402


class StubProvider(Provider):
    name = "stub"

    def __init__(self):
        self.seen = []

    def stream(self, messages, tools, model, max_tokens):
        self.seen.append(list(messages))
        yield CompletionChunk(text="ok", stop_reason="stop")


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-review-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)


# --------------------------------------------------------------------------
# the asker must not be stranded on an agent overlay
# --------------------------------------------------------------------------


class AskerSurvivesTheFirstAgent(TempDirCase):
    """A session that *starts* on an agent with its own permission rules.

    `default_agent: plan` (or `-a plan`) makes Agent.permissions an overlay, so
    a front-end that wires its asker onto agent.permissions -- which is exactly
    what TUI._wire_permissions does -- never reaches the base object the next
    switch builds from.
    """

    def build(self, agent_name):
        return Agent(provider=StubProvider(), model="test", cwd=self.dir,
                     registry=AgentRegistry.load(self.dir),
                     permissions=Permissions(), agent_name=agent_name)

    def test_asker_wired_on_a_plan_overlay_reaches_build(self):
        agent = self.build("plan")
        self.assertIsNot(agent.permissions, agent._base_permissions)
        agent.permissions.asker = lambda request: "once"

        agent.switch_agent("build")
        # Without the mirror this raises "no interactive session is available"
        # for the rest of the session, with the user sitting right there.
        agent.permissions.ask(PermissionRequest("edit", ["a.txt"], "edit"))

    def test_asker_wired_on_build_still_reaches_plan(self):
        agent = self.build("build")
        agent.permissions.asker = lambda request: "once"
        agent.switch_agent("plan")
        self.assertIsNotNone(agent.permissions.asker)

    def test_a_headless_session_is_not_given_an_asker(self):
        agent = self.build("plan")
        agent.switch_agent("build")
        with self.assertRaises(PermissionDenied):
            agent.permissions.ask(PermissionRequest("edit", ["a.txt"], "edit"))

    def test_plan_still_refuses_after_the_asker_is_mirrored(self):
        """Mirroring a capability must not mirror a decision."""
        agent = self.build("plan")
        agent.permissions.asker = lambda request: "always"
        agent.switch_agent("build")
        agent.switch_agent("plan")
        for key in ("edit", "write", "bash"):
            with self.assertRaises(PermissionDenied):
                agent.permissions.ask(PermissionRequest(key, ["x"], "x"))


# --------------------------------------------------------------------------
# tool_call / tool_result pairing
# --------------------------------------------------------------------------


def _assistant(*ids):
    return Msg(role="assistant", content="",
               tool_calls=[ToolCall(id=i, name="bash", arguments={}) for i in ids])


def _result(call_id, text="out"):
    return Msg(role="tool", tool_call_id=call_id, content=text)


class PairToolMessages(unittest.TestCase):
    def test_a_consistent_history_is_returned_untouched(self):
        messages = [Msg(role="user", content="hi"), _assistant("a", "b"),
                    _result("a"), _result("b"),
                    Msg(role="assistant", content="done")]
        self.assertIs(pair_tool_messages(messages), messages)

    def test_a_history_without_tools_is_returned_untouched(self):
        messages = [Msg(role="user", content="hi"),
                    Msg(role="assistant", content="there")]
        self.assertIs(pair_tool_messages(messages), messages)

    def test_an_unanswered_call_gets_a_synthetic_result(self):
        repaired = pair_tool_messages([Msg(role="user", content="go"),
                                       _assistant("a")])
        self.assertEqual([m.role for m in repaired], ["user", "assistant", "tool"])
        self.assertEqual(repaired[-1].tool_call_id, "a")
        self.assertEqual(repaired[-1].content, INTERRUPTED_TOOL_RESULT)

    def test_a_partially_answered_turn_is_completed_in_place(self):
        repaired = pair_tool_messages([_assistant("a", "b"), _result("b"),
                                       Msg(role="user", content="stop")])
        self.assertEqual([m.role for m in repaired],
                         ["assistant", "tool", "tool", "user"])
        self.assertEqual([m.tool_call_id for m in repaired[1:3]], ["b", "a"])

    def test_an_orphaned_result_is_dropped(self):
        repaired = pair_tool_messages([Msg(role="user", content="go"),
                                       _result("gone"),
                                       Msg(role="assistant", content="hi")])
        self.assertEqual([m.role for m in repaired], ["user", "assistant"])

    def test_results_separated_from_their_call_are_re_gathered(self):
        """Adjacency is the invariant, not mere presence."""
        repaired = pair_tool_messages([_assistant("a"),
                                       Msg(role="user", content="wait"),
                                       _result("a")])
        self.assertEqual([m.role for m in repaired], ["assistant", "tool", "user"])
        self.assertEqual(repaired[1].tool_call_id, "a")

    def test_repair_is_idempotent(self):
        once = pair_tool_messages([_assistant("a", "b"), _result("a")])
        self.assertIs(pair_tool_messages(once), once)

    def test_the_original_history_is_not_mutated(self):
        messages = [_assistant("a")]
        pair_tool_messages(messages)
        self.assertEqual(len(messages), 1)


class PairingReachesTheProvider(TempDirCase):
    def test_an_adopted_half_turn_is_repaired_before_the_next_request(self):
        provider = StubProvider()
        agent = Agent(provider=provider, model="test", cwd=self.dir,
                      permissions=Permissions(auto_approve=True))
        # What a front-end copies over when it rebuilds the agent while a tool
        # is still running on the worker thread.
        agent.messages = [Msg(role="user", content="delete tmp"), _assistant("c1")]
        agent.run("never mind")

        sent = provider.seen[0]
        calls = [call.id for m in sent for call in m.tool_calls]
        answered = [m.tool_call_id for m in sent if m.role == "tool"]
        self.assertEqual(calls, ["c1"])
        self.assertEqual(answered, ["c1"])

    def test_the_stored_history_is_left_alone(self):
        """The repair is about the wire, not about rewriting the record."""
        agent = Agent(provider=StubProvider(), model="test", cwd=self.dir,
                      permissions=Permissions(auto_approve=True))
        agent.messages = [_assistant("c1")]
        agent._messages_for_llm()
        self.assertEqual(len(agent.messages), 1)


# --------------------------------------------------------------------------
# runtime: warnings that were collected too early
# --------------------------------------------------------------------------


class RuntimeWarnings(TempDirCase):
    def build(self, project):
        Path(self.dir, ".git").mkdir()
        Path(self.dir, "haikode.json").write_text(json.dumps(project))
        config = Config(str(Path(tempfile.mkdtemp(), "config.json")))
        config.data["providers"] = {"p": {"base_url": "http://x", "model": "m",
                                          "requires_key": False}}
        config.data["default_provider"] = "p"
        return runtime.build_agent(config, "p", self.dir)

    def test_an_instruction_path_outside_the_project_is_reported(self):
        """The warning is written by resolve_instructions(), which build_agent
        used to call *after* it had already snapshotted the warning list.

        The target is created here rather than named (`/etc/hosts`) so the test
        means the same thing on Haiku, where that path does not exist and the
        entry would be skipped for the wrong reason.
        """
        outside = Path(tempfile.mkdtemp(), "id_rsa")
        outside.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
        agent = self.build({"instructions": [str(outside)]})
        self.assertEqual(agent.instruction_paths, [])
        self.assertTrue(any("outside the project" in w for w in agent.warnings),
                        agent.warnings)
        self.assertNotIn("PRIVATE KEY", agent._system_message().content)

    def test_a_widened_permission_is_still_reported(self):
        agent = self.build({"permission": {"bash": "allow"}})
        self.assertTrue(any(w.startswith("permission escalation")
                            for w in agent.warnings), agent.warnings)

    def test_a_failing_escalation_check_is_reported_not_swallowed(self):
        class Exploding:
            errors: list = []
            unknown: list = []
            warnings: list = []

            def escalations(self):
                raise RuntimeError("boom")

        found = runtime.project_warnings(Exploding())
        self.assertTrue(any("escalation check failed" in w for w in found), found)


# --------------------------------------------------------------------------
# the TUI's own wiring
# --------------------------------------------------------------------------


class FakeAgent:
    def __init__(self, registry=None, warnings=()):
        self.registry = registry
        self.warnings = list(warnings)
        self.messages = []
        self.permissions = Permissions()
        self.model = "m"
        self.tokens = {"input": 0, "output": 0}


def make_tui(agent, cwd="."):
    ui = tui.TUI(lambda: agent, config=None, cwd=cwd)
    ui.agent = agent
    return ui


class AgentPickerUsesTheAgentsRegistry(TempDirCase):
    def setUp(self):
        super().setUp()
        Path(self.dir, "haikode.json").write_text(json.dumps(
            {"agents": {"reviewer": {"description": "review", "mode": "primary"},
                        "plan": {"disable": True}}}))
        self.project = json.loads(Path(self.dir, "haikode.json").read_text())

    def test_the_picker_lists_what_switch_agent_can_resolve(self):
        registry = AgentRegistry.load(self.dir, self.project)
        ui = make_tui(FakeAgent(registry=registry), cwd=self.dir)
        _module, found = ui._agent_registry()
        self.assertIs(found, registry)
        self.assertIn("reviewer", found.names())
        self.assertNotIn("plan", found.names())

    def test_an_agent_without_a_registry_still_gets_one(self):
        ui = make_tui(FakeAgent(registry=None), cwd=self.dir)
        _module, found = ui._agent_registry()
        self.assertIn("build", found.names())


class WarningsReachTheScreen(unittest.TestCase):
    def test_config_warnings_are_put_in_the_transcript(self):
        ui = make_tui(FakeAgent(warnings=[
            "permission escalation: /repo/haikode.json widens ask to allow",
            "unknown config key: nonsense"]))
        ui._report_warnings()
        texts = [entry.text for entry in ui.transcript.entries]
        self.assertEqual(len(texts), 2)
        self.assertTrue(any("escalation" in t for t in texts), texts)

    def test_an_escalation_is_shown_as_an_error_not_a_note(self):
        ui = make_tui(FakeAgent(warnings=["permission escalation: bash"]))
        ui._report_warnings()
        self.assertEqual(ui.transcript.entries[0].kind, "error")

    def test_an_unknown_key_is_only_a_note(self):
        ui = make_tui(FakeAgent(warnings=["unknown config key: nonsense"]))
        ui._report_warnings()
        self.assertEqual(ui.transcript.entries[0].kind, "info")

    def test_a_rebuilt_agent_does_not_repeat_them(self):
        ui = make_tui(FakeAgent(warnings=["config error: broken json"]))
        ui._report_warnings()
        ui._report_warnings()
        self.assertEqual(len(ui.transcript.entries), 1)

    def test_status_lists_them_too(self):
        ui = make_tui(FakeAgent(warnings=["config error: broken json"]))
        self.assertIn("config error: broken json", ui._warnings())


class SessionStoreIsOpenedOnce(unittest.TestCase):
    def test_the_store_is_reused_across_calls(self):
        ui = make_tui(FakeAgent())
        first = ui._session_store()
        self.assertIs(ui._session_store(), first)


class WarningsDoNotEatTheHomeScreen(unittest.TestCase):
    """The home screen is selected by an empty transcript."""

    def test_announcing_leaves_the_transcript_empty(self):
        ui = make_tui(FakeAgent(warnings=["config error: broken json"]))
        ui._announce_warnings()
        self.assertEqual(ui.transcript.entries, [])
        self.assertTrue(ui._at_home())
        self.assertIn("config warning", ui.status_hint)

    def test_nothing_is_announced_when_there_is_nothing_to_say(self):
        ui = make_tui(FakeAgent())
        ui._announce_warnings()
        self.assertEqual(ui.status_hint, "")

    def test_the_first_submit_puts_them_above_the_turn(self):
        ui = make_tui(FakeAgent(warnings=["config error: broken json"]))
        ui._submit("hello")
        kinds = [(e.kind, e.text) for e in ui.transcript.entries]
        self.assertEqual(kinds[0], ("error", "config error: broken json"))
        self.assertEqual(kinds[1][0], "user")
        ui.running = False
        ui._submit("again")
        self.assertEqual(len([e for e in ui.transcript.entries
                              if e.kind == "error"]), 1)


class StatusReportsTheRulesInForce(TempDirCase):
    """The home screen and /status must not describe looser rules as tighter."""

    def build(self, project, agent_name="", trusted=True):
        Path(self.dir, ".git").mkdir()
        Path(self.dir, "haikode.json").write_text(json.dumps(project))
        config = Config(str(Path(tempfile.mkdtemp(), "config.json")))
        config.data["providers"] = {"p": {"base_url": "http://x", "model": "m",
                                          "requires_key": False}}
        config.data["default_provider"] = "p"
        # Trust is passed explicitly rather than through the store: a widening
        # only reaches the permission layer from a repository the user trusted,
        # and this class is about *reporting* what reached it, not about the
        # trust boundary itself (tests/test_trust.py owns that).
        agent = runtime.build_agent(
            config, "p", self.dir, agent_name=agent_name,
            project=runtime.load_project(config, self.dir, trusted=trusted))
        ui = tui.TUI(lambda: agent, config=config, cwd=self.dir)
        ui.agent = agent
        return ui, config

    def test_a_project_widened_rule_is_reported_as_widened(self):
        ui, _ = self.build({"permission": {"bash": "allow"}})
        info = ui._setup(refresh=True)
        self.assertIn("bash", info.allow_tools)
        self.assertNotIn("bash", info.ask_tools)

    def test_an_agent_overlay_is_reported_too(self):
        ui, _ = self.build({}, agent_name="plan")
        info = ui._setup(refresh=True)
        self.assertIn("bash", info.deny_tools)

    def test_credentials_still_come_from_the_users_own_config(self):
        ui, _ = self.build({"permission": {"bash": "allow"}})
        self.assertEqual(ui._setup(refresh=True).auth, "no key required")

    def test_a_plain_config_is_passed_straight_through(self):
        ui = make_tui(FakeAgent())
        ui.config = object()
        ui.agent.permissions.config = None
        self.assertIs(ui._effective_config(), ui.config)


# --------------------------------------------------------------------------
# the seams between the security round's fixes
# --------------------------------------------------------------------------


class PermissionsListingReadsTheRealRules(TempDirCase):
    """`/permissions` must describe the rules the permission layer enforces.

    It used to re-parse `config.data["permission"]` itself, which meant a
    second, older idea of what a rule may look like. permission.py accepts a
    list of [pattern, decision] pairs -- the shape its own docstring recommends
    now that order is significant -- and that second reader crashed on it.
    """

    def make_repl(self, rules):
        config = Config(str(Path(self.dir, "config.json")))
        config.data["providers"] = {"p": {"base_url": "http://x", "model": "m",
                                          "requires_key": False}}
        config.data["default_provider"] = "p"
        config.data["permission"] = rules
        return repl_mod.REPL(config, cwd=self.dir)

    def test_the_list_form_is_listed_rather_than_crashing(self):
        repl = self.make_repl({"bash": [["*", "allow"], ["rm *", "deny"]]})
        out = repl._cmd_permissions("")
        self.assertIn("rm *", out)
        # Evaluation order, so a reader can see which rule actually wins.
        self.assertLess(out.index("allow  *"), out.index("deny   rm *"))

    def test_ordered_objects_keep_their_order(self):
        repl = self.make_repl({"bash": {"*": "deny", "git status": "allow"}})
        out = repl._cmd_permissions("")
        self.assertLess(out.index("deny   *"), out.index("allow  git status"))

    def test_session_grants_are_listed_apart_from_the_rules(self):
        repl = self.make_repl({})
        repl.agent.permissions.grant_always("bash", ["git status *"])
        out = repl._cmd_permissions("")
        self.assertIn("granted for this session only", out)
        self.assertIn("git status *", out)


class AnAbortedSubagentStopsTheParent(TempDirCase):
    """Agent.run() swallows its own ToolAborted to keep the history well-formed.

    The task tool therefore has to re-check the shared abort Event, or an
    interrupted delegation is recorded as a completed tool call and the parent
    takes another provider step.
    """

    def test_the_task_tool_reports_the_abort(self):
        from haikode.schema import ToolAborted
        from haikode.tool import REGISTRY

        parent = Agent(provider=StubProvider(), model="test", cwd=self.dir,
                       permissions=Permissions(asker=lambda request: "once"))
        parent.ctx.agent = parent
        parent.abort()
        with self.assertRaises(ToolAborted):
            REGISTRY["task"].execute({"description": "d", "prompt": "p"},
                                     parent.ctx)


if __name__ == "__main__":
    unittest.main()
