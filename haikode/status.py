"""
One source of truth for "what is set up".

The home screen, the /status command and doctor all have to answer the same
question — which provider, which model, is it authenticated, what may it do in
this directory — so the answer is gathered once, here, as data. The three call
sites then only format it, and cannot drift apart.

This module is deliberately pure: no curses, no printing, and every probe
degrades to a safe default instead of raising, because collect() runs while the
UI is drawing and a missing git binary must never take the screen down with it.
"""

import os
import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from .context import INSTRUCTION_FILES, global_config_dir
from .permission import ALLOW, ASK, DEFAULTS, DENY, Permissions
from .tool import REGISTRY, Tool, get_tools

GIT_TIMEOUT = 3            # seconds; a hung git must not stall the home screen
SESSION_SCAN_LIMIT = 500   # the count is decoration, so bound the query
LABEL_WIDTH = 14           # widest label in detail_lines is "Instructions:"
ELLIPSIS = "..."
SUMMARY_KEY_LIMIT = 3      # permission keys named in a summary line before "+n"


@dataclass
class SetupInfo:
    """Everything the UIs report about the current setup. Never partial:
    fields that could not be probed hold their safe default."""

    provider: str = ""
    model: str = ""
    auth: str = "unknown"
    auth_ok: bool = False
    cwd: str = ""
    cwd_label: str = ""
    git_branch: str = ""
    tool_count: int = 0
    tool_names: List[str] = field(default_factory=list)
    ask_tools: List[str] = field(default_factory=list)
    allow_tools: List[str] = field(default_factory=list)
    deny_tools: List[str] = field(default_factory=list)
    config_path: str = ""
    keystore: str = ""
    instructions_files: List[str] = field(default_factory=list)
    session_count: int = 0
    python_version: str = field(default_factory=platform.python_version)
    platform_name: str = field(default_factory=platform.system)
    haiku: bool = False


# --------------------------------------------------------------------------
# small pure helpers (shared with the front-ends)
# --------------------------------------------------------------------------


def home_relative(path: str) -> str:
    """~/project instead of /boot/home/project: shorter, and safe to show."""
    if not path:
        return ""
    home = os.path.expanduser("~")
    if home and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def short_label(path: str, limit: int = 30) -> str:
    """A directory name for a status line: ~-relative, basename if still long."""
    label = home_relative(path)
    if len(label) <= limit:
        return label
    return os.path.basename(path.rstrip(os.sep)) or label


def truncate(text: str, width: int) -> str:
    """Cut to `width`, preferring the last word boundary so a line never ends
    mid-word. The ellipsis is included in the budget: the result always fits."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= len(ELLIPSIS):
        return text[:width]
    room = width - len(ELLIPSIS)
    head = text[:room]
    space = head.rfind(" ")
    # Only honour a boundary that still leaves most of the line readable.
    if space >= room // 2:
        head = head[:space]
    # A trailing separator before the ellipsis reads as a missing word.
    return head.rstrip(" ,;:·|-") + ELLIPSIS


def effective_policy(config, key: str, permissions: Optional[Permissions] = None) -> str:
    """The decision an unprompted request for `key` would get right now.

    Resolution is delegated to Permissions so this report cannot drift from
    what the tools actually enforce. Probing with "*" makes a per-pattern rule
    answer with its catch-all entry, which is the policy an unseen command
    meets; a dict of only specific patterns therefore reports the default.
    """
    lookup = permissions if permissions is not None else Permissions(config=config)
    try:
        decision = lookup._configured(key, "*")
    except Exception:
        decision = None
    if decision not in (ALLOW, ASK, DENY):
        decision = DEFAULTS.get(key, ASK)
    return decision


# --------------------------------------------------------------------------
# probes: each one returns a safe default rather than raising
# --------------------------------------------------------------------------


def _git_branch(cwd: str) -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                cwd=cwd, capture_output=True, text=True,
                                timeout=GIT_TIMEOUT)
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _instruction_files(cwd: str) -> List[str]:
    """The files ContextManager.instructions() would pick up, existence only.

    Reading them just to say whether they are there would cost the home screen
    the whole instruction budget in IO before the first frame.
    """
    found: List[str] = []
    try:
        start = Path(cwd).resolve()
    except OSError:
        return found

    relevant: List[Path] = []
    for directory in [start, *start.parents]:
        relevant.append(directory)
        if (directory / ".git").exists():
            break  # the repo root is where the search stops

    for directory in reversed(relevant):
        for name in INSTRUCTION_FILES:
            path = directory / name
            try:
                if path.is_file():
                    found.append(str(path))
            except OSError:
                continue
    try:
        global_path = global_config_dir() / "AGENTS.md"
        if global_path.is_file():
            found.insert(0, str(global_path))
    except OSError:
        pass
    return found


def _session_count() -> int:
    """0 when the store is unavailable — sqlite3 is not guaranteed on Haiku."""
    try:
        from . import session as session_module
        store = session_module.SessionStore()
        return len(store.list_sessions(SESSION_SCAN_LIMIT))
    except Exception:
        return 0


def _keystore_path() -> str:
    """The native helper's path, or "" when keys fall back to the config file."""
    try:
        from .config import _keystore_bin
        return _keystore_bin() or ""
    except Exception:
        return ""


