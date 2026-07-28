import unittest

from haikode import keybind
from haikode.keybind import (
    COMMAND_MAP,
    DEFINITIONS,
    LEADER_DEFAULT,
    Chord,
    KeyEvent,
    Keymap,
    from_curses,
    is_disabled,
    normalise_key,
    parse_binding,
)


class FakeConfig:
    """Stands in for haikode.config.Config: only .data is read."""

    def __init__(self, keybinds=None, **extra):
        self.data = dict(extra)
        if keybinds is not None:
            self.data["keybinds"] = keybinds


class FakeCurses:
    """The handful of ncurses constants from_curses() looks up."""

    KEY_DOWN = 258
    KEY_UP = 259
    KEY_LEFT = 260
    KEY_RIGHT = 261
    KEY_HOME = 262
    KEY_BACKSPACE = 263
    KEY_F0 = 264
    KEY_DC = 330
    KEY_IC = 331
    KEY_SF = 336
    KEY_SR = 337
    KEY_NPAGE = 338
    KEY_PPAGE = 339
    KEY_ENTER = 343
    KEY_BTAB = 353
    KEY_END = 360
    KEY_SDC = 383
    KEY_SEND = 386
    KEY_SHOME = 391
    KEY_SLEFT = 393
    KEY_SNEXT = 396
    KEY_SPREVIOUS = 398
    KEY_SRIGHT = 402
    KEY_MOUSE = 409
    KEY_RESIZE = 410


CURSES = FakeCurses()


class TestDefinitions(unittest.TestCase):

    def test_required_names_present(self):
        required = [
            "leader", "app_exit", "command_list", "model_list",
            "model_provider_list", "model_cycle_recent", "session_new",
            "session_list", "session_interrupt", "session_compact",
            "session_rename", "status_view", "messages_undo",
            "messages_page_up", "messages_page_down", "messages_first",
            "messages_last", "agent_list", "theme_list", "editor_open",
            "input_clear", "input_submit", "input_newline", "help_show",
        ]
        for name in required:
            self.assertIn(name, DEFINITIONS, name)

    def test_opencode_defaults_are_preserved(self):
        expected = {
            "leader": "ctrl+x",
            "app_exit": "ctrl+c,ctrl+d,<leader>q",
            "command_list": "ctrl+p",
            "model_list": "<leader>m",
            "model_provider_list": "ctrl+a",
            "model_cycle_recent": "f2",
            "model_cycle_recent_reverse": "shift+f2",
            "session_new": "<leader>n",
            "session_list": "<leader>l",
            "session_interrupt": "escape",
            "session_compact": "<leader>c",
            "session_rename": "ctrl+r",
            "status_view": "<leader>s",
            "messages_undo": "<leader>u",
            "agent_list": "<leader>a",
            "theme_list": "<leader>t",
            "editor_open": "<leader>e",
            "input_clear": "ctrl+c",
            "input_submit": "return",
        }
        for name, default in expected.items():
            self.assertEqual(DEFINITIONS[name][0], default, name)

    def test_every_definition_has_a_description(self):
        for name, (_default, description) in DEFINITIONS.items():
            self.assertTrue(description, name)

    def test_command_map_only_references_known_names(self):
        for name in COMMAND_MAP:
            self.assertIn(name, DEFINITIONS, name)

    def test_every_default_parses(self):
        for name, (default, _desc) in DEFINITIONS.items():
            parse_binding(default)  # must not raise
            if default != "none":
                self.assertTrue(parse_binding(default), name)


