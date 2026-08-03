"""
read / write / edit — ported from opencode's tool contract.

Key behaviours kept identical to opencode:
- read returns "<line>: <content>" so the model can reason about line numbers
- edit refuses unless the file was read first, and refuses ambiguous matches
- write refuses to overwrite an unread existing file
- both show a unified diff before asking for permission
"""

import difflib
import errno
import os
import stat
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .. import haiku
from .base import Tool, ToolContext, ToolResult, load_prompt
from .diagnostics import append_diagnostics
from .patch import join_bom, split_bom
from .paths import assert_external_directory

DEFAULT_READ_LIMIT = 2000
MAX_LINE_LENGTH = 2000
MAX_OUTPUT = 60000
# A line budget alone is not a budget: 2000 lines of minified JavaScript is
# tens of megabytes in the model's context. Cap the bytes we hand back, and
# cap the bytes we are willing to walk through just to count lines.
MAX_READ_BYTES = 50 * 1024
MAX_SCAN_BYTES = 16 * 1024 * 1024

# st_mode kinds that must never be opened for reading. A FIFO with no writer
# blocks open() forever, a character device can stream without end, and a
# socket cannot be read this way at all. commands.py shipped this bug once
# (open() on a FIFO wedged the whole process); do not ship it again.
_UNREADABLE_KINDS = (
    (stat.S_ISFIFO, "named pipe (FIFO)"),
    (stat.S_ISCHR, "character device"),
    (stat.S_ISBLK, "block device"),
    (stat.S_ISSOCK, "socket"),
)


def _fsync_dir(directory: Path) -> None:
    """Make a completed rename durable; best-effort on filesystems without it.

    fsync of the file makes the *data* durable; only fsync of the directory
    makes the *name* pointing at it durable. Failure is swallowed — some
    filesystems refuse fsync on directories, and the write itself succeeded.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _file_kind(path: Path) -> str:
    """
    "file", "dir", or a human name for something that must not be opened.

    stat() never blocks, even on a FIFO with no writer, so this is safe to do
    before any open().
    """
    try:
        mode = os.stat(str(path)).st_mode
    except OSError as error:
        if error.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return "missing"
        raise
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    for test, name in _UNREADABLE_KINDS:
        if test(mode):
            return name
    return "special file"


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(4096)
    except OSError:
        return False


# --- serialising edits per file ----------------------------------------

# One lock per resolved path, ported from opencode's `locks` map in edit.ts.
# Read-modify-write is not atomic on its own: two tool calls in one turn that
# touch the same file would both read the old text, both apply their change to
# it, and the second write would silently drop the first. The map is never
# pruned — a Lock is a few dozen bytes and the key set is bounded by the files
# one run actually edits — but it must only ever be *grown* under a guard, or
# two threads racing on a new path would build two different locks for it.
_PATH_LOCKS: Dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


@contextmanager
def path_lock(path: Path) -> Iterator[None]:
    """Hold the write lock for one resolved path.

    Held across the permission prompt as well as the write, which is
    deliberate: releasing it to ask the user would reopen exactly the
    interleaving it exists to prevent.
    """
    key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = _PATH_LOCKS[key] = threading.Lock()
    with lock:
        yield


# --- text encoding and line endings ------------------------------------


def detect_newline(text: str) -> str:
    """The line ending to write `text` back with. opencode's rule exactly."""
    return "\r\n" if "\r\n" in text else "\n"


def normalize_newlines(text: str) -> str:
    """CRLF to LF, so matching and diffing see one canonical form."""
    return text.replace("\r\n", "\n")


def to_newline(text: str, ending: str) -> str:
    """Put LF text back into the file's own line ending."""
    return text.replace("\n", "\r\n") if ending == "\r\n" else text


def read_source(path: Path) -> Tuple[str, str, bool]:
    """Decode a file for editing: (LF-normalised text, line ending, had BOM).

    Strict UTF-8 is the whole point. With errors="replace" every byte the
    codec cannot read comes back as U+FFFD, and writing that string out again
    stores a literal EF BF BD in its place — so a single edit of a Latin-1 or
    UTF-16 file corrupts every non-ASCII byte in it, including the ones the
    edit never touched. There is no safe way to text-edit bytes we cannot
    read, so this refuses instead.

    The line ending is returned rather than normalised away because Python's
    universal-newline mode would otherwise turn a CRLF file into an LF file on
    the first edit, rewriting every line the model did not ask about.
    """
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            f"Refusing to edit {path.name}: it is not valid UTF-8 (invalid "
            f"byte at offset {error.start} of {len(raw)}). Editing it as text "
            "would replace every undecodable byte with U+FFFD and corrupt "
            "parts of the file the edit never touched.") from None
    body, bom = split_bom(text)
    return normalize_newlines(body), detect_newline(body), bom


