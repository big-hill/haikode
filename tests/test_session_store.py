import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from haikode.schema import CompletionChunk, Msg, ToolCall
from haikode.session import SessionStore, capture_modified, summarize_messages


class _StubProvider:
    """Answers one summarising round, or fails the way a real one would."""

    def __init__(self, text: str = "## Objective\n- keep going", fail: str = ""):
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
        yield CompletionChunk(text=self.text)


class _FakeContext:
    """Stands in for a ToolContext: capture_modified only reads modified_files."""

    def __init__(self, modified_files):
        self.modified_files = modified_files


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        # Resolved: ToolContext.resolve() hands the store realpaths, and on
        # macOS the temp dir is /var -> /private/var, so an unresolved root
        # would compare against paths the store never stores.
        self.root = Path(self._temp.name).resolve()
        self.store = SessionStore(self.root / "db" / "sessions.db")
        # Cleanups run LIFO: close the database before the directory goes away.
        self.addCleanup(self._temp.cleanup)
        self.addCleanup(self.store.close)

    def new_session(self, title=""):
        return self.store.new_session(str(self.root), "anthropic",
                                      "claude-sonnet-4", title=title)

    # --- persistence ------------------------------------------------------

    def test_message_with_tool_calls_round_trips_losslessly(self):
        session = self.new_session(title="fixed")
        call = ToolCall(id="call_1", name="read",
                        arguments={"filePath": "/tmp/x.py", "limit": 20,
                                   "nested": {"a": [1, 2, None]}})
        session.append(Msg(role="user", content="read the file"))
        session.append(Msg(role="assistant", content="on it", tool_calls=[call]))
        session.append(Msg(role="tool", content="1: print()", tool_call_id="call_1",
                           display={"tool": "read", "title": "x.py"}))

        loaded = self.store.load(session.id)
        self.assertIsNotNone(loaded)
        self.assertEqual([m.role for m in loaded.messages],
                         ["user", "assistant", "tool"])
        restored_call = loaded.messages[1].tool_calls[0]
        self.assertIsInstance(restored_call, ToolCall)
        self.assertEqual(restored_call, call)
        self.assertEqual(loaded.messages[2].tool_call_id, "call_1")
        self.assertEqual(loaded.messages[2].display,
                         {"tool": "read", "title": "x.py"})
        self.assertEqual(loaded.messages[0].tool_calls, [])

    def test_unknown_session_loads_as_none(self):
        self.assertIsNone(self.store.load("ses_missing"))

    # --- titles -----------------------------------------------------------

    def test_auto_title_collapses_whitespace_and_caps_length(self):
        session = self.new_session()
        session.auto_title("  refactor\n\tthe   parser  ")
        self.assertEqual(session.title, "refactor the parser")

        long_session = self.new_session()
        long_session.auto_title("word " * 60)
        self.assertLessEqual(len(long_session.title), 60)
        self.assertTrue(long_session.title.endswith("..."))
        self.assertNotIn("\n", long_session.title)

        # An explicit title always wins over the derived one.
        titled = self.new_session(title="release notes")
        titled.auto_title("something else entirely")
        self.assertEqual(titled.title, "release notes")

        # And the title survives a reload.
        self.assertEqual(self.store.load(session.id).title, "refactor the parser")

    def test_first_user_message_titles_the_session(self):
        session = self.new_session()
        session.append(Msg(role="user", content="fix the streaming bug"))
        session.append(Msg(role="user", content="second prompt"))
        self.assertEqual(session.title, "fix the streaming bug")

    # --- revert -----------------------------------------------------------

    def test_revert_restores_snapshotted_content(self):
        target = self.root / "src" / "main.py"
        target.parent.mkdir()
        target.write_text("original\n")

        session = self.new_session()
        session.append(Msg(role="user", content="edit it"))
        point = session.checkpoint()
        session.record_snapshot(str(target), "original\n")
        target.write_text("rewritten by the model\n")
        session.append(Msg(role="assistant", content="done"))

        restored = session.revert_to(point)
        self.assertEqual(restored, [str(target)])
        self.assertEqual(target.read_text(), "original\n")
        self.assertEqual([m.content for m in session.messages], ["edit it"])
        self.assertEqual([m.content for m in self.store.load(session.id).messages],
                         ["edit it"])

    def test_revert_deletes_files_that_did_not_exist_before(self):
        created = self.root / "generated" / "new.py"

        session = self.new_session()
        session.append(Msg(role="user", content="write a file"))
        point = session.checkpoint()
        session.record_snapshot(str(created), None)
        created.parent.mkdir()
        created.write_text("brand new\n")
        session.append(Msg(role="assistant", content="written"))

        restored = session.revert_to(point)
        self.assertEqual(restored, [str(created)])
        self.assertFalse(created.exists())
        self.assertEqual(len(session.messages), 1)

    def test_revert_last_undoes_the_newest_checkpoint_only(self):
        target = self.root / "a.txt"
        target.write_text("v1\n")
        session = self.new_session()

        session.checkpoint()
        session.record_snapshot(str(target), "v1\n")
        target.write_text("v2\n")
        session.append(Msg(role="user", content="first run"))

        second_point = session.checkpoint()
        session.record_snapshot(str(target), "v2\n")
        target.write_text("v3\n")
        session.append(Msg(role="assistant", content="second run"))

        self.assertEqual(session.last_checkpoint(), second_point)
        self.assertEqual(session.revert_last(), [str(target)])
        self.assertEqual(target.read_text(), "v2\n")
        self.assertEqual([m.content for m in session.messages], ["first run"])

        # The earlier checkpoint is still there and takes the file back to v1.
        self.assertEqual(session.revert_last(), [str(target)])
        self.assertEqual(target.read_text(), "v1\n")
        self.assertEqual(session.messages, [])
        self.assertEqual(session.revert_last(), [])

    def test_revert_reports_failures_without_aborting(self):
        good = self.root / "good.txt"
        good.write_text("changed\n")
        # A directory cannot be deleted as if it were a created file.
        blocked = self.root / "blocked"
        blocked.mkdir()

        session = self.new_session()
        point = session.checkpoint()
        session.record_snapshot(str(good), "before\n")
        session.record_snapshot(str(blocked), None)

        restored = session.revert_to(point)
        self.assertEqual(good.read_text(), "before\n")
        self.assertIn(str(good), restored)
        failures = [line for line in restored if "(failed:" in line]
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].startswith(str(blocked) + " (failed: "))
        self.assertTrue(blocked.is_dir())

    def test_capture_modified_snapshots_the_tool_context(self):
        edited = self.root / "edited.txt"
        edited.write_text("after\n")
        created = self.root / "created.txt"
        created.write_text("new\n")

        session = self.new_session()
        point = session.checkpoint()
        ctx = _FakeContext({str(edited): "before\n", str(created): None})
        recorded = capture_modified(session, ctx)
        self.assertEqual(sorted(recorded), sorted([str(edited), str(created)]))

        session.revert_to(point)
        self.assertEqual(edited.read_text(), "before\n")
        self.assertFalse(created.exists())

    def test_revert_last_undoes_a_run_that_touched_no_files(self):
        """A talk-only run leaves no snapshot row; undo must still stop there."""
        target = self.root / "a.txt"
        target.write_text("v1\n")
        session = self.new_session()

        session.checkpoint()
        session.record_snapshot(str(target), "v1\n")
        target.write_text("v2\n")
        session.append(Msg(role="user", content="edit it"))
        session.append(Msg(role="assistant", content="edited"))

        talk_point = session.checkpoint()
        session.append(Msg(role="user", content="what does it do?"))
        session.append(Msg(role="assistant", content="it prints"))

        self.assertEqual(session.last_checkpoint(), talk_point)
        self.assertEqual(session.revert_last(), [])
        self.assertEqual([m.content for m in session.messages],
                         ["edit it", "edited"])
        # The earlier run's edit survives; only the next undo rolls it back.
        self.assertEqual(target.read_text(), "v2\n")
        self.assertEqual(session.revert_last(), [str(target)])
        self.assertEqual(target.read_text(), "v1\n")
        self.assertEqual(session.messages, [])

    def test_revert_preserves_file_permissions(self):
        script = self.root / "build.sh"
        script.write_text("#!/bin/sh\noriginal\n")
        os.chmod(script, 0o755)

        session = self.new_session()
        point = session.checkpoint()
        session.record_snapshot(str(script), "#!/bin/sh\noriginal\n")
        script.write_text("#!/bin/sh\nmangled\n")

        session.revert_to(point)
        self.assertEqual(script.read_text(), "#!/bin/sh\noriginal\n")
        self.assertEqual(stat.S_IMODE(os.stat(script).st_mode), 0o755)

    def test_concurrent_appends_do_not_overwrite_each_other(self):
        session = self.new_session()
        start = threading.Event()
        # A short switch interval makes an unlocked seq allocation lose rows
        # essentially every run instead of once in a hundred.
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, previous)

        def worker(worker_id):
            start.wait()
            for n in range(25):
                session.append(Msg(role="assistant", content=f"{worker_id}-{n}"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join()

        # Every append must own a distinct seq, or INSERT OR REPLACE silently
        # drops messages that the in-memory list still claims to hold.
        self.assertEqual(len(self.store.load(session.id).messages), 6 * 25)

    def test_snapshot_keeps_the_earliest_original_per_revert_point(self):
        target = self.root / "twice.txt"
        target.write_text("final\n")

        session = self.new_session()
        point = session.checkpoint()
        session.record_snapshot(str(target), "first\n")
        session.record_snapshot(str(target), "second\n")  # must be ignored

        session.revert_to(point)
        self.assertEqual(target.read_text(), "first\n")

    # --- listing ----------------------------------------------------------

    def test_list_sessions_orders_by_recent_activity(self):
        first = self.new_session(title="first")
        second = self.new_session(title="second")
        third = self.new_session(title="third")

        # Ordering is by wall-clock `updated`; space the writes so the
        # assertion cannot hinge on clock resolution.
        time.sleep(0.01)
        second.append(Msg(role="user", content="hello"))
        second.append(Msg(role="assistant", content="hi"))
        time.sleep(0.01)
        first.append(Msg(role="user", content="later still"))

        listed = self.store.list_sessions()
        self.assertEqual([row["title"] for row in listed],
                         ["first", "second", "third"])
        counts = {row["id"]: row["message_count"] for row in listed}
        self.assertEqual(counts[first.id], 1)
        self.assertEqual(counts[second.id], 2)
        self.assertEqual(counts[third.id], 0)

        row = listed[0]
        self.assertEqual(row["provider"], "anthropic")
        self.assertEqual(row["model"], "claude-sonnet-4")
        self.assertEqual(row["cwd"], str(self.root))

        self.assertEqual(len(self.store.list_sessions(limit=2)), 2)

        self.store.delete(second.id)
        self.assertIsNone(self.store.load(second.id))
        self.assertEqual([row["id"] for row in self.store.list_sessions()],
                         [first.id, third.id])

    def test_database_is_created_lazily_under_a_missing_directory(self):
        path = self.root / "deep" / "nested" / "sessions.db"
        store = SessionStore(path)
        self.addCleanup(store.close)
        self.assertFalse(path.exists())
        store.new_session(str(self.root), "openai", "gpt-5")
        self.assertTrue(os.path.exists(path))

    # --- metadata ---------------------------------------------------------

    def test_rename_replaces_the_title_and_survives_a_reload(self):
        session = self.new_session(title="old name")
        self.assertEqual(session.rename("  new name  "), "new name")
        self.assertEqual(self.store.load(session.id).title, "new name")
        # set_title is the older spelling the REPL still uses.
        session.set_title("third name")
        self.assertEqual(self.store.load(session.id).title, "third name")

    def test_touch_bumps_updated_without_writing_a_message(self):
        session = self.new_session(title="idle")
        before = session.updated
        time.sleep(0.01)
        after = session.touch()
        self.assertGreater(after, before)
        self.assertEqual(self.store.list_sessions()[0]["id"], session.id)
        self.assertEqual(self.store.load(session.id).messages, [])

    def test_archived_sessions_leave_the_default_listing(self):
        keep = self.new_session(title="keep")
        drop = self.new_session(title="drop")
        stamp = drop.updated

        drop.archive()
        self.assertTrue(drop.archived)
        self.assertEqual([row["id"] for row in self.store.list_sessions()], [keep.id])
        listed = self.store.list_sessions(include_archived=True)
        self.assertEqual(sorted(row["id"] for row in listed),
                         sorted([keep.id, drop.id]))
        self.assertTrue({row["id"]: row["archived"] for row in listed}[drop.id])
        # Archiving is not activity: it must not reorder the list.
        self.assertEqual(self.store.load(drop.id).updated, stamp)

        drop.unarchive()
        self.assertFalse(self.store.load(drop.id).archived)
        self.assertEqual(sorted(row["id"] for row in self.store.list_sessions()),
                         sorted([keep.id, drop.id]))

    def test_list_sessions_can_be_scoped_to_one_project(self):
        here = self.new_session(title="this project")
        other_dir = self.root / "elsewhere"
        other_dir.mkdir()
        self.store.new_session(str(other_dir), "anthropic", "claude-sonnet-4",
                               title="other project")

        listed = self.store.list_sessions(cwd=str(self.root))
        self.assertEqual([row["id"] for row in listed], [here.id])
        # An unresolved spelling of the same directory still matches.
        unresolved = str(self.root) + os.sep + "." + os.sep
        self.assertEqual(
            [row["id"] for row in self.store.list_sessions(cwd=unresolved)],
            [here.id])
        self.assertEqual(len(self.store.list_sessions(cwd=str(other_dir))), 1)
        self.assertEqual(len(self.store.list_sessions()), 2)

        here.archive()
        self.assertEqual(self.store.list_sessions(cwd=str(self.root)), [])
        self.assertEqual(len(self.store.list_sessions(cwd=str(self.root),
                                                      include_archived=True)), 1)

    # --- export -----------------------------------------------------------

    def _transcript_session(self):
        session = self.new_session(title="export me")
        session.append(Msg(role="user", content="edit the parser"))
        session.append(Msg(role="assistant", content="reading first",
                           tool_calls=[ToolCall(id="c1", name="read",
                                                arguments={"filePath": "/p/a.py"})]))
        session.append(Msg(role="tool", content="1: x = 1", tool_call_id="c1",
                           display={"tool": "read", "title": "a.py",
                                    "path": "/p/a.py"}))
        session.append(Msg(role="assistant", content="now editing",
                           tool_calls=[ToolCall(id="c2", name="edit",
                                                arguments={"filePath": "/p/a.py"})]))
        session.append(Msg(role="tool", content="applied", tool_call_id="c2",
                           display={"tool": "edit", "title": "a.py",
                                    "diff": "--- a\n+++ b\n-x = 1\n+x = 2"}))
        session.append(Msg(role="assistant", content="done"))
        return session

    def test_markdown_export_renders_turns_tools_and_diffs(self):
        text = self._transcript_session().export()
        self.assertTrue(text.startswith("# export me\n"))
        self.assertIn("- Model: anthropic/claude-sonnet-4", text)
        self.assertIn("## User\n\nedit the parser", text)
        self.assertIn("## Assistant", text)
        self.assertIn("**read** `{\"filePath\": \"/p/a.py\"}`", text)
        self.assertIn("### Tool: read - a.py", text)
        self.assertIn("```\n1: x = 1\n```", text)
        # An edit is shown as its diff, in a diff fence.
        self.assertIn("```diff\n--- a\n+++ b\n-x = 1\n+x = 2\n```", text)
        self.assertEqual(text.count("### Tool:"), 2)

    def test_markdown_export_survives_backticks_in_tool_output(self):
        session = self.new_session(title="fences")
        session.append(Msg(role="tool", content="```\ninner fence\n```",
                           tool_call_id="c1", display={"tool": "bash"}))
        text = session.export("md")
        self.assertIn("````\n```\ninner fence\n```\n````", text)

    def test_markdown_export_marks_denied_tools(self):
        session = self.new_session(title="denied")
        session.append(Msg(role="tool", content="The user rejected this action",
                           tool_call_id="c1",
                           display={"tool": "bash", "denied": True}))
        self.assertIn("_denied by the user_", session.export())

    def test_text_export_is_plain_and_complete(self):
        text = self._transcript_session().export("text")
        self.assertTrue(text.startswith("export me\n"))
        self.assertIn("--- USER ---\nedit the parser", text)
        self.assertIn("--- TOOL read ---", text)
        self.assertIn("read({\"filePath\": \"/p/a.py\"})", text)
        self.assertNotIn("#", text)

    def test_json_export_round_trips_the_transcript(self):
        session = self._transcript_session()
        data = json.loads(session.export_json())
        self.assertEqual(data["id"], session.id)
        self.assertEqual(data["title"], "export me")
        self.assertEqual([m["role"] for m in data["messages"]],
                         ["user", "assistant", "tool", "assistant", "tool",
                          "assistant"])
        self.assertEqual(data["messages"][1]["tool_calls"][0]["name"], "read")
        self.assertEqual(data["messages"][2]["display"]["title"], "a.py")
        self.assertEqual(data["stats"]["messages"], 6)
        self.assertEqual(json.loads(session.export("json"))["id"], session.id)

    def test_export_rejects_an_unknown_format(self):
        with self.assertRaises(ValueError):
            self.new_session().export("pdf")

    # --- compaction -------------------------------------------------------

    def _tool_pair_session(self):
        """user, call/result, call/result, answer -- six messages, two pairs."""
        session = self.new_session(title="long run")
        session.append(Msg(role="user", content="do the work"))
        session.append(Msg(role="assistant", content="step one",
                           tool_calls=[ToolCall(id="c1", name="read",
                                                arguments={"filePath": "/p/a.py"})]))
        session.append(Msg(role="tool", content="file body", tool_call_id="c1",
                           display={"tool": "read", "title": "a.py"}))
        session.append(Msg(role="assistant", content="step two",
                           tool_calls=[ToolCall(id="c2", name="edit",
                                                arguments={"filePath": "/p/a.py"})]))
        session.append(Msg(role="tool", content="edited", tool_call_id="c2",
                           display={"tool": "edit", "title": "a.py"}))
        session.append(Msg(role="assistant", content="all done"))
        return session

    def assert_no_orphan_tool_results(self, session):
        seen = set()
        for message in session.messages:
            for call in message.tool_calls:
                seen.add(call.id)
            if message.role == "tool":
                self.assertIn(message.tool_call_id, seen,
                              "tool result %r lost its call" % message.tool_call_id)

    def test_compact_folds_the_head_into_one_summary_message(self):
        session = self._tool_pair_session()
        folded = session.compact(keep_last=3, summary="we fixed the parser")

        self.assertEqual(folded, 3)
        self.assertEqual(len(session.messages), 4)
        self.assertEqual(session.messages[0].role, "user")
        self.assertEqual(session.messages[0].content, "we fixed the parser")
        self.assertTrue(session.messages[0].display.get("summary"))
        self.assertEqual(session.messages[0].display.get("folded"), 3)
        self.assertEqual([m.content for m in session.messages[1:]],
                         ["step two", "edited", "all done"])
        self.assert_no_orphan_tool_results(session)
        # And it is the persisted state, not just the in-memory one.
        reloaded = self.store.load(session.id)
        self.assertEqual([m.content for m in reloaded.messages],
                         [m.content for m in session.messages])

    def test_compact_never_splits_a_tool_call_from_its_result(self):
        session = self._tool_pair_session()
        # keep_last=2 would cut between "step two" and its result; the fold
        # point has to move back so the pair stays whole.
        folded = session.compact(keep_last=2)

        self.assertEqual(folded, 3)
        self.assertEqual(session.messages[1].role, "assistant")
        self.assertEqual(session.messages[1].tool_calls[0].id, "c2")
        self.assertEqual(session.messages[2].tool_call_id, "c2")
        self.assert_no_orphan_tool_results(session)

    def test_compact_writes_a_useful_summary_when_none_is_given(self):
        session = self._tool_pair_session()
        session.compact(keep_last=2)
        summary = session.messages[0].content
        self.assertIn("3 messages were folded", summary)
        self.assertIn("do the work", summary)
        self.assertIn("read x1", summary)
        self.assertIn("/p/a.py", summary)
        self.assertEqual(summarize_messages([]), "")

    def test_compact_does_nothing_when_the_history_is_short(self):
        session = self._tool_pair_session()
        self.assertEqual(session.compact(keep_last=10), 0)
        self.assertEqual(session.compact(keep_last=6), 0)
        self.assertEqual(len(session.messages), 6)
        self.assertEqual(session.compactions(), [])

    def test_compact_records_what_it_dropped_and_can_put_it_back(self):
        session = self._tool_pair_session()
        original = [(m.role, m.content) for m in session.messages]
        session.compact(keep_last=3, summary="condensed")

        records = session.compactions()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["folded"], 3)
        self.assertEqual(records[0]["first_seq"], 1)
        self.assertEqual(records[0]["summary"], "condensed")
        self.assertEqual([item["role"] for item in records[0]["messages"]],
                         ["user", "assistant", "tool"])

        self.assertEqual(session.restore_compaction(), 3)
        self.assertEqual([(m.role, m.content) for m in session.messages], original)
        self.assertEqual(session.seqs, [1, 2, 3, 4, 5, 6])
        self.assertEqual(session.messages[1].tool_calls[0].name, "read")
        self.assertEqual(session.compactions(), [])
        self.assertEqual(session.restore_compaction(), 0)

    def test_compaction_keeps_appending_at_a_fresh_seq(self):
        session = self._tool_pair_session()
        session.compact(keep_last=2)
        seq = session.append(Msg(role="user", content="next question"))
        self.assertEqual(seq, 7)
        self.assertEqual([m.content for m in self.store.load(session.id).messages][-1],
                         "next question")

    def test_compacting_twice_keeps_both_records(self):
        session = self._tool_pair_session()
        self.assertEqual(session.compact(keep_last=3), 3)
        session.append(Msg(role="assistant", content="more talk"))
        self.assertEqual(session.compact(keep_last=2), 3)
        records = session.compactions()
        self.assertEqual([record["folded"] for record in records], [3, 3])
        # The newer record folded the older summary, so restoring it twice
        # walks the timeline all the way back.
        session.restore_compaction()
        session.restore_compaction()
        self.assertEqual([m.content for m in session.messages][:2],
                         ["do the work", "step one"])

    def test_reverting_past_a_compaction_drops_its_record(self):
        session = self._tool_pair_session()
        session.compact(keep_last=2)
        session.revert_to(2)
        self.assertEqual(session.compactions(), [])

    # --- model-written compaction ----------------------------------------

    def test_a_model_summary_lands_in_the_history_and_in_the_database(self):
        session = self._tool_pair_session()
        provider = _StubProvider("## Objective\n- fix the parser")

        result = session.compact_now(keep_last=3, provider=provider, model="m")

        self.assertTrue(result.summarized)
        self.assertEqual(result.error, "")
        self.assertEqual(result.folded, 3)
        self.assertEqual(result.notice(), "Compacted 3 messages into a summary.")
        self.assertEqual(result.summary, "## Objective\n- fix the parser")
        self.assertEqual(session.messages[0].content, result.summary)
        self.assertTrue(session.messages[0].display.get("summary"))
        # Persisted twice over: as the message that replaced the turns, and as
        # the compaction record /undo reads.
        records = session.compactions()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["summary"], result.summary)
        self.assertEqual(records[0]["folded"], 3)
        self.assert_no_orphan_tool_results(session)

    def test_a_resumed_session_still_has_the_summary(self):
        session = self._tool_pair_session()
        session.compact_now(keep_last=3, provider=_StubProvider("## Objective\n- x"),
                            model="m")

        resumed = self.store.load(session.id)
        self.assertEqual(resumed.messages[0].content, "## Objective\n- x")
        self.assertTrue(resumed.messages[0].display.get("summary"))
        self.assertEqual(resumed.messages[0].display.get("folded"), 3)
        self.assertEqual([m.content for m in resumed.messages[1:]],
                         ["step two", "edited", "all done"])
        self.assertEqual(resumed.previous_summary(), "## Objective\n- x")

    def test_a_failing_summariser_falls_back_without_losing_a_message(self):
        session = self._tool_pair_session()
        original = [(m.role, m.content) for m in session.messages]

        result = session.compact_now(keep_last=3, model="m",
                                     provider=_StubProvider(fail="raise"))

        self.assertFalse(result.summarized)
        self.assertIn("OSError", result.error)
        self.assertEqual(result.folded, 3)
        # The mechanical digest is stored instead of nothing at all...
        self.assertIn("3 messages were folded", session.messages[0].content)
        self.assertIn("do the work", session.messages[0].content)
        # ...and the folded turns themselves are still on disk, verbatim.
        self.assertEqual(session.restore_compaction(), 3)
        self.assertEqual([(m.role, m.content) for m in session.messages], original)

    def test_a_second_compaction_updates_the_stored_summary(self):
        session = self._tool_pair_session()
        first = _StubProvider("## Objective\n- the first summary")
        session.compact_now(keep_last=3, provider=first, model="m")
        session.append(Msg(role="user", content="carry on"))
        session.append(Msg(role="assistant", content="carrying on"))

        second = _StubProvider("## Objective\n- the merged summary")
        result = session.compact_now(keep_last=2, provider=second, model="m")

        prompt = second.requests[0][1].content
        self.assertIn("<previous-summary>", prompt)
        self.assertIn("the first summary", prompt)
        self.assertEqual(session.messages[0].content, result.summary)
        self.assertEqual(session.previous_summary(), "## Objective\n- the merged summary")

    def test_a_pinned_message_survives_a_session_compaction(self):
        session = self.new_session(title="pinned")
        session.append(Msg(role="user", content="never touch vendor/",
                           display={"pinned": True}))
        for index in range(5):
            session.append(Msg(role="assistant", content="turn %d" % index))

        result = session.compact_now(keep_last=2,
                                     provider=_StubProvider("## Objective\n- go"),
                                     model="m")

        self.assertEqual(result.folded, 3)
        self.assertEqual([m.content for m in session.messages],
                         ["never touch vendor/", "## Objective\n- go",
                          "turn 3", "turn 4"])
        # The seq of the pinned message is untouched, so the summary that
        # replaced the turns after it still sorts behind it after a reload.
        reloaded = self.store.load(session.id)
        self.assertEqual([m.content for m in reloaded.messages],
                         [m.content for m in session.messages])
        self.assertEqual(reloaded.seqs, [1, 4, 5, 6])
        self.assertEqual(reloaded.restore_compaction(), 3)
        self.assertEqual([m.content for m in reloaded.messages],
                         ["never touch vendor/", "turn 0", "turn 1", "turn 2",
                          "turn 3", "turn 4"])

    def test_manual_and_automatic_compaction_produce_the_same_shape(self):
        fields = ("folded", "kept", "summary", "summarized", "error")
        manual = self._tool_pair_session().compact_now(
            keep_last=3, provider=_StubProvider("## Objective\n- same"),
            model="m", trigger="manual")
        auto = self._tool_pair_session().compact_now(
            keep_last=3, provider=_StubProvider("## Objective\n- same"),
            model="m", trigger="auto")

        self.assertEqual(manual.trigger, "manual")
        self.assertEqual(auto.trigger, "auto")
        for name in fields:
            self.assertEqual(getattr(manual, name), getattr(auto, name), name)
        self.assertEqual([m.content for m in manual.messages],
                         [m.content for m in auto.messages])
        self.assertEqual(manual.notice(), auto.notice())

    def test_compact_is_compact_now_with_the_count_taken_off(self):
        provider = _StubProvider("## Objective\n- one path")
        session = self._tool_pair_session()
        self.assertEqual(session.compact(keep_last=3, provider=provider,
                                         model="m"), 3)
        self.assertEqual(session.messages[0].content, "## Objective\n- one path")
        self.assertEqual(len(provider.requests), 1)

    def test_needs_compaction_follows_the_window_budget(self):
        session = self.new_session(title="big")
        for index in range(6):
            session.append(Msg(role="assistant", content="x" * 400))

        # 6 messages of ~104 tokens each is past the default share of a 500
        # window and nowhere near it in a 100000 one. `reserve` still means
        # "fraction of the window the history may use"; only the default moved
        # up, so haikode stops folding a conversation away at 40% full.
        self.assertTrue(session.needs_compaction(500))
        self.assertFalse(session.needs_compaction(100000))
        self.assertTrue(session.needs_compaction(100000, reserve=0.001))
        self.assertFalse(session.needs_compaction(0))

        # Too few messages to fold is never worth compacting.
        short = self.new_session(title="short")
        for index in range(4):
            short.append(Msg(role="assistant", content="x" * 4000))
        self.assertFalse(short.needs_compaction(1000))

    # --- search -----------------------------------------------------------

    def test_search_ranks_title_matches_above_body_matches(self):
        titled = self.new_session(title="streaming parser rewrite")
        titled.append(Msg(role="assistant", content="nothing relevant here"))
        body = self.new_session(title="unrelated work")
        body.append(Msg(role="user", content="notes"))
        body.append(Msg(role="assistant",
                        content="first line\nthe streaming parser is slow\nlast line"))
        self.new_session(title="no match at all")

        results = self.store.search("streaming parser")
        self.assertEqual([row["id"] for row in results], [titled.id, body.id])
        self.assertGreater(results[0]["score"], results[1]["score"])
        self.assertEqual(results[0]["snippet"], "streaming parser rewrite")
        self.assertEqual(results[1]["snippet"], "the streaming parser is slow")
        self.assertEqual(results[1]["message_count"], 2)
        self.assertEqual(results[1]["title"], "unrelated work")

    def test_search_snippet_is_a_short_excerpt_of_a_long_line(self):
        session = self.new_session(title="logs")
        session.append(Msg(role="assistant",
                           content="pad " * 200 + "TARGETWORD" + " tail" * 200))
        row = self.store.search("TARGETWORD")[0]
        self.assertIn("TARGETWORD", row["snippet"])
        self.assertLessEqual(len(row["snippet"]), 130)
        self.assertTrue(row["snippet"].startswith("..."))

    def test_search_honours_limit_archiving_and_the_empty_query(self):
        first = self.new_session(title="alpha one")
        time.sleep(0.01)
        second = self.new_session(title="alpha two")

        self.assertEqual([row["id"] for row in self.store.search("alpha")],
                         [second.id, first.id])
        self.assertEqual(len(self.store.search("alpha", limit=1)), 1)
        # An empty query degrades into the recent list.
        self.assertEqual([row["id"] for row in self.store.search("")],
                         [row["id"] for row in self.store.list_sessions()])
        self.assertEqual(self.store.search("nothingmatchesthis"), [])

        second.archive()
        self.assertEqual([row["id"] for row in self.store.search("alpha")], [first.id])
        self.assertEqual(len(self.store.search("alpha", include_archived=True)), 2)

    # --- statistics -------------------------------------------------------

    def test_stats_reports_roles_tools_files_and_timestamps(self):
        target = self.root / "counted.txt"
        target.write_text("after\n")
        session = self.new_session(title="stats")
        session.append(Msg(role="user", content="go"))
        session.append(Msg(role="assistant", content="working",
                           tool_calls=[ToolCall(id="c1", name="read", arguments={}),
                                       ToolCall(id="c2", name="read", arguments={})]))
        session.append(Msg(role="tool", content="a", tool_call_id="c1",
                           display={"tool": "read"}))
        session.append(Msg(role="tool", content="b", tool_call_id="c2",
                           display={"tool": "read"}), tokens=7)
        session.append(Msg(role="assistant", content="done",
                           tool_calls=[ToolCall(id="c3", name="edit", arguments={})]))
        session.record_snapshot(str(target), "before\n")

        stats = session.stats()
        self.assertEqual(stats["messages"], 5)
        self.assertEqual(stats["roles"], {"user": 1, "assistant": 2, "tool": 2})
        self.assertEqual(stats["tools"], {"read": 2, "edit": 1})
        self.assertEqual(stats["tool_calls"], 3)
        self.assertEqual(stats["files"], [str(target)])
        self.assertEqual(stats["compactions"], 0)
        self.assertEqual(stats["folded_messages"], 0)
        self.assertLessEqual(stats["first_message"], stats["last_message"])
        self.assertGreaterEqual(stats["first_message"], stats["created"])
        self.assertEqual(stats["tokens"]["recorded"], 7)
        self.assertEqual(stats["provider"], "anthropic")

        session.compact(keep_last=1, summary="short")
        after = session.stats()
        self.assertEqual(after["compactions"], 1)
        self.assertEqual(after["folded_messages"], 4)
        self.assertEqual(after["messages"], 2)

    # --- token accounting -------------------------------------------------

    def test_token_totals_mix_recorded_counts_with_estimates(self):
        session = self.new_session(title="tokens")
        session.append(Msg(role="user", content="hello"), tokens=11)
        session.append(Msg(role="assistant", content="hi there"), tokens=29)
        session.append(Msg(role="assistant", content="x" * 400))

        totals = session.token_totals()
        self.assertEqual(totals["recorded"], 40)
        self.assertEqual(totals["counted"], 2)
        self.assertEqual(totals["messages"], 3)
        self.assertGreater(totals["estimated"], 100)
        self.assertEqual(totals["total"], totals["recorded"] + totals["estimated"])
        self.assertEqual(totals["by_role"]["user"], 11)
        self.assertEqual(totals["by_role"]["assistant"],
                         29 + totals["estimated"])

        # Counts stay attached to their message across a reload, and can be
        # filled in afterwards when the provider reports them late.
        session.set_tokens(3, 5)
        self.assertEqual(self.store.load(session.id).token_totals()["recorded"], 45)
        session.set_tokens(3, None)
        self.assertEqual(self.store.load(session.id).token_totals()["counted"], 2)

    # --- safety -----------------------------------------------------------

    def test_revert_refuses_to_write_outside_the_session_directory(self):
        project = self.root / "project"
        project.mkdir()
        inside = project / "inside.txt"
        inside.write_text("changed\n")
        outside = self.root / "outside.txt"
        outside.write_text("untouched\n")

        session = self.store.new_session(str(project), "anthropic", "claude-sonnet-4")
        point = session.checkpoint()
        session.record_snapshot(str(inside), "before\n")
        session.record_snapshot(str(outside), "attacker content\n")

        restored = session.revert_to(point)
        self.assertEqual(inside.read_text(), "before\n")
        self.assertEqual(outside.read_text(), "untouched\n")
        skipped = [line for line in restored if "(skipped:" in line]
        self.assertEqual(len(skipped), 1)
        self.assertTrue(skipped[0].startswith(str(outside) + " (skipped: "))

    def test_concurrent_appends_keep_every_message_body(self):
        """The TUI appends from a worker thread; nothing may be lost or merged."""
        session = self.new_session(title="threads")
        start = threading.Event()
        previous = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        self.addCleanup(sys.setswitchinterval, previous)
        expected = set()

        def worker(worker_id):
            start.wait()
            for n in range(20):
                session.append(Msg(role="assistant", content="w%d-%d" % (worker_id, n)),
                               tokens=1)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for worker_id in range(8):
            expected.update("w%d-%d" % (worker_id, n) for n in range(20))
        for thread in threads:
            thread.start()
        start.set()
        for thread in threads:
            thread.join()

        stored = self.store.load(session.id)
        self.assertEqual({m.content for m in stored.messages}, expected)
        self.assertEqual(len(stored.messages), 160)
        self.assertEqual(len(set(stored.seqs)), 160)
        self.assertEqual(stored.token_totals()["recorded"], 160)