class TestParseBinding(unittest.TestCase):

    def test_bare_key(self):
        self.assertEqual(parse_binding("escape"), [Chord(key="escape")])

    def test_single_character(self):
        self.assertEqual(parse_binding("]"), [Chord(key="]")])

    def test_function_key(self):
        self.assertEqual(parse_binding("f2"), [Chord(key="f2")])

    def test_ctrl_modifier(self):
        self.assertEqual(parse_binding("ctrl+x"), [Chord(key="x", ctrl=True)])

    def test_shift_modifier(self):
        self.assertEqual(parse_binding("shift+f2"),
                         [Chord(key="f2", shift=True)])

    def test_two_modifiers(self):
        self.assertEqual(parse_binding("ctrl+alt+u"),
                         [Chord(key="u", ctrl=True, alt=True)])

    def test_three_modifiers(self):
        self.assertEqual(parse_binding("ctrl+alt+shift+k"),
                         [Chord(key="k", ctrl=True, alt=True, shift=True)])

    def test_comma_separated_alternatives(self):
        self.assertEqual(
            parse_binding("ctrl+c,ctrl+d,<leader>q"),
            [Chord(key="c", ctrl=True),
             Chord(key="d", ctrl=True),
             Chord(key="q", leader=True)])

    def test_whitespace_around_alternatives(self):
        self.assertEqual(parse_binding(" pageup , ctrl+alt+b "),
                         [Chord(key="pageup"),
                          Chord(key="b", ctrl=True, alt=True)])

    def test_leader_prefix(self):
        self.assertEqual(parse_binding("<leader>m"),
                         [Chord(key="m", leader=True)])

    def test_leader_prefix_with_named_key(self):
        self.assertEqual(parse_binding("<leader>down"),
                         [Chord(key="down", leader=True)])

    def test_leader_prefix_with_digit(self):
        self.assertEqual(parse_binding("<leader>1"),
                         [Chord(key="1", leader=True)])

    def test_leader_prefix_with_modifier(self):
        self.assertEqual(parse_binding("<leader>ctrl+g"),
                         [Chord(key="g", ctrl=True, leader=True)])

    def test_plus_key(self):
        self.assertEqual(parse_binding("ctrl++"), [Chord(key="+", ctrl=True)])
        self.assertEqual(parse_binding("+"), [Chord(key="+")])

    def test_punctuation_keys(self):
        self.assertEqual(parse_binding("ctrl+-"), [Chord(key="-", ctrl=True)])
        self.assertEqual(parse_binding("ctrl+."), [Chord(key=".", ctrl=True)])

    def test_uppercase_letter_means_shift(self):
        self.assertEqual(parse_binding("E"), [Chord(key="e", shift=True)])
        self.assertEqual(parse_binding("shift+i"), parse_binding("I"))

    def test_disabled_values(self):
        self.assertEqual(parse_binding("none"), [])
        self.assertEqual(parse_binding("NONE"), [])
        self.assertEqual(parse_binding(False), [])
        self.assertEqual(parse_binding(""), [])
        self.assertEqual(parse_binding(None), [])

    def test_list_value(self):
        self.assertEqual(parse_binding(["ctrl+p", "<leader>p"]),
                         [Chord(key="p", ctrl=True),
                          Chord(key="p", leader=True)])

    def test_object_value(self):
        self.assertEqual(parse_binding({"key": "ctrl+v", "preventDefault": False}),
                         [Chord(key="v", ctrl=True)])

    def test_duplicates_collapse(self):
        self.assertEqual(parse_binding("ctrl+p,ctrl+p"),
                         [Chord(key="p", ctrl=True)])

    def test_unsupported_modifier_is_dropped(self):
        self.assertEqual(parse_binding("super+z"), [])
        self.assertEqual(parse_binding("ctrl+-,super+z"),
                         [Chord(key="-", ctrl=True)])

    def test_garbage_never_raises(self):
        self.assertEqual(parse_binding("wat+q"), [])
        self.assertEqual(parse_binding(",,,"), [])
        self.assertEqual(parse_binding(123), [])
        self.assertEqual(parse_binding("<leader>"), [])

    def test_chord_text_round_trips(self):
        for text in ("ctrl+x", "shift+f2", "ctrl+alt+u", "escape", "<leader>q"):
            self.assertEqual(parse_binding(text)[0].text, text)


