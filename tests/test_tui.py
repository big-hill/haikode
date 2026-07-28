"""
Tests for the pure (curses-free) half of haikode.tui.

Nothing here touches a terminal: the TUI module deliberately keeps all
formatting in module-level functions and small dataclass-ish objects so the
tricky parts — wrapping, diff classification, truncation, cursor placement —
can be tested without a tty.
"""

import unittest

from haikode import keybind, tui
from haikode.keybind import KeyEvent
from haikode.palette import PaletteItem
from haikode.usage import ContextState
from haikode.tui import (Dialog, DialogAction, Entry, FormDialog, FormField,
                         Glyphs, Line, RenderOptions, Transcript,
                         agent_items, build_diff_lines, build_entry_lines,
                         build_status, build_todo_lines, classify_diff_line,
                         context_runs, context_text, cursor_row_index,
                         dialog_display_rows, dialog_footer_runs,
                         dialog_item_runs, dialog_view, form_view, format_age,
                         format_duration, format_tokens, highlight_runs,
                         layout_input, model_items, provider_items, sanitize,
                         session_items, summarize_tool, text_items,
                         truncate_lines, window_offset, wrap_text)

UNICODE = Glyphs(True)
ASCII = Glyphs(False)


def texts(lines):
    return [line.text for line in lines]


def styles(lines):
    return [line.style for line in lines]


class GlyphTests(unittest.TestCase):
    def test_detect_prefers_unicode_only_for_utf_encodings(self):
        self.assertTrue(Glyphs.detect("UTF-8").unicode_ok)
        self.assertTrue(Glyphs.detect("utf8").unicode_ok)
        self.assertFalse(Glyphs.detect("ascii").unicode_ok)
        self.assertFalse(Glyphs.detect("ANSI_X3.4-1968").unicode_ok)
        self.assertFalse(Glyphs.detect("").unicode_ok)

    def test_ascii_fallback_uses_plain_markers(self):
        self.assertEqual(ASCII.dot, "*")
        self.assertEqual(ASCII.ellipsis, "...")
        self.assertEqual(UNICODE.dot, "⏺")

    def test_spinner_frames_wrap_around(self):
        self.assertEqual(ASCII.frame(0), ASCII.frame(len(ASCII.spinner)))
        self.assertEqual(UNICODE.frame(3), UNICODE.spinner[3])


class SanitizeTests(unittest.TestCase):
    def test_control_characters_are_dropped(self):
        self.assertEqual(sanitize("a\x00b\x1b[31mc"), "ab[31mc")

    def test_tabs_become_spaces(self):
        self.assertEqual(sanitize("a\tb"), "a    b")

    def test_non_ascii_replaced_when_terminal_cannot_encode(self):
        self.assertEqual(sanitize("café ⏺", unicode_ok=False), "caf? ?")
        self.assertEqual(sanitize("café", unicode_ok=True), "café")

    def test_newlines_survive_so_wrapping_can_split_on_them(self):
        self.assertEqual(sanitize("a\r\nb\rc\nd"), "a\nb\nc\nd")

    def test_draw_helper_can_fold_newlines_away(self):
        self.assertEqual(sanitize("a\nb", keep_newlines=False), "a b")


class WrapTests(unittest.TestCase):
    def test_short_text_is_one_row(self):
        self.assertEqual(wrap_text("hello", 20), ["hello"])

    def test_greedy_wrap_never_exceeds_width(self):
        rows = wrap_text("the quick brown fox jumps over the lazy dog", 12)
        self.assertTrue(all(len(row) <= 12 for row in rows), rows)
        self.assertEqual(" ".join(row.strip() for row in rows),
                         "the quick brown fox jumps over the lazy dog")

    def test_prefixes_apply_to_first_and_following_rows(self):
        rows = wrap_text("hello world again", 12, "> ", "  ")
        self.assertEqual(rows[0][:2], "> ")
        self.assertTrue(all(row.startswith("  ") for row in rows[1:]), rows)
        self.assertTrue(all(len(row) <= 12 for row in rows))

    def test_explicit_newlines_and_blank_lines_survive(self):
        self.assertEqual(wrap_text("one\n\ntwo", 20), ["one", "", "two"])

    def test_empty_string_renders_one_blank_row(self):
        self.assertEqual(wrap_text("", 20), [""])

    def test_word_longer_than_width_is_hard_broken(self):
        rows = wrap_text("abcdefghijkl", 8, "> ", "  ")
        self.assertEqual(rows, ["> abcdef", "  ghijkl"])

    def test_source_indentation_is_preserved(self):
        rows = wrap_text("    indented", 40, "  ", "  ")
        self.assertEqual(rows, ["      indented"])

    def test_width_below_one_does_not_crash(self):
        self.assertTrue(wrap_text("abc", 0))

    def test_carriage_returns_are_normalised(self):
        self.assertEqual(wrap_text("a\r\nb", 20), ["a", "b"])

    def test_first_prefix_lands_on_the_first_non_blank_row(self):
        rows = wrap_text("\nhello", 20, "> ", "  ")
        self.assertEqual(rows, ["", "> hello"])


class TruncateTests(unittest.TestCase):
    def test_short_input_passes_through(self):
        self.assertEqual(truncate_lines(["a", "b"], 5), ["a", "b"])

    def test_exactly_at_limit_is_not_marked(self):
        self.assertEqual(truncate_lines(["a", "b"], 2), ["a", "b"])

    def test_overflow_is_folded_with_a_counter(self):
        lines = [str(n) for n in range(10)]
        self.assertEqual(truncate_lines(lines, 3),
                         ["0", "1", "2", "… +7 lines"])

    def test_single_dropped_line_is_singular(self):
        self.assertEqual(truncate_lines(["a", "b"], 1), ["a", "… +1 line"])

    def test_ascii_ellipsis_is_configurable(self):
        self.assertEqual(truncate_lines(["a", "b"], 1, ellipsis="..."),
                         ["a", "... +1 line"])

    def test_zero_limit_means_no_truncation(self):
        self.assertEqual(truncate_lines(["a", "b"], 0), ["a", "b"])


class DiffClassificationTests(unittest.TestCase):
    def test_headers_beat_the_bare_plus_and_minus_cases(self):
        self.assertEqual(classify_diff_line("--- a/src/main.py"), "diff_meta")
        self.assertEqual(classify_diff_line("+++ b/src/main.py"), "diff_meta")

    def test_hunk_header(self):
        self.assertEqual(classify_diff_line("@@ -1,3 +1,4 @@"), "diff_hunk")

    def test_added_and_removed(self):
        self.assertEqual(classify_diff_line("+new line"), "diff_add")
        self.assertEqual(classify_diff_line("-old line"), "diff_del")

    def test_context_and_empty(self):
        self.assertEqual(classify_diff_line(" unchanged"), "diff_ctx")
        self.assertEqual(classify_diff_line(""), "diff_ctx")
        self.assertEqual(classify_diff_line("no marker"), "diff_ctx")

    def test_git_metadata_lines(self):
        self.assertEqual(classify_diff_line("diff --git a/x b/x"), "diff_meta")
        self.assertEqual(classify_diff_line("index 1234..5678 100644"), "diff_meta")
        self.assertEqual(classify_diff_line("\\ No newline at end of file"),
                         "diff_meta")


class DiffRenderTests(unittest.TestCase):
    DIFF = ("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n"
            " keep\n-gone\n+added\n")

    def test_each_row_gets_its_own_style(self):
        lines = build_diff_lines(self.DIFF, 60, RenderOptions(UNICODE))
        self.assertEqual(styles(lines),
                         ["diff_meta", "diff_meta", "diff_hunk",
                          "diff_ctx", "diff_del", "diff_add"])

    def test_rows_are_indented_and_clipped_not_wrapped(self):
        lines = build_diff_lines("+" + "x" * 200, 30, RenderOptions(UNICODE))
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0].text), 30)
        self.assertTrue(lines[0].text.startswith("  +"))

    def test_long_diffs_are_folded(self):
        diff = "\n".join("+line %d" % n for n in range(40))
        lines = build_diff_lines(diff, 60, RenderOptions(UNICODE, diff_lines=5))
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[-1].style, "hint")
        self.assertIn("+35 lines", lines[-1].text)

    def test_expand_disables_folding(self):
        diff = "\n".join("+line %d" % n for n in range(40))
        lines = build_diff_lines(diff, 60,
                                 RenderOptions(UNICODE, expand=True, diff_lines=5))
        self.assertEqual(len(lines), 40)


