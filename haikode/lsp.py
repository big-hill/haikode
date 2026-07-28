"""
Language Server Protocol client — stdlib only.

Ported from opencode's packages/opencode/src/lsp: one server per language per
workspace, files announced with didOpen/didChange, and the server pushes
textDocument/publishDiagnostics back. The edit/write flow appends those
diagnostics so the model immediately sees errors it just introduced.

LSP is strictly an enhancement. Haiku usually has no language server installed,
so every entry point degrades to an empty result rather than raising — and every
wait has a deadline, because a language server that hangs must never hang the
agent.

The entry point tools should call is `diagnostics_block(ctx, path)`: it returns
"" when no manager is attached, no server is installed, or the server has
nothing to say, and it never raises and never blocks past its deadline.
"""

import functools
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

DEFAULT_REQUEST_TIMEOUT = 10.0
INITIALIZE_TIMEOUT = 30.0
SHUTDOWN_TIMEOUT = 2.0
PROCESS_EXIT_TIMEOUT = 2.0
READER_JOIN_TIMEOUT = 2.0
DEFAULT_DIAGNOSTICS_WAIT = 2.0
MAX_DIAGNOSTICS = 20
READ_CHUNK = 65536
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
# decode_messages refuses any body longer than MAX_MESSAGE_BYTES, so a buffer
# past this size means the stream holds no usable framing at all.
MAX_BUFFER_BYTES = MAX_MESSAGE_BYTES + READ_CHUNK

# LSP DiagnosticSeverity -> the names we expose.
SEVERITY_NAMES = {1: "error", 2: "warning", 3: "info", 4: "hint"}
SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2, "hint": 3}
# What LSPManager reports by default. Hints and infos are mostly style nags
# from an over-eager server; surfacing them after every edit trains the model
# to chase noise, and they cost tokens on every single tool result.
DEFAULT_MIN_SEVERITY = "warning"

TEXT_DOCUMENT_SYNC_INCREMENTAL = 2

# JSON-RPC error code for a method we do not implement.
METHOD_NOT_FOUND = -32601

_WHITESPACE = re.compile(r"\s+")

# Subset of opencode's lsp/language.ts — enough to pick a server and to send a
# correct languageId in didOpen.
LANGUAGE_EXTENSIONS: Dict[str, str] = {
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".c++": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".dart": "dart",
    ".go": "go",
    ".hs": "haskell",
    ".html": "html",
    ".htm": "html",
    ".java": "java",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascriptreact",
    ".json": "json",
    ".kt": "kotlin",
    ".lua": "lua",
    ".md": "markdown",
    ".mm": "objective-cpp",
    ".m": "objective-c",
    ".php": "php",
    ".py": "python",
    ".pyi": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".scss": "scss",
    ".sh": "shellscript",
    ".bash": "shellscript",
    ".sql": "sql",
    ".swift": "swift",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescriptreact",
    ".toml": "toml",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".zig": "zig",
}

# language -> candidate commands, tried in order. The first one whose binary is
# on PATH wins; on Haiku that is usually none of them, which is a clean no-op.
#
# ccls leads the C/C++ list because it is the one language server Haiku's
# package repository actually ships (`pkgman install ccls`); clangd only exists
# on a box where somebody built LLVM's extra tools by hand.
SERVERS: Dict[str, List[List[str]]] = {
    "python": [["pylsp"], ["pyright-langserver", "--stdio"], ["jedi-language-server"]],
    "c": [["ccls"], ["clangd"]],
    "cpp": [["ccls"], ["clangd"]],
    "objective-c": [["ccls"], ["clangd"]],
    "objective-cpp": [["ccls"], ["clangd"]],
    "typescript": [["typescript-language-server", "--stdio"]],
    "typescriptreact": [["typescript-language-server", "--stdio"]],
    "javascript": [["typescript-language-server", "--stdio"]],
    "javascriptreact": [["typescript-language-server", "--stdio"]],
    "rust": [["rust-analyzer"]],
    "go": [["gopls"]],
    "zig": [["zls"]],
    "lua": [["lua-language-server"]],
    "ruby": [["solargraph", "stdio"]],
}


class LSPError(Exception):
    """Any failure talking to a language server."""


