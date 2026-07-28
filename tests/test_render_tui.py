"""
Tests for the screen renderer itself, not for any TUI.

The renderer is the ruler every later TUI judgement is made with, so these
tests pin down the emulator's own behaviour: positioning, erasing, editing,
scrolling, deferred autowrap, and -- the bug that made an earlier version
report a blank screen -- that unknown escape sequences are consumed whole
instead of leaking their letters into the grid.
"""

import os
import sys
import tempfile
import unittest

from tests.render_tui import Screen, parse_key, pty, run_tui, unescape

# Drawn by ncurses, then compared against our reconstruction of the byte
# stream it produced. ncurses' own instr() is the oracle: it reports what the
# library believes is on screen, so agreement means the emulator understood
# every optimisation ncurses chose (scroll regions, IL/DL, ECH, insert mode).
NCURSES_PROBE = r'''
import curses, locale, random, sys
locale.setlocale(locale.LC_ALL, "")
OUT, SEED = sys.argv[1], int(sys.argv[2])
WORDS = ["haikode", "provider", "keystore", "permission", "kjort", "tokens",
         "a-rather-long-word-that-will-need-truncating", "ok"]

def main(stdscr):
    rnd = random.Random(SEED)
    rows, cols = stdscr.getmaxyx()
    curses.curs_set(0)
    for _ in range(80):
        y, x = rnd.randrange(rows), rnd.randrange(cols)
        attr = rnd.choice([0, curses.A_BOLD, curses.A_REVERSE])
        try:
            stdscr.addnstr(y, x, rnd.choice(WORDS), cols - x - 1, attr)
        except curses.error:
            pass
    stdscr.scrollok(True)
    stdscr.setscrreg(2, rows - 3)
    for i in range(20):
        stdscr.move(rows - 3, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(rows - 3, 0, "scrolled %d %s" % (i, rnd.choice(WORDS)), cols - 1)
        stdscr.scroll(1)
        if i % 5 == 0:
            stdscr.refresh()
    stdscr.setscrreg(0, rows - 1)
    for _ in range(12):
        y = rnd.randrange(rows)
        stdscr.move(y, 0)
        stdscr.insertln() if rnd.random() < 0.5 else stdscr.deleteln()
        stdscr.addnstr(y, rnd.randrange(cols // 2), rnd.choice(WORDS), cols // 2 - 1)
    for _ in range(6):
        stdscr.move(rnd.randrange(rows), rnd.randrange(cols))
        stdscr.clrtoeol()
    stdscr.refresh()
    with open(OUT, "w") as fh:
        fh.write("\n".join(stdscr.instr(y, 0, cols).decode("utf-8", "replace")
                           for y in range(rows)))
    curses.napms(4000)

curses.wrapper(main)
'''


def filled(rows=4, cols=8, char="x"):
    screen = Screen(rows, cols)
    for row in range(rows):
        screen.feed("\x1b[%d;1H" % (row + 1))
        screen.feed(char * cols)
    return screen


class TextOutputTests(unittest.TestCase):
    def test_plain_text_lands_on_the_first_row(self):
        screen = Screen(3, 10).feed("hi")
        self.assertEqual(screen.row(0), "hi")
        self.assertEqual(screen.cursor, (0, 2))
        self.assertEqual(screen.text(), "hi\n\n")

    def test_row_text_keeps_full_width_but_row_strips(self):
        screen = Screen(2, 6).feed("ab")
        self.assertEqual(screen.row_text(0), "ab    ")
        self.assertEqual(screen.row(0), "ab")

    def test_carriage_return_and_line_feed(self):
        screen = Screen(3, 10).feed("one\r\ntwo")
        self.assertEqual(screen.row(0), "one")
        self.assertEqual(screen.row(1), "two")

    def test_backspace_moves_left_and_overwrites(self):
        screen = Screen(2, 10).feed("abc\b\bX")
        self.assertEqual(screen.row(0), "aXc")

    def test_tab_advances_to_the_next_eight_column_stop(self):
        screen = Screen(2, 30).feed("a\tb\tc")
        self.assertEqual(screen.row(0), "a       b       c")

    def test_find_and_nonblank_rows(self):
        screen = Screen(5, 20).feed("\x1b[3;5Hneedle")
        self.assertEqual(screen.find("needle"), (2, 4))
        self.assertIsNone(screen.find("haystack"))
        self.assertEqual(screen.nonblank_rows(), 1)
        self.assertEqual(screen.find_all("e"), [(2, 5), (2, 6), (2, 9)])