class ToolSummaryTests(unittest.TestCase):
    def test_file_tools_show_the_path(self):
        self.assertEqual(summarize_tool("read", {"filePath": "src/main.py"}),
                         "src/main.py")
        self.assertEqual(summarize_tool("edit", {"filePath": "a.py",
                                                 "oldString": "x"}), "a.py")

    def test_bash_prefers_the_description_then_the_command(self):
        self.assertEqual(summarize_tool("bash", {"command": "ls -la",
                                                 "description": "List files"}),
                         "List files")
        self.assertEqual(summarize_tool("bash", {"command": "ls -la"}), "ls -la")

    def test_multiline_commands_collapse_to_one_line(self):
        self.assertEqual(summarize_tool("bash", {"command": "git   add .\ngit commit"}),
                         "git add .")

    def test_todowrite_counts_items(self):
        self.assertEqual(summarize_tool("todowrite", {"todos": [{}, {}]}), "2 items")
        self.assertEqual(summarize_tool("todowrite", {"todos": [{}]}), "1 item")

    def test_unknown_tool_falls_back_to_the_first_string_argument(self):
        self.assertEqual(summarize_tool("mystery", {"n": 3, "what": "hello"}), "hello")

    def test_no_usable_argument_yields_empty(self):
        self.assertEqual(summarize_tool("read", {}), "")

    def test_long_values_are_elided(self):
        summary = summarize_tool("bash", {"command": "x" * 300}, limit=20)
        self.assertEqual(len(summary), 20)
        self.assertTrue(summary.endswith("…"))


class EntryRenderTests(unittest.TestCase):
    def setUp(self):
        self.opts = RenderOptions(UNICODE)

    def render(self, entry, width=60, opts=None):
        return build_entry_lines(entry, width, opts or self.opts)

    def test_user_message_gets_an_accent_prefix(self):
        lines = self.render(Entry("user", text="fix the parser"))
        self.assertEqual(lines[0].style, "user")
        self.assertEqual(lines[0].text, "❯ fix the parser")

    def test_user_message_is_ascii_safe(self):
        lines = self.render(Entry("user", text="hi"), opts=RenderOptions(ASCII))
        self.assertEqual(lines[0].text, "> hi")

    def test_assistant_text_uses_the_default_style(self):
        lines = self.render(Entry("assistant", text="here you go"))
        self.assertEqual(styles(lines)[0], "assistant")
        self.assertEqual(lines[0].text, "here you go")

    def test_empty_assistant_entry_renders_nothing(self):
        self.assertEqual(self.render(Entry("assistant", text="   ")), [])

    def test_reasoning_is_hidden_unless_toggled_on(self):
        entry = Entry("reasoning", text="thinking hard")
        self.assertEqual(self.render(entry), [])
        entry.bump()
        shown = self.render(entry, opts=RenderOptions(UNICODE, show_reasoning=True))
        self.assertEqual(shown[0].style, "reasoning")
        self.assertEqual(shown[0].text, "  thinking hard")

    def test_tool_call_is_a_compact_one_liner(self):
        lines = self.render(Entry("tool", name="read", detail="src/main.py"))
        self.assertEqual(lines[0].text, "⏺ read  src/main.py")
        self.assertEqual(lines[0].style, "tool")

    def test_tool_call_one_liner_in_ascii(self):
        lines = self.render(Entry("tool", name="read", detail="src/main.py"),
                            opts=RenderOptions(ASCII))
        self.assertEqual(lines[0].text, "* read  src/main.py")

    def test_tool_result_is_indented_and_folded(self):
        entry = Entry("tool", name="bash", detail="ls",
                      output="\n".join("f%d" % n for n in range(20)))
        lines = self.render(entry, opts=RenderOptions(UNICODE, result_lines=4))
        self.assertEqual(lines[0].text, "⏺ bash  ls")
        self.assertEqual(texts(lines)[1:5], ["  f0", "  f1", "  f2", "  f3"])
        self.assertEqual(lines[5].style, "hint")
        self.assertIn("+16 lines", lines[5].text)

    def test_tool_result_with_a_diff_renders_the_diff(self):
        entry = Entry("tool", name="edit", detail="x.py",
                      diff="@@ -1 +1 @@\n-a\n+b\n", output="ignored")
        lines = self.render(entry)
        self.assertEqual(styles(lines)[1:4], ["diff_hunk", "diff_del", "diff_add"])

    def test_tool_error_is_red(self):
        lines = self.render(Entry("tool", name="edit", detail="x.py",
                                  error="File not found"))
        self.assertEqual(lines[1].style, "error")
        self.assertIn("File not found", lines[1].text)

    def test_tool_denial_is_red(self):
        lines = self.render(Entry("tool", name="bash", detail="rm -rf /",
                                  denied="User rejected"))
        self.assertEqual(lines[1].style, "denied")
        self.assertIn("User rejected", lines[1].text)

    def test_error_entry(self):
        lines = self.render(Entry("error", text="boom"))
        self.assertEqual(lines[0].style, "error")
        self.assertIn("boom", lines[0].text)

    def test_info_entry_can_override_its_style(self):
        lines = self.render(Entry("info", text="haikode", name="header"))
        self.assertEqual(lines[0].style, "header")

    def test_every_non_empty_entry_is_followed_by_a_spacer(self):
        lines = self.render(Entry("assistant", text="hi"))
        self.assertEqual(lines[-1].text, "")

    def test_unknown_kind_degrades_to_info(self):
        lines = self.render(Entry("something-new", text="hello"))
        self.assertEqual(lines[0].text, "hello")


class TranscriptTests(unittest.TestCase):
    def test_lines_concatenate_entries_in_order(self):
        transcript = Transcript()
        transcript.add(Entry("user", text="a"))
        transcript.add(Entry("assistant", text="b"))
        rendered = texts(transcript.lines(40, RenderOptions(ASCII)))
        self.assertEqual(rendered, ["> a", "", "b", ""])

    def test_cache_is_reused_and_invalidated_by_width(self):
        transcript = Transcript()
        transcript.add(Entry("assistant", text="hello world"))
        opts = RenderOptions(ASCII)
        first = transcript.lines(40, opts)
        self.assertIs(transcript.lines(40, opts), first)
        self.assertIsNot(transcript.lines(8, opts), first)

    def test_streaming_text_invalidates_the_cache(self):
        transcript = Transcript()
        entry = transcript.add(Entry("assistant", text="hel"))
        opts = RenderOptions(ASCII)
        self.assertEqual(texts(transcript.lines(40, opts))[0], "hel")
        entry.append_text("lo")
        transcript.invalidate()
        self.assertEqual(texts(transcript.lines(40, opts))[0], "hello")

    def test_toggling_options_changes_the_cache_key(self):
        transcript = Transcript()
        transcript.add(Entry("reasoning", text="why"))
        self.assertEqual(transcript.lines(40, RenderOptions(ASCII)), [])
        shown = transcript.lines(40, RenderOptions(ASCII, show_reasoning=True))
        self.assertEqual(texts(shown), ["  why", ""])

    def test_entry_limit_drops_the_oldest(self):
        transcript = Transcript(limit=3)
        for n in range(6):
            transcript.add(Entry("assistant", text=str(n)))
        self.assertEqual([e.text for e in transcript.entries], ["3", "4", "5"])

    def test_clear_empties_the_transcript(self):
        transcript = Transcript()
        transcript.add(Entry("user", text="a"))
        transcript.clear()
        self.assertEqual(transcript.lines(40, RenderOptions(ASCII)), [])


