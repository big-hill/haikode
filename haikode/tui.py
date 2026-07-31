"""
Full-screen curses TUI — the front end that makes haikode feel like opencode.

The module is deliberately split in two halves:

  * a pure layer (Glyphs, Line, Entry, Transcript, wrap_text, build_*, ...)
    that turns agent events into styled lines and knows nothing about curses,
    so all the fiddly formatting is unit-testable without a terminal;
  * the TUI class, the ONLY place curses is touched.

Threading contract: the agent runs on a worker thread and never touches
curses. It posts events onto a queue that the main thread drains between
key polls. Permission requests originate on the worker thread, are handed to
the main thread through the same queue, and the worker blocks on a
threading.Event until the main thread has drawn the modal and answered.
"""

import locale
import os
import platform
import queue
import random
import re
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import keybind, palette, status, usage
from .palette import PaletteItem, SelectList
from .turn import (ASYNC, MODAL, TURN, TurnController, command_mode,
                   command_name, prepare_init)
from .usage import ContextState, UsageTracker, measure_context

try:  # keep the pure layer importable where curses is unavailable
    import curses
except ImportError:  # pragma: no cover - Haiku and macOS both ship curses
    curses = None

MIN_COLS = 20
MIN_ROWS = 8

# Visible lines of a tool result before it is folded away.
RESULT_LINES = 8
DIFF_LINES = 24
MAX_INPUT_ROWS = 8
MAX_ENTRIES = 2000

# Poll interval of getch(); also the spinner frame rate.
TICK_MS = 90
# Events drained per tick — bounded so a fast stream cannot starve the keyboard.
MAX_EVENTS_PER_TICK = 512

# The home prompt: opencode caps it at 75 columns and centres it.
BOX_MAX_WIDTH = 75
BOX_MIN_WIDTH = 24
BOX_MARGIN = 8

# Below this the wordmark is dropped for a one-line header: four rows of block
# glyphs plus the setup summary plus the prompt does not fit, and a home screen
# that overflows is worse than no home screen.
HOME_MIN_ROWS = 24
HOME_MIN_COLS = 60

# collect() shells out to git, so the home screen reuses its answer.
SETUP_TTL = 5.0
# measure_context() re-reads AGENTS.md and re-prices the tool schemas.
CONTEXT_TTL = 4.0

# Commands the REPL answers by rebuilding its agent. It rebuilds its own
# reference, not ours, so without re-pulling the factory the screen would keep
# naming the provider, model and auth of the session the user just left.
REPROVISION_COMMANDS = frozenset(
    {"/provider", "/model", "/login", "/logout", "/reload"})

# SGR colour escapes as emitted by the REPL's _c(); curses draws them
# literally, so transcript text must be plain.
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

# Keybind names the main screen answers to, and the method each one runs.
# Scoped rather than flat because several commands share a chord on purpose:
# ctrl+a is model_provider_list here and input_line_home in opencode's input
# widget, while ctrl+r renames from either the session dialog or the main view.
# Restricting the lookup to this table is how the TUI declares its focus.
BINDING_ACTIONS: Dict[str, str] = {
    "app_exit": "_quit_app",
    "command_list": "_open_commands",
    "help_show": "_open_help",
    "status_view": "_open_status",
    "session_export": "_export_session",
    "session_new": "_new_session",
    "session_list": "_open_sessions",
    "session_rename": "_start_rename",
    "session_compact": "_compact_session",
    "model_list": "_open_models",
    "model_provider_list": "_open_providers",
    "model_cycle_recent": "_cycle_recent_next",
    "model_cycle_recent_reverse": "_cycle_recent_previous",
    "agent_list": "_open_agents",
    "agent_cycle": "_cycle_agent_next",
    "agent_cycle_reverse": "_cycle_agent_previous",
    "variant_cycle": "_cycle_effort",
    "messages_page_up": "_page_up",
    "messages_page_down": "_page_down",
    "messages_line_up": "_line_up",
    "messages_line_down": "_line_down",
    "messages_half_page_up": "_half_page_up",
    "messages_half_page_down": "_half_page_down",
    "messages_first": "_first_message",
    "messages_last": "_last_message",
    "messages_undo": "_undo_message",
    "terminal_suspend": "_suspend_terminal",
}

# Real handlers for commands opencode leaves unbound by default. They join the
# dispatch table when a user assigns a chord, without making the default-table
# invariant claim that an empty binding has a key.
OPTIONAL_BINDING_ACTIONS: Dict[str, str] = {
    "tool_details": "_toggle_expand",
    "display_thinking": "_toggle_reasoning",
    "prompt_submit": "_on_enter",
}

# Bound names for features this curses port does not have. A custom binding is
# still consumed and reports that fact in the footer; silently typing its chord
# into the prompt made configuration failures indistinguishable from bad keys.
UNAVAILABLE_BINDINGS: Tuple[str, ...] = (
    "app_debug",
    "app_toggle_animations",
    "app_toggle_file_context",
    "app_toggle_session_directory_filter",
    "docs_open",
    "editor_open",
    "theme_list",
    "theme_switch_mode",
    "sidebar_toggle",
    "scrollbar_toggle",
    "debug_view",
    "session_copy",
    "session_timeline",
    "session_fork",
    "session_share",
    "session_unshare",
    "session_toggle_timestamps",
    "session_toggle_generic_tool_output",
    "session_queued_prompts",
    "session_quick_switch_1",
    "session_quick_switch_2",
    "session_quick_switch_3",
    "session_quick_switch_4",
    "session_quick_switch_5",
    "session_quick_switch_6",
    "session_quick_switch_7",
    "session_quick_switch_8",
    "session_quick_switch_9",
    "model_cycle_favorite",
    "model_cycle_favorite_reverse",
    "mcp_list",
    "provider_connect",
    "variant_list",
    "messages_next",
    "messages_previous",
    "messages_copy",
    "messages_redo",
    "prompt_skills",
    "tips_toggle",
)

# Input and modal names are looked up only in their focused widget. Keeping the
# lists explicit both resolves shared chords correctly and makes every setting
# in keybind.DEFINITIONS part of a real dispatch path.
INPUT_COMMANDS: Tuple[str, ...] = (
    "session_interrupt",
    "input_clear",
    "input_paste",
    "input_submit",
    "input_newline",
    "input_move_left",
    "input_move_right",
    "input_move_up",
    "input_move_down",
    "input_line_home",
    "input_line_end",
    "input_buffer_home",
    "input_buffer_end",
    "input_delete_to_line_end",
    "input_delete_to_line_start",
    "input_backspace",
    "input_delete",
    "input_word_forward",
    "input_word_backward",
    "input_delete_word_forward",
    "input_delete_word_backward",
    "history_previous",
    "history_next",
    "prompt.autocomplete.prev",
    "prompt.autocomplete.next",
    "prompt.autocomplete.hide",
    "prompt.autocomplete.select",
    "prompt.autocomplete.complete",
)

DIALOG_BINDINGS: Tuple[str, ...] = (
    "session_rename",
    "session_delete",
    "model_favorite_toggle",
    "dialog.select.prev",
    "dialog.select.next",
    "dialog.select.page_up",
    "dialog.select.page_down",
    "dialog.select.home",
    "dialog.select.end",
    "dialog.select.submit",
    "dialog.select.cancel",
    "dialog.prompt.submit",
    "permission.prompt.fullscreen",
)

SPECIAL_BINDINGS: Tuple[str, ...] = ("leader",)
TOP_LEVEL_COMMANDS: Tuple[str, ...] = (
    tuple(BINDING_ACTIONS) + tuple(OPTIONAL_BINDING_ACTIONS)
    + UNAVAILABLE_BINDINGS)

# Commands the palette lists itself rather than taking from the command layer:
# the TUI owns the screen, so /new, /help and friends must run here.
SLASH_SHADOWED = frozenset({
    "help", "new", "clear", "exit", "quit", "sessions", "resume", "model",
    "models", "provider", "reasoning", "cost",
})


# --------------------------------------------------------------------------
# pure layer: glyphs
# --------------------------------------------------------------------------


class Glyphs:
    """Box-drawing/marker characters with an ASCII fallback.

    Haiku's Terminal is UTF-8, but haikode is also run over serial and inside
    `TERM=vt100` sessions where writing U+23FA throws, so every decorative
    character goes through here.
    """

    def __init__(self, unicode_ok: bool = True):
        self.unicode_ok = unicode_ok
        if unicode_ok:
            self.dot = "⏺"           # ⏺
            self.ellipsis = "…"      # …
            self.arrow = "❯"         # ❯
            self.bullet = "•"        # •
            self.check = "✔"         # ✔
            self.cross = "✘"         # ✘
            self.hbar = "─"          # ─
            self.vbar = "│"          # │
            self.corners = "┌┐└┘"
            self.spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
        else:
            self.dot = "*"
            self.ellipsis = "..."
            self.arrow = ">"
            self.bullet = "-"
            self.check = "+"
            self.cross = "x"
            self.hbar = "-"
            self.vbar = "|"
            self.corners = "++++"
            self.spinner = "|/-\\"

    @classmethod
    def detect(cls, encoding: Optional[str] = None) -> "Glyphs":
        """Pick a glyph set from the encoding curses will actually write in.

        nl_langinfo(CODESET) is what CPython's curses uses to encode addstr
        arguments, so asking anything else risks picking glyphs the screen
        driver then refuses.
        """
        if encoding is None:
            for source in (
                    lambda: locale.nl_langinfo(locale.CODESET),
                    lambda: locale.getpreferredencoding(False),
                    lambda: getattr(sys.stdout, "encoding", "")):
                try:
                    encoding = source()
                except Exception:
                    encoding = ""
                if encoding:
                    break
        normalized = (encoding or "").lower().replace("-", "").replace("_", "")
        return cls(unicode_ok=normalized.startswith("utf"))

    def frame(self, index: int) -> str:
        return self.spinner[index % len(self.spinner)]


# Punctuation the UI (and every model) emits constantly. Turning each of these
# into "?" on a vt100 makes ordinary prose unreadable, so they fold to the
# ASCII spelling they were meant to be; everything else still becomes "?".
_ASCII_FOLD = {
    "—": "-", "–": "-", "‑": "-",   # em/en/non-breaking dash
    "·": "-", "•": "-",                  # middle dot, bullet
    "…": "...",
    "“": '"', "”": '"', "‘": "'", "’": "'",
    "→": "->",
}


def sanitize(text: str, unicode_ok: bool = True, keep_newlines: bool = True) -> str:
    """Make a string safe to hand to curses.addstr.

    Control characters corrupt the screen and unencodable characters raise, so
    both are replaced here rather than at every draw site. Newlines survive by
    default because sanitising happens before wrapping; the draw helper asks
    for them to be folded away.
    """
    text = text.replace("\t", "    ").replace("\r\n", "\n").replace("\r", "\n")
    out = []
    for ch in text:
        code = ord(ch)
        if code == 10:
            out.append("\n" if keep_newlines else " ")
            continue
        if code < 32 or code == 127:
            continue
        if not unicode_ok and code > 126:
            out.append(_ASCII_FOLD.get(ch, "?"))
            continue
        out.append(ch)
    return "".join(out)


# --------------------------------------------------------------------------
# pure layer: the wordmark
# --------------------------------------------------------------------------

# opencode keeps its wordmark as two halves of a four-row block font and paints
# four "mark" characters specially; that is what gives the letters their inset
# shadow (packages/tui/src/logo.ts and util/presentation.ts). haikode reuses the
# encoding verbatim so it is literally the same typeface — the o, d and e glyph
# shapes below are opencode's, h/a/i/k are drawn in the same 4x5 half-block grid:
#
#   "_"  a space painted in the shadow colour   (the counter of a letter)
#   "^"  the upper half block, foreground on the shadow colour
#   "~"  the upper half block in the shadow colour
#   ","  the lower half block in the shadow colour
#   " "  a plain space; every other character is the foreground colour
#
# The left half is dim and the right half bright, so "hai" recedes and "kode"
# carries the eye, exactly as "open"/"code" do.

# Haiku's own emblem is a leaf, so the wordmark carries one. It leans right
# like the system logo, with the stem drawn in the shadow tone so the blade
# reads first. Every row is the same width, which keeps the centring maths in
# the home view honest.
# Haiku's emblem is a leaf, so the wordmark carries one. Both versions were
# drawn by the project owner. Braille gives 2x4 sub-cell resolution, which is
# why the blade reads as a curve rather than a staircase; the hand-drawn ASCII
# leaf stands in when the terminal's codeset cannot carry braille.
LEAF = [
    "⠀⠀⠀⠀⠀⠀⠀⠀⣀⣠⣤⣤⣄⣀⣀⡀⠀⠀⠀",
    "⠀⠀⠀⠀⠀⢀⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣶⠶",
    "⠀⠀⠀⠀⢠⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠃⠀",
    "⠀⠀⠀⢀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋⠀⠀⠀",
    "⢀⣠⠞⠋⠉⠛⠻⠿⣿⣿⣿⠿⠟⠋⠀⠀⠀⠀⠀",
    "⠞⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀",
]
LEAF_ASCII = [
    "   |   ",
    " .'|'. ",
    "/.'|\\ \\",
    "| /|'.|",
    " \\ |\\/ ",
    "  \\|/  ",
    "   `   ",
]
LEAF_GAP = "  "

WORDMARK_LEFT = [
    "▄" + " " * 9 + "▀",
    "█▀▀▄  ▀▀█ █",
    "█__█ █^^█ █",
    "▀~~▀ ▀▀▀▀ ▀",
]
WORDMARK_RIGHT = [
    "▄" + " " * 12 + "▄" + " " * 5,
    "█ ▄▀ █▀▀█ █▀▀█ █▀▀█",
    "█▀▄  █__█ █__█ █^^^",
    "▀ ▀▀ ▀▀▀▀ ▀▀▀▀ ▀▀▀▀",
]

# The same wordmark for terminals whose codeset cannot carry block glyphs.
# Split at the same place, so the two-tone treatment survives the fallback.
WORDMARK_ASCII_LEFT = [
    " _           _ ",
    "| |_   __ _ (_)",
    "| ' \\ / _` || |",
    "|_||_|\\__,_||_|",
]
WORDMARK_ASCII_RIGHT = [
    " _            _      ",
    "| |__ ___  __| | ___ ",
    "| / // _ \\/ _` |/ -_)",
    "|_\\_\\\\___/\\__,_|\\___|",
]

# mark -> (glyph, style suffix). The suffix picks the colour pair: "" is the
# half's own colour, "_fill" adds the shadow background, "_shadow" draws in the
# shadow colour itself.
_MARK_RENDER = {
    "_": (" ", "_fill"),
    "^": ("▀", "_fill"),
    "~": ("▀", "_shadow"),
    ",": ("▄", "_shadow"),
}


def _wordmark_runs(line: str, half: str) -> List[Tuple[str, str]]:
    """Expand one encoded row into (text, style) runs, merging equal styles."""
    runs: List[Tuple[str, str]] = []
    for char in line:
        glyph, suffix = _MARK_RENDER.get(char, (char, ""))
        style = half + suffix
        if runs and runs[-1][1] == style:
            runs[-1] = (runs[-1][0] + glyph, style)
        else:
            runs.append((glyph, style))
    return runs


