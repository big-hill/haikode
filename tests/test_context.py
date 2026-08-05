import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from haikode import context as context_module
from haikode.context import (INSTRUCTION_FILES, MAX_INSTRUCTION_CHARS,
                             ContextManager, clear_summary_cache,
                             compact_history, compact_messages, estimate_tokens,
                             summarize, summarize_with_reason)
from haikode.schema import CompletionChunk, Msg, ToolCall


@contextmanager
def isolated(global_dir: Path, home: Path):
    """Point the two global lookups at a throwaway tree.

    Without this the developer's real ~/.claude/CLAUDE.md leaks into the
    assertions.
    """
    with mock.patch.object(context_module, "global_config_dir",
                           lambda: Path(global_dir)), \
         mock.patch.object(context_module, "home_dir", lambda: Path(home)):
        yield


@contextmanager
def project(*, nested: str = "sub") -> Path:
    """A temp repo: <tmp>/repo/.git plus <tmp>/repo/<nested>."""
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory).resolve()
        repo = base / "repo"
        (repo / nested).mkdir(parents=True)
        (repo / ".git").mkdir()
        (base / "globals").mkdir()
        (base / "home").mkdir()
        yield base


class InstructionDiscoveryTest(unittest.TestCase):
    def test_the_winning_name_is_taken_from_every_ancestor_nearest_first(self):
        # findUp() returns the whole chain and instruction.ts adds all of it,
        # so a monorepo keeps its root AGENTS.md next to the package one.
        with project() as base:
            repo = base / "repo"
            (repo / "AGENTS.md").write_text("parent rules\n")
            (repo / "sub" / "AGENTS.md").write_text("nested rules\n")
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(repo / "sub")).instructions()
                files = ContextManager(str(repo / "sub")).instruction_files()

        self.assertLess(text.index("nested rules"), text.index("parent rules"))
        self.assertEqual(files, [(repo / "sub" / "AGENTS.md").resolve(),
                                 (repo / "AGENTS.md").resolve()])

    def test_without_a_repository_the_walk_does_not_leave_the_directory(self):
        # opencode's worktree falls back to the working directory, so a
        # stray AGENTS.md in a parent (think /tmp) must stay invisible.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            (base / "work").mkdir()
            (base / "AGENTS.md").write_text("someone else's rules\n")
            (base / "globals").mkdir()
            (base / "home").mkdir()
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(base / "work")).instructions()

        self.assertEqual(text, "")

    def test_the_name_order_decides_which_project_file_is_used(self):
        # opencode iterates names first, so AGENTS.md anywhere up the tree
        # beats a CLAUDE.md in the working directory.
        with project() as base:
            repo = base / "repo"
            (repo / "AGENTS.md").write_text("agents rules\n")
            (repo / "sub" / "CLAUDE.md").write_text("claude rules\n")
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(repo / "sub")).instructions()

        self.assertIn("agents rules", text)
        self.assertNotIn("claude rules", text)

    def test_haikode_md_is_still_honoured(self):
        with project() as base:
            repo = base / "repo"
            (repo / "sub" / "HAIKODE.md").write_text("haiku rules\n")
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(repo / "sub")).instructions()

        self.assertIn("haiku rules", text)
        self.assertIn("HAIKODE.md", text)

    def test_the_search_stops_at_the_repository_root(self):
        with project() as base:
            repo = base / "repo"
            (base / "AGENTS.md").write_text("outside the repo\n")
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(repo / "sub")).instructions()

        self.assertEqual(text, "")

    def test_the_global_agents_file_is_included_first(self):
        with project() as base:
            repo = base / "repo"
            (base / "globals").mkdir(exist_ok=True)
            (base / "globals" / "AGENTS.md").write_text("global rules\n")
            (repo / "AGENTS.md").write_text("project rules\n")
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(repo)).instructions()
                files = ContextManager(str(repo)).instruction_files()

        self.assertLess(text.index("global rules"), text.index("project rules"))
        self.assertEqual(files[0], (base / "globals" / "AGENTS.md").resolve())

    def test_claude_code_global_file_is_used_when_there_is_no_global_agents(self):
        with project() as base:
            claude = base / "home" / ".claude"
            claude.mkdir(parents=True)
            (claude / "CLAUDE.md").write_text("claude code global\n")
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(base / "repo")).instructions()

        self.assertIn("claude code global", text)

    def test_only_one_global_file_is_loaded(self):
        # instruction.ts breaks after the first existing global candidate.
        with project() as base:
            (base / "globals" / "AGENTS.md").write_text("global rules\n")
            claude = base / "home" / ".claude"
            claude.mkdir(parents=True)
            (claude / "CLAUDE.md").write_text("claude code global\n")
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(base / "repo")).instructions()

        self.assertIn("global rules", text)
        self.assertNotIn("claude code global", text)