class InputLayoutTests(unittest.TestCase):
    def test_empty_buffer_places_the_cursor_after_the_prompt(self):
        layout = layout_input("", 0, 40)
        self.assertEqual(layout.rows, ["> "])
        self.assertEqual((layout.cursor_row, layout.cursor_col), (0, 2))

    def test_cursor_tracks_the_end_of_the_text(self):
        layout = layout_input("hello", 5, 40)
        self.assertEqual(layout.rows, ["> hello"])
        self.assertEqual((layout.cursor_row, layout.cursor_col), (0, 7))

    def test_explicit_newlines_become_continuation_rows(self):
        layout = layout_input("a\nbb", 4, 40)
        self.assertEqual(layout.rows, ["> a", "  bb"])
        self.assertEqual((layout.cursor_row, layout.cursor_col), (1, 4))

    def test_long_lines_wrap_and_stay_inside_the_width(self):
        layout = layout_input("x" * 30, 30, 20)
        self.assertTrue(all(len(row) <= 20 for row in layout.rows), layout.rows)
        self.assertTrue(layout.cursor_col < 20)

    def test_cursor_never_leaves_the_visible_window(self):
        layout = layout_input("\n".join("line%d" % n for n in range(20)),
                              len("line0\n") * 19, 40, max_rows=4)
        self.assertEqual(len(layout.rows), 4)
        self.assertTrue(0 <= layout.cursor_row < 4)

    def test_cursor_in_the_middle_of_the_first_line(self):
        layout = layout_input("hello", 2, 40)
        self.assertEqual((layout.cursor_row, layout.cursor_col), (0, 4))

    def test_narrow_width_does_not_crash(self):
        layout = layout_input("hello world", 11, 4)
        self.assertTrue(layout.rows)
        self.assertTrue(layout.cursor_col >= 0)


class FormattingTests(unittest.TestCase):
    def test_token_formatting(self):
        self.assertEqual(format_tokens(0), "0")
        self.assertEqual(format_tokens(940), "940")
        self.assertEqual(format_tokens(1000), "1k")
        self.assertEqual(format_tokens(1234), "1.2k")
        self.assertEqual(format_tokens(1000000), "1M")
        self.assertEqual(format_tokens(None), "0")

    def test_duration_formatting(self):
        self.assertEqual(format_duration(3), "3s")
        self.assertEqual(format_duration(65), "1m05s")
        self.assertEqual(format_duration(3725), "1h02m")
        self.assertEqual(format_duration(-1), "0s")

    def test_status_fits_the_width_and_shows_the_interrupt_hint(self):
        line = build_status("openai/gpt", "haikode", 1234, 56, 80, UNICODE,
                            busy=True, frame=0, elapsed=5)
        self.assertEqual(len(line), 80)
        self.assertIn("openai/gpt", line)
        self.assertIn("haikode", line)
        self.assertIn("1.2k in 56 out", line)
        self.assertIn("esc to interrupt", line)

    def test_idle_status_says_ready(self):
        line = build_status("p", "d", 0, 0, 60, ASCII)
        self.assertIn("ready", line)

    def test_hint_replaces_the_busy_indicator(self):
        line = build_status("p", "d", 0, 0, 60, ASCII, busy=True,
                            hint="ctrl-c again to exit")
        self.assertIn("ctrl-c again to exit", line)
        self.assertNotIn("esc to interrupt", line)

    def test_narrow_status_drops_segments_instead_of_overflowing(self):
        line = build_status("a-very-long-provider/model-name", "somedir",
                            123456, 654321, 30, ASCII)
        self.assertEqual(len(line), 30)


def make_tui(completer=None):
    """A TUI that never attaches to a terminal — __init__ touches no curses."""
    return tui.TUI(lambda: None, config=None, cwd=".", completer=completer)


class CompletionTests(unittest.TestCase):
    """Tab completion replaces the word under the cursor and nothing else."""

    def test_word_under_the_cursor_is_replaced(self):
        ui = make_tui(lambda prefix: ["/known"])
        ui.buffer, ui.cursor = "/kn", 3
        ui._complete()
        self.assertEqual(ui.buffer, "/known")
        self.assertEqual(ui.cursor, 6)

    def test_completion_after_a_newline_keeps_the_previous_line(self):
        # Regression: split()-based token detection grabbed the last word of
        # the previous line and the replacement ate the newline with it.
        ui = make_tui(lambda prefix: ["/known"])
        ui.buffer, ui.cursor = "write a file\n", 13
        ui._complete()
        self.assertEqual(ui.buffer, "write a file\n/known")

    def test_completion_after_a_space_appends_rather_than_replaces(self):
        ui = make_tui(lambda prefix: ["/known"])
        ui.buffer, ui.cursor = "foo bar ", 8
        ui._complete()
        self.assertEqual(ui.buffer, "foo bar /known")

    def test_ambiguous_matches_only_extend_to_the_common_prefix(self):
        ui = make_tui(lambda prefix: ["/known", "/knight"])
        ui.buffer, ui.cursor = "line1\n/kn", 9
        ui._complete()
        self.assertEqual(ui.buffer, "line1\n/kn")

    def test_a_failing_completer_never_reaches_the_user(self):
        ui = make_tui(lambda prefix: 1 / 0)
        ui.buffer, ui.cursor = "/kn", 3
        ui._complete()
        self.assertEqual(ui.buffer, "/kn")


class ModuleContractTests(unittest.TestCase):
    def test_run_tui_is_exported_with_the_documented_signature(self):
        import inspect
        params = list(inspect.signature(tui.run_tui).parameters)
        self.assertEqual(params, ["agent_factory", "config", "cwd",
                                  "on_command", "completer", "header",
                                  "agent", "turn"])

    def test_tui_constructor_signature(self):
        import inspect
        params = list(inspect.signature(tui.TUI.__init__).parameters)
        self.assertEqual(params, ["self", "agent_factory", "config", "cwd",
                                  "on_command", "completer", "header",
                                  "agent", "turn"])

    def test_a_supplied_agent_is_adopted_rather_than_rebuilt(self):
        """--continue resumes into an agent; the factory would hand back an
        empty one, so startup must never call it when an agent was given."""
        sentinel = object()
        ui = tui.TUI(lambda: (_ for _ in ()).throw(AssertionError("rebuilt")),
                     config=None, cwd=".", agent=sentinel)
        self.assertIs(ui.agent, sentinel)

    def test_unavailable_is_a_runtime_error_callers_can_catch(self):
        self.assertTrue(issubclass(tui.TUIUnavailable, RuntimeError))

    def test_line_equality_helps_assertions(self):
        self.assertEqual(Line("a", "user"), Line("a", "user"))
        self.assertNotEqual(Line("a", "user"), Line("a", "tool"))


class ContextMeterTests(unittest.TestCase):
    """The meter opencode draws beside its prompt (prompt/index.tsx ~line 275)."""

    def state(self, used, window=100):
        return ContextState(used=used, window=window)

    def test_full_label_when_it_fits(self):
        self.assertEqual(context_text(self.state(50), 40), "50/100 (50%)")

    def test_falls_back_to_the_percentage_when_narrow(self):
        self.assertEqual(context_text(self.state(50), 6), "50%")

    def test_nothing_at_all_when_even_the_percentage_will_not_fit(self):
        self.assertEqual(context_text(self.state(50), 2), "")

    def test_unknown_window_shows_the_raw_count(self):
        self.assertEqual(context_text(ContextState(used=1500, window=0), 20), "1.5k")

    def test_none_state_is_not_an_error(self):
        self.assertEqual(context_text(None, 30), "")
        self.assertEqual(context_runs(None, 30), [])

    def test_runs_carry_the_bar_and_the_numbers(self):
        runs = context_runs(self.state(50), 30, UNICODE)
        self.assertEqual(len(runs), 2)
        self.assertTrue(runs[0][0].startswith("["))
        self.assertIn("50/100", runs[1][0])

    def test_pressure_picks_the_style(self):
        self.assertEqual(context_runs(self.state(10), 30)[0][1], "ctx_ok")
        self.assertEqual(context_runs(self.state(70), 30)[0][1], "ctx_warn")
        self.assertEqual(context_runs(self.state(95), 30)[0][1], "ctx_critical")

    def test_all_runs_share_one_pressure_style(self):
        styles = {style for _, style in context_runs(self.state(95), 30)}
        self.assertEqual(styles, {"ctx_critical"})

    def test_bar_is_dropped_before_the_numbers_are(self):
        runs = context_runs(self.state(50), 13)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][0], "50/100 (50%)")

    def test_meter_never_exceeds_the_width_it_was_given(self):
        for width in range(0, 40):
            total = sum(len(text) for text, _ in context_runs(self.state(63), width))
            self.assertLessEqual(total, width)