class LSPTimeout(LSPError):
    """A request outlived its deadline; the server is slow, wedged or dead."""


# --- pure helpers (framing, paths, formatting) -------------------------

def encode_message(payload: Dict[str, Any]) -> bytes:
    """Serialise one JSON-RPC message with LSP's Content-Length framing."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    header = "Content-Length: %d\r\n\r\n" % len(body)
    return header.encode("ascii") + body


def split_header(buffer: bytes) -> Tuple[Optional[bytes], int]:
    """
    Locate the header block. Returns (header_bytes, body_offset), or
    (None, -1) while the block is still incomplete. Bare-LF separators are
    accepted because a few servers emit them.
    """
    best, skip = -1, 0
    for marker in (b"\r\n\r\n", b"\n\n"):
        index = buffer.find(marker)
        if index >= 0 and (best < 0 or index < best):
            best, skip = index, len(marker)
    if best < 0:
        return None, -1
    return buffer[:best], best + skip


def parse_headers(raw: bytes) -> Dict[str, str]:
    """Parse an LSP header block into a lowercase-keyed dict."""
    headers: Dict[str, str] = {}
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line or b":" not in line:
            continue
        name, _, value = line.partition(b":")
        key = name.strip().decode("ascii", errors="replace").lower()
        headers[key] = value.strip().decode("ascii", errors="replace")
    return headers


def decode_messages(buffer: bytes) -> Tuple[List[Dict[str, Any]], bytes]:
    """
    Pull every complete message out of `buffer`.

    Returns (messages, remainder). Garbage is dropped rather than retried, so a
    server that writes noise to stdout cannot wedge the reader in a loop.
    """
    messages: List[Dict[str, Any]] = []
    while True:
        header_raw, offset = split_header(buffer)
        if header_raw is None:
            return messages, buffer
        headers = parse_headers(header_raw)
        try:
            length = int(headers["content-length"])
        except (KeyError, ValueError):
            buffer = buffer[offset:]
            continue
        if length < 0 or length > MAX_MESSAGE_BYTES:
            # A nonsense length would otherwise grow the buffer forever waiting
            # for bytes that are never coming: drop it and resynchronise.
            buffer = buffer[offset:]
            continue
        if len(buffer) - offset < length:
            return messages, buffer
        body = buffer[offset:offset + length]
        buffer = buffer[offset + length:]
        try:
            decoded = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(decoded, dict):
            messages.append(decoded)


def pump_messages(read_chunk: Callable[[int], bytes],
                  on_message: Callable[[Dict[str, Any]], None],
                  chunk_size: int = READ_CHUNK) -> None:
    """
    Drive the framing loop until `read_chunk` returns b"" (EOF).

    Split out from LSPClient so the framing can be tested against a plain pipe,
    and so a handler that throws cannot kill the reader thread.
    """
    buffer = b""
    while True:
        try:
            chunk = read_chunk(chunk_size)
        except (OSError, ValueError):
            return
        if not chunk:
            return
        buffer += chunk
        if len(buffer) > MAX_BUFFER_BYTES:
            # No usable header in 32MB of output: this process is not speaking
            # LSP. Drop the buffer rather than growing it until memory runs out.
            buffer = b""
            continue
        messages, buffer = decode_messages(buffer)
        for message in messages:
            try:
                on_message(message)
            except Exception:
                continue


def path_to_uri(path: Any) -> str:
    """Absolute path -> file:// URI."""
    absolute = os.path.abspath(str(path))
    return "file://" + urllib.request.pathname2url(absolute)


def uri_to_path(uri: str) -> str:
    """file:// URI -> path. Non-file URIs are returned unchanged."""
    parts = urllib.parse.urlsplit(uri)
    if parts.scheme != "file":
        return uri
    return urllib.request.url2pathname(parts.path)


def normalize_path(path: Any) -> str:
    """The one spelling of a path used as a diagnostics/document key."""
    return os.path.normpath(os.path.abspath(str(path)))


def detect_language(path: Any) -> Optional[str]:
    """LSP languageId for a file, by extension."""
    return LANGUAGE_EXTENSIONS.get(Path(str(path)).suffix.lower())