class TestAliases(unittest.TestCase):

    def test_return_aliases(self):
        for spelling in ("return", "enter", "cr", "RETURN"):
            self.assertEqual(parse_binding(spelling), [Chord(key="return")],
                             spelling)

    def test_escape_aliases(self):
        for spelling in ("esc", "escape", "ESC"):
            self.assertEqual(parse_binding(spelling), [Chord(key="escape")],
                             spelling)

    def test_page_aliases(self):
        self.assertEqual(parse_binding("pgup"), [Chord(key="pageup")])
        self.assertEqual(parse_binding("pgdn"), [Chord(key="pagedown")])

    def test_space_alias(self):
        self.assertEqual(parse_binding("spc"), [Chord(key="space")])
        self.assertEqual(parse_binding("space"), [Chord(key="space")])

    def test_other_aliases(self):
        self.assertEqual(parse_binding("del"), [Chord(key="delete")])
        self.assertEqual(parse_binding("bs"), [Chord(key="backspace")])
        self.assertEqual(parse_binding("ins"), [Chord(key="insert")])

    def test_modifier_aliases(self):
        self.assertEqual(parse_binding("control+a"), parse_binding("ctrl+a"))
        self.assertEqual(parse_binding("meta+f"), parse_binding("alt+f"))
        self.assertEqual(parse_binding("option+f"), parse_binding("alt+f"))

    def test_normalise_key_directly(self):
        self.assertEqual(normalise_key("Enter"), ("return", False))
        self.assertEqual(normalise_key("E"), ("e", True))
        self.assertEqual(normalise_key("e"), ("e", False))
        self.assertEqual(normalise_key(" "), ("space", False))
        self.assertEqual(normalise_key(""), ("", False))


class TestLookup(unittest.TestCase):

    def setUp(self):
        self.keymap = Keymap.default()

    def test_plain_binding(self):
        self.assertEqual(self.keymap.lookup(KeyEvent("escape")),
                         "session_interrupt")

    def test_ctrl_binding(self):
        self.assertEqual(self.keymap.lookup(KeyEvent("p", ctrl=True)),
                         "command_list")

    def test_function_key(self):
        self.assertEqual(self.keymap.lookup(KeyEvent("f2")),
                         "model_cycle_recent")

    def test_shift_function_key(self):
        self.assertEqual(self.keymap.lookup(KeyEvent("f2", shift=True)),
                         "model_cycle_recent_reverse")

    def test_ctrl_alt_binding(self):
        self.assertEqual(self.keymap.lookup(KeyEvent("u", ctrl=True, alt=True)),
                         "messages_half_page_up")

    def test_unbound_key_returns_none(self):
        self.assertIsNone(self.keymap.lookup(KeyEvent("f9")))

    def test_modifiers_must_match_exactly(self):
        self.assertIsNone(self.keymap.lookup(KeyEvent("p", ctrl=True, alt=True)))

    def test_uppercase_event_key_matches_shift_binding(self):
        keymap = Keymap.from_config(FakeConfig({"session_new": "shift+n"}))
        self.assertEqual(keymap.lookup(KeyEvent("N")), "session_new")

    def test_string_event_is_accepted(self):
        self.assertEqual(self.keymap.lookup("escape"), "session_interrupt")

    def test_priority_is_definition_order(self):
        # ctrl+c is app_exit and input_clear in opencode too; the first
        # definition wins unless the caller scopes the search.
        self.assertEqual(self.keymap.lookup(KeyEvent("c", ctrl=True)),
                         "app_exit")

    def test_among_scopes_the_search(self):
        self.assertEqual(
            self.keymap.lookup(KeyEvent("c", ctrl=True), among=["input_clear"]),
            "input_clear")

    def test_commands_for_lists_all_matches(self):
        found = self.keymap.commands_for(KeyEvent("c", ctrl=True))
        self.assertEqual(found, ["app_exit", "input_clear"])

    def test_leader_is_never_returned_as_a_command(self):
        self.assertIsNone(self.keymap.lookup(KeyEvent("x", ctrl=True)))

    def test_bad_event_type_raises(self):
        with self.assertRaises(TypeError):
            self.keymap.lookup(42)


