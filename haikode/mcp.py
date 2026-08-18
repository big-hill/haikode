"""
Model Context Protocol client — stdlib only.

Ported from opencode's packages/opencode/src/mcp: servers declared in config are
dialled at session boot, their tools/list output is wrapped in local Tool
instances named "mcp_<server>_<tool>", and calls are proxied over JSON-RPC.

Two transports are supported:

  stdio   newline-delimited JSON on the child's stdin/stdout (the MCP stdio
          transport), with a reader thread and id correlation.
  remote  plain HTTP POST JSON-RPC via haikode.net.post_json. Servers that
          insist on the SSE half of the Streamable HTTP transport, or on
          Mcp-Session-Id round-tripping, fail with a clear message instead of
          hanging — we cannot read response headers or streams through post_json.

Startup is bounded, not blocking: MCPManager.start_all() dials every server on
its own thread and gives the whole set a few seconds. A server that is slow, or
that never answers, costs the user that budget once — it does not add its own
timeout to the time between typing `haikode` and seeing a prompt. Servers that
connect after the budget expires join the tool set when they are ready.

Security: an MCP server is third-party code we execute and then hand a model.
Three things follow, and all three are enforced here rather than trusted:
  * every proxy tool asks under the permission key "mcp" before it runs;
  * every schema is rebuilt from scratch with depth, size and shape caps before
    it can reach a provider request body;
  * a server that misbehaves — garbage on stdout, a mid-request exit, a
    thousand tools, a self-referential schema — is skipped with a collected
    warning and never takes the session down with it.

permission.DEFAULTS has no entry for "mcp", and Permissions.ask() falls back to
ASK for unknown keys, so an MCP call always prompts unless the user configures
permission.mcp themselves.
"""

import json
import math
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .net import NetError, post_json
from .tool.base import Tool, ToolContext, ToolResult

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "haikode", "version": "1.0"}
DEFAULT_TIMEOUT = 30.0
INITIALIZE_TIMEOUT = 30.0
PROCESS_EXIT_TIMEOUT = 2.0
READER_JOIN_TIMEOUT = 2.0
# How long start_all() will make the caller wait for the *whole* set of servers.
# Short on purpose: this is time added to every `haikode` launch.
DEFAULT_STARTUP_WAIT = 5.0
# How long shutdown_all() waits for in-flight dialler threads to unwind.
SHUTDOWN_JOIN_TIMEOUT = 3.0
MAX_LIST_PAGES = 100
MAX_OUTPUT = 60000
READ_CHUNK = 65536
# A frame is one line; a server that never emits a newline (binary noise on
# stdout) must not grow the reader's buffer until we run out of memory.
MAX_FRAME_BYTES = 32 * 1024 * 1024

# Schema hardening. A tool schema goes straight into a provider request body,
# so it has to survive json.dumps and stay small enough not to crowd out the
# conversation. These are the walls, not suggestions.
MAX_SCHEMA_DEPTH = 12
MAX_SCHEMA_BYTES = 32 * 1024
MAX_SCHEMA_KEYS = 256
MAX_SCHEMA_ITEMS = 256
MAX_SCHEMA_STRING = 4096
EMPTY_SCHEMA: Dict[str, Any] = {"type": "object", "properties": {}}
# One server offering thousands of tools would swamp the model's tool list.
MAX_TOOLS_PER_SERVER = 128

# Transport names accepted for config["mcp"][name]["type"]. opencode writes
# "local"/"remote"; the HTTP spellings are accepted so a usable entry is never
# silently dropped.
REMOTE_TYPES = ("remote", "http", "https", "sse", "streamable-http", "streamablehttp")
STDIO_TYPES = ("stdio", "local", "process", "command")

# JSON-RPC error code for a method we do not implement.
METHOD_NOT_FOUND = -32601


class MCPError(Exception):
    """Any failure talking to an MCP server."""


class MCPTimeout(MCPError):
    """A request outlived its deadline."""


# --- pure helpers (framing, naming, content) ---------------------------

def encode_frame(payload: Dict[str, Any]) -> bytes:
    """
    Serialise one message for the stdio transport: compact JSON on a single
    line. Embedded newlines are illegal in this transport, and json.dumps
    escapes them inside strings, so a compact dump is always one line.
    """
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"


