"""
Minimal HTTP + SSE layer using only stdlib.
Designed to work on Haiku (http.client + ssl).

Supports:
- POST with JSON body
- Streaming SSE responses for OpenAI and Anthropic formats
- Retry with exponential backoff + jitter, Retry-After, wall-clock budget
- Split connect / read timeouts and an explicit stall timeout
- Cancellation through an abort Event or callable

Three hard-won facts are encoded here; do not regress them.

1.  SSE must be consumed line by line. ``read(n)`` on the response blocks
    until n bytes have accumulated, which deadlocks sparse streams where a
    single token arrives every few seconds.
2.  Python-urllib's default User-Agent is rejected by Cloudflare with a 403
    "error 1010" page, so every request carries an explicit UA.
3.  A socket read timeout must never be retried *through* ``HTTPResponse``.
    Chunked decoding keeps ``chunk_left`` state across calls, and resuming a
    read that raised mid-chunk-header silently corrupts the stream. That is
    why the blocking readline lives on a pump thread and the caller polls a
    queue: the caller stays cancellable without ever re-entering a read that
    raised.
"""
import email.utils
import http.client
import json
import queue
import random
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional, Union

DEFAULT_TIMEOUT = 180          # read/stall budget for a single attempt
DEFAULT_CONNECT_TIMEOUT = 20   # TCP + TLS handshake only
POLL_INTERVAL = 0.25           # how often a blocked reader checks for abort
USER_AGENT = "haikode/1.0 (Haiku OS)"

# Transient by definition: the request never reached the model, or the
# provider explicitly asked us to come back later.
#   408 request timeout, 429 rate limited, 5xx server side.
#   529 is Anthropic's "Overloaded" — outside the 500..504 block.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524, 529})

# Fatal: retrying cannot help, and a traceback is a terrible way to learn why.
FATAL_HINTS = {
    400: "the provider rejected the request body (bad parameter or model id)",
    401: "the API key is missing, invalid or expired",
    403: "the API key is not permitted to use this model or endpoint",
    404: "no such model or endpoint at this base URL",
    413: "the request is larger than the provider accepts",
    422: "the provider could not process the request body",
}


class NetError(Exception):
    """A transport-level failure, carrying enough context to classify it.

    ``str(e)`` keeps the historical ``HTTP <code>: <body>`` prefix so callers
    that only log the message (haikode.mcp) read the same as before.
    """

    def __init__(self, message: str, *, status: Optional[int] = None,
                 body: str = "", headers: Optional[Dict[str, str]] = None,
                 retryable: bool = False, url: Optional[str] = None,
                 attempts: int = 1):
        super().__init__(message)
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.retryable = retryable
        self.url = url
        self.attempts = attempts


class Aborted(NetError):
    """The caller tripped the abort handle; not a failure, and never retried."""

    def __init__(self, message: str = "aborted"):
        super().__init__(message, retryable=False)


AbortLike = Union[None, threading.Event, Callable[[], bool]]


def _aborted(abort: AbortLike) -> bool:
    if abort is None:
        return False
    if isinstance(abort, threading.Event):
        return abort.is_set()
    try:
        return bool(abort())
    except Exception:
        return False


def _check_abort(abort: AbortLike):
    if _aborted(abort):
        raise Aborted()


# --------------------------------------------------------------------------
# Retry policy
# --------------------------------------------------------------------------

@dataclass
class RetryPolicy:
    """Exponential backoff with jitter, bounded by attempts *and* wall clock.

    ``max_elapsed`` is the important one: five attempts against a provider
    honouring a 60 s Retry-After would otherwise block a run for minutes.
    """
    max_attempts: int = 4
    initial_delay: float = 0.4
    factor: float = 2.0
    max_delay: float = 20.0
    max_elapsed: float = 60.0
    jitter: float = 0.25
    # A dropped TCP connection is worth retrying immediately; hammering a
    # rate limiter that did not send Retry-After is not.
    rate_limit_delay: float = 2.0

    def backoff(self, attempt: int) -> float:
        """Delay before retry number ``attempt`` (1-based), jitter included."""
        base = min(self.initial_delay * (self.factor ** max(0, attempt - 1)),
                   self.max_delay)
        if self.jitter:
            base *= 1.0 + random.uniform(-self.jitter, self.jitter)
        return max(0.0, base)

    def delay(self, attempt: int, headers: Optional[Dict[str, str]] = None,
              status: Optional[int] = None) -> float:
        """Retry-After wins over backoff, but is still clamped to max_delay."""
        after = retry_after_seconds(headers)
        if after is not None:
            return max(0.0, min(after, self.max_delay))
        wait = self.backoff(attempt)
        if status == 429:
            wait = max(wait, min(self.rate_limit_delay, self.max_delay))
        return wait


