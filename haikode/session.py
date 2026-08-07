"""
Session persistence with undo/revert.

opencode lets you rewind a session: you pick a point in the conversation, every
file the agent touched after that point goes back to how it was, and the
messages after it are dropped. opencode implements this with git snapshots;
Haiku installs cannot assume git exists, so we keep the pre-edit text of every
file a tool touched instead.

The model:
  * every message gets a monotonic `seq` within its session (1-based);
  * `checkpoint()` records the seq a run starts from — that is the revert point;
  * every file the run modifies is stored once per (session, revert point, path)
    together with its ORIGINAL content, NULL meaning "did not exist before";
  * `revert_to(seq)` writes those originals back — deleting the files whose
    original is NULL — and drops the messages after `seq`.

Snapshot rows are keyed by the revert point they belong to rather than by the
message that made the edit, so reverting to a point restores that point's
snapshots plus every later one. When several revert points touched the same
file, the earliest recorded original wins: that is the content the file had
before any of the reverted runs, which is what makes repeated reverts converge
on the true pre-run state instead of an intermediate one.

On top of that the store carries the session bookkeeping a UI needs: archiving
and per-project listing, full-text search, export, statistics, token
accounting, and in-place compaction that folds old turns into one summary
message without ever splitting a tool call from its result.

Compaction here is the persistent half of context.compact_messages: the same
plan_compaction() decides what folds, the same summarize() writes the summary,
and this module stores both — the summary as the message that replaces the
folded turns, and the folded turns themselves in the `compactions` table so
/undo can put them back. A resumed session therefore reads the summary back
from disk instead of starting from a hole.
"""

import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .context import (DEFAULT_TAIL_TURNS, MAX_KEEP_TOKENS, SUMMARY_MAX_TOKENS,
                      CompactionResult, global_config_dir, message_tokens,
                      DEFAULT_RESERVE, needs_compaction, plan_compaction,
                      summarize_with_reason)
from .palette import fuzzy_score
from .schema import Msg, ToolCall
from .tool.base import Tool, ToolResult

DEFAULT_DB_NAME = "sessions.db"
# Verified snapshots kept beside the store, newest first.
BACKUP_GENERATIONS = 3
# The desktop launches one worker process per Send. "Once per process" is
# therefore once per user turn there, and quick_check + sqlite backup scale
# with the whole database (100 ms for 50 MiB on the audit Mac, before Haiku's
# slower storage). Keep recent recovery points without putting a full copy on
# every turn's synchronous startup path.
BACKUP_MIN_INTERVAL = 300.0
# What /compact keeps verbatim when the user names no number.
DEFAULT_COMPACT_KEEP = 10
MAX_TITLE_CHARS = 60
SNIPPET_CHARS = 120
# A title hit describes the whole session, a body hit only one line of it, so
# titles are weighted up before the two are compared.
SEARCH_TITLE_WEIGHT = 2
SEARCH_TITLE_BONUS = 200
# Bound on the lines scanned per session so search stays interactive even when
# a session holds megabytes of tool output.
SEARCH_MAX_LINES = 4000

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL DEFAULT '',
        cwd TEXT NOT NULL DEFAULT '',
        provider TEXT NOT NULL DEFAULT '',
        model TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL DEFAULT 0,
        updated REAL NOT NULL DEFAULT 0,
        archived INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        session_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL DEFAULT '',
        tool_calls TEXT NOT NULL DEFAULT '[]',
        reasoning TEXT NOT NULL DEFAULT '{}',
        tool_call_id TEXT NOT NULL DEFAULT '',
        display TEXT NOT NULL DEFAULT '{}',
        created REAL,
        tokens INTEGER,
        PRIMARY KEY (session_id, seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshots (
        session_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        path TEXT NOT NULL,
        original TEXT,
        created REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (session_id, seq, path)
    )
    """,
    # Surrogate key rather than (session_id, seq): compacting twice can land on
    # the same seq, and the older record must survive so /undo can still see
    # what that round folded away.
    """
    CREATE TABLE IF NOT EXISTS compactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        created REAL NOT NULL DEFAULT 0,
        folded INTEGER NOT NULL DEFAULT 0,
        first_seq INTEGER NOT NULL DEFAULT 0,
        last_seq INTEGER NOT NULL DEFAULT 0,
        summary TEXT NOT NULL DEFAULT '',
        messages TEXT NOT NULL DEFAULT '[]'
    )
    """,
    # Automatic compaction must survive a process boundary: the desktop app
    # starts one Python worker per Send.  This table stores only the
    # provider-facing projection; the lossless transcript remains in
    # ``messages``.  ``raw_digest`` makes a stale projection fail closed after
    # undo, manual compaction or any other timeline rewrite.
    """
    CREATE TABLE IF NOT EXISTS context_checkpoints (
        session_id TEXT PRIMARY KEY,
        through_seq INTEGER NOT NULL DEFAULT 0,
        raw_count INTEGER NOT NULL DEFAULT 0,
        raw_digest TEXT NOT NULL DEFAULT '',
        history TEXT NOT NULL DEFAULT '[]',
        created REAL NOT NULL DEFAULT 0
    )
    """,
)

# Indexes are created *after* the migrations run, not with the tables. An index
# names columns, so building it against a table an older build wrote -- one
# that predates a column the index needs -- fails the whole connect(). That is
# how a database written earlier the same day locked a user out of sessions,
# /undo, --continue and /resume, with the error swallowed by the caller.
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_session ON snapshots (session_id, seq)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_cwd ON sessions (cwd, updated)",
    "CREATE INDEX IF NOT EXISTS idx_compactions_session ON compactions (session_id, id)",
)

# Columns added after the first release, applied to databases that predate
# them. Every entry must be additive with a constant default -- that is the
# only form of ALTER TABLE sqlite accepts, and it is also the only form that
# cannot lose a row.
_ADDED_COLUMNS = (
    ("sessions", "archived", "INTEGER NOT NULL DEFAULT 0"),
    ("messages", "created", "REAL"),
    ("messages", "tokens", "INTEGER"),
    ("messages", "reasoning", "TEXT NOT NULL DEFAULT '{}'"),
)

# Tables that must be rebuilt rather than altered, because the missing piece is
# an INTEGER PRIMARY KEY AUTOINCREMENT -- a column sqlite refuses to add with
# ALTER TABLE at all. Each entry is (table, required_column, columns_to_carry).
# A short-lived build shipped `compactions` without its `id`, so a database
# from that window has the table but not the key its index needs.
_REBUILT_TABLES = (
    ("compactions", "id",
     ("session_id", "seq", "created", "folded", "first_seq", "last_seq",
      "summary", "messages")),
)


def default_db_path() -> Path:
    """Where sessions live unless a caller (or a test) injects its own path."""
    return global_config_dir() / DEFAULT_DB_NAME


def new_session_id() -> str:
    """Time-prefixed so ids sort chronologically, random-suffixed so they collide never."""
    return "ses_%013x%s" % (int(time.time() * 1000), secrets.token_hex(3))


def summarize_title(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    """One line, collapsed whitespace, never longer than `limit`."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 3)].rstrip() + "..."


def _table_columns(conn: sqlite3.Connection, table: str) -> set:
    """Column names of `table`, empty when the table does not exist."""
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.DatabaseError:
        return set()


def _migrate(conn: sqlite3.Connection):
    """
    Bring a database written by an older haikode up to the current schema.

    Idempotent, and additive wherever sqlite allows it: a database that is
    already current is untouched, and one that is not keeps every row it holds.
    A concurrent process making the same change first is not an error, it is
    the outcome we wanted.
    """
    for table, column, declaration in _ADDED_COLUMNS:
        columns = _table_columns(conn, table)
        if not columns or column in columns:
            continue
        try:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                         % (table, column, declaration))
        except sqlite3.OperationalError as exc:
            # Only the race we expected. "database is locked" wears the same
            # exception type, and swallowing it leaves a connection whose
            # hot read path names a column that is not there — every load
            # and every append then fails with "no such column" until the
            # process restarts, matching none of the wedge tokens that would
            # have reopened the store. Let anything else reach the
            # transient-retry ladder that knows what to do with it.
            if "duplicate column" not in str(exc).lower():
                raise

    for table, required, carried in _REBUILT_TABLES:
        _rebuild_table(conn, table, required, carried)