class AbsolutePositioningTests(unittest.TestCase):
    def test_cup_with_both_parameters(self):
        screen = Screen(6, 20).feed("\x1b[3;5HX")
        self.assertEqual(screen.find("X"), (2, 4))

    def test_cup_with_no_parameters_is_home(self):
        screen = Screen(6, 20).feed("\x1b[4;4Hy\x1b[HX")
        self.assertEqual(screen.find("X"), (0, 0))

    def test_cup_with_omitted_parameters_defaults_to_one(self):
        self.assertEqual(Screen(6, 20).feed("\x1b[;5HX").find("X"), (0, 4))
        self.assertEqual(Screen(6, 20).feed("\x1b[3HX").find("X"), (2, 0))
        self.assertEqual(Screen(6, 20).feed("\x1b[;HX").find("X"), (0, 0))

    def test_hvp_lowercase_f_is_the_same_as_cup(self):
        self.assertEqual(Screen(6, 20).feed("\x1b[2;3fX").find("X"), (1, 2))

    def test_cup_clamps_to_the_screen(self):
        screen = Screen(4, 6).feed("\x1b[99;99HX")
        self.assertEqual(screen.find("X"), (3, 5))

    def test_cha_and_vpa(self):
        screen = Screen(5, 20).feed("\x1b[4dr\x1b[10Gc")
        self.assertEqual(screen.find("r"), (3, 0))
        self.assertEqual(screen.find("c"), (3, 9))

    def test_hpa_backtick_is_column_addressing(self):
        self.assertEqual(Screen(3, 20).feed("\x1b[7`X").find("X"), (0, 6))


class RelativeMoveTests(unittest.TestCase):
    def test_cuu_cud_cuf_cub(self):
        screen = Screen(6, 20).feed("\x1b[3;5H")
        screen.feed("\x1b[2A")
        self.assertEqual(screen.cursor, (0, 4))
        screen.feed("\x1b[3B")
        self.assertEqual(screen.cursor, (3, 4))
        screen.feed("\x1b[2C")
        self.assertEqual(screen.cursor, (3, 6))
        screen.feed("\x1b[4D")
        self.assertEqual(screen.cursor, (3, 2))

    def test_relative_moves_default_to_one(self):
        screen = Screen(6, 20).feed("\x1b[3;5H\x1b[A\x1b[C")
        self.assertEqual(screen.cursor, (1, 5))

    def test_relative_moves_clamp_at_the_edges(self):
        screen = Screen(4, 6).feed("\x1b[1;1H\x1b[9A\x1b[9D")
        self.assertEqual(screen.cursor, (0, 0))
        screen.feed("\x1b[9B\x1b[9C")
        self.assertEqual(screen.cursor, (3, 5))

    def test_cnl_and_cpl_move_to_column_zero(self):
        screen = Screen(6, 20).feed("\x1b[3;9H\x1b[E")
        self.assertEqual(screen.cursor, (3, 0))
        screen.feed("\x1b[9G\x1b[2F")
        self.assertEqual(screen.cursor, (1, 0))

    def test_save_and_restore_cursor(self):
        screen = Screen(6, 20).feed("\x1b[3;5H\x1b7\x1b[1;1H\x1b8X")
        self.assertEqual(screen.find("X"), (2, 4))
        screen.feed("\x1b[2;2H\x1b[s\x1b[6;6H\x1b[uY")
        self.assertEqual(screen.find("Y"), (1, 1))


