"""
Haiku desktop integration: notifications, BFS attributes, Tracker, alerts.

haikode is a terminal program, but on Haiku a terminal program is still
expected to behave like a Haiku application. A run that took four minutes
should raise a notification instead of a terminal bell; an exported session
should carry queryable BFS attributes instead of a filename you have to
remember; "show me that file" should open Tracker.

Everything here shells out to the base-system command line tools (`notify`,
`addattr`, `listattr`, `catattr`, `copyattr`, `mkindex`, `open`, `alert`)
rather than binding libbe, because haikode is stdlib-only Python and those
tools ship with every Haiku install. The flags were read off a live Haiku
hrev57937 machine, not guessed.

Two rules hold for every entry point in this module:

  * off Haiku it is a silent no-op returning False / {} / "", so the macOS
    development loop and the test suite never shell out to anything;
  * a missing, failing or hanging tool returns False rather than raising.
    Desktop integration is decoration and must never take a run down with it,
    so every subprocess call carries a timeout.
"""

import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import (Any, Dict, Iterable, List, Mapping, Optional, Sequence,
                    Set, Tuple, Union)

COMMAND_TIMEOUT = 5       # seconds; every helper here is best-effort decoration
ALERT_TIMEOUT = 600       # a human has to click, but we still refuse to hang forever

# `notify --type` accepts exactly these; anything else makes the tool complain.
NOTIFY_KINDS = ("information", "important", "error", "progress")
DEFAULT_KIND = "information"
DEFAULT_GROUP = "haikode"

# `alert <type>` accepts exactly these.
ALERT_KINDS = ("empty", "info", "idea", "warning", "stop")
DEFAULT_ALERT_KIND = "info"

ATTR_NAMESPACE = "haikode:"

# The BFS attributes written onto a session file, with the mkindex type needed
# to make each one answerable by a Tracker query. B_TIME_TYPE is eight bytes,
# which mkindex calls "llong".
QUERY_INDICES: Tuple[Tuple[str, str], ...] = (
    ("haikode:title", "string"),
    ("haikode:provider", "string"),
    ("haikode:model", "string"),
    ("haikode:cwd", "string"),
    ("haikode:updated", "llong"),
)
SESSION_ATTRS: Tuple[str, ...] = tuple(name for name, _ in QUERY_INDICES)

INT32_MIN = -(2 ** 31)
INT32_MAX = 2 ** 31 - 1

PathLike = Union[str, "os.PathLike[str]", Path]

# Volumes whose haikode:* indices we have already tried to create this process.
# mkindex is cheap but it is still five subprocesses, and it only ever has to
# succeed once per volume.
_INDEXED: Set[str] = set()


# --------------------------------------------------------------------------
# platform detection
# --------------------------------------------------------------------------


def is_haiku() -> bool:
    """True only on a real Haiku install.

    Deliberately not cached: the tests patch both probes to exercise the on-
    and off-Haiku branches, and two syscalls are free next to the subprocess
    every caller is about to spawn.
    """
    try:
        return platform.system() == "Haiku" and os.path.isdir("/boot/home")
    except OSError:
        return False


def _interactive() -> bool:
    """Whether a human is plausibly sitting at this terminal."""
    try:
        stream = sys.stdin
        return stream is not None and stream.isatty()
    except (AttributeError, ValueError, OSError):
        return False