def decode_frames(buffer: bytes) -> Tuple[List[Dict[str, Any]], bytes]:
    """
    Pull every complete line out of `buffer`, returning (messages, remainder).

    Lines that are not JSON objects are dropped: many servers print banners or
    log lines to stdout, and that must not desynchronise the stream.
    """
    messages: List[Dict[str, Any]] = []
    while True:
        index = buffer.find(b"\n")
        if index < 0:
            return messages, buffer
        line = buffer[:index]
        buffer = buffer[index + 1:]
        stripped = line.strip()
        if not stripped:
            continue
        try:
            decoded = json.loads(stripped.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        if isinstance(decoded, dict):
            messages.append(decoded)


def pump_frames(read_chunk: Callable[[int], bytes],
                on_message: Callable[[Dict[str, Any]], None],
                chunk_size: int = READ_CHUNK) -> None:
    """Drive the stdio read loop until EOF; handler errors never kill it."""
    buffer = b""
    while True:
        try:
            chunk = read_chunk(chunk_size)
        except (OSError, ValueError):
            return
        if not chunk:
            return
        buffer += chunk
        if len(buffer) > MAX_FRAME_BYTES:
            # Nothing line-shaped in 32MB: this is not an MCP server. Drop what
            # we have instead of buffering the rest of the machine's memory.
            buffer = b""
            continue
        messages, buffer = decode_frames(buffer)
        for message in messages:
            try:
                on_message(message)
            except Exception:
                continue


def path_to_uri(path: str) -> str:
    """Absolute path -> file:// URI (spaces and friends percent-encoded)."""
    return "file://" + urllib.request.pathname2url(os.path.abspath(str(path)))


def sanitize(value: str) -> str:
    """Tool names must match ^[a-zA-Z0-9_-]+$ for the provider APIs."""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value or "")


def tool_name(server: str, remote: str) -> str:
    """Local name for a remote tool, capped at the 64-char provider limit."""
    return ("mcp_%s_%s" % (sanitize(server), sanitize(remote)))[:64]


def _text_of(value: Any) -> str:
    return value if isinstance(value, str) else ""


def describe_part(part: Dict[str, Any]) -> str:
    """
    One-line placeholder for a content part we cannot render as text.

    Every field is treated as attacker-controlled: a server that answers with
    {"type": "image", "data": 17} must produce a description, not a TypeError
    that surfaces as a failed tool call.
    """
    kind = _text_of(part.get("type")) or "unknown"
    if kind in ("image", "audio"):
        mime = _text_of(part.get("mimeType")) or "application/octet-stream"
        return "[%s content omitted: %s, %d base64 bytes]" % (
            kind, mime, len(_text_of(part.get("data"))))
    if kind == "resource_link":
        return "[resource link: %s]" % (_text_of(part.get("uri")) or "?")
    if kind == "resource":
        resource = part.get("resource")
        resource = resource if isinstance(resource, dict) else {}
        mime = _text_of(resource.get("mimeType")) or "application/octet-stream"
        return "[binary resource omitted: %s (%s, %d base64 bytes)]" % (
            _text_of(resource.get("uri")) or "?", mime,
            len(_text_of(resource.get("blob"))))
    return "[unsupported content part: %s]" % kind


# A screenshot of a 1600x900 Haiku desktop lands around a megabyte of
# base64; six is headroom, not an invitation. Anything larger stays a
# described placeholder, exactly as before images existed.
MAX_IMAGE_BASE64 = 6 * 1024 * 1024
MAX_IMAGES_PER_RESULT = 4


def images_from_result(result: Any) -> List[Dict[str, str]]:
    """Image parts of a tools/call result, in the neutral Msg shape.

    Every field is attacker-controlled: non-string data, absurd sizes and
    non-image mime types are dropped, not raised. Dropped parts still show
    up in the text as describe_part placeholders, so nothing disappears
    silently -- the model is told an image existed even when it is not
    given the bytes.
    """
    if not isinstance(result, dict):
        return []
    content = result.get("content")
    if not isinstance(content, list):
        return []
    images: List[Dict[str, str]] = []
    for item in content:
        if len(images) >= MAX_IMAGES_PER_RESULT:
            break
        if not isinstance(item, dict) or item.get("type") != "image":
            continue
        data = item.get("data")
        mime = _text_of(item.get("mimeType")).lower()
        if not isinstance(data, str) or not data:
            continue
        if not mime.startswith("image/"):
            continue
        if len(data) > MAX_IMAGE_BASE64:
            continue
        images.append({"media_type": mime, "data": data})
    return images


def content_to_text(content: Any) -> str:
    """
    MCP content array -> plain text. Text parts are concatenated; embedded
    resources contribute their text when they have any; everything else is
    described so the model knows something was there rather than silently
    receiving a shorter answer than the server sent.
    """
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(_text_of(item.get("text")))
        elif kind == "resource":
            resource = item.get("resource")
            resource = resource if isinstance(resource, dict) else {}
            text = resource.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
            else:
                parts.append(describe_part(item))
        else:
            parts.append(describe_part(item))
    return "\n".join(part for part in parts if part.strip())