class EraseTests(unittest.TestCase):
    def test_ed2_clears_everything_and_keeps_the_cursor(self):
        screen = filled()
        screen.feed("\x1b[2;3H\x1b[2J")
        self.assertEqual(screen.nonblank_rows(), 0)
        self.assertEqual(screen.cursor, (1, 2))

    def test_ed0_clears_from_the_cursor_down(self):
        screen = filled(4, 8)
        screen.feed("\x1b[2;3H\x1b[0J")
        self.assertEqual(screen.row(0), "xxxxxxxx")
        self.assertEqual(screen.row(1), "xx")
        self.assertEqual(screen.row(2), "")
        self.assertEqual(screen.row(3), "")

    def test_ed_without_parameter_means_zero(self):
        screen = filled(3, 4)
        screen.feed("\x1b[2;3H\x1b[J")
        self.assertEqual(screen.row(1), "xx")
        self.assertEqual(screen.row(2), "")

    def test_ed1_clears_up_to_and_including_the_cursor(self):
        screen = filled(3, 6)
        screen.feed("\x1b[2;3H\x1b[1J")
        self.assertEqual(screen.row(0), "")
        self.assertEqual(screen.row_text(1), "   xxx")
        self.assertEqual(screen.row(2), "xxxxxx")

    def test_el0_clears_to_end_of_line(self):
        screen = filled(2, 6)
        screen.feed("\x1b[1;3H\x1b[K")
        self.assertEqual(screen.row(0), "xx")
        self.assertEqual(screen.row(1), "xxxxxx")

    def test_el1_clears_to_start_of_line_inclusive(self):
        screen = filled(2, 6)
        screen.feed("\x1b[1;3H\x1b[1K")
        self.assertEqual(screen.row_text(0), "   xxx")

    def test_el2_clears_the_whole_line(self):
        screen = filled(2, 6)
        screen.feed("\x1b[1;3H\x1b[2K")
        self.assertEqual(screen.row(0), "")
        self.assertEqual(screen.row(1), "xxxxxx")

    def test_ech_erases_in_place_without_shifting(self):
        screen = Screen(2, 8).feed("abcdefgh\x1b[1;3H\x1b[2X")
        self.assertEqual(screen.row_text(0), "ab  efgh")


class EditingTests(unittest.TestCase):
    def test_ich_shifts_right_and_drops_at_the_margin(self):
        screen = Screen(2, 6).feed("abcdef\x1b[1;3H\x1b[2@")
        self.assertEqual(screen.row_text(0), "ab  cd")

    def test_dch_shifts_left_and_pads_with_blanks(self):
        screen = Screen(2, 6).feed("abcdef\x1b[1;3H\x1b[2P")
        self.assertEqual(screen.row_text(0), "abef  ")

    def test_il_pushes_lines_down_inside_the_screen(self):
        screen = Screen(4, 4).feed("a\r\nb\r\nc")
        screen.feed("\x1b[2;1H\x1b[L")
        self.assertEqual(screen.lines(), ["a", "", "b", "c"])

    def test_dl_pulls_lines_up_and_blanks_the_bottom(self):
        screen = Screen(4, 4).feed("a\r\nb\r\nc\r\nd")
        screen.feed("\x1b[2;1H\x1b[2M")
        self.assertEqual(screen.lines(), ["a", "d", "", ""])

    def test_insert_mode_pushes_characters_right(self):
        screen = Screen(2, 6).feed("abcd\x1b[1;1H\x1b[4hXY\x1b[4l")
        self.assertEqual(screen.row(0), "XYabcd")
        screen.feed("\x1b[1;1HZ")
        self.assertEqual(screen.row(0), "ZYabcd")

    def test_rep_repeats_the_last_graphic_character(self):
        screen = Screen(2, 10).feed("-\x1b[4b")
        self.assertEqual(screen.row(0), "-----")