class SessionSchemaMigrationTests(unittest.TestCase):
    """An older database must keep working, with every row intact."""

    # Exactly the schema haikode shipped before archiving, per-message
    # timestamps, token counts and compaction records existed.
    OLD_SCHEMA = (
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            cwd TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            created REAL NOT NULL DEFAULT 0,
            updated REAL NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE messages (
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            tool_calls TEXT NOT NULL DEFAULT '[]',
            tool_call_id TEXT NOT NULL DEFAULT '',
            display TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (session_id, seq)
        )
        """,
        """
        CREATE TABLE snapshots (
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            path TEXT NOT NULL,
            original TEXT,
            created REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (session_id, seq, path)
        )
        """,
    )

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name).resolve()
        self.addCleanup(self._temp.cleanup)
        self.path = self.root / "old.db"
        self.write_old_database()

    def write_old_database(self):
        conn = sqlite3.connect(str(self.path))
        try:
            for statement in self.OLD_SCHEMA:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO sessions (id, title, cwd, provider, model, created, updated) "
                "VALUES ('ses_old', 'legacy work', ?, 'anthropic', 'claude-3', 100.0, 200.0)",
                (str(self.root),))
            conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, tool_calls, "
                "tool_call_id, display) VALUES ('ses_old', 1, 'user', 'legacy prompt', "
                "'[]', '', '{}')")
            conn.execute(
                "INSERT INTO messages (session_id, seq, role, content, tool_calls, "
                "tool_call_id, display) VALUES ('ses_old', 2, 'assistant', 'legacy reply', "
                "?, '', '{}')",
                (json.dumps([{"id": "c1", "name": "read", "arguments": {"filePath": "/a"}}]),))
            conn.execute(
                "INSERT INTO snapshots (session_id, seq, path, original, created) "
                "VALUES ('ses_old', 0, ?, 'before\n', 150.0)",
                (str(self.root / "legacy.txt"),))
            conn.commit()
        finally:
            conn.close()

    def open_store(self):
        store = SessionStore(self.path)
        self.addCleanup(store.close)
        return store

    def test_old_rows_survive_the_migration(self):
        store = self.open_store()
        session = store.load("ses_old")
        self.assertIsNotNone(session)
        self.assertEqual(session.title, "legacy work")
        self.assertEqual([m.content for m in session.messages],
                         ["legacy prompt", "legacy reply"])
        self.assertEqual(session.messages[1].tool_calls[0].name, "read")
        self.assertFalse(session.archived)
        self.assertEqual(session.seqs, [1, 2])
        self.assertEqual(list(session.snapshots(0)),
                         [str(self.root / "legacy.txt")])

    def test_migrated_database_supports_the_new_features(self):
        store = self.open_store()
        session = store.load("ses_old")

        # Columns that did not exist are NULL, so counts fall back to estimates.
        totals = session.token_totals()
        self.assertEqual(totals["recorded"], 0)
        self.assertEqual(totals["counted"], 0)
        self.assertGreater(totals["total"], 0)

        # Timestamps that were never recorded fall back to the session dates.
        stats = session.stats()
        self.assertEqual(stats["first_message"], 100.0)
        self.assertEqual(stats["last_message"], 200.0)

        session.archive()
        self.assertEqual(store.list_sessions(), [])
        session.unarchive()
        self.assertEqual([row["id"] for row in store.list_sessions(cwd=str(self.root))],
                         ["ses_old"])
        self.assertEqual([row["id"] for row in store.search("legacy")], ["ses_old"])

        # New writes get the new columns; old rows keep their NULLs.
        session.append(Msg(role="user", content="a new turn"), tokens=13)
        self.assertEqual(store.load("ses_old").token_totals()["recorded"], 13)
        self.assertEqual(session.compact(keep_last=1, summary="folded"), 2)
        self.assertEqual(len(session.compactions()), 1)

    def test_migration_is_idempotent_and_adds_no_duplicate_columns(self):
        first = self.open_store()
        first.load("ses_old")
        first.close()

        second = self.open_store()
        session = second.load("ses_old")
        self.assertEqual(len(session.messages), 2)
        columns = [row[1] for row in
                   second.connect().execute("PRAGMA table_info(messages)")]
        self.assertEqual(len(columns), len(set(columns)))
        self.assertIn("tokens", columns)
        self.assertIn("created", columns)
        self.assertIn("archived",
                      [row[1] for row in
                       second.connect().execute("PRAGMA table_info(sessions)")])


class TestLegacySchemaMigration(unittest.TestCase):
    """A database written by an older build must still open.

    Reproduced by the parity benchmark: a short-lived build created
    `compactions` without its AUTOINCREMENT `id`, and the index that names that
    column made every connect() raise. REPL._open_session() swallows the error,
    so the user silently lost sessions, /undo, --continue and /resume.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-legacy-")
        self.path = Path(self.dir) / "sessions.db"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write_old_database(self):
        conn = sqlite3.connect(str(self.path))
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, "
                     "cwd TEXT, provider TEXT, model TEXT, created REAL, updated REAL)")
        conn.execute("CREATE TABLE messages (session_id TEXT, seq INTEGER, role TEXT, "
                     "content TEXT, tool_calls TEXT, tool_call_id TEXT, display TEXT)")
        conn.execute("CREATE TABLE snapshots (session_id TEXT, seq INTEGER, path TEXT, "
                     "original TEXT, created REAL)")
        # The damaged shape: the table exists, but without `id`.
        conn.execute("CREATE TABLE compactions (session_id TEXT, seq INTEGER, "
                     "created REAL, folded INTEGER, first_seq INTEGER, "
                     "last_seq INTEGER, summary TEXT, messages TEXT)")
        conn.execute("INSERT INTO sessions VALUES ('ses_old', 'earlier work', "
                     "'/tmp', 'ollama', 'glm-5.2', 1.0, 2.0)")
        conn.execute("INSERT INTO messages VALUES ('ses_old', 1, 'user', 'hei', "
                     "NULL, '', NULL)")
        conn.execute("INSERT INTO compactions VALUES ('ses_old', 1, 3.0, 4, 1, 4, "
                     "'summary text', '[]')")
        conn.commit()
        conn.close()

    def test_old_database_opens_and_keeps_its_rows(self):
        self._write_old_database()
        store = SessionStore(self.path)
        try:
            rows = store.list_sessions()
            self.assertEqual([r["id"] for r in rows], ["ses_old"])
            session = store.load("ses_old")
            self.assertIsNotNone(session)
            self.assertEqual([m.content for m in session.messages], ["hei"])

            carried = store.connect().execute(
                "SELECT session_id, summary, folded FROM compactions").fetchall()
            self.assertEqual([tuple(r) for r in carried],
                             [("ses_old", "summary text", 4)])
            self.assertIn("id", [r[1] for r in store.connect().execute(
                "PRAGMA table_info(compactions)")])
        finally:
            store.close()

    def test_migration_is_idempotent(self):
        self._write_old_database()
        for _ in range(3):
            store = SessionStore(self.path)
            store.list_sessions()
            store.close()
        conn = sqlite3.connect(str(self.path))
        try:
            leftovers = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%_migrating'")]
            self.assertEqual(leftovers, [])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM compactions").fetchone()[0], 1)
        finally:
            conn.close()


