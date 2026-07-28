import unittest

from haikode.palette import (COMMAND_PALETTE_COMMAND, DEFAULT_COMMANDS,
                             EMPTY_LIST_MESSAGE, UNAVAILABLE_DETAIL,
                             CommandPalette, CommandUnavailable, PaletteItem,
                             SelectList, build_default_palette, fuzzy_score,
                             match_item, resolve_handler)


def score(query, text):
    """Score only, so ranking assertions read as comparisons."""
    result = fuzzy_score(query, text)
    assert result is not None, "%r should match %r" % (query, text)
    return result[0]


def positions(query, text):
    result = fuzzy_score(query, text)
    assert result is not None, "%r should match %r" % (query, text)
    return result[1]


def item(title, **kwargs):
    kwargs.setdefault("id", title)
    return PaletteItem(title=title, **kwargs)


class FuzzyScoreTest(unittest.TestCase):

    def test_empty_query_matches_everything(self):
        self.assertEqual(fuzzy_score("", "anything"), (0, []))
        self.assertEqual(fuzzy_score("   ", "anything"), (0, []))
        self.assertEqual(fuzzy_score("", ""), (0, []))

    def test_no_match_returns_none(self):
        self.assertIsNone(fuzzy_score("zzz", "session list"))
        self.assertIsNone(fuzzy_score("abcd", "abc"))      # longer than text
        self.assertIsNone(fuzzy_score("cba", "abc"))       # wrong order
        self.assertIsNone(fuzzy_score("x", ""))

    def test_case_insensitive(self):
        self.assertEqual(positions("LIST", "list models"), [0, 1, 2, 3])
        self.assertEqual(positions("list", "LIST MODELS"), [0, 1, 2, 3])

    def test_positions_are_the_matched_indices(self):
        self.assertEqual(positions("sn", "session new"), [0, 8])
        self.assertEqual(positions("mod", "switch model"), [7, 8, 9])
        for query, text in (("sn", "session new"), ("mdl", "model list"),
                            ("prov", "Set default provider")):
            found = positions(query, text)
            self.assertEqual(len(found), len(query))
            self.assertEqual(sorted(found), found)
            self.assertEqual("".join(text[i] for i in found).lower(),
                             query.lower())

    def test_prefix_beats_word_boundary_beats_mid_word(self):
        prefix = score("mod", "model list")
        boundary = score("mod", "switch model")
        midword = score("mod", "remodelled")
        self.assertGreater(prefix, boundary)
        self.assertGreater(boundary, midword)

    def test_exact_match_is_the_strongest(self):
        self.assertGreater(score("help", "help"), score("help", "help me"))

    def test_consecutive_beats_scattered(self):
        self.assertGreater(score("abc", "abcxyz"), score("abc", "axbxcy"))
        self.assertGreater(score("abc", "abcxyz"), score("abc", "a b c"))
        self.assertGreater(score("new", "new session"),
                           score("new", "no easy way"))

    def test_word_boundary_bonus_for_every_separator(self):
        midword = score("list", "blistering")
        for text in ("session list", "session-list", "session_list",
                     "session/list", "session.list"):
            self.assertGreater(score("list", text), midword, text)

    def test_camel_case_hump_is_a_boundary(self):
        self.assertGreater(score("mini", "gptMiniModel"),
                           score("mini", "gptxminimodel"))

    def test_short_unmatched_tail_wins(self):
        self.assertGreater(score("ses", "session"),
                           score("ses", "session with a very long tail"))

    def test_multiple_terms_must_all_match(self):
        self.assertIsNone(fuzzy_score("session zzz", "new session"))
        result = fuzzy_score("new ses", "new session")
        self.assertIsNotNone(result)
        self.assertEqual(result[1], [0, 1, 2, 4, 5, 6])

    def test_terms_may_appear_in_any_order(self):
        self.assertIsNotNone(fuzzy_score("session new", "New session"))