# --- writing -----------------------------------------------------------


def _carry_metadata(before: os.stat_result, source: Path, tmp: Path) -> None:
    """Move the old inode's identity onto the replacement file.

    Each step is independent and swallows its own error: chown to another user
    needs privileges we normally do not have, and a filesystem may refuse a
    mode or a timestamp. None of that is a reason to fail an edit whose
    content has already been written correctly.

    Note that the timestamps are carried across deliberately, so a file keeps
    the dates Tracker shows. The cost is that mtime-comparing tools (`make`)
    will not notice an edit; git is unaffected because os.replace() gives the
    file a new inode and a new ctime, which the index also checks.

    Atime is best effort by nature: BFS ignores the atime that utime() is
    handed and leaves it at the current time, verified on hrev57937. mtime it
    honours, and that is the one anything actually reads.
    """
    try:
        os.chmod(str(tmp), stat.S_IMODE(before.st_mode))
    except OSError:
        pass
    chown = getattr(os, "chown", None)
    if chown is not None:
        try:
            chown(str(tmp), before.st_uid, before.st_gid)
        except OSError:
            pass          # not ours to give away — the ordinary, harmless case
    haiku.copy_attributes(source, tmp)          # a no-op off Haiku
    # Timestamps last, so nothing after this point can touch the file again
    # and undo them before the rename.
    try:
        os.utime(str(tmp), (before.st_atime, before.st_mtime))
    except OSError:
        pass