def _run(argv: Sequence[str],
         timeout: float = COMMAND_TIMEOUT) -> Optional[subprocess.CompletedProcess]:
    """Run a Haiku command line tool.

    Returns None when the tool is missing, cannot be started, or overran its
    timeout; callers read that as "no desktop here" and carry on.
    """
    try:
        return subprocess.run(list(argv), timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _ok(proc: Optional[subprocess.CompletedProcess]) -> bool:
    return proc is not None and proc.returncode == 0


def _text(blob: Any) -> str:
    if isinstance(blob, bytes):
        return blob.decode("utf-8", "replace")
    return "" if blob is None else str(blob)


# --------------------------------------------------------------------------
# notifications
# --------------------------------------------------------------------------


def notify(title: str, message: str, kind: str = DEFAULT_KIND,
           group: str = DEFAULT_GROUP, id: str = "") -> bool:
    """Raise a Haiku desktop notification. Returns True when it was shown.

    Use this when a long run finishes while the user has switched away from
    the Terminal. `id` is the notification's message ID: reusing the same ID
    replaces the previous notification instead of stacking a new one, which is
    what you want for progress on a single run.
    """
    if not is_haiku():
        return False
    if kind not in NOTIFY_KINDS:
        kind = DEFAULT_KIND
    argv = ["notify",
            "--type", kind,
            "--group", str(group or DEFAULT_GROUP),
            "--title", str(title)]
    if id:
        argv += ["--messageID", str(id)]
    argv.append(str(message))
    return _ok(_run(argv))


# --------------------------------------------------------------------------
# BFS extended attributes
# --------------------------------------------------------------------------


class Timestamp:
    """Marks a value as a BFS `B_TIME_TYPE` attribute.

    Needed because `addattr -t time` parses a human date string
    ("2026-07-27 01:00:00") but silently stores the *current* time when handed
    a raw epoch number — verified on hrev57937. The conversion therefore has
    to happen here, not in the caller.
    """

    __slots__ = ("epoch",)

    def __init__(self, epoch: float):
        try:
            self.epoch = float(epoch)
        except (TypeError, ValueError):
            self.epoch = 0.0

    def format(self) -> str:
        """The local-time string `addattr -t time` understands."""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.epoch))

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, Timestamp):
            return self.epoch == other.epoch
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.epoch)

    def __repr__(self) -> str:
        return "Timestamp(%r)" % self.epoch


def _attr_type(value: Any) -> Tuple[str, str]:
    """Map a Python value onto an `addattr -t` type code and its text form.

    Types matter: an attribute stored as a string sorts and queries as a
    string in Tracker, so a count written as "42" would order before "9".
    """
    if isinstance(value, Timestamp):
        return "time", value.format()
    if isinstance(value, bool):           # before int: bool is an int subclass
        return "bool", "1" if value else "0"
    if isinstance(value, int):
        code = "int" if INT32_MIN <= value <= INT32_MAX else "int64"
        return code, str(value)
    if isinstance(value, float):
        return "double", repr(value)
    return "string", "" if value is None else str(value)


def set_attributes(path: PathLike, attrs: Mapping[str, Any]) -> bool:
    """Write BFS extended attributes onto `path`. True only if all were set."""
    if not is_haiku() or not attrs:
        return False
    target = os.fspath(path)
    ok = True
    for name, value in attrs.items():
        code, text = _attr_type(value)
        if not _ok(_run(["addattr", "-t", code, str(name), text, target])):
            ok = False
    return ok


# `listattr` prints one row per attribute: type label, byte size, quoted name.
# The header, the rule and the trailing total have no quoted name, so they
# simply do not match.
_ATTR_ROW = re.compile(r'^\s*(?P<type>\S.*?)\s+(?P<size>\d+)\s+"(?P<name>.+)"\s*$')

# `catattr -d` prints readable text for string and integer attributes but falls
# back to a hex dump for types it cannot render, such as B_TIME_TYPE.
_HEX_DUMP = re.compile(r'^\s*0x[0-9a-fA-F]+:\s+(?P<bytes>(?:[0-9a-fA-F]{2}\s*)+)')


def _decode_hex(raw: str) -> Optional[int]:
    """Little-endian integer out of a `catattr` hex dump, or None."""
    chunks = []
    for line in raw.splitlines():
        match = _HEX_DUMP.match(line)
        if match:
            chunks.extend(match.group("bytes").split())
    if not chunks:
        return None
    try:
        data = bytes(int(byte, 16) for byte in chunks)
    except ValueError:
        return None
    return int.from_bytes(data, "little", signed=False)


def _decode(type_label: str, raw: str) -> Any:
    """Turn one `catattr -d` payload into a Python value using listattr's type."""
    label = type_label.strip().strip("'")
    if label == "TIME":
        decoded = _decode_hex(raw)
        return decoded if decoded is not None else raw.strip()
    # listattr spells B_BOOL_TYPE "Boolean", not "Bool" — verified on
    # hrev57937. Matching only "Bool" let every bool come back as the string
    # "1", which is truthy but is not True and does not compare equal to it.
    if label in ("Bool", "Boolean"):
        decoded = _decode_hex(raw)
        if decoded is not None:
            return bool(decoded)
        return raw.strip() not in ("", "0", "false")
    if label.lower().startswith(("int", "uint")):
        try:
            return int(raw.strip())
        except ValueError:
            decoded = _decode_hex(raw)
            return decoded if decoded is not None else raw.strip()
    if label in ("Float", "Double"):
        try:
            return float(raw.strip())
        except ValueError:
            return raw.strip()
    # Text, MIME strings and anything else: keep the bytes as written, minus
    # the newline catattr adds.
    return raw[:-1] if raw.endswith("\n") else raw