class ExtraPathsTest(unittest.TestCase):
    def test_extra_paths_are_appended_after_the_discovered_files(self):
        with project() as base:
            repo = base / "repo"
            (repo / "AGENTS.md").write_text("project rules\n")
            (repo / "docs").mkdir()
            (repo / "docs" / "style.md").write_text("style rules\n")
            with isolated(base / "globals", base / "home"):
                manager = ContextManager(str(repo))
                text = manager.instructions(["docs/style.md"])
                files = manager.instruction_files(["docs/style.md"])

        self.assertLess(text.index("project rules"), text.index("style rules"))
        self.assertEqual([p.name for p in files], ["AGENTS.md", "style.md"])

    def test_extra_paths_are_deduplicated_by_resolved_path(self):
        with project() as base:
            repo = base / "repo"
            (repo / "AGENTS.md").write_text("project rules\n")
            with isolated(base / "globals", base / "home"):
                manager = ContextManager(str(repo))
                files = manager.instruction_files(
                    ["AGENTS.md", str(repo / "AGENTS.md"), "./AGENTS.md"])
                text = manager.instructions(["AGENTS.md"])

        self.assertEqual(files, [(repo / "AGENTS.md").resolve()])
        self.assertEqual(text.count("project rules"), 1)

    def test_extra_paths_expand_globs_and_home(self):
        with project() as base:
            repo = base / "repo"
            rules = repo / "rules"
            rules.mkdir()
            (rules / "a.md").write_text("rule a\n")
            (rules / "b.md").write_text("rule b\n")
            (base / "home" / "personal.md").write_text("personal rule\n")
            with isolated(base / "globals", base / "home"), \
                    mock.patch.dict(os.environ, {"HOME": str(base / "home")}):
                text = ContextManager(str(repo)).instructions(
                    ["rules/*.md", "~/personal.md"])

        self.assertIn("rule a", text)
        self.assertIn("rule b", text)
        self.assertIn("personal rule", text)

    def test_urls_are_skipped_with_a_note_instead_of_being_fetched(self):
        with project() as base:
            with isolated(base / "globals", base / "home"):
                manager = ContextManager(str(base / "repo"))
                files = manager.instruction_files(["https://example.com/rules.md"])
                text = manager.instructions(["https://example.com/rules.md"])

        self.assertEqual(files, [])
        self.assertIn("https://example.com/rules.md", text)
        self.assertIn("not fetched", text)

    def test_a_relative_entry_cannot_escape_the_worktree(self):
        # A config that travels with a checked-out repository is untrusted.
        with project() as base:
            (base / "secret.md").write_text("private notes\n")
            with isolated(base / "globals", base / "home"):
                manager = ContextManager(str(base / "repo" / "sub"))
                files = manager.instruction_files(
                    ["../../secret.md", "../../*.md"])
                text = manager.instructions(["../../secret.md"])

        self.assertEqual(files, [])
        self.assertNotIn("private notes", text)

    def test_a_relative_entry_is_searched_up_to_the_worktree_root(self):
        # opencode globs the entry in every directory from cwd to the worktree.
        with project() as base:
            repo = base / "repo"
            (repo / "shared.md").write_text("shared rules\n")
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(repo / "sub")).instructions(["shared.md"])

        self.assertIn("shared rules", text)

    def test_an_absolute_entry_does_not_recurse_into_the_whole_disk(self):
        with project() as base:
            repo = base / "repo"
            deep = repo / "a" / "b"
            deep.mkdir(parents=True)
            (repo / "a" / "top.md").write_text("top rule\n")
            (deep / "deep.md").write_text("deep rule\n")
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(repo)).instructions(
                    [str(repo / "a" / "**" / "*.md")])

        # "**" degrades to a single level for absolute entries, as in
        # instruction.ts, which globs the basename inside its own directory.
        self.assertIn("deep rule", text)
        self.assertNotIn("top rule", text)

    def test_a_huge_instruction_file_is_not_read_whole_into_memory(self):
        with project() as base:
            repo = base / "repo"
            (repo / "AGENTS.md").write_text("y" * (MAX_INSTRUCTION_CHARS * 4))
            self.assertEqual(len(context_module._read(repo / "AGENTS.md")),
                             context_module.MAX_INSTRUCTION_FILE_CHARS)
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(repo)).instructions()
        self.assertLessEqual(len(text), MAX_INSTRUCTION_CHARS)

    def test_instruction_files_are_decoded_as_utf8(self):
        with project() as base:
            repo = base / "repo"
            (repo / "AGENTS.md").write_bytes("bruk rå tegn: æøå\n".encode("utf-8"))
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(repo)).instructions()
        self.assertIn("æøå", text)

    def test_missing_extra_paths_are_ignored(self):
        with project() as base:
            with isolated(base / "globals", base / "home"):
                manager = ContextManager(str(base / "repo"))
                self.assertEqual(manager.instruction_files(["nope.md"]), [])
                self.assertEqual(manager.instructions(["nope.md"]), "")