class SelectListTest(unittest.TestCase):

    def build(self, titles, **kwargs):
        return SelectList([item(title) for title in titles], **kwargs)

    def test_empty_query_keeps_original_order(self):
        titles = ["gamma", "alpha", "beta"]
        select = self.build(titles)
        self.assertEqual([i.title for i, _ in select.matches], titles)
        self.assertEqual([p for _, p in select.matches], [[], [], []])

    def test_query_filters_and_ranks(self):
        select = self.build(["remodelled", "switch model", "model list"])
        select.query = "mod"
        self.assertEqual([i.title for i, _ in select.matches],
                         ["model list", "switch model", "remodelled"])
        select.query = "zzz"
        self.assertEqual(select.matches, [])

    def test_equal_scores_keep_original_order(self):
        select = self.build(["alpha one", "alpha two", "alpha six"])
        select.query = "alpha"
        self.assertEqual([i.title for i, _ in select.matches],
                         ["alpha one", "alpha two", "alpha six"])

    def test_category_and_description_are_searchable(self):
        select = SelectList([
            item("Exit", category="App", description="quit haikode"),
            item("Status", category="View", description="setup overview"),
        ])
        select.query = "app"
        self.assertEqual([i.title for i, _ in select.matches], ["Exit"])
        select.query = "overview"
        self.assertEqual([i.title for i, _ in select.matches], ["Status"])

    def test_positions_index_into_the_title_only(self):
        select = SelectList([item("Exit", category="App")])
        select.query = "app"
        # The category matched, the title did not: no highlight, but a match.
        self.assertEqual(select.matches[0][1], [])

    def test_paging(self):
        select = self.build(["i%d" % n for n in range(7)], page_size=3)
        self.assertEqual(select.page_count, 3)
        self.assertEqual([i.title for i, _ in select.visible],
                         ["i0", "i1", "i2"])
        select.page(1)
        self.assertEqual(select.cursor, 3)
        self.assertEqual(select.page_index, 1)
        self.assertEqual([i.title for i, _ in select.visible],
                         ["i3", "i4", "i5"])
        select.page(1)
        self.assertEqual([i.title for i, _ in select.visible], ["i6"])
        select.page(-1)
        self.assertEqual(select.cursor, 3)

    def test_pages_clamp_but_single_steps_wrap(self):
        select = self.build(["a", "b", "c"], page_size=2)
        select.page(-1)
        self.assertEqual(select.cursor, 0)
        select.page(5)
        self.assertEqual(select.cursor, 2)
        select.move(1)
        self.assertEqual(select.cursor, 0)
        select.move(-1)
        self.assertEqual(select.cursor, 2)

    def test_home_and_end(self):
        select = self.build(["a", "b", "c"])
        select.end()
        self.assertEqual(select.selected.title, "c")
        select.home()
        self.assertEqual(select.selected.title, "a")

    def test_moving_skips_disabled_items(self):
        select = SelectList([item("a"), item("b", disabled=True), item("c")])
        self.assertEqual(select.selected.title, "a")
        select.move(1)
        self.assertEqual(select.selected.title, "c")
        select.move(1)
        self.assertEqual(select.selected.title, "a")
        select.move(-1)
        self.assertEqual(select.selected.title, "c")

    def test_first_selection_skips_a_leading_disabled_item(self):
        select = SelectList([item("a", disabled=True), item("b")])
        self.assertEqual(select.selected.title, "b")
        select.end()
        self.assertEqual(select.selected.title, "b")
        select.home()
        self.assertEqual(select.selected.title, "b")

    def test_all_disabled_selects_nothing(self):
        select = SelectList([item("a", disabled=True), item("b", disabled=True)])
        self.assertIsNone(select.selected)
        self.assertIsNone(select.move(1))
        self.assertEqual(select.count, 2)

    def test_empty_list_selects_nothing(self):
        select = SelectList([])
        self.assertIsNone(select.selected)
        self.assertIsNone(select.move(1))
        self.assertEqual(select.cursor, 0)
        self.assertEqual(select.page_count, 1)

    def test_query_change_moves_the_cursor_back_to_the_top(self):
        # opencode's dialog-select runs moveTo(0) on every filter change; a
        # clamped-but-kept index would leave the cursor on whatever the new
        # query happened to rank into that slot.
        select = self.build(["alpha", "beta", "gamma", "alps"])
        select.end()
        self.assertEqual(select.cursor, 3)
        select.query = "al"
        self.assertEqual(select.count, 2)
        self.assertEqual(select.cursor, 0)
        self.assertIs(select.selected, select.matches[0][0])
        select.query = "alpha"
        self.assertEqual(select.count, 1)
        self.assertEqual(select.cursor, 0)

    def test_query_change_does_not_strand_the_cursor_on_another_item(self):
        select = self.build(["alpha", "beta", "gamma beta alpha"])
        select.move(1)
        select.move(1)
        self.assertEqual(select.selected.title, "gamma beta alpha")
        select.query = "a"
        self.assertEqual(select.selected.title, select.matches[0][0].title)

    def test_replacing_items_keeps_the_cursor_where_it_can(self):
        # Items changing under the cursor only clamps (opencode's
        # preserveSelection path), unlike a filter change.
        select = self.build(["a", "b", "c"])
        select.end()
        self.assertEqual(select.cursor, 2)
        select.items = [item("a"), item("b"), item("c"), item("d")]
        self.assertEqual(select.cursor, 2)
        select.items = [item("a"), item("b")]
        self.assertEqual(select.cursor, 1)

    def test_replacing_items_refilters(self):
        select = self.build(["alpha", "beta"])
        select.query = "al"
        select.items = [item("also"), item("beta")]
        self.assertEqual([i.title for i, _ in select.matches], ["also"])

    def test_grouped_preserves_category_order(self):
        select = SelectList([
            item("new", category="Session"),
            item("models", category="Model"),
            item("list", category="Session"),
            item("exit", category="App"),
        ])
        grouped = select.grouped()
        self.assertEqual([category for category, _ in grouped],
                         ["Session", "Model", "App"])
        self.assertEqual([i.title for i in grouped[0][1]], ["new", "list"])

    def test_grouped_follows_the_filter(self):
        select = SelectList([
            item("new", category="Session"),
            item("models", category="Model"),
        ])
        select.query = "models"
        self.assertEqual([category for category, _ in select.grouped()],
                         ["Model"])

    def test_uncategorised_items_group_under_the_empty_string(self):
        select = SelectList([item("a"), item("b")])
        self.assertEqual(select.grouped(), [("", select.items)])

    def test_grouped_flattens_back_to_the_match_order(self):
        # The cursor indexes the grouped list (opencode's flat()), so the
        # sections concatenated must be exactly `matches` -- otherwise a
        # renderer drawing headers highlights the wrong row.
        select = SelectList([
            item("session new", category="Session"),
            item("show status", category="View"),
            item("session list", category="Session"),
            item("show todos", category="View"),
        ])
        for query in ("", "s", "session", "show", "sn", "st"):
            select.query = query
            flat = [i for _, items in select.grouped() for i in items]
            self.assertEqual(flat, [i for i, _ in select.matches], query)
            for index in range(select.count):
                select.move_to(index)
                self.assertIs(flat[index], select.matches[index][0])

    def test_cursor_matches_the_grouped_row_after_ranking_interleaves(self):
        select = SelectList([
            item("alpha", category="One"),
            item("bravo", category="Two"),
            item("alpine", category="One"),
        ])
        select.query = "al"
        flat = [i for _, items in select.grouped() for i in items]
        select.move_to(1)
        self.assertIs(select.selected, flat[1])

    def test_paging_never_leaves_the_visible_window(self):
        select = self.build(["i%d" % n for n in range(23)], page_size=5)
        for _ in range(10):
            select.page(1)
            self.assertIn(select.selected, [i for i, _ in select.visible])
        for _ in range(10):
            select.page(-1)
            self.assertIn(select.selected, [i for i, _ in select.visible])

    def test_a_one_row_page_still_clamps(self):
        # page() must not inherit move()'s single-step wrapping just because
        # delta * page_size happens to be 1.
        select = self.build(["a", "b", "c"], page_size=1)
        select.page(-1)
        self.assertEqual(select.cursor, 0)
        select.page(5)
        self.assertEqual(select.cursor, 2)
        select.page(1)
        self.assertEqual(select.cursor, 2)

    def test_matches_and_visible_do_not_alias_internal_state(self):
        select = self.build(["alpha", "beta"])
        select.query = "al"
        select.matches[0][1].append(99)
        select.visible[0][1].append(99)
        self.assertEqual(select.matches[0][1], select.selected_positions)

    def test_empty_message(self):
        select = self.build(["alpha", "beta"])
        self.assertEqual(select.empty_message, "")
        select.query = "zzz"
        self.assertIn("No matches", select.empty_message)
        self.assertIn("zzz", select.empty_message)
        self.assertEqual(SelectList([]).empty_message, EMPTY_LIST_MESSAGE)

    def test_page_size_is_at_least_one(self):
        select = self.build(["a", "b"], page_size=0)
        self.assertEqual(select.page_size, 1)
        self.assertEqual(len(select.visible), 1)