class AutowrapTests(unittest.TestCase):
    def test_text_wraps_at_the_right_margin(self):
        screen = Screen(3, 5).feed("abcdefg")
        self.assertEqual(screen.row(0), "abcde")
        self.assertEqual(screen.row(1), "fg")

    def test_wrap_is_deferred_until_the_next_character(self):
        # After filling the last column the cursor stays put; a CR here must
        # bring us back to the SAME line, not the next one.
        screen = Screen(3, 5).feed("abcde")
        self.assertEqual(screen.cursor, (0, 4))
        screen.feed("\rX")
        self.assertEqual(screen.row(0), "Xbcde")
        self.assertEqual(screen.row(1), "")

    def test_sgr_does_not_cancel_a_pending_wrap(self):
        screen = Screen(3, 3).feed("abc\x1b[0md")
        self.assertEqual(screen.row(0), "abc")
        self.assertEqual(screen.row(1), "d")

    def test_autowrap_off_overwrites_the_last_column(self):
        screen = Screen(3, 5).feed("\x1b[?7labcdefg")
        self.assertEqual(screen.row(0), "abcdg")
        self.assertEqual(screen.row(1), "")

    def test_wrapping_at_the_bottom_scrolls(self):
        screen = Screen(2, 3).feed("abcdefghi")
        self.assertEqual(screen.lines(), ["def", "ghi"])


class ScrollRegionTests(unittest.TestCase):
    def numbered(self):
        screen = Screen(5, 4)
        for row in range(5):
            screen.feed("\x1b[%d;1H%d" % (row + 1, row + 1))
        return screen

    def test_line_feed_at_the_region_bottom_scrolls_only_the_region(self):
        screen = self.numbered()
        screen.feed("\x1b[2;4r\x1b[4;1H\n")
        self.assertEqual(screen.lines(), ["1", "3", "4", "", "5"])
        self.assertEqual(screen.cursor, (3, 0))

    def test_decstbm_homes_the_cursor(self):
        screen = self.numbered()
        screen.feed("\x1b[2;4r")
        self.assertEqual(screen.cursor, (0, 0))

    def test_reverse_index_at_the_region_top_scrolls_down(self):
        screen = self.numbered()
        screen.feed("\x1b[2;4r\x1b[2;1H\x1bM")
        self.assertEqual(screen.lines(), ["1", "", "2", "3", "5"])

    def test_cursor_up_stops_at_the_region_top(self):
        screen = self.numbered()
        screen.feed("\x1b[2;4r\x1b[4;1H\x1b[9A")
        self.assertEqual(screen.cursor, (1, 0))

    def test_su_and_sd_move_the_region(self):
        screen = self.numbered()
        screen.feed("\x1b[2S")
        self.assertEqual(screen.lines(), ["3", "4", "5", "", ""])
        screen.feed("\x1b[1T")
        self.assertEqual(screen.lines(), ["", "3", "4", "5", ""])

    def test_su_inside_a_region_leaves_the_rest_alone(self):
        screen = self.numbered()
        screen.feed("\x1b[2;4r\x1b[1S")
        self.assertEqual(screen.lines(), ["1", "3", "4", "", "5"])

    def test_il_inside_a_region_never_pushes_past_the_bottom(self):
        screen = self.numbered()
        screen.feed("\x1b[2;4r\x1b[2;1H\x1b[L")
        self.assertEqual(screen.lines(), ["1", "", "2", "3", "5"])

    def test_an_impossible_region_is_ignored_entirely(self):
        screen = self.numbered()
        screen.feed("\x1b[2;4r\x1b[3;3H")
        screen.feed("\x1b[4;2r")  # bottom above top: no region change, no home
        self.assertEqual((screen.top, screen.bottom), (1, 3))
        self.assertEqual(screen.cursor, (2, 2))

    def test_reset_region_with_bare_r(self):
        screen = self.numbered()
        screen.feed("\x1b[2;4r\x1b[r\x1b[5;1H\n")
        self.assertEqual(screen.lines(), ["2", "3", "4", "5", ""])

    def test_origin_mode_makes_cup_relative_to_the_region(self):
        screen = self.numbered()
        screen.feed("\x1b[2;4r\x1b[?6h\x1b[1;1HX")
        self.assertEqual(screen.find("X"), (1, 0))
        screen.feed("\x1b[?6l\x1b[1;1HY")
        self.assertEqual(screen.find("Y"), (0, 0))