def _rebuild_table(conn: sqlite3.Connection, table: str, required: str,
                   carried: Sequence[str]) -> None:
    """Recreate `table` with the current schema when `required` is missing.

    Only reachable for a table sqlite cannot ALTER into shape (an AUTOINCREMENT
    key). Rows are carried across by name, so a column the old build never had
    takes its declared default rather than a NULL the schema forbids. The whole
    move is one transaction: either the rebuilt table replaces the old one or
    the old one survives untouched.
    """
    columns = _table_columns(conn, table)
    if not columns or required in columns:
        return

    keep = [name for name in carried if name in columns]
    names = ", ".join(keep)
    temporary = "%s_migrating" % table
    creator = next((s for s in _SCHEMA
                    if ("CREATE TABLE IF NOT EXISTS %s" % table) in s), "")
    if not creator:
        return
    try:
        conn.execute("SAVEPOINT haikode_rebuild")
        conn.execute("DROP TABLE IF EXISTS %s" % temporary)
        conn.execute(creator.replace(table, temporary, 1))
        if keep:
            conn.execute("INSERT INTO %s (%s) SELECT %s FROM %s"
                         % (temporary, names, names, table))
        conn.execute("DROP TABLE %s" % table)
        conn.execute("ALTER TABLE %s RENAME TO %s" % (temporary, table))
        conn.execute("RELEASE haikode_rebuild")
    except sqlite3.DatabaseError:
        # Losing the rebuild is survivable; losing the rows is not.
        try:
            conn.execute("ROLLBACK TO haikode_rebuild")
            conn.execute("RELEASE haikode_rebuild")
        except sqlite3.DatabaseError:
            pass


def _transient_shaped(error: BaseException) -> bool:
    """True for the errors a live neighbour causes in passing.

    "locking protocol" is Haiku's WAL shared-memory glue coughing under a
    second live process; "database is locked" is ordinary contention. Both
    clear on their own. Anything else does not, and must never be waited
    out or recovered over.
    """
    message = str(error).lower()
    return ("locking protocol" in message
            or "database is locked" in message)


def _reason(exc: BaseException) -> str:
    return getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__


def _serialize_calls(calls: List[ToolCall]) -> str:
    # default=str: a tool argument the model never round-tripped through JSON
    # (a Path, a set) must not crash the run at persist time.
    return json.dumps([asdict(call) for call in calls], default=str)


def _deserialize_calls(raw: Optional[str]) -> List[ToolCall]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return []
    calls: List[ToolCall] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        arguments = item.get("arguments")
        calls.append(ToolCall(
            id=str(item.get("id", "")),
            name=str(item.get("name", "")),
            arguments=arguments if isinstance(arguments, dict) else {}))
    return calls