class TestLeaderSequence(unittest.TestCase):

    def setUp(self):
        self.keymap = Keymap.default()

    def test_two_step_match(self):
        self.assertFalse(self.keymap.leader_pending)
        self.assertIsNone(self.keymap.lookup(KeyEvent("x", ctrl=True)))
        self.assertTrue(self.keymap.leader_pending)
        self.assertEqual(self.keymap.lookup(KeyEvent("n")), "session_new")
        self.assertFalse(self.keymap.leader_pending)

    def test_every_leader_binding(self):
        cases = {"m": "model_list", "l": "session_list", "c": "session_compact",
                 "s": "status_view", "u": "messages_undo", "a": "agent_list",
                 "t": "theme_list", "e": "editor_open", "q": "app_exit",
                 "1": "session_quick_switch_1"}
        for key, command in cases.items():
            self.keymap.lookup(KeyEvent("x", ctrl=True))
            self.assertEqual(self.keymap.lookup(KeyEvent(key)), command, key)

    def test_wrong_second_key_resets_cleanly(self):
        self.keymap.lookup(KeyEvent("x", ctrl=True))
        self.assertIsNone(self.keymap.lookup(KeyEvent("z")))
        self.assertFalse(self.keymap.leader_pending)
        # and the map keeps working afterwards
        self.assertEqual(self.keymap.lookup(KeyEvent("p", ctrl=True)),
                         "command_list")

    def test_second_key_does_not_fall_through_to_plain_binding(self):
        self.keymap.lookup(KeyEvent("x", ctrl=True))
        self.assertIsNone(self.keymap.lookup(KeyEvent("escape")))
        self.assertFalse(self.keymap.leader_pending)

    def test_leader_twice_stays_armed(self):
        self.keymap.lookup(KeyEvent("x", ctrl=True))
        self.assertIsNone(self.keymap.lookup(KeyEvent("x", ctrl=True)))
        self.assertTrue(self.keymap.leader_pending)
        self.assertEqual(self.keymap.lookup(KeyEvent("m")), "model_list")

    def test_reset_abandons_the_sequence(self):
        self.keymap.lookup(KeyEvent("x", ctrl=True))
        self.keymap.reset()
        self.assertFalse(self.keymap.leader_pending)
        self.assertEqual(self.keymap.lookup(KeyEvent("escape")),
                         "session_interrupt")

    def test_leader_binding_needs_the_leader_first(self):
        self.assertIsNone(self.keymap.lookup(KeyEvent("m")))

    def test_custom_leader(self):
        keymap = Keymap.from_config(FakeConfig({"leader": "ctrl+b"}))
        self.assertIsNone(keymap.lookup(KeyEvent("x", ctrl=True)))
        self.assertFalse(keymap.leader_pending)
        keymap.lookup(KeyEvent("b", ctrl=True))
        self.assertTrue(keymap.leader_pending)
        self.assertEqual(keymap.lookup(KeyEvent("n")), "session_new")

    def test_disabled_leader_kills_the_sequence(self):
        keymap = Keymap.from_config(FakeConfig({"leader": False}))
        self.assertIsNone(keymap.lookup(KeyEvent("x", ctrl=True)))
        self.assertFalse(keymap.leader_pending)
        self.assertIsNone(keymap.lookup(KeyEvent("n")))