class TestWedgedWalRecovery(unittest.TestCase):
    """A dead process's WAL index must not cost the user their sessions.

    Reproduced on Haiku: a killed run left sessions.db-shm behind and every
    later open raised "locking protocol". Fifteen sessions were intact on disk
    and completely unreachable, and the only symptom the user saw was one line
    saying undo was unavailable.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="haikode-wal-")
        self.path = Path(self.dir) / "sessions.db"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _seed(self):
        store = SessionStore(self.path)
        store.new_session(self.dir, "zen", "m", title="earlier work")
        store.close()

    def test_a_stale_index_is_cleared_and_the_rows_survive(self):
        self._seed()
        index = Path(str(self.path) + "-shm")
        index.write_bytes(b"\0" * 32768)          # what the dead process left
        journal = Path(str(self.path) + "-wal")
        journal.write_bytes(b"\0" * 4096)

        store = SessionStore(self.path)
        store._TRANSIENT_PAUSES = (0, 0)
        # A dead process's lock state does not clear on its own: the error
        # must survive the transient retries, or retrying (rightly) makes
        # recovery unnecessary.
        wedged_opens = 1 + len(store._TRANSIENT_PAUSES)
        opens = {"count": 0}
        real = sqlite3.connect

        def flaky(*args, **kwargs):
            opens["count"] += 1
            if opens["count"] <= wedged_opens:
                raise sqlite3.OperationalError("locking protocol")
            return real(*args, **kwargs)

        try:
            with patch.object(sqlite3, "connect", flaky):
                rows = store.list_sessions()
            self.assertEqual([r["title"] for r in rows], ["earlier work"])
            self.assertTrue(Path(str(self.path) + "-wal.recovered").exists(),
                            "the wal is kept aside, never deleted outright")
            # sqlite rebuilds -shm the moment it reopens in WAL mode, so its
            # presence proves nothing; that it is no longer the dead process's
            # copy does.
            if index.exists():
                self.assertNotEqual(index.read_bytes(), b"\0" * 32768)
        finally:
            store.close()

    def test_an_unrelated_error_is_not_papered_over(self):
        self._seed()

        def broken(*args, **kwargs):
            raise sqlite3.OperationalError("no such table: whatever")

        store = SessionStore(self.path)
        try:
            with patch.object(sqlite3, "connect", broken):
                with self.assertRaises(sqlite3.OperationalError):
                    store.connect()
        finally:
            store.close()


class LiveWalGuardTests(unittest.TestCase):
    """Field failure, two evenings running: a spawned one-shot haikode hit
    Haiku's transient "locking protocol" on open, found the guard unheld,
    and "recovered" the live terminal's WAL out from under it — "session
    not saved" from there until restart, committed turns left in a
    -wal.recovered file. The guard must be held for the store's lifetime,
    transient errors must be retried before recovery is even considered,
    and a wedged connection must heal itself on the next write.
    """

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "sessions.db"

    def store(self):
        store = SessionStore(self.path)
        self.addCleanup(store.close)
        return store

    def test_connect_holds_the_guard_while_open(self):
        store = self.store()
        store.connect()
        self.assertIsNotNone(store._guard)

    def test_recovery_declines_while_a_live_owner_holds_the_guard(self):
        live = self.store()
        session = live.new_session(".", "p", "m")
        session.append(Msg(role="user", content="precious, uncheckpointed"))
        stranger = self.store()
        stranger._open_here = lambda: False      # simulate another process
        cleared = stranger._clear_stale_wal(
            sqlite3.OperationalError("locking protocol"))
        self.assertFalse(cleared)
        self.assertFalse(Path(str(self.path) + "-wal.recovered").exists())
        self.assertTrue(Path(str(self.path) + "-shm").exists())

    def test_every_live_store_stays_guarded_until_the_last_one_leaves(self):
        # The adversarial review's staircase: with a single exclusive owner,
        # the second store lived unguarded the moment the first one left,
        # and recovery could rip its live WAL. Shared lifetime locks close
        # that: recovery needs the exclusive lock, impossible while ANY
        # store lives.
        first = self.store()
        first.connect()
        second = self.store()
        second.connect()
        stranger = self.store()
        stranger._open_here = lambda: False
        err = sqlite3.OperationalError("locking protocol")
        self.assertFalse(stranger._clear_stale_wal(err))
        first.close()                      # the original owner leaves
        self.assertFalse(stranger._clear_stale_wal(err),
                         "the survivor must still be guarded")
        second.close()
        loner = self.store()
        self.assertTrue(loner._claim(exclusive=True),
                        "with everyone gone, recovery is claimable again")

    def test_a_file_that_is_not_a_database_fails_loudly_at_open(self):
        # Field forensics: a live store got TLS record bytes written into
        # it. The WAL pragma's "filesystems may refuse WAL" tolerance then
        # swallowed the resulting "file is not a database", and the session
        # limped on half-blind instead of failing at the door.
        self.path.write_bytes(b"SQLit\x17\x03\x03 tls garbage" + b"\0" * 128)
        store = self.store()
        with self.assertRaises(sqlite3.DatabaseError):
            store.connect()

    def test_a_non_transient_error_mid_retry_is_raised_not_recovered(self):
        store = self.store()
        store._TRANSIENT_PAUSES = (0, 0)
        real = store._open
        blows = [sqlite3.OperationalError("locking protocol"),
                 sqlite3.OperationalError("database disk image is malformed")]

        def flaky():
            if blows:
                raise blows.pop(0)
            return real()

        store._open = flaky
        with self.assertRaises(sqlite3.OperationalError) as caught:
            store.connect()
        self.assertIn("malformed", str(caught.exception))
        self.assertFalse(Path(str(self.path) + "-wal.recovered").exists())

    def test_a_durable_but_reported_failed_commit_is_not_overwritten(self):
        # The review's reproduction: commit lands, then reports "disk I/O
        # error"; the old INSERT OR REPLACE retry then overwrote the
        # committed row with the next message.
        store = self.store()
        session = store.new_session(".", "p", "m")
        session.append(Msg(role="user", content="first"))
        real = store.connect()

        class Liar:
            armed = True

            def execute(self, sql, *params):
                result = real.execute(sql, *params)
                if Liar.armed and sql == "COMMIT":
                    Liar.armed = False
                    raise sqlite3.OperationalError("disk I/O error")
                return result

            def close(self):
                pass

        store._conn = Liar()
        seq = session.append(Msg(role="user", content="durable"))
        self.assertEqual(2, seq)
        session.append(Msg(role="user", content="third"))
        rows = [tuple(row) for row in store.connect().execute(
            "SELECT seq, content FROM messages WHERE session_id = ? "
            "ORDER BY seq", (session.id,))]
        self.assertEqual([(1, "first"), (2, "durable"), (3, "third")], rows)

    def test_a_failed_second_statement_rolls_back_the_first(self):
        store = self.store()
        session = store.new_session(".", "p", "m")
        real = store.connect()

        class Boom:
            def execute(self, sql, *params):
                if sql.startswith("UPDATE sessions"):
                    raise sqlite3.OperationalError("boom")
                return real.execute(sql, *params)

        store._conn = Boom()
        with self.assertRaises(sqlite3.OperationalError):
            session.append(Msg(role="user", content="half"))
        store._conn = real
        count = real.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session.id,)).fetchone()[0]
        self.assertEqual(0, count, "the insert must not survive alone")

    def test_a_transient_locking_error_is_retried_not_recovered(self):
        store = self.store()
        store._TRANSIENT_PAUSES = (0,)
        real = store._open
        blows = [sqlite3.OperationalError("locking protocol")]

        def flaky():
            if blows:
                raise blows.pop()
            return real()

        store._open = flaky
        store.connect()
        self.assertFalse(Path(str(self.path) + "-wal.recovered").exists())

    def test_append_reopens_a_wedged_connection(self):
        store = self.store()
        session = store.new_session(".", "p", "m")
        session.append(Msg(role="user", content="first"))

        class Wedged:
            def execute(self, *args):
                raise sqlite3.OperationalError("disk I/O error")

            def close(self):
                pass

        store._conn = Wedged()
        seq = session.append(Msg(role="user", content="second"))
        self.assertEqual(2, seq)
        count = store.connect().execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session.id,)).fetchone()[0]
        self.assertEqual(2, count)


if __name__ == "__main__":
    unittest.main()