DEFAULT_RETRY = RetryPolicy()
NO_RETRY = RetryPolicy(max_attempts=1, max_elapsed=0.0)


def retry_after_seconds(headers: Optional[Dict[str, str]]) -> Optional[float]:
    """Parse Retry-After (delta-seconds or HTTP-date) and the -ms variant."""
    if not headers:
        return None
    lower = {str(k).lower(): v for k, v in headers.items()}

    raw_ms = lower.get("retry-after-ms")
    if raw_ms:
        try:
            return max(0.0, float(str(raw_ms).strip()) / 1000.0)
        except (TypeError, ValueError):
            pass

    raw = lower.get("retry-after")
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    try:
        delta = when.timestamp() - time.time()
    except (OverflowError, OSError, ValueError):
        return None
    return max(0.0, delta)


def _sleep(seconds: float, abort: AbortLike):
    """Sleep in slices so an abort is noticed inside a long Retry-After."""
    deadline = time.monotonic() + seconds
    while True:
        _check_abort(abort)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, POLL_INTERVAL))


# --------------------------------------------------------------------------
# Connection plumbing: a read timeout distinct from the connect timeout
# --------------------------------------------------------------------------

def _make_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    return ctx


def _with_ua(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    h = dict(headers or {})
    # Python-urllib's default UA is blocked by some CDNs (Cloudflare error 1010).
    h.setdefault("User-Agent", USER_AGENT)
    return h


class _SplitTimeoutMixin:
    """Connect with one timeout, then switch the socket to another.

    urllib only exposes a single timeout, applied to both the handshake and
    every subsequent read. A stream that legitimately idles for a minute needs
    a long read timeout, but waiting a minute to discover a dead host is
    useless — hence the split.
    """

    def __init__(self, *args, **kwargs):
        self._read_timeout = kwargs.pop("read_timeout", None)
        super().__init__(*args, **kwargs)

    def connect(self):
        super().connect()
        if self._read_timeout is not None and self.sock is not None:
            try:
                self.sock.settimeout(self._read_timeout)
            except OSError:
                pass


class _SplitTimeoutHTTPConnection(_SplitTimeoutMixin, http.client.HTTPConnection):
    pass


class _SplitTimeoutHTTPSConnection(_SplitTimeoutMixin, http.client.HTTPSConnection):
    pass


class _SplitTimeoutHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, read_timeout):
        super().__init__()
        self._read_timeout = read_timeout

    def do_open(self, http_class, req, **kwargs):
        if http_class is http.client.HTTPConnection:
            http_class = _SplitTimeoutHTTPConnection
            kwargs["read_timeout"] = self._read_timeout
        return super().do_open(http_class, req, **kwargs)


class _SplitTimeoutHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, read_timeout, context=None):
        super().__init__(context=context)
        self._read_timeout = read_timeout

    def do_open(self, http_class, req, **kwargs):
        if http_class is http.client.HTTPSConnection:
            http_class = _SplitTimeoutHTTPSConnection
            kwargs["read_timeout"] = self._read_timeout
        return super().do_open(http_class, req, **kwargs)


def _opener(read_timeout: float):
    return urllib.request.build_opener(
        _SplitTimeoutHTTPHandler(read_timeout),
        _SplitTimeoutHTTPSHandler(read_timeout, context=_make_context()))