class TestFromConfig(unittest.TestCase):

    def test_defaults_when_no_keybinds_section(self):
        keymap = Keymap.from_config(FakeConfig())
        self.assertEqual(keymap.bindings_for("model_list"),
                         [Chord(key="m", leader=True)])
        self.assertEqual(keymap.warnings, [])

    def test_override_replaces_default(self):
        keymap = Keymap.from_config(FakeConfig({"model_list": "ctrl+o"}))
        self.assertEqual(keymap.bindings_for("model_list"),
                         [Chord(key="o", ctrl=True)])
        self.assertEqual(keymap.lookup(KeyEvent("o", ctrl=True)), "model_list")
        self.assertEqual(keymap.warnings, [])

    def test_untouched_bindings_keep_defaults(self):
        keymap = Keymap.from_config(FakeConfig({"model_list": "ctrl+o"}))
        self.assertEqual(keymap.bindings_for("session_new"),
                         [Chord(key="n", leader=True)])

    def test_override_with_list(self):
        keymap = Keymap.from_config(FakeConfig({"status_view": ["ctrl+o", "f5"]}))
        self.assertEqual(keymap.bindings_for("status_view"),
                         [Chord(key="o", ctrl=True), Chord(key="f5")])

    def test_disable_with_false(self):
        keymap = Keymap.from_config(FakeConfig({"session_interrupt": False}))
        self.assertEqual(keymap.bindings_for("session_interrupt"), [])
        self.assertIsNone(keymap.lookup(KeyEvent("escape"),
                                        among=["session_interrupt"]))
        self.assertEqual(keymap.warnings, [])

    def test_disable_with_none_string(self):
        keymap = Keymap.from_config(FakeConfig({"command_list": "none"}))
        self.assertEqual(keymap.bindings_for("command_list"), [])
        self.assertEqual(keymap.warnings, [])

    def test_unknown_name_warns_but_does_not_raise(self):
        keymap = Keymap.from_config(FakeConfig({"teleport": "ctrl+t",
                                                "model_list": "ctrl+o"}))
        self.assertEqual(len(keymap.warnings), 1)
        self.assertIn("teleport", keymap.warnings[0])
        self.assertEqual(keymap.bindings_for("model_list"),
                         [Chord(key="o", ctrl=True)])

    def test_unparseable_override_warns_and_disables(self):
        keymap = Keymap.from_config(FakeConfig({"model_list": "super+m"}))
        self.assertEqual(keymap.bindings_for("model_list"), [])
        self.assertEqual(len(keymap.warnings), 1)
        self.assertIn("model_list", keymap.warnings[0])

    def test_non_object_keybinds_section_warns(self):
        keymap = Keymap.from_config(FakeConfig(keybinds=["ctrl+p"]))
        self.assertEqual(len(keymap.warnings), 1)
        self.assertEqual(keymap.bindings_for("command_list"),
                         [Chord(key="p", ctrl=True)])

    def test_plain_dict_is_accepted(self):
        keymap = Keymap.from_config({"keybinds": {"model_list": "ctrl+o"}})
        self.assertEqual(keymap.bindings_for("model_list"),
                         [Chord(key="o", ctrl=True)])

    def test_none_config_falls_back_to_defaults(self):
        keymap = Keymap.from_config(None)
        self.assertEqual(keymap.bindings_for("leader"),
                         parse_binding(LEADER_DEFAULT))

    def test_broken_config_object_never_raises(self):
        class Exploding:
            @property
            def data(self):
                raise RuntimeError("boom")

        keymap = Keymap.from_config(Exploding())
        self.assertEqual(len(keymap.warnings), 1)
        self.assertEqual(keymap.lookup(KeyEvent("p", ctrl=True)), "command_list")

    def test_instances_do_not_share_state(self):
        first = Keymap.from_config(FakeConfig({"model_list": "ctrl+o"}))
        second = Keymap.default()
        self.assertEqual(second.bindings_for("model_list"),
                         [Chord(key="m", leader=True)])
        first.lookup(KeyEvent("x", ctrl=True))
        self.assertFalse(second.leader_pending)


class TestDescribe(unittest.TestCase):

    def setUp(self):
        self.keymap = Keymap.default()

    def test_simple(self):
        self.assertEqual(self.keymap.describe("command_list"), "ctrl+p")

    def test_alternatives_are_joined(self):
        self.assertEqual(self.keymap.describe("messages_first"),
                         "ctrl+g, home")

    def test_leader_is_expanded(self):
        self.assertEqual(self.keymap.describe("session_new"), "ctrl+x n")
        self.assertEqual(self.keymap.describe("app_exit"),
                         "ctrl+c, ctrl+d, ctrl+x q")

    def test_leader_can_stay_literal(self):
        self.assertEqual(self.keymap.describe("session_new", expand_leader=False),
                         "<leader>n")

    def test_expansion_follows_custom_leader(self):
        keymap = Keymap.from_config(FakeConfig({"leader": "ctrl+b"}))
        self.assertEqual(keymap.describe("session_new"), "ctrl+b n")

    def test_modifier_order_is_stable(self):
        self.assertEqual(self.keymap.describe("messages_half_page_up"),
                         "ctrl+alt+u")

    def test_disabled_and_unknown_describe_as_empty(self):
        self.assertEqual(self.keymap.describe("mcp_list"), "")
        self.assertEqual(self.keymap.describe("no_such_command"), "")

    def test_bindings_for_unknown_is_empty(self):
        self.assertEqual(self.keymap.bindings_for("no_such_command"), [])

    def test_bindings_for_returns_a_copy(self):
        chords = self.keymap.bindings_for("command_list")
        chords.append(Chord(key="zzz"))
        self.assertEqual(len(self.keymap.bindings_for("command_list")), 1)