class FooterTests(unittest.TestCase):
    """One row, everything on it, and it never grows past the width."""

    def test_agent_and_context_are_shown(self):
        line = build_status("openai/gpt", "proj", 10, 20, 100, UNICODE,
                            agent="plan", context="1.2k/128k (1%)")
        self.assertIn("plan", line)
        self.assertIn("1.2k/128k (1%)", line)
        self.assertIn("10 in 20 out", line)

    def test_leader_indicator_is_visible_while_armed(self):
        line = build_status("p", "d", 0, 0, 90, ASCII, leader="ctrl+x...")
        self.assertIn("ctrl+x...", line)

    def test_agent_is_dropped_before_the_provider_is(self):
        line = build_status("openai/gpt-4o-mini", "some-project", 1000, 2000,
                            56, ASCII, agent="plan", context="1k/128k (1%)")
        self.assertEqual(len(line), 56)
        self.assertIn("openai/gpt-4o-mini", line)
        self.assertNotIn("plan", line)

    def test_leader_survives_a_narrow_footer(self):
        line = build_status("openai/gpt-4o-mini", "project", 1000, 2000, 44,
                            ASCII, agent="plan", leader="^X...")
        self.assertEqual(len(line), 44)
        self.assertIn("^X...", line)

    def test_every_width_produces_exactly_that_many_columns(self):
        for width in range(20, 120):
            line = build_status("openai/gpt-4o-mini", "project", 12345, 678,
                                width, UNICODE, agent="build",
                                context="12.3k/128k (10%)", leader="ctrl+x…")
            self.assertEqual(len(line), width)


class TodoBlockTests(unittest.TestCase):
    """todowrite output renders as opencode's checklist, not as raw JSON."""

    TODOS = [
        {"content": "read the file", "status": "completed"},
        {"content": "write the patch", "status": "in_progress"},
        {"content": "run the tests", "status": "pending"},
        {"content": "ship it", "status": "cancelled"},
    ]

    def opts(self, expand=False):
        return RenderOptions(glyphs=UNICODE, expand=expand)

    def test_markers_match_the_status(self):
        lines = build_todo_lines(self.TODOS, 60, self.opts())
        self.assertEqual(texts(lines), [
            "  [✔] read the file",
            "  [•] write the patch",
            "  [ ] run the tests",
            "  [✘] ship it",
        ])

    def test_running_item_is_highlighted_and_done_items_are_not(self):
        styles_by_row = styles(build_todo_lines(self.TODOS, 60, self.opts()))
        self.assertEqual(styles_by_row,
                         ["diff_add", "warn", "result", "hint"])

    def test_ascii_terminals_get_ascii_markers(self):
        lines = build_todo_lines(self.TODOS[:1], 60,
                                 RenderOptions(glyphs=ASCII))
        self.assertEqual(texts(lines), ["  [+] read the file"])

    def test_long_content_wraps_under_the_marker(self):
        todos = [{"content": "a " * 30, "status": "pending"}]
        lines = build_todo_lines(todos, 30, self.opts())
        self.assertGreater(len(lines), 1)
        self.assertTrue(lines[1].text.startswith("      "))

    def test_junk_entries_are_skipped_not_raised(self):
        todos = ["nonsense", {"status": "pending"}, {"content": "ok",
                                                     "status": "pending"}]
        self.assertEqual(texts(build_todo_lines(todos, 40, self.opts())),
                         ["  [ ] ok"])

    def test_a_very_long_plan_is_folded_and_expand_unfolds_it(self):
        todos = [{"content": "step %d" % n, "status": "pending"}
                 for n in range(30)]
        folded = build_todo_lines(todos, 40, self.opts())
        self.assertEqual(len(folded), tui.TODO_LINES + 1)
        self.assertIn("+14 lines", folded[-1].text)
        self.assertEqual(len(build_todo_lines(todos, 40, self.opts(True))), 30)

    def test_a_todowrite_entry_renders_its_checklist_instead_of_output(self):
        entry = Entry("tool", name="todowrite", detail="1 item",
                      output='[{"content": "x", "status": "pending"}]',
                      meta={"todos": [{"content": "x", "status": "pending"}]})
        rows = texts(build_entry_lines(entry, 60, self.opts()))
        self.assertIn("  [ ] x", rows)
        self.assertNotIn('[{"content": "x", "status": "pending"}]', rows)

    def test_a_failed_todowrite_still_shows_its_error(self):
        entry = Entry("tool", name="todowrite", error="boom",
                      meta={"todos": [{"content": "x", "status": "pending"}]})
        rows = texts(build_entry_lines(entry, 60, self.opts()))
        self.assertTrue(any("boom" in row for row in rows))


class ExpandTests(unittest.TestCase):
    """ctrl+o toggles RenderOptions.expand, which unfolds tool output."""

    def entry(self):
        return Entry("tool", name="read", detail="a.py",
                     output="\n".join("line %d" % n for n in range(40)))

    def test_output_is_folded_by_default(self):
        lines = build_entry_lines(self.entry(), 40, RenderOptions(glyphs=UNICODE))
        self.assertIn("  … +32 lines", texts(lines))

    def test_expand_shows_everything(self):
        lines = build_entry_lines(self.entry(), 40,
                                  RenderOptions(glyphs=UNICODE, expand=True))
        self.assertNotIn("  … +32 lines", texts(lines))
        self.assertIn("  line 39", texts(lines))

    def test_the_toggle_flips_and_invalidates_the_cache(self):
        ui = make_tui()
        ui.transcript.add(self.entry())
        before = len(ui.transcript.lines(40, ui.opts))
        ui._toggle_expand()
        self.assertTrue(ui.opts.expand)
        self.assertGreater(len(ui.transcript.lines(40, ui.opts)), before)
        ui._toggle_expand()
        self.assertFalse(ui.opts.expand)


# --------------------------------------------------------------------------
# dialogs
# --------------------------------------------------------------------------


def items(*titles, **kwargs):
    category = kwargs.get("category", "")
    return [PaletteItem(id=title, title=title, category=category, value=title)
            for title in titles]


class DialogItemTests(unittest.TestCase):
    """Each dialog is the same component; only its item list differs."""

    def test_model_items_keep_the_catalogue_sections(self):
        from haikode.models import ModelRef
        rows = model_items(
            [ModelRef("anthropic", "claude", category="Favourites"),
             ModelRef("openai", "gpt-4o", category="openai", free=True)],
            favourites=[ModelRef("anthropic", "claude")],
            current="openai/gpt-4o")
        self.assertEqual([item.category for item in rows],
                         ["Favourites", "openai"])
        self.assertEqual(rows[0].footer, "*")          # favourite marker
        self.assertEqual(rows[1].footer, "Free")
        self.assertEqual(rows[1].detail, "current")
        self.assertEqual(rows[0].id, "anthropic/claude")

    def test_provider_items_report_auth_and_add_an_entry(self):
        rows = provider_items([
            {"name": "openai", "auth": "key from keystore", "auth_ok": True,
             "is_default": True, "model": "gpt-4o", "category": "Popular",
             "base_url": "https://api.openai.com/v1"},
            {"name": "local", "auth": "no key set", "auth_ok": False,
             "is_default": False, "model": "", "category": "Providers",
             "base_url": "http://localhost"},
        ])
        self.assertEqual([item.title for item in rows],
                         ["openai", "local", "Add provider"])
        self.assertEqual(rows[0].footer, "default")
        self.assertEqual(rows[1].footer, "no key")
        self.assertEqual(rows[2].value, "__add__")

    def test_provider_items_can_omit_the_add_entry(self):
        self.assertEqual(provider_items([], add_entry=False), [])

    def test_session_items_use_the_search_snippet_when_there_is_one(self):
        rows = session_items([
            {"id": "abc", "title": "Fix the parser", "message_count": 4,
             "updated": 0, "model": "gpt-4o"},
            {"id": "def", "title": "", "message_count": 1, "updated": 0,
             "snippet": "...the parser bug..."},
        ], current="abc")
        self.assertEqual(rows[0].detail, "4 messages")
        self.assertEqual(rows[0].footer, "current")
        self.assertEqual(rows[1].title, "(untitled)")
        self.assertEqual(rows[1].detail, "...the parser bug...")

    def test_agent_items_flag_read_only_agents(self):
        class Fake:
            def __init__(self, name, description=""):
                self.name = name
                self.description = description

        rows = agent_items([Fake("build", "does everything"), Fake("plan")],
                           current="build", readonly=["plan"])
        self.assertEqual(rows[0].footer, "current")
        self.assertEqual(rows[1].footer, "read-only")
        self.assertEqual(rows[0].detail, "does everything")

    def test_text_items_are_a_read_only_pane(self):
        rows = text_items(["a", "b"])
        self.assertTrue(all(item.disabled for item in rows))
        self.assertEqual([item.title for item in rows], ["a", "b"])

    def test_format_age_is_relative_and_compact(self):
        import time as _time
        now = _time.time()
        self.assertEqual(format_age(now), "now")
        self.assertEqual(format_age(now - 600), "10m")
        self.assertEqual(format_age(now - 7200), "2h")
        self.assertEqual(format_age(now - 86400 * 3), "3d")
        self.assertEqual(format_age(0), "")
        self.assertEqual(format_age("nonsense"), "")