def result_to_text(result: Dict[str, Any]) -> str:
    """
    tools/call result -> text. Falls back to structuredContent when the server
    returned no content array, which the spec allows for typed results.
    """
    text = content_to_text(result.get("content"))
    if text.strip():
        return text
    structured = result.get("structuredContent")
    if structured is not None:
        try:
            return json.dumps(structured, indent=2)
        except (TypeError, ValueError):
            return str(structured)
    return text


# --- schema hardening --------------------------------------------------

def sanitize_schema(value: Any, depth: int = 0) -> Any:
    """
    Rebuild a value out of nothing but JSON-safe primitives.

    Rebuilding rather than validating is the point: whatever the server sent is
    never the object we pass on, so a schema cannot smuggle a cycle, a NaN, a
    non-string key or a 40-deep nest into a provider request body. Depth and
    width caps terminate the walk, which is also what makes a self-referential
    structure safe — it hits the depth wall instead of recursing forever.
    """
    if depth >= MAX_SCHEMA_DEPTH:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # json.dumps writes NaN/Infinity, which is not valid JSON and which
        # some provider endpoints reject with an opaque 400.
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:MAX_SCHEMA_STRING]
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in list(value.items())[:MAX_SCHEMA_KEYS]:
            if not isinstance(key, str):
                continue
            cleaned = sanitize_schema(item, depth + 1)
            if cleaned is None and item is not None:
                continue
            out[key[:MAX_SCHEMA_STRING]] = cleaned
        return out
    if isinstance(value, (list, tuple)):
        out_list = []
        for item in list(value)[:MAX_SCHEMA_ITEMS]:
            cleaned = sanitize_schema(item, depth + 1)
            if cleaned is None and item is not None:
                continue
            out_list.append(cleaned)
        return out_list
    # Anything else (bytes, a custom object smuggled in by a plugin) has no
    # JSON spelling. Dropping beats guessing.
    return None


def normalize_schema(schema: Any) -> Dict[str, Any]:
    """
    Coerce a remote inputSchema into something every provider accepts.

    Returns the empty object schema for anything unusable, so schema assembly
    for a whole tool set can never be brought down by one hostile entry.
    """
    cleaned = sanitize_schema(schema)
    if not isinstance(cleaned, dict):
        return dict(EMPTY_SCHEMA)
    result: Dict[str, Any] = dict(cleaned)
    # A tool's arguments are always a JSON object, whatever the server claims.
    result["type"] = "object"
    properties = result.get("properties")
    if not isinstance(properties, dict):
        result["properties"] = {}
    else:
        # Every property must itself be a schema object; a bare string or list
        # there is what makes a provider reject the entire request, taking the
        # other tools down with it.
        result["properties"] = {name: prop for name, prop in properties.items()
                                if isinstance(prop, dict)}
    required = result.get("required")
    if required is not None:
        if isinstance(required, list):
            result["required"] = [name for name in required
                                  if isinstance(name, str)
                                  and name in result["properties"]]
        else:
            result.pop("required", None)
    try:
        encoded = json.dumps(result, allow_nan=False)
    except (TypeError, ValueError):
        return dict(EMPTY_SCHEMA)
    if len(encoded) > MAX_SCHEMA_BYTES:
        # A schema this big is either generated nonsense or an attempt to eat
        # the context window. Keep the tool, drop the parameter detail.
        return dict(EMPTY_SCHEMA)
    return result


# --- clients -----------------------------------------------------------