class TestHelpRows(unittest.TestCase):

    def setUp(self):
        self.rows = Keymap.default().help_rows()

    def test_leader_comes_first(self):
        self.assertEqual(self.rows[0],
                         ("ctrl+x", DEFINITIONS["leader"][1]))

    def test_rest_is_alphabetical_by_command(self):
        keymap = Keymap.default()
        names = [name for name, chords in keymap._bindings.items() if chords]
        expected = sorted(name for name in names if name != "leader")
        actual = [row[1] for row in self.rows[1:]]
        self.assertEqual(actual, [DEFINITIONS[name][1] for name in expected])

    def test_disabled_commands_are_omitted(self):
        descriptions = [row[1] for row in self.rows]
        self.assertNotIn(DEFINITIONS["mcp_list"][1], descriptions)

    def test_every_row_has_keys_and_a_description(self):
        for keys, description in self.rows:
            self.assertTrue(keys)
            self.assertTrue(description)

    def test_contains_a_known_leader_row(self):
        self.assertIn(("ctrl+x m", DEFINITIONS["model_list"][1]), self.rows)


class TestFromCurses(unittest.TestCase):

    def event(self, ch, **kwargs):
        return from_curses(ch, CURSES, **kwargs)

    def test_control_characters(self):
        self.assertEqual(self.event(1), KeyEvent("a", ctrl=True))
        self.assertEqual(self.event(16), KeyEvent("p", ctrl=True))
        self.assertEqual(self.event(24), KeyEvent("x", ctrl=True))
        self.assertEqual(self.event(26), KeyEvent("z", ctrl=True))

    def test_control_punctuation(self):
        self.assertEqual(self.event(0), KeyEvent("space", ctrl=True))
        self.assertEqual(self.event(28), KeyEvent("\\", ctrl=True))
        self.assertEqual(self.event(31), KeyEvent("_", ctrl=True))

    def test_named_control_characters(self):
        self.assertEqual(self.event(9), KeyEvent("tab"))
        self.assertEqual(self.event(13), KeyEvent("return"))
        self.assertEqual(self.event(27), KeyEvent("escape"))
        self.assertEqual(self.event(8), KeyEvent("backspace"))
        self.assertEqual(self.event(127), KeyEvent("backspace"))

    def test_newline_is_enter_by_default(self):
        self.assertEqual(self.event(10), KeyEvent("return"))

    def test_newline_can_be_ctrl_j(self):
        self.assertEqual(self.event(10, newline_is_enter=False),
                         KeyEvent("j", ctrl=True))

    def test_printable_characters(self):
        self.assertEqual(self.event(ord("a")), KeyEvent("a"))
        self.assertEqual(self.event(ord("7")), KeyEvent("7"))
        self.assertEqual(self.event(ord("]")), KeyEvent("]"))
        self.assertEqual(self.event(32), KeyEvent("space"))

    def test_uppercase_is_shift(self):
        self.assertEqual(self.event(ord("E")), KeyEvent("e", shift=True))

    def test_string_input(self):
        self.assertEqual(self.event("a"), KeyEvent("a"))
        self.assertEqual(self.event("\x01"), KeyEvent("a", ctrl=True))

    def test_arrow_keys(self):
        self.assertEqual(self.event(CURSES.KEY_UP), KeyEvent("up"))
        self.assertEqual(self.event(CURSES.KEY_DOWN), KeyEvent("down"))
        self.assertEqual(self.event(CURSES.KEY_LEFT), KeyEvent("left"))
        self.assertEqual(self.event(CURSES.KEY_RIGHT), KeyEvent("right"))

    def test_navigation_keys(self):
        self.assertEqual(self.event(CURSES.KEY_HOME), KeyEvent("home"))
        self.assertEqual(self.event(CURSES.KEY_END), KeyEvent("end"))
        self.assertEqual(self.event(CURSES.KEY_PPAGE), KeyEvent("pageup"))
        self.assertEqual(self.event(CURSES.KEY_NPAGE), KeyEvent("pagedown"))
        self.assertEqual(self.event(CURSES.KEY_BACKSPACE), KeyEvent("backspace"))
        self.assertEqual(self.event(CURSES.KEY_DC), KeyEvent("delete"))
        self.assertEqual(self.event(CURSES.KEY_ENTER), KeyEvent("return"))

    def test_function_keys(self):
        self.assertEqual(self.event(CURSES.KEY_F0 + 1), KeyEvent("f1"))
        self.assertEqual(self.event(CURSES.KEY_F0 + 2), KeyEvent("f2"))
        self.assertEqual(self.event(CURSES.KEY_F0 + 12), KeyEvent("f12"))

    def test_shifted_function_keys(self):
        # ncurses reports shift+F2 as F14.
        self.assertEqual(self.event(CURSES.KEY_F0 + 14),
                         KeyEvent("f2", shift=True))

    def test_shifted_navigation_keys(self):
        self.assertEqual(self.event(CURSES.KEY_BTAB), KeyEvent("tab", shift=True))
        self.assertEqual(self.event(CURSES.KEY_SLEFT), KeyEvent("left", shift=True))
        self.assertEqual(self.event(CURSES.KEY_SEND), KeyEvent("end", shift=True))
        self.assertEqual(self.event(CURSES.KEY_SDC), KeyEvent("delete", shift=True))

    def test_alt_sequence_as_pair(self):
        self.assertEqual(self.event((27, ord("u"))), KeyEvent("u", alt=True))

    def test_alt_sequence_as_string(self):
        self.assertEqual(self.event("\x1bf"), KeyEvent("f", alt=True))

    def test_alt_plus_control_sequence(self):
        self.assertEqual(self.event((27, 21)), KeyEvent("u", ctrl=True, alt=True))

    def test_alt_special_key_sequence(self):
        self.assertEqual(self.event((27, CURSES.KEY_LEFT)),
                         KeyEvent("left", alt=True))

    def test_meta_high_bit(self):
        self.assertEqual(self.event(128 + ord("b")), KeyEvent("b", alt=True))
        self.assertEqual(self.event(128 + 2), KeyEvent("b", ctrl=True, alt=True))

    def test_unmapped_values(self):
        self.assertEqual(self.event(-1), KeyEvent(""))
        self.assertEqual(self.event(500), KeyEvent(""))
        self.assertEqual(self.event(""), KeyEvent(""))

    def test_resize_and_mouse(self):
        self.assertEqual(self.event(CURSES.KEY_RESIZE), KeyEvent("resize"))
        self.assertEqual(self.event(CURSES.KEY_MOUSE), KeyEvent("mouse"))

    def test_works_without_a_curses_module(self):
        # No module argument: the fallback table still resolves ncurses codes.
        self.assertEqual(from_curses(259), KeyEvent("up"))
        self.assertEqual(from_curses(1), KeyEvent("a", ctrl=True))

    def test_module_imports_without_curses(self):
        self.assertNotIn("curses", dir(keybind))