class UnknownSequenceTests(unittest.TestCase):
    def test_long_unknown_csi_is_fully_consumed(self):
        seq = "\x1b[?1;2;3;4;5;6;7;8;9;10;11;12;13;14;15;16;17;18;19;20$p"
        screen = Screen(3, 40).feed("A" + seq + "B")
        self.assertEqual(screen.row(0), "AB")

    def test_private_and_intermediate_forms_do_not_leak(self):
        noise = [
            "\x1b[>4;2m",          # xterm modifyOtherKeys
            "\x1b[?2004h",         # bracketed paste
            "\x1b[?25l\x1b[?25h",  # hide/show cursor
            "\x1b[3 q",            # DECSCUSR, with an intermediate
            "\x1b[\"q",            # DECSCA
            "\x1b[999;999;999t",   # window manipulation
            "\x1b[<0;1;1M",        # SGR mouse report
            "\x1b[=5c",            # DA3
        ]
        for seq in noise:
            screen = Screen(3, 40).feed("A" + seq + "B")
            self.assertEqual(screen.row(0), "AB", "leaked: %r" % seq)

    def test_osc_terminated_by_bel_or_st(self):
        screen = Screen(2, 30).feed("A\x1b]0;window title\x07B")
        self.assertEqual(screen.row(0), "AB")
        self.assertEqual(screen.title, "window title")
        screen = Screen(2, 30).feed("A\x1b]2;other\x1b\\B")
        self.assertEqual(screen.row(0), "AB")
        self.assertEqual(screen.title, "other")

    def test_osc_split_across_feeds(self):
        screen = Screen(2, 30)
        screen.feed("A\x1b]0;win")
        screen.feed("dow\x07B")
        self.assertEqual(screen.row(0), "AB")
        self.assertEqual(screen.title, "window")

    def test_dcs_string_is_swallowed(self):
        screen = Screen(2, 30).feed("A\x1bP+q62656c\x1b\\B")
        self.assertEqual(screen.row(0), "AB")

    def test_charset_selection_and_keypad_modes(self):
        for seq in ("\x1b(B", "\x1b)0", "\x1b*A", "\x1b=", "\x1b>", "\x1b#8",
                    "\x1b%G", "\x0e", "\x0f"):
            screen = Screen(2, 20).feed("A" + seq + "B")
            self.assertEqual(screen.row(0), "AB", "leaked: %r" % seq)

    def test_sgr_never_reaches_the_grid(self):
        screen = Screen(3, 40)
        screen.feed("\x1b[1;31mRED\x1b[0m \x1b[38;5;196mX\x1b[m\x1b[m done")
        self.assertEqual(screen.row(0), "RED X done")
        self.assertNotIn("[", screen.text())
        self.assertNotIn("m", screen.text().replace("done", ""))

    def test_escape_sequence_split_across_feeds(self):
        screen = Screen(4, 20)
        screen.feed("\x1b[3")
        screen.feed(";5")
        screen.feed("HX")
        self.assertEqual(screen.find("X"), (2, 4))

    def test_alternate_screen_hides_and_restores_the_primary_buffer(self):
        screen = Screen(3, 10).feed("primary")
        screen.feed("\x1b[?1049h")
        self.assertEqual(screen.nonblank_rows(), 0)
        screen.feed("alt")
        self.assertEqual(screen.row(0), "alt")
        screen.feed("\x1b[?1049l")
        self.assertEqual(screen.row(0), "primary")

    def test_leaving_the_alternate_screen_keeps_the_last_image(self):
        screen = Screen(3, 10)
        self.assertIsNone(screen.alt_exit_frame())
        screen.feed("\x1b[?1049hdrawn\x1b[?1049l")
        self.assertEqual(screen.nonblank_rows(), 0)
        leftover = screen.alt_exit_frame()
        self.assertIsNotNone(leftover)
        self.assertIn("|drawn     |", leftover)

    def test_ris_resets_the_screen(self):
        screen = Screen(3, 10).feed("junk\x1b[2;4r\x1bc")
        self.assertEqual(screen.nonblank_rows(), 0)
        self.assertEqual((screen.top, screen.bottom), (0, 2))