class MCPClient:
    """
    An MCP server spoken to over the stdio transport.

    One daemon reader thread owns stdout; requests block on an Event keyed by
    JSON-RPC id and always carry a deadline, and the reader releases every
    waiter when the child exits.
    """

    def __init__(self, name: str, command: List[str],
                 env: Optional[Dict[str, str]] = None,
                 cwd: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.name = name
        self.command = list(command)
        self.env = env
        self.cwd = str(Path(cwd or ".").resolve())
        self.timeout = timeout
        self.process: Optional[subprocess.Popen] = None
        self.server_info: Dict[str, Any] = {}
        self.instructions = ""
        self.initialized = False

        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        # Held across Popen so close() cannot slip between the "are we closed?"
        # check and the spawn. That window is precisely how a shutdown leaves an
        # MCP server running on the user's machine forever.
        self._spawn_lock = threading.Lock()
        self._next_id = 0
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._reader: Optional[threading.Thread] = None
        self._dead = False
        self._closed = False

    # --- lifecycle ----------------------------------------------------

    def start(self) -> "MCPClient":
        """Spawn the server and run the initialize handshake."""
        environment = None
        if self.env:
            environment = dict(os.environ)
            environment.update({str(k): str(v) for k, v in self.env.items()})
        with self._spawn_lock:
            if self._closed:
                raise MCPError("MCP server %r was shut down before it started"
                               % self.name)
            try:
                # stderr is where MCP servers are told to log; discard it so a
                # full pipe can never block the child mid-response.
                self.process = subprocess.Popen(
                    self.command, cwd=self.cwd, env=environment,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, bufsize=0)
            except OSError as e:
                raise MCPError("failed to start MCP server %r: %s" % (self.name, e))

            self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                            name="mcp-%s" % self.name)
            self._reader.start()
        try:
            self._handshake()
        except BaseException:
            # The child is already running: a failed handshake must not leave it
            # behind, because nothing else holds a reference to reap it.
            self.close()
            raise
        return self

    def _read_loop(self) -> None:
        stream = self.process.stdout if self.process else None
        try:
            if stream is not None:
                pump_frames(stream.read, self._dispatch)
        finally:
            with self._lock:
                self._dead = True
                pending = list(self._pending.values())
            for slot in pending:
                slot["message"] = {"error": {"message": "MCP server exited"}}
                slot["event"].set()

    def close(self) -> None:
        """Close stdin (the transport's shutdown signal), then reap the child."""
        with self._spawn_lock:
            if self._closed:
                return
            self._closed = True
            process = self.process
        self.initialized = False
        # Release anyone blocked in request(): the answer is never coming, and
        # they should not sit out their full timeout to learn that.
        with self._lock:
            self._dead = True
            pending = list(self._pending.values())
        for slot in pending:
            slot["message"] = {"error": {"message": "MCP server closed"}}
            slot["event"].set()
        if process is None:
            self._join_reader()
            return
        # Closing stdin is the transport's shutdown signal. stdout stays open
        # until the child is gone and the reader has unwound: the reader thread
        # is blocked on it, and closing a pipe under a blocked reader is exactly
        # the kind of thing that turns a clean exit into a hang.
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

    def _join_reader(self) -> None:
        """Wait for the reader thread, so close() really does leave nothing."""
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
            raise MCPError("MCP server %r is not running" % self.name)
        data = encode_frame(payload)
        with self._write_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (OSError, ValueError) as e:
                raise MCPError("write to MCP server %r failed: %s" % (self.name, e))

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def request(self, method: str, params: Optional[Dict[str, Any]] = None,
                timeout: Optional[float] = None) -> Any:
        deadline = self.timeout if timeout is None else timeout
        with self._lock:
            if self._dead:
                raise MCPError("MCP server %r is not running" % self.name)
            self._next_id += 1
            ident = self._next_id
            slot: Dict[str, Any] = {"event": threading.Event(), "message": None}
            self._pending[ident] = slot

        payload: Dict[str, Any] = {"jsonrpc": "2.0", "id": ident, "method": method}
        payload["params"] = params if params is not None else {}
        try:
            self._write(payload)
        except MCPError:
            with self._lock:
                self._pending.pop(ident, None)
            raise

        got = slot["event"].wait(deadline)
        with self._lock:
            self._pending.pop(ident, None)
        if not got:
            raise MCPTimeout("%s on %r timed out after %.1fs"
                             % (method, self.name, deadline))
        return _unwrap(slot["message"] or {}, method, self.name)

    def _dispatch(self, message: Dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            self._handle_server_request(message)
        elif "id" in message:
            ident = message.get("id")
            with self._lock:
                slot = self._pending.get(ident)
                if slot is None:
                    return
                slot["message"] = message
            slot["event"].set()

    def _handle_server_request(self, message: Dict[str, Any]) -> None:
        """A server blocks on its requests, so answer every one."""
        method = message.get("method")
        reply: Dict[str, Any] = {"jsonrpc": "2.0", "id": message.get("id")}
        if method == "roots/list":
            reply["result"] = {"roots": [
                {"uri": path_to_uri(self.cwd), "name": "workspace"}]}
        elif method == "ping":
            reply["result"] = {}
        else:
            reply["error"] = {"code": METHOD_NOT_FOUND,
                              "message": "unsupported: %s" % method}
        try:
            self._write(reply)
        except MCPError:
            pass

    # --- protocol -----------------------------------------------------

    def _handshake(self) -> None:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": CLIENT_INFO,
        }, timeout=INITIALIZE_TIMEOUT) or {}
        if not isinstance(result, dict):
            raise MCPError("MCP server %r answered initialize with a non-object"
                           % self.name)
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        self.instructions = _text_of(result.get("instructions"))
        self.notify("notifications/initialized")
        self.initialized = True

    def list_tools(self) -> List[Dict[str, Any]]:
        return _paginate(lambda cursor: self.request(
            "tools/list", {"cursor": cursor} if cursor else {}))

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None,
                  timeout: Optional[float] = None) -> Dict[str, Any]:
        result = self.request("tools/call",
                              {"name": name, "arguments": arguments or {}},
                              timeout=timeout)
        return result if isinstance(result, dict) else {}


