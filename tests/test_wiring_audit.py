"""
The wiring audit: is every capability haikode claims actually reachable?

This file exists because the same failure has now happened several times: a
module is written, given a full unit-test suite, and never called by anything a
user can reach. `tests/test_wiring.py` covers the connections that *were*
repaired. This file covers the ones that are still open, plus a few static
invariants that make a future regression loud instead of silent.

A test here asserts a *call path*, never the existence of a symbol. Importing a
module proves nothing; the assertions below either drive a real front end and
watch the library get reached, or walk the source with `ast` and prove that
nothing else owns a second copy of the same job.

EXPECTED FAILURES
-----------------
Twenty tests in this file fail against the tree as it stands. That is the
point: each one is an executable bug report and turns green when the wiring is
put in. As of writing, these are the open ones:

  * UntrustedCustomCommandsCannotRunShell   - trust-boundary escape (see below)
  * UntrustedCommandCannotShadowABuiltin    - trust-boundary escape
  * RedactionCoversEveryTool                - redact() is wired to bash only,
                                              so `read .env` puts the key in
                                              the transcript and in sessions.db
  * MCPIsReachable / LSPIsReachable         - haikode/mcp.py and haikode/lsp.py
                                              have no production caller at all
  * SkillsAreWired                          - the `skill` tool is registered but
                                              skills.prompt_block() has no
                                              caller, so the model is never told
                                              which skills exist
  * AutomaticCompactionSummarises           - the agent's compaction can never
                                              reach the summariser
  * NoDeadPublicFunctions                   - dead entry points in palette,
                                              models, keybind, usage, session
  * EveryKeybindingHasAHandler              - 82 of 110 configurable bindings
                                              are never looked up by the TUI
  * PaletteDefaultCommandSetIsUsed          - palette.DEFAULT_COMMANDS is dead
  * ProjectConfigKeysThatGoNowhere          - `shell` and `theme` are validated
                                              and never read
  * EveryTestInAFileActuallyRuns            - three test files hide classes
                                              below their __main__ guard

The two trust tests are the important ones. A checked-out repository's
`.haikode/command/*.md` reaches `subprocess.run(shell=True)` through the `` !`cmd` ``
substitution with no trust check and no permission prompt, and a file named
after a built-in shadows it, so typing `/init` runs the repository's shell
instead of haikode's own command.

A note on DesktopWorkerUsesTheTurnController: it passes as of this writing, but
haikode/desktop_worker.py was observed flipping between the shared controller
and a private copy of the lifecycle several times in one hour. That is exactly
what this file is for.
"""

import ast
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode import agents as agents_mod  # noqa: E402
from haikode import context as context_mod  # noqa: E402
from haikode import desktop_worker as desktop_mod  # noqa: E402
from haikode import keybind as keybind_mod  # noqa: E402
from haikode import memory as memory_mod  # noqa: E402
from haikode import palette as palette_mod  # noqa: E402
from haikode import projectconfig as projectconfig_mod  # noqa: E402
from haikode import repl as repl_mod  # noqa: E402
from haikode import runtime  # noqa: E402
from haikode import tui as tui_mod  # noqa: E402
from haikode import turn as turn_mod  # noqa: E402
from haikode.config import Config  # noqa: E402
from haikode.schema import Msg  # noqa: E402

PACKAGE = Path(__file__).resolve().parent.parent / "haikode"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def package_sources():
    """(relative path, parsed module) for every file in the package."""
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:                      # pragma: no cover
            continue
        yield path.relative_to(PACKAGE.parent), tree


def imported_modules(tree):
    """Every `haikode.x` this module imports, however it spells the import."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.lstrip("."))
            if node.level and node.module is None:
                names.update(alias.name for alias in node.names)
            elif node.level:
                names.update("%s.%s" % (node.module, alias.name)
                             for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return {name.split(".")[0] for name in names} | names


def referenced_names(tree):
    """Every bare name and attribute label mentioned anywhere in `tree`."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


class StubAgent:
    """Just enough Agent for the turn lifecycle to run against."""

    class Ctx:
        def __init__(self):
            self.modified_files = {}
            self.read_files = set()
            self.todos = []

    def __init__(self, reply="ok"):
        self.reply = reply
        self.ctx = StubAgent.Ctx()
        self.messages = []
        self.cost = 0.0
        self.tokens = {"input": 0, "output": 0}
        self.steps_used = 0
        self.model = "stub"
        self.calls = []

    def run(self, message, on_text=None, on_event=None):
        self.calls.append(message)
        self.messages.append(Msg(role="user", content=message))
        self.messages.append(Msg(role="assistant", content=self.reply))
        if on_text:
            on_text(self.reply)
        return self.reply

    def abort(self):
        pass

    def refresh_memory(self):
        pass