class DecodingTests(unittest.TestCase):
    def test_utf8_split_across_feeds(self):
        data = "kjørt ✓".encode("utf-8")
        screen = Screen(2, 20)
        screen.feed(data[:3])
        screen.feed(data[3:9])
        screen.feed(data[9:])
        self.assertEqual(screen.row(0), "kjørt ✓")

    def test_utf8_fed_one_byte_at_a_time(self):
        screen = Screen(2, 20)
        for byte in "æøå ⏺".encode("utf-8"):
            screen.feed(bytes([byte]))
        self.assertEqual(screen.row(0), "æøå ⏺")

    def test_str_input_is_accepted_directly(self):
        self.assertEqual(Screen(2, 10).feed("rå").row(0), "rå")

    def test_invalid_bytes_do_not_raise(self):
        screen = Screen(2, 10).feed(b"a\xffb")
        self.assertIn("a", screen.row(0))
        self.assertIn("b", screen.row(0))


class ReplyTests(unittest.TestCase):
    def test_cursor_position_report(self):
        screen = Screen(10, 20).feed("\x1b[3;5H\x1b[6n")
        self.assertEqual(screen.take_replies(), [b"\x1b[3;5R"])
        self.assertEqual(screen.take_replies(), [])

    def test_device_attributes_are_answered(self):
        screen = Screen(4, 10).feed("\x1b[c\x1b[5n")
        self.assertEqual(screen.take_replies(), [b"\x1b[?1;2c", b"\x1b[0n"])


class KeySpecTests(unittest.TestCase):
    def test_parse_key_splits_delay_from_payload(self):
        self.assertEqual(parse_key("1.5:hi\\r"), (1.5, b"hi\r"))
        self.assertEqual(parse_key("hi"), (0.0, b"hi"))
        self.assertEqual(parse_key("0:\\e[A"), (0.0, b"\x1b[A"))

    def test_unescape_handles_hex_and_specials(self):
        self.assertEqual(unescape("a\\x1b[Bb"), "a\x1b[Bb")
        self.assertEqual(unescape("\\t\\n\\\\"), "\t\n\\")