def _hard_close(response):
    """Tear the socket down now, not whenever the GC gets round to it.

    shutdown() before close() so a reader blocked in recv() on another thread
    returns immediately instead of waiting out its timeout.

    That is enough on Linux and macOS but not on Haiku, where a recv() that is
    already blocked ignores shutdown() and only returns when its own timeout
    expires. The pump thread holds the BufferedReader lock for that whole time,
    so response.close() below would block behind it and an abort would feel
    like a hang for the length of the read timeout. Releasing the file
    descriptor does wake the blocked reader on every platform, so do that too:
    sock.close() alone will not, because makefile() holds an io reference that
    defers the real close, hence closing the SocketIO first. The socket belongs
    to this response alone and the pump is already stopping, so there is no
    other reader to strand.
    """
    if response is None:
        return
    released = False
    try:
        raw = getattr(getattr(response, "fp", None), "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            # Closing the descriptor here is what corrupted a database in
            # the field: close() frees the NUMBER, another thread's sqlite
            # reused it within the race window, and when the reader that
            # was still blocked inside OpenSSL woke up, a stray TLS record
            # (17 03 03 …) was written into the new owner's file. dup2 of
            # /dev/null wakes the blocked Haiku reader exactly like close
            # — it closes the old description — but keeps the number
            # OCCUPIED until the socket object's own close runs at GC, so
            # no stranger can inherit it in between. Every straggling read
            # or write in the SSL machinery lands in /dev/null.
            import os
            descriptor = -1
            try:
                descriptor = sock.fileno()
            except Exception:
                pass
            if descriptor >= 0:
                try:
                    null_fd = os.open(os.devnull, os.O_RDWR)
                    try:
                        os.dup2(null_fd, descriptor)
                        released = True
                    finally:
                        os.close(null_fd)
                except OSError:
                    pass
        if not released:
            # No descriptor to park: fall back to the plain close, which at
            # least frees the resources.
            for closeable in (raw, sock):
                if closeable is not None:
                    try:
                        closeable.close()
                        released = True
                    except Exception:
                        pass
    except Exception:
        pass

    if released:
        # The descriptor is gone, which is the resource that matters, and the
        # remaining objects are the GC's business. response.close() must not
        # run here: it takes the BufferedReader lock, and a pump thread that
        # entered poll() just before the fd was closed holds that lock until
        # its own read timeout expires (poll() does not wake when the fd is
        # closed under it on macOS). Waiting for that turned every abort into
        # a hang for the length of stall_timeout.
        return
    try:
        response.close()
    except Exception:
        pass


def _describe_status(status: int, body: str) -> str:
    hint = FATAL_HINTS.get(status)
    snippet = (body or "").strip()[:300]
    if status in RETRYABLE_STATUS and not hint:
        hint = "the provider is unavailable or throttling"
    parts = [f"HTTP {status}:"]
    if hint:
        parts.append(hint)
    if snippet:
        parts.append(f"- {snippet}" if hint else snippet)
    return " ".join(parts)


def _open(url: str, data: Optional[bytes], headers: Optional[Dict[str, str]],
          connect_timeout: float, read_timeout: float,
          abort: AbortLike = None):
    """One attempt. Raises NetError (retryable flag set) on any failure."""
    _check_abort(abort)
    req = urllib.request.Request(url, data=data, headers=_with_ua(headers))
    try:
        return _opener(read_timeout).open(req, timeout=connect_timeout)
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = ""
        try:
            head = dict(e.headers.items()) if e.headers else {}
        except Exception:
            head = {}
        raise NetError(_describe_status(e.code, body), status=e.code, body=body,
                       headers=head, url=url,
                       retryable=e.code in RETRYABLE_STATUS) from e
    except urllib.error.URLError as e:
        reason = e.reason
        # A bad certificate or an unknown host will not fix itself.
        fatal = isinstance(reason, (ssl.SSLCertVerificationError, socket.gaierror))
        raise NetError(f"Connection failed: {reason}", url=url,
                       retryable=not fatal) from e
    except (socket.timeout, TimeoutError) as e:
        raise NetError(f"Connection timed out after {connect_timeout:g}s",
                       url=url, retryable=True) from e
    except Exception as e:
        raise NetError(f"Connection failed: {e}", url=url, retryable=True) from e


# --------------------------------------------------------------------------
# Line reader: blocking readline on a pump thread, polled by the caller
# --------------------------------------------------------------------------

_EOF = object()


def _truncated(response) -> Optional[str]:
    """Detect a body that stopped early, which readline() cannot.

    ``HTTPResponse.peek`` swallows IncompleteRead by design, so iterating
    lines over a chunked body that lost its terminating chunk looks exactly
    like a clean end of stream — and the agent would treat a half-delivered
    answer as a finished one. The framing state says otherwise: a chunked body
    that ran to completion leaves ``chunk_left`` at None, while one cut short
    leaves the size of the chunk it was still expecting.
    """
    try:
        if getattr(response, "chunked", False) and \
                getattr(response, "chunk_left", None) is not None:
            return "Stream truncated: chunked body ended without its terminator"
        remaining = getattr(response, "length", None)
        if remaining:
            return f"Stream truncated: {remaining} bytes of the body never arrived"
    except Exception:
        return None
    return None


def _iter_lines(response, stall_timeout: float, abort: AbortLike,
                poll: float = POLL_INTERVAL) -> Iterator[bytes]:
    """Yield raw response lines, staying cancellable and stall-aware.

    readline() is what keeps sparse streams alive (see module docstring), but
    it blocks. Running it on a pump thread lets the consumer poll, so an abort
    is honoured within ``poll`` seconds no matter which thread trips it, and a
    stream that goes silent for ``stall_timeout`` is abandoned.
    """
    box: "queue.Queue[Any]" = queue.Queue(maxsize=512)
    stop = threading.Event()

    def put(item) -> bool:
        while not stop.is_set():
            try:
                box.put(item, timeout=poll)
                return True
            except queue.Full:
                continue
        return False

    def pump():
        try:
            for line in response:
                if stop.is_set():
                    return
                if not put(line):
                    return
            truncated = _truncated(response)
            if truncated:
                put(NetError(truncated, retryable=True))
                return
        except BaseException as exc:  # noqa: BLE001 - forwarded to the consumer
            put(exc)
            return
        put(_EOF)

    thread = threading.Thread(target=pump, daemon=True, name="haikode-sse")
    thread.start()

    last_byte = time.monotonic()
    try:
        while True:
            _check_abort(abort)
            try:
                item = box.get(timeout=poll)
            except queue.Empty:
                if stall_timeout and time.monotonic() - last_byte > stall_timeout:
                    raise NetError(
                        f"Stream stalled: no data for {stall_timeout:g}s",
                        retryable=True)
                continue
            last_byte = time.monotonic()
            if item is _EOF:
                return
            if isinstance(item, NetError):
                raise item
            if isinstance(item, BaseException):
                if isinstance(item, (socket.timeout, TimeoutError)):
                    raise NetError(
                        f"Stream stalled: no data for {stall_timeout:g}s",
                        retryable=True) from item
                raise NetError(f"Stream error: {item}", retryable=True) from item
            yield item
    finally:
        stop.set()
        _hard_close(response)


def _sse_events(lines: Iterator[bytes], stop_on_done: bool) -> Iterator[Dict[str, Any]]:
    """Frame SSE properly: `data:` lines accumulate until a blank line."""
    pending = []

    def flush():
        if not pending:
            return None
        payload = "\n".join(pending)
        pending.clear()
        if stop_on_done and payload.strip() == "[DONE]":
            return _EOF
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None

    for raw in lines:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            event = flush()
            if event is _EOF:
                return
            if event is not None:
                yield event
            continue
        if line.startswith(":"):
            continue  # comment / keep-alive
        if line.startswith("data:"):
            pending.append(line[5:].lstrip())

    event = flush()
    if event is not None and event is not _EOF:
        yield event


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    connect_timeout: Optional[float] = None,
    retry: Optional[RetryPolicy] = None,
    abort: AbortLike = None,
) -> Dict[str, Any]:
    """POST JSON, return the decoded JSON body.

    ``retry`` defaults to *off*. This function carries MCP ``tools/call``
    requests, which are not idempotent: replaying one after a dropped
    connection could run a shell command twice. Callers that know their
    request is safe to repeat pass ``retry=DEFAULT_RETRY`` explicitly.
    """
    policy = retry or NO_RETRY
    data = json.dumps(payload).encode("utf-8")
    connect = connect_timeout if connect_timeout is not None else min(
        DEFAULT_CONNECT_TIMEOUT, float(timeout))
    deadline = time.monotonic() + policy.max_elapsed
    attempt = 0

    while True:
        attempt += 1
        response = None
        try:
            response = _open(url, data, headers, connect, float(timeout), abort)
            body = response.read().decode("utf-8")
            return json.loads(body)
        except Aborted:
            raise
        except NetError as e:
            e.attempts = attempt
            if not _should_retry(e, attempt, policy, deadline):
                raise
            _sleep(policy.delay(attempt, e.headers, e.status), abort)
        except Exception as e:
            raise NetError(str(e), url=url) from e
        finally:
            _hard_close(response)