class TruncationTest(unittest.TestCase):
    def test_oversized_instructions_end_with_an_explicit_marker(self):
        with project() as base:
            repo = base / "repo"
            (repo / "AGENTS.md").write_text("x" * (MAX_INSTRUCTION_CHARS * 2))
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(repo)).instructions()

        self.assertLessEqual(len(text), MAX_INSTRUCTION_CHARS)
        self.assertIn("truncated at", text)
        self.assertTrue(text.rstrip().endswith("]"))

    def test_instructions_under_the_cap_are_left_alone(self):
        with project() as base:
            repo = base / "repo"
            (repo / "AGENTS.md").write_text("short rules\n")
            with isolated(base / "globals", base / "home"):
                text = ContextManager(str(repo)).instructions()

        self.assertNotIn("truncated", text)
        self.assertTrue(text.endswith("short rules"))


class EnvironmentBlockTest(unittest.TestCase):
    def test_a_missing_git_binary_does_not_break_the_block(self):
        with project() as base:
            manager = ContextManager(str(base / "repo"))
            with mock.patch.object(context_module.subprocess, "run",
                                   side_effect=FileNotFoundError("git")):
                block = manager.environment_block()
                self.assertEqual(manager._git_branch(), "")

        self.assertIn("# Environment", block)
        self.assertNotIn("Current branch", block)

    def test_a_hanging_git_is_bounded_by_a_timeout(self):
        calls = {}

        def fake_run(cmd, **kwargs):
            calls.update(kwargs)
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

        with project() as base:
            manager = ContextManager(str(base / "repo"))
            with mock.patch.object(context_module.subprocess, "run", fake_run):
                self.assertEqual(manager._git_branch(), "")

        self.assertTrue(0 < calls["timeout"] <= 10)

    def test_the_block_reports_the_worktree_and_the_date(self):
        with project() as base:
            manager = ContextManager(str(base / "repo" / "sub"))
            with mock.patch.object(context_module.subprocess, "run",
                                   side_effect=FileNotFoundError("git")):
                block = manager.environment_block()

            self.assertIn(f"Workspace root folder: {base / 'repo'}", block)
        # a subdirectory of a repository is still inside the repository
        self.assertIn("Is a git repository: yes", block)
        self.assertIn("Today's date:", block)

    def test_a_directory_outside_a_repository_reports_no(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = ContextManager(directory)
            with mock.patch.object(context_module.subprocess, "run",
                                   side_effect=FileNotFoundError("git")):
                block = manager.environment_block()
        self.assertIn("Is a git repository: no", block)

    def test_project_tree_lists_files_and_skips_noise(self):
        with project() as base:
            repo = base / "repo"
            (repo / "main.py").write_text("x")
            (repo / ".hidden").write_text("x")
            (repo / "__pycache__").mkdir()
            (repo / "__pycache__" / "main.pyc").write_text("x")
            tree = ContextManager(str(repo)).project_tree()

        self.assertIn("main.py", tree)
        self.assertNotIn(".hidden", tree)
        self.assertNotIn("main.pyc", tree)


class UnchangedHelpersTest(unittest.TestCase):
    def test_instruction_file_names_are_stable(self):
        self.assertEqual(INSTRUCTION_FILES,
                         ("AGENTS.md", "CLAUDE.md", "HAIKODE.md"))

    def test_compaction_keeps_the_tail_and_drops_a_notice_in_front(self):
        messages = [Msg(role="user", content="x" * 4000) for _ in range(12)]
        kept = compact_history(messages, window=2000)
        self.assertLess(len(kept), len(messages))
        self.assertIn("dropped", kept[0].content)

    def test_token_estimate_is_never_zero(self):
        self.assertEqual(estimate_tokens(""), 1)


class _StubProvider:
    """A provider that answers exactly one summarising round.

    `fail` reproduces the three ways the real ones fail: raising out of the
    generator, ending the stream with an error chunk, and saying nothing.
    """

    def __init__(self, text: str = "## Objective\n- port the parser",
                 fail: str = ""):
        self.text = text
        self.fail = fail
        self.requests = []

    def stream(self, messages, tools, model, max_tokens):
        self.requests.append(list(messages))
        if self.fail == "raise":
            raise OSError("connection reset by peer")
        if self.fail == "error":
            yield CompletionChunk(text="rate limited", stop_reason="error")
            return
        if self.fail == "empty":
            return
        half = len(self.text) // 2
        yield CompletionChunk(text=self.text[:half])
        yield CompletionChunk(text=self.text[half:])


def _long(text: str) -> str:
    """Padded so a handful of messages already overflow a small window."""
    return text + " " + "x" * 4000


class CompactionTest(unittest.TestCase):
    """The model-written compaction that replaced dropping the past."""

    def setUp(self):
        # Process-wide and content-keyed, so one test's summary would
        # otherwise be handed to the next one's identical history.
        clear_summary_cache()
        self.addCleanup(clear_summary_cache)

    def assert_no_orphan_tool_messages(self, messages):
        seen = set()
        for message in messages:
            for call in message.tool_calls:
                seen.add(call.id)
            if message.role == "tool":
                self.assertIn(message.tool_call_id, seen,
                              "tool result %r lost its call" % message.tool_call_id)

    def tool_history(self):
        return [
            Msg(role="user", content=_long("port the parser")),
            Msg(role="assistant", content=_long("reading"),
                tool_calls=[ToolCall(id="c1", name="read",
                                     arguments={"filePath": "/p/a.py"})]),
            Msg(role="tool", content=_long("file body"), tool_call_id="c1",
                display={"tool": "read"}),
            Msg(role="assistant", content=_long("we will keep the old parser")),
            Msg(role="user", content=_long("now edit it")),
            Msg(role="assistant", content=_long("editing"),
                tool_calls=[ToolCall(id="c2", name="edit",
                                     arguments={"filePath": "/p/a.py"})]),
            Msg(role="tool", content=_long("edited"), tool_call_id="c2",
                display={"tool": "edit"}),
            Msg(role="assistant", content=_long("done")),
        ]

    def test_the_model_summary_replaces_the_folded_turns(self):
        provider = _StubProvider("## Objective\n- port the parser to Haiku")
        result = compact_messages(self.tool_history(), window=2000,
                                  provider=provider, model="test-model",
                                  keep_last=2)

        self.assertTrue(result.summarized)
        self.assertEqual(result.error, "")
        self.assertEqual(result.folded, 5)
        self.assertEqual(result.summary, "## Objective\n- port the parser to Haiku")
        self.assertEqual(result.notice(), "Compacted 5 messages into a summary.")
        # The summary is in the history the provider will actually see, and it
        # is marked so a front end can render it as a summary. The recovery
        # footer rides along, so the model knows session_history un-folds it.
        self.assertTrue(result.messages[0].content.startswith(result.summary))
        self.assertIn("session_history", result.messages[0].content)
        self.assertTrue(result.messages[0].display.get("summary"))
        self.assertEqual(result.messages[0].display.get("folded"), 5)
        self.assertNotIn("port the parser to Haiku",
                         [m.content for m in result.messages[1:]])

    def test_compaction_never_splits_a_tool_call_from_its_result(self):
        provider = _StubProvider()
        # keep_last=2 cuts between the edit call and its result; the fold has
        # to reach back for the assistant message or every later request 400s.
        result = compact_messages(self.tool_history(), window=2000,
                                  provider=provider, model="m", keep_last=2)

        self.assert_no_orphan_tool_messages(result.messages)
        self.assertEqual([m.role for m in result.messages],
                         ["user", "assistant", "tool", "assistant"])
        self.assertEqual(result.messages[1].tool_calls[0].id, "c2")
        self.assertEqual(result.messages[2].tool_call_id, "c2")

    def test_system_and_pinned_messages_are_never_folded(self):
        history = [
            Msg(role="system", content=_long("you are haikode")),
            Msg(role="user", content=_long("never touch vendor/"),
                display={"pinned": True}),
            Msg(role="user", content=_long("start work")),
            Msg(role="assistant", content=_long("working")),
            Msg(role="user", content=_long("keep going")),
            Msg(role="assistant", content=_long("still working")),
        ]
        result = compact_messages(history, window=2000, provider=_StubProvider(),
                                  model="m", keep_last=2)

        self.assertEqual(result.folded, 2)
        contents = [m.content for m in result.messages]
        self.assertEqual(contents[0], history[0].content)
        self.assertEqual(contents[1], history[1].content)
        # The summary lands where the folded turns were: after the pinned
        # constraint, before the tail it describes the run-up to.
        self.assertTrue(result.messages[2].display.get("summary"))
        self.assertEqual(contents[3:], [history[4].content, history[5].content])

    def test_pinning_a_tool_result_keeps_its_call_too(self):
        history = self.tool_history()
        history[2].display["pinned"] = True
        result = compact_messages(history, window=2000, provider=_StubProvider(),
                                  model="m", keep_last=2)

        self.assert_no_orphan_tool_messages(result.messages)
        self.assertIn(history[1].content, [m.content for m in result.messages])
        self.assertIn(history[2].content, [m.content for m in result.messages])

    def test_a_failing_summariser_degrades_to_the_old_notice(self):
        for mode, expected in (("raise", "OSError"), ("error", "rate limited"),
                               ("empty", "empty summary")):
            with self.subTest(mode=mode):
                # Same messages every round, so the cached first failure would
                # otherwise answer for the other two.
                clear_summary_cache()
                result = compact_messages(
                    self.tool_history(), window=2000, keep_last=2,
                    provider=_StubProvider(fail=mode), model="m")

                self.assertFalse(result.summarized)
                self.assertIn(expected, result.error)
                self.assertEqual(result.folded, 5)
                self.assertIn("dropped", result.messages[0].content)
                self.assertIn("Dropped 5 messages", result.notice())
                # The tail is untouched: a failed summary costs the old turns,
                # never the conversation that is still running.
                self.assert_no_orphan_tool_messages(result.messages)
                self.assertEqual(len(result.messages), 4)

    def test_no_provider_means_the_pre_summary_behaviour(self):
        result = compact_messages(self.tool_history(), window=2000, keep_last=2)
        self.assertFalse(result.summarized)
        self.assertEqual(result.error, "no summariser available")
        self.assertIn("dropped", result.messages[0].content)

    def test_a_history_that_fits_is_returned_untouched(self):
        history = self.tool_history()
        provider = _StubProvider()
        result = compact_messages(history, window=1000000, provider=provider,
                                  model="m")
        self.assertFalse(result.changed)
        self.assertEqual(result.folded, 0)
        self.assertEqual(result.notice(), "")
        self.assertEqual([m.content for m in result.messages],
                         [m.content for m in history])
        self.assertEqual(provider.requests, [],
                         "a history that fits must not cost a provider round")

    def test_the_summariser_is_asked_for_the_structured_template(self):
        provider = _StubProvider()
        compact_messages(self.tool_history(), window=2000, provider=provider,
                         model="m", keep_last=2)

        system, user = provider.requests[0]
        self.assertEqual(system.role, "system")
        self.assertIn("anchored context summarization", system.content)
        for section in ("## Objective", "## Important Details", "### Completed",
                        "### Blocked", "## Next Move", "## Relevant Files"):
            self.assertIn(section, user.content)
        # The folded turns are in the prompt as a transcript, the kept tail is
        # not -- it is still in the history verbatim.
        self.assertIn("[User]: port the parser", user.content)
        self.assertIn("[Assistant tool call]: read(", user.content)
        self.assertIn("Create a new anchored summary", user.content)
        self.assertNotIn(_long("done"), user.content)

    def test_an_earlier_summary_is_updated_rather_than_re_summarised(self):
        history = [Msg(role="user", content="old summary text",
                       display={"summary": True, "folded": 9})]
        history.extend(self.tool_history())
        provider = _StubProvider()
        compact_messages(history, window=2000, provider=provider, model="m",
                         keep_last=2)

        prompt = provider.requests[0][1].content
        self.assertIn("<previous-summary>", prompt)
        self.assertIn("old summary text", prompt)
        self.assertNotIn("[User]: old summary text", prompt)

    def test_summarize_is_the_one_call_and_reports_its_failures(self):
        messages = [Msg(role="user", content="decide the schema")]
        self.assertEqual(summarize(messages, _StubProvider("a summary"), "m"),
                         "a summary")
        self.assertEqual(summarize(messages, _StubProvider(fail="raise"), "m"), "")
        text, reason = summarize_with_reason(messages, _StubProvider(fail="error"),
                                             "m")
        self.assertEqual(text, "")
        self.assertIn("rate limited", reason)

    def test_the_same_fold_is_only_summarised_once(self):
        provider = _StubProvider()
        history = self.tool_history()
        first = compact_messages(history, window=2000, provider=provider,
                                 model="m", keep_last=2)
        second = compact_messages(history, window=2000, provider=provider,
                                  model="m", keep_last=2)

        # The request path runs this on every provider round; a run of ten
        # steps must not buy ten summaries of the same messages.
        self.assertEqual(len(provider.requests), 1)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(first.summary, second.summary)

        # A history that has moved on is a different fold, and is asked again.
        history.append(Msg(role="user", content=_long("something new")))
        compact_messages(history, window=2000, provider=provider, model="m",
                         keep_last=2)
        self.assertEqual(len(provider.requests), 2)

    def test_a_dead_provider_is_dialled_once_not_once_per_step(self):
        provider = _StubProvider(fail="raise")
        history = self.tool_history()
        for _ in range(3):
            result = compact_messages(history, window=2000, provider=provider,
                                      model="m", keep_last=2)
            self.assertFalse(result.summarized)
        self.assertEqual(len(provider.requests), 1)

    def test_compact_history_passes_the_provider_through(self):
        provider = _StubProvider("## Objective\n- from the agent loop")
        kept = compact_history(self.tool_history(), 2000, provider=provider,
                               model="m")
        joined = "\n".join(m.content or "" for m in kept)
        self.assertIn("from the agent loop", joined)
        self.assertNotIn("dropped to fit the context window", joined)

    def test_compaction_announces_itself_before_the_summariser_call(self):
        # Field report: the summariser is its own provider call, and both
        # front ends showed nothing while it ran — the pause read as a hang.
        told = []
        compact_history(self.tool_history(), 2000,
                        provider=_StubProvider("## Objective\n- ok"),
                        model="m", notify=lambda: told.append(True))
        self.assertEqual([True], told)

    def test_no_notification_when_nothing_needs_compacting(self):
        told = []
        compact_history(self.tool_history(), 10_000_000,
                        notify=lambda: told.append(True))
        self.assertEqual([], told)

    def test_compact_history_still_hands_back_a_plain_list(self):
        history = self.tool_history()
        kept = compact_history(history, window=2000)
        self.assertIsInstance(kept, list)
        self.assertLess(len(kept), len(history))
        self.assertIn("dropped", kept[0].content)
        # And it never mutates what it was given.
        self.assertEqual(len(history), 8)


if __name__ == "__main__":
    unittest.main()