class CommandPaletteTest(unittest.TestCase):

    def test_register_and_run(self):
        palette = CommandPalette()
        palette.register("session.new", "New session", "start fresh",
                         "Session", lambda: "ran", keys="<leader>n")
        self.assertIn("session.new", palette)
        self.assertEqual(len(palette), 1)
        self.assertEqual(palette.run("session.new"), "ran")

    def test_run_forwards_arguments(self):
        palette = CommandPalette()
        palette.register("echo", "Echo", handler=lambda value: value * 2)
        self.assertEqual(palette.run("echo", 21), 42)

    def test_unknown_id_raises_key_error(self):
        palette = CommandPalette()
        with self.assertRaises(KeyError):
            palette.run("nope")

    def test_items_carry_the_command_metadata(self):
        palette = CommandPalette()
        palette.register("app.exit", "Exit", "Quit haikode", "App",
                         lambda: None, keys="ctrl+c")
        entry = palette.items()[0]
        self.assertEqual(entry.id, "app.exit")
        self.assertEqual(entry.title, "Exit")
        self.assertEqual(entry.description, "Quit haikode")
        self.assertEqual(entry.category, "App")
        self.assertEqual(entry.keys, "ctrl+c")
        self.assertEqual(entry.footer, "ctrl+c")
        self.assertEqual(entry.value, "app.exit")
        self.assertFalse(entry.disabled)

    def test_items_keep_registration_order(self):
        palette = CommandPalette()
        for name in ("c", "a", "b"):
            palette.register(name, name.upper(), handler=lambda: None)
        self.assertEqual([i.id for i in palette.items()], ["c", "a", "b"])

    def test_re_registering_replaces(self):
        palette = CommandPalette()
        palette.register("x", "First", handler=lambda: 1)
        palette.register("x", "Second", handler=lambda: 2)
        self.assertEqual(len(palette), 1)
        self.assertEqual(palette.items()[0].title, "Second")
        self.assertEqual(palette.run("x"), 2)

    def test_disabled_predicate_hides_the_entry(self):
        palette = CommandPalette()
        palette.register("a", "A", handler=lambda: None, enabled=lambda: False)
        palette.register("b", "B", handler=lambda: None)
        self.assertEqual([i.id for i in palette.items()], ["b"])
        with self.assertRaises(CommandUnavailable):
            palette.run("a")

    def test_hidden_entry_is_not_listed_but_still_runs(self):
        palette = CommandPalette()
        palette.register("a", "A", handler=lambda: "ok", hidden=True)
        self.assertEqual(palette.items(), [])
        self.assertEqual(palette.run("a"), "ok")

    def test_palette_command_hides_itself(self):
        palette = CommandPalette()
        palette.register(COMMAND_PALETTE_COMMAND, "Commands",
                         handler=lambda: None)
        palette.register("app.exit", "Exit", handler=lambda: None)
        self.assertEqual([i.id for i in palette.items()], ["app.exit"])

    def test_palette_command_id_matches_the_keybind_table(self):
        # keybind.COMMAND_MAP is what the TUI dispatches through; if the two
        # modules disagree the palette silently lists itself.
        from haikode.keybind import COMMAND_MAP
        self.assertEqual(COMMAND_MAP["command_list"], COMMAND_PALETTE_COMMAND)

    def test_broken_enabled_predicate_does_not_crash(self):
        def boom():
            raise RuntimeError("no config")

        palette = CommandPalette()
        palette.register("a", "A", handler=lambda: None, enabled=boom)
        self.assertEqual(palette.items(), [])

    def test_missing_handler_is_listed_but_disabled(self):
        palette = CommandPalette()
        palette.register("a", "A")
        entry = palette.items()[0]
        self.assertTrue(entry.disabled)
        self.assertEqual(entry.detail, UNAVAILABLE_DETAIL)
        with self.assertRaises(CommandUnavailable):
            palette.run("a")

    def test_select_list_wraps_the_items(self):
        palette = CommandPalette()
        palette.register("session.new", "New session", category="Session",
                         handler=lambda: None)
        palette.register("app.exit", "Exit", category="App",
                         handler=lambda: None)
        select = palette.select_list(query="exit")
        self.assertEqual(select.selected.id, "app.exit")