class DialogFilterTests(unittest.TestCase):
    def dialog(self, **kwargs):
        return Dialog("test", "Test",
                      items("New session", "List models", "List sessions"),
                      **kwargs)

    def test_typing_narrows_the_list(self):
        dialog = self.dialog()
        self.assertEqual(dialog.handle(KeyEvent(key="m"), keybind.Keymap()),
                         tui.DIALOG_QUERY)
        self.assertEqual(dialog.text, "m")
        self.assertEqual([item.title for item, _ in dialog.select.matches],
                         ["List models"])

    def test_shifted_letters_keep_their_case(self):
        dialog = self.dialog()
        dialog.handle(KeyEvent(key="n", shift=True), keybind.Keymap())
        self.assertEqual(dialog.text, "N")

    def test_backspace_widens_it_again(self):
        dialog = self.dialog()
        dialog.set_text("mo")
        dialog.handle(KeyEvent(key="backspace"), keybind.Keymap())
        self.assertEqual(dialog.text, "m")
        dialog.handle(KeyEvent(key="backspace"), keybind.Keymap())
        self.assertEqual(dialog.select.count, 3)

    def test_ctrl_u_clears_the_filter(self):
        dialog = self.dialog()
        dialog.set_text("models")
        dialog.handle(KeyEvent(key="u", ctrl=True), keybind.Keymap())
        self.assertEqual(dialog.text, "")

    def test_an_unfiltered_dialog_types_without_filtering(self):
        dialog = self.dialog(filtered=False)
        dialog.handle(KeyEvent(key="z"), keybind.Keymap())
        self.assertEqual(dialog.text, "z")
        self.assertEqual(dialog.select.count, 3)   # the query went elsewhere

    def test_a_menu_ignores_typing_entirely(self):
        dialog = self.dialog(mode="menu")
        self.assertEqual(dialog.handle(KeyEvent(key="m"), keybind.Keymap()),
                         tui.DIALOG_IGNORED)
        self.assertEqual(dialog.text, "")

    def test_filtering_moves_the_cursor_back_to_the_top(self):
        dialog = self.dialog()
        dialog.select.move(2)
        self.assertEqual(dialog.select.cursor, 2)
        dialog.set_text("list")
        self.assertEqual(dialog.select.cursor, 0)

    def test_empty_message_explains_a_dead_filter(self):
        dialog = self.dialog()
        dialog.set_text("zzzz")
        self.assertIn("No matches", dialog.select.empty_message)


class DialogKeyTests(unittest.TestCase):
    def setUp(self):
        self.keymap = keybind.Keymap()
        self.dialog = Dialog("test", "Test", items("a", "b", "c"))

    def test_arrow_and_ctrl_n_both_move(self):
        self.dialog.handle(KeyEvent(key="down"), self.keymap)
        self.assertEqual(self.dialog.select.cursor, 1)
        self.dialog.handle(KeyEvent(key="n", ctrl=True), self.keymap)
        self.assertEqual(self.dialog.select.cursor, 2)
        self.dialog.handle(KeyEvent(key="p", ctrl=True), self.keymap)
        self.assertEqual(self.dialog.select.cursor, 1)

    def test_home_and_end(self):
        self.dialog.handle(KeyEvent(key="end"), self.keymap)
        self.assertEqual(self.dialog.select.cursor, 2)
        self.dialog.handle(KeyEvent(key="home"), self.keymap)
        self.assertEqual(self.dialog.select.cursor, 0)

    def test_page_keys_clamp_instead_of_wrapping(self):
        self.dialog.select.page_size = 2
        self.dialog.handle(KeyEvent(key="pageup"), self.keymap)
        self.assertEqual(self.dialog.select.cursor, 0)
        self.dialog.handle(KeyEvent(key="pagedown"), self.keymap)
        self.assertEqual(self.dialog.select.cursor, 2)

    def test_enter_submits_and_escape_cancels(self):
        self.assertEqual(self.dialog.handle(KeyEvent(key="return"), self.keymap),
                         tui.DIALOG_SUBMIT)
        self.assertEqual(self.dialog.handle(KeyEvent(key="escape"), self.keymap),
                         tui.DIALOG_CANCEL)

    def test_actions_come_back_by_their_command_name(self):
        dialog = Dialog("models", "Models", items("a"),
                        actions=[DialogAction("model_favorite_toggle", "Fav")])
        self.assertEqual(dialog.handle(KeyEvent(key="f", ctrl=True), self.keymap),
                         "model_favorite_toggle")

    def test_an_action_not_declared_by_this_dialog_is_not_fired(self):
        # ctrl+f is model_favorite_toggle, but this dialog never asked for it.
        self.assertEqual(self.dialog.handle(KeyEvent(key="f", ctrl=True),
                                            self.keymap), tui.DIALOG_IGNORED)

    def test_a_dialog_never_arms_the_leader(self):
        self.dialog.handle(KeyEvent(key="x", ctrl=True), self.keymap)
        self.assertFalse(self.keymap.leader_pending)

    def test_submit_on_an_empty_list_is_swallowed(self):
        dialog = Dialog("empty", "Empty", [])
        self.assertEqual(dialog.handle(KeyEvent(key="return"), self.keymap),
                         tui.DIALOG_CONSUMED)

    def test_disabled_rows_are_never_selected(self):
        dialog = Dialog("x", "X", [
            PaletteItem(id="a", title="a", disabled=True, value="a"),
            PaletteItem(id="b", title="b", value="b")])
        self.assertEqual(dialog.selected.id, "b")


class DialogRenderTests(unittest.TestCase):
    def build(self, **kwargs):
        rows = [PaletteItem(id="new", title="New session",
                            description="Start fresh", category="Session",
                            footer="ctrl+x n", value="new"),
                PaletteItem(id="list", title="List models",
                            description="Switch model", category="Model",
                            value="list")]
        return Dialog("commands", "Commands", rows, **kwargs)

    def test_the_frame_is_title_filter_blank_list_footer(self):
        view = dialog_view(self.build(), 50, 10, UNICODE, keybind.Keymap())
        first = "".join(text for text, _ in view.rows[0])
        self.assertTrue(first.startswith("Commands"))
        self.assertTrue(first.rstrip().endswith("esc"))
        self.assertEqual("".join(t for t, _ in view.rows[1]), "❯ Search")
        self.assertEqual(view.rows[2], [])
        self.assertIn("Session", "".join(t for t, _ in view.rows[3]))

    def test_the_cursor_sits_after_the_typed_filter(self):
        dialog = self.build()
        dialog.set_text("mod")
        view = dialog_view(dialog, 50, 10, UNICODE)
        self.assertEqual(view.cursor, (1, 5))

    def test_a_pane_has_no_filter_line_and_no_cursor(self):
        view = dialog_view(Dialog("s", "Status", text_items(["a", "b"]),
                                  mode="pane"), 40, 8, UNICODE)
        self.assertIsNone(view.cursor)
        self.assertEqual("".join(t for t, _ in view.rows[1]), "a")

    def test_never_more_rows_than_the_height_allows(self):
        dialog = Dialog("x", "X", items(*["item %d" % n for n in range(50)]))
        for height in range(1, 20):
            view = dialog_view(dialog, 40, height, UNICODE)
            self.assertLessEqual(len(view.rows), height)

    def test_rows_never_exceed_the_width(self):
        dialog = self.build()
        for width in range(10, 60):
            for row in dialog_view(dialog, width, 12, UNICODE).rows:
                self.assertLessEqual(sum(len(t) for t, _ in row), width)

    def test_empty_list_draws_its_message(self):
        view = dialog_view(Dialog("x", "X", [], empty="No saved sessions"),
                           40, 8, UNICODE)
        self.assertIn("No saved sessions",
                      "".join(t for t, _ in view.rows[3]))

    def test_footer_shows_actions_and_a_counter(self):
        dialog = self.build(actions=[DialogAction("model_favorite_toggle",
                                                  "Favourite")])
        runs = dialog_footer_runs(dialog, 60, UNICODE, keybind.Keymap())
        text = "".join(t for t, _ in runs)
        self.assertIn("Favourite ctrl+f", text)
        self.assertTrue(text.rstrip().endswith("1/2"))

    def test_footer_message_replaces_the_counter(self):
        dialog = self.build()
        dialog.message = "favourited"
        runs = dialog_footer_runs(dialog, 60, UNICODE, keybind.Keymap())
        self.assertIn("favourited", "".join(t for t, _ in runs))

    def test_an_unbound_action_gets_no_hint(self):
        dialog = self.build(actions=[DialogAction("mcp_list", "MCP")])
        self.assertNotIn("MCP", "".join(
            t for t, _ in dialog_footer_runs(dialog, 60, UNICODE,
                                             keybind.Keymap())))