def wordmark_rows(unicode_ok: bool = True) -> List[List[Tuple[str, str]]]:
    """The wordmark as rows of (text, style) runs, ready to hand to addstr.

    Styles are half-specific because the halves are drawn in different colours;
    the caller only has to look them up in its style table. The ASCII wordmark
    is NOT mark-encoded — its letters are built from "_", "^" and "," — so it
    keeps the two-tone split and nothing else.
    """
    if not unicode_ok:
        letters = [[(WORDMARK_ASCII_LEFT[index], "logo_dim"), (line, "logo_bright")]
                   for index, line in enumerate(WORDMARK_ASCII_RIGHT)]
    else:
        letters = []
        for index, line in enumerate(WORDMARK_LEFT):
            runs = _wordmark_runs(line, "logo_dim")
            runs.append((" ", "logo_dim"))
            runs.extend(_wordmark_runs(WORDMARK_RIGHT[index], "logo_bright"))
            letters.append(runs)

    # Pad the rows the lettering does not reach, so every row stays the same
    # width and the home view can centre the block as one unit.
    blade_rows = LEAF if unicode_ok else LEAF_ASCII
    offset = max(0, (len(blade_rows) - len(letters)) // 2)
    width = max(sum(len(text) for text, _ in row) for row in letters)
    rows = []
    for index, blade in enumerate(blade_rows):
        runs = [(blade + LEAF_GAP, "logo_leaf")]
        letter_index = index - offset
        if 0 <= letter_index < len(letters):
            runs.extend(letters[letter_index])
        else:
            runs.append((" " * width, "logo_dim"))
        rows.append(runs)
    return rows


def wordmark_width(unicode_ok: bool = True) -> int:
    return max(sum(len(text) for text, _ in row)
               for row in wordmark_rows(unicode_ok))


# --------------------------------------------------------------------------
# pure layer: home screen text
# --------------------------------------------------------------------------

# opencode rotates example prompts here (routes/home.tsx); the owner found
# the fake-looking pre-filled text distracting, so the composer stays clean.
PLACEHOLDERS = ("",)


def placeholder_text(index: int, width: int = 0) -> str:
    text = "Ask anything"
    if width <= 0 or len(text) <= width:
        return text
    return status.truncate(text, width)


def hint_line(unicode_ok: bool = True, width: int = 0) -> str:
    """The home hint. Narrow screens lose whole clauses rather than being
    clipped mid-word, which would read as a rendering bug."""
    separator = " · " if unicode_ok else " | "
    parts = ["/help for commands", "@file to attach", "esc to interrupt"]
    while parts:
        text = separator.join(parts)
        if width <= 0 or len(text) <= width:
            return text
        parts.pop()
    return ""


# --------------------------------------------------------------------------
# pure layer: text wrapping
# --------------------------------------------------------------------------


_TOKENS = re.compile(r"\s+|\S+")


def _wrap_line(line: str, width: int, first: str, cont: str) -> List[str]:
    """Greedy word wrap of one logical line, keeping its leading indent."""
    stripped = line.lstrip(" ")
    lead = line[:len(line) - len(stripped)]
    # A source line indented past the pane is pointless; clamp it.
    if len(first) + len(lead) >= width:
        lead = ""
    first_prefix = first + lead
    cont_prefix = cont + lead
    if len(cont_prefix) >= width:
        cont_prefix = cont

    out: List[str] = []
    prefix = first_prefix
    current = prefix
    pending_space = ""

    for token in _TOKENS.findall(stripped):
        if token.isspace():
            if len(current) > len(prefix):
                pending_space = " " * len(token)
            continue
        while token:
            candidate = current + pending_space
            room = width - len(candidate)
            if room >= len(token):
                current = candidate + token
                pending_space = ""
                break
            if len(current) > len(prefix):
                out.append(current.rstrip())
                prefix = cont_prefix
                current = prefix
                pending_space = ""
                continue
            # A single token wider than the pane: hard-break it.
            room = max(1, width - len(current))
            out.append(current + token[:room])
            token = token[room:]
            prefix = cont_prefix
            current = prefix
            pending_space = ""
    out.append(current.rstrip() if current.strip() else "")
    return [entry[:width] for entry in out]


def wrap_text(text: str, width: int, first: str = "", cont: Optional[str] = None) -> List[str]:
    """Wrap `text` to `width`, preserving explicit newlines and blank lines.

    `first` prefixes the first visual row, `cont` every following row
    (defaults to `first`), which is how tool output gets its hanging indent.
    """
    if width < 1:
        width = 1
    if cont is None:
        cont = first
    if text == "":
        return [""]
    rows: List[str] = []
    used_first = False
    for logical in text.expandtabs(4).replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not logical.strip():
            rows.append("")
            continue
        rows.extend(_wrap_line(logical, width, cont if used_first else first, cont))
        used_first = True
    return rows or [""]


def truncate_lines(lines: Sequence[str], limit: int, ellipsis: str = "…") -> List[str]:
    """Fold a long block down to `limit` rows plus a "… +N lines" marker."""
    if limit <= 0 or len(lines) <= limit:
        return list(lines)
    dropped = len(lines) - limit
    plural = "" if dropped == 1 else "s"
    return list(lines[:limit]) + ["%s +%d line%s" % (ellipsis, dropped, plural)]


def format_tokens(count: int) -> str:
    """Compact token counter for the footer: 940, 12.3k, 1.4M."""
    try:
        count = int(count)
    except (TypeError, ValueError):
        return "0"
    if count < 1000:
        return str(count)
    if count < 1000000:
        return ("%.1fk" % (count / 1000.0)).replace(".0k", "k")
    return ("%.1fM" % (count / 1000000.0)).replace(".0M", "M")


def format_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return "%ds" % int(seconds)
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return "%dm%02ds" % (minutes, rest)
    hours, minutes = divmod(minutes, 60)
    return "%dh%02dm" % (hours, minutes)


# --------------------------------------------------------------------------
# pure layer: diffs and tool summaries
# --------------------------------------------------------------------------


def classify_diff_line(line: str) -> str:
    """Map one unified-diff line to a style name.

    The +++/--- headers must be tested before the bare +/- cases or every
    diff header shows up as an added and a removed line.
    """
    if line.startswith("+++") or line.startswith("---"):
        return "diff_meta"
    if line.startswith("@@"):
        return "diff_hunk"
    if line.startswith("diff ") or line.startswith("index ") or line.startswith("\\"):
        return "diff_meta"
    if line.startswith("+"):
        return "diff_add"
    if line.startswith("-"):
        return "diff_del"
    return "diff_ctx"


# Which argument identifies a call, in the order we prefer to show it.
TOOL_ARG_KEYS: Dict[str, Sequence[str]] = {
    "read": ("filePath",),
    "write": ("filePath",),
    "edit": ("filePath",),
    "bash": ("description", "command"),
    "grep": ("pattern",),
    "glob": ("pattern",),
    "list": ("path",),
    "webfetch": ("url",),
    "task": ("description", "prompt"),
}


def summarize_tool(name: str, args: Optional[Dict[str, Any]], limit: int = 120) -> str:
    """One-line argument summary, e.g. summarize_tool("read", ...) -> "src/main.py"."""
    args = args or {}
    if name == "todowrite":
        todos = args.get("todos")
        count = len(todos) if isinstance(todos, list) else 0
        return "%d item%s" % (count, "" if count == 1 else "s")

    value = None
    for key in TOOL_ARG_KEYS.get(name, ()):
        candidate = args.get(key)
        if isinstance(candidate, str) and candidate.strip():
            value = candidate
            break
    if value is None:
        for candidate in args.values():
            if isinstance(candidate, str) and candidate.strip():
                value = candidate
                break
    if value is None:
        return ""

    value = " ".join(value.strip().split("\n")[0].split())
    if len(value) > limit:
        value = value[:limit - 1] + "…"
    return value


# --------------------------------------------------------------------------
# pure layer: transcript model
# --------------------------------------------------------------------------


class Line:
    """One rendered row: the text plus the name of the style to draw it in."""

    __slots__ = ("text", "style")

    def __init__(self, text: str, style: str = "assistant"):
        self.text = text
        self.style = style

    def __eq__(self, other):
        return (isinstance(other, Line) and other.text == self.text
                and other.style == self.style)

    def __repr__(self):  # pragma: no cover - debugging aid
        return "Line(%r, %r)" % (self.text, self.style)


class RenderOptions:
    """Everything that changes how entries render; also the line-cache key."""

    def __init__(self, glyphs: Optional[Glyphs] = None, show_reasoning: bool = False,
                 expand: bool = False, result_lines: int = RESULT_LINES,
                 diff_lines: int = DIFF_LINES):
        self.glyphs = glyphs or Glyphs(True)
        self.show_reasoning = show_reasoning
        self.expand = expand
        self.result_lines = result_lines
        self.diff_lines = diff_lines

    def key(self):
        return (self.glyphs.unicode_ok, self.show_reasoning, self.expand,
                self.result_lines, self.diff_lines)


class Entry:
    """One transcript item. Mutable: streaming text and tool results land here."""

    __slots__ = ("kind", "text", "name", "detail", "output", "diff", "error",
                 "denied", "meta", "_lines", "_key")

    def __init__(self, kind: str, text: str = "", name: str = "", detail: str = "",
                 output: str = "", diff: str = "", error: str = "",
                 denied: str = "", meta: Optional[Dict[str, Any]] = None):
        self.kind = kind
        self.text = text
        self.name = name
        self.detail = detail
        self.output = output
        self.diff = diff
        self.error = error
        self.denied = denied
        self.meta = meta or {}
        self._lines: Optional[List[Line]] = None
        self._key = None

    def bump(self):
        """Drop the cached rendering after a mutation."""
        self._lines = None

    def append_text(self, chunk: str):
        self.text += chunk
        self.bump()

    def lines(self, width: int, opts: RenderOptions) -> List[Line]:
        key = (width, opts.key())
        if self._lines is None or self._key != key:
            self._lines = build_entry_lines(self, width, opts)
            self._key = key
        return self._lines


def _styled(text: str, width: int, style: str, first: str = "",
            cont: Optional[str] = None) -> List[Line]:
    return [Line(row, style) for row in wrap_text(text, width, first, cont)]


def _truncate_styled(lines: List[Line], limit: int, glyphs: Glyphs,
                     indent: str = "  ") -> List[Line]:
    if limit <= 0 or len(lines) <= limit:
        return lines
    dropped = len(lines) - limit
    plural = "" if dropped == 1 else "s"
    marker = "%s%s +%d line%s" % (indent, glyphs.ellipsis, dropped, plural)
    return lines[:limit] + [Line(marker, "hint")]


def build_diff_lines(diff: str, width: int, opts: RenderOptions,
                     indent: str = "  ") -> List[Line]:
    """Render a unified diff with per-line +/- colouring."""
    out: List[Line] = []
    for raw in diff.replace("\r\n", "\n").split("\n"):
        if raw == "" and not out:
            continue
        style = classify_diff_line(raw)
        # Diff rows are clipped, not wrapped: a wrapped +/- line loses its
        # marker column and stops reading as a diff.
        text = sanitize(indent + raw, opts.glyphs.unicode_ok)[:width]
        out.append(Line(text, style))
    while out and out[-1].text.strip() == "":
        out.pop()
    return _truncate_styled(out, 0 if opts.expand else opts.diff_lines,
                            opts.glyphs, indent)


def build_user_lines(entry: Entry, width: int, opts: RenderOptions) -> List[Line]:
    g = opts.glyphs
    body = sanitize(entry.text, g.unicode_ok)
    return _styled(body, width, "user", "%s " % g.arrow, "  ")


def build_assistant_lines(entry: Entry, width: int, opts: RenderOptions) -> List[Line]:
    """The agent's own words, marked as such.

    Everything else in the transcript carries a sign of what it is — the user
    gets ❯, a tool gets ⏺ and its output is dimmed and indented. The reply
    carried none, so the one thing the user is meant to read looked like more
    tool output. A left rule down its whole height is the cheapest mark that
    survives wrapping: it stays visible on the tenth line of a long answer,
    where a single leading glyph would have scrolled out of sight.
    """
    g = opts.glyphs
    body = sanitize(entry.text, g.unicode_ok)
    if not body.strip():
        return []
    rule = "%s " % (g.vbar if g.unicode_ok else "|")
    return _styled(body, width, "assistant", rule, rule)


def build_reasoning_lines(entry: Entry, width: int, opts: RenderOptions) -> List[Line]:
    if not opts.show_reasoning:
        return []
    body = sanitize(entry.text, opts.glyphs.unicode_ok)
    if not body.strip():
        return []
    return _styled(body, width, "reasoning", "  ", "  ")


# Todo statuses -> (marker, style). opencode's component/todo-item.tsx draws
# "[✓]" done, "[•]" in progress and "[ ]" pending, and paints only the running
# item in the warning colour; the marker set here is the same, plus the
# cancelled state haikode's todowrite tool accepts.
TODO_STYLES: Dict[str, Tuple[str, str]] = {
    "completed": ("check", "diff_add"),
    "in_progress": ("bullet", "warn"),
    "cancelled": ("cross", "hint"),
    "pending": (" ", "result"),
}

# The pinned band above the prompt: how many rows it may take, and how much
# transcript must survive next to it.
MAX_PINNED_TODO_ROWS = 6
MIN_BODY_ROWS = 4

# A todo list is a plan, not tool spew, so it is folded much later than
# ordinary output — a 12-step plan cut off at 8 rows is worse than useless.
TODO_LINES = 16


def build_todo_lines(todos: Sequence[Any], width: int,
                     opts: RenderOptions) -> List[Line]:
    """Render a todowrite payload as a checklist block.

    Accepts the raw list the tool stores in its metadata; anything that is not
    a {"content": ..., "status": ...} mapping is skipped rather than raising,
    because this runs from the draw loop on data a model produced.
    """
    g = opts.glyphs
    out: List[Line] = []
    for raw in todos or []:
        if not isinstance(raw, dict):
            continue
        content = str(raw.get("content", "")).strip()
        if not content:
            continue
        status_name = str(raw.get("status", "pending"))
        marker, style = TODO_STYLES.get(status_name, TODO_STYLES["pending"])
        glyph = getattr(g, marker, marker) if marker != " " else " "
        prefix = "  [%s] " % glyph
        out.extend(_styled(sanitize(content, g.unicode_ok), width, style,
                           prefix, " " * len(prefix)))
    return _truncate_styled(out, 0 if opts.expand else TODO_LINES, g)


def build_pinned_todo_lines(todos: Sequence[Any], width: int,
                            opts: RenderOptions,
                            limit: int = MAX_PINNED_TODO_ROWS) -> List[Line]:
    """The always-visible plan above the prompt.

    Everything still to do is shown; finished items only fill the rows left
    over, newest first, so a long list degrades into "what remains" rather
    than "what happened". Returns [] when there is nothing outstanding, which
    is what collapses the band.
    """
    g = opts.glyphs
    items = [raw for raw in todos or []
             if isinstance(raw, dict) and str(raw.get("content", "")).strip()]
    if not items:
        return []
    open_items = [raw for raw in items
                  if str(raw.get("status", "pending")) in ("pending",
                                                           "in_progress")]
    if not open_items:
        return []
    done = [raw for raw in items if raw not in open_items]
    header_rows = 1
    room = max(0, limit - header_rows)
    shown = open_items[:room]
    if len(shown) < room:
        shown = done[-(room - len(shown)):] + shown
    hidden = len(items) - len(shown)
    label = "Plan"
    if hidden > 0:
        label += "  (+%d more, /todos)" % hidden
    out: List[Line] = _styled(sanitize(label, g.unicode_ok), width, "header",
                              "  ", "  ")
    for raw in shown:
        status_name = str(raw.get("status", "pending"))
        marker, style = TODO_STYLES.get(status_name, TODO_STYLES["pending"])
        glyph = getattr(g, marker, marker) if marker != " " else " "
        prefix = "  [%s] " % glyph
        text = status.truncate(str(raw.get("content", "")).strip(),
                               max(4, width - len(prefix)))
        out.extend(_styled(sanitize(text, g.unicode_ok), width, style,
                           prefix, " " * len(prefix)))
    return out[:limit]


def build_tool_lines(entry: Entry, width: int, opts: RenderOptions) -> List[Line]:
    g = opts.glyphs
    head = "%s %s" % (g.dot, entry.name or "tool")
    if entry.detail:
        head += "  " + entry.detail
    out = _styled(sanitize(head, g.unicode_ok), width, "tool", "", "    ")

    todos = entry.meta.get("todos")
    if entry.name == "todowrite" and isinstance(todos, list) and not entry.error \
            and not entry.denied:
        # The tool's own output is the same list as indented JSON; the
        # checklist says the same thing in a quarter of the rows.
        out.extend(build_todo_lines(todos, width, opts))
        return out

    if entry.error:
        out.extend(_styled(sanitize(entry.error, g.unicode_ok), width, "error",
                           "  %s " % g.cross, "    "))
        return out
    if entry.denied:
        out.extend(_styled(sanitize(entry.denied, g.unicode_ok), width, "denied",
                           "  %s " % g.cross, "    "))
        return out
    if entry.diff:
        out.extend(build_diff_lines(entry.diff, width, opts))
        return out
    if entry.output.strip():
        # Indented under its own ⏺ header rather than beside it: with dim
        # unavailable this is the only thing left that says "this is what the
        # tool printed, not what the agent told you".
        body = _styled(sanitize(entry.output, g.unicode_ok), width, "result",
                       "    ", "    ")
        out.extend(_truncate_styled(body, 0 if opts.expand else opts.result_lines, g))
    return out


def build_error_lines(entry: Entry, width: int, opts: RenderOptions) -> List[Line]:
    g = opts.glyphs
    body = sanitize(entry.text, g.unicode_ok)
    return _styled(body, width, "error", "%s " % g.cross, "  ")


def build_denied_lines(entry: Entry, width: int, opts: RenderOptions) -> List[Line]:
    g = opts.glyphs
    body = sanitize(entry.text, g.unicode_ok)
    return _styled(body, width, "denied", "%s " % g.cross, "  ")


def build_info_lines(entry: Entry, width: int, opts: RenderOptions) -> List[Line]:
    body = sanitize(entry.text, opts.glyphs.unicode_ok)
    style = entry.name or "info"
    indent = str(entry.meta.get("indent", ""))
    if not indent:
        return _styled(body, width, style, "", "")
    # A label/value report (/status) keeps its columns when a long value has to
    # wrap, so the hanging indent belongs to each line rather than the block.
    out: List[Line] = []
    for logical in body.split("\n"):
        out.extend(_styled(logical, width, style, "", indent))
    return out


BUILDERS: Dict[str, Callable[[Entry, int, RenderOptions], List[Line]]] = {
    "user": build_user_lines,
    "assistant": build_assistant_lines,
    "reasoning": build_reasoning_lines,
    "tool": build_tool_lines,
    "error": build_error_lines,
    "denied": build_denied_lines,
    "info": build_info_lines,
}


def build_entry_lines(entry: Entry, width: int, opts: RenderOptions) -> List[Line]:
    builder = BUILDERS.get(entry.kind, build_info_lines)
    lines = builder(entry, width, opts)
    return lines + [Line("", "assistant")] if lines else []


class Transcript:
    """Ordered entries plus a width-keyed cache of their rendered lines."""

    def __init__(self, limit: int = MAX_ENTRIES):
        self.entries: List[Entry] = []
        self.limit = limit
        self._cache: Optional[List[Line]] = None
        self._cache_key = None

    def add(self, entry: Entry) -> Entry:
        self.entries.append(entry)
        if len(self.entries) > self.limit:
            del self.entries[:len(self.entries) - self.limit]
        self.invalidate()
        return entry

    def clear(self):
        self.entries = []
        self.invalidate()

    def invalidate(self):
        self._cache = None

    def lines(self, width: int, opts: RenderOptions) -> List[Line]:
        key = (width, opts.key())
        if self._cache is not None and self._cache_key == key:
            return self._cache
        out: List[Line] = []
        for entry in self.entries:
            out.extend(entry.lines(width, opts))
        self._cache = out
        self._cache_key = key
        return out


# --------------------------------------------------------------------------
# pure layer: input area + footer
# --------------------------------------------------------------------------


class InputLayout:
    __slots__ = ("rows", "cursor_row", "cursor_col")

    def __init__(self, rows: List[str], cursor_row: int, cursor_col: int):
        self.rows = rows
        self.cursor_row = cursor_row
        self.cursor_col = cursor_col

    def __repr__(self):  # pragma: no cover - debugging aid
        return "InputLayout(%r, %d, %d)" % (self.rows, self.cursor_row, self.cursor_col)


def layout_input(buffer: str, cursor: int, width: int, prompt: str = "> ",
                 cont: str = "  ", max_rows: int = MAX_INPUT_ROWS) -> InputLayout:
    """Wrap the input buffer and locate the cursor within the wrapped rows.

    One column is reserved on the right so a cursor sitting at the end of a
    full row still has somewhere to be drawn.
    """
    width = max(len(prompt) + 2, width)
    avail = max(1, width - len(prompt) - 1)
    cursor = max(0, min(cursor, len(buffer)))

    rows: List[str] = []
    spans: List[tuple] = []  # (prefix_len, start_index, length)
    index = 0
    for line_no, logical in enumerate(buffer.split("\n")):
        offset = 0
        while True:
            segment = logical[offset:offset + avail]
            prefix = prompt if (line_no == 0 and offset == 0) else cont
            rows.append(prefix + segment)
            spans.append((len(prefix), index + offset, len(segment)))
            offset += avail
            if offset >= len(logical):
                break
        index += len(logical) + 1  # + the newline itself

    cursor_row, cursor_col = 0, len(prompt)
    for row_no, (prefix_len, start, length) in enumerate(spans):
        if start <= cursor <= start + length:
            cursor_row = row_no
            cursor_col = prefix_len + (cursor - start)
            break
    else:  # pragma: no cover - cursor is always clamped into range
        cursor_row = len(rows) - 1
        cursor_col = len(rows[-1])

    if len(rows) > max_rows:
        start_row = min(max(0, cursor_row - max_rows + 1), len(rows) - max_rows)
        rows = rows[start_row:start_row + max_rows]
        cursor_row -= start_row
    return InputLayout(rows, cursor_row, min(cursor_col, width - 1))


def build_status(provider: str, cwd_name: str, tokens_in: int, tokens_out: int,
                 width: int, glyphs: Optional[Glyphs] = None, busy: bool = False,
                 frame: int = 0, elapsed: float = 0.0, hint: str = "",
                 state: str = "ready", agent: str = "", context: str = "",
                 leader: str = "", yolo: bool = False) -> str:
    """Assemble the footer, dropping segments right-to-left as space runs out.

    `state` is the idle word ("ready", "interrupted"): a run that was aborted
    has to say so, or the screen looks identical to one that simply finished.
    `leader` is the half-typed leader chord ("ctrl+x"); it is the one segment
    that is never dropped, because a user who cannot see that the TUI is
    waiting for the second key has no way to understand what it is doing.
    """
    g = glyphs or Glyphs(True)
    dot = "  %s  " % g.bullet
    segments = [provider]
    if yolo:
        # First, so it is the last thing squeezed out: a mode with no gates
        # must never be invisible.
        segments.insert(0, "YOLO")
    if cwd_name:
        segments.append(cwd_name)
    if agent:
        segments.append(agent)
    left = " %s " % dot.join(segments)
    tokens = "%s in %s out" % (format_tokens(tokens_in), format_tokens(tokens_out))
    if hint:
        right = hint
    elif busy:
        right = "%s working %s  esc to interrupt" % (g.frame(frame),
                                                     format_duration(elapsed))
    else:
        right = state or "ready"
    if context:
        tokens = "%s  %s" % (context, tokens)
    right = "%s  %s " % (tokens, right)
    if leader:
        right = "%s  %s " % (leader, right.rstrip())

    # Widest first, then progressively less: provider+cwd+agent, provider+agent,
    # provider alone, nothing. The right-hand side is what the user is waiting
    # on, so it keeps its space.
    if len(left) + len(right) > width and agent:
        left = " %s%s%s " % (provider, dot, agent)
        if yolo:
            left = " YOLO%s%s " % (dot, provider)
    if len(left) + len(right) > width:
        left = " YOLO " if yolo else " %s " % provider
    if len(left) + len(right) > width:
        # Everything else may go; a mode with no gates may not become
        # invisible, so it outlives even the provider name.
        left = "YOLO" if yolo else ""
    gap = max(1, width - len(left) - len(right))
    if yolo and "YOLO" not in left:
        return ("YOLO " + right)[:width]
    return (left + " " * gap + right)[:width]


# --------------------------------------------------------------------------
# pure layer: the context meter
# --------------------------------------------------------------------------

# ContextState.pressure -> style name. The three tones exist because opencode
# turns its prompt's context percentage from muted to warning to error as the
# window fills (component/prompt/index.tsx), which is the only warning a user
# gets before compaction becomes unavoidable.
CONTEXT_STYLES = {"ok": "ctx_ok", "warn": "ctx_warn", "critical": "ctx_critical"}

CONTEXT_BAR_WIDTH = 12
# "12%" is the narrowest meter that still says something.
CONTEXT_MIN_WIDTH = 3


def context_text(state: Optional[ContextState], width: int) -> str:
    """The widest context label that fits: full, then percent, then nothing."""
    if state is None or width < CONTEXT_MIN_WIDTH:
        return ""
    full = usage.format_context(state)
    if len(full) <= width:
        return full
    short = ("%d%%" % int(state.percent + 0.5)) if state.window > 0 \
        else usage.format_tokens(state.used)
    return short if len(short) <= width else ""


def context_runs(state: Optional[ContextState], width: int,
                 glyphs: Optional[Glyphs] = None) -> List[Tuple[str, str]]:
    """The meter opencode draws beside its prompt, as styled runs.

    Runs rather than a string because the bar and the numbers share one
    pressure colour, and the caller has to be able to paint them in it.
    """
    text = context_text(state, width)
    if not text:
        return []
    style = CONTEXT_STYLES.get(state.pressure, "ctx_ok")
    room = width - len(text) - 1
    if room >= usage.BAR_MIN_WIDTH:
        bar = usage.context_bar(state, min(CONTEXT_BAR_WIDTH, room))
        if bar:
            return [(bar, style), (" " + text, style)]
    return [(text, style)]


# --------------------------------------------------------------------------
# pure layer: dialogs
# --------------------------------------------------------------------------

# opencode builds its command palette, model picker, provider picker, session
# list and agent picker out of ONE component (ui/dialog-select.tsx) with
# different options; Dialog below is that component, and every dialog in this
# file is an instance of it. The state lives here, curses-free, so the whole
# interaction model is testable without a terminal.

DIALOG_MAX_WIDTH = 92
DIALOG_MIN_WIDTH = 28
# Widest the title column grows before descriptions start being squeezed.
DIALOG_TITLE_COLUMN = 30
# Title, filter, blank, one row of list, footer.
DIALOG_MIN_ROWS = 5
DIALOG_MARGIN = 4

# Keybind names dialog-select answers to itself. Everything else a dialog
# reacts to is declared as a DialogAction and resolved through the same keymap.
DIALOG_COMMANDS: Tuple[str, ...] = (
    "dialog.select.cancel", "dialog.select.submit",
    "dialog.select.prev", "dialog.select.next",
    "dialog.select.page_up", "dialog.select.page_down",
    "dialog.select.home", "dialog.select.end",
)

# Results of Dialog.handle(). Anything else is the id of a DialogAction.
DIALOG_IGNORED = ""
DIALOG_CONSUMED = "consumed"
DIALOG_QUERY = "query"
DIALOG_SUBMIT = "submit"
DIALOG_CANCEL = "cancel"


class DialogAction:
    """A footer action: a keybind name, its label, and what it should do.

    `handler` takes the selected PaletteItem. It is stored here rather than in
    a table on the TUI so that the dialog and the footer hint can never drift
    apart -- the row a user sees is the callable that will run.
    """

    __slots__ = ("command", "title", "handler")

    def __init__(self, command: str, title: str,
                 handler: Optional[Callable[[Any], Any]] = None):
        self.command = command
        self.title = title
        self.handler = handler


class Dialog:
    """A filterable, scrollable modal list.

    `mode` picks how much furniture is drawn:
      "list"  filter line and a cursor -- the palette, models, sessions;
      "menu"  a cursor but no filter -- confirmations, short menus;
      "pane"  neither -- /status and help, which are read, not chosen from.

    `filtered` is separate from the mode because the session dialog types into
    the filter line but does NOT filter locally: its query goes to
    SessionStore.search(), which matches message bodies the row never shows, so
    a local title filter on top of it would throw the results away again.
    """

    __slots__ = ("name", "title", "select", "mode", "placeholder", "empty",
                 "actions", "filtered", "current", "message", "offset",
                 "payload", "_text")

    def __init__(self, name: str, title: str,
                 items: Sequence[PaletteItem] = (), mode: str = "list",
                 actions: Sequence[DialogAction] = (), placeholder: str = "Search",
                 empty: str = "", filtered: bool = True, current: str = "",
                 payload: Any = None):
        self.name = name
        self.title = title
        self.mode = mode
        self.placeholder = placeholder
        self.empty = empty
        self.actions = list(actions)
        self.filtered = filtered
        self.current = current
        self.message = ""
        self.offset = 0
        self.payload = payload
        self._text = ""
        self.select = SelectList(items, page_size=10)

    # -- state ------------------------------------------------------------

    @property
    def text(self) -> str:
        """What is typed on the filter line (not necessarily the local filter)."""
        return self._text

    @property
    def selected(self) -> Optional[PaletteItem]:
        return self.select.selected

    def set_items(self, items: Sequence[PaletteItem], keep_cursor: bool = False):
        self.select.items = items
        if not keep_cursor:
            self.select.reset_cursor()
        self.offset = 0

    def set_text(self, value: str):
        self._text = value
        if self.filtered:
            self.select.query = value      # setter refilters and resets the cursor
        else:
            self.select.reset_cursor()
        self.offset = 0

    def action(self, command: str) -> Optional[DialogAction]:
        for item in self.actions:
            if item.command == command:
                return item
        return None

    # -- keys -------------------------------------------------------------

    def commands(self) -> List[str]:
        return list(DIALOG_COMMANDS) + [item.command for item in self.actions]

    def handle(self, event: Any, keymap: Any) -> str:
        """Apply one key press; returns what the caller has to do about it.

        Deliberately uses commands_for() rather than lookup(): a dialog must
        not arm the leader, or ctrl+x inside the model picker would swallow the
        next key and look like a hang.
        """
        found = keymap.commands_for(event, among=self.commands())
        command = found[0] if found else None
        if command == "dialog.select.cancel":
            return DIALOG_CANCEL
        if command == "dialog.select.submit":
            return DIALOG_SUBMIT if self.selected is not None else DIALOG_CONSUMED
        if command == "dialog.select.prev":
            self.select.move(-1)
            return DIALOG_CONSUMED
        if command == "dialog.select.next":
            self.select.move(1)
            return DIALOG_CONSUMED
        if command == "dialog.select.page_up":
            self.select.page(-1)
            return DIALOG_CONSUMED
        if command == "dialog.select.page_down":
            self.select.page(1)
            return DIALOG_CONSUMED
        if command == "dialog.select.home":
            self.select.home()
            return DIALOG_CONSUMED
        if command == "dialog.select.end":
            self.select.end()
            return DIALOG_CONSUMED
        if command is not None:
            return command
        return self._edit(event)

    def _edit(self, event: Any) -> str:
        """Typing into the filter line. Only "list" mode has one."""
        key = getattr(event, "key", "")
        ctrl = bool(getattr(event, "ctrl", False))
        alt = bool(getattr(event, "alt", False))
        if self.mode != "list":
            return DIALOG_IGNORED
        if key == "backspace" and not (ctrl or alt):
            if not self._text:
                return DIALOG_IGNORED
            self.set_text(self._text[:-1])
            return DIALOG_QUERY
        if ctrl and key == "u":
            if not self._text:
                return DIALOG_IGNORED
            self.set_text("")
            return DIALOG_QUERY
        if ctrl and key == "w":
            trimmed = self._text.rstrip()
            cut = trimmed.rfind(" ")
            self.set_text(trimmed[:cut + 1] if cut >= 0 else "")
            return DIALOG_QUERY
        if ctrl or alt:
            return DIALOG_IGNORED
        if key == "space":
            self.set_text(self._text + " ")
            return DIALOG_QUERY
        if len(key) == 1 and key.isprintable():
            # normalise_key() folds "A" to ("a", shift); put the capital back.
            self.set_text(self._text + (key.upper()
                                        if getattr(event, "shift", False) else key))
            return DIALOG_QUERY
        return DIALOG_IGNORED


class FormField:
    """One row of a FormDialog: text, a yes/no toggle, a masked secret, or a
    three-way toggle whose "auto" leaves the decision to the caller."""

    __slots__ = ("name", "label", "value", "kind", "hint")

    def __init__(self, name: str, label: str, value: str = "",
                 kind: str = "text", hint: str = ""):
        self.name = name
        self.label = label
        self.value = value
        self.kind = kind
        self.hint = hint


class FormDialog:
    """The small prompt behind "Add provider" and "Rename session".

    opencode reaches these through dialog-provider's add flow and
    dialog-session-rename; both are a handful of fields and a submit, so one
    tiny form covers them.
    """

    __slots__ = ("name", "title", "fields", "index", "message", "payload")

    def __init__(self, name: str, title: str, fields: Sequence[FormField],
                 payload: Any = None):
        self.name = name
        self.title = title
        self.fields = list(fields)
        self.index = 0
        self.message = ""
        self.payload = payload

    @property
    def field(self) -> Optional[FormField]:
        if not self.fields:
            return None
        return self.fields[max(0, min(self.index, len(self.fields) - 1))]

    def values(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for field in self.fields:
            if field.kind == "bool":
                out[field.name] = field.value == "yes"
            elif field.kind == "tribool":
                # "auto" is None so the caller applies its own default.
                out[field.name] = (None if field.value == "auto"
                                   else field.value == "yes")
            else:
                out[field.name] = field.value
        return out

    def move(self, delta: int):
        if not self.fields:
            return
        self.index = (self.index + delta) % len(self.fields)

    def handle(self, event: Any, keymap: Any) -> str:
        key = getattr(event, "key", "")
        ctrl = bool(getattr(event, "ctrl", False))
        alt = bool(getattr(event, "alt", False))
        field = self.field
        if key == "escape":
            return DIALOG_CANCEL
        if key == "return":
            return DIALOG_SUBMIT
        if key in ("down", "tab") and not ctrl:
            self.move(1)
            return DIALOG_CONSUMED
        if key == "up" or (key == "tab" and getattr(event, "shift", False)):
            self.move(-1)
            return DIALOG_CONSUMED
        if field is None:
            return DIALOG_IGNORED
        if field.kind in ("bool", "tribool"):
            if key in ("space", "left", "right"):
                cycle = (("auto", "yes", "no") if field.kind == "tribool"
                         else ("yes", "no"))
                step = -1 if key == "left" else 1
                try:
                    index = cycle.index(field.value)
                except ValueError:
                    index = 0
                field.value = cycle[(index + step) % len(cycle)]
                return DIALOG_CONSUMED
            return DIALOG_IGNORED
        if key == "backspace" and not (ctrl or alt):
            field.value = field.value[:-1]
            return DIALOG_CONSUMED
        if ctrl and key == "u":
            field.value = ""
            return DIALOG_CONSUMED
        if ctrl or alt:
            return DIALOG_IGNORED
        if key == "space":
            field.value += " "
            return DIALOG_CONSUMED
        if len(key) == 1 and key.isprintable():
            field.value += key.upper() if getattr(event, "shift", False) else key
            return DIALOG_CONSUMED
        return DIALOG_IGNORED


class DeviceDialog:
    """The sign-in flow for a provider that has no API key to type.

    ChatGPT and SuperGrok authenticate with an RFC 8628 device code: the user
    opens a URL on any device, types a short code, and this end polls until
    the provider says yes. A FormDialog cannot express that — it asked for an
    "API key" that does not exist, which is why those two providers could not
    be signed into from the TUI at all.

    The dialog owns no threads. The screen thread reads these fields; the
    worker fills them in and the queue wakes the redraw, which is the same
    split every other background load here uses.
    """

    __slots__ = ("name", "title", "provider", "url", "code", "message",
                 "state", "payload")

    def __init__(self, provider: str, payload: Any = None):
        self.name = "device"
        self.title = "Sign in to %s" % provider
        self.provider = provider
        self.url = ""
        self.code = ""
        self.message = "requesting a device code"
        self.state = "starting"     # starting | waiting | done | failed
        self.payload = payload or {}

    def handle(self, event: Any, keymap: Any) -> str:
        key = getattr(event, "key", "")
        if key == "escape":
            return DIALOG_CANCEL
        if key == "return" and self.state in ("done", "failed"):
            return DIALOG_CANCEL
        # Every other key is swallowed: there is nothing to type here, and
        # letting keys through would put them in the prompt behind the modal.
        return DIALOG_CONSUMED


# --------------------------------------------------------------------------
# pure layer: drawing a dialog
# --------------------------------------------------------------------------

# A drawable row: styled segments, left to right, like the wordmark's.
Row = List[Tuple[str, str]]


class DialogView:
    """Rows to draw plus where the text cursor belongs (or None)."""

    __slots__ = ("rows", "cursor")

    def __init__(self, rows: List[Row], cursor: Optional[Tuple[int, int]] = None):
        self.rows = rows
        self.cursor = cursor


def highlight_runs(text: str, positions: Sequence[int], base: str,
                   match: str) -> Row:
    """Split `text` so the fuzzy-matched characters carry their own style."""
    marks = {index for index in positions if 0 <= index < len(text)}
    if not marks:
        return [(text, base)] if text else []
    runs: Row = []
    for index, char in enumerate(text):
        style = match if index in marks else base
        if runs and runs[-1][1] == style:
            runs[-1] = (runs[-1][0] + char, style)
        else:
            runs.append((char, style))
    return runs


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else status.truncate(text, width)


def dialog_item_runs(item: PaletteItem, positions: Sequence[int], width: int,
                     glyphs: Glyphs, selected: bool = False,
                     current: bool = False, marker: bool = True,
                     title_width: int = 0) -> Row:
    """One list row: marker, highlighted title, detail column, right-hand tag.

    `title_width` pads every title to the same column so the descriptions line
    up down the list; without it "New session Start a fresh..." reads as one
    sentence rather than two columns.
    """
    if width < 4:
        return []
    if selected:
        base, match, muted = "dialog_sel", "dialog_sel_match", "dialog_sel"
    elif item.disabled:
        base, match, muted = "hint", "dialog_match", "hint"
    else:
        base, match, muted = "dialog_item", "dialog_match", "hint"

    runs: Row = []
    room = width
    if marker:
        glyph = glyphs.arrow if selected else (glyphs.dot if current else " ")
        runs.append((glyph + " ", base))
        room -= 2

    footer = sanitize(str(item.footer or ""), glyphs.unicode_ok, False)
    if footer and len(footer) + 6 > room:
        footer = ""
    body = room - (len(footer) + 2 if footer else 0)

    detail = sanitize(str(item.detail or item.description or ""),
                      glyphs.unicode_ok, False)
    title = sanitize(str(item.title or ""), glyphs.unicode_ok, False)
    # The detail column only gets room the title does not need, but a very long
    # title never squeezes it out entirely -- "not available here" on a greyed
    # command is the whole reason that row is legible.
    if not detail:
        title_room = body
    elif title_width > 0:
        title_room = max(6, min(title_width, body - 8))
    else:
        title_room = max(6, body - 14)
    # The floor above is a preference, not a licence to overrun: on a narrow
    # row the title gives way, because the right-hand tag is what says whether
    # a model is free or a provider is the default.
    title = _fit(title, max(1, min(title_room, body)))
    runs.extend(highlight_runs(title, positions, base, match))
    used = (2 if marker else 0) + len(title)

    if detail:
        pad = " " * max(0, title_room - len(title)) if title_width > 0 else ""
        space = body - len(title) - len(pad) - 2
        if space >= 4:
            shown = _fit(detail, space)
            runs.append((pad + "  " + shown, muted))
            used += len(pad) + 2 + len(shown)
    if footer:
        gap = width - used - len(footer)
        if gap >= 1:
            runs.append((" " * gap + footer, muted))
            used = width
    if selected and used < width:
        # Selected rows are painted edge to edge, which is what makes the
        # cursor readable without colour.
        runs.append((" " * (width - used), base))
    return runs


def dialog_display_rows(dialog: Dialog) -> List[Tuple[str, Any]]:
    """The list as ("header", text) / ("item", (index, item, positions)).

    SelectList.matches is already laid out in section order, so cutting it at
    the category boundaries reproduces exactly the order the cursor indexes.
    """
    out: List[Tuple[str, Any]] = []
    last: Optional[str] = None
    for index, (item, found) in enumerate(dialog.select.matches):
        category = item.category or ""
        if category != last:
            if category:
                out.append(("header", category))
            last = category
        out.append(("item", (index, item, found)))
    return out


def cursor_row_index(rows: Sequence[Tuple[str, Any]], cursor: int) -> int:
    for index, (kind, payload) in enumerate(rows):
        if kind == "item" and payload[0] == cursor:
            return index
    return 0


def window_offset(rows: Sequence[Any], target: int, height: int,
                  offset: int) -> int:
    """Scroll `target` into a `height`-row window, moving as little as possible."""
    if height <= 0:
        return 0
    limit = max(0, len(rows) - height)
    offset = max(0, min(offset, limit))
    if target < offset:
        offset = target
    elif target >= offset + height:
        offset = target - height + 1
    # A row whose section header sits just above it is drawn with the header,
    # so the user can see which group the cursor is in -- but never on a
    # one-row window, where that would show the header INSTEAD of the item.
    if height > 1 and target > 0 and offset == target \
            and rows[target - 1][0] == "header":
        offset = target - 1
    return max(0, min(offset, limit))


def dialog_footer_runs(dialog: Dialog, width: int, glyphs: Glyphs,
                       keymap: Any = None) -> Row:
    """Action hints on the left, a position counter (or message) on the right."""
    runs: Row = []
    used = 0
    for action in dialog.actions:
        keys = ""
        if keymap is not None:
            try:
                keys = (keymap.describe(action.command) or "").split(",")[0].strip()
            except Exception:
                keys = ""
        if not keys:
            continue
        text = "%s %s" % (action.title, keys)
        if used + len(text) + 2 > width:
            break
        if runs:
            runs.append(("  ", "hint"))
            used += 2
        runs.append((action.title + " ", "dialog_action"))
        runs.append((keys, "hint"))
        used += len(text)

    right = dialog.message
    style = "warn" if right else "hint"
    if not right and dialog.select.count:
        right = "%d/%d" % (dialog.select.cursor + 1, dialog.select.count)
    if right and used + len(right) + 1 <= width:
        runs.append((" " * (width - used - len(right)), "hint"))
        runs.append((right, style))
    return runs


def dialog_view(dialog: Dialog, width: int, height: int, glyphs: Glyphs,
                keymap: Any = None) -> DialogView:
    """Everything inside the modal frame, as styled rows.

    Rows are exactly `width` wide at most and never more than `height` of them,
    so the caller can blit them into a box without re-measuring anything.
    """
    width = max(4, int(width))
    height = max(1, int(height))
    rows: List[Row] = []
    cursor: Optional[Tuple[int, int]] = None

    title = _fit(sanitize(dialog.title, glyphs.unicode_ok, False), width - 6)
    head: Row = [(title, "modal_title")]
    if width - len(title) - 3 > 0:
        head.append((" " * (width - len(title) - 3), "hint"))
        head.append(("esc", "hint"))
    rows.append(head)

    if dialog.mode == "list" and height >= 4:
        text = sanitize(dialog.text, glyphs.unicode_ok, False)
        if text:
            body = [(_fit(text, width - 2), "assistant")]
        else:
            body = [(_fit(dialog.placeholder, width - 2), "hint")]
        rows.append([("%s " % glyphs.arrow, "prompt")] + body)
        cursor = (len(rows) - 1, min(width - 1, 2 + len(text)))
        rows.append([])

    footer = dialog_footer_runs(dialog, width, glyphs, keymap)
    list_height = height - len(rows) - (1 if footer else 0)
    if list_height < 1:
        # A terminal too short for the furniture keeps the list and loses the
        # rest; an empty modal would be a bug the user cannot work around.
        rows = rows[:1]
        footer = []
        list_height = max(1, height - 1)

    # Paging keys have to move by what is actually on screen, and only the
    # renderer knows how tall that is.
    dialog.select.page_size = max(1, list_height)

    display = dialog_display_rows(dialog)
    if not display:
        message = dialog.empty or dialog.select.empty_message
        rows.append([(_fit(message, width), "hint")])
    else:
        target = cursor_row_index(display, dialog.select.cursor)
        dialog.offset = window_offset(display, target, list_height, dialog.offset)
        marker = dialog.mode != "pane"
        # One title column for the whole list, not per page: a column that
        # jumped every time the user paged would read as the rows moving.
        column = min(DIALOG_TITLE_COLUMN,
                     max((len(entry.title) for entry, _ in dialog.select.matches),
                         default=0))
        for kind, item in display[dialog.offset:dialog.offset + list_height]:
            if kind == "header":
                rows.append([("  " if marker else "", "header"),
                             (_fit(item, width - 2), "header")])
                continue
            index, entry, found = item
            rows.append(dialog_item_runs(
                entry, found, width, glyphs,
                selected=marker and index == dialog.select.cursor,
                current=bool(dialog.current) and entry.id == dialog.current,
                marker=marker, title_width=column))

    while len(rows) < height - (1 if footer else 0):
        rows.append([])
    if footer:
        rows.append(footer)
    return DialogView(rows[:height], cursor)


def form_view(form: FormDialog, width: int, height: int,
              glyphs: Glyphs) -> DialogView:
    """The same frame, for a FormDialog."""
    width = max(4, int(width))
    rows: List[Row] = []
    title = _fit(sanitize(form.title, glyphs.unicode_ok, False), width - 6)
    head: Row = [(title, "modal_title")]
    if width - len(title) - 3 > 0:
        head.append((" " * (width - len(title) - 3), "hint"))
        head.append(("esc", "hint"))
    rows.append(head)
    rows.append([])

    label_width = min(16, max(8, max((len(f.label) for f in form.fields),
                                     default=8) + 2))
    cursor = None
    for index, field in enumerate(form.fields):
        active = index == form.index
        label = ("%-*s" % (label_width, field.label + ":"))[:label_width]
        if field.kind == "tribool" and field.value == "auto":
            value = "[-] auto"
        elif field.kind in ("bool", "tribool"):
            value = "[x] yes" if field.value == "yes" else "[ ] no"
        elif field.kind == "secret":
            # An API key must never reach the screen: /login in a curses front
            # end is the replacement for getpass(), so it has to hide as well.
            value = "*" * len(field.value)
        else:
            value = field.value
        value = _fit(sanitize(value, glyphs.unicode_ok, False),
                     max(1, width - label_width - 2))
        rows.append([(label, "dialog_action" if active else "hint"),
                     (value, "assistant" if active else "hint")])
        if active and field.kind not in ("bool", "tribool"):
            cursor = (len(rows) - 1, min(width - 1, label_width + len(value)))
        if field.hint and active:
            rows.append([(" " * label_width, "hint"),
                         (_fit(field.hint, width - label_width), "hint")])

    rows.append([])
    hint = form.message or "enter to save   esc to cancel   up/down to move"
    rows.append([(_fit(hint, width), "warn" if form.message else "hint")])
    while len(rows) < height:
        rows.append([])
    return DialogView(rows[:max(1, height)], cursor)


def device_view(dialog: DeviceDialog, width: int, height: int,
                glyphs: Glyphs) -> DialogView:
    """The device-code dialog: the URL and the code, big enough to read."""
    width = max(4, int(width))
    rows: List[Row] = []
    title = _fit(sanitize(dialog.title, glyphs.unicode_ok, False), width - 6)
    head: Row = [(title, "modal_title")]
    if width - len(title) - 3 > 0:
        head.append((" " * (width - len(title) - 3), "hint"))
        head.append(("esc", "hint"))
    rows.append(head)
    rows.append([])

    if dialog.url:
        rows.append([(_fit("Open this address:", width), "hint")])
        rows.append([(_fit(sanitize(dialog.url, glyphs.unicode_ok, False),
                           width), "assistant")])
        rows.append([])
    if dialog.code:
        rows.append([(_fit("and enter this code:", width), "hint")])
        rows.append([(_fit(sanitize(dialog.code, glyphs.unicode_ok, False),
                           width), "dialog_action")])
        rows.append([])

    style = {"failed": "warn", "done": "dialog_action"}.get(dialog.state, "hint")
    rows.append([(_fit(sanitize(dialog.message, glyphs.unicode_ok, False),
                       width), style)])
    if dialog.state == "waiting":
        # sanitize() is what folds the dash down for a terminal that cannot
        # encode it; writing the literal straight into the row skipped it.
        rows.append([(_fit(sanitize(
            "esc cancels — nothing is stored until you approve",
            glyphs.unicode_ok, False), width), "hint")])
    elif dialog.state in ("done", "failed"):
        rows.append([(_fit("enter or esc to close", width), "hint")])
    while len(rows) < height:
        rows.append([])
    return DialogView(rows[:max(1, height)], None)


# --------------------------------------------------------------------------
# pure layer: dialog item construction
# --------------------------------------------------------------------------


def model_items(choices: Sequence[Any], favourites: Sequence[Any] = (),
                current: str = "") -> List[PaletteItem]:
    """ModelCatalog.choices() as rows (opencode's dialog-model.tsx).

    The category the catalogue put on the ref is the section header, so
    Favourites and Recent stay above the per-provider groups.
    """
    starred = {getattr(ref, "id", "") for ref in favourites}
    items: List[PaletteItem] = []
    for ref in choices:
        identifier = getattr(ref, "id", "")
        footer = "Free" if getattr(ref, "free", False) else ""
        if identifier in starred:
            footer = ("%s  *" % footer) if footer else "*"
        items.append(PaletteItem(
            id=identifier,
            title=getattr(ref, "label", "") or getattr(ref, "model", ""),
            description=getattr(ref, "provider", ""),
            category=getattr(ref, "category", "") or getattr(ref, "provider", ""),
            detail=("current" if identifier == current
                    else getattr(ref, "provider", "")),
            footer=footer,
            value=ref))
    return items


def provider_items(rows: Sequence[Dict[str, Any]],
                   add_entry: bool = True) -> List[PaletteItem]:
    """ModelCatalog.providers() as rows, plus opencode's "add" affordance."""
    items: List[PaletteItem] = []
    for row in rows:
        name = str(row.get("name", ""))
        detail = str(row.get("auth", ""))
        if row.get("model"):
            detail = "%s  %s" % (row.get("model"), detail)
        items.append(PaletteItem(
            id=name,
            title=name,
            description=str(row.get("base_url", "")),
            category=str(row.get("category", "")),
            detail=detail,
            footer="default" if row.get("is_default") else
                   ("" if row.get("auth_ok") else "no key"),
            value=name))
    if add_entry:
        items.append(PaletteItem(
            id="__add__", title="Add provider",
            description="Register a new endpoint",
            category="Config", detail="name, base URL, model",
            value="__add__"))
    return items


def session_items(rows: Sequence[Dict[str, Any]],
                  current: str = "") -> List[PaletteItem]:
    """SessionStore.list_sessions()/search() as rows."""
    items: List[PaletteItem] = []
    for row in rows:
        identifier = str(row.get("id", ""))
        title = str(row.get("title") or "").strip() or "(untitled)"
        detail = str(row.get("snippet") or "").strip()
        if not detail:
            count = row.get("message_count", 0)
            detail = "%s message%s" % (count, "" if count == 1 else "s")
        items.append(PaletteItem(
            id=identifier,
            title=title,
            description=str(row.get("model") or row.get("provider") or ""),
            category="Sessions",
            detail=detail,
            footer=("current" if identifier == current
                    else format_age(row.get("updated", 0))),
            value=identifier))
    return items


def agent_items(defs: Sequence[Any], current: str = "",
                readonly: Sequence[str] = ()) -> List[PaletteItem]:
    """AgentRegistry entries as rows; read-only agents say so (plan mode)."""
    locked = set(readonly)
    items: List[PaletteItem] = []
    for entry in defs:
        name = getattr(entry, "name", str(entry))
        description = getattr(entry, "description", "") or ""
        if not description:
            description = "built-in" if getattr(entry, "builtin", False) else ""
        items.append(PaletteItem(
            id=name,
            title=name,
            description=description,
            category="Agents",
            detail=description,
            footer=("read-only" if name in locked else
                    ("current" if name == current else "")),
            value=name))
    return items


def text_items(lines: Sequence[str], category: str = "") -> List[PaletteItem]:
    """Plain lines as a scrollable pane (the status and help dialogs)."""
    return [PaletteItem(id="line-%d" % index, title=line, category=category,
                        disabled=True, value=line)
            for index, line in enumerate(lines)]


def format_age(when: Any) -> str:
    """"3m", "2h", "5d" — the relative time opencode's session list shows."""
    try:
        seconds = time.time() - float(when or 0)
    except (TypeError, ValueError):
        return ""
    if seconds < 0 or when in (None, 0, 0.0):
        return ""
    if seconds < 90:
        return "now"
    if seconds < 3600:
        return "%dm" % int(seconds // 60)
    if seconds < 86400:
        return "%dh" % int(seconds // 3600)
    if seconds < 86400 * 30:
        return "%dd" % int(seconds // 86400)
    return "%dmo" % int(seconds // (86400 * 30))


# --------------------------------------------------------------------------
# pure layer: screen bands
# --------------------------------------------------------------------------


class Frame:
    """Which rows each band of the screen owns.

    Computed in exactly one place, because a status bar and a prompt that
    disagree about a single row is precisely how they end up drawn on top of
    each other.
    """

    __slots__ = ("rows", "cols", "box_top", "box_left", "box_width",
                 "box_height", "input_rows", "content_width", "hint_row",
                 "footer_row", "body_height", "todo_rows", "todo_top")

    def __repr__(self):  # pragma: no cover - debugging aid
        return ("Frame(rows=%d, cols=%d, body=%d, box_top=%d, box_height=%d, "
                "hint=%d, footer=%d)" % (self.rows, self.cols, self.body_height,
                                         self.box_top, self.box_height,
                                         self.hint_row, self.footer_row))


def box_width(cols: int, session: bool = False) -> int:
    """Prompt width for opencode's two distinct home and session layouts.

    The home route caps and centres its composer; the session route uses the
    available width inside two-column outer padding. Neither touches the last
    cell, where curses refuses to draw reliably.
    """
    if session:
        return max(4, cols - 4)
    width = min(BOX_MAX_WIDTH, max(BOX_MIN_WIDTH, cols - BOX_MARGIN))
    return max(4, min(width, cols - 1))


def layout_frame(rows: int, cols: int, wanted_input_rows: int = 1,
                 session: bool = False, wanted_todo_rows: int = 0) -> Frame:
    """Split the screen bottom-up: footer, hint, prompt box, todos, then the rest.

    The footer owns the last row and the box grows upwards from it, so the
    body can be squeezed to nothing but the bands never overlap. The todo band
    sits directly above the prompt so the current plan stays in view instead
    of scrolling away with the rest of the transcript; it yields to the body
    first, because a pinned list that leaves no room to read is worse than no
    list at all.
    """
    rows = max(MIN_ROWS, int(rows))
    cols = max(MIN_COLS, int(cols))
    frame = Frame()
    frame.rows = rows
    frame.cols = cols
    frame.box_width = box_width(cols, session=session)
    frame.box_left = max(0, (cols - frame.box_width) // 2)
    frame.content_width = max(4, frame.box_width - 4)
    # Rows the box may not take: the footer, the hint, its own two borders and
    # one row of body.
    frame.input_rows = max(1, min(int(wanted_input_rows), MAX_INPUT_ROWS, rows - 5))
    frame.box_height = frame.input_rows + 2
    frame.footer_row = rows - 1
    frame.hint_row = frame.footer_row - 1
    frame.box_top = frame.hint_row - frame.box_height
    available = max(0, frame.box_top - MIN_BODY_ROWS)
    frame.todo_rows = max(0, min(int(wanted_todo_rows), MAX_PINNED_TODO_ROWS,
                                 available))
    frame.todo_top = frame.box_top - frame.todo_rows
    frame.body_height = max(1, frame.todo_top)
    return frame


# --------------------------------------------------------------------------
# curses layer
# --------------------------------------------------------------------------


# style name -> (foreground colour index, extra attribute)
# Haiku Terminal reports 8 colours, no grey (colour 8 is absent), cannot
# redefine colours, and does not render SGR 2 — a screenshot from the owner's
# machine shows dim-styled text at exactly the brightness of normal text. The
# hierarchy therefore cannot rest on dim, which is what made a session read as
# one undifferentiated wall: tool output, reasoning and hints were all styled
# "quieter" and all came out the same. Bold and the eight colours do work, so
# the reply is made louder rather than the noise quieter, and indentation
# carries the rest for terminals with neither.
_STYLE_SPECS = {
    "assistant": (-1, "bold"),     # the one thing to read: the loudest row
    "user": (6, "bold"),           # cyan
    "reasoning": (-1, "dim"),
    "tool": (6, 0),
    "result": (-1, "dim"),
    "hint": (-1, "dim"),
    "info": (3, 0),                # yellow
    "header": (6, "bold"),
    "error": (1, "bold"),          # red
    "denied": (1, 0),
    "diff_add": (2, 0),            # green
    "diff_del": (1, 0),            # red
    "diff_hunk": (6, "dim"),
    "diff_meta": (-1, "dim"),
    "diff_ctx": (-1, "dim"),
    "prompt": (6, "bold"),
    "modal_border": (3, 0),
    "modal_title": (3, "bold"),
    "box": (-1, "dim"),
    "warn": (3, "bold"),           # yellow: the "no key" line has to be seen
    "dialog_item": (-1, 0),
    "dialog_match": (3, "bold"),   # the fuzzy-matched characters
    "dialog_sel": (-1, "reverse"),
    "dialog_sel_match": (-1, "reverse"),
    "dialog_action": (6, "bold"),
    "ctx_ok": (-1, "dim"),
    "ctx_warn": (3, 0),
    "ctx_critical": (1, "bold"),
}

# Attributes used when the terminal has no colour at all.
_MONO_SPECS = {
    "user": "bold",
    "reasoning": "dim",
    "tool": "bold",
    "result": "dim",
    "hint": "dim",
    "header": "bold",
    "error": "bold",
    "denied": "bold",
    "diff_add": "bold",
    "diff_del": "reverse",
    "diff_hunk": "dim",
    "diff_meta": "dim",
    "diff_ctx": "dim",
    "prompt": "bold",
    "modal_border": "bold",
    "modal_title": "bold",
    "box": "dim",
    "warn": "reverse",
    "dialog_match": "bold",
    "dialog_sel": "reverse",
    "dialog_sel_match": "reverse",
    "dialog_action": "bold",
    "ctx_ok": "dim",
    "ctx_warn": "bold",
    "ctx_critical": "reverse",
}

# The home screen's summary styles (status.summary_lines) mapped onto ours.
SUMMARY_STYLES = {"info": "header", "muted": "hint", "warn": "warn"}


class TUIUnavailable(RuntimeError):
    """Raised when curses cannot drive this terminal — callers fall back."""


class _ReportedConfig:
    """Effective settings from one object, credentials from another.

    `runtime.SessionConfig` and `agents.AgentPermissions` both carry the
    permission rules that are actually in force, but only the user's own Config
    can answer where an API key came from. Rather than teach every overlay the
    whole config protocol, the reporting path pairs the two. Read-only: nothing
    here ever saves.
    """

    __slots__ = ("data", "_credentials", "path")

    def __init__(self, data: Dict[str, Any], credentials: Any):
        self.data = data
        self._credentials = credentials
        self.path = getattr(credentials, "path", None)

    def get_provider(self, name: Optional[str] = None) -> Dict[str, Any]:
        providers = self.data.get("providers") or {}
        selected = name or self.data.get("default_provider", "")
        return providers.get(selected) or next(iter(providers.values()), {})

    def get_api_key(self, name: str) -> str:
        return self._credentials.get_api_key(name)

    def key_source(self, name: str) -> str:
        return self._credentials.key_source(name)


class TUI:
    """The full-screen front end.

    Constructor is intentionally decoupled from config plumbing so main.py can
    wire it up: `on_command(line)` handles slash commands and returns text to
    display or None if the line was not a command, `completer(prefix)` returns
    candidate completions for Tab.

    Agent contract, which is load-bearing for `haikode --continue`:

      * `agent` is the agent to START with. When main.py has just resumed a
        session into it, the TUI adopts it as-is. Asking the factory at
        startup instead is what used to erase the resumed conversation before
        the first frame was drawn.
      * `agent_factory()` is only ever called for /new (an empty agent) and
        after a command that reprovisioned (whatever the command layer built).

    `turn` is the TurnController both front ends share, so a conversation
    started here is the same one /undo, /sessions and --continue see.
    """

    def __init__(self, agent_factory: Callable[[], Any], config: Any, cwd: str = ".",
                 on_command: Optional[Callable[[str], Optional[str]]] = None,
                 completer: Optional[Callable[[str], List[str]]] = None,
                 header: str = "", agent: Any = None,
                 turn: Optional[TurnController] = None):
        self.agent_factory = agent_factory
        self.config = config
        self.cwd = os.path.abspath(os.path.expanduser(cwd or "."))
        self.on_command = on_command
        self.completer = completer
        self.header = header
        # Standalone (tests, an embedder) still persists: it just owns the
        # controller instead of sharing main.py's.
        self.turn = turn if turn is not None else TurnController(cwd=self.cwd)

        self.glyphs = Glyphs.detect()
        self.opts = RenderOptions(glyphs=self.glyphs)
        self.transcript = Transcript()

        self.agent = agent
        self.stdscr = None
        self._styles: Dict[str, int] = {}

        # keys, dialogs and accounting
        self.keymap = keybind.Keymap.from_config(config)
        self.dialog: Optional[Any] = None
        self.usage = UsageTracker()
        self._seen_tokens = {"input": 0, "output": 0}
        self._catalog_cache = None
        self._context: Optional[ContextState] = None
        self._context_at = 0.0
        self._palette: Optional[palette.CommandPalette] = None
        self.agent_name = ""
        # Serial for asynchronous dialog loads: a result that arrives after the
        # dialog was closed (or replaced) must be dropped, not drawn.
        self._dialog_serial = 0

        # input state
        self.buffer = ""
        self.cursor = 0
        self.history: List[str] = []
        self.history_index = 0
        self.history_draft = ""

        # view state
        self.scroll = 0
        self.follow = True
        self.frame = 0
        self.status_hint = ""
        self.interrupted = False
        # opencode picks a suggestion at random and re-rolls it after each
        # submit, so the home screen is not the same screen every time.
        self._placeholder = random.randrange(len(PLACEHOLDERS))
        self._setup_cache: Optional[status.SetupInfo] = None
        self._setup_at = 0.0

        # run state. Every queued item carries the token of the run that
        # produced it, so events from a run that /new (or a fresh submit)
        # superseded are dropped instead of bleeding into the new transcript.
        self._queue: "queue.Queue" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._run_token: Optional[object] = None
        self._local = threading.local()
        self.running = False
        self._run_started = 0.0
        self._quit = False
        self._exit_armed = 0.0
        self._dirty = True
        self._stream_entry: Optional[Entry] = None
        self._reasoning_entry: Optional[Entry] = None
        self._open_tool: Optional[Entry] = None
        self._pending: List[tuple] = []   # unanswered permission requests
        # Prompts typed while a run was in flight. opencode queues them; this
        # used to drop them on the floor (the buffer was cleared before the
        # "still working" check ever ran).
        self.queued: List[str] = []
        # Commands running off the UI thread. The serial is the cancel token:
        # a result whose serial no longer matches is dropped.
        self._command_serial = 0
        self._busy_label = ""
        # Configuration warnings already on screen. Every rebuilt agent carries
        # the same list, so without this a /model would repeat all of them.
        self._reported: set = set()
        self._reported_persistence = ""

    # --- lifecycle ------------------------------------------------------

    def run(self, stdscr=None) -> None:
        """Main loop. Call with no argument to manage curses setup here."""
        if stdscr is None:
            return _wrap_curses(self.run)
        self._attach(stdscr)
        try:
            self._loop()
        finally:
            self._shutdown()

    def _attach(self, stdscr):
        if curses is None:  # pragma: no cover - platform guard
            raise TUIUnavailable("curses is not available in this Python build")
        self.stdscr = stdscr
        rows, cols = stdscr.getmaxyx()
        if cols < MIN_COLS or rows < MIN_ROWS:
            raise TUIUnavailable(
                "terminal is %dx%d; haikode's TUI needs at least %dx%d"
                % (cols, rows, MIN_COLS, MIN_ROWS))

        self._init_styles()
        try:
            curses.raw()          # deliver ^C/^S/^Q as keys instead of signals
        except curses.error:
            try:
                curses.cbreak()
            except curses.error:
                pass
        try:
            curses.noecho()
        except curses.error:
            pass
        stdscr.keypad(True)
        stdscr.timeout(TICK_MS)
        # ncurses waits ESCDELAY (1s by default) after a bare Esc to see if a
        # function-key sequence follows. That would make "esc to interrupt"
        # feel broken, so shorten it to just longer than a terminal's burst.
        try:
            curses.set_escdelay(25)
        except (AttributeError, curses.error):
            pass
        # Stop the input driver folding Enter (13) and ctrl+j (10) into one
        # code: with them distinct, Enter submits and ctrl+j inserts the
        # newline its default binding promises.
        try:
            curses.nonl()
        except curses.error:
            pass
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        self._enable_mouse()
        # Re-detect now that initscr has fixed the screen's codeset.
        self.glyphs = Glyphs.detect()
        self.opts.glyphs = self.glyphs

        self._startup_agent()
        self._wire_permissions()
        self._announce_warnings()
        self._announce_persistence()
        # No banner entry: an empty transcript is what selects the home screen,
        # and the home screen says everything the banner used to (and more).

    def _startup_agent(self):
        """The agent this screen starts with.

        Adopt, never rebuild: `haikode --continue` has already resumed a
        session into the agent main.py handed us, and the factory means "give
        me an empty one" — calling it here erased the resumption before the
        first frame was drawn.
        """
        if self.agent is None:
            self.agent = self.agent_factory()
        # `--session` and `--continue` restore the conversation into the agent
        # before this screen exists, and the transcript is built from entries
        # rather than from messages — so a resumed session came up looking
        # empty while every following turn behaved as though it were not. The
        # picker's own /resume already replays; this is the same for the two
        # paths that arrive with history already in place.
        restored = getattr(self.agent, "messages", None) or []
        if restored and not self.transcript.entries:
            self._replay(restored)
            self.follow = True
        return self.agent

    def _shutdown(self):
        """Never leave a worker blocked on us, and never leave a broken tty."""
        self._quit = True
        if self.agent is not None:
            try:
                self.agent.abort()
            except Exception:
                pass
        self._release_pending()
        # Anything still queued may include a permission waiter.
        while True:
            try:
                kind, payload, _token = self._queue.get_nowait()
            except queue.Empty:
                break
            if kind == "permission":
                self._answer(payload, "reject")
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=2.0)
        self.turn.close()

    def _enable_mouse(self):
        """Subscribe to wheel presses only.

        REPORT_MOUSE_POSITION (0x8000000) must NOT be in the mask: it puts the
        terminal into any-motion tracking, which floods the queue on every
        mouse move and takes the terminal's own text selection away from the
        user. Old ncurses builds simply have no wheel-down code; on those the
        wheel scrolls up only, which is better than reacting to motion.
        """
        mask = 0
        for name in ("BUTTON4_PRESSED", "BUTTON5_PRESSED"):
            value = getattr(curses, name, 0)
            if value > 0:
                mask |= value
        if not mask:
            return
        try:
            curses.mousemask(mask)
        except (curses.error, AttributeError):
            pass

    def _init_styles(self):
        has_colors = False
        try:
            has_colors = curses.has_colors()
        except curses.error:
            has_colors = False

        if has_colors:
            try:
                curses.start_color()
            except curses.error:
                has_colors = False
        background = -1
        if has_colors:
            try:
                curses.use_default_colors()
            except curses.error:
                background = 0

        attributes = {"bold": curses.A_BOLD, "dim": curses.A_DIM,
                      "reverse": curses.A_REVERSE, 0: curses.A_NORMAL}
        self._styles = {}
        pair = 0
        for name, (colour, extra) in _STYLE_SPECS.items():
            attr = attributes.get(extra, curses.A_NORMAL)
            if has_colors and colour >= 0 and pair < min(60, curses.COLOR_PAIRS - 1):
                pair += 1
                try:
                    curses.init_pair(pair, colour, background)
                    attr |= curses.color_pair(pair)
                except curses.error:
                    pass
            elif not has_colors:
                attr = attributes.get(_MONO_SPECS.get(name, 0), curses.A_NORMAL)
            self._styles[name] = attr
        self._styles["assistant"] = curses.A_NORMAL
        self._styles["status"] = curses.A_REVERSE
        self._init_logo_styles(has_colors, background, pair)

    def _init_logo_styles(self, has_colors: bool, background: int, pair: int):
        """Colour pairs for the wordmark.

        The generic table above can only set a foreground, but the wordmark's
        "_" and "^" marks are drawn ON the shadow colour — that inset shadow is
        the whole trick of opencode's logo — so those pairs are built here.
        Without colour, or without the 256-colour cube the shadow tones live
        in, the marks simply lose their shading and the glyphs stay readable.
        """
        colours = 0
        try:
            colours = curses.COLORS if has_colors else 0
        except (AttributeError, curses.error):
            colours = 0
        # 235/238 are opencode's two shadow tones; on an 8-colour terminal the
        # nearest thing to "slightly lighter than the background" is black.
        shadows = {"logo_dim": 235 if colours >= 256 else curses.COLOR_BLACK,
                   "logo_bright": 238 if colours >= 256 else curses.COLOR_BLACK,
                   # The leaf's shadow is its stem, so tint it green rather than
                   # grey: 22 is a dark leaf green, falling back to plain green.
                   "logo_leaf": 22 if colours >= 256 else curses.COLOR_GREEN}
        bases = {"logo_dim": curses.A_DIM, "logo_bright": curses.A_BOLD,
                 "logo_leaf": curses.A_BOLD}
        limit = 0
        if has_colors:
            try:
                limit = min(60, curses.COLOR_PAIRS - 1)
            except (AttributeError, curses.error):
                limit = 0

        for half, base in bases.items():
            self._styles[half] = base
            self._styles[half + "_fill"] = base
            self._styles[half + "_shadow"] = base
            if pair >= limit:
                continue
            shadow = shadows[half]
            foreground = -1 if background < 0 else curses.COLOR_WHITE
            if half == "logo_leaf":
                # The blade itself is green, not terminal-default.
                leaf = 34 if colours >= 256 else curses.COLOR_GREEN
                pair += 1
                try:
                    curses.init_pair(pair, leaf, background)
                    self._styles[half] = base | curses.color_pair(pair)
                except curses.error:
                    pass
                foreground = leaf
            for suffix, spec in (("_fill", (foreground, shadow)),
                                 ("_shadow", (shadow, background))):
                if pair >= limit:
                    break
                pair += 1
                try:
                    curses.init_pair(pair, spec[0], spec[1])
                except curses.error:
                    continue
                self._styles[half + suffix] = base | curses.color_pair(pair)

    def _attr(self, style: str) -> int:
        return self._styles.get(style, curses.A_NORMAL)

    # --- permissions -----------------------------------------------------

    def _wire_permissions(self):
        """Point the agent's Permissions at our modal asker."""
        permissions = getattr(self.agent, "permissions", None)
        if permissions is not None:
            permissions.asker = self.ask_permission

    # --- configuration warnings -------------------------------------------

    def _warnings(self) -> List[str]:
        """Everything the configuration layers wanted the user to know."""
        out = [str(w) for w in (getattr(self.agent, "warnings", None) or ())]
        out += ["keybind: %s" % w
                for w in (getattr(self.keymap, "warnings", None) or ())]
        return out

    def _announce_warnings(self):
        """Say in the footer that there are warnings, without spending the home
        screen on them.

        An empty transcript is what selects the home screen, so writing the
        warnings straight into it at startup would replace the home screen with
        a wall of config errors on any project that has one.
        """
        count = len([text for text in self._warnings()
                     if text not in self._reported])
        if count:
            self.status_hint = ("%d config warning%s - /status"
                                % (count, "" if count == 1 else "s"))
            self._dirty = True

    def _report_warnings(self):
        """Put those warnings in the transcript, once each.

        runtime.build_agent deliberately never prints: a broken haikode.json, a
        permission the project widened, an instruction path that pointed
        outside the checkout, an agent name that does not exist all arrive as
        agent.warnings and it is the front-end's job to show them. Nothing in
        the TUI was showing them at all, so an untrusted repository could
        widen `bash` to allow with nothing whatsoever on screen -- which is the
        one thing escalation reporting exists to prevent.
        """
        for text in self._warnings():
            if text in self._reported:
                continue
            self._reported.add(text)
            loud = text.startswith(("config error:", "permission escalation",
                                    "config tools:", "config instructions:"))
            self.transcript.add(Entry("error" if loud else "info", text=text))
            self._dirty = True

    # --- persistence ------------------------------------------------------

    def _announce_persistence(self):
        """Put "nothing is being saved" on screen, once per distinct reason.

        A silent failure here is the difference between a session the user can
        undo and one that only looked like it existed, so it goes in the
        transcript as an error and stays in the footer for as long as it holds.
        """
        notice = self.turn.persistence_notice()
        if notice and notice != self._reported_persistence:
            self.transcript.add(Entry("error", text=notice))
            self._dirty = True
        self._reported_persistence = notice

    def _undo_available(self) -> bool:
        return bool(self.turn.undo_available)

    def ask_permission(self, request) -> str:
        """Called on the WORKER thread. Blocks until the main thread answers."""
        # A tool that asks from some other thread has no token of its own;
        # treat it as belonging to the run currently on screen.
        token = getattr(self._local, "token", None) or self._run_token
        if self._quit or token is not self._run_token:
            return "reject"
        box: Dict[str, str] = {"answer": "reject"}
        event = threading.Event()
        self._queue.put(("permission", (request, box, event), token))
        # Poll rather than wait forever: if the UI dies mid-request the worker
        # must still be able to unwind instead of pinning the process.
        while not event.wait(0.2):
            if self._quit:
                return "reject"
        return box.get("answer", "reject")

    def _answer(self, pending, answer: str):
        _request, box, event = pending
        box["answer"] = answer
        event.set()

    def _release_pending(self):
        while self._pending:
            self._answer(self._pending.pop(), "reject")

    # --- worker ----------------------------------------------------------

    def _submit(self, text: str):
        if self.running or not text.strip():
            return
        # Ahead of the first user turn: the transcript is about to stop being
        # empty anyway, so the home screen is not the price, and a warning
        # about the configuration belongs above the turn it applies to.
        self._report_warnings()
        self.transcript.add(Entry("user", text=text))
        # A queued prompt was already recorded when it was typed; recording it
        # again would put it in the history twice.
        if not self.history or self.history[-1] != text:
            self.history.append(text)
        self.history_index = len(self.history)
        self.interrupted = False
        self._placeholder += 1
        self._stream_entry = None
        self._reasoning_entry = None
        self._open_tool = None
        self.running = True
        self._run_started = time.time()
        self.follow = True
        self._context = None
        self._dirty = True
        token = object()
        self._run_token = token
        self._worker = threading.Thread(target=self._run_agent,
                                        args=(text, self.agent, token),
                                        daemon=True)
        self._worker.start()

    def _run_agent(self, text: str, agent, token):
        """Worker thread body. Touches the queue and the agent, never curses.

        The whole turn — mentions, session, checkpoint, persistence — goes
        through the shared controller. Calling agent.run() straight from here
        is what made the default front end write nothing at all.
        """
        self._local.token = token
        put = self._queue.put
        try:
            result = self.turn.run_turn(
                agent, text,
                on_text=lambda chunk: put(("text", chunk, token)),
                on_event=lambda kind, payload: put(
                    ("event", (kind, payload), token)),
                on_attach=lambda paths: put(("event", ("attached", paths), token)))
            put(("turn", result, token))
        except BaseException as exc:  # provider/network failures must surface
            put(("fatal", "%s: %s" % (type(exc).__name__, exc), token))
        finally:
            put(("done", None, token))

    # --- event pump ------------------------------------------------------

    def _pump(self):
        pending_text: List[str] = []
        handled = 0
        while handled < MAX_EVENTS_PER_TICK:
            try:
                kind, payload, token = self._queue.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if kind in ("dialog", "async"):
                # Dialog loads and background commands carry their own serial
                # and outlive runs, so they are dispatched before the run-token
                # filter below.
                if pending_text:
                    self._on_text("".join(pending_text))
                    pending_text = []
                if kind == "dialog":
                    self._on_dialog_data(payload)
                else:
                    self._on_async_done(payload)
                continue
            if token is not self._run_token:
                # Superseded run. Drop its output, but never leave a worker
                # parked on an unanswered permission request.
                if kind == "permission":
                    self._answer(payload, "reject")
                continue
            if kind == "text":
                pending_text.append(payload)
                continue
            if pending_text:
                self._on_text("".join(pending_text))
                pending_text = []
            if kind == "event":
                self._on_event(payload[0], payload[1])
            elif kind == "fatal":
                self.transcript.add(Entry("error", text=payload))
                self._dirty = True
            elif kind == "turn":
                self._on_turn(payload)
            elif kind == "done":
                self.running = False
                self._stream_entry = None
                self._reasoning_entry = None
                self._open_tool = None
                self.status_hint = ""
                self._record_usage()
                self._context = None      # the turn just changed what fits
                self._dirty = True
                if self._send_queued():
                    break   # the next run owns the queue from here
            elif kind == "permission":
                self._pending.append(payload)
                break  # draw, then run the modal from the main loop
        if pending_text:
            self._on_text("".join(pending_text))

    def _on_turn(self, result):
        """A turn finished. Report anything the run itself could not show."""
        if getattr(result, "captured", ""):
            self.transcript.add(Entry("info", text=str(result.captured)))
        if getattr(result, "error", ""):
            self.transcript.add(Entry("error", text=str(result.error)))
        self._announce_persistence()
        self._dirty = True

    def _send_queued(self) -> bool:
        """Start the prompt the user typed while the last run was working."""
        while self.queued:
            text = self.queued.pop(0)
            if text.strip():
                self._submit(text)
                return True
        return False

    def _on_text(self, chunk: str):
        if not chunk:
            return
        self._reasoning_entry = None
        if self._stream_entry is None:
            self._stream_entry = self.transcript.add(Entry("assistant"))
        self._stream_entry.append_text(chunk)
        self.transcript.invalidate()
        self._dirty = True

    def _on_event(self, kind: str, payload):
        if kind == "attached":
            # @-mentions the controller expanded into the prompt.
            self.transcript.add(Entry("info", text="attached: %s"
                                      % ", ".join(str(p) for p in payload)))
            self._dirty = True
        elif kind == "reasoning":
            if self._reasoning_entry is None:
                self._reasoning_entry = self.transcript.add(Entry("reasoning"))
            self._reasoning_entry.append_text(str(payload))
        elif kind == "tool":
            name = payload.get("name", "tool")
            args = payload.get("args") or {}
            entry = Entry("tool", name=name, detail=summarize_tool(name, args))
            # The checklist is drawn from the call, not the result, so a plan
            # is on screen while the tool runs and survives a failed one.
            if name == "todowrite" and isinstance(args.get("todos"), list):
                entry.meta = {"todos": args["todos"]}
            self._open_tool = self.transcript.add(entry)
            self._stream_entry = None
            self._reasoning_entry = None
        elif kind == "tool_result":
            entry = self._open_tool or self.transcript.add(
                Entry("tool", name=payload.get("name", "tool")))
            metadata = payload.get("metadata") or {}
            entry.diff = metadata.get("diff", "") or ""
            # Merged, not replaced: the call already put what it knows in here.
            merged = dict(entry.meta)
            merged.update(metadata)
            entry.meta = merged
            entry.output = payload.get("output", "") or ""
            if payload.get("title") and not entry.detail:
                entry.detail = str(payload["title"])
            entry.bump()
            self._open_tool = None
        elif kind == "tool_denied":
            entry = self._open_tool or self.transcript.add(
                Entry("tool", name=payload.get("name", "tool")))
            entry.denied = payload.get("reason", "denied")
            entry.bump()
            self._open_tool = None
        elif kind == "tool_error":
            entry = self._open_tool or self.transcript.add(
                Entry("tool", name=payload.get("name", "tool")))
            entry.error = payload.get("error", "error")
            entry.bump()
            self._open_tool = None
        elif kind == "limit":
            self.transcript.add(Entry(
                "info",
                text=("step budget reached after %s steps; send 'continue' "
                      "for a fresh budget. /config shows the active value; "
                      "external edits need /reload"
                      % payload.get("steps", "?"))))
        self.transcript.invalidate()
        self._dirty = True

    # --- main loop -------------------------------------------------------

    def _loop(self):
        while not self._quit:
            try:
                self._pump()
                if self._pending:
                    # Pop first, then answer in a finally: a crash inside the
                    # modal must still unblock the worker waiting on us.
                    pending = self._pending.pop(0)
                    answer = "reject"
                    try:
                        answer = self._modal_permission(pending[0])
                    finally:
                        self._answer(pending, answer)
                    continue
                if self._dirty or self.running:
                    self._draw()
                    self._dirty = False
                key = self._read_key()
                if key is not None:
                    self._handle_key(key)
                elif self.running:
                    self.frame += 1
            except KeyboardInterrupt:
                self._interrupt()

    def _read_key(self):
        """Return a str for printable input, an int for control/function keys."""
        try:
            key = self.stdscr.get_wch()
        except curses.error:
            return None
        except (UnicodeDecodeError, ValueError):
            return None
        except AttributeError:  # pragma: no cover - very old curses builds
            key = self.stdscr.getch()
            if key == -1:
                return None
            return chr(key) if 32 <= key < 127 else key
        if isinstance(key, str):
            if len(key) == 1:
                code = ord(key)
                if code < 32:
                    return code
                if code == 127:
                    return curses.KEY_BACKSPACE
            return key
        return key

    def _peek_key(self):
        """Non-blocking read, used to tell Alt+Enter from a bare Esc."""
        self.stdscr.timeout(0)
        try:
            return self._read_key()
        finally:
            self.stdscr.timeout(TICK_MS)

    # --- key handling ----------------------------------------------------

    def _handle_key(self, key):
        """Route one key: dialog first, then the keymap, then plain input.

        The three layers are tried in that order because they overlap on
        purpose -- ctrl+p is "previous item" inside a dialog and "command
        palette" outside one, exactly as in opencode, which disambiguates the
        same way by widget focus.
        """
        if key == curses.KEY_RESIZE:
            self._on_resize()
            return
        if key == getattr(curses, "KEY_MOUSE", -1):
            if self.dialog is None:
                self._on_mouse()
            return

        alt = False
        if key == 27:                       # Esc, or the lead byte of Alt+key
            follow = self._peek_key()
            if follow in ("[", "O"):
                # Haiku Terminal sends PageUp/PageDown as raw CSI even after
                # keypad(True). Decode the common sequences instead of dropping
                # precisely the navigation keys the user was trying to press.
                decoded = self._decode_escape(follow)
                if decoded is None:
                    return
                key = decoded
            if follow is not None:
                if follow not in ("[", "O"):
                    key, alt = follow, True

        event = self._key_event(key, alt)
        if self.dialog is not None:
            self._dialog_key(event)
            return
        if event is not None and self._keymap_key(event):
            return
        if event is not None and self._input_binding(event):
            return
        self._input_key(key, alt)

    def _key_event(self, key, alt: bool = False):
        """One getch value as a keybind.KeyEvent, or None if it is not a key."""
        try:
            event = keybind.from_curses(key, curses, newline_is_enter=False)
        except Exception:
            return None
        if not event.key:
            return None
        if alt:
            return keybind.KeyEvent(key=event.key, ctrl=event.ctrl, alt=True,
                                    shift=event.shift)
        return event

    def _keymap_key(self, event) -> bool:
        """Try the top-level bindings. True when the key was consumed.

        Also owns the leader: pressing ctrl+x arms it (and shows that in the
        footer), and the following key either completes a <leader> binding or
        is swallowed, never typed into the prompt.
        """
        if event.ctrl and not event.alt and event.key in ("c", "d"):
            return False    # interrupt and end-of-input keep their own handling
        if (event.key == "tab" and not event.ctrl and not event.alt
                and not event.shift):
            head = self.buffer[:self.cursor]
            start = len(head)
            while start > 0 and not head[start - 1].isspace():
                start -= 1
            if head[start:].startswith("/"):
                # The autocomplete widget has focus while a slash command is
                # being entered. Elsewhere Tab keeps opencode's agent-cycle
                # binding; without this focus rule the two advertised features
                # cannot coexist.
                return False
        if event.key in ("home", "end", "escape"):
            # These chords have a global meaning and an input meaning. The
            # prompt owns them while it has focus; ctrl+g still reaches
            # messages_first, and session_interrupt is resolved below.
            return False
        pending = self.keymap.leader_pending
        try:
            command = self.keymap.lookup(event, among=TOP_LEVEL_COMMANDS)
        except Exception:
            return False
        if self.keymap.leader_pending:
            self._dirty = True
            return True
        if command is None:
            if pending:
                self._dirty = True      # a leader sequence that meant nothing
                return True
            return False
        self._dirty = True
        self._run_binding(command)
        return True

    def _run_binding(self, command: str):
        if command in UNAVAILABLE_BINDINGS:
            self.status_hint = "%s is not available in haikode" % command
            self._dirty = True
            return
        name = (BINDING_ACTIONS.get(command)
                or OPTIONAL_BINDING_ACTIONS.get(command))
        handler = getattr(self, name, None) if name else None
        if handler is None:
            return
        try:
            handler()
        except Exception as exc:
            self.transcript.add(Entry("error", text="%s: %s"
                                      % (type(exc).__name__, exc)))
            self._dirty = True

    def _input_binding(self, event) -> bool:
        """Dispatch configurable editing keys after global bindings decline."""
        try:
            command = self.keymap.lookup(event, among=INPUT_COMMANDS)
        except Exception:
            return False
        if command is None:
            return False

        if command in ("session_interrupt", "input_clear",
                       "prompt.autocomplete.hide"):
            # Esc and ctrl+c both land here, but only ctrl+c may arm the
            # double-press exit: hammering Esc after an interrupt must be
            # harmless, and Esc is also what leaves scrollback (_on_escape's
            # follow-restore).
            if getattr(event, "key", "") == "escape" and not event.ctrl:
                self._on_escape()
            else:
                self._interrupt()
        elif command in ("input_submit",):
            self._on_enter()
        elif command == "input_paste":
            self.status_hint = "paste text with the terminal's paste command"
            self._dirty = True
        elif command == "input_newline":
            self._insert("\n")
        elif command == "input_move_left":
            self.cursor = max(0, self.cursor - 1)
            self._dirty = True
        elif command == "input_move_right":
            self.cursor = min(len(self.buffer), self.cursor + 1)
            self._dirty = True
        elif command in ("input_move_up", "history_previous",
                         "prompt.autocomplete.prev"):
            self._on_vertical(-1)
        elif command in ("input_move_down", "history_next",
                         "prompt.autocomplete.next"):
            self._on_vertical(1)
        elif command == "input_line_home":
            self.cursor = self.buffer.rfind("\n", 0, self.cursor) + 1
            self._dirty = True
        elif command == "input_line_end":
            end = self.buffer.find("\n", self.cursor)
            self.cursor = len(self.buffer) if end < 0 else end
            self._dirty = True
        elif command == "input_buffer_home":
            self.cursor = 0
            self._dirty = True
        elif command == "input_buffer_end":
            self.cursor = len(self.buffer)
            self._dirty = True
        elif command == "input_delete_to_line_end":
            end = self.buffer.find("\n", self.cursor)
            end = len(self.buffer) if end < 0 else end
            self.buffer = self.buffer[:self.cursor] + self.buffer[end:]
            self._dirty = True
        elif command == "input_delete_to_line_start":
            start = self.buffer.rfind("\n", 0, self.cursor) + 1
            self.buffer = self.buffer[:start] + self.buffer[self.cursor:]
            self.cursor = start
            self._dirty = True
        elif command == "input_backspace":
            self._backspace()
        elif command == "input_delete":
            if self.cursor < len(self.buffer):
                self.buffer = (self.buffer[:self.cursor]
                               + self.buffer[self.cursor + 1:])
                self._dirty = True
            elif not self.buffer and event.ctrl and \
                    getattr(event, "key", "") == "d":
                # EOF-on-empty is a ctrl+d convention; the Delete key shares
                # this binding and must never exit the app (Haiku Terminal
                # sends ESC[3~ and users press it with an empty prompt).
                self._quit = True
        elif command == "input_word_forward":
            self._move_word(1)
        elif command == "input_word_backward":
            self._move_word(-1)
        elif command == "input_delete_word_forward":
            self._delete_word_forward()
        elif command == "input_delete_word_backward":
            self._delete_word()
        elif command in ("prompt.autocomplete.select",
                         "prompt.autocomplete.complete"):
            self._complete()
        else:
            return False
        return True

    def _input_key(self, key, alt: bool = False):
        """The prompt's own editing keys — everything the layers above passed on.

        Several branches here are shadowed by a default binding (ctrl+a is
        model_provider_list, pageup is messages_page_up). They are kept because
        a config that sets those keybinds to "none" hands the chord straight
        back to the prompt, which is the whole point of unbinding one.
        """
        if alt and key in (10, 13, curses.KEY_ENTER):
            self._insert("\n")
            return
        if alt and isinstance(key, str):
            self._insert(key)
            return
        if isinstance(key, str):
            self._insert(key)
            return

        if key == 27:                       # a bare Esc got this far
            self._on_escape()
        elif key in (10, 13, curses.KEY_ENTER):
            self._on_enter()
        elif key == 3:                      # Ctrl-C
            self._interrupt()
        elif key == 4:                      # Ctrl-D
            if not self.buffer:
                self._quit = True
        elif key == 12:                     # Ctrl-L
            self._redraw()
        elif key == 21:                     # Ctrl-U
            self.buffer = ""
            self.cursor = 0
            self._dirty = True
        elif key == 11:                     # Ctrl-K
            self.buffer = self.buffer[:self.cursor]
            self._dirty = True
        elif key == 1:                      # Ctrl-A
            self.cursor = self.buffer.rfind("\n", 0, self.cursor) + 1
            self._dirty = True
        elif key == 5:                      # Ctrl-E
            end = self.buffer.find("\n", self.cursor)
            self.cursor = len(self.buffer) if end < 0 else end
            self._dirty = True
        elif key == 23:                     # Ctrl-W
            self._delete_word()
        elif key == 18:                     # Ctrl-R
            self.opts.show_reasoning = not self.opts.show_reasoning
            self._invalidate_view()
        elif key == 15:                     # Ctrl-O
            self.opts.expand = not self.opts.expand
            self._invalidate_view()
        elif key == 9:                      # Tab
            self._complete()
        elif key in (curses.KEY_BACKSPACE, 8):
            self._backspace()
        elif key == curses.KEY_DC:
            if self.cursor < len(self.buffer):
                self.buffer = self.buffer[:self.cursor] + self.buffer[self.cursor + 1:]
                self._dirty = True
        elif key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
            self._dirty = True
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.buffer), self.cursor + 1)
            self._dirty = True
        elif key == curses.KEY_HOME:
            self.cursor = 0
            self._dirty = True
        elif key == curses.KEY_END:
            self.cursor = len(self.buffer)
            self._dirty = True
        elif key == curses.KEY_UP:
            self._on_vertical(-1)
        elif key == curses.KEY_DOWN:
            self._on_vertical(1)
        elif key == curses.KEY_PPAGE:
            self._scroll(-self._transcript_height() + 1)
        elif key == curses.KEY_NPAGE:
            self._scroll(self._transcript_height() - 1)

    def _decode_escape(self, introducer: str):
        """Decode CSI/SS3 keys omitted by Haiku's terminfo, else consume them."""
        tail = ""
        limit = 1 if introducer == "O" else 16
        for _ in range(limit):
            nxt = self._peek_key()
            if not isinstance(nxt, str) or len(nxt) != 1:
                break
            tail += nxt
            if introducer == "O" or "@" <= nxt <= "~":
                break
        sequence = introducer + tail
        mapping = {
            "[A": curses.KEY_UP,
            "[B": curses.KEY_DOWN,
            "[C": curses.KEY_RIGHT,
            "[D": curses.KEY_LEFT,
            "[H": curses.KEY_HOME,
            "[F": curses.KEY_END,
            "[1~": curses.KEY_HOME,
            "[4~": curses.KEY_END,
            "[7~": curses.KEY_HOME,
            "[8~": curses.KEY_END,
            "[3~": curses.KEY_DC,
            "[5~": curses.KEY_PPAGE,
            "[6~": curses.KEY_NPAGE,
            "[Z": getattr(curses, "KEY_BTAB", -1),
            "OP": getattr(curses, "KEY_F1", -1),
            "OQ": getattr(curses, "KEY_F2", -1),
            "OR": getattr(curses, "KEY_F3", -1),
            "OS": getattr(curses, "KEY_F4", -1),
        }
        return mapping.get(sequence)

    def _insert(self, text: str):
        text = text.replace("\r", "\n")
        self.buffer = self.buffer[:self.cursor] + text + self.buffer[self.cursor:]
        self.cursor += len(text)
        self._exit_armed = 0.0
        self.status_hint = ""
        self._dirty = True

    def _backspace(self):
        if self.cursor > 0:
            self.buffer = self.buffer[:self.cursor - 1] + self.buffer[self.cursor:]
            self.cursor -= 1
            self._dirty = True

    def _delete_word(self):
        index = self.cursor
        while index > 0 and self.buffer[index - 1].isspace():
            index -= 1
        while index > 0 and not self.buffer[index - 1].isspace():
            index -= 1
        self.buffer = self.buffer[:index] + self.buffer[self.cursor:]
        self.cursor = index
        self._dirty = True

    def _move_word(self, direction: int):
        """Move over whitespace and one word without assuming shell syntax."""
        index = self.cursor
        if direction < 0:
            while index > 0 and self.buffer[index - 1].isspace():
                index -= 1
            while index > 0 and not self.buffer[index - 1].isspace():
                index -= 1
        else:
            while index < len(self.buffer) and self.buffer[index].isspace():
                index += 1
            while index < len(self.buffer) and not self.buffer[index].isspace():
                index += 1
        self.cursor = index
        self._dirty = True

    def _delete_word_forward(self):
        """Delete the same span input_word_forward would cross."""
        start = self.cursor
        self._move_word(1)
        end = self.cursor
        self.buffer = self.buffer[:start] + self.buffer[end:]
        self.cursor = start
        self._dirty = True

    def _on_enter(self):
        # A trailing backslash is the "continue on the next line" convention.
        if self.buffer[:self.cursor].endswith("\\"):
            self.buffer = self.buffer[:self.cursor - 1] + "\n" + self.buffer[self.cursor:]
            self._dirty = True
            return
        text = self.buffer.strip()
        self.buffer = ""
        self.cursor = 0
        self._exit_armed = 0.0
        self.status_hint = ""
        self._dirty = True
        if not text:
            return
        if text.startswith("/"):
            self.history.append(text)
            self.history_index = len(self.history)
            self._dispatch_command(text)
            return
        if self.running:
            self._enqueue(text)
            return
        self._submit(text)

    def _enqueue(self, text: str):
        """Hold a prompt typed mid-run instead of throwing it away.

        The buffer is cleared the moment enter is pressed, so "you are busy,
        try again" meant the typed text was simply gone. opencode queues; so
        do we, and the queue is visible so the user knows it will be sent.
        """
        self.history.append(text)
        self.history_index = len(self.history)
        first = text.strip().splitlines()[0] if text.strip() else text

        # Steer by default: the model sees this at its next step rather than
        # after the whole turn. Waiting for the turn was the old behaviour and
        # it is wrong once turns are unlimited — by the time a correction
        # arrives, it is about work already finished.
        steer = getattr(self.agent, "steer", None)
        if callable(steer) and steer(text):
            self.transcript.add(Entry("info", text="steering: %s" % first[:60]))
            self.status_hint = "steering — the model sees this at its next step"
            self._dirty = True
            return

        self.queued.append(text)
        self.transcript.add(Entry("info", text="queued: %s" % first[:60]))
        self.status_hint = ("%d queued" % len(self.queued)
                            if len(self.queued) > 1 else "queued")
        self._dirty = True

    def _on_escape(self):
        if self._busy_label:
            self._cancel_async()
        elif self.running:
            self._interrupt()
        elif self.buffer:
            self.buffer = ""
            self.cursor = 0
            self._dirty = True
        elif not self.follow:
            self.follow = True
            self._dirty = True

    def _interrupt(self):
        if self._busy_label:
            self._cancel_async()
            return
        if self.running:
            try:
                self.agent.abort()
            except Exception:
                pass
            self.interrupted = True
            self._drop_queued()
            self.transcript.add(Entry("info", text="[interrupted]"))
            self.status_hint = "interrupting…" if self.glyphs.unicode_ok else "interrupting..."
            self._dirty = True
            return
        if self.buffer:
            self.buffer = ""
            self.cursor = 0
            self._dirty = True
            return
        now = time.time()
        if self._exit_armed and now - self._exit_armed < 3.0:
            self._quit = True
            return
        self._exit_armed = now
        self.status_hint = "ctrl-c again to exit"
        self._dirty = True

    def _drop_queued(self):
        """Stopping means stopping: queued prompts must not fire afterwards."""
        if not self.queued:
            return
        count = len(self.queued)
        self.queued = []
        self.transcript.add(Entry("info", text="%d queued prompt%s discarded"
                                  % (count, "" if count == 1 else "s")))
        self._dirty = True

    def _on_vertical(self, direction: int):
        """Up/Down browse history for a single-line prompt, else move the cursor.

        While a turn is streaming, or once the view has left bottom-follow,
        arrows scroll the transcript instead. This is what makes the mouse
        wheel work on Haiku at all: in the alternate screen Haiku Terminal
        never reports the wheel as mouse events — it writes three arrow keys
        per tick (TermView.cpp, B_MOUSE_WHEEL_CHANGED; Shift+wheel sends
        PageUp/PageDown). Scrolling down to the bottom re-enters follow, and
        arrows mean history again.
        """
        if self.running or not self.follow:
            self._scroll(direction)
            return
        if "\n" in self.buffer:
            self._move_line(direction)
            return
        if not self.history:
            return
        if direction < 0:
            if self.history_index == len(self.history):
                self.history_draft = self.buffer
            self.history_index = max(0, self.history_index - 1)
        else:
            self.history_index = min(len(self.history), self.history_index + 1)
        if self.history_index >= len(self.history):
            self.buffer = self.history_draft
        else:
            self.buffer = self.history[self.history_index]
        self.cursor = len(self.buffer)
        self._dirty = True

    def _move_line(self, direction: int):
        start = self.buffer.rfind("\n", 0, self.cursor) + 1
        column = self.cursor - start
        if direction < 0:
            if start == 0:
                return
            previous = self.buffer.rfind("\n", 0, start - 1) + 1
            self.cursor = min(previous + column, start - 1)
        else:
            end = self.buffer.find("\n", self.cursor)
            if end < 0:
                return
            next_end = self.buffer.find("\n", end + 1)
            if next_end < 0:
                next_end = len(self.buffer)
            self.cursor = min(end + 1 + column, next_end)
        self._dirty = True

    def _complete(self):
        if self.completer is None:
            self._insert("    ")
            return
        # The token is the run of non-whitespace ending at the cursor. Using
        # split() here would pick the last word of the *previous* line when the
        # cursor sits just after a newline, and the replacement below would
        # then eat that word plus the newline.
        head = self.buffer[:self.cursor]
        start = len(head)
        while start > 0 and not head[start - 1].isspace():
            start -= 1
        token = head[start:]
        try:
            matches = list(self.completer(token) or [])
        except Exception:
            matches = []
        if not matches:
            return
        if len(matches) == 1:
            completion = matches[0]
        else:
            completion = os.path.commonprefix(matches)
            self.transcript.add(Entry("info", text="  ".join(matches[:24])))
            self._dirty = True
        if completion and completion != token:
            self.buffer = self.buffer[:start] + completion + self.buffer[self.cursor:]
            self.cursor = start + len(completion)
        self._dirty = True

    def _on_mouse(self):
        try:
            _, _, _, _, state = curses.getmouse()
        except curses.error:
            return
        motion = getattr(curses, "REPORT_MOUSE_POSITION", 0)
        if motion > 0 and state & motion:
            return  # a mouse move is not a wheel click
        up = getattr(curses, "BUTTON4_PRESSED", 0)
        down = getattr(curses, "BUTTON5_PRESSED", 0)
        if up > 0 and state & up:
            self._scroll(-3)
        elif down > 0 and state & down:
            self._scroll(3)

    def _scroll(self, delta: int):
        height = self._transcript_height()
        total = len(self._view_lines())
        maximum = max(0, total - height)
        if self.follow:
            self.scroll = maximum
        self.scroll = max(0, min(maximum, self.scroll + delta))
        self.follow = self.scroll >= maximum
        self._dirty = True

    def _invalidate_view(self):
        for entry in self.transcript.entries:
            entry.bump()
        self.transcript.invalidate()
        self._dirty = True

    def _on_resize(self):
        try:
            curses.update_lines_cols()
        except (AttributeError, curses.error):
            pass
        self._invalidate_view()
        self._redraw()

    def _redraw(self):
        try:
            self.stdscr.clearok(True)
        except curses.error:
            pass
        self._dirty = True

    # --- commands --------------------------------------------------------

    def _dispatch_command(self, line: str):
        name = line.split()[0].lower()
        if name == "/reload" and self.running:
            self.transcript.add(Entry(
                "info",
                text="configuration was not reloaded: wait for the active turn "
                     "to finish, or interrupt it first"))
            self._dirty = True
            return
        if name in ("/exit", "/quit", "/q"):
            self._quit = True
            return
        if name in ("/new", "/clear"):
            self._new_session()
            return
        if name == "/reasoning":
            self.opts.show_reasoning = not self.opts.show_reasoning
            self.transcript.add(Entry("info", text="reasoning %s"
                                      % ("shown" if self.opts.show_reasoning else "hidden")))
            self._invalidate_view()
            return
        if name == "/expand":
            self.opts.expand = not self.opts.expand
            self.transcript.add(Entry("info", text="tool output %s"
                                      % ("expanded" if self.opts.expand else "folded")))
            self._invalidate_view()
            return
        if name in ("/models", "/model") and len(line.split()) == 1:
            self._open_models()
            return
        if name in ("/providers",) or (name == "/provider" and len(line.split()) == 1):
            self._open_providers()
            return
        if name in ("/sessions", "/resume") and len(line.split()) == 1:
            self._open_sessions()
            return
        if name in ("/agents", "/agent") and len(line.split()) == 1:
            self._open_agents()
            return
        if name in ("/commands", "/palette"):
            self._open_commands()
            return
        if name == "/keybinds":
            self._open_help()
            return
        if name == "/redraw":
            self._redraw()
            return
        if name == "/status":
            self._open_status()
            return
        if name == "/help":
            self.transcript.add(Entry("info", text=self._help_text(), name="info"))
            self._dirty = True
            return

        # Everything the screen does not own itself is classified before it
        # runs: curses owns the terminal and this is its only thread, so a
        # command that talks to the network, shells out to the keystore or
        # wants the user to type a secret must not be called from here.
        mode = command_mode(line, custom=self._is_custom_command(line))
        if mode == TURN:
            self._run_command_turn(line)
            return
        if mode == MODAL:
            self._run_command_modal(line)
            return
        if mode == ASYNC:
            self._run_command_async(line)
            return
        self._finish_command(line, self._call_command(line))

    def _call_command(self, line: str):
        """Hand `line` to the command layer. Returns the text, or an error one."""
        if self.on_command is None:
            return None
        try:
            return self.on_command(line)
        except Exception as exc:
            return "%s: %s" % (type(exc).__name__, exc)

    def _finish_command(self, line: str, result):
        """Show a command's answer and re-read the agent if it reprovisioned."""
        name = line.split()[0].lower()
        if result is None:
            self.transcript.add(Entry("info", text="unknown command: %s" % name))
        else:
            # Command handlers colour for the plain REPL with SGR escapes;
            # the transcript renders text literally, so they must come off
            # here or the user sees "[2m...[0m".
            self.transcript.add(Entry(
                "info", text=_SGR_RE.sub("", str(result))))
        if result is not None and name in REPROVISION_COMMANDS:
            self._adopt_agent()
        self._dirty = True

    def _is_custom_command(self, line: str) -> bool:
        """True for a command file: it renders to a prompt, so it is a turn."""
        registry = self._command_registry()
        if registry is None:
            return False
        try:
            return hasattr(registry.get(command_name(line)), "render")
        except Exception:
            return False

    # --- commands that need a turn, a worker or a modal --------------------

    def _run_command_turn(self, line: str):
        """A command that expands into a prompt goes through the normal turn.

        Running it through the command layer instead would call the REPL's
        blocking send(): a full agent run on the curses thread, printing to a
        stdout the screen owns, with no permission modal.
        """
        name = command_name(line)
        prompt = ""
        if name == "init":
            notice, prompt = prepare_init(self.cwd)
            self.transcript.add(Entry("info", text=notice))
        else:
            registry = self._command_registry()
            arg = line.strip()[1:].partition(" ")[2].strip()
            try:
                prompt = registry.get(name).render(arg, self.cwd)
            except Exception as exc:
                self.transcript.add(Entry("error", text="%s: %s"
                                          % (type(exc).__name__, exc)))
                self._dirty = True
                return
        if not prompt.strip():
            self._dirty = True
            return
        if self.running:
            self._enqueue(prompt)
            return
        self._submit(prompt)

    def _run_command_async(self, line: str):
        """Run a slow command on a worker so the screen keeps drawing."""
        self._run_async(command_name(line),
                        lambda: self._call_command(line),
                        lambda result: self._finish_command(line, result))

    def _run_async(self, label: str, work, done=None, on_error=None):
        """Run `work()` off the curses thread; `done(value)` on the main one.

        The serial is the cancel token. A cancelled or superseded result is
        dropped in _on_async_done rather than drawn, which is as close to
        cancellation as a blocking keystore subprocess allows.

        `on_error` lets a caller that owns a dialog show the failure inside it
        instead of only as a transcript line — a sign-in that fails while its
        modal is up must say so in the modal the user is looking at.
        """
        self._command_serial += 1
        serial = self._command_serial
        self._busy_label = label
        self.status_hint = "%s%s  esc to cancel" % (label, self.glyphs.ellipsis)
        self._dirty = True
        put = self._queue.put

        def body():
            try:
                value, error = work(), ""
            except BaseException as exc:
                value, error = None, "%s: %s" % (type(exc).__name__, exc)
            put(("async", (serial, done, value, error, on_error), None))

        threading.Thread(target=body, daemon=True).start()

    def _cancel_async(self):
        self._command_serial += 1       # orphans whatever is in flight
        self._busy_label = ""
        self.status_hint = "cancelled"
        self._dirty = True

    def _on_async_done(self, payload):
        on_error = None
        try:
            serial, done, value, error, on_error = payload
        except (TypeError, ValueError):
            try:
                serial, done, value, error = payload
            except (TypeError, ValueError):
                return
        if serial != self._command_serial:
            return                      # cancelled, or a newer command won
        self._busy_label = ""
        self.status_hint = ""
        self._dirty = True
        if error:
            if on_error is not None:
                self._safely(on_error, error)
            else:
                self.transcript.add(Entry("error", text=error))
            return
        if done is not None:
            self._safely(done, value)

    def _run_command_modal(self, line: str):
        """A command that needs the user to type something opens a form.

        /login used to reach interactive_login(), which calls input() and
        getpass() while curses holds the tty in raw mode: the prompt is
        invisible, the keystrokes go to the screen, and the app looks hung.
        """
        if command_name(line) == "login":
            self._open_login(line.strip()[1:].partition(" ")[2].strip())
            return
        self._run_command_async(line)

    def _provider_spec(self, name: str) -> Dict[str, Any]:
        try:
            return self.config.data.get("providers", {}).get(name, {}) or {}
        except Exception:
            return {}

    def _open_login(self, provider: str = ""):
        name = provider or self._provider_name()
        if self._provider_spec(name).get("oauth_provider"):
            # No key exists to type: these sign in with a device code.
            self._open_device_login(name)
            return
        self._open_dialog(FormDialog(
            "login", "Sign in",
            [FormField("provider", "Provider", name),
             FormField("key", "API key", kind="secret",
                       hint="never echoed; stored in the Haiku keystore when "
                            "one is installed")],
            payload={"submit": self._save_login}))

    def _open_device_login(self, provider: str):
        """Start an RFC 8628 device flow and show its code while it polls.

        Two background steps, not one: the code has to appear the moment the
        provider issues it, because the user needs it to do their half. Polling
        only starts afterwards, and it can run for minutes — far too long to
        hold the screen thread, and the reason /login used to look hung.
        """
        dialog = DeviceDialog(provider)
        self._open_dialog(dialog)

        def begin():
            from .oauth import OAuthStore, begin_device_authorization
            store = OAuthStore.for_config(self.config)
            pending = begin_device_authorization(provider)
            store.save_pending(provider, pending)
            return pending

        def started(pending):
            if self.dialog is not dialog:
                return              # the user closed it while we were asking
            dialog.url = str(pending.get("verification_uri_complete")
                             or pending.get("verification_uri") or "")
            dialog.code = str(pending.get("user_code") or "")
            dialog.state = "waiting"
            dialog.message = "waiting for you to approve it"
            self._dirty = True
            self._poll_device_login(dialog, pending)

        def failed(error):
            if self.dialog is not dialog:
                return
            dialog.state = "failed"
            dialog.message = error
            self._dirty = True

        self._run_async("signing in", begin,
                        lambda value: (started(value) if value
                                       else failed("no device code was issued")),
                        on_error=failed)

    def _poll_device_login(self, dialog, pending):
        provider = dialog.provider

        def wait():
            from .oauth import OAuthStore, poll_device_authorization
            store = OAuthStore.for_config(self.config)
            tokens = poll_device_authorization(provider, pending)
            store.set(provider, tokens)
            store.clear_pending(provider)
            return True

        def done(_value):
            if self.dialog is not dialog:
                return
            dialog.state = "done"
            dialog.message = "signed in to %s" % provider
            self._dirty = True
            # Credentials changed, so the agent has to be rebuilt to pick the
            # new ones up — the same reprovision /provider does.
            self._adopt_agent()

        def failed(error):
            if self.dialog is not dialog:
                return
            dialog.state = "failed"
            dialog.message = error
            self._dirty = True

        self._run_async("waiting for approval", wait, done, on_error=failed)

    def _save_login(self, form):
        values = form.values()
        provider = str(values.get("provider", "")).strip()
        key = str(values.get("key", "")).strip()
        if not provider:
            form.message = "provider is required"
            self._dirty = True
            return
        if not key:
            form.message = "key is required"
            self._dirty = True
            return
        self._close_dialog()
        # Storing a key shells out to the keystore helper, and the reprovision
        # that follows re-resolves credentials, so both go on the worker.
        self._run_async("signing in", lambda: self._store_key(provider, key),
                        lambda result: self._finish_command(
                            "/provider %s" % self._provider_name(), result))

    def _store_key(self, provider: str, key: str) -> str:
        """Worker-thread half of /login: save the key, then reprovision."""
        where = self.config.set_api_key(provider, key)
        message = "key for %s saved (%s)" % (provider, where)
        refreshed = self._call_command("/provider %s" % self._provider_name())
        return message if refreshed is None else "%s\n%s" % (message, refreshed)

    def _adopt_agent(self):
        """Take the agent the command layer just rebuilt, and re-read setup.

        A failed rebuild must not leave the UI without an agent, so the old one
        is kept if the factory raises.
        """
        previous = self.agent
        try:
            agent = self.agent_factory()
        except Exception as exc:
            self.transcript.add(Entry("error", text="%s: %s" % (type(exc).__name__, exc)))
            return
        if agent is not None and agent is not self.agent:
            # Switching model or provider mid-conversation must not throw the
            # conversation away: the transcript on screen would then describe a
            # history the new agent has never seen.
            #
            # Repaired on the way across, because the worker thread may be
            # between "the model asked for a tool" and "the tool answered":
            # copying that snapshot verbatim leaves the new agent holding an
            # assistant turn whose call is unanswered, and every later request
            # 400s until the user starts a new session.
            history = getattr(previous, "messages", None)
            if history and not getattr(agent, "messages", None):
                try:
                    from .agent import pair_tool_messages
                    agent.messages = pair_tool_messages(list(history))
                except Exception:
                    pass
            self.agent = agent
            self._wire_permissions()
            self._report_warnings()
            self._seen_tokens = {"input": 0, "output": 0}
        self._setup_cache = None
        self._context = None

    # --- dialogs ---------------------------------------------------------

    def _open_dialog(self, dialog):
        """Show `dialog`, superseding whatever was open.

        Bumping the serial is what makes an in-flight background load for the
        previous dialog land in the bin instead of on top of this one.
        """
        self._dialog_serial += 1
        self.dialog = dialog
        self.keymap.reset()
        self._dirty = True

    def _close_dialog(self):
        self._dialog_serial += 1
        self.dialog = None
        self.keymap.reset()
        self._dirty = True

    def _safely(self, handler, *args):
        """Run a dialog handler; a failure becomes a transcript line, not a crash."""
        try:
            handler(*args)
        except Exception as exc:
            self.transcript.add(Entry("error", text="%s: %s"
                                      % (type(exc).__name__, exc)))
            self._dirty = True

    def _dialog_key(self, event):
        dialog = self.dialog
        if dialog is None or event is None:
            return
        self._dirty = True
        try:
            result = dialog.handle(event, self.keymap)
        except Exception as exc:
            self._close_dialog()
            self.transcript.add(Entry("error", text="%s: %s"
                                      % (type(exc).__name__, exc)))
            return
        if result == DIALOG_CANCEL:
            self._close_dialog()
            return
        if result == DIALOG_SUBMIT:
            self._dialog_submit(dialog)
            return
        if result == DIALOG_QUERY:
            handler = (getattr(dialog, "payload", None) or {}).get("query")
            if handler is not None:
                self._safely(handler, dialog.text)
            return
        if result in (DIALOG_CONSUMED, DIALOG_IGNORED):
            return
        action = dialog.action(result) if hasattr(dialog, "action") else None
        if action is not None and action.handler is not None:
            item = dialog.selected
            if item is not None:
                self._safely(action.handler, item)

    def _dialog_submit(self, dialog):
        handler = (getattr(dialog, "payload", None) or {}).get("submit")
        if handler is None:
            self._close_dialog()
            return
        if isinstance(dialog, DeviceDialog):
            return          # nothing to submit; the worker owns this dialog
        if isinstance(dialog, FormDialog):
            self._safely(handler, dialog)
            return
        item = dialog.selected
        if item is not None:
            self._safely(handler, item)

    def _on_dialog_data(self, payload):
        """A background load finished. Ignored unless its dialog is still up."""
        try:
            name, serial, data = payload
        except (TypeError, ValueError):
            return
        dialog = self.dialog
        if dialog is None or dialog.name != name or serial != self._dialog_serial:
            return
        if name == "models":
            choices, favourites = data
            dialog.payload["choices"] = choices
            dialog.set_items(model_items(choices, favourites,
                                         self._current_model_id()),
                             keep_cursor=True)
            self._dirty = True
        elif name == "providers":
            dialog.set_items(provider_items(data), keep_cursor=True)
            self._dirty = True

    # --- dialogs: the command palette ------------------------------------

    def _keys(self, command: str) -> str:
        """First chord bound to `command`, for a palette row's key hint."""
        try:
            return (self.keymap.describe(command) or "").split(",")[0].strip()
        except Exception:
            return ""

    def _command_registry(self):
        """The command layer's CommandRegistry, when the callback exposes one.

        Feature-detecting it is what lets ctrl+p list /login, /tools and every
        custom command without the TUI owning a second copy of that table, and
        what tells a custom command (a prompt, so a turn) from a built-in.

        Both shapes are accepted: a bound method, whose `__self__` owns the
        registry, and a callable object that exposes `commands` directly —
        main.py hands over the latter.
        """
        owner = getattr(self.on_command, "__self__", self.on_command)
        registry = getattr(owner, "commands", None)
        return registry if getattr(registry, "builtins", None) is not None else None

    def _slash_runner(self, name: str):
        def run():
            self._dispatch_command("/" + name)
        return run

    def _build_palette(self) -> "palette.CommandPalette":
        registry = palette.CommandPalette()
        add = registry.register
        add("session.new", "New session", "Start a fresh conversation",
            "Session", self._new_session, self._keys("session_new"))
        add("session.list", "List sessions", "Resume a saved session",
            "Session", self._open_sessions, self._keys("session_list"))
        add("session.compact", "Compact session",
            "Summarise the history to free up context", "Session",
            self._compact_session, self._keys("session_compact"))
        add("model.list", "List models", "Switch the active model", "Model",
            self._open_models, self._keys("model_list"))
        add("provider.list", "List providers", "Browse and switch provider",
            "Model", self._open_providers, self._keys("model_provider_list"))
        add("model.cycle_recent", "Cycle recent model",
            "Jump to the next recently used model", "Model",
            self._cycle_recent_next, self._keys("model_cycle_recent"))
        add("provider.add", "Add provider", "Register a new endpoint",
            "Config", self._open_add_provider)
        add("agent.list", "List agents", "Switch the active agent", "Agent",
            self._open_agents, self._keys("agent_list"))
        add("status.view", "Status", "Setup, tokens and context usage",
            "View", self._open_status, self._keys("status_view"))
        add("help.show", "Help", "Keybindings", "View", self._open_help,
            self._keys("help_show"))
        add("reasoning.toggle", "Toggle reasoning",
            "Show or hide model reasoning blocks", "View",
            self._toggle_reasoning, "ctrl+r")
        add("tool.expand", "Expand tool output",
            "Show folded tool output in full", "View", self._toggle_expand,
            "ctrl+o")
        add("app.redraw", "Redraw", "Repaint the screen", "View",
            self._redraw, "ctrl+l")
        add("app.exit", "Exit", "Quit haikode", "App", self._quit_app,
            self._keys("app_exit"))
        self._register_slash_commands(registry)
        return registry

    def _register_slash_commands(self, registry):
        source = self._command_registry()
        if source is None:
            return
        try:
            source.complete("")     # forces custom commands off disk
        except Exception:
            pass
        for name in sorted(getattr(source, "builtins", None) or {}):
            if name in SLASH_SHADOWED:
                continue
            builtin = source.builtins[name]
            # Undo is only real if the snapshots were written. Offering it
            # while persistence is broken promises a revert that cannot happen.
            enabled = self._undo_available if name == "undo" else None
            registry.register("cmd.%s" % name, "/" + name,
                              getattr(builtin, "help", ""), "Commands",
                              self._slash_runner(name), enabled=enabled)
        for name in sorted(getattr(source, "custom", None) or {}):
            command = source.custom[name]
            try:
                summary = command.summary()
            except Exception:
                summary = ""
            registry.register("custom.%s" % name, "/" + name, summary,
                              "Custom", self._slash_runner(name))

    def _open_commands(self):
        self._palette = self._build_palette()
        self._open_dialog(Dialog(
            "commands", "Commands", self._palette.items(),
            placeholder="Search commands",
            payload={"submit": self._run_palette_item}))

    def _run_palette_item(self, item):
        registry = self._palette
        self._close_dialog()
        if registry is None:
            return
        try:
            registry.run(item.value)
        except palette.CommandUnavailable:
            self.status_hint = "%s is not available here" % item.title
        except KeyError:
            self.status_hint = "unknown command"

    # --- dialogs: models and providers ------------------------------------

    def _catalog(self):
        if self._catalog_cache is None:
            if self.config is None:
                return None
            from . import models
            self._catalog_cache = models.ModelCatalog(self.config)
        return self._catalog_cache

    def _current_model_id(self) -> str:
        name = self._provider_name()
        model = str(getattr(self.agent, "model", "") or "")
        return "%s/%s" % (name, model) if name and model else ""

    def _offline_choices(self, catalog) -> List[Any]:
        """Favourites, recents and the active model — everything listable
        without a network round trip, so the dialog opens instantly."""
        import copy as _copy

        def tagged(ref, category):
            try:
                clone = _copy.copy(ref)
                clone.category = category
                return clone
            except Exception:
                return ref

        out: List[Any] = []
        seen = set()
        for refs, category in ((catalog.favourites(), "Favourites"),
                               (catalog.recent(), "Recent")):
            for ref in refs:
                if ref.id in seen:
                    continue
                seen.add(ref.id)
                out.append(tagged(ref, category))
        current = catalog.current()
        if current is not None and current.id not in seen:
            out.append(tagged(current, current.provider))
        return out

    def _open_models(self):
        catalog = self._catalog()
        if catalog is None:
            self.status_hint = "no configuration to list models from"
            return
        current = self._current_model_id()
        choices = self._offline_choices(catalog)
        actions = [
            DialogAction("model_favorite_toggle", "Favourite",
                         self._toggle_favourite),
            DialogAction("model_provider_list", "Providers",
                         lambda item: self._open_providers()),
        ]
        dialog = Dialog("models", "Select model",
                        model_items(choices, catalog.favourites(), current),
                        actions=actions, placeholder="Search models",
                        current=current,
                        empty="Loading models...",
                        payload={"submit": self._select_model,
                                 "choices": choices})
        self._open_dialog(dialog)
        self._load_models_async(catalog)

    def _load_models_async(self, catalog):
        """Fetch the full catalogue off the main thread.

        Listing a provider costs a network round trip, which on Haiku over a
        slow link is seconds; doing it inline would freeze the screen. The
        worker only puts data on the queue — it never touches curses.
        """
        serial = self._dialog_serial
        queue_put = self._queue.put

        def work():
            try:
                data = (catalog.choices(), catalog.favourites())
            except Exception:
                return
            queue_put(("dialog", ("models", serial, data), None))

        threading.Thread(target=work, daemon=True).start()

    def _toggle_favourite(self, item):
        catalog = self._catalog()
        dialog = self.dialog
        if catalog is None or dialog is None:
            return
        now = catalog.toggle_favourite(item.value)
        dialog.message = "favourited" if now else "unfavourited"
        choices = (dialog.payload or {}).get("choices") or []
        dialog.set_items(model_items(choices, catalog.favourites(),
                                     self._current_model_id()),
                         keep_cursor=True)

    def _select_model(self, item):
        catalog = self._catalog()
        if catalog is None:
            return
        selected = catalog.select(item.value)
        self._close_dialog()
        self._reprovision(selected.provider)
        self.transcript.add(Entry("info", text="model → %s" % selected.id))

    def _cycle_recent(self, step: int):
        catalog = self._catalog()
        if catalog is None:
            return
        ref = catalog.cycle_recent(self._current_model_id() or None, step)
        if ref is None:
            self.status_hint = "no recent models yet"
            self._dirty = True
            return
        catalog.select(ref)
        self._reprovision(ref.provider)
        self.status_hint = "model → %s" % ref.id
        self._dirty = True

    def _cycle_recent_next(self):
        self._cycle_recent(1)

    def _cycle_recent_previous(self):
        self._cycle_recent(-1)

    def _open_providers(self):
        """Open first, list second.

        catalog.providers() asks the config layer for every provider's auth
        state, and on Haiku each of those can shell out to the keystore helper
        for up to five seconds. Doing that inline froze the whole screen for
        as many seconds as the user had providers.
        """
        catalog = self._catalog()
        if catalog is None:
            self.status_hint = "no configuration to list providers from"
            return
        # Signing in belongs here: the list is where a user finds out a
        # provider has no credentials, and having to leave for /login was the
        # gap that made ChatGPT and SuperGrok feel unreachable.
        actions = [DialogAction("session_rename", "Sign in", self._sign_in_to)]
        self._open_dialog(Dialog(
            "providers", "Providers", [], actions=actions,
            placeholder="Search providers", current=self._provider_name(),
            empty="Loading providers%s" % self.glyphs.ellipsis,
            payload={"submit": self._select_provider}))
        self._load_providers_async(catalog)

    def _sign_in_to(self, item):
        """Sign in to the highlighted provider, by key or device code."""
        name = getattr(item, "value", "") or getattr(item, "id", "")
        if not name or name == "__add__":
            return
        self._close_dialog()
        self._open_login(str(name))

    def _load_providers_async(self, catalog):
        serial = self._dialog_serial
        queue_put = self._queue.put

        def work():
            try:
                rows = catalog.providers()
            except Exception:
                return
            queue_put(("dialog", ("providers", serial, rows), None))

        threading.Thread(target=work, daemon=True).start()

    def _select_provider(self, item):
        if item.value == "__add__":
            self._open_add_provider()
            return
        from . import models
        ok, message = models.set_default(self.config, item.value)
        self._close_dialog()
        self.transcript.add(Entry("info" if ok else "error", text=message))
        if ok:
            self._reprovision(item.value)

    def _open_add_provider(self):
        self._open_dialog(FormDialog(
            "add_provider", "Add provider",
            [FormField("name", "Name", hint="the key you pass to -p"),
             FormField("base_url", "Base URL", "https://"),
             FormField("model", "Model"),
             FormField("dialect", "Dialect", "openai", hint="openai or anthropic"),
             FormField("requires_key", "Needs key", "auto", kind="tribool",
                       hint="auto: no key for a local or LAN address")],
            payload={"submit": self._save_provider}))

    def _save_provider(self, form):
        from . import models
        values = form.values()
        ok, message = models.add_provider(
            self.config, str(values.get("name", "")),
            str(values.get("base_url", "")), str(values.get("model", "")),
            str(values.get("dialect", "openai")) or "openai",
            values.get("requires_key"))
        if not ok:
            form.message = message
            self._dirty = True
            return
        catalog = self._catalog()
        if catalog is not None:
            catalog.invalidate()
        self._close_dialog()
        self.transcript.add(Entry("info", text=message))

    # --- dialogs: sessions -------------------------------------------------

    def _session_store(self):
        """The session database, opened once and kept.

        A fresh SessionStore per call reopens the file and replays the schema
        and migration statements every time, and _search_sessions calls this on
        every keystroke in the session picker. The store serialises its own
        statements, so holding one across the worker thread is safe.
        """
        return self.turn.store()

    def _current_session_id(self) -> str:
        """The session this screen is writing to — the controller's, not a
        guess made by reaching through the command callback."""
        return str(getattr(self.turn.session, "id", "") or "")

    def _session_rows(self, query: str = ""):
        store = self._session_store()
        if store is None:            # no sqlite3: an empty picker, not a crash
            return []
        if query.strip():
            return store.search(query, limit=30)
        return store.list_sessions(limit=50)

    def _open_sessions(self):
        rows = self._session_rows()
        actions = [DialogAction("session_rename", "Rename", self._rename_session),
                   DialogAction("session_delete", "Delete",
                                self._confirm_delete_session)]
        self._open_dialog(Dialog(
            "sessions", "Sessions", session_items(rows, self._current_session_id()),
            actions=actions, placeholder="Search sessions",
            filtered=False,        # the query goes to SessionStore.search()
            current=self._current_session_id(),
            empty="No saved sessions",
            payload={"submit": self._resume_session,
                     "query": self._search_sessions}))

    def _search_sessions(self, query: str):
        dialog = self.dialog
        if dialog is None or dialog.name != "sessions":
            return
        dialog.set_items(session_items(self._session_rows(query),
                                       self._current_session_id()))

    def _resume_session(self, item):
        self._close_dialog()
        result = None
        if self.on_command is not None:
            result = self.on_command("/resume %s" % item.value)
        if result is None:
            store = self._session_store()
            session = store.load(item.value) if store is not None else None
            if session is None:
                self.transcript.add(Entry("error", text="no session %s" % item.value))
                return
            self.agent.messages = list(session.messages)
            # Adopt it, so the turns that follow append to the session the
            # user just picked instead of silently starting a new one.
            self.turn.adopt(session)
            result = "Resumed %s (%d messages)" % (str(item.value)[:8],
                                                   len(session.messages))
        self.transcript.clear()
        self._replay(getattr(self.agent, "messages", None) or [])
        self.transcript.add(Entry("info", text=str(result)))
        self._context = None
        self.follow = True

    def _replay(self, messages):
        """Rebuild the transcript from a restored history.

        Only the shape the screen can show is reconstructed: tool calls come
        back as their result text, because the arguments were never stored.
        """
        for message in messages:
            if isinstance(message, dict):
                role = str(message.get("role", ""))
                content = str(message.get("content") or "")
            else:
                role = str(getattr(message, "role", "") or "")
                content = str(getattr(message, "content", "") or "")
            if not content.strip():
                continue
            if role == "user":
                self.transcript.add(Entry("user", text=content))
            elif role == "assistant":
                self.transcript.add(Entry("assistant", text=content))
            elif role == "tool":
                self.transcript.add(Entry("tool", name="tool", output=content))

    def _rename_session(self, item):
        self._open_dialog(FormDialog(
            "rename_session", "Rename session",
            [FormField("title", "Title",
                       "" if item.title == "(untitled)" else item.title)],
            payload={"submit": self._save_session_title, "session": item.value}))

    def _save_session_title(self, form):
        title = str(form.values().get("title", "")).strip()
        if not title:
            form.message = "a title is required"
            self._dirty = True
            return
        store = self._session_store()
        session = store.load((form.payload or {}).get("session")) if store else None
        if session is None:
            form.message = "session is gone"
            self._dirty = True
            return
        rename = getattr(session, "rename", None) or getattr(session, "set_title", None)
        if rename is None:
            form.message = "this build cannot rename sessions"
            self._dirty = True
            return
        rename(title)
        self._open_sessions()

    def _confirm_delete_session(self, item):
        self._open_dialog(Dialog(
            "confirm_delete", "Delete this session?",
            [PaletteItem(id="no", title="Cancel", value="no"),
             PaletteItem(id="yes", title="Delete", detail=item.title, value="yes")],
            mode="menu", payload={"submit": self._delete_session,
                                  "session": item.value}))

    def _delete_session(self, choice):
        target = (self.dialog.payload or {}).get("session") if self.dialog else None
        if choice.value != "yes" or not target:
            self._open_sessions()
            return
        store = self._session_store()
        if store is not None:
            store.delete(target)
        self._open_sessions()
        self.dialog.message = "deleted"

    # --- dialogs: agents, status, help -------------------------------------

    def _agent_registry(self):
        """The agents module plus the registry `switch_agent` resolves against.

        Loading a fresh one here would list a *different* table: the agent's
        registry was built with the merged project config, so an agent declared
        in haikode.json would be missing from the picker while an agent the
        project disabled would be offered and then raise KeyError on selection.
        """
        from . import agents
        try:
            registry = getattr(self.agent, "registry", None)
        except Exception:
            registry = None
        if isinstance(registry, agents.AgentRegistry):
            return agents, registry
        return agents, agents.AgentRegistry.load(self.cwd)

    def _open_agents(self):
        module, registry = self._agent_registry()
        defs = registry.primary() or [registry.get(name) for name in registry.names()]
        defs = [entry for entry in defs if entry is not None]
        readonly = []
        for entry in defs:
            try:
                if module.is_readonly(entry):
                    readonly.append(entry.name)
            except Exception:
                continue
        self._open_dialog(Dialog(
            "agents", "Select agent", agent_items(defs, self.agent_name, readonly),
            placeholder="Search agents", current=self.agent_name,
            empty="No agents defined",
            payload={"submit": self._select_agent}))

    def _select_agent(self, item):
        name = str(item.value)
        self._close_dialog()
        if self.running:
            # switch_agent swaps tools, permissions and model on the live
            # agent; doing that under a streaming turn resolves the turn's
            # remaining tool calls with the wrong agent's rules.
            self.status_hint = ("agent unchanged: wait for the turn "
                                "to finish, or interrupt it first")
            self._dirty = True
            return
        switch = getattr(self.agent, "switch_agent", None)
        message = ""
        if callable(switch):
            message = str(switch(name) or "")
        elif self.on_command is not None:
            message = str(self.on_command("/agent %s" % name) or "")
        if not message:
            message = "agent → %s" % name
        self.agent_name = name
        self._context = None
        self.transcript.add(Entry("info", text=message))

    def _open_status(self):
        lines = list(status.detail_lines(self._setup(refresh=True)))
        lines.append("")
        lines.extend(usage.detail_lines(self.usage, self._context_state(True)))
        warnings = self._warnings()
        if warnings:
            lines.append("")
            lines.append("Warnings")
            lines.extend("  %s" % warning for warning in warnings)
        self._open_dialog(Dialog("status", "Status", text_items(lines),
                                 mode="pane"))

    def _open_help(self):
        lines = []
        for command, (_default, description) in keybind.DEFINITIONS.items():
            keys = self.keymap.describe(command)
            if not keys:
                continue
            suffix = " (not available)" if command in UNAVAILABLE_BINDINGS else ""
            lines.append("%-26s %s%s" % (keys, description, suffix))
        lines.append("")
        lines.append("%-26s %s" % ("/new /status /help", "screen commands"))
        lines.append("%-26s %s" % ("ctrl+p", "everything else"))
        self._open_dialog(Dialog("help", "Keybindings", text_items(lines),
                                 mode="pane"))

    # --- bindings without a dialog ----------------------------------------

    def _new_session(self):
        if self.running:
            self._interrupt()
        # Orphan the in-flight run so its late events cannot land in the fresh
        # transcript; _pump drops anything carrying the old token.
        self._run_token = None
        self.running = False
        self._stream_entry = None
        self._reasoning_entry = None
        self._open_tool = None
        self.queued = []
        self.transcript.clear()
        # A new conversation needs a new session row; without this the next
        # turn would append to the one the user just left.
        self.turn.reset()
        self.agent = self.agent_factory()
        self._wire_permissions()
        self.interrupted = False
        self._placeholder += 1
        self._setup_cache = None        # the model may have changed under us
        self.usage.reset()
        self._seen_tokens = {"input": 0, "output": 0}
        self._context = None
        self.follow = True
        self._dirty = True

    def _compact_session(self):
        self._dispatch_command("/compact")

    def _export_session(self):
        self._dispatch_command("/export")

    def _undo_message(self):
        self._dispatch_command("/undo")

    def _start_rename(self):
        self.buffer = "/rename "
        self.cursor = len(self.buffer)
        self.status_hint = "type the new session name and press enter"
        self._dirty = True

    def _toggle_reasoning(self):
        self.opts.show_reasoning = not self.opts.show_reasoning
        self._invalidate_view()

    def _toggle_expand(self):
        self.opts.expand = not self.opts.expand
        self._invalidate_view()

    def _quit_app(self):
        self._quit = True

    def _suspend_terminal(self):
        """Yield the terminal to the shell; redraw after the process resumes."""
        if not hasattr(signal, "SIGTSTP"):
            self.status_hint = "terminal suspend is unavailable"
            self._dirty = True
            return
        try:
            curses.endwin()
            os.kill(os.getpid(), signal.SIGTSTP)
        finally:
            self._redraw()

    def _page_up(self):
        self._scroll(-self._transcript_height() + 1)

    def _page_down(self):
        self._scroll(self._transcript_height() - 1)

    def _line_up(self):
        self._scroll(-1)

    def _line_down(self):
        self._scroll(1)

    def _half_page_up(self):
        self._scroll(-max(1, self._transcript_height() // 2))

    def _half_page_down(self):
        self._scroll(max(1, self._transcript_height() // 2))

    def _first_message(self):
        self.scroll = 0
        self.follow = False
        self._dirty = True

    def _last_message(self):
        self.follow = True
        self._dirty = True

    def _cycle_agent(self, step: int):
        _module, registry = self._agent_registry()
        defs = registry.primary() or [registry.get(name)
                                      for name in registry.names()]
        names = [entry.name for entry in defs if entry is not None]
        if not names:
            self.status_hint = "no agents defined"
            self._dirty = True
            return
        current = self.agent_name if self.agent_name in names else names[0]
        index = names.index(current)
        target = names[(index + step) % len(names)]
        self._select_agent(PaletteItem(id=target, title=target, value=target))

    def _cycle_agent_next(self):
        self._cycle_agent(1)

    def _cycle_agent_previous(self):
        self._cycle_agent(-1)

    def _cycle_effort(self):
        choices = tuple(getattr(self.agent, "reasoning_efforts", lambda: ())())
        if not choices:
            self.status_hint = "reasoning effort is not available for this provider"
            self._dirty = True
            return
        current = str(getattr(self.agent, "reasoning_effort", "") or "")
        index = choices.index(current) if current in choices else -1
        selected = choices[(index + 1) % len(choices)]
        if self.on_command is not None:
            result = self.on_command("/effort %s" % selected)
            if result and str(result).startswith("[error]"):
                self.status_hint = str(result)
                self._dirty = True
                return
        else:
            self.agent.set_reasoning_effort(selected)
        self.status_hint = "reasoning effort -> %s" % selected
        self._dirty = True

    # --- usage and context -------------------------------------------------

    def _record_usage(self):
        """Fold what the last turn cost into the session totals.

        Agent.tokens is cumulative for the life of one agent, so the delta
        since the previous turn is what a run actually spent; a rebuilt agent
        restarts at zero and the negative delta is simply dropped.
        """
        tokens = getattr(self.agent, "tokens", None)
        if not isinstance(tokens, dict):
            return
        try:
            now_in = int(tokens.get("input", 0) or 0)
            now_out = int(tokens.get("output", 0) or 0)
        except (TypeError, ValueError):
            return
        delta_in = max(0, now_in - self._seen_tokens["input"])
        delta_out = max(0, now_out - self._seen_tokens["output"])
        self._seen_tokens = {"input": now_in, "output": now_out}
        if delta_in or delta_out:
            self.usage.record(usage.Usage(input_tokens=delta_in,
                                          output_tokens=delta_out))

    def _context_state(self, refresh: bool = False) -> ContextState:
        """The context meter's numbers, cached.

        measure_context() re-reads AGENTS.md and re-prices every tool schema,
        which is far too much work for a 90 ms draw tick, so the answer is kept
        for CONTEXT_TTL and dropped outright whenever a turn ends.
        """
        now = time.time()
        if refresh or self._context is None or now - self._context_at > CONTEXT_TTL:
            try:
                self._context = measure_context(self.agent)
            except Exception:
                self._context = ContextState()
            self._context_at = now
        return self._context

    def _reprovision(self, provider: str = ""):
        """Rebuild the agent after the model or provider changed underneath us.

        The rebuild re-resolves the provider's credentials, which on Haiku
        means the keystore helper and a subprocess per lookup, so it runs on a
        worker; only adopting the result belongs on the curses thread.
        """
        if provider and self.on_command is not None:
            self._run_async("switching to %s" % provider,
                            lambda: self._call_command("/provider %s" % provider),
                            lambda _result: self._adopt_reprovisioned())
            return
        self._adopt_reprovisioned()

    def _adopt_reprovisioned(self, _result=None):
        self._adopt_agent()
        self._context = None
        self._setup_cache = None

    def _agent_label(self) -> str:
        name = self.agent_name
        if not name:
            for attribute in ("agent_name", "agent"):
                value = getattr(self.agent, attribute, "")
                if isinstance(value, str) and value:
                    name = value
                    break
        return name

    def _leader_label(self) -> str:
        if not self.keymap.leader_pending:
            return ""
        chords = self.keymap.leader_chords()
        return "%s%s" % (chords[0].text if chords else "leader", self.glyphs.ellipsis)

    def _help_text(self) -> str:
        """Keys and screen commands, then whatever the command layer owns.

        The REPL handler holds most of the command set (/model, /login, custom
        commands, ...); asking it through the same callback the TUI dispatches
        with is the only way /help can list them without duplicating a table
        that would drift.
        """
        leader = self._keys("leader") or "ctrl+x"
        text = (
            "dialogs\n"
            "  ctrl+p         commands    %-8s m              models\n"
            "  ctrl+a         providers   %-8s l              sessions\n"
            "  %-8s a     agents      %-8s s              status\n"
            "  f1             keybindings f2 / shift+f2         cycle recent model\n"
            "keys\n"
            "  enter          send        alt+enter / trailing \\   newline\n"
            "  esc            interrupt   ctrl-c                   interrupt / exit\n"
            "  pgup/pgdn      scroll      wheel                    scroll\n"
            "  up/down        history     tab                      complete command\n"
            "  ctrl-u         clear input ctrl-w                   delete word\n"
            "  ctrl-r         rename      ctrl-o                   expand tool output\n"
            "  ctrl-t         cycle reasoning effort\n"
            "  ctrl-l         redraw      ctrl-d                   exit\n"
            "screen commands\n"
            "  /new /clear /status /help /keybinds /reasoning /expand /redraw /exit"
            % (leader, leader, leader, leader))
        if self.on_command is None:
            return text
        try:
            rest = self.on_command("/help")
        except Exception:
            rest = None
        return "%s\n%s" % (text, rest) if rest else text

    # --- drawing ---------------------------------------------------------

    def _size(self):
        return self.stdscr.getmaxyx()

    def _prompt(self) -> str:
        return "%s " % self.glyphs.arrow

    def _frame(self) -> Frame:
        """The current band layout, including how tall the prompt wants to be."""
        rows, cols = self._size()
        session = not self._at_home()
        content = max(4, box_width(cols, session=session) - 4)
        wanted = len(layout_input(self.buffer, self.cursor, content,
                                  prompt=self._prompt()).rows)
        todo_rows = 0
        if session:
            todo_rows = len(self._pinned_todo_lines(content))
        return layout_frame(rows, cols, wanted, session=session,
                            wanted_todo_rows=todo_rows)

    def _pinned_todo_lines(self, width: int) -> List[Line]:
        """The plan band's rendered lines, or [] when nothing is outstanding."""
        todos = getattr(getattr(self.agent, "ctx", None), "todos", None)
        if not todos:
            return []
        try:
            return build_pinned_todo_lines(todos, max(1, width), self.opts)
        except Exception:
            return []

    def _transcript_height(self) -> int:
        return self._frame().body_height

    def _at_home(self) -> bool:
        """The home screen stands in for an empty transcript, like opencode's."""
        return not self.transcript.entries and not self.running

    def _view_lines(self) -> List[Line]:
        # Messages and composer share one column. Previously the transcript
        # touched the edge while the prompt floated elsewhere; the home route
        # remains capped, while a live session uses the available padded width.
        return self.transcript.lines(max(1, self._frame().content_width),
                                     self.opts)

    def _effective_config(self):
        """The config `status.collect` should report on.

        It reads two different things off a config: the permission rules, which
        after a project file and an agent overlay live on the object the tools
        actually consult, and the credentials, which only the user's own Config
        knows about. Handed the global config alone -- which is what it used to
        get -- the home screen and /status say "bash asks first" while a
        checked-out haikode.json has quietly made bash allow. Reporting the
        permissions as looser than they are would be a bug; reporting them as
        tighter is how a user gets surprised.
        """
        rules = getattr(getattr(self.agent, "permissions", None), "config", None)
        data = getattr(rules, "data", None)
        if not isinstance(data, dict) or rules is self.config:
            return self.config
        return _ReportedConfig(data, self.config)

    def _setup(self, refresh: bool = False) -> status.SetupInfo:
        """The setup report, cached: collect() runs git and sqlite, and the
        home screen must not pay for that on every frame."""
        now = time.time()
        if refresh or self._setup_cache is None or now - self._setup_at > SETUP_TTL:
            try:
                self._setup_cache = status.collect(
                    self._effective_config(), self._provider_name(), self.cwd,
                    getattr(self.agent, "tools", None))
            except Exception:
                self._setup_cache = status.SetupInfo()
            self._setup_at = now
        return self._setup_cache

    def _addstr(self, y: int, x: int, text: str, attr: int = 0):
        """Clipped, exception-proof write. curses raises on the last cell."""
        rows, cols = self._size()
        if y < 0 or y >= rows or x >= cols:
            return
        text = sanitize(text, self.glyphs.unicode_ok, keep_newlines=False)
        if not text:
            return
        try:
            self.stdscr.addnstr(y, x, text, max(0, cols - x - 1), attr)
        except curses.error:
            pass
        except (UnicodeError, ValueError):
            # The screen's codeset cannot represent what we detected. Fall back
            # to ASCII for good rather than dying halfway down a repaint.
            self._downgrade_encoding()
            try:
                self.stdscr.addnstr(y, x, sanitize(text, False, keep_newlines=False),
                                    max(0, cols - x - 1), attr)
            except Exception:
                pass

    def _downgrade_encoding(self):
        if not self.glyphs.unicode_ok:
            return
        self.glyphs = Glyphs(False)
        self.opts.glyphs = self.glyphs
        self._invalidate_view()

    def _draw(self, refresh: bool = True):
        rows, cols = self._size()
        try:
            self.stdscr.erase()
        except curses.error:
            return
        if rows < MIN_ROWS or cols < MIN_COLS:
            self._addstr(0, 0, "terminal too small", self._attr("error"))
            self._addstr(1, 0, "need %dx%d" % (MIN_COLS, MIN_ROWS), self._attr("hint"))
            if refresh:
                self._refresh()
            return

        frame = self._frame()
        if self._at_home():
            cursor = self._draw_home(frame)
        else:
            cursor = self._draw_transcript(frame)
        self._draw_footer(frame)
        if self.dialog is not None:
            cursor = self._draw_dialog()

        try:
            curses.curs_set(1 if cursor is not None else 0)
        except curses.error:
            pass
        if cursor is not None:
            try:
                self.stdscr.move(min(cursor[0], rows - 1), min(cursor[1], cols - 1))
            except curses.error:
                pass
        if refresh:
            self._refresh()

    def _draw_dialog(self):
        """Blit the open dialog over everything else. Returns its cursor."""
        dialog = self.dialog
        rows, cols = self._size()
        width = min(max(DIALOG_MIN_WIDTH, min(cols - DIALOG_MARGIN,
                                              DIALOG_MAX_WIDTH)), max(4, cols - 3))
        height = min(max(DIALOG_MIN_ROWS, rows - 6), max(1, rows - 3))
        top = max(0, (rows - height - 2) // 2)
        left = max(0, (cols - width - 2) // 2)
        self._draw_box(top, left, height + 2, width + 2)
        if isinstance(dialog, DeviceDialog):
            view = device_view(dialog, width, height, self.glyphs)
        elif isinstance(dialog, FormDialog):
            view = form_view(dialog, width, height, self.glyphs)
        else:
            view = dialog_view(dialog, width, height, self.glyphs, self.keymap)
        for index, row in enumerate(view.rows[:height]):
            self._draw_runs(top + 1 + index, left + 1, row, blanks=True)
        if view.cursor is None:
            return None
        return (top + 1 + view.cursor[0], left + 1 + view.cursor[1])

    def _refresh(self):
        try:
            self.stdscr.noutrefresh()
            curses.doupdate()
        except curses.error:
            pass

    # --- the two views ---------------------------------------------------

    def _draw_transcript(self, frame: Frame):
        """The scrolling conversation, with the same prompt box as the home
        screen sitting under it so the two states read as one design."""
        lines = self._view_lines()
        maximum = max(0, len(lines) - frame.body_height)
        if self.follow:
            self.scroll = maximum
        else:
            self.scroll = max(0, min(self.scroll, maximum))
        # A short conversation hangs from the prompt rather than floating at
        # the top of an empty screen, which is how opencode's session view
        # reads once the home screen has scrolled away.
        pad = max(0, frame.body_height - len(lines))
        for offset, line in enumerate(lines[self.scroll:self.scroll + frame.body_height]):
            if line.text:
                self._addstr(pad + offset, frame.box_left + 2, line.text,
                             self._attr(line.style))
        if frame.todo_rows:
            for offset, line in enumerate(
                    self._pinned_todo_lines(frame.content_width)[:frame.todo_rows]):
                if line.text:
                    self._addstr(frame.todo_top + offset, frame.box_left + 2,
                                 line.text, self._attr(line.style))
        cursor = self._draw_prompt_box(frame.box_top, frame)
        self._draw_hint_row(frame.hint_row, frame)
        return cursor

    def _draw_home(self, frame: Frame):
        """opencode's home route: wordmark, a gap, the prompt, a hint — the
        whole block centred vertically above the footer (routes/home.tsx)."""
        rows, cols = self._size()
        info = self._setup()
        summary = status.summary_lines(info, width=min(frame.box_width, cols - 4),
                                       unicode_ok=self.glyphs.unicode_ok)

        logo = wordmark_rows(self.glyphs.unicode_ok)
        # Four rows of block glyphs plus the summary plus the prompt simply do
        # not fit a small terminal; a one-line header does.
        if (rows < HOME_MIN_ROWS or cols < HOME_MIN_COLS
                or wordmark_width(self.glyphs.unicode_ok) > cols - 2):
            logo = []

        head = len(logo) if logo else 1
        block = head + 1 + len(summary) + 1 + frame.box_height + 1
        available = frame.footer_row
        while block > available and len(summary) > 1:
            # Decoration goes first, then facts, and the warning last: "no key
            # for X" is the one line that has to survive a short screen.
            order = []
            for style in ("muted", "info", "warn"):
                order.extend(reversed([i for i, (_, own) in enumerate(summary)
                                       if own == style]))
            summary.pop(order[0] if order else len(summary) - 1)
            block -= 1
        if block > available and logo:
            block -= len(logo) - 1
            logo = []

        y = max(0, (available - block) // 2)
        if logo:
            for runs in logo:
                self._draw_runs(y, max(0, (cols - sum(len(t) for t, _ in runs)) // 2),
                                runs)
                y += 1
        else:
            self._centred(y, self.header or "haikode", "header")
            y += 1
        y += 1
        for text, style in summary:
            self._centred(y, text, SUMMARY_STYLES.get(style, "hint"))
            y += 1
        y += 1
        # On a screen too short even for the trimmed block the prompt drops to
        # where the transcript view keeps it, so it can never land on (or
        # below) the footer; the summary above it is what gets squeezed.
        y = min(y, frame.box_top)
        cursor = self._draw_prompt_box(y, frame)
        self._draw_hint_row(y + frame.box_height, frame)
        return cursor

    # --- shared furniture -------------------------------------------------

    def _centred(self, y: int, text: str, style: str):
        _, cols = self._size()
        text = text[:max(0, cols - 1)]
        self._addstr(y, max(0, (cols - len(text)) // 2), text, self._attr(style))

    def _draw_runs(self, y: int, x: int, runs: Sequence[Sequence[str]],
                   blanks: bool = False):
        """Draw pre-styled segments left to right — the wordmark's marks each
        carry their own colour pair, so it cannot go out as one string.

        Blank runs are skipped by default so the wordmark does not erase what
        it is drawn over; a dialog asks for them, because its selected row is
        painted edge to edge and that padding IS the highlight.
        """
        for text, style in runs:
            if blanks or text.strip() or style.endswith("_fill"):
                self._addstr(y, x, text, self._attr(style))
            x += len(text)

    def _draw_prompt_box(self, top: int, frame: Frame):
        """The bordered prompt of component/prompt: a box, two columns of
        padding, and a dim suggestion while the buffer is empty."""
        self._draw_box(top, frame.box_left, frame.box_height, frame.box_width,
                       style="box")
        prompt = self._prompt()
        left = frame.box_left + 2
        layout = layout_input(self.buffer, self.cursor, frame.content_width,
                              prompt=prompt, cont=" " * len(prompt),
                              max_rows=frame.input_rows)
        cursor = None
        for offset, row in enumerate(layout.rows[:frame.input_rows]):
            y = top + 1 + offset
            self._addstr(y, left, row[:len(prompt)],
                         self._attr("prompt" if offset == 0 else "hint"))
            self._addstr(y, left + len(prompt), row[len(prompt):],
                         self._attr("assistant"))
            if offset == layout.cursor_row:
                cursor = (y, left + layout.cursor_col)
        if not self.buffer:
            text = placeholder_text(self._placeholder,
                                    max(0, frame.content_width - len(prompt)))
            self._addstr(top + 1, left + len(prompt), text, self._attr("hint"))
        return cursor

    def _draw_hint_row(self, y: int, frame: Frame):
        """The row under the prompt: the context meter on the right, hint left.

        opencode puts the context share right next to its prompt
        (component/prompt/index.tsx); the meter is aligned with the prompt
        box's right edge so the two read as one widget.
        """
        runs = context_runs(self._context_state(),
                            max(0, min(30, frame.box_width)), self.glyphs)
        meter = sum(len(text) for text, _ in runs)
        room = frame.cols - 1
        if meter:
            left = max(0, min(frame.box_left + frame.box_width - meter,
                              frame.cols - meter - 1))
            self._draw_runs(y, left, runs, blanks=True)
            room = left - 1
        text = hint_line(self.glyphs.unicode_ok, max(0, room - 2))
        if text:
            self._addstr(y, max(0, (room - len(text)) // 2), text,
                         self._attr("hint"))

    def _draw_footer(self, frame: Frame):
        hint = self.status_hint
        if not hint and self.queued:
            hint = "%d queued" % len(self.queued)
        if not hint and not self.follow:
            hint = "scrolled — esc to follow" if self.glyphs.unicode_ok \
                else "scrolled - esc to follow"
        # Last, so it never hides a live one, but permanent while it holds:
        # the user must not believe /undo is available when it is not.
        if not hint:
            hint = self.turn.persistence_notice()
        text = build_status(
            provider=self._provider_label(),
            cwd_name=status.short_label(self.cwd, 24),
            tokens_in=self._tokens("input"),
            tokens_out=self._tokens("output"),
            width=frame.cols - 1,
            glyphs=self.glyphs,
            busy=self.running,
            frame=self.frame,
            elapsed=time.time() - self._run_started if self.running else 0.0,
            hint=hint,
            state="interrupted" if self.interrupted else "ready",
            agent=self._agent_label(),
            # The hint row directly above already owns the context meter. It
            # appeared twice on every frame and crowded out model/status text.
            context="",
            leader=self._leader_label(),
            yolo=bool(getattr(getattr(self.agent, "permissions", None),
                              "yolo", False)))
        self._addstr(frame.footer_row, 0, text.ljust(frame.cols - 1),
                     self._attr("status"))

    def _provider_name(self) -> str:
        """Which provider this session actually talks to.

        The live agent is asked first: `haikode -p openai` overrides the
        configured default for this run only, so reading default_provider
        would make the home screen and the footer report a provider the
        session is not using. Anthropic and the OAuth providers hardcode
        their class name, which need not be the config key, so the agent's
        answer is only trusted when it names a configured provider.
        """
        try:
            configured = self.config.data.get("providers", {}) or {}
        except Exception:
            configured = {}
        try:
            name = str(getattr(getattr(self.agent, "provider", None), "name", ""))
            if name in configured:
                return name
        except Exception:
            pass
        try:
            return self.config.data.get("default_provider", "") or ""
        except Exception:
            return ""

    def _provider_label(self) -> str:
        name = self._provider_name()
        model = str(getattr(self.agent, "model", "") or "")
        if name and model:
            return "%s/%s" % (name, model)
        return model or name or "haikode"

    def _tokens(self, key: str) -> int:
        try:
            return int(self.agent.tokens.get(key, 0))
        except Exception:
            return 0

    # --- permission modal ------------------------------------------------

    def _permission_body(self, request) -> List[Line]:
        """Turn a PermissionRequest into styled body lines."""
        _, cols = self._size()
        width = max(MIN_COLS, min(cols - 6, 110)) - 2
        metadata = getattr(request, "metadata", None) or {}
        diff = metadata.get("diff")
        if diff:
            return build_diff_lines(str(diff), width,
                                    RenderOptions(self.glyphs, expand=False,
                                                  diff_lines=200), indent=" ")
        for key, style in (("command", "tool"), ("url", "tool"), ("path", "result")):
            value = metadata.get(key)
            if value:
                return _styled(str(value), width, style, " ", "   ")
        patterns = getattr(request, "patterns", None) or []
        if patterns:
            return _styled(", ".join(str(p) for p in patterns), width, "result", " ", "   ")
        return []

    def _modal_permission(self, request) -> str:
        """Draw the modal and block the MAIN thread until the user answers.

        Safe to block here: the only producer of agent events is the worker,
        and it is parked on the Event we are about to set.
        """
        title = str(getattr(request, "title", "") or "Permission required")
        key = str(getattr(request, "key", "") or "")
        body = self._permission_body(request)
        offset = 0
        body_height = 1
        needs_draw = True
        fullscreen = False

        while True:
            rows, cols = self._size()
            if rows < MIN_ROWS or cols < MIN_COLS:
                return "reject"

            if needs_draw:
                width = (max(MIN_COLS, cols - 2) if fullscreen
                         else max(MIN_COLS, min(cols - 4, 112)))
                body_height = max(1, min(len(body) or 1, rows - 8))
                height = body_height + 6
                top = max(0, (rows - height) // 2)
                left = max(0, (cols - width) // 2)
                offset = max(0, min(offset, max(0, len(body) - body_height)))

                self._draw(refresh=False)  # transcript stays visible behind
                self._draw_box(top, left, height, width)
                self._addstr(top + 1, left + 2, title[:width - 4],
                             self._attr("modal_title"))
                if key:
                    label = "[%s]" % key
                    self._addstr(top + 1, left + width - len(label) - 2, label,
                                 self._attr("hint"))
                for row, line in enumerate(body[offset:offset + body_height]):
                    self._addstr(top + 3 + row, left + 2, line.text[:width - 4],
                                 self._attr(line.style))
                hidden = len(body) - body_height - offset
                if hidden > 0:
                    more = "%s %d more" % (self.glyphs.ellipsis, hidden)
                    # Body occupies top+3 .. top+2+body_height; the marker gets
                    # the free row below it, not the last line of the body.
                    self._addstr(top + 3 + body_height,
                                 max(left + 2, left + width - len(more) - 3),
                                 more, self._attr("hint"))
                options = "[o]nce   [a]lways   [r]eject      (esc rejects)"
                self._addstr(top + height - 2, left + 2, options[:width - 4],
                             self._attr("modal_border"))
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
                self._refresh()
                needs_draw = False

            try:
                action = self._modal_key()
            except KeyboardInterrupt:
                action = "reject"
            if action in ("once", "always", "reject"):
                self.status_hint = "rejected" if action == "reject" else ""
                self._dirty = True
                return action
            if action is None:
                continue
            if action == "up":
                offset -= 1
            elif action == "down":
                offset += 1
            elif action == "pgup":
                offset -= body_height
            elif action == "pgdn":
                offset += body_height
            elif action == "fullscreen":
                fullscreen = not fullscreen
            needs_draw = True

    def _modal_key(self) -> Optional[str]:
        """None = nothing happened (poll timeout); "" = redraw but no action."""
        key = self._read_key()
        if key is None:
            return None
        event = self._key_event(key)
        if event is not None:
            try:
                command = self.keymap.lookup(
                    event, among=("permission.prompt.fullscreen",))
            except Exception:
                command = None
            if command == "permission.prompt.fullscreen":
                return "fullscreen"
        if isinstance(key, str):
            lowered = key.lower()
            if lowered in ("o", "y"):
                return "once"
            if lowered == "a":
                return "always"
            if lowered in ("r", "n", "q"):
                return "reject"
            return None
        if key in (10, 13, curses.KEY_ENTER):
            return "once"
        if key == 27:
            return "reject" if self._peek_key() is None else None
        if key == 3:
            return "reject"
        if key == curses.KEY_UP:
            return "up"
        if key == curses.KEY_DOWN:
            return "down"
        if key == curses.KEY_PPAGE:
            return "pgup"
        if key == curses.KEY_NPAGE:
            return "pgdn"
        if key == curses.KEY_RESIZE:
            self._on_resize()
            return ""
        return None

    def _draw_box(self, top: int, left: int, height: int, width: int,
                  style: str = "modal_border"):
        g = self.glyphs
        attr = self._attr(style)
        tl, tr, bl, br = (g.corners + "++++")[:4]
        horizontal = g.hbar * max(0, width - 2)
        self._addstr(top, left, tl + horizontal + tr, attr)
        for row in range(1, height - 1):
            self._addstr(top + row, left, g.vbar, attr)
            self._addstr(top + row, left + 1, " " * max(0, width - 2))
            self._addstr(top + row, left + width - 1, g.vbar, attr)
        self._addstr(top + height - 1, left, bl + horizontal + br, attr)


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


def _prepare_locale():
    """Give ncurses a codeset it can actually write the wordmark in.

    ncurses encodes every addstr through the C locale, so the codeset decided
    here is what Glyphs.detect() will find after initscr. Haiku exports no LANG
    at all — its Terminal is UTF-8 only, so nobody ever needed one — which
    leaves the locale at US-ASCII and turns every box glyph into "?". That one
    platform gets upgraded, but only when the environment asked for nothing:
    an explicit LC_ALL=C is how a serial session says "ASCII, please".
    """
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    if any(os.environ.get(name) for name in ("LC_ALL", "LC_CTYPE", "LANG")):
        return
    try:
        if platform.system() != "Haiku":
            return
    except Exception:  # pragma: no cover - platform never raises in practice
        return
    for candidate in ("en_US.UTF-8", "C.UTF-8", "UTF-8"):
        try:
            locale.setlocale(locale.LC_ALL, candidate)
            return
        except locale.Error:
            continue


def _wrap_curses(body: Callable[[Any], Any]):
    """curses.wrapper, but it always restores the tty and never swallows why.

    curses.wrapper leaves the terminal wedged if initscr() itself half-succeeds
    (no TERM, a pty that vanished), which is exactly the case where the caller
    wants to fall back to the plain REPL — so the teardown is unconditional.
    """
    if curses is None:
        raise TUIUnavailable("curses is not available in this Python build")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise TUIUnavailable("not a terminal")
    term = os.environ.get("TERM", "")
    if not term or term == "dumb":
        raise TUIUnavailable("TERM=%r cannot drive a full-screen UI" % term)

    _prepare_locale()

    stdscr = None
    try:
        try:
            stdscr = curses.initscr()
        except Exception as exc:
            raise TUIUnavailable("curses could not start: %s" % exc)
        try:
            curses.noecho()
            curses.cbreak()
            stdscr.keypad(True)
        except curses.error:
            pass
        return body(stdscr)
    finally:
        if stdscr is not None:
            for restore in (lambda: stdscr.keypad(False),
                            curses.nocbreak,
                            curses.echo,
                            lambda: curses.curs_set(1),
                            lambda: curses.mousemask(0),
                            curses.noraw,
                            curses.endwin):
                try:
                    restore()
                except Exception:
                    pass
            # endwin() leaves the cursor wherever the app left it.
            try:
                sys.stdout.write("\n")
                sys.stdout.flush()
            except Exception:
                pass


def run_tui(agent_factory: Callable[[], Any], config: Any, cwd: str = ".",
            on_command: Optional[Callable[[str], Optional[str]]] = None,
            completer: Optional[Callable[[str], List[str]]] = None,
            header: str = "", agent: Any = None,
            turn: Optional[TurnController] = None) -> None:
    """Run the full-screen UI.

    `agent` is the agent to start with (main.py may already have resumed a
    session into it) and `turn` is the TurnController shared with the REPL.

    Raises TUIUnavailable (a RuntimeError) if this terminal cannot host it, so
    main.py can catch it and fall back to the readline REPL.
    """
    tui = TUI(agent_factory, config, cwd, on_command=on_command,
              completer=completer, header=header, agent=agent, turn=turn)
    _wrap_curses(tui.run)