def _auth(config, name: str, provider: Dict[str, Any]) -> Tuple[str, bool]:
    """A human sentence plus "is this usable right now"."""
    if not provider:
        return "unknown provider", False
    try:
        source = config.key_source(name)
    except Exception:
        return "auth check failed", False

    if provider.get("oauth_provider"):
        signed_in = source not in ("none", "")
        return ("oauth: signed in" if signed_in else "oauth: not signed in"), signed_in
    if source == "keystore":
        return "key from keystore", True
    if source == "config":
        return "key from config file", True
    if source == "env":
        env = provider.get("key_env", "")
        return (f"key from ${env}" if env else "key from environment"), True
    if source == "n/a":
        return "no key required", True
    return "no key set", False


def _resolve_tools(tools) -> Dict[str, Tool]:
    """Accept what each caller happens to hold: the registry, a name list, or
    the dict get_tools() returns."""
    if tools is None:
        return dict(REGISTRY)
    if isinstance(tools, dict):
        return dict(tools)
    try:
        return get_tools([str(name) for name in tools])
    except TypeError:
        return dict(REGISTRY)


def _is_haiku() -> bool:
    try:
        return platform.system() == "Haiku" or os.path.isdir("/boot/home")
    except OSError:
        return False


# --------------------------------------------------------------------------
# collection
# --------------------------------------------------------------------------


def collect(config, provider_name: Optional[str] = None, cwd: str = ".",
            tools: Union[None, Dict[str, Tool], Iterable[str]] = None) -> SetupInfo:
    """Gather the current setup. Raises nothing: every field degrades."""
    data = getattr(config, "data", None) or {}
    providers = data.get("providers") or {}
    name = (provider_name or data.get("default_provider", "")
            or next(iter(providers), ""))
    provider = providers.get(name) or {}

    try:
        resolved_cwd = str(Path(cwd).resolve())
    except OSError:
        resolved_cwd = str(cwd)

    selected = _resolve_tools(tools)
    auth, auth_ok = _auth(config, name, provider)

    # Everything with a policy: the built-in defaults, whatever the user
    # configured (including keys we have no tool for) and the active tools.
    keys = set(DEFAULTS)
    rules = data.get("permission")
    if isinstance(rules, dict):
        keys.update(key for key in rules if isinstance(key, str))
    for tool in selected.values():
        key = getattr(tool, "permission", "")
        if key:
            keys.add(key)

    permissions = Permissions(config=config)
    buckets: Dict[str, List[str]] = {ALLOW: [], ASK: [], DENY: []}
    for key in sorted(keys):
        buckets.setdefault(effective_policy(config, key, permissions), []).append(key)

    return SetupInfo(
        provider=name,
        model=str(provider.get("model", "") or ""),
        auth=auth,
        auth_ok=auth_ok,
        cwd=resolved_cwd,
        cwd_label=short_label(resolved_cwd),
        git_branch=_git_branch(resolved_cwd),
        tool_count=len(selected),
        tool_names=sorted(selected),
        ask_tools=buckets[ASK],
        allow_tools=buckets[ALLOW],
        deny_tools=buckets[DENY],
        config_path=str(getattr(config, "path", "") or ""),
        keystore=_keystore_path(),
        instructions_files=_instruction_files(resolved_cwd),
        session_count=_session_count(),
        haiku=_is_haiku(),
    )


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def _plural(count: int, word: str) -> str:
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def _join_keys(keys: List[str], limit: int = SUMMARY_KEY_LIMIT) -> str:
    shown = ", ".join(keys[:limit])
    extra = len(keys) - limit
    return f"{shown} +{extra}" if extra > 0 else shown


