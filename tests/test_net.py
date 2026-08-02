"""
Transport resilience: retries, Retry-After, stall detection, cancellation.

Everything here runs against a real local HTTP server rather than a mock, so
the properties proven are properties of the socket path the providers actually
use — including the ones that only exist because of how http.client buffers.
"""

import json
import sys
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode import net  # noqa: E402
from haikode.net import (NO_RETRY, Aborted, NetError, RetryPolicy,  # noqa: E402
                         post_json, retry_after_seconds, stream_sse_events)

FAST = RetryPolicy(max_attempts=4, initial_delay=0.02, factor=2.0,
                   max_delay=0.2, max_elapsed=5.0, jitter=0.0,
                   rate_limit_delay=0.02)


# --------------------------------------------------------------------------
# A programmable endpoint: one scripted step per request, last step repeats.
# --------------------------------------------------------------------------

class ScriptedServer:
    """Serves a list of steps, each ``fn(handler)`` writing a whole response."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []          # decoded JSON bodies, in order
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _serve(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    outer.requests.append(json.loads(raw or b"{}"))
                except ValueError:
                    outer.requests.append({"_raw": raw.decode("utf-8", "replace")})
                index = min(len(outer.requests) - 1, len(outer.steps) - 1)
                try:
                    outer.steps[index](self)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            do_POST = _serve
            do_GET = _serve

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # shutdown() waits out one poll interval; the 0.5 s default would add
        # half a second of pure teardown to every test in this module.
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01},
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self):
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def count(self):
        return len(self.requests)


def _frames(handler, frames, gap=0.0, close_early=False):
    """Write SSE frames, optionally idling between them."""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Transfer-Encoding", "chunked")
    handler.end_headers()
    for frame in frames:
        body = frame if isinstance(frame, str) else json.dumps(frame)
        payload = f"data: {body}\n\n".encode()
        handler.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
        handler.wfile.flush()
        if gap:
            time.sleep(gap)
    if close_early:
        # No terminating 0-length chunk: the client sees a truncated body,
        # which is a stream *failure* rather than a clean end of stream.
        handler.close_connection = True
        return
    handler.wfile.write(b"0\r\n\r\n")
    handler.wfile.flush()


def sse(*frames, gap=0.0, close_early=False):
    return lambda h: _frames(h, frames, gap=gap, close_early=close_early)


def status(code, body="{}", headers=None, delay=0.0):
    def step(handler):
        if delay:
            time.sleep(delay)
        raw = body.encode()
        handler.send_response(code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(raw)
    return step


def json_ok(payload):
    return status(200, json.dumps(payload))


# --------------------------------------------------------------------------

class TestRetryAfter(unittest.TestCase):
    def test_delta_seconds(self):
        self.assertEqual(retry_after_seconds({"Retry-After": "7"}), 7.0)

    def test_header_name_is_case_insensitive(self):
        self.assertEqual(retry_after_seconds({"retry-after": "3"}), 3.0)

    def test_millisecond_variant_wins(self):
        self.assertAlmostEqual(
            retry_after_seconds({"retry-after-ms": "1500", "retry-after": "60"}),
            1.5)

    def test_http_date_in_the_future(self):
        import email.utils
        when = email.utils.formatdate(time.time() + 30, usegmt=True)
        value = retry_after_seconds({"Retry-After": when})
        self.assertTrue(25 <= value <= 31, value)

    def test_http_date_in_the_past_is_zero_not_negative(self):
        import email.utils
        when = email.utils.formatdate(time.time() - 300, usegmt=True)
        self.assertEqual(retry_after_seconds({"Retry-After": when}), 0.0)

    def test_garbage_is_ignored(self):
        self.assertIsNone(retry_after_seconds({"Retry-After": "soon"}))
        self.assertIsNone(retry_after_seconds({}))
        self.assertIsNone(retry_after_seconds(None))


class TestRetryPolicy(unittest.TestCase):
    def test_backoff_grows_and_is_capped(self):
        policy = RetryPolicy(initial_delay=1.0, factor=2.0, max_delay=5.0,
                             jitter=0.0)
        self.assertEqual([policy.backoff(n) for n in (1, 2, 3, 4, 9)],
                         [1.0, 2.0, 4.0, 5.0, 5.0])

    def test_jitter_stays_inside_its_band(self):
        policy = RetryPolicy(initial_delay=1.0, factor=1.0, jitter=0.25)
        values = {round(policy.backoff(1), 6) for _ in range(200)}
        self.assertTrue(all(0.75 <= v <= 1.25 for v in values))
        self.assertGreater(len(values), 1, "jitter produced a constant delay")

    def test_retry_after_beats_backoff_but_is_still_capped(self):
        policy = RetryPolicy(initial_delay=1.0, max_delay=10.0, jitter=0.0)
        self.assertEqual(policy.delay(1, {"Retry-After": "4"}), 4.0)
        self.assertEqual(policy.delay(1, {"Retry-After": "600"}), 10.0)

    def test_rate_limit_has_a_floor_when_no_header_is_sent(self):
        policy = RetryPolicy(initial_delay=0.1, jitter=0.0, rate_limit_delay=2.0)
        self.assertEqual(policy.delay(1, None, 429), 2.0)
        self.assertEqual(policy.delay(1, None, 503), 0.1)


class TestStreamRetries(unittest.TestCase):
    def collect(self, server, **kwargs):
        kwargs.setdefault("retry", FAST)
        return list(stream_sse_events(server.url, {"m": 1}, **kwargs))

    def test_retry_then_succeed(self):
        with ScriptedServer(status(503), status(503),
                            sse({"ok": 1}, "[DONE]")) as server:
            events = self.collect(server)
        self.assertEqual(events, [{"ok": 1}])
        self.assertEqual(server.count, 3)

    def test_retry_exhausted_raises_with_attempt_count(self):
        with ScriptedServer(status(503, '{"error":{"message":"down"}}')) as server:
            with self.assertRaises(NetError) as caught:
                self.collect(server)
        self.assertEqual(server.count, FAST.max_attempts)
        self.assertEqual(caught.exception.attempts, FAST.max_attempts)
        self.assertEqual(caught.exception.status, 503)
        self.assertTrue(caught.exception.retryable)

    def test_wall_clock_budget_stops_retrying_before_the_attempt_count(self):
        # Ten attempts are allowed, but the half-second budget only pays for
        # one 0.3 s backoff — the clock, not the counter, ends the run.
        # Measures wall time, so it uses the real sleep (tests/__init__.py).
        policy = RetryPolicy(max_attempts=10, initial_delay=0.3, factor=1.0,
                             jitter=0.0, max_elapsed=0.5)
        with patch.object(net, "_sleep", net.REAL_SLEEP), \
                ScriptedServer(status(500)) as server:
            started = time.monotonic()
            with self.assertRaises(NetError):
                self.collect(server, retry=policy)
            elapsed = time.monotonic() - started
        self.assertEqual(server.count, 2)
        self.assertLess(elapsed, 1.5)

    def test_retry_after_header_is_honoured(self):
        policy = RetryPolicy(max_attempts=2, initial_delay=0.0, jitter=0.0,
                             max_delay=5.0, max_elapsed=5.0)
        # This test MEASURES the backoff, so it needs the real sleep the
        # test bootstrap caps for everybody else (tests/__init__.py).
        with patch.object(net, "_sleep", net.REAL_SLEEP):
            with ScriptedServer(status(429, "{}", {"Retry-After": "1"}),
                                sse({"ok": 1}, "[DONE]")) as server:
                started = time.monotonic()
                events = self.collect(server, retry=policy)
                elapsed = time.monotonic() - started
        self.assertEqual(events, [{"ok": 1}])
        self.assertGreaterEqual(elapsed, 0.9)

    def test_retry_after_is_clamped_to_max_delay(self):
        policy = RetryPolicy(max_attempts=2, initial_delay=0.0, jitter=0.0,
                             max_delay=0.1, max_elapsed=5.0)
        with ScriptedServer(status(429, "{}", {"Retry-After": "3600"}),
                            sse({"ok": 1}, "[DONE]")) as server:
            started = time.monotonic()
            events = self.collect(server, retry=policy)
            elapsed = time.monotonic() - started
        self.assertEqual(events, [{"ok": 1}])
        self.assertLess(elapsed, 2.0)

    def test_connection_error_is_retried(self):
        # Nothing listens on port 9 (discard); every attempt is refused.
        with self.assertRaises(NetError) as caught:
            list(stream_sse_events("http://127.0.0.1:9/", {}, retry=FAST))
        self.assertEqual(caught.exception.attempts, FAST.max_attempts)
        self.assertTrue(caught.exception.retryable)

    def test_no_retry_once_the_first_event_has_been_yielded(self):
        # The stream dies mid-body. Re-issuing the prompt would duplicate the
        # tokens already delivered and re-run any tool calls in them.
        with ScriptedServer(sse({"first": 1}, close_early=True),
                            sse({"second": 2}, "[DONE]")) as server:
            events = []
            with self.assertRaises(NetError):
                for event in stream_sse_events(server.url, {}, retry=FAST):
                    events.append(event)
        self.assertEqual(events, [{"first": 1}])
        self.assertEqual(server.count, 1, "a committed stream was replayed")


class TestFatalErrors(unittest.TestCase):
    def fatal(self, code, body="{}"):
        with ScriptedServer(status(code, body)) as server:
            with self.assertRaises(NetError) as caught:
                list(stream_sse_events(server.url, {}, retry=FAST))
            return caught.exception, server.count

    def test_401_is_not_retried_and_names_the_key(self):
        error, count = self.fatal(401)
        self.assertEqual(count, 1)
        self.assertFalse(error.retryable)
        self.assertIn("API key", str(error))

    def test_403_is_not_retried(self):
        error, count = self.fatal(403)
        self.assertEqual(count, 1)
        self.assertIn("not permitted", str(error))

    def test_404_points_at_the_model_or_base_url(self):
        error, count = self.fatal(404)
        self.assertEqual(count, 1)
        self.assertIn("no such model", str(error))

    def test_400_is_not_retried(self):
        error, count = self.fatal(400, '{"error":{"message":"bad param"}}')
        self.assertEqual(count, 1)
        self.assertIn("bad param", str(error))

    def test_message_keeps_the_historical_http_prefix(self):
        error, _ = self.fatal(500)
        self.assertTrue(str(error).startswith("HTTP 500:"), str(error))

    def test_body_and_status_are_preserved_for_classification(self):
        with ScriptedServer(status(400, '{"error":{"code":"x"}}')) as server:
            with self.assertRaises(NetError) as caught:
                list(stream_sse_events(server.url, {}, retry=FAST))
        self.assertEqual(caught.exception.status, 400)
        self.assertIn('"code":"x"', caught.exception.body)


class TestTimeouts(unittest.TestCase):
    def test_stall_timeout_abandons_a_dead_stream(self):
        with ScriptedServer(sse({"a": 1}, {"b": 2}, gap=3.0)) as server:
            events = []
            started = time.monotonic()
            with self.assertRaises(NetError) as caught:
                for event in stream_sse_events(server.url, {}, retry=NO_RETRY,
                                               stall_timeout=0.4):
                    events.append(event)
            elapsed = time.monotonic() - started
        self.assertEqual(events, [{"a": 1}])
        self.assertIn("stalled", str(caught.exception))
        self.assertLess(elapsed, 2.0, "stall was not detected promptly")

    def test_idle_gap_shorter_than_the_stall_budget_is_survivable(self):
        with ScriptedServer(sse({"a": 1}, {"b": 2}, "[DONE]", gap=0.6)) as server:
            events = list(stream_sse_events(server.url, {}, retry=NO_RETRY,
                                            stall_timeout=3.0))
        self.assertEqual(events, [{"a": 1}, {"b": 2}])

    def test_connect_timeout_does_not_bound_reads(self):
        # A short connect timeout must not kill a stream that idles longer
        # than it; the two budgets are independent.
        with ScriptedServer(sse({"a": 1}, {"b": 2}, "[DONE]", gap=1.0)) as server:
            events = list(stream_sse_events(server.url, {}, retry=NO_RETRY,
                                            connect_timeout=0.5,
                                            stall_timeout=5.0))
        self.assertEqual(events, [{"a": 1}, {"b": 2}])

    def test_connect_timeout_fires_on_an_unroutable_host(self):
        started = time.monotonic()
        with self.assertRaises(NetError):
            # 203.0.113.0/24 is TEST-NET-3: routed nowhere, so the handshake
            # hangs rather than being refused.
            list(stream_sse_events("http://203.0.113.1:81/", {},
                                   retry=NO_RETRY, connect_timeout=1.0,
                                   stall_timeout=30.0))
        self.assertLess(time.monotonic() - started, 10.0)


class TestCancellation(unittest.TestCase):
    def test_abort_event_tears_the_stream_down(self):
        flag = threading.Event()
        with ScriptedServer(sse(*[{"n": i} for i in range(200)], gap=0.2)) as server:
            started = time.monotonic()
            events = []
            with self.assertRaises(Aborted):
                for event in stream_sse_events(server.url, {}, retry=NO_RETRY,
                                               stall_timeout=30.0, abort=flag):
                    events.append(event)
                    flag.set()
            elapsed = time.monotonic() - started
        self.assertEqual(len(events), 1)
        self.assertLess(elapsed, 2.0, "abort was not honoured promptly")

    def test_abort_callable_is_supported(self):
        state = {"stop": False}
        with ScriptedServer(sse(*[{"n": i} for i in range(200)], gap=0.2)) as server:
            with self.assertRaises(Aborted):
                for _ in stream_sse_events(server.url, {}, retry=NO_RETRY,
                                           stall_timeout=30.0,
                                           abort=lambda: state["stop"]):
                    state["stop"] = True

    def test_abort_from_another_thread_interrupts_a_blocked_read(self):
        flag = threading.Event()
        with ScriptedServer(sse({"a": 1}, {"b": 2}, gap=10.0)) as server:
            stream = stream_sse_events(server.url, {}, retry=NO_RETRY,
                                       stall_timeout=30.0, abort=flag)
            self.assertEqual(next(stream), {"a": 1})
            timer = threading.Timer(0.3, flag.set)
            timer.start()
            started = time.monotonic()
            with self.assertRaises(Aborted):
                next(stream)
            elapsed = time.monotonic() - started
            timer.cancel()
        self.assertLess(elapsed, 2.0)

    def test_abort_during_a_retry_backoff_is_honoured(self):
        flag = threading.Event()
        policy = RetryPolicy(max_attempts=5, initial_delay=5.0, jitter=0.0,
                             max_elapsed=60.0)
        # The point is that the abort lands INSIDE a long backoff, so the
        # backoff has to be real (tests/__init__.py caps it otherwise).
        with patch.object(net, "_sleep", net.REAL_SLEEP), \
                ScriptedServer(status(503)) as server:
            threading.Timer(0.3, flag.set).start()
            started = time.monotonic()
            with self.assertRaises(Aborted):
                list(stream_sse_events(server.url, {}, retry=policy, abort=flag))
        self.assertLess(time.monotonic() - started, 3.0)

    def test_streams_do_not_leak_pump_threads(self):
        """An abandoned stream's pump exits, bounded by its read timeout.

        Counted by thread name, not by active_count(): the process total also
        sees the test server's handler threads, whose teardown races this
        assertion — that form failed four runs in six regardless of the code
        under test.

        The read timeout is the bound that matters. A pump blocked in recv()
        is freed when its socket times out, so a stream configured with a
        short one clears immediately, while a long one keeps its thread for
        that long. Closing the descriptor alone does not reliably wake a
        blocked reader, which is the same platform behaviour _hard_close
        documents.
        """
        def pumps():
            return [t for t in threading.enumerate()
                    if t.is_alive() and t.name == "haikode-sse"]

        # Only this test's own pumps: earlier tests in the module abandon
        # streams with a long read timeout, and their threads are still
        # winding down while this one runs.
        pre_existing = {id(t) for t in pumps()}

        def mine():
            return [t for t in pumps() if id(t) not in pre_existing]

        with ScriptedServer(sse(*[{"n": i} for i in range(50)], "[DONE]")) as server:
            for _ in range(5):
                stream = stream_sse_events(server.url, {}, retry=NO_RETRY,
                                           stall_timeout=2.0)
                next(stream)
                stream.close()       # abandon mid-stream
        deadline = time.monotonic() + 6.0
        while mine() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([], mine())


class TestSSEFraming(unittest.TestCase):
    def test_multi_line_data_fields_are_joined(self):
        def step(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(b'data: {"a":\ndata: 1}\n\n')
            handler.wfile.flush()
            handler.close_connection = True

        with ScriptedServer(step) as server:
            events = list(stream_sse_events(server.url, {}, retry=NO_RETRY))
        self.assertEqual(events, [{"a": 1}])

    def test_comments_and_event_names_are_skipped(self):
        def step(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(b': keep-alive\n\nevent: ping\ndata: {"a":1}\n\n')
            handler.wfile.flush()
            handler.close_connection = True

        with ScriptedServer(step) as server:
            events = list(stream_sse_events(server.url, {}, retry=NO_RETRY))
        self.assertEqual(events, [{"a": 1}])

    def test_done_sentinel_ends_the_stream(self):
        with ScriptedServer(sse({"a": 1}, "[DONE]", {"b": 2})) as server:
            events = list(stream_sse_events(server.url, {}, retry=NO_RETRY))
        self.assertEqual(events, [{"a": 1}])

    def test_events_arrive_as_they_are_written_not_in_one_batch(self):
        # The reason this layer reads line by line: read(n) blocks until n
        # bytes accumulate, which deadlocks a stream that emits one token
        # every few seconds.
        with ScriptedServer(sse({"a": 1}, {"b": 2}, "[DONE]", gap=0.8)) as server:
            stream = stream_sse_events(server.url, {}, retry=NO_RETRY,
                                       stall_timeout=5.0)
            started = time.monotonic()
            self.assertEqual(next(stream), {"a": 1})
            first = time.monotonic() - started
            self.assertEqual(next(stream), {"b": 2})
            second = time.monotonic() - started
            stream.close()
        self.assertLess(first, 0.5, "first event was buffered, not streamed")
        self.assertGreater(second, 0.7)

    def test_text_only_helper_handles_both_dialects(self):
        with ScriptedServer(sse(
                {"choices": [{"delta": {"content": "he"}}]},
                {"choices": [{"delta": {"reasoning": "ignored"}}]},
                {"type": "content_block_delta",
                 "delta": {"type": "text_delta", "text": "llo"}},
                {"type": "content_block_delta",
                 "delta": {"type": "thinking_delta", "thinking": "ignored"}},
                "[DONE]")) as server:
            text = "".join(net.stream_sse(server.url, {}, retry=NO_RETRY))
        self.assertEqual(text, "hello")

    def test_undecodable_frames_are_skipped_not_fatal(self):
        with ScriptedServer(sse("not json", {"a": 1}, "[DONE]")) as server:
            events = list(stream_sse_events(server.url, {}, retry=NO_RETRY))
        self.assertEqual(events, [{"a": 1}])


class TestPostJson(unittest.TestCase):
    def test_round_trip(self):
        with ScriptedServer(json_ok({"pong": True})) as server:
            self.assertEqual(post_json(server.url, {"ping": 1}), {"pong": True})
            self.assertEqual(server.requests, [{"ping": 1}])

    def test_no_retry_by_default(self):
        # post_json carries MCP tools/call, which is not safe to replay.
        with ScriptedServer(status(503), json_ok({"ok": 1})) as server:
            with self.assertRaises(NetError):
                post_json(server.url, {}, timeout=5)
        self.assertEqual(server.count, 1)

    def test_retries_when_a_policy_is_supplied(self):
        with ScriptedServer(status(503), json_ok({"ok": 1})) as server:
            self.assertEqual(post_json(server.url, {}, timeout=5, retry=FAST),
                             {"ok": 1})
        self.assertEqual(server.count, 2)

    def test_fatal_status_is_not_retried_even_with_a_policy(self):
        with ScriptedServer(status(401)) as server:
            with self.assertRaises(NetError):
                post_json(server.url, {}, timeout=5, retry=FAST)
        self.assertEqual(server.count, 1)


class TestHeaders(unittest.TestCase):
    def test_custom_user_agent_is_always_sent(self):
        # urllib's default UA trips Cloudflare's error 1010.
        seen = {}

        def step(handler):
            seen.update({k.lower(): v for k, v in handler.headers.items()})
            status(200, "{}")(handler)

        with ScriptedServer(step) as server:
            post_json(server.url, {})
        self.assertEqual(seen.get("user-agent"), net.USER_AGENT)

    def test_caller_headers_survive(self):
        seen = {}

        def step(handler):
            seen.update({k.lower(): v for k, v in handler.headers.items()})
            status(200, "{}")(handler)

        with ScriptedServer(step) as server:
            post_json(server.url, {}, headers={"Authorization": "Bearer k",
                                               "User-Agent": "custom/1"})
        self.assertEqual(seen.get("authorization"), "Bearer k")
        self.assertEqual(seen.get("user-agent"), "custom/1")


if __name__ == "__main__":
    unittest.main()