class HighlightTests(unittest.TestCase):
    def test_matched_positions_get_their_own_style(self):
        runs = highlight_runs("model", [0, 1], "base", "match")
        self.assertEqual(runs, [("mo", "match"), ("del", "base")])

    def test_no_positions_is_a_single_run(self):
        self.assertEqual(highlight_runs("model", [], "base", "match"),
                         [("model", "base")])

    def test_positions_past_the_end_are_ignored(self):
        self.assertEqual(highlight_runs("ab", [5], "base", "match"),
                         [("ab", "base")])

    def test_a_filtered_dialog_highlights_what_matched(self):
        dialog = Dialog("x", "X", items("model list", "session list"))
        dialog.set_text("mod")
        view = dialog_view(dialog, 40, 8, UNICODE)
        row = view.rows[3]
        self.assertIn(("mod", "dialog_sel_match"), row)


class DialogItemRowTests(unittest.TestCase):
    ITEM = PaletteItem(id="a", title="List models", description="",
                       detail="Switch the active model", footer="ctrl+x m",
                       value="a")

    def test_selected_rows_are_painted_edge_to_edge(self):
        runs = dialog_item_runs(self.ITEM, [], 60, UNICODE, selected=True)
        self.assertEqual(sum(len(t) for t, _ in runs), 60)
        self.assertTrue(runs[0][0].startswith("❯"))

    def test_the_current_item_gets_a_dot(self):
        runs = dialog_item_runs(self.ITEM, [], 60, UNICODE, current=True)
        self.assertTrue(runs[0][0].startswith("⏺"))

    def test_a_pane_row_has_no_marker(self):
        runs = dialog_item_runs(PaletteItem(id="a", title="hello"), [], 20,
                                UNICODE, marker=False)
        self.assertEqual(runs[0][0], "hello")

    def test_title_column_lines_the_details_up(self):
        short = dialog_item_runs(PaletteItem(id="a", title="ab", detail="x"),
                                 [], 60, UNICODE, title_width=12)
        long = dialog_item_runs(PaletteItem(id="b", title="abcdefgh",
                                            detail="y"), [], 60, UNICODE,
                                title_width=12)
        self.assertEqual(len(short[0][0]) + len(short[1][0]) + len(short[2][0])
                         - len(short[2][0].lstrip()),
                         len(long[0][0]) + len(long[1][0]) + len(long[2][0])
                         - len(long[2][0].lstrip()))

    def test_the_right_hand_tag_is_right_aligned(self):
        runs = dialog_item_runs(self.ITEM, [], 60, UNICODE)
        self.assertTrue("".join(t for t, _ in runs).endswith("ctrl+x m"))

    def test_a_hopelessly_narrow_row_renders_nothing(self):
        self.assertEqual(dialog_item_runs(self.ITEM, [], 3, UNICODE), [])

    def test_disabled_rows_are_muted(self):
        runs = dialog_item_runs(PaletteItem(id="a", title="x", disabled=True),
                                [], 20, UNICODE)
        self.assertEqual(runs[0][1], "hint")


class DialogWindowTests(unittest.TestCase):
    def rows(self):
        dialog = Dialog("x", "X", [
            PaletteItem(id="a", title="a", category="One", value="a"),
            PaletteItem(id="b", title="b", category="One", value="b"),
            PaletteItem(id="c", title="c", category="Two", value="c")])
        return dialog_display_rows(dialog)

    def test_headers_are_interleaved_in_match_order(self):
        self.assertEqual([kind for kind, _ in self.rows()],
                         ["header", "item", "item", "header", "item"])

    def test_cursor_row_index_points_at_the_item_not_the_header(self):
        self.assertEqual(cursor_row_index(self.rows(), 2), 4)

    def test_scrolling_moves_as_little_as_possible(self):
        rows = [("item", n) for n in range(20)]
        self.assertEqual(window_offset(rows, 0, 5, 0), 0)
        self.assertEqual(window_offset(rows, 7, 5, 0), 3)
        self.assertEqual(window_offset(rows, 2, 5, 5), 2)
        self.assertEqual(window_offset(rows, 19, 5, 0), 15)

    def test_a_section_header_is_pulled_into_view_with_its_first_item(self):
        rows = [("header", "h")] + [("item", n) for n in range(5)]
        self.assertEqual(window_offset(rows, 1, 3, 1), 0)

    def test_but_never_on_a_one_row_window(self):
        rows = [("header", "h"), ("item", 0)]
        self.assertEqual(window_offset(rows, 1, 1, 1), 1)

    def test_offset_is_clamped_to_the_list(self):
        rows = [("item", n) for n in range(3)]
        self.assertEqual(window_offset(rows, 0, 10, 99), 0)


class FormDialogTests(unittest.TestCase):
    def form(self):
        return FormDialog("add", "Add provider", [
            FormField("name", "Name"),
            FormField("base_url", "Base URL", "https://"),
            FormField("requires_key", "Needs key", "yes", kind="bool")])

    def test_typing_goes_into_the_active_field(self):
        form = self.form()
        keymap = keybind.Keymap()
        for char in "zed":
            form.handle(KeyEvent(key=char), keymap)
        self.assertEqual(form.values()["name"], "zed")

    def test_down_moves_between_fields_and_wraps(self):
        form = self.form()
        keymap = keybind.Keymap()
        form.handle(KeyEvent(key="down"), keymap)
        self.assertEqual(form.field.name, "base_url")
        form.handle(KeyEvent(key="up"), keymap)
        self.assertEqual(form.field.name, "name")
        form.handle(KeyEvent(key="up"), keymap)
        self.assertEqual(form.field.name, "requires_key")

    def test_space_toggles_a_boolean_field(self):
        form = self.form()
        keymap = keybind.Keymap()
        form.index = 2
        self.assertTrue(form.values()["requires_key"])
        form.handle(KeyEvent(key="space"), keymap)
        self.assertFalse(form.values()["requires_key"])

    def test_typing_never_lands_in_a_boolean_field(self):
        form = self.form()
        form.index = 2
        form.handle(KeyEvent(key="a"), keybind.Keymap())
        self.assertEqual(form.fields[2].value, "yes")

    def test_enter_submits_and_escape_cancels(self):
        form = self.form()
        keymap = keybind.Keymap()
        self.assertEqual(form.handle(KeyEvent(key="return"), keymap),
                         tui.DIALOG_SUBMIT)
        self.assertEqual(form.handle(KeyEvent(key="escape"), keymap),
                         tui.DIALOG_CANCEL)

    def test_backspace_edits_the_active_field(self):
        form = self.form()
        form.index = 1
        form.handle(KeyEvent(key="backspace"), keybind.Keymap())
        self.assertEqual(form.fields[1].value, "https:/")

    def test_rendering_puts_the_cursor_at_the_end_of_the_active_value(self):
        form = self.form()
        form.index = 1
        view = form_view(form, 50, 12, UNICODE)
        self.assertIsNotNone(view.cursor)
        row = "".join(t for t, _ in view.rows[view.cursor[0]])
        self.assertIn("https://", row)

    def test_a_message_is_shown_instead_of_the_hint(self):
        form = self.form()
        form.message = "provider name is required"
        text = "\n".join("".join(t for t, _ in row)
                         for row in form_view(form, 60, 12, UNICODE).rows)
        self.assertIn("provider name is required", text)