class RemoteMCPClient:
    """
    An MCP server reached over HTTP with plain JSON-RPC POSTs.

    This covers servers that answer with `application/json`. Anything that needs
    the SSE half of the Streamable HTTP transport, or session resumption via
    Mcp-Session-Id, raises a clear MCPError — post_json cannot see response
    headers or read a stream, and guessing would mean hanging.
    """

    def __init__(self, name: str, url: str,
                 headers: Optional[Dict[str, str]] = None,
                 timeout: float = DEFAULT_TIMEOUT):
        self.name = name
        self.url = url
        self.timeout = timeout
        self.headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self.headers.update(headers or {})
        self.server_info: Dict[str, Any] = {}
        self.instructions = ""
        self.initialized = False
        self._next_id = 0
        self._lock = threading.Lock()
        self._closed = False

    def start(self) -> "RemoteMCPClient":
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"roots": {"listChanged": False}},
            "clientInfo": CLIENT_INFO,
        }) or {}
        if not isinstance(result, dict):
            raise MCPError("remote MCP server %r answered initialize with a "
                           "non-object" % self.name)
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        self.instructions = _text_of(result.get("instructions"))
        # Notifications get no response body; a server that returns 202 with an
        # empty body would trip post_json's JSON decode, so failure is ignored.
        try:
            self.request("notifications/initialized", notification=True)
        except MCPError:
            pass
        self.initialized = True
        return self

    def request(self, method: str, params: Optional[Dict[str, Any]] = None,
                notification: bool = False,
                timeout: Optional[float] = None) -> Any:
        if self._closed:
            raise MCPError("remote MCP server %r is closed" % self.name)
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method,
                                   "params": params if params is not None else {}}
        if not notification:
            with self._lock:
                self._next_id += 1
                payload["id"] = self._next_id
        # post_json takes whole seconds; floor at 1 so a fractional configured
        # timeout cannot become 0, which urlopen reads as "non-blocking".
        seconds = max(1, int(timeout or self.timeout or DEFAULT_TIMEOUT))
        try:
            response = post_json(self.url, payload, headers=self.headers,
                                 timeout=seconds)
        except NetError as e:
            raise MCPError(
                "remote MCP server %r (%s) failed on %s: %s. Only plain JSON-RPC "
                "over HTTP is supported; SSE-only servers are not."
                % (self.name, self.url, method, e))
        if notification:
            return None
        if not isinstance(response, dict):
            raise MCPError("remote MCP server %r returned a non-object response"
                           % self.name)
        return _unwrap(response, method, self.name)

    def list_tools(self) -> List[Dict[str, Any]]:
        return _paginate(lambda cursor: self.request(
            "tools/list", {"cursor": cursor} if cursor else {}))

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None,
                  timeout: Optional[float] = None) -> Dict[str, Any]:
        result = self.request("tools/call",
                              {"name": name, "arguments": arguments or {}},
                              timeout=timeout)
        return result if isinstance(result, dict) else {}

    def close(self) -> None:
        self._closed = True
        self.initialized = False

    @property
    def alive(self) -> bool:
        return self.initialized and not self._closed


def _unwrap(message: Dict[str, Any], method: str, server: str) -> Any:
    """JSON-RPC envelope -> result, or raise the server's error."""
    if "error" in message:
        error = message.get("error") or {}
        if isinstance(error, dict):
            detail = error.get("message") or error
        else:
            detail = error
        raise MCPError("%s on %r failed: %s" % (method, server, detail))
    return message.get("result")