def atomic_write(path: Path, content: str,
                 source: Optional[Path] = None) -> None:
    """Replace `path` with `content` without discarding what the inode carries.

    A fresh mkstemp inode plus os.replace() is the only way to make the swap
    atomic, but the new inode starts life with none of the old one's identity:
    mode 0600, this process's ownership, fresh timestamps and — on BFS — not
    one extended attribute. The last part is the serious one on Haiku, where
    `BEOS:TYPE`, the Tracker metadata and every custom indexed attribute live
    on the inode: an editor that does not carry them forward strips a file's
    type, and drops it out of every saved query, on every single edit.

    All of it is therefore copied onto the temp file *before* the rename, so
    the swap stays atomic and the file is never visible half-restored.

    `source` names the file whose identity to carry forward when that is not
    `path` itself — a rename has to inherit from the file it came from, which
    no longer exists at the destination.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    origin = path if source is None else source
    try:
        before: Optional[os.stat_result] = os.stat(str(origin))
    except OSError:
        before = None          # a genuinely new file: nothing to carry forward

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        # newline="" so a CRLF file is written back byte for byte, and an
        # explicit encoding so the result never depends on the run's locale.
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            # Data to disk BEFORE the rename, or the swap is atomic in name
            # only. Measured in the field: a machine lost power right after
            # a report was written; BFS journaled the metadata, the data
            # blocks never flushed, and the file came back the right size
            # but full of another package's bytes. An acknowledged write
            # must survive the plug being pulled.
            f.flush()
            os.fsync(f.fileno())
        if before is not None:
            _carry_metadata(before, origin, Path(tmp))
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _diff(old: str, new: str, path: str) -> str:
    lines = list(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=path, tofile=path, n=3))
    text = "".join(lines)
    return text[:MAX_OUTPUT]


class ReadTool(Tool):
    name = "read"
    description = load_prompt("read.txt")
    permission = "read"
    parameters = {
        "type": "object",
        "properties": {
            "filePath": {"type": "string",
                         "description": "The absolute path to the file or directory to read"},
            "offset": {"type": "integer",
                       "description": "The line number to start reading from (1-indexed)"},
            "limit": {"type": "integer",
                      "description": "The maximum number of lines to read (defaults to 2000)"},
        },
        "required": ["filePath"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args["filePath"])
        offset = max(1, int(args.get("offset") or 1))
        limit = int(args.get("limit") or DEFAULT_READ_LIMIT)

        kind = _file_kind(path)
        if kind == "missing":
            # Same helpful nudge opencode gives: suggest near-misses
            siblings = []
            if path.parent.is_dir():
                stem = path.name.lower()
                siblings = [e.name for e in path.parent.iterdir()
                            if stem[:4] and stem[:4] in e.name.lower()][:5]
            hint = f" Did you mean: {', '.join(siblings)}?" if siblings else ""
            raise FileNotFoundError(f"File not found: {path}.{hint}")

        # Reading outside the session directory is not forbidden, but it is
        # not free either: the user approves the containing directory once.
        # ctx.resolve() has already followed symlinks, so a link out of the
        # tree lands here too.
        assert_external_directory(ctx, path,
                                  kind="directory" if kind == "dir" else "file",
                                  action="Read")
        ctx.ask("read", [ctx.relative(path)], f"Read {ctx.relative(path)}")

        if kind == "dir":
            entries = []
            for entry in sorted(path.iterdir(), key=lambda e: e.name):
                entries.append(entry.name + ("/" if entry.is_dir() else ""))
            shown = entries[offset - 1: offset - 1 + limit]
            out = "\n".join(shown) or "(empty directory)"
            return ToolResult(
                title=ctx.relative(path),
                output=out,
                metadata={"type": "directory", "total": len(entries)})

        if kind != "file":
            # Never open() this: see _UNREADABLE_KINDS.
            raise ValueError(
                f"Refusing to read {ctx.relative(path)}: it is a {kind}, not a "
                "regular file. Reading it could block forever or never end.")

        if _is_binary(path):
            size = path.stat().st_size
            return ToolResult(
                title=ctx.relative(path),
                output=f"[binary file, {size} bytes — not shown]",
                metadata={"type": "binary", "size": size})

        chunk, total, more, capped, scan_capped = self._read_lines(
            path, offset, limit, ctx)

        rendered = [f"{i}: {line}" for i, line in enumerate(chunk, start=offset)]
        out = "\n".join(rendered)

        end = offset - 1 + len(chunk)
        if capped:
            out += (f"\n\n[stopped at {MAX_READ_BYTES // 1024} KB; showing "
                    f"lines {offset}-{end}; call read again with "
                    f"offset={end + 1} for more]")
        elif more:
            total_label = f"{total}+" if scan_capped else str(total)
            out += (f"\n\n[showing lines {offset}-{end} of {total_label}; "
                    f"call read again with offset={end + 1} for more]")
        if not chunk:
            out = (f"[file has {total} lines; offset {offset} is past the end]")

        ctx.read_files.add(str(path))
        return ToolResult(
            title=ctx.relative(path),
            output=out,
            metadata={"type": "file", "lines": total,
                      "lineStart": offset, "lineEnd": end,
                      "truncated": bool(capped or more)})

    @staticmethod
    def _read_lines(path: Path, offset: int, limit: int, ctx: ToolContext):
        """
        Stream the file instead of slurping it: a 4 GB log must cost us one
        buffer, not 4 GB of RAM. Returns
        (lines, total_lines, more, byte_capped, scan_capped).
        """
        chunk: List[str] = []
        total = 0
        used = 0
        more = False
        capped = False
        scanned = 0
        scan_capped = False

        # Lenient here and only here: read only ever renders, so a stray byte
        # costs one U+FFFD on screen. The write path decodes strictly, because
        # there the same byte would be stored back. Naming the encoding keeps
        # the rendering identical whatever locale the run inherited.
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                total += 1
                scanned += len(raw)
                if total < offset:
                    if scanned > MAX_SCAN_BYTES:
                        scan_capped = True
                        break
                    continue
                if len(chunk) >= limit:
                    more = True
                    # Keep counting lines so the "of N" hint is true, but stop
                    # if the file is so large that counting is itself a stall.
                    if scanned > MAX_SCAN_BYTES:
                        scan_capped = True
                        break
                    if total % 4096 == 0:
                        ctx.check_abort()
                    continue

                line = raw.rstrip("\n").rstrip("\r")
                if len(line) > MAX_LINE_LENGTH:
                    line = line[:MAX_LINE_LENGTH] + "… [truncated]"
                cost = len(line.encode("utf-8", errors="replace")) + 1
                if used + cost > MAX_READ_BYTES and chunk:
                    capped = True
                    more = True
                    break
                chunk.append(line)
                used += cost

        return chunk, total, more, capped, scan_capped


class WriteTool(Tool):
    name = "write"
    description = load_prompt("write.txt")
    permission = "write"
    parameters = {
        "type": "object",
        "properties": {
            "filePath": {"type": "string",
                         "description": "The absolute path to the file to write (must be absolute, not relative)"},
            "content": {"type": "string", "description": "The content to write to the file"},
        },
        "required": ["filePath", "content"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args["filePath"])
        content = args.get("content", "")
        rel = ctx.relative(path)

        with path_lock(path):
            return self._write(path, rel, content, ctx)

    def _write(self, path: Path, rel: str, content: str,
               ctx: ToolContext) -> ToolResult:
        exists = path.exists()

        if exists and str(path) not in ctx.read_files:
            raise ValueError(
                f"{rel} exists but has not been read. Use the read tool first "
                "so you do not overwrite content you have not seen.")

        old, old_bom = "", False
        if exists:
            try:
                old, _, old_bom = read_source(path)
            except ValueError:
                # A full rewrite derives nothing from the old bytes, so an
                # undecodable file is still legal to replace outright — we
                # just have no diff to show against it.
                pass
        body, new_bom = split_bom(content)
        diff = _diff(old, normalize_newlines(body), rel)

        assert_external_directory(ctx, path, action="Write")
        # always=["*"]: approving one write grants writes generally (opencode
        # behaviour) — otherwise the user is re-prompted for every new file.
        ctx.ask("write", [rel], f"{'Overwrite' if exists else 'Create'} {rel}",
                {"diff": diff, "path": str(path)}, always=["*"])

        ctx.record_original(path)
        # An existing BOM survives a rewrite that did not mention one, exactly
        # as opencode's `source.bom || next.bom` does.
        atomic_write(path, join_bom(body, old_bom or new_bom))
        ctx.read_files.add(str(path))

        added = len(content.splitlines())
        output = f"{'Updated' if exists else 'Created'} {rel} ({added} lines)"
        output = append_diagnostics(ctx, path, output, label=rel)
        return ToolResult(
            title=rel,
            output=output,
            metadata={"diff": diff, "path": str(path)})


class EditTool(Tool):
    name = "edit"
    description = load_prompt("edit.txt")
    permission = "edit"
    parameters = {
        "type": "object",
        "properties": {
            "filePath": {"type": "string",
                         "description": "The absolute path to the file to modify"},
            "oldString": {"type": "string", "description": "The text to replace"},
            "newString": {"type": "string",
                          "description": "The text to replace it with (must be different from oldString)"},
            "replaceAll": {"type": "boolean",
                           "description": "Replace all occurrences of oldString (default false)"},
        },
        "required": ["filePath", "oldString", "newString"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(args["filePath"])
        old_string = args["oldString"]
        new_string = args["newString"]
        replace_all = bool(args.get("replaceAll"))
        rel = ctx.relative(path)

        if old_string == new_string:
            raise ValueError("No changes to apply: oldString and newString are identical.")

        with path_lock(path):
            return self._edit(path, rel, old_string, new_string, replace_all, ctx)

    def _edit(self, path: Path, rel: str, old_string: str, new_string: str,
              replace_all: bool, ctx: ToolContext) -> ToolResult:
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if str(path) not in ctx.read_files:
            raise ValueError(
                f"{rel} has not been read. Use the read tool before editing it.")

        # Raises rather than corrupting when the file is not valid UTF-8.
        content, ending, bom = read_source(path)
        # The model always writes LF; the file may not. Matching happens in the
        # normalised form and only the result is put back into the file's own
        # ending, so a CRLF file stays CRLF instead of being silently converted.
        old_string = normalize_newlines(old_string)
        new_string = normalize_newlines(new_string)

        count = content.count(old_string)
        if count == 0:
            raise ValueError(f"oldString not found in content of {rel}")
        if count > 1 and not replace_all:
            raise ValueError(
                f"Found {count} matches for oldString in {rel}. Provide more "
                "surrounding lines to identify the correct match, or set "
                "replaceAll to change every instance.")

        updated = (content.replace(old_string, new_string) if replace_all
                   else content.replace(old_string, new_string, 1))
        diff = _diff(content, updated, rel)

        assert_external_directory(ctx, path, action="Edit")
        ctx.ask("edit", [rel], f"Edit {rel}",
                {"diff": diff, "path": str(path)}, always=["*"])

        ctx.record_original(path)
        atomic_write(path, join_bom(to_newline(updated, ending), bom))

        replaced = count if replace_all else 1
        output = (f"Edited {rel} ({replaced} "
                  f"replacement{'s' if replaced != 1 else ''})\n\n{diff}")
        output = append_diagnostics(ctx, path, output, label=rel)
        return ToolResult(
            title=rel,
            output=output,
            metadata={"diff": diff, "path": str(path), "replacements": replaced})