class TestCursesToCommand(unittest.TestCase):
    """The full path a TUI takes: getch() value -> command name."""

    def setUp(self):
        self.keymap = Keymap.default()

    def press(self, ch):
        return self.keymap.lookup(from_curses(ch, CURSES))

    def test_command_palette(self):
        self.assertEqual(self.press(16), "command_list")

    def test_leader_then_model_list(self):
        self.assertIsNone(self.press(24))
        self.assertEqual(self.press(ord("m")), "model_list")

    def test_page_up(self):
        self.assertEqual(self.press(CURSES.KEY_PPAGE), "messages_page_up")

    def test_interrupt(self):
        self.assertEqual(self.press(27), "session_interrupt")

    def test_submit(self):
        self.assertEqual(self.keymap.lookup(from_curses(13, CURSES),
                                            among=["input_submit"]),
                         "input_submit")

    def test_half_page_up_via_alt_sequence(self):
        self.assertEqual(self.press((27, 21)), "messages_half_page_up")

    def test_shift_tab_cycles_agent_backwards(self):
        self.assertEqual(self.press(CURSES.KEY_BTAB), "agent_cycle_reverse")

    def test_shift_f2_cycles_model_backwards(self):
        self.assertEqual(self.press(CURSES.KEY_F0 + 14),
                         "model_cycle_recent_reverse")