# --------------------------------------------------------------------------
# key routing
# --------------------------------------------------------------------------


class KeymapRoutingTests(unittest.TestCase):
    """The TUI resolves keys through keybind.Keymap, leader included."""

    def setUp(self):
        self.ui = make_tui()

    def test_ctrl_p_opens_the_command_palette(self):
        self.assertTrue(self.ui._keymap_key(KeyEvent(key="p", ctrl=True)))
        self.assertIsNotNone(self.ui.dialog)
        self.assertEqual(self.ui.dialog.name, "commands")

    def test_f1_opens_the_keybinding_help(self):
        self.ui._keymap_key(KeyEvent(key="f1"))
        self.assertEqual(self.ui.dialog.name, "help")

    def test_the_leader_arms_and_then_completes(self):
        self.assertTrue(self.ui._keymap_key(KeyEvent(key="x", ctrl=True)))
        self.assertTrue(self.ui.keymap.leader_pending)
        self.assertIsNone(self.ui.dialog)
        self.assertTrue(self.ui._keymap_key(KeyEvent(key="s")))
        self.assertFalse(self.ui.keymap.leader_pending)
        self.assertEqual(self.ui.dialog.name, "status")

    def test_a_leader_sequence_that_means_nothing_is_swallowed(self):
        self.ui._keymap_key(KeyEvent(key="x", ctrl=True))
        self.assertTrue(self.ui._keymap_key(KeyEvent(key="z")))
        self.assertIsNone(self.ui.dialog)
        self.assertEqual(self.ui.buffer, "")

    def test_the_pending_leader_is_shown_in_the_footer(self):
        self.assertEqual(self.ui._leader_label(), "")
        self.ui._keymap_key(KeyEvent(key="x", ctrl=True))
        self.assertTrue(self.ui._leader_label().startswith("ctrl+x"))

    def test_plain_letters_are_not_consumed(self):
        self.assertFalse(self.ui._keymap_key(KeyEvent(key="a")))

    def test_tab_completes_a_slash_command_instead_of_cycling_agent(self):
        self.ui.completer = lambda prefix: ["/status"]
        self.ui.buffer = "/sta"
        self.ui.cursor = len(self.ui.buffer)

        self.ui._handle_key(9)

        self.assertEqual(self.ui.buffer, "/status")

    def test_ctrl_c_and_ctrl_d_keep_their_own_handling(self):
        self.assertFalse(self.ui._keymap_key(KeyEvent(key="c", ctrl=True)))
        self.assertFalse(self.ui._keymap_key(KeyEvent(key="d", ctrl=True)))

    def test_ctrl_a_opens_the_provider_list(self):
        # No config in this TUI, so the binding reports rather than opening.
        self.assertTrue(self.ui._keymap_key(KeyEvent(key="a", ctrl=True)))
        self.assertIn("provider", self.ui.status_hint)

    def test_escape_is_left_to_the_input_layer(self):
        self.assertFalse(self.ui._keymap_key(KeyEvent(key="escape")))

    def test_home_and_end_still_belong_to_the_prompt(self):
        self.assertFalse(self.ui._keymap_key(KeyEvent(key="home")))
        self.assertFalse(self.ui._keymap_key(KeyEvent(key="end")))

    def test_leader_q_exits(self):
        self.ui._keymap_key(KeyEvent(key="x", ctrl=True))
        self.ui._keymap_key(KeyEvent(key="q"))
        self.assertTrue(self.ui._quit)

    def test_a_binding_that_raises_becomes_a_transcript_line(self):
        def boom():
            raise RuntimeError("nope")

        self.ui._open_status = boom
        self.ui._run_binding("status_view")
        self.assertEqual(self.ui.transcript.entries[-1].kind, "error")

    def test_every_binding_names_a_method_that_exists(self):
        for command, name in tui.BINDING_ACTIONS.items():
            self.assertTrue(callable(getattr(self.ui, name, None)),
                            "%s -> %s is missing" % (command, name))
            self.assertTrue(self.ui.keymap.describe(command),
                            "%s has no key" % command)


class DialogRoutingTests(unittest.TestCase):
    def setUp(self):
        self.ui = make_tui()
        self.chosen = []

    def open(self, **kwargs):
        dialog = Dialog("test", "Test", items("alpha", "beta"),
                        payload={"submit": self.chosen.append}, **kwargs)
        self.ui._open_dialog(dialog)
        return dialog

    def test_a_dialog_takes_the_keys_before_the_keymap_does(self):
        self.open()
        self.ui._dialog_key(KeyEvent(key="p", ctrl=True))
        self.assertEqual(self.ui.dialog.name, "test")   # not the palette

    def test_enter_runs_the_submit_handler(self):
        self.open()
        self.ui._dialog_key(KeyEvent(key="return"))
        self.assertEqual([item.title for item in self.chosen], ["alpha"])

    def test_escape_closes_it(self):
        self.open()
        self.ui._dialog_key(KeyEvent(key="escape"))
        self.assertIsNone(self.ui.dialog)

    def test_an_action_handler_receives_the_selected_item(self):
        seen = []
        self.open(actions=[DialogAction("model_favorite_toggle", "Fav",
                                        seen.append)])
        self.ui._dialog_key(KeyEvent(key="f", ctrl=True))
        self.assertEqual([item.title for item in seen], ["alpha"])
        self.assertIsNotNone(self.ui.dialog)      # actions do not close it

    def test_a_query_handler_sees_what_was_typed(self):
        seen = []
        dialog = Dialog("sessions", "Sessions", items("a"), filtered=False,
                        payload={"query": seen.append})
        self.ui._open_dialog(dialog)
        self.ui._dialog_key(KeyEvent(key="q"))
        self.assertEqual(seen, ["q"])

    def test_a_failing_handler_does_not_take_the_screen_down(self):
        dialog = Dialog("x", "X", items("a"),
                        payload={"submit": lambda item: 1 / 0})
        self.ui._open_dialog(dialog)
        self.ui._dialog_key(KeyEvent(key="return"))
        self.assertEqual(self.ui.transcript.entries[-1].kind, "error")

    def test_opening_a_dialog_disarms_a_half_typed_leader(self):
        self.ui._keymap_key(KeyEvent(key="x", ctrl=True))
        self.open()
        self.assertFalse(self.ui.keymap.leader_pending)

    def test_a_stale_background_load_is_dropped(self):
        dialog = self.open()
        serial = self.ui._dialog_serial
        self.ui._close_dialog()
        self.ui._on_dialog_data(("test", serial, ([], [])))
        self.assertIsNone(self.ui.dialog)
        self.assertEqual(dialog.select.count, 2)

    def test_a_load_for_the_open_dialog_replaces_its_items(self):
        from haikode.models import ModelRef
        dialog = Dialog("models", "Models", [], payload={})
        self.ui._open_dialog(dialog)
        self.ui._on_dialog_data(
            ("models", self.ui._dialog_serial,
             ([ModelRef("openai", "gpt-4o", category="openai")], [])))
        self.assertEqual(dialog.select.count, 1)