def _deserialize_reasoning(raw) -> dict:
    """The stored reasoning blob, or {} — never an exception.

    A row written before the column existed reads as NULL, and a row a
    human edited may be anything at all. Neither is a reason to fail
    loading a session: the blocks are an optimisation the next request can
    live without, unlike the messages around them.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _deserialize_display(raw: Optional[str]) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _serialize_message_list(messages: Sequence[Msg]) -> str:
    """Lossless-enough JSON for a provider-facing context checkpoint.

    The canonical transcript is still the ``messages`` table.  This encoding
    preserves every field the adapters may replay, including signed reasoning
    blocks; malformed values use the same ``default=str`` safety valve as the
    ordinary message writer.
    """
    payload = []
    for message in messages:
        payload.append({
            "role": str(getattr(message, "role", "") or ""),
            "content": str(getattr(message, "content", "") or ""),
            "tool_calls": [asdict(call) for call in
                           list(getattr(message, "tool_calls", None) or [])],
            "tool_call_id": str(getattr(message, "tool_call_id", "") or ""),
            "display": dict(getattr(message, "display", None) or {}),
            "reasoning": dict(getattr(message, "reasoning", None) or {}),
        })
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                      default=str)


def _deserialize_message_list(raw: Optional[str]) -> List[Msg]:
    """Read a checkpoint history, returning [] for any invalid envelope."""
    try:
        payload = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    messages: List[Msg] = []
    for item in payload:
        if not isinstance(item, dict):
            return []
        calls = _deserialize_calls(json.dumps(item.get("tool_calls") or [],
                                              default=str))
        display = item.get("display")
        reasoning = item.get("reasoning")
        messages.append(Msg(
            role=str(item.get("role") or ""),
            content=str(item.get("content") or ""),
            tool_calls=calls,
            tool_call_id=str(item.get("tool_call_id") or ""),
            display=dict(display) if isinstance(display, dict) else {},
            reasoning=dict(reasoning) if isinstance(reasoning, dict) else {},
        ))
    return messages


def _checkpoint_digest(first_seq: int, through_seq: int, raw_count: int) -> str:
    """Hash a raw prefix's immutable boundary identity, never its contents.

    Timeline-rewriting operations delete the checkpoint in their transaction.
    The boundary digest is the cheap second guard: validation stays O(1), not
    another scan of a raw archive that can grow for months.
    """
    encoded = "%d:%d:%d" % (int(first_seq), int(through_seq), int(raw_count))
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def message_files(message: Msg) -> List[str]:
    """Paths a message touched, read from the tool metadata the UI already gets."""
    found: List[str] = []
    path = (message.display or {}).get("path")
    if isinstance(path, str) and path:
        found.append(path)
    for call in message.tool_calls:
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        for key in ("filePath", "path", "file"):
            value = arguments.get(key)
            if isinstance(value, str) and value:
                found.append(value)
                break
    return found


def summarize_messages(messages: List[Msg], prompts: int = 8) -> str:
    """
    A factual digest of the messages a compaction is about to fold away.

    Used when the caller has no model-written summary. It stays to what
    provably happened -- prompts, tools, files -- because the model reads this
    text back as its own memory, and an invented narrative there is worse than
    a terse one.
    """
    if not messages:
        return ""
    lines = ["[earlier conversation, condensed]",
             "%d messages were folded into this summary." % len(messages)]

    asked = [m.content.strip() for m in messages
             if m.role == "user" and m.content.strip()]
    if asked:
        lines.append("")
        lines.append("Requests made:")
        for text in asked[:prompts]:
            lines.append("- " + summarize_title(text, 100))
        if len(asked) > prompts:
            lines.append("- ... and %d more" % (len(asked) - prompts))

    tools: Dict[str, int] = {}
    files: List[str] = []
    for message in messages:
        for call in message.tool_calls:
            tools[call.name] = tools.get(call.name, 0) + 1
        for path in message_files(message):
            if path not in files:
                files.append(path)
    if tools:
        ranked = sorted(tools.items(), key=lambda item: (-item[1], item[0]))
        lines.append("")
        lines.append("Tools used: " + ", ".join(
            "%s x%d" % (name, count) for name, count in ranked))
    if files:
        lines.append("Files involved: " + ", ".join(files[:20]))
        if len(files) > 20:
            lines.append("... and %d more files" % (len(files) - 20))

    replies = [m.content.strip() for m in messages
               if m.role == "assistant" and m.content.strip()]
    if replies:
        lines.append("")
        lines.append("Last answer: " + summarize_title(replies[-1], 200))
    return "\n".join(lines)


def _directory_keys(cwd: Union[str, Path]) -> List[str]:
    """Every spelling of `cwd` a session row may have been stored under."""
    raw = str(cwd)
    keys = [raw]
    for variant in (os.path.abspath(raw), os.path.realpath(raw),
                    raw.rstrip(os.sep) or os.sep):
        if variant not in keys:
            keys.append(variant)
    return keys


def _fence(text: str, language: str = "") -> str:
    """
    Wrap `text` in a code fence long enough to survive backticks inside it.

    Exported transcripts routinely contain markdown (the model writes it) and a
    three-backtick fence around a message that itself uses three backticks
    would end the block in the middle of the quote.
    """
    body = text if text.endswith("\n") or not text else text + "\n"
    longest = 0
    run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    ticks = "`" * max(3, longest + 1)
    return "%s%s\n%s%s" % (ticks, language, body, ticks)


def _timestamp(value: Optional[float]) -> str:
    """Local, human-readable, empty for a missing timestamp."""
    if not value:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(value)))


def _snippet(text: str, positions: List[int], width: int = SNIPPET_CHARS) -> str:
    """A one-line excerpt of `text` centred on the first matched character."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= width:
        return collapsed
    # positions index the raw text; whitespace collapsing shifts them slightly,
    # which is fine for an excerpt -- it only has to land near the match.
    focus = positions[0] if positions else 0
    start = max(0, min(focus - width // 3, len(collapsed) - width))
    excerpt = collapsed[start:start + width].strip()
    return ("..." if start > 0 else "") + excerpt + (
        "..." if start + width < len(collapsed) else "")


def _atomic_write(path: Path, text: str):
    """Restore through a sibling temp file so a crash cannot leave a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # mkstemp creates 0600; without this a reverted shell script would come
        # back non-executable. Restoring content must not restyle permissions.
        mode = os.stat(path).st_mode & 0o7777
    except OSError:
        mode = 0o644
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".haikode-revert-")
    try:
        # newline="" keeps the stored bytes exactly as they were captured.
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        try:
            os.chmod(temp, mode)
        except OSError:
            pass  # filesystems without permission bits (some Haiku mounts)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


class SessionStore:
    """SQLite-backed session storage. One store per database file."""

    # Databases this process already has open, by resolved path. Recovery may
    # never run against one of them: the WAL it would move aside belongs to a
    # connection that is still using it, and moving it corrupts that
    # connection's view of the file.
    _open_paths: Dict[str, int] = {}
    _open_lock = threading.Lock()

    def __init__(self, db_path: Union[str, Path, None] = None):
        self.path = Path(db_path) if db_path is not None else default_db_path()
        self._conn: Optional[sqlite3.Connection] = None
        # Tools may run from a worker thread; serialize every statement.
        self._lock = threading.RLock()
        # Held open while this store is in use; another process holding it is
        # how we know a WAL belongs to a live instance rather than a corpse.
        self._guard = None
        self._registered = False

    # --- connection ------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Open (and on first use create) the database."""
        with self._lock:
            if self._conn is not None:
                return self._conn
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # The shared guard comes FIRST (adversarial-review finding):
            # claimed after the open there was a window where a neighbour's
            # recovery could win the exclusive lock against a database we
            # were already inside — and a claim failure after a successful
            # open left an unguarded live connection registered. Recovery in
            # the except-path below upgrades this same handle to exclusive
            # atomically, and any failure rolls the whole claim back.
            self._claim_shared()
            try:
                try:
                    conn = self._open()
                except sqlite3.OperationalError as first:
                    # A process that died mid-write leaves the WAL index
                    # behind, and every later open fails with "locking
                    # protocol" until it is cleared. Observed on Haiku after
                    # a killed run: 15 sessions intact on disk, unreachable.
                    conn = self._retry_transient(first)
                    if conn is None:
                        if not self._clear_stale_wal(first):
                            raise
                        conn = self._open()
            except BaseException:
                # Neither a held guard nor recovery's exclusive upgrade may
                # outlive a failed open: the next attempt — ours or a
                # neighbour's — would find the store permanently "busy".
                self._release_guard()
                raise
            self._conn = conn
            self._register()
            # Downgrade a recovery's exclusive hold back to the shared
            # lifetime hold; a no-op when no recovery ran.
            self._claim()
            self._rotate_backup(conn)
            return conn

    # Between-try pauses for a transient open failure. A tuple so tests can
    # shrink the wait, not a magic number buried in the loop. The ladder got
    # fatter after the desktop worker, opening beside a TUI mid-turn, burned
    # through 1.2s of "locking protocol" and ran session-less; recovery
    # stays safe regardless — it needs the exclusive guard, which no live
    # neighbour will yield.
    _TRANSIENT_PAUSES = (0.3, 0.9, 1.8, 3.0)

    def _retry_transient(self, error: sqlite3.OperationalError):
        """A couple more tries for errors a live neighbour causes in passing.

        On Haiku a second live instance intermittently gets "locking
        protocol" from WAL's shared-memory glue, and it clears in well under
        a second. Recovery must stay the last resort: it moves the WAL
        aside, which for a *live* neighbour is how committed turns vanished
        in the field ("session not saved", twice in two evenings).
        """
        if not _transient_shaped(error):
            return None
        for pause in self._TRANSIENT_PAUSES:
            time.sleep(pause)
            try:
                return self._open()
            except sqlite3.OperationalError as again:
                if not _transient_shaped(again):
                    # A different failure class mid-retry is a different
                    # problem: judging recovery on the *first* error's shape
                    # would clear WAL state over, say, a malformed database.
                    raise
                continue
        return None

    def reset(self) -> None:
        """Drop a wedged connection so the next use reopens from scratch.

        The guard is kept: the store is still in use, and releasing it would
        invite a neighbour's recovery into the very WAL we are reopening.
        """
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
            self._unregister()

    def _rotate_backup(self, conn: sqlite3.Connection) -> None:
        """Keep a few verified snapshots beside the database.

        Twice in one day a defect destroyed a live store, and both times the
        only thing that saved the conversations was a copy someone had taken
        by hand. This takes it automatically at open, but no more often than
        BACKUP_MIN_INTERVAL across short-lived worker processes.

        Two rules make it a safety net rather than a second way to lose data:
        the source is checked before anything is written, so a store that is
        already corrupt never overwrites a good snapshot; and the snapshots
        rotate, so one bad generation cannot erase every earlier one. The
        sqlite backup API is used rather than a file copy because it produces
        a consistent image even while another connection is writing.

        Any failure here is silent: a missing backup must never stop a user
        from reaching their sessions.
        """
        if self._guard is None:
            # On a platform with advisory locks, no guard means something is
            # off — do not also race a sibling's rotation. Lockless platforms
            # keep rotating: an occasional double copy beats no safety net.
            try:
                import fcntl  # noqa: F401
            except ImportError:
                pass
            else:
                return
        try:
            newest = Path("%s.bak1" % self.path)
            try:
                age = time.time() - newest.stat().st_mtime
            except OSError:
                age = BACKUP_MIN_INTERVAL
            if 0 <= age < BACKUP_MIN_INTERVAL:
                return
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return
            if not conn.execute(
                    "SELECT 1 FROM sessions LIMIT 1").fetchone():
                return          # nothing worth keeping yet
            oldest = Path("%s.bak%d" % (self.path, BACKUP_GENERATIONS))
            if oldest.exists():
                oldest.unlink()
            for index in range(BACKUP_GENERATIONS - 1, 0, -1):
                older = Path("%s.bak%d" % (self.path, index))
                if older.exists():
                    older.replace(Path("%s.bak%d" % (self.path, index + 1)))
            staging = Path("%s.bak.tmp" % self.path)
            if staging.exists():
                staging.unlink()
            target = sqlite3.connect(str(staging))
            try:
                conn.backup(target)
            finally:
                target.close()
            staging.replace(newest)
        except Exception:
            return

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError as exc:
            # Some network/older filesystems reject WAL; the rollback
            # journal is slower but correct. But "file is not a database"
            # is no WAL refusal: swallowing it here masked a corrupted
            # store on a field machine until every later write failed.
            if "not a database" in str(exc).lower():
                raise
        # Explicit, though it is sqlite's default: every committed turn is
        # fsynced. A session's last turns before a power cut are exactly the
        # ones the user comes back for — one machine lost that tail in the
        # field. If the disk's write cache lies about fsync, nothing here
        # can save it, but nothing above this layer should make it worse.
        try:
            conn.execute("PRAGMA synchronous=FULL")
        except sqlite3.DatabaseError:
            pass
        # Order matters: tables, then migrations, then indexes. An index
        # names columns, so it can only be built once every table has the
        # shape this build expects.
        for statement in _SCHEMA:
            conn.execute(statement)
        _migrate(conn)
        for statement in _INDEXES:
            conn.execute(statement)
        conn.commit()
        return conn

    def _key(self) -> str:
        try:
            return str(self.path.resolve())
        except OSError:
            return str(self.path)

    def _open_here(self) -> bool:
        """True when this process already has a connection to this file."""
        with SessionStore._open_lock:
            return SessionStore._open_paths.get(self._key(), 0) > 0

    def _register(self) -> None:
        if self._registered:
            return
        with SessionStore._open_lock:
            key = self._key()
            SessionStore._open_paths[key] = \
                SessionStore._open_paths.get(key, 0) + 1
        self._registered = True

    def _unregister(self) -> None:
        if not self._registered:
            return
        with SessionStore._open_lock:
            key = self._key()
            remaining = SessionStore._open_paths.get(key, 1) - 1
            if remaining > 0:
                SessionStore._open_paths[key] = remaining
            else:
                SessionStore._open_paths.pop(key, None)
        self._registered = False

    def _claim(self, exclusive: bool = False) -> bool:
        """Take (or convert) the cross-process guard for this database.

        The lock model an adversarial review demanded, after the one-owner
        version left every store but the first unguarded: all live stores
        hold the guard *shared*, recovery alone needs it *exclusive*, so no
        recovery can run while anyone at all is alive — and no store is ever
        the unlucky one that "merely failed to claim". Conversion on the
        already-held handle is atomic, which is how a recovery's exclusive
        hold downgrades to the shared lifetime hold afterwards.
        """
        try:
            import fcntl
        except ImportError:
            return False  # no advisory locks: never assume we are alone
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if self._guard is not None:
            try:
                fcntl.flock(self._guard.fileno(), mode | fcntl.LOCK_NB)
                return True
            except OSError:
                return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(str(self.path) + ".guard", "a+")
        except OSError:
            return False
        try:
            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._guard = handle
        return True

    def _release_guard(self) -> None:
        if self._guard is not None:
            try:
                self._guard.close()
            except OSError:
                pass
            self._guard = None

    def _claim_shared(self) -> None:
        """Hold the shared guard for the store's lifetime.

        Waits out a neighbour recovery's brief exclusive hold rather than
        living unguarded past it; raising after the patience runs out is
        deliberate — an unguarded store is exactly the state that lost
        committed turns in the field. No-lock platforms are exempt: there
        the guard cannot protect anyone anyway.
        """
        try:
            import fcntl  # noqa: F401
        except ImportError:
            return
        for _ in range(25):
            if self._claim():
                return
            time.sleep(0.2)
        raise sqlite3.OperationalError(
            "another haikode is recovering this database; try again shortly")

    def _clear_stale_wal(self, error: sqlite3.OperationalError) -> bool:
        """Remove a dead process's WAL index so the database opens again.

        Only the `-shm` file is deleted, and only for the errors that actually
        mean "the lock state is unusable". The `-shm` is a rebuildable index
        into the `-wal`, so dropping it costs nothing; the `-wal` itself holds
        committed data and is moved aside rather than deleted, so a bad guess
        here can still be undone by hand.

        Recovery only runs when this process holds the guard. Haiku reports
        "locking protocol" for a *live* second instance too, and without the
        guard a newly started haikode would rip the running one's WAL out from
        under it — losing the turns it had committed but not checkpointed.

        Returns True when something was cleared and a retry is worth making.
        """
        message = str(error).lower()
        if not any(token in message for token in
                   ("locking protocol", "unable to open database",
                    "disk i/o error")):
            return False
        if not self.path.exists():
            return False
        if self._open_here():
            return False
        if not self._claim(exclusive=True):
            # Someone alive holds the guard shared — their WAL, not a corpse's.
            return False

        cleared = False
        index = Path(str(self.path) + "-shm")
        try:
            if index.exists():
                index.unlink()
                cleared = True
        except OSError:
            return False

        journal = Path(str(self.path) + "-wal")
        try:
            if journal.exists() and journal.stat().st_size:
                # Keep it: if this recovery turns out to be wrong, the data is
                # still on disk next to the database rather than gone.
                journal.replace(Path(str(self.path) + "-wal.recovered"))
                cleared = True
        except OSError:
            pass
        return cleared

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
                self._unregister()
            self._release_guard()

    def _query(self, sql: str, params=()) -> List[sqlite3.Row]:
        with self._lock:
            return self.connect().execute(sql, params).fetchall()

    def _write(self, sql: str, params=()):
        with self._lock:
            conn = self.connect()
            conn.execute(sql, params)
            conn.commit()

    # --- sessions --------------------------------------------------------

    def new_session(self, cwd: str, provider: str, model: str,
                    title: str = "") -> "Session":
        now = time.time()
        session_id = new_session_id()
        self._write(
            "INSERT INTO sessions "
            "(id, title, cwd, provider, model, created, updated, archived) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
            (session_id, title, str(cwd), provider, model, now, now))
        return Session(self, session_id, title=title, cwd=str(cwd),
                       provider=provider, model=model, created=now, updated=now)

    def load(self, session_id: str) -> Optional["Session"]:
        rows = self._query(
            "SELECT id, title, cwd, provider, model, created, updated, archived "
            "FROM sessions WHERE id = ?", (session_id,))
        if not rows:
            return None
        row = rows[0]
        session = Session(self, row["id"], title=row["title"] or "",
                          cwd=row["cwd"] or "", provider=row["provider"] or "",
                          model=row["model"] or "", created=row["created"] or 0.0,
                          updated=row["updated"] or 0.0,
                          archived=bool(row["archived"]))
        session.reload()
        return session

    def list_sessions(self, limit: int = 50, include_archived: bool = False,
                      cwd: Union[str, Path, None] = None) -> List[Dict[str, Any]]:
        """
        Most recently updated first.

        `cwd` restricts the list to one project, the way opencode's session
        dialog does. Both the stored and the requested directory are compared
        raw and resolved, because a session may have been opened through a
        symlinked path (/var vs /private/var on macOS, /boot/home symlinks on
        Haiku) and the user still means the same project.
        """
        where: List[str] = []
        params: List[Any] = []
        if not include_archived:
            where.append("s.archived = 0")
        if cwd is not None:
            candidates = _directory_keys(cwd)
            where.append("s.cwd IN (%s)" % ", ".join("?" * len(candidates)))
            params.extend(candidates)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.append(int(limit))
        rows = self._query(
            "SELECT s.id, s.title, s.cwd, s.provider, s.model, s.created, "
            "s.updated, s.archived, "
            "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS n "
            "FROM sessions s" + clause +
            " ORDER BY s.updated DESC, s.id DESC LIMIT ?", tuple(params))
        return [self._row_to_dict(row) for row in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"] or "",
            "cwd": row["cwd"] or "",
            "provider": row["provider"] or "",
            "model": row["model"] or "",
            "created": row["created"] or 0.0,
            "updated": row["updated"] or 0.0,
            "archived": bool(row["archived"]),
            "message_count": row["n"],
        }

    def search(self, query: str, limit: int = 20,
               include_archived: bool = False) -> List[Dict[str, Any]]:
        """
        Rank sessions against `query` over titles and message text.

        Rows have the same shape as list_sessions() plus "score" and
        "snippet". Message bodies are scored line by line rather than whole:
        fuzzy matching is subsequence matching, and over a ten-kilobyte tool
        output almost any query matches, which would rank noise first.
        """
        where = "" if include_archived else " WHERE s.archived = 0"
        rows = self._query(
            "SELECT s.id, s.title, s.cwd, s.provider, s.model, s.created, "
            "s.updated, s.archived, "
            "(SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS n "
            "FROM sessions s" + where + " ORDER BY s.updated DESC, s.id DESC")
        results: List[Dict[str, Any]] = []
        for row in rows:
            scored = self._score_session(row["id"], row["title"] or "", query)
            if scored is None:
                continue
            score, snippet = scored
            item = self._row_to_dict(row)
            item["score"] = score
            item["snippet"] = snippet
            results.append(item)
        # Stable tie-break on recency so an empty query degrades exactly into
        # list_sessions() rather than into an arbitrary order.
        results.sort(key=lambda item: (-item["score"], -item["updated"], item["id"]))
        return results[:max(0, int(limit))]

    def _score_session(self, session_id: str, title: str,
                       query: str) -> Optional[Tuple[int, str]]:
        best: Optional[Tuple[int, str]] = None
        title_hit = fuzzy_score(query, title)
        if title_hit is not None:
            best = (title_hit[0] * SEARCH_TITLE_WEIGHT + SEARCH_TITLE_BONUS,
                    _snippet(title, title_hit[1]))
        scanned = 0
        for row in self._query(
                "SELECT content FROM messages WHERE session_id = ? ORDER BY seq",
                (session_id,)):
            for line in (row["content"] or "").splitlines():
                if not line.strip():
                    continue
                scanned += 1
                if scanned > SEARCH_MAX_LINES:
                    break
                hit = fuzzy_score(query, line)
                if hit is None:
                    continue
                if best is None or hit[0] > best[0]:
                    best = (hit[0], _snippet(line, hit[1]))
            if scanned > SEARCH_MAX_LINES:
                break
        return best

    def delete(self, session_id: str):
        with self._lock:
            conn = self.connect()
            conn.execute("DELETE FROM snapshots WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM compactions WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM context_checkpoints WHERE session_id = ?",
                         (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.commit()


class Session:
    """A conversation plus the file snapshots that make it revertible."""

    def __init__(self, store: SessionStore, session_id: str, title: str = "",
                 cwd: str = "", provider: str = "", model: str = "",
                 created: float = 0.0, updated: float = 0.0,
                 archived: bool = False):
        self.store = store
        self.id = session_id
        self.title = title
        self.cwd = cwd
        self.provider = provider
        self.model = model
        self.created = created
        self.updated = updated
        self.archived = archived
        self.messages: List[Msg] = []
        # Sequence number of each entry in `messages`. After a compaction the
        # numbers are no longer 1..n (folded rows are gone and snapshots still
        # point at the old ones), so index and seq must be tracked separately.
        self.seqs: List[int] = []
        self._seq = 0
        # The revert point new snapshots belong to; None until a run starts.
        self._checkpoint: Optional[int] = None

    # --- loading ---------------------------------------------------------

    def reload(self):
        """Re-read messages from the database into `self.messages`."""
        rows = self.store._query(
            "SELECT seq, role, content, tool_calls, tool_call_id, display, "
            "reasoning FROM messages WHERE session_id = ? ORDER BY seq",
            (self.id,))
        self.messages = [Msg(role=row["role"], content=row["content"] or "",
                             tool_calls=_deserialize_calls(row["tool_calls"]),
                             tool_call_id=row["tool_call_id"] or "",
                             display=_deserialize_display(row["display"]),
                             reasoning=_deserialize_reasoning(row["reasoning"]))
                         for row in rows]
        self.seqs = [int(row["seq"]) for row in rows]
        self._seq = rows[-1]["seq"] if rows else 0

    # --- messages --------------------------------------------------------

    _append_wedge_tokens = ("disk i/o error",
                            "database disk image is malformed",
                            "locking protocol")

    def append(self, message: Msg, tokens: Optional[int] = None) -> int:
        """
        Persist one message immediately; returns its seq.

        `tokens` is the provider's own count for this message when the caller
        has one. It is stored as-is and never invented: a NULL means "not
        reported", which token_totals() then fills in with an estimate.
        """
        now = time.time()

        def write(conn: sqlite3.Connection, at: int) -> None:
            # An explicit transaction with a guaranteed rollback: without it
            # a failure between the two statements left half an append
            # behind. Plain INSERT, never OR REPLACE — a seq collision must
            # surface, not silently swallow whichever message came first.
            if getattr(conn, "in_transaction", False):
                # A sibling write path's implicit transaction (legacy
                # isolation) would make BEGIN fail; every such path commits
                # under the same store lock, so anything open here is stray.
                conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO messages "
                    "(session_id, seq, role, content, tool_calls, "
                    "tool_call_id, display, reasoning, created, tokens) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.id, at, message.role, message.content or "",
                     _serialize_calls(message.tool_calls),
                     message.tool_call_id or "",
                     json.dumps(message.display or {}, default=str),
                     json.dumps(getattr(message, "reasoning", None) or {},
                                default=str), now,
                     None if tokens is None else int(tokens)))
                conn.execute("UPDATE sessions SET updated = ? WHERE id = ?",
                             (now, self.id))
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

        def landed(conn: sqlite3.Connection, at: int) -> bool:
            # A commit can be durable and still *report* failure (the fsync
            # answered late, the mapping dropped). Rewriting in that state
            # is how a message overwrites its committed self — check first.
            row = conn.execute(
                "SELECT content FROM messages WHERE session_id = ? "
                "AND seq = ?", (self.id, at)).fetchone()
            return row is not None and row[0] == (message.content or "")

        # Allocating the seq under the store lock: two threads (the task tool
        # runs tools off the main thread) would otherwise pick the same seq.
        with self.store._lock:
            seq = self._seq + 1
            try:
                try:
                    write(self.store.connect(), seq)
                except sqlite3.IntegrityError:
                    # Another writer of this session took our seq. Adopt the
                    # database's truth and take the next one, once.
                    row = self.store.connect().execute(
                        "SELECT MAX(seq) FROM messages WHERE session_id = ?",
                        (self.id,)).fetchone()
                    seq = int(row[0] or 0) + 1
                    write(self.store.connect(), seq)
            except sqlite3.DatabaseError as exc:
                if not any(token in str(exc).lower()
                           for token in self._append_wedge_tokens):
                    raise
                # A ripped WAL or a dropped mapping breaks the connection for
                # good; without this reopen the session would answer "session
                # not saved" to every turn until the user restarts.
                self.store.reset()
                conn = self.store.connect()
                if not landed(conn, seq):
                    write(conn, seq)
            # Only after the write succeeded, so a failed insert cannot leave a
            # hole in the sequence or a message in memory that is not on disk.
            self._seq = seq
            self.updated = now
            self.messages.append(message)
            self.seqs.append(seq)
        if message.role == "user":
            self.auto_title(message.content)
        return seq

    def extend(self, messages: List[Msg]):
        for message in messages:
            self.append(message)

    def set_tokens(self, seq: int, tokens: Optional[int]):
        """Attach (or clear) a provider token count for an already stored message."""
        self.store._write("UPDATE messages SET tokens = ? WHERE session_id = ? AND seq = ?",
                          (None if tokens is None else int(tokens), self.id, int(seq)))

    def token_totals(self) -> Dict[str, Any]:
        """
        Token usage of the stored history.

        Messages the caller reported a count for are summed as reported;
        the rest are estimated with context.message_tokens so the total is
        always usable, and "recorded"/"estimated" say how much of it is which.
        """
        rows = self.store._query(
            "SELECT role, content, tool_calls, tokens FROM messages "
            "WHERE session_id = ? ORDER BY seq", (self.id,))
        recorded = estimated = counted = 0
        by_role: Dict[str, int] = {}
        for row in rows:
            value = row["tokens"]
            if value is None:
                value = message_tokens(Msg(
                    role=row["role"], content=row["content"] or "",
                    tool_calls=_deserialize_calls(row["tool_calls"])))
                estimated += value
            else:
                value = int(value)
                recorded += value
                counted += 1
            by_role[row["role"]] = by_role.get(row["role"], 0) + value
        return {"total": recorded + estimated, "recorded": recorded,
                "estimated": estimated, "counted": counted,
                "messages": len(rows), "by_role": by_role}

    # --- title and state -------------------------------------------------

    def rename(self, title: str) -> str:
        """Set the title explicitly; returns the stored (stripped) value."""
        self.title = (title or "").strip()
        self.updated = time.time()
        self.store._write("UPDATE sessions SET title = ?, updated = ? WHERE id = ?",
                          (self.title, self.updated, self.id))
        return self.title

    def set_title(self, text: str):
        """Older name for rename(); kept because the REPL and TUI call it."""
        self.rename(text)

    def touch(self) -> float:
        """Mark the session as active now so it sorts to the top of a listing."""
        self.updated = time.time()
        self.store._write("UPDATE sessions SET updated = ? WHERE id = ?",
                          (self.updated, self.id))
        return self.updated

    def archive(self):
        """Hide the session from the default listing without deleting it."""
        self._set_archived(True)

    def unarchive(self):
        self._set_archived(False)

    def _set_archived(self, flag: bool):
        self.archived = bool(flag)
        # `updated` is deliberately left alone: archiving is not activity, and
        # unarchiving must not push an old session to the top of the list.
        self.store._write("UPDATE sessions SET archived = ? WHERE id = ?",
                          (1 if flag else 0, self.id))

    def auto_title(self, first_user_message: str) -> str:
        """Derive a short title from the first prompt, unless one is already set."""
        if self.title.strip():
            return self.title
        title = summarize_title(first_user_message)
        if title:
            self.set_title(title)
        return self.title

    # --- revert points ---------------------------------------------------

    def checkpoint(self) -> int:
        """Mark a revert point before a run; returns the seq to revert back to."""
        self._checkpoint = self._seq
        return self._checkpoint

    @property
    def current_point(self) -> int:
        """The revert point snapshots attach to, opened implicitly if needed."""
        if self._checkpoint is None:
            self._checkpoint = self._seq
        return self._checkpoint

    def last_checkpoint(self) -> Optional[int]:
        """
        The newest revert point: the open in-memory one or the newest one that
        has snapshots, whichever is later.

        Both halves are needed. The stored maximum survives a restart, but a run
        that only talked (no file edits) leaves no snapshot row, and reverting to
        the stored maximum would then throw away that run *plus* every earlier
        one. The open checkpoint alone is not enough either — it is None in a
        session that was just loaded from disk.
        """
        rows = self.store._query(
            "SELECT MAX(seq) AS seq FROM snapshots WHERE session_id = ?", (self.id,))
        stored = int(rows[0]["seq"]) if rows and rows[0]["seq"] is not None else None
        if stored is None:
            return self._checkpoint
        if self._checkpoint is None:
            return stored
        return max(stored, self._checkpoint)

    def record_snapshot(self, path: str, original: Optional[str]):
        """
        Remember a file's pre-edit content for the current revert point.

        Only the first record for a (revert point, path) is kept — later edits in
        the same run must not overwrite the content the run started with. The key
        is the realpath, matching ToolContext.resolve(), so /var/x and
        /private/var/x cannot become two rows that then restore over each other.
        """
        self.store._write(
            "INSERT OR IGNORE INTO snapshots (session_id, seq, path, original, created) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.id, self.current_point, os.path.realpath(path), original, time.time()))

    def snapshots(self, seq: int = 0) -> Dict[str, Optional[str]]:
        """Original content per path for every revert point at or after `seq`."""
        rows = self.store._query(
            "SELECT path, original FROM snapshots WHERE session_id = ? AND seq >= ? "
            "ORDER BY seq DESC, created DESC, rowid DESC", (self.id, int(seq)))
        # Descending order means the *last* row written for a path is the
        # earliest one recorded, i.e. the true pre-run content.
        originals: Dict[str, Optional[str]] = {}
        for row in rows:
            originals[row["path"]] = row["original"]
        return originals

    # --- revert ----------------------------------------------------------

    def revert_to(self, seq: int) -> List[str]:
        """
        Undo everything after message `seq`: restore snapshotted files, drop the
        later messages, and return the paths that were restored. A path that
        could not be restored is reported as "path (failed: reason)" rather than
        aborting the rest of the revert.
        """
        seq = max(0, int(seq))
        restored: List[str] = []
        for path, original in self.snapshots(seq).items():
            target = Path(path)
            if not self._within_cwd(path):
                # Snapshot rows are just data: a database moved between
                # machines, or a tool that followed a symlink out of the
                # project, must never let undo write outside the session.
                restored.append("%s (skipped: outside %s)" % (path, self.cwd))
                continue
            try:
                if original is None:
                    if target.is_dir():
                        raise IsADirectoryError(f"{path} is a directory")
                    if target.exists() or target.is_symlink():
                        os.unlink(target)
                else:
                    _atomic_write(target, original)
                restored.append(path)
            except (OSError, ValueError) as e:
                restored.append(f"{path} (failed: {_reason(e)})")

        with self.store._lock:
            conn = self.store.connect()
            conn.execute("DELETE FROM messages WHERE session_id = ? AND seq > ?",
                         (self.id, seq))
            conn.execute("DELETE FROM snapshots WHERE session_id = ? AND seq >= ?",
                         (self.id, seq))
            # A compaction record whose summary message just disappeared would
            # otherwise describe messages that are no longer in the timeline.
            conn.execute("DELETE FROM compactions WHERE session_id = ? AND seq > ?",
                         (self.id, seq))
            conn.execute("DELETE FROM context_checkpoints WHERE session_id = ?",
                         (self.id,))
            conn.execute("UPDATE sessions SET updated = ? WHERE id = ?",
                         (time.time(), self.id))
            conn.commit()

        self.reload()
        self._checkpoint = None
        return restored

    def revert_last(self) -> List[str]:
        """Undo the most recent checkpoint. No checkpoint means nothing to do."""
        point = self.last_checkpoint()
        if point is None:
            return []
        return self.revert_to(point)

    def _within_cwd(self, path: str) -> bool:
        """
        True when `path` lies inside the session's own directory.

        A session with no recorded directory (rows from before cwd was stored)
        is trusted, because there is nothing to compare against and refusing
        every restore would break undo for those sessions entirely.
        """
        root = (self.cwd or "").strip()
        if not root:
            return True
        try:
            root_real = os.path.realpath(root)
            target = os.path.realpath(path)
        except (OSError, ValueError):
            return False
        if target == root_real:
            return True
        return target.startswith(root_real.rstrip(os.sep) + os.sep)

    # --- automatic provider-context checkpoint --------------------------

    def save_context_checkpoint(self, history: Sequence[Msg],
                                raw_count: int) -> bool:
        """Persist a successful automatic-compaction view without folding raw rows.

        ``raw_count`` is how many messages from the ordered raw transcript the
        view has incorporated.  Later appends are deliberately allowed: a new
        worker restores this prefix and appends the newer raw tail.  Revert and
        manual-compaction paths clear the row transactionally.
        """
        try:
            raw_count = int(raw_count)
        except (TypeError, ValueError, OverflowError):
            return False
        snapshot = list(history or [])
        if (raw_count <= 0 or raw_count > len(self.seqs)
                or not any(bool((message.display or {}).get("summary"))
                           for message in snapshot)):
            return False
        with self.store._lock:
            if raw_count > len(self.seqs):
                return False
            first_seq = self.seqs[0]
            through_seq = self.seqs[raw_count - 1]
            encoded = _serialize_message_list(snapshot)
            conn = self.store.connect()
            conn.execute(
                "INSERT OR REPLACE INTO context_checkpoints "
                "(session_id, through_seq, raw_count, raw_digest, history, created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self.id, through_seq, raw_count,
                 _checkpoint_digest(first_seq, through_seq, raw_count),
                 encoded, time.time()))
            conn.commit()
        return True

    def load_context_checkpoint(self) -> Optional[Tuple[List[Msg], int]]:
        """Return a valid ``(provider_history, raw_count)`` checkpoint.

        Validation never scans prompt text.  Sequence ids are append-only, and
        every in-process timeline rewrite deletes this row in the same SQLite
        transaction, so a matching prefix identifies the raw material the
        projection was built from.
        """
        rows = self.store._query(
            "SELECT through_seq, raw_count, raw_digest, history "
            "FROM context_checkpoints WHERE session_id = ?", (self.id,))
        if not rows:
            return None
        row = rows[0]
        try:
            raw_count = int(row["raw_count"])
            through_seq = int(row["through_seq"])
        except (TypeError, ValueError, OverflowError):
            return None
        if raw_count <= 0 or raw_count > len(self.seqs):
            return None
        first_seq = self.seqs[0]
        if (self.seqs[raw_count - 1] != through_seq
                or _checkpoint_digest(first_seq, through_seq, raw_count)
                != str(row["raw_digest"] or "")):
            return None
        history = _deserialize_message_list(row["history"])
        if not history or not any(
                bool((message.display or {}).get("summary"))
                for message in history):
            return None
        return history, raw_count

    # --- compaction ------------------------------------------------------

    def needs_compaction(self, window: int,
                         reserve: float = DEFAULT_RESERVE) -> bool:
        """
        True when the stored history no longer fits its share of `window`.

        Estimated with context.message_tokens rather than with the recorded
        provider counts: those are per-request totals that overlap heavily
        between messages, so summing them would demand compaction far too
        early. Literally the same budget rule as context.compact_messages --
        the same function -- so the automatic trigger and this answer can never
        disagree.
        """
        return needs_compaction(self.messages, window, reserve)

    def previous_summary(self) -> str:
        """The newest stored summary, so the next one updates it in place.

        opencode re-anchors: every compaction rewrites one summary rather than
        stacking a summary of a summary, which is what keeps a session's oldest
        decisions readable after the tenth fold.
        """
        rows = self.store._query(
            "SELECT summary FROM compactions WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 1", (self.id,))
        return (rows[0]["summary"] or "") if rows else ""

    def compact_now(self, keep_last: int = DEFAULT_COMPACT_KEEP, *,
                    summary: str = "", provider: Any = None, model: str = "",
                    trigger: str = "manual",
                    tail_turns: int = DEFAULT_TAIL_TURNS,
                    keep_tokens: int = MAX_KEEP_TOKENS,
                    max_tokens: int = SUMMARY_MAX_TOKENS,
                    on_usage: Any = None) -> CompactionResult:
        """
        Fold the old turns into one summary, in memory and on disk.

        With a `provider` the summary is written by the model, the way opencode
        does it: the folded turns go up as a transcript and come back as the
        anchored summary that replaces them. Without one — or when that call
        fails — the mechanical digest from summarize_messages() is stored
        instead, so a failing summariser costs detail and never the
        conversation. The folded messages themselves are kept verbatim in the
        compactions table either way, so restore_compaction() can undo it.

        Sequence numbers are never renumbered: snapshot rows are keyed by seq,
        and renumbering would silently point them at the wrong revert points.
        """
        plan = plan_compaction(self.messages, keep_last=int(keep_last),
                               tail_turns=tail_turns, keep_tokens=keep_tokens)
        if not plan.folded:
            return CompactionResult(messages=list(self.messages),
                                    kept=len(self.messages), trigger=trigger)

        folded = [self.messages[index] for index in plan.folded]
        # Planned and summarised outside the store lock: the provider round can
        # take tens of seconds, and holding the lock through it would block
        # every other thread's append. The write below re-checks that nothing
        # moved underneath us.
        seen = list(self.seqs)
        text, error, summarized = (summary or "").strip(), "", True
        if not text:
            if provider is None:
                error, summarized = "no summariser available", False
            else:
                text, error = summarize_with_reason(
                    folded, provider, model,
                    previous_summary=self.previous_summary(),
                    max_tokens=max_tokens, on_usage=on_usage)
                summarized = bool(text)
        if not text:
            text = summarize_messages(folded)

        with self.store._lock:
            if self.seqs[:len(seen)] != seen:
                # A revert or a concurrent fold rewrote the timeline while the
                # summary was being written; the plan describes messages that
                # may no longer be there, so refuse rather than delete blindly.
                return CompactionResult(
                    messages=list(self.messages), kept=len(self.messages),
                    error="the session changed while it was being summarised",
                    trigger=trigger)
            folded_seqs = [seen[index] for index in plan.folded]
            summary_seq = folded_seqs[-1]
            first_seq = folded_seqs[0]
            # Expressed as "everything up to the summary except what stays"
            # rather than as an IN list of the folded seqs: the kept set before
            # the boundary is only the system and pinned messages, while the
            # folded one can be thousands of rows and blow sqlite's variable
            # limit.
            protected = [seq for seq in
                         (seen[index] for index in plan.keep)
                         if seq <= summary_seq]
            holes = " AND seq NOT IN (%s)" % ", ".join("?" * len(protected)) \
                if protected else ""
            params = (self.id, summary_seq, *protected)
            conn = self.store.connect()
            stored = conn.execute(
                "SELECT seq, role, content, tool_calls, tool_call_id, display, "
                "reasoning, created, tokens "
                "FROM messages WHERE session_id = ? AND seq <= ?"
                + holes + " ORDER BY seq", params).fetchall()
            now = time.time()
            conn.execute("DELETE FROM messages WHERE session_id = ? AND seq <= ?"
                         + holes, params)
            conn.execute("DELETE FROM context_checkpoints WHERE session_id = ?",
                         (self.id,))
            # role "user": the summary must be replayed as context, and a
            # history that opens with an assistant turn is rejected by some
            # providers while a user turn is accepted by all of them.
            conn.execute(
                "INSERT INTO messages "
                "(session_id, seq, role, content, tool_calls, tool_call_id, "
                "display, created, tokens) VALUES (?, ?, ?, ?, '[]', '', ?, ?, NULL)",
                (self.id, summary_seq, "user", text,
                 json.dumps({"summary": True, "folded": len(folded)}), now))
            conn.execute(
                "INSERT INTO compactions (session_id, seq, created, folded, "
                "first_seq, last_seq, summary, messages) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (self.id, summary_seq, now, len(folded), first_seq, summary_seq,
                 text, json.dumps([dict(row) for row in stored], default=str)))
            conn.execute("UPDATE sessions SET updated = ? WHERE id = ?",
                         (now, self.id))
            conn.commit()
            self.updated = now
            self.reload()
        return CompactionResult(messages=list(self.messages), folded=len(folded),
                                kept=len(plan.keep), summary=text,
                                summarized=summarized, error=error,
                                trigger=trigger)

    def compact(self, keep_last: int = DEFAULT_COMPACT_KEEP, summary: str = "",
                provider: Any = None, model: str = "",
                on_usage: Any = None) -> int:
        """compact_now() for callers that only want the folded count."""
        return self.compact_now(keep_last=keep_last, summary=summary,
                                provider=provider, model=model,
                                on_usage=on_usage).folded

    def compactions(self) -> List[Dict[str, Any]]:
        """Every compaction of this session, oldest first."""
        rows = self.store._query(
            "SELECT id, seq, created, folded, first_seq, last_seq, summary, messages "
            "FROM compactions WHERE session_id = ? ORDER BY id", (self.id,))
        records = []
        for row in rows:
            try:
                messages = json.loads(row["messages"])
            except (TypeError, ValueError):
                messages = []
            records.append({
                "id": int(row["id"]),
                "seq": int(row["seq"]),
                "created": row["created"] or 0.0,
                "folded": int(row["folded"]),
                "first_seq": int(row["first_seq"]),
                "last_seq": int(row["last_seq"]),
                "summary": row["summary"] or "",
                "messages": messages if isinstance(messages, list) else [],
            })
        return records

    def restore_compaction(self, record_id: Optional[int] = None) -> int:
        """
        Undo a compaction: drop its summary message and put the folded ones
        back at their original seqs. Defaults to the most recent compaction.
        Returns how many messages were restored.
        """
        with self.store._lock:
            conn = self.store.connect()
            if record_id is None:
                rows = conn.execute(
                    "SELECT id, seq, messages FROM compactions WHERE session_id = ? "
                    "ORDER BY id DESC LIMIT 1", (self.id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, seq, messages FROM compactions "
                    "WHERE session_id = ? AND id = ?",
                    (self.id, int(record_id))).fetchall()
            if not rows:
                return 0
            row = rows[0]
            try:
                stored = json.loads(row["messages"])
            except (TypeError, ValueError):
                stored = []
            stored = [item for item in stored if isinstance(item, dict)]
            conn.execute("DELETE FROM messages WHERE session_id = ? AND seq = ?",
                         (self.id, row["seq"]))
            for item in stored:
                conn.execute(
                    "INSERT OR REPLACE INTO messages "
                    "(session_id, seq, role, content, tool_calls, tool_call_id, "
                    "display, reasoning, created, tokens) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.id, int(item.get("seq", 0)), str(item.get("role", "user")),
                     item.get("content") or "", item.get("tool_calls") or "[]",
                     item.get("tool_call_id") or "", item.get("display") or "{}",
                     item.get("reasoning") or "{}",
                     item.get("created"), item.get("tokens")))
            conn.execute("DELETE FROM compactions WHERE id = ?", (int(row["id"]),))
            conn.execute("DELETE FROM context_checkpoints WHERE session_id = ?",
                         (self.id,))
            conn.execute("UPDATE sessions SET updated = ? WHERE id = ?",
                         (time.time(), self.id))
            conn.commit()
            self.reload()
        return len(stored)

    # --- reporting -------------------------------------------------------

    def files_touched(self) -> List[str]:
        """Every file this session snapshotted, i.e. every file it changed."""
        rows = self.store._query(
            "SELECT DISTINCT path FROM snapshots WHERE session_id = ? ORDER BY path",
            (self.id,))
        return [row["path"] for row in rows]

    def stats(self) -> Dict[str, Any]:
        """Everything a status view wants to say about this session."""
        rows = self.store._query(
            "SELECT seq, role, content, tool_calls, display, created "
            "FROM messages WHERE session_id = ? ORDER BY seq", (self.id,))
        roles: Dict[str, int] = {}
        tools: Dict[str, int] = {}
        first: Optional[float] = None
        last: Optional[float] = None
        for row in rows:
            roles[row["role"]] = roles.get(row["role"], 0) + 1
            for call in _deserialize_calls(row["tool_calls"]):
                tools[call.name] = tools.get(call.name, 0) + 1
            stamp = row["created"]
            if stamp:
                first = stamp if first is None else min(first, stamp)
                last = stamp if last is None else max(last, stamp)
        # Messages written before per-message timestamps existed carry none;
        # the session's own dates are the honest fallback.
        if rows and first is None:
            first, last = self.created or None, self.updated or None
        compactions = self.compactions()
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
            "archived": self.archived,
            "created": self.created,
            "updated": self.updated,
            "messages": len(rows),
            "roles": roles,
            "tools": tools,
            "tool_calls": sum(tools.values()),
            "first_message": first,
            "last_message": last,
            "files": self.files_touched(),
            "tokens": self.token_totals(),
            "compactions": len(compactions),
            "folded_messages": sum(item["folded"] for item in compactions),
        }

    # --- export ----------------------------------------------------------

    def export(self, fmt: str = "markdown") -> str:
        """Render the transcript. fmt is markdown, text or json."""
        name = (fmt or "markdown").strip().lower()
        if name in ("markdown", "md"):
            return self._export_markdown()
        if name in ("text", "txt", "plain"):
            return self._export_text()
        if name == "json":
            return self.export_json()
        raise ValueError("unknown export format: %s" % fmt)

    def export_json(self, indent: int = 2) -> str:
        """The transcript as JSON text, for tooling rather than for reading."""
        return json.dumps(self.export_data(), indent=indent, default=str)

    def export_data(self) -> Dict[str, Any]:
        """The dict export_json() serialises; useful on its own for the desktop UI."""
        messages = []
        for index, message in enumerate(self.messages):
            messages.append({
                "seq": self.seqs[index] if index < len(self.seqs) else index + 1,
                "role": message.role,
                "content": message.content or "",
                "tool_calls": [asdict(call) for call in message.tool_calls],
                "tool_call_id": message.tool_call_id or "",
                "display": message.display or {},
            })
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
            "created": self.created,
            "updated": self.updated,
            "archived": self.archived,
            "messages": messages,
            "stats": self.stats(),
        }

    def _header_lines(self) -> List[str]:
        model = "/".join(part for part in (self.provider, self.model) if part)
        rows = [("Session", self.id), ("Directory", self.cwd), ("Model", model),
                ("Created", _timestamp(self.created)),
                ("Updated", _timestamp(self.updated)),
                ("Messages", str(len(self.messages)))]
        return ["- %s: %s" % (label, value) for label, value in rows if value]

    def _export_markdown(self) -> str:
        out = ["# " + (self.title or "Untitled session"), ""]
        out.extend(self._header_lines())
        for message in self.messages:
            out.append("")
            if message.role == "user":
                out.append("## User")
                out.append("")
                out.append(message.content or "")
            elif message.role == "assistant":
                out.append("## Assistant")
                out.append("")
                if message.content:
                    out.append(message.content)
                for call in message.tool_calls:
                    out.append("")
                    out.append("**%s** `%s`" % (
                        call.name, json.dumps(call.arguments, default=str)))
            elif message.role == "tool":
                display = message.display or {}
                name = str(display.get("tool") or "tool")
                title = str(display.get("title") or "")
                heading = "### Tool: %s" % name
                if title:
                    heading += " - %s" % title
                out.append(heading)
                out.append("")
                diff = display.get("diff")
                if display.get("denied"):
                    out.append("_denied by the user_")
                elif isinstance(diff, str) and diff.strip():
                    # The diff is the readable form of an edit; the raw output
                    # of the tool repeats it less legibly.
                    out.append(_fence(diff, "diff"))
                else:
                    out.append(_fence(message.content or ""))
            else:
                out.append("## " + (message.role or "message").title())
                out.append("")
                out.append(message.content or "")
        return "\n".join(out).rstrip() + "\n"

    def _export_text(self) -> str:
        out = [self.title or "Untitled session"]
        out.extend(line[2:] for line in self._header_lines())
        for message in self.messages:
            display = message.display or {}
            label = message.role.upper()
            if message.role == "tool":
                label = "TOOL %s" % (display.get("tool") or "")
            out.append("")
            out.append("--- %s ---" % label.strip())
            if message.content:
                out.append(message.content)
            for call in message.tool_calls:
                out.append("%s(%s)" % (call.name,
                                       json.dumps(call.arguments, default=str)))
        return "\n".join(out).rstrip() + "\n"