class TestNonAsciiInput(unittest.TestCase):
    """get_wch() hands back real characters as str; they must stay characters."""

    def setUp(self):
        self.keymap = Keymap.default()

    def test_latin1_letters_are_not_meta_sequences(self):
        for char in "æøå":
            event = from_curses(char, CURSES)
            self.assertEqual(event, KeyEvent(char))
            self.assertIsNone(self.keymap.lookup(event))

    def test_uppercase_latin1_implies_shift(self):
        self.assertEqual(from_curses("Æ", CURSES),
                         KeyEvent("æ", shift=True))

    def test_characters_above_latin1_survive(self):
        self.assertEqual(from_curses("中", CURSES), KeyEvent("中"))
        self.assertEqual(from_curses("—", CURSES), KeyEvent("—"))

    def test_int_high_bit_is_still_meta(self):
        # Only ints can be the meta(True) high-bit form.
        self.assertEqual(from_curses(128 + ord("f"), CURSES),
                         KeyEvent("f", alt=True))

    def test_escape_prefixed_non_ascii_is_alt(self):
        self.assertEqual(from_curses("\x1bæ", CURSES),
                         KeyEvent("æ", alt=True))


class TestIsDisabled(unittest.TestCase):

    def test_opencode_disabling_values(self):
        for value in (False, None, "none", "NONE", " none ", "", "   ", [], ()):
            self.assertTrue(is_disabled(value), value)

    def test_bindings_are_not_disabled(self):
        for value in ("ctrl+c", ["ctrl+c"], {"key": "ctrl+c"}, 0, True):
            self.assertFalse(is_disabled(value), value)

    def test_zero_is_not_false(self):
        # 0 == False, so a naive membership test would disable the command.
        self.assertFalse(is_disabled(0))

    def test_empty_list_disables_without_warning(self):
        keymap = Keymap.from_config(FakeConfig({"app_exit": []}))
        self.assertEqual(keymap.bindings_for("app_exit"), [])
        self.assertEqual(keymap.warnings, [])


class TestKeyStrokeObjects(unittest.TestCase):
    """opencode's BindingValueSchema also allows objects, not just strings."""

    def test_binding_object_with_string_key(self):
        self.assertEqual(parse_binding({"key": "ctrl+v", "preventDefault": False}),
                         [Chord(key="v", ctrl=True)])

    def test_bare_stroke(self):
        self.assertEqual(parse_binding({"name": "v", "ctrl": True}),
                         [Chord(key="v", ctrl=True)])

    def test_stroke_meta_means_alt(self):
        self.assertEqual(parse_binding({"name": "f", "meta": True}),
                         [Chord(key="f", alt=True)])

    def test_nested_stroke(self):
        self.assertEqual(parse_binding({"key": {"name": "z", "ctrl": True,
                                                "shift": True}}),
                         [Chord(key="z", ctrl=True, shift=True)])

    def test_unsupported_modifiers_drop_the_stroke(self):
        self.assertEqual(parse_binding({"name": "a", "super": True}), [])
        self.assertEqual(parse_binding({"name": "a", "hyper": True}), [])

    def test_stroke_without_a_name(self):
        self.assertEqual(parse_binding({"ctrl": True}), [])

    def test_mixed_list(self):
        self.assertEqual(parse_binding(["ctrl+q", {"name": "F2"}]),
                         [Chord(key="q", ctrl=True), Chord(key="f2")])

    def test_config_accepts_a_stroke(self):
        keymap = Keymap.from_config(FakeConfig({"model_list": {"name": "m",
                                                              "ctrl": True}}))
        self.assertEqual(keymap.warnings, [])
        self.assertEqual(keymap.bindings_for("model_list"),
                         [Chord(key="m", ctrl=True)])


class TestCommandMapCoverage(unittest.TestCase):

    def test_every_input_command_has_an_id(self):
        # opencode maps all of these; a palette built on COMMAND_MAP would
        # silently lose them otherwise.
        for name in ("input_move_left", "input_line_home", "input_backspace",
                     "input_delete", "input_word_forward",
                     "input_delete_word_backward"):
            self.assertIn(name, COMMAND_MAP)
        self.assertEqual(COMMAND_MAP["input_line_home"], "input.line.home")

    def test_only_dotted_group_names_are_unmapped(self):
        unmapped = [name for name in DEFINITIONS
                    if name not in COMMAND_MAP and name != "leader"]
        self.assertTrue(all("." in name for name in unmapped), unmapped)


if __name__ == "__main__":
    unittest.main()