def _paginate(fetch: Callable[[Optional[str]], Any]) -> List[Dict[str, Any]]:
    """Follow nextCursor through a paginated MCP list, guarding against loops."""
    items: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    seen: set = set()
    for _ in range(MAX_LIST_PAGES):
        page = fetch(cursor) or {}
        if not isinstance(page, dict):
            return items
        entries = page.get("tools")
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and isinstance(entry.get("name"), str) \
                    and entry["name"]:
                items.append(entry)
                if len(items) >= MAX_TOOLS_PER_SERVER:
                    return items
        cursor = page.get("nextCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            return items
        seen.add(cursor)
    return items


# --- proxy tool --------------------------------------------------------

class MCPProxyTool(Tool):
    """
    A remote MCP tool exposed as a local Tool.

    Asks under the "mcp" permission key. permission.DEFAULTS deliberately has no
    "mcp" entry — Permissions.ask() treats unknown keys as ASK, which is the
    right default for code we did not write.
    """

    permission = "mcp"

    def __init__(self, server: str, client: Any, definition: Dict[str, Any]):
        if not isinstance(definition, dict):
            raise ValueError("tool definition must be an object")
        self.server = server
        self.client = client
        self.definition = definition
        self.remote_name = _text_of(definition.get("name"))
        if not self.remote_name:
            raise ValueError("tool definition has no name")
        self.name = tool_name(server, self.remote_name)
        # The "always" grant covers the whole server, so the glob has to match
        # this tool's own name — and tool_name() truncates at 64 chars, which
        # can chop the prefix off entirely for a very long server name. When
        # that happens, grant only this tool rather than a pattern that would
        # never match again (re-prompting forever).
        prefix = "mcp_%s_" % sanitize(server)
        self._server_pattern = (prefix + "*" if self.name.startswith(prefix)
                                else self.name)
        title = _text_of(definition.get("title")).strip()
        described = _text_of(definition.get("description")).strip()
        self.description = described or title or (
            "Tool %s provided by the MCP server %r." % (self.remote_name, server))
        self.parameters = normalize_schema(definition.get("inputSchema"))

    def rename(self, name: str) -> None:
        """Take a different local name after a collision with another server."""
        self.name = name[:64]
        prefix = "mcp_%s_" % sanitize(self.server)
        self._server_pattern = (prefix + "*" if self.name.startswith(prefix)
                                else self.name)

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        ctx.check_abort()
        # Second pattern is the "always" grant: approving once covers every
        # tool on this server, so it must match this tool's own name too.
        ctx.ask("mcp", [self.name, self._server_pattern],
                "Run MCP tool %s on server %s" % (self.remote_name, self.server),
                {"server": self.server, "tool": self.remote_name, "args": args})

        started = time.monotonic()
        try:
            result = self.client.call_tool(self.remote_name,
                                           args if isinstance(args, dict) else {})
        except MCPError as e:
            raise RuntimeError(str(e))
        ctx.check_abort()
        if not isinstance(result, dict):
            result = {}

        output = result_to_text(result)
        if result.get("isError"):
            raise RuntimeError(output or "MCP tool %s returned an error"
                               % self.remote_name)
        if len(output) > MAX_OUTPUT:
            output = output[:MAX_OUTPUT] + "\n\n[output truncated]"
        return ToolResult(
            title="%s: %s" % (self.server, self.remote_name),
            output=output or "(no output)",
            metadata={"server": self.server, "tool": self.remote_name,
                      "duration": round(time.monotonic() - started, 3)},
            images=images_from_result(result))


# --- manager -----------------------------------------------------------

def parse_server_config(name: str, raw: Any) -> Optional[Dict[str, Any]]:
    """
    Normalise one entry of config["mcp"].

    Returns None when the server is disabled or the entry is unusable; the
    caller treats that as "skip", never as an error.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("enabled") is False or raw.get("disabled") is True:
        return None

    declared = str(raw.get("type") or "").strip().lower()
    if declared in REMOTE_TYPES:
        remote = True
    elif declared in STDIO_TYPES:
        remote = False
    else:
        # Unknown or missing type: classify by what the entry actually carries,
        # so a usable server is never dropped over a spelling we don't know.
        remote = bool(raw.get("url")) and not raw.get("command")

    try:
        timeout = float(raw.get("timeout") or DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    if timeout <= 0 or not math.isfinite(timeout):
        timeout = DEFAULT_TIMEOUT
    entry: Dict[str, Any] = {"name": name,
                             "type": "remote" if remote else "stdio",
                             "timeout": timeout}
    if remote:
        url = raw.get("url")
        if not isinstance(url, str) or not url:
            return None
        entry["url"] = url
        headers = raw.get("headers")
        entry["headers"] = {str(k): str(v) for k, v in headers.items()} \
            if isinstance(headers, dict) else {}
        return entry

    command = raw.get("command")
    if isinstance(command, str):
        try:
            command = shlex.split(command)
        except ValueError:
            # Unbalanced quotes in a config string: skip this server. Raising
            # here would take down every other server's startup with it.
            return None
    if not isinstance(command, list) or not command:
        return None
    entry["command"] = [str(part) for part in command]
    env = raw.get("env")
    entry["env"] = {str(k): str(v) for k, v in env.items()} \
        if isinstance(env, dict) else {}
    entry["cwd"] = str(raw.get("cwd") or "")
    return entry


class MCPServerStatusTool(Tool):
    """The stand-in for a configured server that offers no tools yet.

    A server that is still connecting, or failed to start, would otherwise
    be invisible to the model: the user configured it, asks the model to use
    it, and the model has no idea it exists. This tool is the honest answer
    — it names the server, and calling it reports the connection state and
    error instead of guessing. When the real server comes up its tools
    replace this one (agent_tools() stops emitting it).

    No permission prompt: it reads local state and never dials the server.
    """

    permission = "mcp"

    def __init__(self, server: str, manager: "MCPManager"):
        self.server = server
        self.manager = manager
        self.name = tool_name(server, "status")
        self.description = (
            "The MCP server %r is configured but currently offers no tools "
            "(still connecting, or failed). Call this to see its connection "
            "status and error. Its real tools appear once it connects."
            % server)
        self.parameters = {"type": "object", "properties": {}}

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        state = "unknown"
        try:
            state = dict(self.manager.status()).get(self.server, "unknown")
        except Exception:
            pass
        return ToolResult(title="mcp %s" % self.server,
                          output="MCP server %r: %s" % (self.server, state))


class MCPManager:
    """
    Owns every configured MCP server for a session.

    start_all() never raises and never blocks past its budget: a server that
    will not start, or will not start *quickly*, is recorded and skipped,
    because a broken or slow third-party server must not stop — or delay — the
    agent from running.
    """

    def __init__(self, config: Any = None, cwd: str = ".",
                 timeout: float = DEFAULT_TIMEOUT):
        self.config = config
        self.cwd = str(Path(cwd).resolve())
        self.timeout = timeout
        self.clients: Dict[str, Any] = {}
        self.errors: Dict[str, str] = {}
        self.warnings: List[str] = []
        self._tools: List[MCPProxyTool] = []
        self._lock = threading.Lock()
        self._threads: List[threading.Thread] = []
        self._inflight: Dict[str, Any] = {}
        self._pending: set = set()
        self._closed = False

    # --- config -------------------------------------------------------

    def servers(self) -> Dict[str, Dict[str, Any]]:
        """Enabled, well-formed server entries from config["mcp"]."""
        data = getattr(self.config, "data", None)
        raw = data.get("mcp") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            return {}
        result = {}
        for name, entry in raw.items():
            key = str(name)
            try:
                parsed = parse_server_config(key, entry)
            except Exception as e:  # a hand-edited config can hold anything
                self._warn(key, "bad config: %s" % e)
                self.errors[key] = "bad config: %s" % e
                continue
            if parsed is None:
                if isinstance(entry, dict) and (entry.get("enabled") is False
                                                or entry.get("disabled") is True):
                    continue  # deliberately off: not a problem to report
                self._warn(key, "unusable entry, skipped")
                continue
            result[key] = parsed
        return result

    def _warn(self, name: str, message: str) -> None:
        text = "mcp %s: %s" % (name, message)
        with self._lock:
            if text not in self.warnings:
                self.warnings.append(text)

    # --- startup ------------------------------------------------------

    def start_all(self, wait: float = DEFAULT_STARTUP_WAIT) -> None:
        """
        Dial every configured server, spending at most `wait` seconds in total.

        Each server gets its own thread, so the cost is the slowest server, not
        the sum — and it is capped either way. Anything still connecting when
        the budget runs out keeps going in the background and joins the tool
        set when it is ready; status() calls it "connecting" until then.

        Never raises. This runs on the startup path of an interactive app.
        """
        entries = self.servers()
        with self._lock:
            if self._closed:
                return
            todo = [entry for name, entry in entries.items()
                    if name not in self.clients and name not in self._pending]
            for entry in todo:
                self._pending.add(entry["name"])
        if not todo:
            return

        threads = []
        for entry in todo:
            thread = threading.Thread(target=self._dial, args=(entry,),
                                      daemon=True,
                                      name="mcp-connect-%s" % entry["name"])
            thread.start()
            threads.append(thread)
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()] + threads

        deadline = time.monotonic() + max(0.0, wait)
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    def _dial(self, entry: Dict[str, Any]) -> None:
        """One server's whole connect-and-list, on its own thread. Never raises."""
        name = entry["name"]
        client = None
        try:
            client = self._connect(entry)
            definitions = client.list_tools()
        except Exception as e:  # a third-party server may fail any way it likes
            with self._lock:
                self._pending.discard(name)
                self._inflight.pop(name, None)
                self.errors[name] = str(e)
            self._warn(name, "not available: %s" % e)
            # _connect may have spawned a child before failing (a server that
            # handshakes then rejects tools/list); nothing else holds a
            # reference to it, so reap it here or it lives forever.
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            return

        tools, problems = self._build_tools(name, client, definitions)
        with self._lock:
            self._pending.discard(name)
            self._inflight.pop(name, None)
            stale = self._closed
            if not stale:
                self.clients[name] = client
                existing = {tool.name for tool in self._tools}
                for tool in tools:
                    if tool.name in existing:
                        renamed = self._unique_name(tool.name, existing)
                        problems.append("tool %r renamed to %r (name collision)"
                                        % (tool.name, renamed))
                        tool.rename(renamed)
                    existing.add(tool.name)
                    self._tools.append(tool)
                self.errors.pop(name, None)
        for problem in problems:
            self._warn(name, problem)
        if stale:
            # shutdown_all() ran while we were still dialling. This child is
            # ours to reap or it outlives the application.
            try:
                client.close()
            except Exception:
                pass

    @staticmethod
    def _unique_name(name: str, taken: set) -> str:
        for suffix in range(2, 100):
            tail = "_%d" % suffix
            candidate = name[:64 - len(tail)] + tail
            if candidate not in taken:
                return candidate
        return name

    def _build_tools(self, name: str, client: Any, definitions: Any
                     ) -> Tuple[List[MCPProxyTool], List[str]]:
        """
        Wrap tool definitions, dropping the ones we cannot make safe.

        Runs outside the lock: this is where a hostile schema does its worst,
        and it must not be able to wedge the manager for everyone else.
        """
        tools: List[MCPProxyTool] = []
        problems: List[str] = []
        entries = definitions if isinstance(definitions, list) else []
        if len(entries) > MAX_TOOLS_PER_SERVER:
            problems.append("offered %d tools, keeping the first %d"
                            % (len(entries), MAX_TOOLS_PER_SERVER))
            entries = entries[:MAX_TOOLS_PER_SERVER]
        for definition in entries:
            try:
                tools.append(MCPProxyTool(name, client, definition))
            except Exception as e:
                problems.append("bad tool definition: %s" % e)
        return tools, problems

    def _new_client(self, entry: Dict[str, Any]) -> Any:
        """Build the client object without doing any I/O."""
        if entry["type"] == "remote":
            return RemoteMCPClient(entry["name"], entry["url"],
                                   headers=entry.get("headers"),
                                   timeout=entry.get("timeout", self.timeout))
        return MCPClient(entry["name"], entry["command"],
                         env=entry.get("env"),
                         cwd=entry.get("cwd") or self.cwd,
                         timeout=entry.get("timeout", self.timeout))

    def _connect(self, entry: Dict[str, Any]) -> Any:
        """Build, register and start one client. Registration comes first so
        that shutdown_all() can reap a server whose handshake is still in
        flight — otherwise a server that hangs during initialize survives the
        application it was launched for."""
        client = self._new_client(entry)
        with self._lock:
            if self._closed:
                raise MCPError("shutting down")
            self._inflight[entry["name"]] = client
        try:
            client.start()
        except BaseException:
            with self._lock:
                self._inflight.pop(entry["name"], None)
            raise
        return client

    # --- use ----------------------------------------------------------

    def tools(self) -> List[MCPProxyTool]:
        """Proxy Tool instances for every tool every connected server offers."""
        with self._lock:
            return list(self._tools)

    def agent_tools(self) -> List[Tool]:
        """What an agent should carry: real proxies, stand-ins for the rest.

        Every configured server is represented — by its tools when it is
        connected, by one MCPServerStatusTool when it is connecting or
        failed — so the model always knows the server the user configured
        exists, and can say *why* it is unusable instead of guessing.
        """
        offered: List[Tool] = list(self.tools())
        covered = {getattr(tool, "server", "") for tool in offered}
        try:
            configured = list(self.servers())
        except Exception:
            configured = []
        for name in configured:
            if name not in covered:
                offered.append(MCPServerStatusTool(name, self))
        return offered

    def status(self) -> Dict[str, str]:
        """name -> connected | connecting | failed: ..., for /mcp style output."""
        with self._lock:
            result = {name: "connected" for name in self.clients}
            for name in self._pending:
                result.setdefault(name, "connecting")
            for name, error in self.errors.items():
                if name not in result:
                    result[name] = "failed: %s" % error
        return result

    def shutdown_all(self) -> None:
        """
        Close every server: connected, mid-handshake, or still being dialled.

        The order matters. Marking closed first stops a dialler thread from
        stashing a fresh child on the way out; closing the in-flight clients
        unblocks any thread parked on a handshake; joining last means a thread
        that had already connected has closed its own client before we return.
        """
        with self._lock:
            self._closed = True
            clients = list(self.clients.values()) + list(self._inflight.values())
            self.clients.clear()
            self._inflight.clear()
            self._tools = []
            threads = list(self._threads)
            self._threads = []
        for client in clients:
            try:
                client.close()
            except Exception:
                continue

        deadline = time.monotonic() + SHUTDOWN_JOIN_TIMEOUT
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

        # A thread that connected between our snapshot and its own _closed
        # check closes its client itself; this sweeps up anything else.
        with self._lock:
            late = list(self.clients.values()) + list(self._inflight.values())
            self.clients.clear()
            self._inflight.clear()
            self._pending.clear()
            self._tools = []
        for client in late:
            try:
                client.close()
            except Exception:
                continue