class UsageAccountingTests(unittest.TestCase):
    class Agent:
        def __init__(self):
            self.tokens = {"input": 0, "output": 0}
            self.messages = []
            self.context_window = 1000
            self.system_prompt = ""
            self.specs = []

    def test_only_the_delta_of_a_turn_is_recorded(self):
        ui = make_tui()
        ui.agent = self.Agent()
        ui.agent.tokens = {"input": 100, "output": 50}
        ui._record_usage()
        self.assertEqual(ui.usage.session.input_tokens, 100)
        ui.agent.tokens = {"input": 175, "output": 90}
        ui._record_usage()
        self.assertEqual(ui.usage.session.input_tokens, 175)
        self.assertEqual(ui.usage.session.output_tokens, 90)

    def test_a_rebuilt_agent_restarting_at_zero_adds_nothing(self):
        ui = make_tui()
        ui.agent = self.Agent()
        ui.agent.tokens = {"input": 500, "output": 100}
        ui._record_usage()
        ui.agent = self.Agent()          # counters back to zero
        ui._record_usage()
        self.assertEqual(ui.usage.session.input_tokens, 500)

    def test_junk_counters_are_ignored(self):
        ui = make_tui()
        ui.agent = self.Agent()
        ui.agent.tokens = {"input": "lots"}
        ui._record_usage()
        self.assertEqual(ui.usage.session.total, 0)

    def test_context_state_is_cached_and_droppable(self):
        ui = make_tui()
        ui.agent = self.Agent()
        ui.agent.messages = [{"role": "user", "content": "x" * 400}]
        first = ui._context_state()
        self.assertGreater(first.used, 0)
        self.assertIs(ui._context_state(), first)
        ui._context = None
        self.assertIsNot(ui._context_state(), first)

    def test_a_broken_agent_measures_as_empty_rather_than_raising(self):
        class Broken:
            @property
            def messages(self):
                raise RuntimeError("no")

        ui = make_tui()
        ui.agent = Broken()
        self.assertEqual(ui._context_state().used, 0)


class TestDeviceLoginDialog(unittest.TestCase):
    """ChatGPT and SuperGrok have no key to type; they sign in with a code.

    The login modal only ever offered an "API key" field, so the two
    subscription providers could not be signed into from the TUI at all.
    """

    def setUp(self):
        self.glyphs = tui.Glyphs(unicode_ok=True)

    def dialog(self, state="waiting"):
        d = tui.DeviceDialog("chatgpt")
        d.url = "https://auth.openai.com/codex/device"
        d.code = "TWHK-654EG"
        d.state = state
        d.message = {"waiting": "waiting for you to approve it",
                     "done": "signed in to chatgpt",
                     "failed": "the code expired"}[state]
        return d

    def rendered(self, dialog, width=62, height=14):
        view = tui.device_view(dialog, width, height, self.glyphs)
        return [" ".join(text for text, _ in row).strip() for row in view.rows]

    def test_the_url_and_code_are_both_shown(self):
        lines = self.rendered(self.dialog())
        self.assertIn("https://auth.openai.com/codex/device", lines)
        self.assertIn("TWHK-654EG", lines)

    def test_escape_cancels_at_any_point(self):
        for state in ("starting", "waiting", "done", "failed"):
            dialog = tui.DeviceDialog("chatgpt")
            dialog.state = state
            self.assertEqual(
                dialog.handle(keybind.KeyEvent("escape"), None),
                tui.DIALOG_CANCEL, state)

    def test_typing_never_leaks_into_the_prompt_behind_the_modal(self):
        dialog = self.dialog()
        for key in ("a", "space", "backspace", "up"):
            self.assertEqual(dialog.handle(keybind.KeyEvent(key), None),
                             tui.DIALOG_CONSUMED, key)

    def test_enter_closes_only_once_the_flow_has_finished(self):
        waiting = self.dialog("waiting")
        self.assertEqual(waiting.handle(keybind.KeyEvent("return"), None),
                         tui.DIALOG_CONSUMED)
        for state in ("done", "failed"):
            self.assertEqual(
                self.dialog(state).handle(keybind.KeyEvent("return"), None),
                tui.DIALOG_CANCEL, state)

    def test_a_failure_is_styled_as_a_warning(self):
        view = tui.device_view(self.dialog("failed"), 62, 14, self.glyphs)
        styles = {style for row in view.rows for _, style in row}
        self.assertIn("warn", styles)

    def test_it_fits_a_narrow_terminal_without_overflowing(self):
        view = tui.device_view(self.dialog(), 30, 12, self.glyphs)
        for row in view.rows:
            self.assertLessEqual(sum(len(text) for text, _ in row), 30)

    def test_the_ascii_fallback_stays_ascii(self):
        view = tui.device_view(self.dialog(), 62, 14,
                               tui.Glyphs(unicode_ok=False))
        for row in view.rows:
            for text, _ in row:
                self.assertTrue(text.isascii(), repr(text))


class PinnedTodoTests(unittest.TestCase):
    """The plan stays above the prompt instead of scrolling away."""

    OPTS = tui.RenderOptions(glyphs=ASCII)

    def _todos(self, *pairs):
        return [{"content": text, "status": state} for text, state in pairs]

    def _texts(self, lines):
        return [line.text.strip() for line in lines]

    def test_outstanding_work_is_listed_under_a_header(self):
        lines = tui.build_pinned_todo_lines(
            self._todos(("les loggen", "in_progress"),
                        ("fiks parseren", "pending")), 40, self.OPTS)
        joined = " ".join(self._texts(lines))
        self.assertIn("Plan", joined)
        self.assertIn("les loggen", joined)
        self.assertIn("fiks parseren", joined)

    def test_a_finished_plan_collapses_the_band(self):
        lines = tui.build_pinned_todo_lines(
            self._todos(("ferdig", "completed")), 40, self.OPTS)
        self.assertEqual(lines, [])

    def test_open_items_win_the_rows_and_the_rest_is_counted(self):
        todos = self._todos(*[("gjort %d" % i, "completed") for i in range(5)],
                            *[("igjen %d" % i, "pending") for i in range(5)])
        lines = tui.build_pinned_todo_lines(todos, 40, self.OPTS, limit=4)
        joined = " ".join(self._texts(lines))
        self.assertLessEqual(len(lines), 4)
        self.assertIn("igjen 0", joined)
        self.assertIn("more", joined)
        self.assertNotIn("gjort 0", joined)

    def test_the_band_takes_rows_from_the_frame_but_leaves_a_body(self):
        bare = tui.layout_frame(24, 80, 1, session=True)
        with_todos = tui.layout_frame(24, 80, 1, session=True,
                                      wanted_todo_rows=3)
        self.assertEqual(with_todos.todo_rows, 3)
        self.assertEqual(with_todos.todo_top + 3, with_todos.box_top)
        self.assertEqual(with_todos.body_height, bare.body_height - 3)

    def test_a_tiny_screen_drops_the_band_rather_than_the_transcript(self):
        frame = tui.layout_frame(tui.MIN_ROWS, 80, 1, session=True,
                                 wanted_todo_rows=6)
        self.assertGreaterEqual(frame.body_height, 1)
        self.assertLessEqual(frame.todo_rows, 6)
        self.assertEqual(frame.todo_top + frame.todo_rows, frame.box_top)


class WheelEmulationTests(unittest.TestCase):
    """Arrows scroll instead of browsing history when that is the intent.

    Haiku Terminal turns wheel ticks into arrow keys in the alternate screen
    (it never sends mouse reports there), so arrows during a running turn or
    in scrollback must reach _scroll, not the prompt history.
    """

    def _seeded(self):
        ui = make_tui()
        ui.history = ["earlier prompt"]
        ui.history_index = 1
        return ui

    def test_arrow_up_scrolls_while_a_turn_is_running(self):
        ui = self._seeded()
        ui.running = True
        calls = []
        ui._scroll = calls.append
        ui._on_vertical(-1)
        self.assertEqual(calls, [-1])
        self.assertEqual(ui.buffer, "")

    def test_arrow_up_scrolls_when_already_in_scrollback(self):
        ui = self._seeded()
        ui.follow = False
        calls = []
        ui._scroll = calls.append
        ui._on_vertical(-1)
        self.assertEqual(calls, [-1])
        self.assertEqual(ui.buffer, "")

    def test_arrow_up_still_browses_history_when_idle_at_bottom(self):
        ui = self._seeded()
        ui._on_vertical(-1)
        self.assertEqual(ui.buffer, "earlier prompt")


class CommandOutputTests(unittest.TestCase):
    """Command answers reach the transcript as plain text."""

    def test_sgr_escapes_are_stripped_from_command_output(self):
        # Regression: /memory colours its footer with _c() for the plain
        # REPL; on the Haiku box the codes showed up literally as
        # "[2mProject files:...[0m" in the transcript.
        ui = make_tui()
        ui._finish_command(
            "/memory", "plain\n\x1b[2mProject files: /x\x1b[0m")
        self.assertEqual(ui.transcript.entries[-1].text,
                         "plain\nProject files: /x")


if __name__ == "__main__":
    unittest.main()