def list_attributes(path: PathLike) -> List[Tuple[str, str]]:
    """Every BFS attribute of `path` as (name, listattr type label).

    [] off Haiku, on a missing file, or when the file simply has none — the
    caller cannot tell those apart and does not need to, because all three
    mean "nothing to copy".
    """
    if not is_haiku():
        return []
    listing = _run(["listattr", os.fspath(path)])
    if not _ok(listing):
        return []
    found: List[Tuple[str, str]] = []
    for line in _text(listing.stdout).splitlines():
        match = _ATTR_ROW.match(line)
        if match:
            found.append((match.group("name"), match.group("type")))
    return found


def read_attribute(path: PathLike, name: str) -> Optional[bytes]:
    """The raw bytes of one BFS attribute, or None.

    `catattr --raw` writes the stored bytes with no formatting, which is the
    only way to see a typed attribute exactly as BFS holds it; `catattr -d`
    renders an Int-32 as text and a B_TIME_TYPE as a hex dump.
    """
    if not is_haiku():
        return None
    proc = _run(["catattr", "--raw", str(name), os.fspath(path)])
    if not _ok(proc):
        return None
    blob = proc.stdout
    return blob if isinstance(blob, bytes) else str(blob).encode("utf-8", "replace")


def copy_attributes(path: PathLike, target: PathLike,
                    names: Optional[Sequence[str]] = None) -> bool:
    """Copy BFS extended attributes from `path` onto `target`.

    This is what keeps a file's identity alive across an atomic replace. On
    BFS the MIME type (`BEOS:TYPE`), the Tracker metadata and every custom
    indexed attribute live on the *inode*, so writing a new inode and renaming
    it over the old one silently strips all of them.

    `copyattr` is the only faithful route, and the obvious alternative is a
    trap: reading each value with `catattr --raw` and writing it back with
    `addattr -f` destroys typed attributes, because addattr re-parses the file
    it is handed as *text* for every type except string, mime and raw. On
    hrev57937 an Int-32 of 42 came back as 0, an Int-64 and a Double as 0, and
    B_BOOL_TYPE and B_TIME_TYPE failed outright. copyattr carries the type
    code and the bytes across untouched.

    Without `-d` copyattr copies attributes only and never the file's data, so
    it is safe to aim at a temp file that already holds the new contents.
    Returns True only if every requested copy succeeded; False off Haiku.
    """
    if not is_haiku():
        return False
    source, dest = os.fspath(path), os.fspath(target)
    if names is None:
        # A source with no attributes at all still exits 0, so "nothing to
        # copy" reports success rather than a failure the caller must ignore.
        return _ok(_run(["copyattr", "--", source, dest]))
    ok = True
    for name in names:
        if not _ok(_run(["copyattr", "-n", str(name), "--", source, dest])):
            ok = False
    return ok


def get_attributes(path: PathLike) -> Dict[str, Any]:
    """Read every BFS extended attribute of `path`, typed. {} when unavailable.

    Two tools are needed: `listattr` names the attributes and their types,
    `catattr` produces each value. listattr's own `--long` output cannot be
    parsed reliably because a multi-line string attribute wraps into the
    column layout.
    """
    if not is_haiku():
        return {}
    target = os.fspath(path)
    found: Dict[str, Any] = {}
    for name, type_label in list_attributes(target):
        value = _run(["catattr", "-d", name, target])
        if not _ok(value):
            continue
        found[name] = _decode(type_label, _text(value.stdout))
    return found


