"""
todowrite / webfetch — task tracking and web access.
"""

import html.parser
import http.client
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from ..net import USER_AGENT
from .base import Tool, ToolContext, ToolResult, load_prompt

VALID_STATUS = {"pending", "in_progress", "completed", "cancelled"}
MAX_FETCH_BYTES = 500_000
FETCH_TIMEOUT = 30          # total wall clock, not per socket operation
FETCH_CHUNK = 64 * 1024
MAX_FETCH_OUTPUT = 50000


class TodoWriteTool(Tool):
    name = "todowrite"
    description = load_prompt("todowrite.txt")
    permission = "todowrite"
    parameters = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "description": "The updated todo list",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string",
                                    "description": "Brief description of the task"},
                        "status": {"type": "string",
                                   "description": "Current status: pending, in_progress, completed, cancelled"},
                        "priority": {"type": "string",
                                     "description": "Priority level: high, medium, low"},
                    },
                    "required": ["content", "status"],
                },
            },
        },
        "required": ["todos"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        todos: List[Dict[str, str]] = []
        for raw in args.get("todos") or []:
            status = str(raw.get("status", "pending"))
            todos.append({
                "content": str(raw.get("content", "")),
                "status": status if status in VALID_STATUS else "pending",
                "priority": str(raw.get("priority", "medium")),
            })
        ctx.todos = todos
        open_count = sum(1 for t in todos if t["status"] not in ("completed", "cancelled"))
        return ToolResult(
            title=f"{open_count} todo{'s' if open_count != 1 else ''}",
            output=json.dumps(todos, indent=2),
            metadata={"todos": todos})


class _TextExtractor(html.parser.HTMLParser):
    """Minimal HTML → text/markdown converter (stdlib only)."""

    SKIP = {"script", "style", "head", "noscript", "svg"}
    BLOCK = {"p", "div", "section", "article", "br", "tr", "li",
             "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}

    def __init__(self, markdown: bool = True):
        super().__init__(convert_charrefs=True)
        self.markdown = markdown
        self.parts: List[str] = []
        self._skip_depth = 0
        self._heading = ""

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
            if self.markdown:
                if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    self._heading = "#" * int(tag[1]) + " "
                    self.parts.append(self._heading)
                elif tag == "li":
                    self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
            self._heading = ""

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = data.strip()
        if text:
            self.parts.append(text + " ")

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = re.sub(r"[ \t]+", " ", joined)
        return re.sub(r"\n\s*\n\s*\n+", "\n\n", joined).strip()


# --- SSRF guard --------------------------------------------------------

class WebFetchBlocked(ValueError):
    """The URL points somewhere the tool refuses to go."""


def _resolve_addresses(host: str) -> List[str]:
    """Every address `host` resolves to. Empty when DNS fails."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        return []
    return [info[4][0] for info in infos]


def _private(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _blocked_reason(url: str) -> Optional[str]:
    """
    Why this URL must not be fetched, or None.

    The threat is a prompt-injected page telling the model to fetch
    http://127.0.0.1:8080/ or http://169.254.169.254/ — the tool would happily
    read the user's local services or cloud metadata and hand the contents back
    to the model. Every hop is checked, not just the first, because the
    redirect is the interesting half of the attack.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "scheme %r is not allowed (only http and https)" % parts.scheme
    host = parts.hostname
    if not host:
        return "no host in URL"

    addresses = _resolve_addresses(host)
    if not addresses:
        # An unresolvable host used to be allowed through on the theory that
        # the connect would fail anyway. It would not: urllib resolves again,
        # and a resolver that answers the second time (split horizon, a racing
        # attacker, a cache that just filled) gets a free pass. Nothing can be
        # pinned to an address we never saw, so this is a refusal.
        return "%s does not resolve; refusing to fetch a host we cannot screen" % host
    private = [address for address in addresses if _private(address)]
    if private:
        return ("%s resolves to a private or loopback address (%s); refusing "
                "to fetch internal services" % (host, ", ".join(sorted(set(private)))))
    return None


def _assert_public_url(url: str) -> None:
    reason = _blocked_reason(url)
    if reason:
        raise WebFetchBlocked("Refusing to fetch %s: %s" % (url, reason))


def _pinned_addresses(host: str) -> List[str]:
    """
    Every address this connection is allowed to use, in resolution order.

    Checking the name and then letting urllib look it up again is a
    time-of-check/time-of-use hole: DNS rebinding answers "93.184.216.34" to
    the screening lookup and "127.0.0.1" to the one that matters. Resolving
    here and connecting to *these* results closes the window, because the
    addresses that were screened are the only ones the socket may get.

    All of them, not just the first: a host with an AAAA record resolves to
    IPv6 first, and on a machine with no IPv6 route — the owner's Haiku box —
    pinning to that one address made every such host unreachable while
    IPv4-only hosts worked. That is most of the modern web. The screening is
    unchanged, since a single private answer still rejects the whole name.
    """
    addresses = _resolve_addresses(host)
    if not addresses:
        raise WebFetchBlocked(
            "Refusing to connect to %s: it does not resolve" % host)
    private = [address for address in addresses if _private(address)]
    if private:
        raise WebFetchBlocked(
            "Refusing to connect to %s: it resolves to a private or loopback "
            "address (%s)" % (host, ", ".join(sorted(set(private)))))
    seen, ordered = set(), []
    for address in addresses:
        cleaned = address.split("%")[0]
        if cleaned not in seen:
            seen.add(cleaned)
            ordered.append(cleaned)
    return ordered


def _connect_pinned(connection, hostname: str) -> None:
    """Try each screened address until one connects.

    Mirrors what socket.create_connection does for a name, except the
    candidate list is the screened one rather than a fresh lookup.
    """
    last = None
    for address in connection.pinned or [hostname]:
        connection.host = address
        try:
            http.client.HTTPConnection.connect(connection)
            return
        except OSError as exc:
            last = exc
        finally:
            connection.host = hostname
    raise last if last is not None else OSError("no address could be reached")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """Connects to the screened address; `Host:` still carries the name."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pinned = _pinned_addresses(self.host)

    def connect(self) -> None:
        # putrequest() builds the Host header from self.host, and the server
        # must see the name it was asked for, not the address — which is why
        # _connect_pinned restores it after every attempt.
        _connect_pinned(self, self.host)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """As above, and SNI/certificate validation still use the hostname."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pinned = _pinned_addresses(self.host)

    def connect(self) -> None:
        hostname = self.host
        _connect_pinned(self, hostname)
        server_hostname = self._tunnel_host or hostname
        self.sock = self._context.wrap_socket(self.sock,
                                              server_hostname=server_hostname)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        # check_hostname is only forwarded when it was actually set: newer
        # http.client versions have dropped the parameter, and the context
        # carries the same setting anyway.
        options: Dict[str, Any] = {"context": self._context}
        if getattr(self, "_check_hostname", None) is not None:
            options["check_hostname"] = self._check_hostname
        return self.do_open(_PinnedHTTPSConnection, req, **options)


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-checks every redirect target before urllib follows it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    """
    An opener that only ever connects to addresses it screened itself.

    The redirect guard checks the *name* of each hop; the pinned handlers check
    and then pin the *address*. Both are needed: a redirect is how the attacker
    gets a second URL, rebinding is how they get a second answer for the first.
    """
    return urllib.request.build_opener(
        _GuardedRedirectHandler(), _PinnedHTTPHandler(), _PinnedHTTPSHandler())


def _tighten(response: Any, seconds: float) -> None:
    """
    Shrink the socket timeout to whatever is left of the total budget.

    Without this a server that dribbles one byte just under the socket timeout
    can keep the connection (and the agent) alive far past the deadline. Every
    attribute here is private to http.client, hence the belt-and-braces.
    """
    seconds = max(0.1, min(seconds, 10.0))
    for attribute in ("fp",):
        stream = getattr(response, attribute, None)
        socket_object = getattr(stream, "_sock", None)
        if socket_object is None:
            socket_object = getattr(getattr(stream, "raw", None), "_sock", None)
        if socket_object is not None:
            try:
                socket_object.settimeout(seconds)
            except Exception:
                pass
            return


class WebFetchTool(Tool):
    name = "webfetch"
    description = load_prompt("webfetch.txt")
    permission = "webfetch"
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch content from"},
            "format": {"type": "string", "enum": ["markdown", "text", "html"],
                       "description": "The format to return the content in (default markdown)"},
            "timeout": {"type": "integer",
                        "description": f"Optional total timeout in seconds (max {FETCH_TIMEOUT})"},
        },
        "required": ["url"],
    }

    def execute(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = args["url"]
        fmt = args.get("format") or "markdown"
        if url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        if not url.startswith("https://"):
            raise ValueError(f"Invalid URL: {url}")

        budget = FETCH_TIMEOUT
        try:
            requested = float(args.get("timeout") or 0)
            if requested > 0:
                budget = min(requested, FETCH_TIMEOUT)
        except (TypeError, ValueError):
            pass

        _assert_public_url(url)
        ctx.ask("webfetch", [url], f"Fetch {url}", {"url": url})
        # Re-check after the prompt: approval may have taken a while, and the
        # check is cheap.
        _assert_public_url(url)

        raw, charset, content_type, truncated = self._fetch(url, budget)

        body = raw.decode(charset, errors="replace")
        if fmt == "html" or "text/html" not in content_type:
            output = body
        else:
            parser = _TextExtractor(markdown=(fmt == "markdown"))
            parser.feed(body)
            output = parser.text()

        if truncated:
            output += f"\n\n[response truncated at {MAX_FETCH_BYTES} bytes]"
        if len(output) > MAX_FETCH_OUTPUT:
            output = output[:MAX_FETCH_OUTPUT] + "\n\n[content truncated]"
        return ToolResult(title=url, output=output,
                          metadata={"url": url, "format": fmt,
                                    "bytes": len(raw), "truncated": truncated})

    @staticmethod
    def _fetch(url: str, budget: float):
        """
        Body bytes under both a size cap and a total wall-clock cap.

        urlopen's `timeout` is per socket operation: a server dribbling one byte
        per second never trips it. The read loop below enforces a real deadline
        and a real byte ceiling, so a hostile endpoint cannot hold the agent
        open or flood the context window.
        """
        deadline = time.monotonic() + budget
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        opener = _build_opener()

        try:
            response = opener.open(request, timeout=budget)
        except WebFetchBlocked:
            raise
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Failed to fetch {url}: HTTP {error.code} {error.reason}")
        except Exception as error:
            raise RuntimeError(f"Failed to fetch {url}: {error}")

        try:
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_FETCH_BYTES:
                raise RuntimeError(
                    f"Refusing to fetch {url}: response is {declared} bytes "
                    f"(limit {MAX_FETCH_BYTES})")

            charset = response.headers.get_content_charset() or "utf-8"
            content_type = response.headers.get("Content-Type", "")

            chunks: List[bytes] = []
            total = 0
            truncated = False
            while True:
                if total >= MAX_FETCH_BYTES:
                    truncated = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    truncated = True
                    break
                _tighten(response, remaining)
                try:
                    chunk = response.read(min(FETCH_CHUNK, MAX_FETCH_BYTES - total))
                except Exception as error:
                    if not chunks:
                        raise RuntimeError(f"Failed to fetch {url}: {error}")
                    truncated = True
                    break
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        finally:
            try:
                response.close()
            except Exception:
                pass

        return b"".join(chunks), charset, content_type, truncated