def _should_retry(error: NetError, attempt: int, policy: RetryPolicy,
                  deadline: float) -> bool:
    if not error.retryable or attempt >= policy.max_attempts:
        return False
    wait = policy.delay(attempt, error.headers, error.status)
    return time.monotonic() + wait <= deadline


def sse_json_events(
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    stop_on_done: bool = False,
    connect_timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
    retry: Optional[RetryPolicy] = None,
    abort: AbortLike = None,
) -> Iterator[Dict[str, Any]]:
    """
    Generic SSE reader: yields each `data:` block parsed as JSON.
    GET when payload is None, otherwise POST with JSON body.

    Retries are only attempted *before the first event reaches the caller*.
    Re-issuing a prompt whose output has already been partially consumed would
    duplicate tokens and re-run whatever tool calls were already emitted, so
    once a single event is out the request is committed.
    """
    policy = retry if retry is not None else DEFAULT_RETRY
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    stall = stall_timeout if stall_timeout is not None else float(timeout)
    connect = connect_timeout if connect_timeout is not None else min(
        DEFAULT_CONNECT_TIMEOUT, float(timeout))
    deadline = time.monotonic() + policy.max_elapsed
    attempt = 0

    while True:
        attempt += 1
        committed = False
        response = None
        lines = None
        events = None
        try:
            response = _open(url, data, headers, connect, stall, abort)
            lines = _iter_lines(response, stall, abort)
            events = _sse_events(lines, stop_on_done)
            for event in events:
                committed = True
                yield event
            return
        except Aborted:
            raise
        except NetError as e:
            e.attempts = attempt
            if committed or not _should_retry(e, attempt, policy, deadline):
                raise
            _sleep(policy.delay(attempt, e.headers, e.status), abort)
        finally:
            # Close the generators explicitly: _iter_lines' finally is what
            # stops the pump thread and tears the socket down, and waiting for
            # the GC to run it leaks a thread per aborted stream.
            for generator in (events, lines):
                if generator is not None:
                    try:
                        generator.close()
                    except Exception:
                        pass
            _hard_close(response)