def ensure_query_indices(path: Optional[PathLike] = None) -> bool:
    """Create the BFS indices the haikode:* Tracker queries need.

    BFS only answers a query for an *indexed* attribute — without this an
    attribute is visible in Tracker's info window but no query will ever find
    it. mkindex fails harmlessly when the index already exists, so the result
    is ignored; the per-volume cache just keeps it to one attempt per run.
    """
    if not is_haiku():
        return False
    volume = os.fspath(path) if path is not None else "/boot/home"
    if volume in _INDEXED:
        return True
    for name, kind in QUERY_INDICES:
        _run(["mkindex", "-t", kind, "-d", volume, name])
    _INDEXED.add(volume)
    return True


# --------------------------------------------------------------------------
# session tagging
# --------------------------------------------------------------------------


def session_attributes(session: Any) -> Dict[str, Any]:
    """The exact BFS attribute set written for a session. See tag_session_file.

    All five attributes are always present, empty ones included, so that the
    set is predictable enough to document and to query with a plain wildcard.
    """
    return {
        "haikode:title": str(getattr(session, "title", "") or ""),
        "haikode:provider": str(getattr(session, "provider", "") or ""),
        "haikode:model": str(getattr(session, "model", "") or ""),
        "haikode:cwd": str(getattr(session, "cwd", "") or ""),
        "haikode:updated": Timestamp(getattr(session, "updated", 0) or 0),
    }


def tag_session_file(path: PathLike, session: Any) -> bool:
    """Tag an exported session file so Tracker can find it.

    Writes `haikode:title`, `haikode:provider`, `haikode:model`, `haikode:cwd`
    (strings) and `haikode:updated` (B_TIME_TYPE), and makes sure the volume
    has the indices those queries need.

    From a Terminal, the matching query — verified on hrev57937 — is:

        query '((haikode:provider=="anthropic")&&(haikode:title=="*parser*"))'

    or, for everything haikode ever wrote on this volume:

        query 'haikode:updated>=%Y-%m-%d'   # any indexed attribute works
        query 'haikode:model=="*"'

    In Tracker the same thing is Find (Alt-F) on "All files and folders", then
    adding the attribute name `haikode:provider` to the criteria; saving that
    query gives a live folder of sessions.
    """
    if not is_haiku():
        return False
    target = Path(os.fspath(path))
    ensure_query_indices(target.parent)
    return set_attributes(target, session_attributes(session))


# --------------------------------------------------------------------------
# Tracker and the preferred application
# --------------------------------------------------------------------------


def open_in_tracker(path: PathLike) -> bool:
    """Reveal `path` in Tracker.

    Haiku's `open` hands a directory to Tracker, so a file is revealed by
    opening the folder that contains it — which is the "reveal in Tracker"
    action every desktop expects from a CLI.
    """
    if not is_haiku():
        return False
    target = Path(os.fspath(path))
    try:
        folder = target if target.is_dir() else target.parent
    except OSError:
        folder = target.parent
    return _ok(_run(["open", str(folder)]))


def open_with_preferred(path: PathLike) -> bool:
    """Open `path` with its preferred application, as double-clicking would."""
    if not is_haiku():
        return False
    return _ok(_run(["open", os.fspath(path)]))


# --------------------------------------------------------------------------
# alerts
# --------------------------------------------------------------------------


def alert(text: str, buttons: Iterable[str] = (),
          kind: str = DEFAULT_ALERT_KIND) -> str:
    """Ask a question with a real Haiku alert window; return the button label.

    Returns "" when the question could not be asked, so a caller can always
    fall back to a terminal prompt.

    NEVER call this from a non-interactive run. The alert opens a window on
    the machine's physical screen and blocks until somebody clicks it, so a
    cron job, a piped command, a test or an SSH session would leave a dialog
    stranded in front of a user who did not ask for one. The stdin-is-a-tty
    guard below enforces that mechanically, but the judgement is the caller's:
    reach for an alert only when the CLI genuinely needs a GUI answer and a
    human is known to be at the machine.
    """
    if not is_haiku() or not _interactive():
        return ""
    labels = [str(button) for button in buttons]
    argv = ["alert", "--%s" % (kind if kind in ALERT_KINDS else DEFAULT_ALERT_KIND),
            str(text)]
    # `alert` accepts at most three buttons; extra arguments are ignored, so
    # the slice keeps the argv honest about what the user will actually see.
    argv.extend(labels[:3])
    proc = _run(argv, timeout=ALERT_TIMEOUT)
    if proc is None:
        return ""
    return _text(proc.stdout).strip()