def capture_modified(session: Session, ctx) -> List[str]:
    """
    Snapshot every file an agent run touched.

    `ctx` is a ToolContext: its `modified_files` maps absolute path -> the text
    the file held before the run, or None when the file was created. Returns the
    paths that were recorded.
    """
    recorded: List[str] = []
    for path, original in dict(getattr(ctx, "modified_files", {}) or {}).items():
        session.record_snapshot(path, original)
        recorded.append(os.path.realpath(path))
    return recorded


# --------------------------------------------------------------------------
# the model's window into earlier conversations
# --------------------------------------------------------------------------

# Enough of a transcript to reconstruct what happened without replaying tool
# output, which is the bulk of a session and the least useful part of it.
HISTORY_SESSIONS = 10
HISTORY_TURN_CHARS = 600
HISTORY_MAX_TURNS = 40


def _ago(when: float) -> str:
    """Coarse relative age. Exact timestamps invite arithmetic, not recall."""
    if not when:
        return "unknown"
    delta = max(0.0, time.time() - when)
    for seconds, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if delta >= seconds:
            return "%d%s ago" % (int(delta // seconds), unit)
    return "just now"


def summarize_transcript(messages: Sequence[Any],
                         turn_chars: int = HISTORY_TURN_CHARS,
                         max_turns: int = HISTORY_MAX_TURNS) -> str:
    """The conversation as alternating turns, tool traffic collapsed.

    Tool calls become one `[ran: name, name]` line rather than their output:
    a single grep can outweigh every word either party said, and what the
    reader needs is the shape of the work, not its intermediate data.
    """
    lines: List[str] = []
    pending_tools: List[str] = []

    def flush_tools():
        if pending_tools:
            names = ", ".join(dict.fromkeys(pending_tools))
            lines.append("  [ran: %s]" % names)
            pending_tools.clear()

    for message in messages:
        role = getattr(message, "role", "")
        content = (getattr(message, "content", "") or "").strip()
        if role == "tool":
            continue
        calls = getattr(message, "tool_calls", None) or []
        if role == "assistant":
            pending_tools.extend(getattr(call, "name", "?") for call in calls)
            if not content:
                continue
        if role in ("user", "assistant"):
            flush_tools()
            label = "user" if role == "user" else "assistant"
            text = content if len(content) <= turn_chars else \
                content[:turn_chars].rstrip() + " ..."
            lines.append("%s: %s" % (label, text.replace("\n", " ")))
    flush_tools()
    if len(lines) > max_turns:
        dropped = len(lines) - max_turns
        lines = ["[%d earlier lines omitted]" % dropped] + lines[-max_turns:]
    return "\n".join(lines)


class SessionHistoryTool(Tool):
    name = "session_history"
    # Reading the user's own past conversations is a read, not an action.
    permission = "read"
    description = (
        "Look up what earlier sessions in this workspace were about.\n\n"
        "Without arguments: the recent sessions, newest first. With `query`: "
        "sessions ranked against that text. With `session_id`: that session's "
        "transcript, tool output collapsed. Use it when the user refers to "
        "earlier work ('what did we do last time', 'continue where we left "
        "off') — the current conversation does not contain it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "Optional text to rank sessions against"},
            "session_id": {"type": "string",
                           "description": "Read one session's transcript"},
            "all_projects": {"type": "boolean",
                             "description": "Include sessions from other "
                                            "directories (default false)"},
        },
        "required": [],
    }

    def execute(self, args: Dict[str, Any], ctx: Any) -> ToolResult:
        query = str(args.get("query") or "").strip()
        session_id = str(args.get("session_id") or "").strip()
        everywhere = bool(args.get("all_projects"))
        ctx.ask("read", ["session history"], "Read earlier sessions",
                {"query": query, "session": session_id})

        # The live session's own store, never a second connection to the same
        # file. Opening one from inside the running agent ran schema DDL
        # against a database another connection already held, and on a failed
        # open the stale-WAL recovery would move that live WAL aside — which
        # is how a session store ended up reporting "database disk image is
        # malformed" mid-conversation. Only fall back to opening the default
        # store when there is no session yet (a tool call before the first
        # turn was persisted), where nothing else has it open.
        live = getattr(getattr(ctx, "session", None), "store", None)
        store = live if isinstance(live, SessionStore) else SessionStore()
        try:
            if session_id:
                return self._transcript(store, session_id)
            return self._listing(store, query, everywhere, ctx)
        finally:
            if store is not live:
                store.close()

    def _transcript(self, store: "SessionStore", session_id: str) -> ToolResult:
        session = store.load(session_id)
        if session is None:
            raise ValueError(
                "No session with id %r. Call session_history without "
                "arguments to see the ids that exist." % session_id)
        body = summarize_transcript(session.messages)
        header = "# %s\n%s · %s · %s\n" % (
            session.title or "(untitled)", session.cwd or "?",
            session.model or "?", _ago(session.updated))
        return ToolResult(
            title=session.title or session_id,
            output=header + "\n" + (body or "(no messages)"),
            metadata={"session": session_id,
                      "messages": len(session.messages)})

    def _listing(self, store: "SessionStore", query: str, everywhere: bool,
                 ctx: Any) -> ToolResult:
        # ctx.session is the live Session object, not an id — reading the
        # wrong attribute made the current conversation list itself as
        # "earlier work", which is exactly the confusion the tool exists to
        # remove.
        current = getattr(getattr(ctx, "session", None), "id", "") or ""
        if query:
            rows = store.search(query, limit=HISTORY_SESSIONS)
        else:
            rows = store.list_sessions(
                limit=HISTORY_SESSIONS,
                cwd=None if everywhere else getattr(ctx, "cwd", None))
        rows = [row for row in rows if row["id"] != current]
        if not rows:
            note = ("No earlier sessions match %r." % query if query
                    else "No earlier sessions in this workspace.")
            if not everywhere and not query:
                note += " Pass all_projects for other directories."
            return ToolResult(title="no sessions", output=note,
                              metadata={"count": 0})

        lines = []
        for row in rows:
            line = "%s  %s  (%d messages, %s)" % (
                row["id"], row["title"] or "(untitled)",
                row["message_count"], _ago(row["updated"]))
            if row.get("snippet"):
                line += "\n    ..." + row["snippet"].strip()
            lines.append(line)
        return ToolResult(
            title="%d session%s" % (len(rows), "" if len(rows) == 1 else "s"),
            output="\n".join(lines) +
                   "\n\nPass session_id to read one of these in full.",
            metadata={"count": len(rows)})


SESSION_TOOLS: List[Tool] = [SessionHistoryTool()]