def stream_sse_events(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    connect_timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
    retry: Optional[RetryPolicy] = None,
    abort: AbortLike = None,
) -> Iterator[Dict[str, Any]]:
    """
    POST a JSON body and yield every SSE `data:` event parsed as JSON.

    This is what the chat providers use: they need the whole event (tool call
    deltas, finish_reason, usage), not just the text field.
    """
    for event in sse_json_events(url, payload, headers=headers, timeout=timeout,
                                 stop_on_done=True,
                                 connect_timeout=connect_timeout,
                                 stall_timeout=stall_timeout,
                                 retry=retry, abort=abort):
        yield event


def stream_sse(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    connect_timeout: Optional[float] = None,
    stall_timeout: Optional[float] = None,
    retry: Optional[RetryPolicy] = None,
    abort: AbortLike = None,
) -> Iterator[str]:
    """
    Stream SSE and yield text deltas.
    Handles both OpenAI and Anthropic streaming formats (best effort).
    Reasoning channels are deliberately skipped: this helper yields answer
    text only.
    """
    for obj in stream_sse_events(url, payload, headers=headers, timeout=timeout,
                                 connect_timeout=connect_timeout,
                                 stall_timeout=stall_timeout,
                                 retry=retry, abort=abort):
        if "choices" in obj:
            choices = obj["choices"]
            delta = choices[0].get("delta", {}) if choices else {}
            if delta.get("content"):
                yield delta["content"]
        elif obj.get("type") == "content_block_delta":
            delta = obj.get("delta", {})
            if delta.get("type") in (None, "text_delta") and delta.get("text"):
                yield delta["text"]