@functools.lru_cache(maxsize=256)
def _which(binary: str) -> Optional[str]:
    """
    PATH lookup, memoised for the life of the process.

    detect_server() is on the hot path of every edit, and on Haiku the answer
    is almost always "not installed" — which is the expensive answer, because a
    miss walks every PATH entry. Caching turns the steady state into a dict hit.
    """
    return shutil.which(binary)


def clear_binary_cache() -> None:
    """Forget memoised PATH lookups (a server installed mid-session, or a test)."""
    _which.cache_clear()


def detect_server(path: Any) -> Optional[List[str]]:
    """
    Command that can serve this file, or None when nothing is installed.
    The binary is resolved through PATH so the caller gets an absolute argv[0].

    Costs nothing but memoised PATH lookups: no process is ever started here.
    """
    language = detect_language(path)
    if not language:
        return None
    for command in SERVERS.get(language, []):
        binary = _which(command[0])
        if binary:
            return [binary] + list(command[1:])
    return None


def end_position(text: str) -> Dict[str, int]:
    """Position just past the last character — the end of a whole-file range."""
    lines = text.split("\n")
    return {"line": len(lines) - 1, "character": len(lines[-1])}


def normalize_diagnostic(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    LSP Diagnostic -> our flat shape. Line and character are 1-based here
    (LSP counts from 0) because the only consumer is human/model-facing text.

    The message is flattened to a single line: servers happily embed newlines
    (TypeScript writes multi-line type mismatches) and a diagnostics block that
    is one-record-per-line stays greppable and countable.
    """
    start = (raw.get("range") or {}).get("start") or {}
    severity = raw.get("severity")
    try:
        line = int(start.get("line") or 0)
        character = int(start.get("character") or 0)
    except (TypeError, ValueError):
        line, character = 0, 0
    message = _WHITESPACE.sub(" ", str(raw.get("message") or "")).strip()
    return {
        "severity": SEVERITY_NAMES.get(severity, "error"),
        "line": max(1, line + 1),
        "character": max(1, character + 1),
        "message": message,
        "source": str(raw.get("source") or ""),
    }


def _diagnostic_key(item: Dict[str, Any]) -> Tuple:
    return (item.get("severity", "error"), item.get("line", 0),
            item.get("character", 0), item.get("message", ""),
            item.get("source", ""))


def filter_diagnostics(diags: Iterable[Dict[str, Any]],
                       min_severity: Optional[str] = None,
                       ) -> List[Dict[str, Any]]:
    """
    Apply the severity floor, drop duplicates, and sort into a stable order.

    Duplicates are real: several servers can serve one language, and a single
    server will happily report the same unused import from two analysers. The
    sort key includes the message and source, so the same input always produces
    byte-identical output — a diagnostics block that reshuffles between runs
    looks like new information to the model.
    """
    floor = SEVERITY_ORDER.get(min_severity or "", None)
    seen = set()
    kept: List[Dict[str, Any]] = []
    for item in diags or []:
        if not isinstance(item, dict):
            continue
        rank = SEVERITY_ORDER.get(item.get("severity", "error"), 9)
        if floor is not None and rank > floor:
            continue
        key = _diagnostic_key(item)
        if key in seen:
            continue
        seen.add(key)
        kept.append(item)
    kept.sort(key=lambda d: (SEVERITY_ORDER.get(d.get("severity", "error"), 9),
                             d.get("line", 0), d.get("character", 0),
                             d.get("message", ""), d.get("source", "")))
    return kept


def format_diagnostics(diags: List[Dict[str, Any]], path: str,
                       limit: int = MAX_DIAGNOSTICS,
                       min_severity: Optional[str] = None) -> str:
    """
    Compact block to append to a tool result, most severe first:

        src/x.py:12:5 error: undefined name 'foo' (pyflakes)

    Deliberately shaped like compiler output rather than XML: it is the format
    every model has seen a million times, and a human reading the transcript
    can paste a line straight into their editor's jump-to-error.
    """
    ordered = filter_diagnostics(diags, min_severity)
    if not ordered:
        return ""
    lines = []
    for item in ordered[:limit]:
        source = " (%s)" % item["source"] if item.get("source") else ""
        lines.append("%s:%s:%s %s: %s%s" % (
            path, item.get("line", 0), item.get("character", 0),
            item.get("severity", "error"), item.get("message", ""), source))
    remaining = len(ordered) - limit
    if remaining > 0:
        lines.append("... and %d more" % remaining)
    return "\n".join(lines)


# --- the client --------------------------------------------------------

class LSPClient:
    """
    One language server subprocess, spoken to over stdio.

    Threading model: a single daemon reader thread owns stdout and fans messages
    out to waiting requesters (correlated by JSON-RPC id) and to the diagnostics
    store. Writers serialise on a lock. Nothing blocks forever: requests carry a
    deadline and the reader releases every waiter when the process dies.
    """

    def __init__(self, command: List[str], root: str = ".",
                 env: Optional[Dict[str, str]] = None,
                 request_timeout: float = DEFAULT_REQUEST_TIMEOUT):
        self.command = list(command)
        self.root = str(Path(root).resolve())
        self.env = env
        self.request_timeout = request_timeout
        self.process: Optional[subprocess.Popen] = None
        self.capabilities: Dict[str, Any] = {}
        self.initialized = False

        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        # Held across Popen so close() can never run *between* the "are we
        # closed?" check and the spawn — that window is exactly how a shutdown
        # leaves a language server running on the user's machine.
        self._spawn_lock = threading.Lock()
        self._next_id = 0
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._diagnostics: Dict[str, List[Dict[str, Any]]] = {}
        self._publishes: Dict[str, int] = {}
        self._documents: Dict[str, Dict[str, Any]] = {}
        self._reader: Optional[threading.Thread] = None
        self._dead = False
        self._closed = False

    # --- lifecycle ----------------------------------------------------

    def start(self) -> "LSPClient":
        """Spawn the server and complete the initialize/initialized handshake."""
        environment = None
        if self.env:
            environment = dict(os.environ)
            environment.update(self.env)
        with self._spawn_lock:
            if self._closed:
                raise LSPError("%s was shut down before it started"
                               % self.command[0])
            try:
                # stderr goes to /dev/null: nobody drains it, and a full stderr
                # pipe would deadlock the server mid-request.
                self.process = subprocess.Popen(
                    self.command, cwd=self.root, env=environment,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=0)
            except OSError as e:
                raise LSPError("failed to start %s: %s" % (self.command[0], e))

            self._reader = threading.Thread(
                target=self._read_loop, daemon=True,
                name="lsp-%s" % Path(self.command[0]).name)
            self._reader.start()
        try:
            self._handshake()
        except BaseException:
            # A server that fails or times out during initialize is already
            # running; the caller only sees the exception and drops us, so it
            # must be reaped here or it lingers for the life of the session.
            self.close()
            raise
        return self

    def _read_loop(self) -> None:
        stream = self.process.stdout if self.process else None
        try:
            if stream is not None:
                pump_messages(stream.read, self._dispatch)
        finally:
            # The server is gone: nobody will ever answer the in-flight
            # requests, so wake every waiter instead of letting them time out.
            with self._condition:
                self._dead = True
                for slot in self._pending.values():
                    slot["message"] = {"error": {"message": "language server exited"}}
                    slot["event"].set()
                self._condition.notify_all()

    def close(self) -> None:
        """Shut the server down politely, then make sure the process is gone."""
        with self._spawn_lock:
            if self._closed:
                return
            self._closed = True
            process = self.process
        if process is None:
            self._join_reader()
            return
        if self.initialized and process.poll() is None:
            # didClose what we opened first: a server that keeps per-document
            # state (rust-analyzer, ccls) otherwise carries a stale copy of the
            # buffer into whatever opens the file next.
            for path in self.open_files():
                try:
                    self.close_file(path)
                except LSPError:
                    break
            try:
                self.request("shutdown", None, timeout=SHUTDOWN_TIMEOUT)
            except LSPError:
                pass
            try:
                self.notify("exit")
            except LSPError:
                pass
        # stdin first (that is the server's EOF signal), stdout only once the
        # process is gone and the reader has finished: the reader thread is
        # parked inside stdout.read(), and pulling a pipe out from under a
        # blocked reader is how clean exits turn into hangs.
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=PROCESS_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=PROCESS_EXIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=PROCESS_EXIT_TIMEOUT)
                except subprocess.TimeoutExpired:
                    pass
        self._join_reader()
        try:
            if process.stdout is not None:
                process.stdout.close()
        except OSError:
            pass
        with self._condition:
            self._dead = True
            self._documents.clear()
            self._condition.notify_all()
        self.initialized = False

    def _join_reader(self) -> None:
        """
        Wait for the reader thread to notice EOF.

        Without this, close() returns while a thread is still parked on a pipe
        we are about to close — and "no threads survive close()" stops being
        something anyone can assert.
        """
        thread = self._reader
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=READER_JOIN_TIMEOUT)

    @property
    def alive(self) -> bool:
        return (self.process is not None and self.process.poll() is None
                and not self._dead)

    # --- transport ----------------------------------------------------

    def _write(self, payload: Dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise LSPError("language server is not running")
        data = encode_message(payload)
        with self._write_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (OSError, ValueError) as e:
                raise LSPError("write to language server failed: %s" % e)

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def request(self, method: str, params: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> Any:
        """Send a request and block for its response, or raise LSPTimeout."""
        deadline = self.request_timeout if timeout is None else timeout
        with self._condition:
            if self._dead:
                raise LSPError("language server is not running")
            self._next_id += 1
            ident = self._next_id
            slot: Dict[str, Any] = {"event": threading.Event(), "message": None}
            self._pending[ident] = slot

        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._write(payload)
        except LSPError:
            with self._condition:
                self._pending.pop(ident, None)
            raise

        got = slot["event"].wait(deadline)
        with self._condition:
            self._pending.pop(ident, None)
        if not got:
            raise LSPTimeout("%s timed out after %.1fs" % (method, deadline))
        message = slot["message"] or {}
        if "error" in message:
            error = message["error"] or {}
            raise LSPError("%s failed: %s" % (method, error.get("message", error)))
        return message.get("result")

    def _dispatch(self, message: Dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            self._handle_server_request(message)
        elif "id" in message:
            self._handle_response(message)
        elif "method" in message:
            self._handle_notification(message)

    def _handle_response(self, message: Dict[str, Any]) -> None:
        with self._condition:
            slot = self._pending.get(message.get("id"))
            if slot is None:
                return
            slot["message"] = message
        slot["event"].set()

    def _handle_server_request(self, message: Dict[str, Any]) -> None:
        """
        Servers block on their own requests, so every one must get an answer —
        a real result where it is cheap, method-not-found otherwise.
        """
        method = message.get("method")
        params = message.get("params") or {}
        result: Any = None
        error: Optional[Dict[str, Any]] = None

        if method == "workspace/workspaceFolders":
            result = [{"name": "workspace", "uri": path_to_uri(self.root)}]
        elif method == "workspace/configuration":
            items = params.get("items") if isinstance(params, dict) else None
            result = [None] * len(items if isinstance(items, list) and items else [{}])
        elif method in ("client/registerCapability", "client/unregisterCapability",
                        "window/workDoneProgress/create",
                        "workspace/diagnostic/refresh",
                        "workspace/semanticTokens/refresh",
                        "workspace/codeLens/refresh",
                        "workspace/inlayHint/refresh"):
            result = None
        elif method == "workspace/applyEdit":
            # We never let a server rewrite files behind the user's back.
            result = {"applied": False, "failureReason": "not supported"}
        else:
            error = {"code": METHOD_NOT_FOUND, "message": "unsupported: %s" % method}

        reply: Dict[str, Any] = {"jsonrpc": "2.0", "id": message.get("id")}
        if error is not None:
            reply["error"] = error
        else:
            reply["result"] = result
        try:
            self._write(reply)
        except LSPError:
            pass

    def _handle_notification(self, message: Dict[str, Any]) -> None:
        if message.get("method") != "textDocument/publishDiagnostics":
            return
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return
        uri = params.get("uri")
        if not isinstance(uri, str):
            return
        items = params.get("diagnostics")
        version = params.get("version")
        key = normalize_path(uri_to_path(uri))
        with self._condition:
            document = self._documents.get(key)
            if (isinstance(version, int) and document is not None
                    and version < document["version"]):
                # A publish for a buffer we have already replaced. Keeping it
                # would report errors against text the server no longer holds
                # and the user never wrote.
                return
            self._diagnostics[key] = list(items) if isinstance(items, list) else []
            self._publishes[key] = self._publishes.get(key, 0) + 1
            self._condition.notify_all()

    # --- handshake ----------------------------------------------------

    def _handshake(self) -> None:
        result = self.request("initialize", {
            "processId": os.getpid(),
            "rootPath": self.root,
            "rootUri": path_to_uri(self.root),
            "clientInfo": {"name": "haikode", "version": "1.0"},
            "workspaceFolders": [{"name": "workspace", "uri": path_to_uri(self.root)}],
            "initializationOptions": {},
            "capabilities": {
                "window": {"workDoneProgress": True},
                "workspace": {
                    "configuration": True,
                    "workspaceFolders": True,
                    "didChangeConfiguration": {"dynamicRegistration": True},
                    "didChangeWatchedFiles": {"dynamicRegistration": True},
                },
                "textDocument": {
                    "synchronization": {
                        "dynamicRegistration": False,
                        "didSave": False,
                        "willSave": False,
                    },
                    "publishDiagnostics": {
                        "relatedInformation": False,
                        "versionSupport": False,
                    },
                },
            },
        }, timeout=INITIALIZE_TIMEOUT)
        capabilities = (result or {}).get("capabilities")
        self.capabilities = capabilities if isinstance(capabilities, dict) else {}
        self.notify("initialized", {})
        self.initialized = True

    def _sync_kind(self) -> Optional[int]:
        sync = self.capabilities.get("textDocumentSync")
        if isinstance(sync, int):
            return sync
        if isinstance(sync, dict):
            change = sync.get("change")
            return change if isinstance(change, int) else None
        return None

    # --- documents ----------------------------------------------------

    def open_files(self) -> List[str]:
        """Snapshot of the paths the server currently believes are open."""
        with self._condition:
            return sorted(self._documents)

    def is_open(self, path: Any) -> bool:
        with self._condition:
            return normalize_path(path) in self._documents

    def _read_text(self, resolved: str) -> str:
        try:
            return Path(resolved).read_text(errors="replace")
        except OSError as e:
            raise LSPError("cannot read %s: %s" % (resolved, e))

    def open_file(self, path: Any, text: Optional[str] = None) -> int:
        """
        didOpen the file, or didChange it when it is already open.
        Returns the document version the server now holds.

        didOpen strictly precedes didChange: a server that receives didChange
        for a document it never opened is entitled to ignore it, close the
        connection, or (rust-analyzer) panic.
        """
        resolved = normalize_path(path)
        with self._condition:
            already = resolved in self._documents
        if already:
            return self.change_file(resolved, text)
        if text is None:
            text = self._read_text(resolved)

        with self._condition:
            if resolved in self._documents:
                # Another thread opened it while we were reading the file.
                already = True
            else:
                already = False
                # Only the end position is kept, never the text: the sole
                # consumer is the whole-document range below, and holding every
                # file a long session touches would grow without bound.
                self._documents[resolved] = {"version": 0,
                                             "end": end_position(text)}
        if already:
            return self.change_file(resolved, text)

        try:
            self.notify("textDocument/didOpen", {
                "textDocument": {
                    "uri": path_to_uri(resolved),
                    "languageId": detect_language(resolved) or "plaintext",
                    "version": 0,
                    "text": text,
                },
            })
        except LSPError:
            # The server never saw the didOpen, so it must not stay in our
            # book-keeping — otherwise the next edit sends a didChange for a
            # document that was never opened.
            with self._condition:
                self._documents.pop(resolved, None)
            raise
        return 0

    def change_file(self, path: Any, text: Optional[str] = None) -> int:
        """didChange an already-open file; returns the new version."""
        resolved = normalize_path(path)
        with self._condition:
            document = self._documents.get(resolved)
        if document is None:
            return self.open_file(resolved, text)
        if text is None:
            text = self._read_text(resolved)

        with self._condition:
            document = self._documents.get(resolved)
            if document is not None:
                # Claim the version inside the critical section so two threads
                # editing one file cannot both send version N.
                version = document["version"] + 1
                previous_end = document["end"]
                self._documents[resolved] = {"version": version,
                                             "end": end_position(text)}
        if document is None:
            # close_file() ran between our two critical sections.
            return self.open_file(resolved, text)

        if self._sync_kind() == TEXT_DOCUMENT_SYNC_INCREMENTAL:
            # "Incremental" servers still accept one change spanning the whole
            # document, which keeps us out of the diffing business.
            changes = [{
                "range": {"start": {"line": 0, "character": 0},
                          "end": previous_end},
                "text": text,
            }]
        else:
            changes = [{"text": text}]
        self.notify("textDocument/didChange", {
            "textDocument": {"uri": path_to_uri(resolved), "version": version},
            "contentChanges": changes,
        })
        return version

    def close_file(self, path: Any) -> None:
        resolved = normalize_path(path)
        with self._condition:
            if self._documents.pop(resolved, None) is None:
                return
        try:
            self.notify("textDocument/didClose", {
                "textDocument": {"uri": path_to_uri(resolved)},
            })
        except LSPError:
            pass

    # --- diagnostics --------------------------------------------------

    def acknowledged(self, path: Any) -> bool:
        """True once the server has published diagnostics for this file."""
        with self._condition:
            return self._publishes.get(normalize_path(path), 0) > 0

    def diagnostics(self, path: Any,
                    wait: float = DEFAULT_DIAGNOSTICS_WAIT
                    ) -> List[Dict[str, Any]]:
        """
        Open (or refresh) the file and return its diagnostics.

        Waits up to `wait` seconds for a fresh publish; whatever the server has
        already told us is returned when the deadline passes, so an idle server
        costs `wait` once and never blocks indefinitely.

        A file the server has never acknowledged returns [] rather than
        anything inferred: silence from a language server means "no opinion",
        and reporting "no errors" for a file it never looked at is a lie the
        model will act on.
        """
        resolved = normalize_path(path)
        with self._condition:
            seen = self._publishes.get(resolved, 0)

        self.open_file(resolved)

        deadline = time.monotonic() + max(0.0, wait)
        with self._condition:
            while (not self._dead
                   and self._publishes.get(resolved, 0) <= seen):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            if self._publishes.get(resolved, 0) == 0:
                return []
            raw = list(self._diagnostics.get(resolved, []))
        return [normalize_diagnostic(item) for item in raw
                if isinstance(item, dict)]

    def all_diagnostics(self) -> Dict[str, List[Dict[str, Any]]]:
        """Everything published so far, keyed by absolute path."""
        with self._condition:
            snapshot = {key: list(value) for key, value in self._diagnostics.items()}
        return {key: [normalize_diagnostic(item) for item in value
                      if isinstance(item, dict)]
                for key, value in snapshot.items()}


# --- manager -----------------------------------------------------------

class LSPManager:
    """
    Lazily starts at most one server per language for a workspace and caches it.

    A language that has no server, or whose server failed to start, is recorded
    as broken so we never pay the spawn cost twice. Nothing is ever started
    speculatively: no server exists until a file of that language is actually
    touched, which on a Haiku box with nothing installed means no server ever.
    """

    def __init__(self, root: str = ".", enabled: bool = True,
                 request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
                 min_severity: Optional[str] = DEFAULT_MIN_SEVERITY,
                 limit: int = MAX_DIAGNOSTICS):
        self.root = str(Path(root).resolve())
        self.enabled = enabled
        self.request_timeout = request_timeout
        self.min_severity = min_severity
        self.limit = limit
        self._clients: Dict[str, Optional[LSPClient]] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._lock = threading.Lock()
        self._closed = False

    @classmethod
    def from_config(cls, config: Any, root: str = ".") -> "LSPManager":
        """Honour `lsp: false` in the config file; anything else enables LSP."""
        enabled = True
        data = getattr(config, "data", None)
        if isinstance(data, dict):
            enabled = data.get("lsp", True) is not False
        return cls(root=root, enabled=enabled)

    def has_server(self, path: Any) -> bool:
        """
        Whether a server for this file exists, without starting anything.

        Cheap enough to call before every edit: memoised PATH lookups only.
        """
        if not self.enabled:
            return False
        return detect_server(path) is not None

    def client_for(self, path: Any) -> Optional[LSPClient]:
        """Server for this file's language, starting it on first use."""
        if not self.enabled:
            return None
        language = detect_language(path)
        if not language:
            return None

        with self._lock:
            if self._closed:
                return None
            if language in self._clients:
                client = self._clients[language]
                if client is None:
                    return None
                if client.alive:
                    return client
                # Reap the corpse rather than leaking the zombie, and mark the
                # language broken so we do not respawn in a loop.
                self._clients[language] = None
                dead: Optional[LSPClient] = client
            else:
                dead = None
            spawn_lock = self._locks.setdefault(language, threading.Lock())

        if dead is not None:
            try:
                dead.close()
            except Exception:
                pass
            return None

        # Per-language, not global: a spawn can block for INITIALIZE_TIMEOUT,
        # and one slow server must not stall edits in every other language.
        with spawn_lock:
            with self._lock:
                if self._closed:
                    return None
                if language in self._clients:
                    return self._clients[language]
            command = detect_server(path)
            if command is None:
                with self._lock:
                    self._clients[language] = None
                return None
            try:
                client = LSPClient(command, root=self.root,
                                   request_timeout=self.request_timeout).start()
            except (LSPError, OSError):
                with self._lock:
                    self._clients[language] = None
                return None
            with self._lock:
                if self._closed:
                    stale = True
                else:
                    stale = False
                    self._clients[language] = client
            if stale:
                # shutdown_all() ran while we were spawning. Nothing else holds
                # a reference to this child, so it is ours to kill.
                try:
                    client.close()
                except Exception:
                    pass
                return None
            return client

    def diagnostics(self, path: Any,
                    wait: float = DEFAULT_DIAGNOSTICS_WAIT
                    ) -> List[Dict[str, Any]]:
        """Diagnostics for one file; [] when no server is available."""
        client = self.client_for(path)
        if client is None:
            return []
        try:
            return client.diagnostics(path, wait=wait)
        except LSPTimeout:
            # Slow, not dead. Killing the server here would mean re-paying the
            # startup cost on the next edit for a server that is merely busy.
            return []
        except (LSPError, OSError):
            # An unreadable file raises LSPError too, and that says nothing
            # about the server — only drop the client when it is actually gone.
            if not client.alive:
                self._forget(client)
            return []

    def report(self, path: Any,
               wait: float = DEFAULT_DIAGNOSTICS_WAIT,
               min_severity: Optional[str] = None,
               limit: Optional[int] = None) -> str:
        """Formatted diagnostics block for appending to a tool result."""
        diags = self.diagnostics(path, wait=wait)
        if not diags:
            return ""
        try:
            display = str(Path(str(path)).resolve().relative_to(self.root))
        except (ValueError, OSError):
            display = str(path)
        return format_diagnostics(
            diags, display,
            limit=self.limit if limit is None else limit,
            min_severity=self.min_severity if min_severity is None else min_severity)

    def status(self) -> Dict[str, str]:
        """language -> "running" | "unavailable", for a /lsp style command."""
        with self._lock:
            snapshot = dict(self._clients)
        result = {}
        for language, client in snapshot.items():
            result[language] = ("running" if client is not None and client.alive
                                else "unavailable")
        return result

    def _forget(self, client: LSPClient) -> None:
        with self._lock:
            for language, existing in list(self._clients.items()):
                if existing is client:
                    self._clients[language] = None
        try:
            client.close()
        except Exception:
            pass

    def shutdown_all(self) -> None:
        with self._lock:
            self._closed = True
            clients = [c for c in self._clients.values() if c is not None]
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                continue


# --- the entry point tools call ----------------------------------------

def diagnostics_block(ctx: Any, path: Any,
                      wait: float = DEFAULT_DIAGNOSTICS_WAIT) -> str:
    """
    Diagnostics for `path` as a text block, or "" when there are none.

    This is the call edit/write tools should make after a successful write. It
    is deliberately total: no LSP manager attached to the context, no language
    server installed, an unreadable file, a wedged server — every one of those
    is "", never an exception and never a wait longer than `wait`. A tool that
    just succeeded must not fail because an optional enhancement did.

    Attach a manager with `ctx.lsp = LSPManager(...)` to turn it on.
    """
    manager = getattr(ctx, "lsp", None)
    if manager is None:
        return ""
    try:
        return manager.report(path, wait=wait)
    except Exception:
        return ""