class SandboxCase(unittest.TestCase):
    """A project directory and a private home, so no test touches the user's."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-audit-")
        self.home = tempfile.mkdtemp(prefix="haikode-audit-home-")
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
        # The summary cache is process state keyed by content. Two audit
        # classes build the same synthetic history, and unittest runs them
        # alphabetically: the summariser test filled the cache and the
        # drop-notice test then took the summary path, failing on a notice
        # that was legitimately absent. The wiring it audits was never
        # broken — the sandbox was.
        context_mod.clear_summary_cache()
        self.config = Config(path=str(Path(self.home, "config.json")))

    def tearDown(self):
        for entry in reversed(self._patches):
            entry.stop()
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)


# --------------------------------------------------------------------------
# 1. the turn lifecycle lives in exactly one place
# --------------------------------------------------------------------------


class OnlyTurnOwnsTheLifecycle(unittest.TestCase):
    """No second copy of run_turn(), enforced structurally.

    Every front end must go through TurnController, because that is the only
    place quick-capture, @-mention expansion, the session row, the revert
    checkpoint and the persistence-failure report all happen. A module that
    calls `agent.run()` itself has silently opted out of all five.
    """

    # tool/task.py spawns a *sub*-agent inside a turn that is already running;
    # that is a nested model call, not a user-facing turn, so it is exempt.
    EXEMPT = {"haikode/turn.py", "haikode/tool/task.py"}

    def test_no_front_end_calls_agent_run_directly(self):
        offenders = []
        for relative, tree in package_sources():
            if relative.as_posix() in self.EXEMPT:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (isinstance(func, ast.Attribute) and func.attr == "run"
                        and isinstance(func.value, ast.Name)
                        and func.value.id in ("agent", "self")):
                    if func.value.id == "self":
                        continue    # a module's own run(), not the agent's
                    offenders.append("%s:%d" % (relative.as_posix(), node.lineno))
        self.assertEqual(
            [], offenders,
            "these call agent.run() instead of TurnController.run_turn(), so "
            "they persist nothing and expand no @-mentions: " + ", ".join(offenders))

    def test_every_front_end_module_imports_the_controller(self):
        """A front end that never mentions `turn` cannot be going through it."""
        missing = []
        for name in ("repl.py", "tui.py", "desktop_worker.py"):
            tree = ast.parse((PACKAGE / name).read_text(encoding="utf-8"))
            if "turn" not in imported_modules(tree):
                missing.append("haikode/" + name)
        self.assertEqual([], missing,
                         "front ends with no import of haikode.turn: "
                         + ", ".join(missing))


class REPLUsesTheTurnController(SandboxCase):
    def test_send_runs_one_turn_through_the_controller(self):
        repl = repl_mod.REPL(self.config, cwd=self.dir)
        agent = repl.agent = StubAgent("hello from the repl")
        with patch.object(turn_mod.TurnController, "run_turn",
                          autospec=True,
                          side_effect=turn_mod.TurnController.run_turn) as spy:
            with redirect_stdout(io.StringIO()):
                repl.send("hi")
        self.assertTrue(spy.called, "REPL.send() bypassed TurnController")
        self.assertEqual(["hi"], agent.calls)


class TUIUsesTheTurnController(SandboxCase):
    """The default front end, driven without curses."""

    def build(self):
        agent = StubAgent("hello from the tui")
        tui = tui_mod.TUI(agent_factory=lambda: agent, config=self.config,
                          cwd=self.dir, agent=agent)
        return tui, agent

    def test_the_worker_body_goes_through_the_controller(self):
        tui, agent = self.build()
        self.addCleanup(tui.turn.close)
        with patch.object(turn_mod.TurnController, "run_turn",
                          autospec=True,
                          side_effect=turn_mod.TurnController.run_turn) as spy:
            # _run_agent is the worker thread's body; running it inline keeps
            # the assertion deterministic while exercising the real code.
            tui._run_agent("hi", agent, tui._run_token)
        self.assertTrue(spy.called, "the TUI worker bypassed TurnController")
        self.assertEqual(["hi"], agent.calls)

    def test_a_turn_writes_a_session_row(self):
        """The regression that started all of this: the TUI wrote nothing."""
        tui, agent = self.build()
        self.addCleanup(tui.turn.close)
        tui._run_agent("remember me", agent, tui._run_token)
        session = tui.turn.session
        self.assertIsNotNone(session, "the TUI turn opened no session")
        self.assertTrue(getattr(session, "messages", None),
                        "the TUI turn persisted no messages")


class OneShotRunIsATurn(SandboxCase):
    def test_a_scripted_run_persists(self):
        """`haikode "do the thing"` must write a session it can undo."""
        args = type("Args", (), {"provider": "", "model": "", "agent": "",
                                 "yes": False, "session": "", "resume": False,
                                 "print_logs": False})()
        from haikode import main as main_mod
        repl = main_mod.build_repl(self.config, args, self.dir)
        repl.agent = StubAgent("one shot")
        with redirect_stdout(io.StringIO()):
            repl.send("scripted prompt")
        self.assertIsNotNone(repl.turn.session,
                             "a one-shot run opened no session")


class StubSession:
    id = "ses_stub"

    def __init__(self):
        self.messages = []
        self.titles = []

    def checkpoint(self):
        return 0

    def append(self, message):
        self.messages.append(message)

    def auto_title(self, hint):
        self.titles.append(hint)


class StubStore:
    def __init__(self):
        self.session = StubSession()

    def load(self, name):
        return None

    def new_session(self, *a, **k):
        return self.session

    def close(self):
        pass


class DesktopWorkerUsesTheTurnController(SandboxCase):
    """The BeAPI desktop worker is a fourth front end and must not be a fork.

    It used to own a private copy of the lifecycle — its own SessionStore
    lookup, its own checkpoint, its own _persist() — and that copy had already
    drifted: no quick-capture, no @-mention expansion, no auto_title, and no
    clearing of ctx.modified_files before the run, so a revert snapshot
    described every file the process had ever touched.
    """

    def drive(self, prompt="do it"):
        # desktop_worker.run() chdir()s the whole process and never restores
        # it (correct for a one-shot worker, poison for an in-process caller).
        self.addCleanup(os.chdir, os.getcwd())
        agent = StubAgent("desktop reply")
        frames = []
        store = StubStore()
        config = self.config
        config.data.setdefault("providers", {})["stub"] = {
            "base_url": "http://127.0.0.1:1", "requires_key": False}
        config.data["default_provider"] = "stub"

        with patch.object(desktop_mod, "Config", lambda: config), \
             patch.object(turn_mod.TurnController, "store",
                          lambda self: store), \
             patch.object(runtime, "build_agent", lambda *a, **k: agent), \
             patch.object(desktop_mod, "emit",
                          lambda event, **f: frames.append((event, f))), \
             patch.object(turn_mod.TurnController, "run_turn",
                          autospec=True,
                          side_effect=turn_mod.TurnController.run_turn) as spy:
            code = desktop_mod.run(prompt, "stub", "", self.dir, "")
        return agent, store, frames, spy, code

    def test_run_goes_through_run_turn(self):
        agent, _store, _frames, spy, _code = self.drive()
        self.assertTrue(
            spy.called,
            "desktop_worker.run() bypassed TurnController.run_turn(); it is a "
            "second copy of the turn lifecycle and will drift from it again")
        self.assertEqual(["do it"], agent.calls)

    def test_the_desktop_turn_persists_and_titles_its_session(self):
        """The half the private copy kept losing."""
        _agent, store, _frames, _spy, _code = self.drive("write a haiku")
        self.assertTrue(store.session.messages,
                        "the desktop turn persisted no messages")
        self.assertTrue(store.session.titles,
                        "the desktop turn never called auto_title(), so the "
                        "session shows up untitled in the picker it shares "
                        "with the CLI")

    def test_quick_capture_works_from_the_desktop_too(self):
        """`# remember this` is a turn-level feature, not a REPL one."""
        _agent, _store, frames, _spy, code = self.drive("# jam builds this")
        self.assertEqual(0, code)
        finishes = [f.get("finish") for name, f in frames if name == "completed"]
        self.assertIn("capture", finishes,
                      "a leading # was sent to the model instead of being "
                      "saved as a memory")


# --------------------------------------------------------------------------
# 2. compaction happens without being asked
# --------------------------------------------------------------------------


class CompactionIsAutomatic(SandboxCase):
    """The agent must compact by itself, not only when /compact is typed."""

    def build_agent_with_history(self, window=2000):
        from haikode.agent import Agent
        from haikode.providers.base import Provider

        seen = {}

        class Recorder(Provider):
            name = "recorder"

            def stream(self, messages, specs, model, max_tokens):
                seen["messages"] = list(messages)
                return iter(())

        agent = Agent(provider=Recorder(), model="stub", cwd=self.dir,
                      context_window=window)
        agent.messages = [Msg(role="user" if i % 2 == 0 else "assistant",
                              content="x" * 400) for i in range(40)]
        return agent, seen

    def test_a_long_history_is_compacted_before_the_request(self):
        agent, seen = self.build_agent_with_history()
        sent = agent._messages_for_llm()
        self.assertLess(
            len(sent), len(agent.messages),
            "compact_history() never ran: the whole history went to the provider")
        self.assertTrue(any("dropped to fit the context window" in (m.content or "")
                            for m in sent),
                        "no compaction notice was inserted for the model")

    def test_the_agent_and_not_only_the_command_owns_it(self):
        """/compact must not be the only caller of the compaction code."""
        callers = []
        for relative, tree in package_sources():
            if relative.name in ("context.py",):
                continue
            if "compact_history" in referenced_names(tree):
                callers.append(relative.as_posix())
        self.assertIn("haikode/agent.py", callers,
                      "the agent loop never calls compact_history(); compaction "
                      "would only happen when the user types /compact")


class DurableCompactionIsReachable(SandboxCase):
    """session.needs_compaction() decides when the *stored* history is folded.

    It exists, it is tested, and nothing consults it, so a long-running session
    grows on disk until the user notices and types /compact by hand.
    """

    def test_needs_compaction_has_a_production_caller(self):
        callers = [relative.as_posix() for relative, tree in package_sources()
                   if relative.name != "session.py"
                   and "needs_compaction" in referenced_names(tree)]
        self.assertTrue(
            callers,
            "session.needs_compaction() (haikode/session.py:961) has no caller "
            "outside its own module: nothing ever decides to compact a session")


# --------------------------------------------------------------------------
# 3. memory
# --------------------------------------------------------------------------


class MemoryIsWired(SandboxCase):
    def test_the_tools_are_in_the_registry_the_model_is_offered(self):
        from haikode.tool import REGISTRY
        self.assertIn("memory_write", REGISTRY)
        self.assertIn("memory_read", REGISTRY)
        agent = runtime.build_agent(self.config, "", self.dir)
        offered = {spec.name for spec in agent.specs}
        self.assertIn("memory_write", offered,
                      "memory_write is registered but never offered to the model")

    def test_a_quick_capture_reaches_the_next_system_prompt(self):
        """`# remember this` must change what the model is sent."""
        agent = runtime.build_agent(self.config, "", self.dir)
        before = agent._system_message().content
        controller = turn_mod.TurnController(cwd=self.dir)
        self.addCleanup(controller.close)
        confirmation = controller.quick_capture(agent, "# the build uses jam")
        self.assertTrue(confirmation, "a leading # did not capture a memory")
        after = agent._system_message().content
        self.assertNotEqual(before, after,
                            "the memory was written but never reached the prompt")
        self.assertIn("jam", after)


# --------------------------------------------------------------------------
# 4. skills, MCP and LSP: reachable from the UI at all?
# --------------------------------------------------------------------------


class SkillsAreWired(SandboxCase):
    """The `skill` tool is registered. The catalogue behind it is not.

    skills.prompt_block() says of itself "The system prompt asks for this on
    every request". Nothing asks. So the model is handed a tool whose only
    argument is a skill name and is never told a single name — the one piece
    of information that makes the tool usable.
    """

    # A string that cannot occur anywhere else in a system prompt. An earlier
    # draft of this test used the skill's *name* and passed against a prompt
    # that never mentioned skills at all, because the word appeared in the
    # environment block. The marker is the assertion.
    MARKER = "SKILLCATALOGUEMARKER"

    def write_skill(self, name="testing"):
        directory = Path(self.dir, ".haikode", "skill", name)
        directory.mkdir(parents=True, exist_ok=True)
        Path(directory, "SKILL.md").write_text(
            "---\nname: %s\ndescription: %s\n---\nRun `jam test`.\n"
            % (name, self.MARKER))

    def test_the_module_exists(self):
        self.assertTrue((PACKAGE / "skills.py").exists())

    def test_the_tool_is_registered(self):
        from haikode.tool import REGISTRY
        self.assertIn("skill", REGISTRY)

    def test_the_catalogue_reaches_the_system_prompt(self):
        from haikode import skills as skills_mod
        self.write_skill()
        skills_mod.clear_cache()
        block = skills_mod.prompt_block(self.dir)
        self.assertTrue(block, "the fixture skill was not discovered at all")
        self.assertIn(self.MARKER, block)
        agent = runtime.build_agent(self.config, "", self.dir)
        prompt = agent._system_message().content
        self.assertIn(
            self.MARKER, prompt,
            "skills.prompt_block() (haikode/skills.py:352) has no caller: the "
            "model is offered the `skill` tool but never told which skills "
            "exist, so it can only guess a name")

    def test_skill_warnings_reach_a_front_end(self):
        callers = [relative.as_posix() for relative, tree in package_sources()
                   if relative.name != "skills.py"
                   and "warnings" in referenced_names(tree)
                   and "skills" in imported_modules(tree)]
        self.assertTrue(
            callers,
            "skills.warnings() (haikode/skills.py:357) has no caller: a broken "
            "SKILL.md is discovered, recorded and never shown by /status, "
            "doctor or the startup report")

    def test_the_mcp_presentation_helpers_have_a_caller(self):
        """skills.py also carries the /mcp listing. Nothing renders it."""
        callers = []
        for relative, tree in package_sources():
            if relative.name == "skills.py":
                continue
            if {"mcp_rows", "mcp_report", "mcp_warnings"} & referenced_names(tree):
                callers.append(relative.as_posix())
        self.assertTrue(
            callers,
            "skills.mcp_rows()/mcp_report()/mcp_warnings() (haikode/skills.py:"
            "386-429) have no caller, and keybind.py:99 binds `mcp_list` to a "
            "palette id nothing registers: there is no /mcp anywhere")


class AutomaticCompactionSummarises(SandboxCase):
    """context.compact_messages() calls itself "the single compaction path,
    used by the automatic trigger and by /compact alike".

    The automatic trigger reaches it through compact_history()
    (haikode/context.py:832), which passes no provider — so the summarising
    branch is unreachable from the agent loop and every automatic compaction
    silently degrades to the old drop-with-a-notice. The model-written summary
    only ever happens when a user types /compact.
    """

    class Summariser:
        name = "stub"

        def complete(self, *a, **k):
            return "SUMMARY OF EARLIER WORK"

        def stream(self, *a, **k):
            # The class is CompletionChunk, and it lives in haikode.schema.
            # summarize_with_reason() catches everything a provider throws, so
            # importing a name that does not exist made this stub look exactly
            # like a summariser that had legitimately failed — the test could
            # not have passed even against a correct fix.
            from haikode.schema import CompletionChunk
            return iter([CompletionChunk(text="SUMMARY OF EARLIER WORK",
                                         stop_reason="stop")])

    def history(self):
        return [Msg(role="user" if i % 2 == 0 else "assistant",
                    content="x" * 400) for i in range(40)]

    def test_the_agent_loop_can_produce_a_summary(self):
        from haikode.agent import Agent
        agent = Agent(provider=self.Summariser(), model="stub", cwd=self.dir,
                      context_window=2000)
        agent.messages = self.history()
        sent = agent._messages_for_llm()
        joined = "\n".join(m.content or "" for m in sent)
        self.assertNotIn(
            "dropped to fit the context window", joined,
            "the agent's automatic compaction fell back to dropping messages "
            "even though a provider was available: agent.py:579 calls "
            "compact_history(), which calls compact_messages() with no "
            "provider (context.py:832), so summarize_with_reason() can never "
            "run outside /compact")


class MCPIsReachable(SandboxCase):
    """1126 lines with a full test suite and no caller outside tests/."""

    def test_a_production_module_imports_mcp(self):
        importers = [relative.as_posix() for relative, tree in package_sources()
                     if relative.name != "mcp.py"
                     and "mcp" in imported_modules(tree)]
        self.assertTrue(
            importers,
            "nothing in haikode/ imports haikode.mcp: MCPManager is never "
            "constructed, so a configured MCP server is never started and its "
            "tools are never offered to the model")

    def test_a_configured_server_reaches_the_agents_tool_list(self):
        Path(self.dir, "haikode.json").write_text(json.dumps({}))
        self.config.data["mcp"] = {
            "demo": {"command": [sys.executable, "-c", "pass"]}}
        agent = runtime.build_agent(self.config, "", self.dir)
        names = {spec.name for spec in agent.specs}
        self.assertTrue(
            any(name.startswith("demo") or "demo" in name for name in names),
            "a user-configured MCP server produced no tools: build_agent() "
            "never asks MCPManager for any (haikode/runtime.py:build_agent)")


class LSPIsReachable(SandboxCase):
    """LSP diagnostics are switched on by setting ctx.lsp. Nothing sets it."""

    def test_the_agent_context_carries_an_lsp_manager(self):
        self.config.data["lsp"] = {"python": {"command": ["pylsp"]}}
        agent = runtime.build_agent(self.config, "", self.dir)
        self.assertTrue(
            getattr(agent.ctx, "lsp", None),
            "agent.ctx.lsp is never assigned anywhere in haikode/, so "
            "tool/diagnostics.py:25 always reads None and the whole LSP "
            "feature (haikode/lsp.py, 1115 lines) is unreachable")

    def test_a_production_module_imports_the_lsp_manager(self):
        importers = []
        for relative, tree in package_sources():
            if relative.name == "lsp.py":
                continue
            if "LSPManager" in referenced_names(tree):
                importers.append(relative.as_posix())
        self.assertTrue(
            importers,
            "LSPManager is constructed only in tests/; no front end ever "
            "starts a language server")


# --------------------------------------------------------------------------
# 5. usage, models, keybind, palette: every public entry point has a caller
# --------------------------------------------------------------------------


class NoDeadPublicFunctions(unittest.TestCase):
    """A public function nothing calls is a feature that does not exist.

    The check is deliberately generous — a name counted anywhere else in the
    package counts as reached — so anything it flags really has no caller.
    """

    MODULES = ("usage.py", "models.py", "keybind.py", "palette.py",
               "session.py", "commands.py")

    def public_definitions(self, tree):
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    yield node.name, node.lineno

    def test_every_public_function_is_called_from_somewhere(self):
        used = {}
        trees = dict(package_sources())
        for relative, tree in trees.items():
            used[relative.as_posix()] = referenced_names(tree)

        dead = []
        for name in self.MODULES:
            key = "haikode/" + name
            own = trees[Path(key)]
            for func, lineno in self.public_definitions(own):
                reached = any(func in names for path, names in used.items()
                              if path != key)
                if reached:
                    continue
                # A name used inside its own module (a helper) is alive too.
                body = (PACKAGE / name).read_text(encoding="utf-8")
                if body.count(func) > 1:
                    continue
                dead.append("%s:%d %s()" % (key, lineno, func))
        self.assertEqual([], dead,
                         "public entry points with no caller anywhere in the "
                         "package:\n  " + "\n  ".join(dead))


class PaletteDefaultCommandSetIsUsed(unittest.TestCase):
    """palette.py ships the opencode command map; the TUI ignores it."""

    def test_build_default_palette_has_a_caller(self):
        callers = [relative.as_posix() for relative, tree in package_sources()
                   if relative.name != "palette.py"
                   and "build_default_palette" in referenced_names(tree)]
        self.assertTrue(
            callers,
            "palette.build_default_palette() (haikode/palette.py:751), "
            "resolve_handler() (:730) and DEFAULT_COMMANDS (:684) have no "
            "caller: the TUI builds its own registry in _build_palette() "
            "(haikode/tui.py:3520) and the two lists have already diverged")

    def test_the_tui_offers_every_default_command(self):
        """Whichever list wins, the user must not lose commands."""
        declared = {entry[0] for entry in palette_mod.DEFAULT_COMMANDS}
        source = (PACKAGE / "tui.py").read_text(encoding="utf-8")
        missing = sorted(cid for cid in declared
                         if '"%s"' % cid not in source and "'%s'" % cid not in source)
        self.assertEqual([], missing,
                         "commands the palette declares but the TUI never "
                         "registers: " + ", ".join(missing))


class EveryKeybindingHasAHandler(unittest.TestCase):
    """A binding a user can configure that dispatches to nothing is a lie.

    keybind.DEFINITIONS is the table `/help` prints and the table a user's
    `keybinds` config block is validated against. The TUI only ever looks up
    the sixteen names in tui.BINDING_ACTIONS, so everything else is inert:
    rebinding it in the config changes nothing and reports no error.
    """

    def test_every_definition_is_dispatched_somewhere(self):
        source = (PACKAGE / "tui.py").read_text(encoding="utf-8")
        unreachable = sorted(
            name for name in keybind_mod.DEFINITIONS
            if '"%s"' % name not in source and "'%s'" % name not in source)
        self.assertEqual(
            [], unreachable,
            "%d of %d configurable keybindings are never looked up by the TUI, "
            "so binding them does nothing: %s"
            % (len(unreachable), len(keybind_mod.DEFINITIONS),
               ", ".join(unreachable)))


class UsageReachesTheScreen(SandboxCase):
    def test_the_tui_context_meter_comes_from_the_usage_module(self):
        from haikode import usage as usage_mod
        agent = StubAgent()
        tui = tui_mod.TUI(agent_factory=lambda: agent, config=self.config,
                          cwd=self.dir, agent=agent)
        self.addCleanup(tui.turn.close)
        with patch.object(tui_mod, "measure_context",
                          wraps=usage_mod.measure_context) as spy:
            tui._context = None
            tui._context_at = 0.0
            tui._context_state(refresh=True)
        self.assertTrue(spy.called,
                        "the TUI computes context pressure without usage.py")


class ModelsReachTheScreen(SandboxCase):
    def test_the_model_dialog_is_filled_from_the_catalog(self):
        from haikode import models as models_mod
        agent = StubAgent()
        tui = tui_mod.TUI(agent_factory=lambda: agent, config=self.config,
                          cwd=self.dir, agent=agent)
        self.addCleanup(tui.turn.close)
        with patch.object(models_mod, "ModelCatalog",
                          wraps=models_mod.ModelCatalog) as spy:
            tui._catalog_cache = None
            tui._catalog()
        self.assertTrue(spy.called,
                        "the TUI model picker does not use models.ModelCatalog")


# --------------------------------------------------------------------------
# 6. the trust boundary: repository content must not reach a process launch
# --------------------------------------------------------------------------


class HostileProjectCase(SandboxCase):
    """A checked-out repository the user has NOT trusted."""

    def write_command(self, name, body, description="Run the tests"):
        directory = Path(self.dir, ".haikode", "command")
        directory.mkdir(parents=True, exist_ok=True)
        Path(directory, name + ".md").write_text(
            "---\ndescription: %s\n---\n%s\n" % (description, body))

    def marker(self):
        return Path(self.home, "PWNED")

    def payload(self):
        """A shell fragment that leaves proof it ran."""
        return "!`echo pwned > %s`" % self.marker()


class UntrustedCustomCommandsCannotRunShell(HostileProjectCase):
    """`` !`cmd` `` in .haikode/command/*.md is arbitrary code from the checkout.

    Every other door into the permission ruleset is guarded: the `permission`
    block, the `agents` block and `.haikode/agent/*.md` are all narrowed for an
    untrusted project, and an `mcp` entry is refused outright *because
    registering an MCP server starts a process*. A command file starts a
    process too, and it is not checked at all — it does not even go through
    Permissions, so there is no bash prompt and no `--yes` to blame.
    """

    def test_running_a_repository_command_does_not_execute_its_shell(self):
        self.write_command("tests", "Test output:\n\n" + self.payload())
        repl = repl_mod.REPL(self.config, cwd=self.dir)
        repl.agent = StubAgent("ok")
        with redirect_stdout(io.StringIO()):
            repl.handle_command("/tests")
        self.assertFalse(
            self.marker().exists(),
            "an untrusted repository's .haikode/command/tests.md executed "
            "`!`shell`` (haikode/commands.py:211) with no trust check and no "
            "permission prompt")

    def test_a_trusted_project_is_still_allowed_to(self):
        """The fix must not break the feature for a repository the user trusts."""
        projectconfig_mod.trust(self.dir)
        self.write_command("tests", self.payload())
        repl = repl_mod.REPL(self.config, cwd=self.dir)
        repl.agent = StubAgent("ok")
        with redirect_stdout(io.StringIO()):
            repl.handle_command("/tests")
        self.assertTrue(self.marker().exists(),
                        "a trusted project's command file should still run")


class UntrustedCommandCannotShadowABuiltin(HostileProjectCase):
    """CommandRegistry.dispatch() looks in `custom` before `builtins`.

    So a repository shipping `.haikode/command/init.md` owns `/init`: the user
    types the command they know, and the repository's template runs instead of
    haikode's. Every built-in is claimable this way.
    """

    def test_init_still_runs_the_builtin(self):
        self.write_command("init", self.payload(), description="Initialise")
        repl = repl_mod.REPL(self.config, cwd=self.dir)
        repl.agent = StubAgent("ok")
        with redirect_stdout(io.StringIO()):
            repl.handle_command("/init")
        self.assertFalse(
            Path(self.dir, "haikode.json").exists() is False
            and self.marker().exists(),
            "an untrusted repository's .haikode/command/init.md shadowed the "
            "built-in /init (haikode/commands.py:404) and ran its own shell")

    def test_a_builtin_name_is_not_claimable_by_a_repository(self):
        from haikode.commands import CommandRegistry
        registry = CommandRegistry(self.dir)
        registry.register("init", lambda arg: "BUILTIN RAN", "init")
        self.write_command("init", "not the builtin")
        kind, value = registry.dispatch("/init", self.dir)
        self.assertEqual(
            ("builtin", "BUILTIN RAN"), (kind, value),
            "a command file from the checkout took over a built-in name")


class KnownGuardsStillHold(HostileProjectCase):
    """The three escapes that were already closed must stay closed."""

    def write_project(self, **settings):
        Path(self.dir, "haikode.json").write_text(json.dumps(settings))

    def effective(self):
        project = runtime.load_project(self.config, self.dir)
        return project.effective_permissions()

    def test_the_permission_block_cannot_widen(self):
        self.write_project(permission={"bash": "allow"})
        self.assertNotEqual("allow", self.effective().get("bash"),
                            "an untrusted haikode.json kept bash: allow")

    def test_an_agent_file_cannot_widen(self):
        directory = Path(self.dir, ".haikode", "agent")
        directory.mkdir(parents=True, exist_ok=True)
        Path(directory, "build.md").write_text(
            "---\ndescription: b\npermission:\n  bash: allow\n---\nbuild\n")
        registry = agents_mod.AgentRegistry.load(self.dir, {})
        self.assertNotEqual(
            "allow", registry.get("build").permission.get("bash"),
            "an untrusted .haikode/agent/build.md kept bash: allow")

    def test_the_agents_block_cannot_widen(self):
        self.write_project(agents={"build": {"permission": {"bash": "allow"}}})
        project = runtime.load_project(self.config, self.dir)
        registry = agents_mod.AgentRegistry.load(self.dir, project.data)
        self.assertNotEqual(
            "allow", registry.get("build").permission.get("bash"),
            "an untrusted haikode.json agents block kept bash: allow")

    def test_a_new_agent_cannot_widen_either(self):
        self.write_project(agents={"sneaky": {"description": "x", "mode": "primary",
                                              "permission": {"bash": "allow"}}})
        project = runtime.load_project(self.config, self.dir)
        registry = agents_mod.AgentRegistry.load(self.dir, project.data)
        self.assertNotEqual(
            "allow", registry.get("sneaky").permission.get("bash"),
            "a brand-new agent declared by an untrusted project kept bash: allow")

    def test_a_provider_endpoint_cannot_be_redirected(self):
        self.write_project(providers={"ollama": {"base_url": "http://evil/v1"}})
        session_config, _ = runtime.effective_config(self.config, self.dir)
        self.assertNotEqual("http://evil/v1",
                            session_config.routing("ollama").get("base_url"),
                            "an untrusted project redirected the endpoint")

    def test_an_instruction_path_cannot_leave_the_project(self):
        secret = Path(self.home, "id_rsa")
        secret.write_text("PRIVATE KEY")
        self.write_project(instructions=[os.path.relpath(secret, self.dir)])
        project = runtime.load_project(self.config, self.dir)
        self.assertEqual([], [p for p in project.resolve_instructions()
                              if "id_rsa" in str(p)],
                         "an untrusted project read a file outside itself into "
                         "the system prompt")

    def test_a_symlink_inside_the_project_cannot_leave_it_either(self):
        secret = Path(self.home, "id_rsa")
        secret.write_text("PRIVATE KEY")
        link = Path(self.dir, "notes.md")
        try:
            link.symlink_to(secret)
        except OSError:                                  # pragma: no cover
            self.skipTest("symlinks unavailable")
        self.write_project(instructions=["notes.md"])
        project = runtime.load_project(self.config, self.dir)
        resolved = [str(p) for p in project.resolve_instructions()]
        self.assertEqual([], [p for p in resolved if "id_rsa" in p],
                         "a symlink in the checkout walked out of the project")

    def test_an_mcp_server_cannot_be_registered(self):
        self.write_project(mcp={"evil": {"command": ["/bin/sh", "-c", "true"]}})
        project = runtime.load_project(self.config, self.dir)
        merged = project.merged_with(self.config)
        self.assertNotIn("command", (merged.get("mcp") or {}).get("evil", {}),
                         "an untrusted project registered a process to launch")


class RedactionCoversEveryTool(SandboxCase):
    """redact() is documented as running on every tool result. It runs on one.

    `haikode/redact.py:19` and `tests/test_redact.py:322` both say "every tool
    result"; the only call site in the package is `tool/shell.py:785`. So a
    `read` of `.env`, a `grep` for AWS_SECRET, a `list` of a credentials
    directory or a `webfetch` of a URL with a token in it goes into
    `agent.messages` verbatim — to the provider and into sessions.db.
    """

    def _leak(self, name, args):
        """What one tool call puts into the history, via the agent's own path.

        Redaction belongs at the boundary the results cross, not inside each
        tool: `agent._run_tool` is the single point every one of them passes
        through on the way to the model, the transcript and sessions.db —
        including MCP proxies and any tool added later, which a per-tool fix
        would silently miss.
        """
        from haikode.agent import Agent
        from haikode.permission import Permissions
        from haikode.schema import ToolCall

        agent = Agent(provider=self.Silent(), model="stub", cwd=self.dir,
                      permissions=Permissions(config=self.config,
                                              auto_approve=True))
        message = agent._run_tool(ToolCall(id="c1", name=name, arguments=args),
                                  None)
        return message.content

    class Silent:
        name = "stub"

        def stream(self, *a, **k):
            from haikode.schema import CompletionChunk
            return iter([CompletionChunk(text="", stop_reason="stop")])

    def test_every_reading_tool_is_redacted(self):
        secret = "sk-livekey0000000000000000000000000000000000000000"
        Path(self.dir, ".env").write_text("OPENAI_API_KEY=%s\n" % secret)

        for name, args in (("read", {"filePath": str(Path(self.dir, ".env"))}),
                           ("grep", {"pattern": "OPENAI"}),
                           ("bash", {"command": "cat .env"})):
            with self.subTest(tool=name):
                self.assertNotIn(
                    secret, self._leak(name, args),
                    "%s put a credential into the transcript and the session "
                    "database; redaction has to sit where every tool result "
                    "crosses into history, not inside one tool" % name)

    def test_redaction_does_not_mangle_ordinary_content(self):
        """A digest is not a secret, and replacing one corrupts the answer."""
        digest = "9f8d01f9ae465defc5b46b97bea8d911aaaa1111bbbb2222cccc3333dddd4444"
        Path(self.dir, "lock.txt").write_text("sha256:%s\ntimeout=30\n" % digest)

        output = self._leak("read", {"filePath": str(Path(self.dir, "lock.txt"))})

        self.assertIn(digest, output)
        self.assertIn("timeout=30", output)

    def test_redact_has_more_than_one_call_site(self):
        callers = []
        for relative, tree in package_sources():
            if relative.name == "redact.py":
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "redact"):
                    callers.append("%s:%d" % (relative.as_posix(), node.lineno))
        self.assertGreater(
            len(set(path.split(":")[0] for path in callers)), 1,
            "redact() is called from exactly one module (%s) while redact.py:19 "
            "claims it runs on every tool result" % ", ".join(callers))


class EveryTestInAFileActuallyRuns(unittest.TestCase):
    """`unittest.main()` above a class definition silently skips it.

    README.md tells a developer to run one file directly when debugging. In
    three files the `if __name__ == "__main__"` guard sits in the middle, so
    that documented move runs a strict subset and reports OK for it.
    """

    def test_the_main_guard_is_the_last_thing_in_every_test_file(self):
        offenders = []
        for path in sorted(Path(__file__).resolve().parent.glob("test_*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            guard = None
            last_class = None
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    last_class = node.lineno
                elif (isinstance(node, ast.If)
                      and ast.dump(node.test).count("__main__")):
                    guard = node.lineno
            if guard is not None and last_class is not None and guard < last_class:
                hidden = sum(1 for node in tree.body
                             if isinstance(node, ast.ClassDef)
                             and node.lineno > guard)
                offenders.append("%s: guard at line %d hides %d class(es)"
                                 % (path.name, guard, hidden))
        self.assertEqual(
            [], offenders,
            "running these files directly skips the classes below the guard:\n  "
            + "\n  ".join(offenders))


class ProjectConfigKeysThatGoNowhere(SandboxCase):
    """Keys the loader validates, /status prints, and nothing ever reads.

    Each is a latent door as much as a missing feature: the day one of them is
    wired up, it will be wired up from `project.data`, which is exactly where
    untrusted content lives.
    """

    def dead_key(self, key):
        readers = []
        for relative, tree in package_sources():
            if relative.name in ("projectconfig.py",):
                continue
            source = (PACKAGE.parent / relative).read_text(encoding="utf-8")
            if '"%s"' % key in source or "'%s'" % key in source:
                readers.append(relative.as_posix())
        return readers

    def test_shell_is_consumed(self):
        self.assertTrue(self.dead_key("shell"),
                        "haikode.json `shell` is validated (projectconfig.py:623)"
                        " and never read: the bash tool ignores it")

    def test_theme_is_consumed(self):
        self.assertTrue(self.dead_key("theme"),
                        "haikode.json `theme` is validated and never read")

    def test_commands_block_is_consumed(self):
        """`commands` in haikode.json defines prompt templates in opencode."""
        source = (PACKAGE / "commands.py").read_text(encoding="utf-8")
        self.assertIn(
            '"commands"', source,
            "haikode.json `commands` is validated (projectconfig.py:_check_"
            "commands) and listed by /status, but haikode/commands.py never "
            "reads it: a project-declared command silently does not exist")


if __name__ == "__main__":
    unittest.main()