@unittest.skipIf(pty is None, "pty is unavailable on this platform")
class RunTuiTests(unittest.TestCase):
    def test_program_output_reaches_the_grid(self):
        screen = run_tui([sys.executable, "-c", "print('hello')"],
                         rows=10, cols=40, settle=0.2, timeout=20)
        self.assertEqual(screen.find("hello"), (0, 0))
        self.assertEqual(screen.nonblank_rows(), 1)
        self.assertFalse(screen.timed_out)

    def test_child_sees_the_requested_window_size(self):
        code = ("import shutil;"
                "s=shutil.get_terminal_size();"
                "print('size', s.columns, s.lines)")
        screen = run_tui([sys.executable, "-c", code],
                         rows=17, cols=63, settle=0.2, timeout=20)
        self.assertIsNotNone(screen.find("size 63 17"))

    def test_keys_are_delivered(self):
        code = ("import sys;"
                "line=sys.stdin.readline();"
                "print('got', line.strip())")
        screen = run_tui([sys.executable, "-c", code], rows=8, cols=40,
                         keys=[(0.3, "abc\r")], settle=0.4, timeout=20)
        self.assertIsNotNone(screen.find("got abc"))

    def test_timeout_returns_the_screen_instead_of_hanging(self):
        code = "import sys,time; sys.stdout.write('waiting'); sys.stdout.flush(); time.sleep(30)"
        screen = run_tui([sys.executable, "-c", code], rows=6, cols=20,
                         settle=5.0, timeout=1.5)
        self.assertTrue(screen.timed_out)
        self.assertEqual(screen.row(0), "waiting")
        self.assertLess(screen.elapsed, 10.0)

    def test_a_slow_starting_program_is_still_captured(self):
        # The settle timer must not expire while the child is still booting,
        # or a perfectly good screen gets reported as blank.
        code = ("import sys,time; time.sleep(1.2);"
                "sys.stdout.write('late but present'); sys.stdout.flush();"
                "time.sleep(5)")
        screen = run_tui([sys.executable, "-c", code], rows=6, cols=30,
                         settle=0.3, timeout=20)
        self.assertEqual(screen.row(0), "late but present")

    def test_escape_sequences_from_the_child_are_interpreted(self):
        code = r"import sys; sys.stdout.write('\x1b[3;5Hmarker\x1b[1;1Htop')"
        screen = run_tui([sys.executable, "-c", code], rows=8, cols=30,
                         settle=0.2, timeout=20)
        self.assertEqual(screen.find("marker"), (2, 4))
        self.assertEqual(screen.find("top"), (0, 0))

    def test_frame_and_summary_describe_the_capture(self):
        screen = run_tui([sys.executable, "-c", "print('ok')"],
                         rows=4, cols=12, settle=0.2, timeout=20)
        frame = screen.frame().splitlines()
        self.assertEqual(len(frame), 6)
        self.assertEqual(frame[0], "+" + "-" * 12 + "+")
        self.assertTrue(all(len(line) == 14 for line in frame))
        self.assertIn("nonblank_rows=1", screen.summary())
        self.assertIn("exit=0", screen.summary())


@unittest.skipIf(pty is None, "pty is unavailable on this platform")
class NcursesAgreementTests(unittest.TestCase):
    """The instrument is only trustworthy if it agrees with ncurses itself."""

    def render(self, seed, rows, cols):
        temp = tempfile.mkdtemp()
        script = os.path.join(temp, "probe.py")
        expected_path = os.path.join(temp, "expected.txt")
        with open(script, "w") as fh:
            fh.write(NCURSES_PROBE)
        screen = run_tui([sys.executable, script, expected_path, str(seed)],
                         rows=rows, cols=cols, settle=0.6, timeout=25)
        if not os.path.exists(expected_path):
            self.skipTest("curses could not run here (TERM=%r, exit=%s)"
                          % (os.environ.get("TERM"), screen.exit_status))
        with open(expected_path) as fh:
            expected = fh.read().split("\n")
        return screen, expected

    def test_reconstruction_matches_what_ncurses_thinks_it_drew(self):
        for seed, rows, cols in ((1, 24, 80), (2, 31, 97)):
            with self.subTest(seed=seed):
                screen, expected = self.render(seed, rows, cols)
                self.assertTrue(screen.alt_screen,
                                "captured after the program tore down its screen")
                got = [screen.row_text(i) for i in range(rows)]
                self.assertEqual(len(expected), rows)
                for index in range(rows):
                    self.assertEqual(expected[index].rstrip(),
                                     got[index].rstrip(),
                                     "row %d disagrees with ncurses" % index)


class WideCharacterTests(unittest.TestCase):
    def test_glyphs_the_tui_uses_are_never_flagged_as_wide(self):
        screen = Screen(3, 40).feed("— • ⏺ ✓ ┌─┐ │ ▏ « » æøå")
        self.assertEqual(screen.wide_chars(), [])
        self.assertNotIn("WARNING", screen.summary())

    def test_double_width_characters_are_reported_not_hidden(self):
        screen = Screen(3, 20).feed("\x1b[2;3H日本")
        self.assertEqual(screen.wide_chars(),
                         [(1, 2, "日"), (1, 3, "本")])
        self.assertIn("WARNING", screen.summary())


if __name__ == "__main__":
    unittest.main()