def summary_lines(info: SetupInfo, width: int = 60,
                  unicode_ok: bool = True) -> List[Tuple[str, str]]:
    """Three to five (text, style) pairs for the home screen.

    style is "info" | "muted" | "warn". unicode_ok is passed through from the
    TUI's glyph set: the separators are decorative, and a vt100 session would
    otherwise show them as question marks.
    """
    sep = " · " if unicode_ok else " | "
    dash = " — " if unicode_ok else " - "
    lines: List[Tuple[str, str]] = []

    place = [info.cwd_label or info.cwd or "."]
    if info.git_branch:
        place.append(info.git_branch)
    lines.append((sep.join(place), "muted"))

    lines.append((sep.join([part for part in (info.provider, info.model, info.auth)
                            if part]) or "no provider configured", "info"))

    if not info.auth_ok:
        target = info.provider or "your provider"
        problem = (f"not signed in to {target}" if info.auth.startswith("oauth")
                   else f"no key for {target}")
        lines.append((f"{problem}{dash}run /login {target}", "warn"))

    tools = [_plural(info.tool_count, "tool")]
    if info.ask_tools:
        tools.append(_join_keys(info.ask_tools) + " ask first")
    if info.deny_tools:
        tools.append(_join_keys(info.deny_tools, 2) + " denied")
    lines.append((sep.join(tools), "muted"))

    extras = []
    if info.instructions_files:
        names = sorted({os.path.basename(path) for path in info.instructions_files})
        extras.append(", ".join(names) + " loaded")
    if info.session_count:
        extras.append(_plural(info.session_count, "session"))
    if extras:
        lines.append((sep.join(extras), "muted"))

    return [(truncate(text, width), style) for text, style in lines]


def _row(label: str, value: str) -> str:
    return "%-*s %s" % (LABEL_WIDTH, (label + ":") if label else "", value)


def detail_lines(info: SetupInfo) -> List[str]:
    """The full report behind /status, one field per line."""
    rows = [
        _row("Provider", info.provider or "(none configured)"),
        _row("Model", info.model or "(unset)"),
        _row("Auth", info.auth),
        _row("Config", home_relative(info.config_path) or "(none)"),
        _row("Keystore", home_relative(info.keystore) or
             "not installed (keys go to the config file)"),
        _row("Directory", home_relative(info.cwd) or "(unknown)"),
        _row("Branch", info.git_branch or "(not a git repository)"),
        _row("Instructions",
             ", ".join(home_relative(path) for path in info.instructions_files)
             or "(none found)"),
        _row("Tools (%d)" % info.tool_count, ", ".join(info.tool_names) or "(none)"),
    ]

    label = "Permissions"
    for policy, keys in ((ALLOW, info.allow_tools), (ASK, info.ask_tools),
                         (DENY, info.deny_tools)):
        if not keys:
            continue
        rows.append(_row(label, "%s: %s" % (policy, ", ".join(keys))))
        label = ""  # continuation lines sit under the first
    if label:
        rows.append(_row("Permissions", "(none)"))

    rows.append(_row("Sessions", str(info.session_count)))
    rows.append(_row("Python", "%s on %s" % (
        info.python_version, "Haiku" if info.haiku else (info.platform_name or "unknown"))))
    return rows


# --------------------------------------------------------------------------
# the sign-off: a haiku on the way out
# --------------------------------------------------------------------------

# The farewell collection: the three Edo masters in this project's own
# renderings (their originals are long free; published translations are
# not, so these are ours), and botfred's contributions, 2026. One flat
# collection, drawn from blindly — a poem about thirty-two bits on a
# 64-bit machine is a charm, not a bug (the user ruled).
# Entries are (line, line, line, attribution).

HAIKU_MASTERS = (
    # Bashō (1644-1694)
    ("old pond, still water", "a frog leaps into the dark", "the sound of water", "Bashō"),
    ("on a bare branch now", "a crow has come down to rest", "autumn's nightfall", "Bashō"),
    ("summer grasses grow", "all that remains of the dreams", "of the warriors", "Bashō"),
    ("the first soft snowfall", "just enough to bend the leaves", "of the daffodil", "Bashō"),
    ("sick on a journey", "my dreams keep wandering on", "over withered fields", "Bashō"),
    ("stillness everywhere", "sinking into the stones", "the cicada's cry", "Bashō"),
    # Buson (1716-1784)
    ("spring's slow ocean waves", "rising and falling all day", "rising and falling", "Buson"),
    ("a sudden chill, then", "in our bedroom, stepping on", "my dead wife's comb", "Buson"),
    ("the piercing winter wind", "has no place at all to sleep", "over the graveyard", "Buson"),
    ("plum blossoms falling", "and the old garden lantern", "lights itself again", "Buson"),
    ("winter solitude —", "in a world of one colour", "the sound of the wind", "Buson"),
    ("evening river breeze", "the water laps against", "the heron's thin legs", "Buson"),
    # Issa (1763-1828)
    ("little snail, so slow", "climb up, climb up Mount Fuji", "but take your own time", "Issa"),
    ("this dewdrop world is", "a dewdrop world, and yet — and", "yet, it is, it is", "Issa"),
    ("do not swat the fly", "see how it wrings its small hands", "wrings its small feet too", "Issa"),
    ("the moon and flowers", "forty-nine years of walking", "wasting time, at night", "Issa"),
    ("a good world it is", "the dew still drips from the leaves", "one drop at a time", "Issa"),
)

