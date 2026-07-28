"""
Keybindings, ported from opencode (packages/tui/src/config/keybind.ts) so the
Haiku TUI answers to the same keys people already know.

Names, defaults and descriptions are carried over verbatim wherever the feature
exists here. Groups tied to features haikode does not have (the diff viewer,
plugin manager and which-key panel) are left out entirely; individual bindings
for missing features keep their opencode name but default to "none" so a shared
config file never trips the unknown-name warning.

Deviations from opencode, all forced by curses or by a missing feature:
  * help_show gets "<leader>?,f1" (opencode ships it unbound and reaches help
    through its command palette).
  * dialog.select.cancel ("escape") is added; opencode's select dialog closes
    through its own escape handler rather than a named binding.
  * session_timeline defaults to "none" instead of opencode's "<leader>g",
    because haikode has no timeline view yet.
  * super+/hyper+ alternatives are dropped at parse time -- a terminal never
    reports those modifiers, so a chord containing one can never fire.

Resolution is deliberately flat: several commands share a chord (ctrl+a is both
model_provider_list and input_line_home, exactly as in opencode, which
disambiguates by widget focus). lookup() returns the first match in DEFINITIONS
order; callers that know their focus pass `among=` to scope the search:

    keymap.lookup(event, among=("input_line_home", "input_submit"))
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

LEADER_DEFAULT = "ctrl+x"
LEADER_TOKEN = "<leader>"

# Values that mean "this command has no key". opencode allows the JSON literal
# false as well as the string "none". Use is_disabled() to test a config value;
# membership in this tuple compares with == and so would also swallow 0.
DISABLED_VALUES = (False, None, "none", "")


def _kb(default: Any, description: str) -> Tuple[Any, str]:
    return (default, description)


# Ordered name -> (default binding, description). Order is significant: it is
# the priority used when two commands share a chord.
DEFINITIONS: Dict[str, Tuple[Any, str]] = {
    "leader": _kb(LEADER_DEFAULT, "Leader key for keybind combinations"),

    "app_exit": _kb("ctrl+c,ctrl+d,<leader>q", "Exit the application"),
    "app_debug": _kb("none", "Toggle debug panel"),
    "app_toggle_animations": _kb("none", "Toggle animations"),
    "app_toggle_file_context": _kb("none", "Toggle file context"),
    "app_toggle_session_directory_filter": _kb("none", "Toggle session directory filtering"),
    "command_list": _kb("ctrl+p", "List available commands"),
    "help_show": _kb("<leader>?,f1", "Open help dialog"),
    "docs_open": _kb("none", "Open documentation"),

    "editor_open": _kb("<leader>e", "Open external editor"),
    "theme_list": _kb("<leader>t", "List available themes"),
    "theme_switch_mode": _kb("none", "Switch between light and dark theme mode"),
    "sidebar_toggle": _kb("<leader>b", "Toggle sidebar"),
    "scrollbar_toggle": _kb("none", "Toggle session scrollbar"),
    "status_view": _kb("<leader>s", "View status"),
    "debug_view": _kb("none", "View debug info"),

    "session_export": _kb("<leader>x", "Export session to editor"),
    "session_copy": _kb("none", "Copy session transcript"),
    "session_new": _kb("<leader>n", "Create a new session"),
    "session_list": _kb("<leader>l", "List all sessions"),
    "session_timeline": _kb("none", "Show session timeline"),
    "session_fork": _kb("none", "Fork session from message"),
    "session_rename": _kb("ctrl+r", "Rename session"),
    "session_delete": _kb("ctrl+d", "Delete session"),
    "session_share": _kb("none", "Share current session"),
    "session_unshare": _kb("none", "Unshare current session"),
    "session_interrupt": _kb("escape", "Interrupt current session"),
    "session_compact": _kb("<leader>c", "Compact the session"),
    "session_toggle_timestamps": _kb("none", "Toggle message timestamps"),
    "session_toggle_generic_tool_output": _kb("none", "Toggle generic tool output"),
    "session_queued_prompts": _kb("<leader>q", "Manage queued prompts"),
    "session_quick_switch_1": _kb("<leader>1", "Switch to session in quick slot 1"),
    "session_quick_switch_2": _kb("<leader>2", "Switch to session in quick slot 2"),
    "session_quick_switch_3": _kb("<leader>3", "Switch to session in quick slot 3"),
    "session_quick_switch_4": _kb("<leader>4", "Switch to session in quick slot 4"),
    "session_quick_switch_5": _kb("<leader>5", "Switch to session in quick slot 5"),
    "session_quick_switch_6": _kb("<leader>6", "Switch to session in quick slot 6"),
    "session_quick_switch_7": _kb("<leader>7", "Switch to session in quick slot 7"),
    "session_quick_switch_8": _kb("<leader>8", "Switch to session in quick slot 8"),
    "session_quick_switch_9": _kb("<leader>9", "Switch to session in quick slot 9"),

    "model_provider_list": _kb("ctrl+a", "Open provider list from model dialog"),
    "model_favorite_toggle": _kb("ctrl+f", "Toggle model favorite status"),
    "model_list": _kb("<leader>m", "List available models"),
    "model_cycle_recent": _kb("f2", "Next recently used model"),
    "model_cycle_recent_reverse": _kb("shift+f2", "Previous recently used model"),
    "model_cycle_favorite": _kb("none", "Next favorite model"),
    "model_cycle_favorite_reverse": _kb("none", "Previous favorite model"),
    "mcp_list": _kb("none", "List MCP servers"),
    "provider_connect": _kb("none", "Connect provider"),
    "agent_list": _kb("<leader>a", "List agents"),
    "agent_cycle": _kb("tab", "Next agent"),
    "agent_cycle_reverse": _kb("shift+tab", "Previous agent"),
    "variant_cycle": _kb("ctrl+t", "Cycle model variants"),
    "variant_list": _kb("none", "List model variants"),

    "messages_page_up": _kb("pageup,ctrl+alt+b", "Scroll messages up by one page"),
    "messages_page_down": _kb("pagedown,ctrl+alt+f", "Scroll messages down by one page"),
    "messages_line_up": _kb("ctrl+alt+y", "Scroll messages up by one line"),
    "messages_line_down": _kb("ctrl+alt+e", "Scroll messages down by one line"),
    "messages_half_page_up": _kb("ctrl+alt+u", "Scroll messages up by half page"),
    "messages_half_page_down": _kb("ctrl+alt+d", "Scroll messages down by half page"),
    "messages_first": _kb("ctrl+g,home", "Navigate to first message"),
    "messages_last": _kb("ctrl+alt+g,end", "Navigate to last message"),
    "messages_next": _kb("none", "Navigate to next message"),
    "messages_previous": _kb("none", "Navigate to previous message"),
    "messages_copy": _kb("<leader>y", "Copy message"),
    "messages_undo": _kb("<leader>u", "Undo message"),
    "messages_redo": _kb("<leader>r", "Redo message"),
    "tool_details": _kb("none", "Toggle tool details visibility"),
    "display_thinking": _kb("none", "Toggle thinking blocks visibility"),

    "prompt_submit": _kb("none", "Submit prompt"),
    "prompt_skills": _kb("none", "Open skill selector"),

    "input_clear": _kb("ctrl+c", "Clear input field"),
    "input_paste": _kb("ctrl+v", "Paste from clipboard"),
    "input_submit": _kb("return", "Submit input"),
    "input_newline": _kb("shift+return,ctrl+return,alt+return,ctrl+j", "Insert newline in input"),
    "input_move_left": _kb("left,ctrl+b", "Move cursor left in input"),
    "input_move_right": _kb("right,ctrl+f", "Move cursor right in input"),
    "input_move_up": _kb("up", "Move cursor up in input"),
    "input_move_down": _kb("down", "Move cursor down in input"),
    "input_line_home": _kb("ctrl+a", "Move to start of line in input"),
    "input_line_end": _kb("ctrl+e", "Move to end of line in input"),
    "input_buffer_home": _kb("home", "Move to start of buffer in input"),
    "input_buffer_end": _kb("end", "Move to end of buffer in input"),
    "input_delete_to_line_end": _kb("ctrl+k", "Delete to end of line in input"),
    "input_delete_to_line_start": _kb("ctrl+u", "Delete to start of line in input"),
    "input_backspace": _kb("backspace,shift+backspace", "Backspace in input"),
    "input_delete": _kb("ctrl+d,delete,shift+delete", "Delete character in input"),
    "input_word_forward": _kb("alt+f,alt+right,ctrl+right", "Move word forward in input"),
    "input_word_backward": _kb("alt+b,alt+left,ctrl+left", "Move word backward in input"),
    "input_delete_word_forward": _kb("alt+d,alt+delete,ctrl+delete", "Delete word forward in input"),
    "input_delete_word_backward": _kb("ctrl+w,ctrl+backspace,alt+backspace", "Delete word backward in input"),
    "history_previous": _kb("up", "Previous history item"),
    "history_next": _kb("down", "Next history item"),

    "dialog.select.prev": _kb("up,ctrl+p", "Move to previous dialog item"),
    "dialog.select.next": _kb("down,ctrl+n", "Move to next dialog item"),
    "dialog.select.page_up": _kb("pageup", "Move up one page in dialog"),
    "dialog.select.page_down": _kb("pagedown", "Move down one page in dialog"),
    "dialog.select.home": _kb("home", "Move to first dialog item"),
    "dialog.select.end": _kb("end", "Move to last dialog item"),
    "dialog.select.submit": _kb("return", "Submit selected dialog item"),
    "dialog.select.cancel": _kb("escape", "Close the dialog"),
    "dialog.prompt.submit": _kb("return", "Submit dialog prompt"),
    "prompt.autocomplete.prev": _kb("up,ctrl+p", "Move to previous autocomplete item"),
    "prompt.autocomplete.next": _kb("down,ctrl+n", "Move to next autocomplete item"),
    "prompt.autocomplete.hide": _kb("escape", "Hide autocomplete"),
    "prompt.autocomplete.select": _kb("return", "Select autocomplete item"),
    "prompt.autocomplete.complete": _kb("tab", "Complete autocomplete item"),
    "permission.prompt.fullscreen": _kb("ctrl+f", "Toggle permission prompt fullscreen"),

    "terminal_suspend": _kb("ctrl+z", "Suspend terminal"),
    "tips_toggle": _kb("<leader>h", "Toggle tips on home screen"),
}

# opencode's CommandMap: keybind name -> command id used by its command
# palette. Kept so the palette here can show and dispatch the same ids.
COMMAND_MAP: Dict[str, str] = {
    "app_exit": "app.exit",
    "app_debug": "app.debug",
    "app_toggle_animations": "app.toggle.animations",
    "app_toggle_file_context": "app.toggle.file_context",
    "app_toggle_session_directory_filter": "app.toggle.session_directory_filter",
    "command_list": "command.palette.show",
    "help_show": "help.show",
    "docs_open": "docs.open",
    "editor_open": "prompt.editor",
    "theme_list": "theme.switch",
    "theme_switch_mode": "theme.switch_mode",
    "sidebar_toggle": "session.sidebar.toggle",
    "scrollbar_toggle": "session.toggle.scrollbar",
    "status_view": "opencode.status",
    "debug_view": "opencode.debug",
    "session_export": "session.export",
    "session_copy": "session.copy",
    "session_new": "session.new",
    "session_list": "session.list",
    "session_timeline": "session.timeline",
    "session_fork": "session.fork",
    "session_rename": "session.rename",
    "session_delete": "session.delete",
    "session_share": "session.share",
    "session_unshare": "session.unshare",
    "session_interrupt": "session.interrupt",
    "session_compact": "session.compact",
    "session_toggle_timestamps": "session.toggle.timestamps",
    "session_toggle_generic_tool_output": "session.toggle.generic_tool_output",
    "session_queued_prompts": "session.queued_prompts",
    "model_provider_list": "model.dialog.provider",
    "model_favorite_toggle": "model.dialog.favorite",
    "model_list": "model.list",
    "model_cycle_recent": "model.cycle_recent",
    "model_cycle_recent_reverse": "model.cycle_recent_reverse",
    "model_cycle_favorite": "model.cycle_favorite",
    "model_cycle_favorite_reverse": "model.cycle_favorite_reverse",
    "mcp_list": "mcp.list",
    "provider_connect": "provider.connect",
    "agent_list": "agent.list",
    "agent_cycle": "agent.cycle",
    "agent_cycle_reverse": "agent.cycle.reverse",
    "variant_cycle": "variant.cycle",
    "variant_list": "variant.list",
    "messages_page_up": "session.page.up",
    "messages_page_down": "session.page.down",
    "messages_line_up": "session.line.up",
    "messages_line_down": "session.line.down",
    "messages_half_page_up": "session.half.page.up",
    "messages_half_page_down": "session.half.page.down",
    "messages_first": "session.first",
    "messages_last": "session.last",
    "messages_next": "session.message.next",
    "messages_previous": "session.message.previous",
    "messages_copy": "messages.copy",
    "messages_undo": "session.undo",
    "messages_redo": "session.redo",
    "tool_details": "session.toggle.actions",
    "display_thinking": "session.toggle.thinking",
    "prompt_submit": "prompt.submit",
    "prompt_skills": "prompt.skills",
    "input_clear": "prompt.clear",
    "input_paste": "prompt.paste",
    "input_submit": "input.submit",
    "input_newline": "input.newline",
    "input_move_left": "input.move.left",
    "input_move_right": "input.move.right",
    "input_move_up": "input.move.up",
    "input_move_down": "input.move.down",
    "input_line_home": "input.line.home",
    "input_line_end": "input.line.end",
    "input_buffer_home": "input.buffer.home",
    "input_buffer_end": "input.buffer.end",
    "input_delete_to_line_end": "input.delete.to.line.end",
    "input_delete_to_line_start": "input.delete.to.line.start",
    "input_backspace": "input.backspace",
    "input_delete": "input.delete",
    "input_word_forward": "input.word.forward",
    "input_word_backward": "input.word.backward",
    "input_delete_word_forward": "input.delete.word.forward",
    "input_delete_word_backward": "input.delete.word.backward",
    "history_previous": "prompt.history.previous",
    "history_next": "prompt.history.next",
    "terminal_suspend": "terminal.suspend",
    "tips_toggle": "tips.toggle",
}
for _slot in range(1, 10):
    COMMAND_MAP["session_quick_switch_%d" % _slot] = "session.quick_switch.%d" % _slot

DESCRIPTIONS: Dict[str, str] = {name: item[1] for name, item in DEFINITIONS.items()}

# Alternate spellings accepted in binding strings, mapped to the canonical name
# a KeyEvent carries.
KEY_ALIASES: Dict[str, str] = {
    "enter": "return",
    "cr": "return",
    "ret": "return",
    "esc": "escape",
    "pgup": "pageup",
    "prior": "pageup",
    "pgdn": "pagedown",
    "pagedn": "pagedown",
    "next": "pagedown",
    "spc": "space",
    "bs": "backspace",
    "del": "delete",
    "ins": "insert",
}

_MODIFIER_ALIASES = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "ctl": "ctrl",
    "c": "ctrl",
    "alt": "alt",
    "meta": "alt",
    "opt": "alt",
    "option": "alt",
    "m": "alt",
    "shift": "shift",
    "s": "shift",
}

# Modifiers a terminal cannot report; chords using them are dropped.
_UNSUPPORTED_MODIFIERS = {"super", "cmd", "command", "win", "hyper", "mod"}


def is_disabled(value: Any) -> bool:
    """
    True for the values opencode reads as "this command has no key": the JSON
    literal false, null, "none", and the empty string or list. Identity checks
    on the booleans, because 0 == False would otherwise disable a command.
    """
    if value is None or value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("", "none")
    if isinstance(value, (list, tuple)):
        return len(value) == 0
    return False


def normalise_key(name: str) -> Tuple[str, bool]:
    """
    Canonical key name plus whether the spelling itself implies shift.

    A bare uppercase letter is opencode's shorthand for shift+letter (see its
    diff_expand_all: "E"), and curses hands us the same uppercase character, so
    both sides collapse to ("e", True).
    """
    if not name:
        return ("", False)
    if len(name) == 1:
        if name.isalpha() and name.isupper():
            return (name.lower(), True)
        if name == " ":
            return ("space", False)
        return (name, False)
    low = name.strip().lower()
    return (KEY_ALIASES.get(low, low), False)


@dataclass(frozen=True)
class Chord:
    """One key press. `leader` means it only fires after the leader chord."""

    key: str
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    leader: bool = False

    @property
    def text(self) -> str:
        """Round-trips through parse_binding()."""
        parts = []
        if self.ctrl:
            parts.append("ctrl")
        if self.alt:
            parts.append("alt")
        if self.shift:
            parts.append("shift")
        parts.append(self.key)
        body = "+".join(parts)
        return LEADER_TOKEN + body if self.leader else body

    def __str__(self) -> str:
        return self.text


@dataclass
class KeyEvent:
    """A key press as the TUI observed it."""

    key: str
    ctrl: bool = False
    alt: bool = False
    shift: bool = False

    @property
    def text(self) -> str:
        return self.chord().text

    def chord(self) -> Chord:
        """Normalised, leaderless chord this event matches against."""
        key, implied_shift = normalise_key(self.key)
        return Chord(key=key, ctrl=self.ctrl, alt=self.alt,
                     shift=self.shift or implied_shift)


def _parse_chord(token: str) -> Optional[Chord]:
    """None when the token is empty, malformed, or needs a modifier a terminal cannot send."""
    text = token.strip()
    if not text:
        return None

    leader = False
    while text[:len(LEADER_TOKEN)].lower() == LEADER_TOKEN:
        leader = True
        text = text[len(LEADER_TOKEN):].strip()
    if not text:
        return None

    # "ctrl++" and a bare "+" both name the plus key, so peel it off before
    # splitting on the modifier separator.
    if len(text) == 1:
        parts = [text]
    elif text.endswith("+"):
        parts = [p for p in text[:-1].split("+") if p] + ["+"]
    else:
        parts = text.split("+")

    key_text = parts[-1]
    ctrl = alt = shift = False
    for raw in parts[:-1]:
        mod = raw.strip().lower()
        if mod in _UNSUPPORTED_MODIFIERS:
            return None
        canonical = _MODIFIER_ALIASES.get(mod)
        if canonical == "ctrl":
            ctrl = True
        elif canonical == "alt":
            alt = True
        elif canonical == "shift":
            shift = True
        else:
            return None

    key, implied_shift = normalise_key(key_text)
    if not key:
        return None
    return Chord(key=key, ctrl=ctrl, alt=alt, shift=shift or implied_shift,
                 leader=leader)


def _chord_from_stroke(stroke: Dict[str, Any]) -> Optional[Chord]:
    """
    opencode's KeyStroke object form: {"name": "v", "ctrl": true, "meta": true}.
    Note that it spells alt "meta". Returns None for the modifiers a terminal
    cannot report, same as the string form.
    """
    name = stroke.get("name")
    if not isinstance(name, str):
        return None
    if stroke.get("super") or stroke.get("hyper"):
        return None
    key, implied_shift = normalise_key(name)
    if not key:
        return None
    return Chord(key=key, ctrl=bool(stroke.get("ctrl")),
                 alt=bool(stroke.get("meta")) or bool(stroke.get("alt")),
                 shift=bool(stroke.get("shift")) or implied_shift)


def parse_binding(text: Any) -> List[Chord]:
    """
    Accepts opencode's binding values: a comma-separated string
    ("ctrl+c,ctrl+d,<leader>q"), a list of such items, a {"key": ...} object, a
    bare {"name": ..., "ctrl": ...} stroke, or the disabling values false /
    "none". Never raises -- unparseable alternatives are simply dropped, so a
    typo costs one binding, not the TUI.
    """
    if is_disabled(text):
        return []

    items: List[Any]
    if isinstance(text, (list, tuple)):
        items = list(text)
    else:
        items = [text]

    chords: List[Chord] = []
    for item in items:
        if isinstance(item, dict):
            # A BindingObject wraps the real key; a bare KeyStroke is the key.
            inner = item.get("key", item)
            if isinstance(inner, dict):
                chord = _chord_from_stroke(inner)
                if chord is not None and chord not in chords:
                    chords.append(chord)
                continue
            item = inner
        if not isinstance(item, str):
            continue
        if item.strip().lower() == "none":
            continue
        for token in item.split(","):
            chord = _parse_chord(token)
            if chord is not None and chord not in chords:
                chords.append(chord)
    return chords


def default_bindings() -> Dict[str, List[Chord]]:
    return {name: parse_binding(item[0]) for name, item in DEFINITIONS.items()}


class Keymap:
    """
    Chord -> command resolution, including opencode's two-step leader sequence.

    Stateful on purpose: after the leader chord the map is "pending" until the
    next key, which the TUI mirrors with an indicator (leader_pending).
    """

    def __init__(self, bindings: Optional[Dict[str, List[Chord]]] = None,
                 warnings: Optional[Sequence[str]] = None):
        source = default_bindings() if bindings is None else bindings
        self._bindings: Dict[str, List[Chord]] = {
            name: list(chords) for name, chords in source.items()
        }
        self.warnings: List[str] = list(warnings or ())
        self.leader_pending = False

    @classmethod
    def default(cls) -> "Keymap":
        return cls()

    @classmethod
    def from_config(cls, config: Any) -> "Keymap":
        """
        Overlay config["keybinds"] on the defaults. Unknown names and values
        that yield no usable chord are collected in .warnings instead of
        raising -- a bad keybind must never stop the TUI from starting.
        """
        warnings: List[str] = []
        raw: Any = {}
        try:
            data = getattr(config, "data", config)
            if isinstance(data, dict):
                raw = data.get("keybinds", {}) or {}
        except Exception as exc:  # defensive: config objects are user data
            warnings.append("could not read keybinds: %s" % exc)
            raw = {}
        if not isinstance(raw, dict):
            warnings.append("keybinds must be an object, ignoring %s"
                            % type(raw).__name__)
            raw = {}

        for name in raw:
            if name not in DEFINITIONS:
                warnings.append("unknown keybind: %s" % name)

        bindings: Dict[str, List[Chord]] = {}
        for name, (default, _desc) in DEFINITIONS.items():
            if name in raw:
                value = raw[name]
                chords = parse_binding(value)
                if not chords and not is_disabled(value):
                    warnings.append("keybind %s: no usable key in %r"
                                    % (name, value))
                bindings[name] = chords
            else:
                bindings[name] = parse_binding(default)
        return cls(bindings, warnings)

    # -- introspection -----------------------------------------------------

    def bindings_for(self, command: str) -> List[Chord]:
        return list(self._bindings.get(command, ()))

    def leader_chords(self) -> List[Chord]:
        return list(self._bindings.get("leader", ()))

    def describe(self, command: str, expand_leader: bool = True) -> str:
        """
        Key string for help screens, e.g. "ctrl+c, ctrl+d, ctrl+x q".
        Empty when the command is unknown or disabled. With expand_leader the
        literal "<leader>" is replaced by whatever the leader is bound to,
        which is what a user actually has to press.
        """
        chords = self._bindings.get(command)
        if not chords:
            return ""
        return ", ".join(self._chord_text(chord, expand_leader) for chord in chords)

    def help_rows(self) -> List[Tuple[str, str]]:
        """(keys, description) for every bound command; leader first, then A-Z."""
        names = [name for name, chords in self._bindings.items() if chords]
        names.sort(key=lambda name: (name != "leader", name))
        return [(self.describe(name), DESCRIPTIONS.get(name, name)) for name in names]

    def _chord_text(self, chord: Chord, expand_leader: bool) -> str:
        if not chord.leader or not expand_leader:
            return chord.text
        leaders = self._bindings.get("leader") or []
        if not leaders:
            return chord.text
        body = Chord(key=chord.key, ctrl=chord.ctrl, alt=chord.alt,
                     shift=chord.shift).text
        return "%s %s" % (leaders[0].text, body)

    # -- resolution --------------------------------------------------------

    def reset(self) -> None:
        """Abandon a half-typed leader sequence (dialog opened, focus lost, ...)."""
        self.leader_pending = False

    def is_leader(self, event: Any) -> bool:
        return self._as_chord(event) in self._bindings.get("leader", ())

    def commands_for(self, event: Any, among: Optional[Iterable[str]] = None,
                     leader: bool = False) -> List[str]:
        """Every command bound to this chord, in DEFINITIONS (priority) order."""
        target = self._as_chord(event)
        allowed = None if among is None else set(among)
        found: List[str] = []
        for name, chords in self._bindings.items():
            if name == "leader":
                continue
            if allowed is not None and name not in allowed:
                continue
            for chord in chords:
                if chord.leader != leader:
                    continue
                if (chord.key == target.key and chord.ctrl == target.ctrl
                        and chord.alt == target.alt and chord.shift == target.shift):
                    found.append(name)
                    break
        return found

    def lookup(self, event: Any, among: Optional[Iterable[str]] = None) -> Optional[str]:
        """
        Command bound to this key, or None. Pressing the leader arms the
        sequence and returns None; the next key either completes a <leader>
        binding or cancels cleanly, so no key is ever swallowed twice.
        """
        if self.leader_pending:
            if self.is_leader(event):
                return None  # leader pressed twice: stay armed
            self.leader_pending = False
            found = self.commands_for(event, among, leader=True)
            return found[0] if found else None
        if self.is_leader(event):
            self.leader_pending = True
            return None
        found = self.commands_for(event, among)
        return found[0] if found else None

    def _as_chord(self, event: Any) -> Chord:
        if isinstance(event, Chord):
            return Chord(key=event.key, ctrl=event.ctrl, alt=event.alt,
                         shift=event.shift)
        if isinstance(event, KeyEvent):
            return event.chord()
        if isinstance(event, str):
            return KeyEvent(key=event).chord()
        raise TypeError("expected KeyEvent, Chord or str, got %r" % type(event).__name__)


# -- curses bridge --------------------------------------------------------

# ncurses values, used when curses cannot be imported (no terminal, or the
# module is missing) so this file stays importable anywhere.
_FALLBACK_CODES: Dict[str, int] = {
    "KEY_DOWN": 258, "KEY_UP": 259, "KEY_LEFT": 260, "KEY_RIGHT": 261,
    "KEY_HOME": 262, "KEY_BACKSPACE": 263, "KEY_F0": 264, "KEY_DC": 330,
    "KEY_IC": 331, "KEY_SF": 336, "KEY_SR": 337, "KEY_NPAGE": 338,
    "KEY_PPAGE": 339, "KEY_ENTER": 343, "KEY_BTAB": 353, "KEY_END": 360,
    "KEY_SDC": 383, "KEY_SEND": 386, "KEY_SHOME": 391, "KEY_SLEFT": 393,
    "KEY_SNEXT": 396, "KEY_SPREVIOUS": 398, "KEY_SRIGHT": 402,
    "KEY_MOUSE": 409, "KEY_RESIZE": 410,
}

_PLAIN_KEYS = {
    "KEY_DOWN": "down", "KEY_UP": "up", "KEY_LEFT": "left", "KEY_RIGHT": "right",
    "KEY_HOME": "home", "KEY_END": "end", "KEY_PPAGE": "pageup",
    "KEY_NPAGE": "pagedown", "KEY_BACKSPACE": "backspace", "KEY_DC": "delete",
    "KEY_IC": "insert", "KEY_ENTER": "return", "KEY_RESIZE": "resize",
    "KEY_MOUSE": "mouse",
}

_SHIFT_KEYS = {
    "KEY_BTAB": "tab", "KEY_SLEFT": "left", "KEY_SRIGHT": "right",
    "KEY_SHOME": "home", "KEY_SEND": "end", "KEY_SPREVIOUS": "pageup",
    "KEY_SNEXT": "pagedown", "KEY_SDC": "delete", "KEY_SF": "down",
    "KEY_SR": "up",
}

_code_cache: Optional[Dict[int, Tuple[str, bool]]] = None


def _key_codes(curses_module: Any = None) -> Dict[int, Tuple[str, bool]]:
    """code -> (key name, shift). Built lazily so importing needs no terminal."""
    global _code_cache
    explicit = curses_module is not None
    if not explicit:
        if _code_cache is not None:
            return _code_cache
        try:
            import curses as curses_module  # noqa: F811 - optional dependency
        except Exception:
            curses_module = None

    def code(name: str) -> Optional[int]:
        value = getattr(curses_module, name, None) if curses_module else None
        if not isinstance(value, int):
            value = _FALLBACK_CODES.get(name)
        return value

    table: Dict[int, Tuple[str, bool]] = {}
    for const, key in _PLAIN_KEYS.items():
        value = code(const)
        if value is not None:
            table.setdefault(value, (key, False))
    for const, key in _SHIFT_KEYS.items():
        value = code(const)
        if value is not None:
            table.setdefault(value, (key, True))
    base = code("KEY_F0")
    if base is not None:
        for number in range(1, 13):
            table.setdefault(base + number, ("f%d" % number, False))
            # ncurses reports shifted function keys as F13-F24.
            table.setdefault(base + 12 + number, ("f%d" % number, True))
    if not explicit:
        _code_cache = table
    return table


def from_curses(ch: Any, curses_module: Any = None,
                newline_is_enter: bool = True) -> KeyEvent:
    """
    Map one getch()/get_wch() value to a KeyEvent.

    Accepts an int, a one-character string, or an (27, key) pair / "\\x1b?"
    string for the ESC-prefixed sequences a terminal sends for alt+key. With
    curses.meta(True) the high-bit form (128+code) is handled too.

    newline_is_enter keeps 10 meaning Return, which is right unless the TUI
    called curses.nonl(); pass False there so ctrl+j reaches input_newline.
    """
    if isinstance(ch, (list, tuple)):
        if len(ch) == 2 and _as_code(ch[0]) == 27:
            return _with_alt(from_curses(ch[1], curses_module, newline_is_enter))
        if len(ch) == 1:
            return from_curses(ch[0], curses_module, newline_is_enter)
        return KeyEvent(key="")

    if isinstance(ch, str):
        if len(ch) > 1 and ch[0] == "\x1b":
            return _with_alt(from_curses(ch[1:], curses_module, newline_is_enter))
        if len(ch) != 1:
            return KeyEvent(key="")
        # get_wch() returns real characters as str, so anything above ASCII is
        # literal text -- only an int can be the high-bit meta form. Without
        # this, typing "ae" would arrive as alt+f (= input_word_forward).
        if ord(ch) >= 128:
            key, shift = normalise_key(ch)
            return KeyEvent(key=key, shift=shift)
        ch = ord(ch)

    if not isinstance(ch, int) or ch < 0:
        return KeyEvent(key="")

    if ch == 0:
        return KeyEvent(key="space", ctrl=True)
    if ch == 9:
        return KeyEvent(key="tab")
    if ch == 13 or (ch == 10 and newline_is_enter):
        return KeyEvent(key="return")
    if ch == 27:
        return KeyEvent(key="escape")
    if ch in (8, 127):
        return KeyEvent(key="backspace")
    if 1 <= ch <= 26:
        return KeyEvent(key=chr(ch + 96), ctrl=True)
    if 28 <= ch <= 31:
        # ctrl+\ ctrl+] ctrl+^ ctrl+_
        return KeyEvent(key=chr(ch + 64).lower(), ctrl=True)
    if ch == 32:
        return KeyEvent(key="space")
    if 33 <= ch <= 126:
        key, shift = normalise_key(chr(ch))
        return KeyEvent(key=key, shift=shift)
    if 128 <= ch <= 255:
        return _with_alt(from_curses(ch - 128, curses_module, newline_is_enter))

    # Above 255 every value is a curses KEY_* constant, never a character.
    entry = _key_codes(curses_module).get(ch)
    if entry is not None:
        return KeyEvent(key=entry[0], shift=entry[1])
    return KeyEvent(key="")


def _as_code(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and len(value) == 1:
        return ord(value)
    return None


def _with_alt(event: KeyEvent) -> KeyEvent:
    if not event.key:
        return event
    return KeyEvent(key=event.key, ctrl=event.ctrl, alt=True, shift=event.shift)