class ResolveHandlerTest(unittest.TestCase):

    def test_dotted_and_underscore_keys(self):
        dotted = resolve_handler({"session.new": len}, "session.new")
        underscore = resolve_handler({"session_new": len}, "session.new")
        self.assertIs(dotted, len)
        self.assertIs(underscore, len)

    def test_objects_work_too(self):
        class Context:
            def session_new(self):
                return "ok"

        context = Context()
        handler = resolve_handler(context, "session.new")
        self.assertIsNotNone(handler)
        self.assertEqual(handler(), "ok")

    def test_missing_or_non_callable_resolves_to_none(self):
        self.assertIsNone(resolve_handler(None, "session.new"))
        self.assertIsNone(resolve_handler({}, "session.new"))
        self.assertIsNone(resolve_handler({"session.new": 3}, "session.new"))


class BuildDefaultPaletteTest(unittest.TestCase):

    def test_registers_the_whole_command_set(self):
        palette = build_default_palette({})
        self.assertEqual(len(palette), len(DEFAULT_COMMANDS))
        for command_id, _, _, _, _ in DEFAULT_COMMANDS:
            self.assertIn(command_id, palette)

    def test_category_order(self):
        select = build_default_palette({}).select_list()
        self.assertEqual([category for category, _ in select.grouped()],
                         ["Session", "Model", "Config", "View", "App"])

    def test_missing_context_degrades_to_disabled_items(self):
        palette = build_default_palette(None)
        items = palette.items()
        self.assertEqual(len(items), len(DEFAULT_COMMANDS))
        self.assertTrue(all(i.disabled for i in items))
        self.assertTrue(all(i.detail == UNAVAILABLE_DETAIL for i in items))
        with self.assertRaises(CommandUnavailable):
            palette.run("session.new")

    def test_no_context_at_all_still_builds(self):
        palette = build_default_palette()
        self.assertEqual(len(palette.items()), len(DEFAULT_COMMANDS))

    def test_partial_context_enables_only_what_it_supplies(self):
        calls = []
        context = {
            "session_new": lambda: calls.append("new") or "created",
            "model.list": lambda: "models",
        }
        palette = build_default_palette(context)
        by_id = {i.id: i for i in palette.items()}
        self.assertFalse(by_id["session.new"].disabled)
        self.assertFalse(by_id["model.list"].disabled)
        self.assertTrue(by_id["app.exit"].disabled)
        self.assertEqual(palette.run("session.new"), "created")
        self.assertEqual(calls, ["new"])
        with self.assertRaises(CommandUnavailable):
            palette.run("app.exit")

    def test_object_context(self):
        class Context:
            def app_exit(self):
                return "bye"

        palette = build_default_palette(Context())
        self.assertEqual(palette.run("app.exit"), "bye")

    def test_cursor_lands_on_the_first_usable_command(self):
        palette = build_default_palette({"app.exit": lambda: None})
        select = palette.select_list(page_size=30)
        self.assertEqual(select.selected.id, "app.exit")

    def test_searching_the_default_set(self):
        select = build_default_palette({}).select_list()
        select.query = "provider"
        found = [i.id for i, _ in select.matches]
        self.assertIn("provider.add", found)
        self.assertIn("provider.list", found)
        self.assertNotIn("app.exit", found)
        select.query = "session"
        self.assertEqual([i.id for i, _ in select.matches][0], "session.new")

    def test_key_hints_come_from_the_keybind_table(self):
        palette = build_default_palette({})
        self.assertEqual(palette.get("session.new").keys, "<leader>n")
        self.assertEqual(palette.get("model.list").keys, "<leader>m")
        self.assertEqual(palette.get("session.rename").keys, "ctrl+r")


class MatchItemTest(unittest.TestCase):

    def test_empty_query_scores_zero(self):
        self.assertEqual(match_item(item("anything"), ""), (0, []))

    def test_title_outweighs_category(self):
        title_hit = match_item(item("Model list", category="View"), "model")
        category_hit = match_item(item("Something", category="Model"), "model")
        self.assertGreater(title_hit[0], category_hit[0])

    def test_no_field_matches(self):
        self.assertIsNone(match_item(item("Exit", category="App"), "zzz"))


if __name__ == "__main__":
    unittest.main()