FAREWELL_HAIKU = HAIKU_MASTERS + (
    ("old machines still think", "thirty-two bits are enough", "for one good idea", "botfred, 2026"),
    ("a netbook wakes up", "carrying its whole address space", "lightly, up the hill", "botfred, 2026"),
    ("small atom, slow clock", "yet the answer still arrives", "patience is a core", "botfred, 2026"),
    ("compile, test, deploy", "the kernel hums its one song", "be, and be again", "botfred, 2026"),
    ("yellow tab sunset", "the tracker folds its windows", "media kit sleeps", "botfred, 2026"),
    ("one team at a time", "the deskbar holds the evening", "replicants at rest", "botfred, 2026"),
    ("silent threads wind down", "the deskbar clock keeps ticking", "your code is at rest", "botfred, 2026"),
    ("tokens ebb away", "context folded into sleep", "the model dreams on", "botfred, 2026"),
    ("a green LED fades", "somewhere a server exhales", "sessions saved to disk", "botfred, 2026"),
    ("the cursor blinks twice", "everything you typed remains", "nothing has been lost", "botfred, 2026"),
    ("packets cross the night", "an answer finds its way home", "the prompt waits for you", "botfred, 2026"),
    ("autumn of the disk", "each block flushed before the dark", "morning finds them whole", "botfred, 2026"),
    ("one more quiet diff", "reviewed by the evening light", "merged without a sound", "botfred, 2026"),
    ("the tests all went green", "somewhere a cursor exhales", "ship it in the dawn", "botfred, 2026"),
)



# Alias kept so callers and tests have one obvious name for "everything".
ALL_FAREWELL_HAIKU = FAREWELL_HAIKU


def validated_haiku(text: str):
    """Three plausible haiku lines from model output, or None.

    The model was told "three lines only", and models embellish anyway —
    a preamble, quotes, a fourth line. Anything that does not reduce to
    exactly three short lines is discarded rather than repaired: the
    curated collection is always available, and a mangled poem at the
    very last moment of a session is worse than a familiar one.
    """
    lines = [line.strip().strip('"').strip()
             for line in str(text or "").splitlines()]
    lines = [line for line in lines if line]
    if len(lines) != 3:
        return None
    if any(len(line) > 60 for line in lines):
        return None
    return tuple(lines)


def validated_title(text: str) -> str:
    """A plausible 3-5 word display title from model output, or ""."""
    line = " ".join(str(text or "").split())
    line = line.strip().strip('"').strip("'").rstrip(".!").strip()
    words = line.split()
    if not 2 <= len(words) <= 7 or len(line) > 48:
        return ""
    return line


def terminal_title(title: str) -> str:
    """The xterm OSC sequence that names the terminal tab.

    Haiku's Terminal honours it like xterm does. Emitted around curses by
    writing straight to the tty — the sequence addresses the emulator, not
    the screen buffer, so curses never notices.
    """
    clean = " ".join(str(title or "").split())[:64]
    return "\x1b]0;%s\x07" % clean.replace("\x1b", "").replace("\x07", "")


def startup_haiku():
    """One attributed poem from the collection, for the home screen."""
    import random
    first, second, third, author = random.choice(FAREWELL_HAIKU)
    return (first, second, third, "— %s" % author)


def farewell(session_id: str = "", poem=None, poet: str = "") -> str:
    """The exit message: one attributed haiku, and the way back in.

    `poem` is this session's model-written haiku when the background
    composer got one in time (turn.py), attributed to `poet` — the model
    that wrote it. The curated collection answers otherwise, so quitting
    never waits on anything.

    The resume line prints the full session id on purpose — the ids are
    time-prefixed, so every id from the same era shares its first eight
    characters and a shortened form would only ever be ambiguous.
    """
    import random
    chosen = validated_haiku("\n".join(poem)) if poem else None
    if chosen is not None:
        first, second, third = chosen
        author = poet or "the model"
    else:
        first, second, third, author = random.choice(FAREWELL_HAIKU)
    lines = ["", "  %s" % first, "  %s" % second, "  %s" % third,
             "%s— %s" % (" " * 8, author), ""]
    if session_id:
        lines.append("resume this session:  haikode -s %s" % session_id)
    return "\n".join(lines)
