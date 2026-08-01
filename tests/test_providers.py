"""
Provider streaming contract — the phase-1 change that made tool calling real.

Both dialects are exercised against a local HTTP server that replays recorded
SSE frames, so these tests prove the parsing without touching the network.
"""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode.net import NO_RETRY, RetryPolicy  # noqa: E402
from haikode.providers import base as provider_base  # noqa: E402
from haikode.providers.anthropic import (AnthropicProvider,  # noqa: E402
                                         max_output_tokens)
from haikode.providers.base import (ThinkTagSplitter, chunk_error,  # noqa: E402
                                    classify_error, is_context_overflow,
                                    reasoning_from_delta)
from haikode.providers.gemini import GeminiProvider, sanitize_schema  # noqa: E402
from haikode.providers import openai_compat  # noqa: E402
from haikode.providers.openai_compat import OpenAICompatProvider  # noqa: E402
from haikode.schema import Msg, ToolCall, ToolSpec  # noqa: E402

FAST = RetryPolicy(max_attempts=3, initial_delay=0.02, factor=2.0,
                   max_delay=0.1, max_elapsed=2.0, jitter=0.0,
                   rate_limit_delay=0.02)

SPEC = ToolSpec(name="read", description="Read a file",
                parameters={"type": "object",
                            "properties": {"filePath": {"type": "string"}},
                            "required": ["filePath"]})


class SSEServer:
    """Serves a fixed list of SSE `data:` payloads and captures the request."""

    def __init__(self, frames):
        self.frames = frames
        self.received = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.received = json.loads(self.rfile.read(length) or b"{}")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for frame in outer.frames:
                    body = frame if isinstance(frame, str) else json.dumps(frame)
                    self.wfile.write(f"data: {body}\n\n".encode())
                    self.wfile.flush()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # shutdown() waits out one poll interval; the 0.5 s default would add
        # half a second of teardown to every test that builds a server.
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


def openai_frame(**delta):
    return {"choices": [{"index": 0, "delta": delta}]}


class TestOpenAIDialect(unittest.TestCase):
    def collect(self, frames, messages=None, tools=(SPEC,)):
        with SSEServer(frames) as server:
            provider = OpenAICompatProvider(base_url=server.url, api_key="k",
                                            retry=NO_RETRY)
            chunks = list(provider.stream(
                messages or [Msg(role="user", content="hi")],
                list(tools), "m", 256))
            return chunks, server.received

    def test_text_deltas(self):
        chunks, _ = self.collect([openai_frame(content="he"),
                                  openai_frame(content="llo"), "[DONE]"])
        self.assertEqual("".join(c.text for c in chunks), "hello")

    def test_tool_call_deltas_are_surfaced(self):
        frames = [
            openai_frame(tool_calls=[{"index": 0, "id": "c1",
                                      "function": {"name": "read", "arguments": ""}}]),
            openai_frame(tool_calls=[{"index": 0,
                                      "function": {"arguments": '{"filePath"'}}]),
            openai_frame(tool_calls=[{"index": 0,
                                      "function": {"arguments": ': "a.txt"}'}}]),
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            "[DONE]",
        ]
        chunks, _ = self.collect(frames)
        deltas = [c.tool_call_delta for c in chunks if c.tool_call_delta]
        self.assertEqual(deltas[0]["id"], "c1")
        self.assertEqual(deltas[0]["name"], "read")
        self.assertEqual("".join(d["arguments"] for d in deltas),
                         '{"filePath": "a.txt"}')
        self.assertEqual(chunks[-1].stop_reason, "tool_calls")

    def test_reasoning_channel(self):
        chunks, _ = self.collect([openai_frame(reasoning_content="thinking"),
                                  "[DONE]"])
        self.assertEqual(chunks[0].reasoning, "thinking")

    def test_tools_are_sent_in_the_request(self):
        _, received = self.collect(["[DONE]"])
        self.assertEqual(received["tools"][0]["function"]["name"], "read")
        self.assertEqual(received["tool_choice"], "auto")

    def test_no_tools_key_when_none_offered(self):
        _, received = self.collect(["[DONE]"], tools=())
        self.assertNotIn("tools", received)

    def test_tool_result_encoded_as_tool_role(self):
        messages = [
            Msg(role="user", content="read it"),
            Msg(role="assistant", content="",
                tool_calls=[ToolCall(id="c1", name="read",
                                     arguments={"filePath": "a.txt"})]),
            Msg(role="tool", tool_call_id="c1", content="file body"),
        ]
        _, received = self.collect(["[DONE]"], messages=messages)
        encoded = received["messages"]
        self.assertEqual(encoded[1]["tool_calls"][0]["id"], "c1")
        self.assertEqual(json.loads(encoded[1]["tool_calls"][0]["function"]["arguments"]),
                         {"filePath": "a.txt"})
        self.assertEqual(encoded[2]["role"], "tool")
        self.assertEqual(encoded[2]["tool_call_id"], "c1")

    def test_usage_reported(self):
        frames = [{"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 7, "completion_tokens": 2}}, "[DONE]"]
        chunks, _ = self.collect(frames)
        self.assertEqual(chunks[-1].usage, {"input": 7, "output": 2})

    def test_http_error_becomes_a_chunk_not_an_exception(self):
        provider = OpenAICompatProvider(base_url="http://127.0.0.1:9",
                                        api_key="k", retry=NO_RETRY)
        chunks = list(provider.stream([Msg(role="user", content="hi")], [], "m", 16))
        self.assertTrue(any(c.stop_reason == "error" for c in chunks))

    def test_the_request_asks_for_usage(self):
        """A streaming response need not carry usage unless asked.

        Measured against the real endpoints: an Ollama server (local or
        cloud) returns no usage whatsoever without the parameter and full
        counts with it, which is what left the footer at "0 in 0 out".
        """
        _, received = self.collect(["[DONE]"])
        self.assertEqual({"include_usage": True},
                         received.get("stream_options"))


class TestOpenAIUsageOptOut(unittest.TestCase):
    """An endpoint that rejects stream_options must keep working."""

    def _server(self, message, status=400):
        rejected = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                if "stream_options" in body:
                    rejected.append(body)
                    payload = json.dumps({"error": {"message": message}}).encode()
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for frame in ({"choices": [{"index": 0, "delta": {"content": "ok"}}]},
                              "[DONE]"):
                    text = frame if isinstance(frame, str) else json.dumps(frame)
                    self.wfile.write(f"data: {text}\n\n".encode())
                    self.wfile.flush()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}", rejected

    def test_a_rejected_parameter_is_dropped_and_the_turn_still_runs(self):
        url, rejected = self._server("unknown parameter: stream_options")
        provider = OpenAICompatProvider(base_url=url, api_key="k",
                                        retry=NO_RETRY)
        chunks = list(provider.stream([Msg(role="user", content="hi")], [], "m", 16))
        self.assertEqual("ok", "".join(c.text for c in chunks))
        self.assertEqual(1, len(rejected))     # asked once, then gave up on it
        self.assertFalse(provider.stream_usage)

        # And it stays dropped for the life of the process, so the cost is one
        # rejected request rather than one per turn.
        list(provider.stream([Msg(role="user", content="again")], [], "m", 16))
        self.assertEqual(1, len(rejected))

    def test_an_endpoint_that_always_fails_yields_an_error_not_an_exception(self):
        """The retry must not turn a real failure into a crashed turn."""
        url = self._always_failing_server(
            "bad request near: {\"stream_options\": {\"include_usage\": true}}")
        provider = OpenAICompatProvider(base_url=url, api_key="k",
                                        retry=NO_RETRY)
        chunks = list(provider.stream([Msg(role="user", content="hi")], [], "m", 16))
        self.assertTrue(any(c.stop_reason == "error" for c in chunks))

    def test_a_back_off_is_re_tested_instead_of_trusted_forever(self):
        """One 400 is weak evidence, so it is not believed indefinitely.

        A 400 that quotes the request body back mentions stream_options
        whatever it is really about. Backing off on that reading is a cheap
        guess to make; believing it for the life of the process would cost
        every later turn its token counts on an endpoint that reports usage
        only when asked.
        """
        url, rejected = self._server("unknown parameter: stream_options")
        provider = OpenAICompatProvider(base_url=url, api_key="k",
                                        retry=NO_RETRY)
        turn = lambda: list(provider.stream(
            [Msg(role="user", content="hi")], [], "m", 16))

        turn()
        self.assertFalse(provider.stream_usage)
        self.assertEqual(1, len(rejected))

        # Quiet for a while: the back-off holds, so the endpoint is not asked
        # again on every single turn.
        for _ in range(openai_compat.USAGE_REPROBE_AFTER - 1):
            turn()
        self.assertEqual(1, len(rejected))

        # Then it is tested once more — and this server still refuses, so the
        # back-off simply renews rather than sticking on.
        turn()
        self.assertEqual(2, len(rejected))
        self.assertFalse(provider.stream_usage)

    def _always_failing_server(self, message, status=400):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0)
                payload = json.dumps({"error": {"message": message}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def test_a_400_about_something_else_still_reaches_the_user(self):
        url, _ = self._server("context length exceeded")
        provider = OpenAICompatProvider(base_url=url, api_key="k",
                                        retry=NO_RETRY)
        chunks = list(provider.stream([Msg(role="user", content="hi")], [], "m", 16))
        self.assertTrue(any(c.stop_reason == "error" for c in chunks))
        self.assertTrue(provider.stream_usage)


class TestAnthropicDialect(unittest.TestCase):
    def collect(self, frames, messages=None, tools=(SPEC,)):
        with SSEServer(frames) as server:
            provider = AnthropicProvider(base_url=server.url, api_key="k",
                                         retry=NO_RETRY)
            chunks = list(provider.stream(
                messages or [Msg(role="user", content="hi")],
                list(tools), "m", 256))
            return chunks, server.received

    def test_text_and_thinking(self):
        frames = [
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "hmm"}},
        ]
        chunks, _ = self.collect(frames)
        self.assertEqual(chunks[0].text, "hi")
        self.assertEqual(chunks[1].reasoning, "hmm")

    def test_tool_use_blocks(self):
        frames = [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "tool_use", "id": "t1", "name": "read"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": '{"filePath":'}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "input_json_delta", "partial_json": ' "a.txt"}'}},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
             "usage": {"input_tokens": 5, "output_tokens": 1}},
        ]
        chunks, _ = self.collect(frames)
        deltas = [c.tool_call_delta for c in chunks if c.tool_call_delta]
        self.assertEqual(deltas[0]["id"], "t1")
        self.assertEqual(deltas[0]["name"], "read")
        self.assertEqual("".join(d["arguments"] for d in deltas),
                         '{"filePath": "a.txt"}')
        self.assertEqual(chunks[-1].stop_reason, "tool_calls")

    def test_system_message_is_hoisted(self):
        messages = [Msg(role="system", content="be brief"),
                    Msg(role="user", content="hi")]
        _, received = self.collect([], messages=messages)
        self.assertEqual(received["system"], "be brief")
        self.assertEqual([m["role"] for m in received["messages"]], ["user"])

    def test_tool_results_become_user_content_blocks(self):
        messages = [
            Msg(role="user", content="read them"),
            Msg(role="assistant", content="ok", tool_calls=[
                ToolCall(id="t1", name="read", arguments={"filePath": "a"}),
                ToolCall(id="t2", name="read", arguments={"filePath": "b"})]),
            Msg(role="tool", tool_call_id="t1", content="A"),
            Msg(role="tool", tool_call_id="t2", content="B"),
        ]
        _, received = self.collect([], messages=messages)
        encoded = received["messages"]
        assistant = encoded[1]["content"]
        self.assertEqual(assistant[0]["type"], "text")
        self.assertEqual([b["type"] for b in assistant[1:]], ["tool_use", "tool_use"])
        # Both results must land in ONE user turn, or Anthropic rejects it.
        self.assertEqual(len(encoded), 3)
        self.assertEqual([b["tool_use_id"] for b in encoded[2]["content"]],
                         ["t1", "t2"])

    def test_tools_use_input_schema_key(self):
        _, received = self.collect([])
        self.assertIn("input_schema", received["tools"][0])

    def test_api_error_event(self):
        chunks, _ = self.collect([{"type": "error",
                                   "error": {"message": "overloaded"}}])
        self.assertEqual(chunks[-1].stop_reason, "error")
        self.assertIn("overloaded", chunks[-1].text)


# ==========================================================================
# Error classification
# ==========================================================================

class TestErrorClassification(unittest.TestCase):
    """One table, seven kinds, every dialect folded onto it."""

    CASES = [
        # (label, status, body, expected kind, retryable)
        ("openai auth", 401,
         '{"error":{"message":"Incorrect API key provided","type":'
         '"invalid_request_error","code":"invalid_api_key"}}', "auth", False),
        ("anthropic auth", 401,
         '{"type":"error","error":{"type":"authentication_error",'
         '"message":"invalid x-api-key"}}', "auth", False),
        ("gemini auth", 403,
         '{"error":{"code":403,"message":"API key not valid",'
         '"status":"PERMISSION_DENIED"}}', "auth", False),

        ("openai rate limit", 429,
         '{"error":{"message":"Rate limit reached","type":"requests",'
         '"code":"rate_limit_exceeded"}}', "rate_limit", True),
        ("anthropic rate limit", 429,
         '{"type":"error","error":{"type":"rate_limit_error",'
         '"message":"Number of requests has exceeded your rate limit"}}',
         "rate_limit", True),
        ("gemini resource exhausted", 429,
         '{"error":{"code":429,"message":"Quota exceeded",'
         '"status":"RESOURCE_EXHAUSTED"}}', "rate_limit", True),
        ("openai quota is terminal", 429,
         '{"error":{"message":"You exceeded your current quota",'
         '"code":"insufficient_quota"}}', "rate_limit", False),

        ("openai context overflow", 400,
         '{"error":{"message":"This model\'s maximum context length is 8192 '
         'tokens","code":"context_length_exceeded"}}', "context_overflow", False),
        ("anthropic context overflow", 400,
         '{"type":"error","error":{"type":"invalid_request_error",'
         '"message":"prompt is too long: 250000 tokens > 200000"}}',
         "context_overflow", False),
        ("ollama context overflow", 400,
         '{"error":"input length exceeds context length"}',
         "context_overflow", False),
        ("payload too large", 413, "{}", "context_overflow", False),

        ("openai model not found", 404,
         '{"error":{"message":"The model `gpt-9` does not exist",'
         '"code":"model_not_found"}}', "model_not_found", False),
        ("ollama model not found", 404,
         '{"error":"model \'qwen9\' not found"}', "model_not_found", False),
        ("zen model not found", 404, '{"detail":"Model not found"}',
         "model_not_found", False),

        ("azure content filter", 400,
         '{"error":{"message":"blocked by content management policy",'
         '"code":"content_filter"}}', "content_filter", False),
        ("gemini safety block", 400,
         '{"error":{"status":"PROHIBITED_CONTENT","message":"blocked"}}',
         "content_filter", False),

        ("anthropic overloaded", 529,
         '{"type":"error","error":{"type":"overloaded_error",'
         '"message":"Overloaded"}}', "server", True),
        ("openai server error", 500,
         '{"error":{"message":"The server had an error","type":"server_error"}}',
         "server", True),
        ("bare 502", 502, "<html>bad gateway</html>", "server", True),

        ("unclassifiable", 400, '{"error":{"message":"weird"}}', "unknown", False),
    ]

    def test_every_case_maps_to_its_kind(self):
        for label, status, body, kind, retryable in self.CASES:
            with self.subTest(label):
                error = classify_error(status=status, body=body,
                                       provider="p", model="m")
                self.assertEqual(error.kind, kind)
                self.assertEqual(error.retryable, retryable)

    def test_messages_are_actionable(self):
        auth = classify_error(status=401, body="{}", provider="zen", model="m")
        self.assertIn("API key", auth.message)
        self.assertIn("zen", auth.message)

        missing = classify_error(status=404, body="{}", provider="zen",
                                 model="ghost")
        self.assertIn("ghost", missing.message)

        limited = classify_error(status=429, body="{}", provider="zen")
        self.assertIn("Rate limited", limited.message)

        overflow = classify_error(status=413, body="{}", provider="zen")
        self.assertIn("Context window", overflow.message)

    def test_an_unknown_model_reported_as_401_is_still_model_not_found(self):
        # opencode Zen answers an unknown model id with 401, not 404; the
        # words are more trustworthy than the status here.
        error = classify_error(
            status=401, body='{"error":{"message":"Model gpt-9 is not supported"}}',
            provider="zen", model="gpt-9")
        self.assertEqual(error.kind, "model_not_found")
        self.assertIn("gpt-9", error.message)

    def test_a_genuine_401_is_still_auth_even_next_to_a_model_name(self):
        error = classify_error(
            status=401, body='{"error":{"message":"Invalid API key."}}',
            provider="zen", model="north-mini-code-free")
        self.assertEqual(error.kind, "auth")

    def test_raw_body_is_kept_but_truncated(self):
        error = classify_error(status=500, body="x" * 5000)
        self.assertEqual(len(error.body), provider_base.MAX_BODY)

    def test_rate_limit_wording_is_never_read_as_overflow(self):
        # "too many tokens" appears in both families of message.
        self.assertFalse(is_context_overflow(
            "rate limit reached: too many tokens per minute"))
        self.assertTrue(is_context_overflow(
            "input length exceeds context length"))

    def test_html_gateway_pages_do_not_crash_the_parser(self):
        error = classify_error(status=403, body="<html><body>1010</body></html>",
                               provider="zen")
        self.assertEqual(error.kind, "auth")


class TestErrorChunkShape(unittest.TestCase):
    """Errors travel as structure, not as words the model appears to say."""

    def failing(self, status, body):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            provider = OpenAICompatProvider(base_url=f"http://{host}:{port}",
                                            api_key="k", name="zen",
                                            retry=NO_RETRY)
            return list(provider.stream([Msg(role="user", content="hi")], [],
                                        "north-mini-code-free", 64))
        finally:
            server.shutdown()
            server.server_close()

    def test_structured_error_rides_in_usage(self):
        chunks = self.failing(401, '{"error":{"message":"bad key"}}')
        error = chunk_error(chunks[-1])
        self.assertEqual(chunks[-1].stop_reason, "error")
        self.assertEqual(error["kind"], "auth")
        self.assertEqual(error["status"], 401)
        self.assertEqual(error["provider"], "zen")
        self.assertEqual(error["model"], "north-mini-code-free")
        self.assertFalse(error["retryable"])
        self.assertIn("bad key", error["body"])

    def test_error_usage_does_not_poison_the_token_counter(self):
        # agent.py adds usage["input"]/["output"]; both must stay numeric.
        chunks = self.failing(500, "{}")
        usage = chunks[-1].usage
        self.assertEqual(usage.get("input", 0), 0)
        self.assertEqual(usage.get("output", 0), 0)

    def test_context_overflow_is_reported_distinctly_so_it_can_be_compacted(self):
        chunks = self.failing(
            400, '{"error":{"code":"context_length_exceeded",'
                 '"message":"maximum context length is 8192 tokens"}}')
        self.assertEqual(chunk_error(chunks[-1])["kind"], "context_overflow")

    def test_human_readable_text_is_still_emitted_for_the_existing_uis(self):
        # repl.py prints on_text, desktop_worker.py keys off the marker.
        chunks = self.failing(401, '{"error":{"message":"bad key"}}')
        self.assertTrue(chunks[-1].text.lstrip().startswith(
            provider_base.ERROR_TEXT_MARKER))

    def test_text_can_be_suppressed_once_the_uis_read_the_structure(self):
        original = provider_base.ERROR_TEXT_COMPAT
        provider_base.ERROR_TEXT_COMPAT = False
        try:
            chunks = self.failing(401, "{}")
        finally:
            provider_base.ERROR_TEXT_COMPAT = original
        self.assertEqual(chunks[-1].text, "")
        self.assertEqual(chunk_error(chunks[-1])["kind"], "auth")


class TestProviderRetries(unittest.TestCase):
    """The retry policy reaches all the way through a provider."""

    class Flaky:
        def __init__(self, failures, headers=None):
            self.failures = failures
            self.headers = headers or {}
            self.count = 0
            outer = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def do_POST(self):
                    length = int(self.headers.get("Content-Length", 0))
                    self.rfile.read(length)
                    outer.count += 1
                    if outer.count <= outer.failures:
                        self.send_response(503)
                        for key, value in outer.headers.items():
                            self.send_header(key, value)
                        self.send_header("Content-Length", "2")
                        self.end_headers()
                        self.wfile.write(b"{}")
                        return
                    body = (b'data: {"choices":[{"index":0,"delta":'
                            b'{"content":"ok"}}]}\n\n'
                            b'data: {"choices":[{"index":0,"delta":{},'
                            b'"finish_reason":"stop"}]}\n\n'
                            b"data: [DONE]\n\n")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass

            self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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

    def test_transient_failure_is_retried_and_the_run_survives(self):
        with self.Flaky(failures=2) as server:
            provider = OpenAICompatProvider(base_url=server.url, api_key="k",
                                            retry=FAST)
            chunks = list(provider.stream([Msg(role="user", content="hi")], [],
                                          "m", 64))
        self.assertEqual("".join(c.text for c in chunks), "ok")
        self.assertEqual(server.count, 3)
        self.assertFalse(any(c.stop_reason == "error" for c in chunks))

    def test_exhausted_retries_surface_as_a_server_error_kind(self):
        with self.Flaky(failures=99) as server:
            provider = OpenAICompatProvider(base_url=server.url, api_key="k",
                                            retry=FAST)
            chunks = list(provider.stream([Msg(role="user", content="hi")], [],
                                          "m", 64))
        self.assertEqual(server.count, FAST.max_attempts)
        error = chunk_error(chunks[-1])
        self.assertEqual(error["kind"], "server")
        self.assertTrue(error["retryable"])


# ==========================================================================
# Reasoning
# ==========================================================================

class TestReasoningDialects(unittest.TestCase):
    """Every key a reasoning model has been seen to use, and none mistaken
    for answer text."""

    VARIANTS = [
        ("reasoning_content", {"reasoning_content": "why"}),        # deepseek, glm
        ("reasoning", {"reasoning": "why"}),                        # gpt-oss, xai
        ("thinking", {"thinking": "why"}),                          # kimi
        ("thought", {"thought": "why"}),
        ("reasoning object", {"reasoning": {"content": "why"}}),
        ("reasoning summary object", {"reasoning": {"summary": "why"}}),
        ("reasoning_details list",
         {"reasoning_details": [{"type": "reasoning.text", "text": "why"}]}),
    ]

    def test_every_dialect_lands_on_the_reasoning_channel(self):
        for label, delta in self.VARIANTS:
            with self.subTest(label):
                self.assertEqual(reasoning_from_delta(delta), "why")

    def test_none_of_them_are_mistaken_for_text(self):
        for label, delta in self.VARIANTS:
            with self.subTest(label):
                with SSEServer([openai_frame(**delta), "[DONE]"]) as server:
                    provider = OpenAICompatProvider(base_url=server.url,
                                                    api_key="k", retry=NO_RETRY)
                    chunks = list(provider.stream(
                        [Msg(role="user", content="hi")], [], "m", 64))
                self.assertEqual("".join(c.text for c in chunks), "")
                self.assertEqual("".join(c.reasoning for c in chunks), "why")

    def test_content_and_reasoning_in_one_delta_stay_separate(self):
        with SSEServer([openai_frame(content="answer", reasoning="thought"),
                        "[DONE]"]) as server:
            provider = OpenAICompatProvider(base_url=server.url, api_key="k",
                                            retry=NO_RETRY)
            chunks = list(provider.stream([Msg(role="user", content="hi")], [],
                                          "m", 64))
        self.assertEqual("".join(c.text for c in chunks), "answer")
        self.assertEqual("".join(c.reasoning for c in chunks), "thought")

    def test_empty_reasoning_is_not_emitted(self):
        self.assertEqual(reasoning_from_delta({"reasoning": ""}), "")
        self.assertEqual(reasoning_from_delta({"content": "hi"}), "")

    def test_mirrored_channels_are_not_rendered_twice(self):
        # Observed live on opencode Zen: the same token arrives under both
        # `reasoning` and `reasoning_details`. Concatenating them prints
        # "TheThe user user wants wants...".
        delta = {"role": "assistant", "content": "",
                 "reasoning": "The user",
                 "reasoning_details": [{"type": "reasoning.text",
                                        "text": "The user", "index": 0}]}
        self.assertEqual(reasoning_from_delta(delta), "The user")

    def test_reasoning_content_and_reasoning_mirrors_collapse(self):
        self.assertEqual(
            reasoning_from_delta({"reasoning": "hm", "reasoning_content": "hm"}),
            "hm")


class TestThinkTags(unittest.TestCase):
    """Endpoints without a reasoning channel inline <think> in content."""

    def split(self, *pieces):
        splitter = ThinkTagSplitter()
        out = []
        for piece in pieces:
            out.extend(splitter.feed(piece))
        out.extend(splitter.flush())
        text = "".join(t for c, t in out if c == "text")
        reasoning = "".join(t for c, t in out if c == "reasoning")
        return text, reasoning

    def test_inline_thinking_is_routed_away_from_the_answer(self):
        self.assertEqual(self.split("<think>hmm</think>answer"),
                         ("answer", "hmm"))

    def test_tags_split_across_deltas(self):
        self.assertEqual(self.split("<thi", "nk>hm", "m</thi", "nk>ans"),
                         ("ans", "hmm"))

    def test_plain_text_is_untouched_and_not_held_back(self):
        splitter = ThinkTagSplitter()
        self.assertEqual(splitter.feed("hello"), [("text", "hello")])

    def test_a_lone_angle_bracket_is_released_at_the_end(self):
        self.assertEqual(self.split("a < b"), ("a < b", ""))

    def test_unterminated_thinking_is_never_promoted_to_text(self):
        self.assertEqual(self.split("<think>still going"), ("", "still going"))

    def test_through_the_provider(self):
        frames = [openai_frame(content="<think>rea"),
                  openai_frame(content="son</think>out"), "[DONE]"]
        with SSEServer(frames) as server:
            provider = OpenAICompatProvider(base_url=server.url, api_key="k",
                                            retry=NO_RETRY)
            chunks = list(provider.stream([Msg(role="user", content="hi")], [],
                                          "m", 64))
        self.assertEqual("".join(c.text for c in chunks), "out")
        self.assertEqual("".join(c.reasoning for c in chunks), "reason")


# ==========================================================================
# Anthropic specifics
# ==========================================================================

class TestAnthropicCeilingAndCaching(unittest.TestCase):
    def request_for(self, model, max_tokens=200000, messages=None, tools=(SPEC,),
                    cache=True):
        with SSEServer([]) as server:
            provider = AnthropicProvider(base_url=server.url, api_key="k",
                                         retry=NO_RETRY, cache=cache)
            list(provider.stream(messages or [Msg(role="user", content="hi")],
                                 list(tools), model, max_tokens))
            return server.received

    def test_max_tokens_table(self):
        self.assertEqual(max_output_tokens("claude-3-5-sonnet-20241022"), 8192)
        self.assertEqual(max_output_tokens("claude-3-haiku-20240307"), 4096)
        self.assertEqual(max_output_tokens("claude-opus-4-1-20250805"), 32000)
        self.assertEqual(max_output_tokens("claude-sonnet-4-5"), 64000)
        # An unlisted model is never capped by a stale table.
        self.assertIsNone(max_output_tokens("claude-sonnet-9"))

    def test_max_tokens_is_clamped_to_the_model_ceiling(self):
        received = self.request_for("claude-3-5-sonnet-20241022")
        self.assertEqual(received["max_tokens"], 8192)

    def test_unknown_models_pass_max_tokens_through(self):
        received = self.request_for("claude-sonnet-9", max_tokens=123456)
        self.assertEqual(received["max_tokens"], 123456)

    def test_a_small_request_is_never_inflated(self):
        received = self.request_for("claude-3-haiku-20240307", max_tokens=100)
        self.assertEqual(received["max_tokens"], 100)

    def test_long_system_prompts_get_a_cache_breakpoint(self):
        system = "You are haikode. " * 400          # well past the minimum
        received = self.request_for(
            "claude-sonnet-9",
            messages=[Msg(role="system", content=system),
                      Msg(role="user", content="hi")])
        self.assertEqual(received["system"][0]["cache_control"],
                         {"type": "ephemeral"})
        self.assertEqual(received["system"][0]["text"], system)

    def test_short_system_prompts_stay_plain_strings(self):
        # Below the provider minimum a breakpoint cannot hit; spending one
        # would only burn a slot.
        received = self.request_for(
            "claude-sonnet-9",
            messages=[Msg(role="system", content="be brief"),
                      Msg(role="user", content="hi")])
        self.assertEqual(received["system"], "be brief")

    def test_large_tool_sets_get_a_single_trailing_breakpoint(self):
        tools = [ToolSpec(name=f"t{i}", description="d" * 200,
                          parameters={"type": "object",
                                      "properties": {"a": {"type": "string"}}})
                 for i in range(30)]
        received = self.request_for("claude-sonnet-9", tools=tools)
        marked = [t for t in received["tools"] if "cache_control" in t]
        self.assertEqual(len(marked), 1)
        self.assertEqual(marked[0]["name"], "t29")

    def test_caching_can_be_turned_off(self):
        system = "You are haikode. " * 400
        received = self.request_for(
            "claude-sonnet-9", cache=False,
            messages=[Msg(role="system", content=system),
                      Msg(role="user", content="hi")])
        self.assertEqual(received["system"], system)


class TestAnthropicStreamDetails(unittest.TestCase):
    def collect(self, frames):
        with SSEServer(frames) as server:
            provider = AnthropicProvider(base_url=server.url, api_key="k",
                                         retry=NO_RETRY)
            return list(provider.stream([Msg(role="user", content="hi")], [],
                                        "claude-sonnet-9", 256))

    def test_prompt_tokens_and_cache_counters_come_from_message_start(self):
        chunks = self.collect([
            {"type": "message_start", "message": {"usage": {
                "input_tokens": 12, "cache_read_input_tokens": 900,
                "cache_creation_input_tokens": 30}}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"output_tokens": 4}},
        ])
        self.assertEqual(chunks[0].usage["input"], 12)
        self.assertEqual(chunks[0].usage["cache_read"], 900)
        self.assertEqual(chunks[0].usage["cache_write"], 30)
        self.assertEqual(chunks[-1].usage["output"], 4)
        self.assertEqual(sum(c.usage.get("input", 0) for c in chunks if c.usage), 12)

    def test_prompt_tokens_fall_back_to_message_delta(self):
        chunks = self.collect([
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
             "usage": {"input_tokens": 5, "output_tokens": 1}}])
        self.assertEqual(chunks[-1].usage, {"input": 5, "output": 1})

    def test_signature_delta_is_not_answer_text(self):
        chunks = self.collect([
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "abc"}}])
        self.assertEqual("".join(c.text for c in chunks), "")
        self.assertEqual("".join(c.reasoning for c in chunks), "")

    def test_max_tokens_stop_reason_is_normalised_to_length(self):
        chunks = self.collect([
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
             "usage": {"output_tokens": 9}}])
        self.assertEqual(chunks[-1].stop_reason, "length")

    def test_error_event_carries_structure(self):
        chunks = self.collect([
            {"type": "error", "error": {"type": "overloaded_error",
                                        "message": "Overloaded"}}])
        error = chunk_error(chunks[-1])
        self.assertEqual(chunks[-1].stop_reason, "error")
        self.assertEqual(error["kind"], "server")
        self.assertTrue(error["retryable"])


# ==========================================================================
# Gemini
# ==========================================================================

def gemini_frame(parts=None, finish=None, usage=None):
    candidate = {}
    if parts is not None:
        candidate["content"] = {"role": "model", "parts": parts}
    if finish:
        candidate["finishReason"] = finish
    event = {"candidates": [candidate]}
    if usage:
        event["usageMetadata"] = usage
    return event


class TestGeminiDialect(unittest.TestCase):
    def collect(self, frames, messages=None, tools=(SPEC,), model="gemini-3-pro"):
        with SSEServer(frames) as server:
            provider = GeminiProvider(base_url=server.url, api_key="k",
                                      retry=NO_RETRY)
            chunks = list(provider.stream(
                messages or [Msg(role="user", content="hi")],
                list(tools), model, 256))
            return chunks, server.received

    def test_text_parts(self):
        chunks, _ = self.collect([gemini_frame([{"text": "he"}]),
                                  gemini_frame([{"text": "llo"}], finish="STOP")])
        self.assertEqual("".join(c.text for c in chunks), "hello")
        self.assertEqual(chunks[-1].stop_reason, "stop")

    def test_thought_parts_are_reasoning_not_text(self):
        chunks, _ = self.collect([
            gemini_frame([{"text": "planning", "thought": True},
                          {"text": "answer"}], finish="STOP")])
        self.assertEqual("".join(c.text for c in chunks), "answer")
        self.assertEqual("".join(c.reasoning for c in chunks), "planning")

    def test_function_calls_arrive_whole_and_get_synthetic_ids(self):
        chunks, _ = self.collect([
            gemini_frame([{"functionCall": {"name": "read",
                                            "args": {"filePath": "a.txt"}}}],
                         finish="STOP")])
        deltas = [c.tool_call_delta for c in chunks if c.tool_call_delta]
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0]["name"], "read")
        self.assertTrue(deltas[0]["id"])
        self.assertEqual(json.loads(deltas[0]["arguments"]), {"filePath": "a.txt"})
        self.assertEqual(chunks[-1].stop_reason, "tool_calls")

    def test_tools_are_declared_natively(self):
        _, received = self.collect([])
        declarations = received["tools"][0]["functionDeclarations"]
        self.assertEqual(declarations[0]["name"], "read")
        self.assertEqual(declarations[0]["parameters"]["properties"]["filePath"],
                         {"type": "string"})
        self.assertEqual(received["toolConfig"]["functionCallingConfig"]["mode"],
                         "AUTO")

    def test_no_tools_key_when_none_offered(self):
        _, received = self.collect([], tools=())
        self.assertNotIn("tools", received)

    def test_system_messages_become_system_instruction(self):
        _, received = self.collect([], messages=[
            Msg(role="system", content="be brief"),
            Msg(role="user", content="hi")])
        self.assertEqual(received["systemInstruction"]["parts"][0]["text"],
                         "be brief")
        self.assertEqual([c["role"] for c in received["contents"]], ["user"])

    def test_tool_results_are_addressed_by_function_name(self):
        # Gemini has no call ids; the name must be recovered from the model
        # turn that requested the call.
        messages = [
            Msg(role="user", content="read them"),
            Msg(role="assistant", content="ok", tool_calls=[
                ToolCall(id="read-0", name="read", arguments={"filePath": "a"}),
                ToolCall(id="read-1", name="read", arguments={"filePath": "b"})]),
            Msg(role="tool", tool_call_id="read-0", content="A"),
            Msg(role="tool", tool_call_id="read-1", content="B"),
        ]
        _, received = self.collect([], messages=messages)
        contents = received["contents"]
        self.assertEqual([c["role"] for c in contents], ["user", "model", "user"])
        self.assertEqual([p["functionCall"]["name"]
                          for p in contents[1]["parts"][1:]], ["read", "read"])
        # Both results in ONE user turn.
        self.assertEqual(len(contents[2]["parts"]), 2)
        self.assertEqual(contents[2]["parts"][0]["functionResponse"]["response"],
                         {"content": "A"})

    def test_thinking_is_requested_so_reasoning_is_actually_streamed(self):
        _, received = self.collect([])
        self.assertTrue(
            received["generationConfig"]["thinkingConfig"]["includeThoughts"])
        self.assertEqual(received["generationConfig"]["maxOutputTokens"], 256)

    def test_usage_sums_thoughts_into_output_and_is_reported_once(self):
        usage = {"promptTokenCount": 10, "candidatesTokenCount": 4,
                 "thoughtsTokenCount": 7, "cachedContentTokenCount": 6}
        chunks, _ = self.collect([gemini_frame([{"text": "a"}], usage=usage),
                                  gemini_frame([{"text": "b"}], finish="STOP",
                                               usage=usage)])
        reported = [c.usage for c in chunks if c.usage]
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0]["input"], 10)
        self.assertEqual(reported[0]["output"], 11)
        self.assertEqual(reported[0]["reasoning"], 7)
        self.assertEqual(reported[0]["cache_read"], 6)

    def test_max_tokens_finish_becomes_length(self):
        chunks, _ = self.collect([gemini_frame([{"text": "x"}],
                                               finish="MAX_TOKENS")])
        self.assertEqual(chunks[-1].stop_reason, "length")

    def test_safety_finish_is_a_content_filter_error(self):
        chunks, _ = self.collect([gemini_frame([], finish="SAFETY")])
        self.assertEqual(chunks[-1].stop_reason, "error")
        self.assertEqual(chunk_error(chunks[-1])["kind"], "content_filter")

    def test_prompt_feedback_block_is_a_content_filter_error(self):
        chunks, _ = self.collect([
            {"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"}}])
        self.assertEqual(chunk_error(chunks[-1])["kind"], "content_filter")

    def test_inline_error_frame_is_classified(self):
        chunks, _ = self.collect([
            {"error": {"code": 429, "message": "Quota exceeded",
                       "status": "RESOURCE_EXHAUSTED"}}])
        error = chunk_error(chunks[-1])
        self.assertEqual(error["kind"], "rate_limit")
        self.assertTrue(error["retryable"])

    def test_api_key_travels_in_a_header_not_the_query_string(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen["path"] = self.path
                seen["key"] = self.headers.get("x-goog-api-key")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            host, port = server.server_address[:2]
            provider = GeminiProvider(base_url=f"http://{host}:{port}",
                                      api_key="secret", retry=NO_RETRY)
            list(provider.stream([Msg(role="user", content="hi")], [],
                                 "gemini-3-pro", 64))
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(seen["key"], "secret")
        self.assertNotIn("secret", seen["path"])
        self.assertIn("gemini-3-pro:streamGenerateContent", seen["path"])
        self.assertIn("alt=sse", seen["path"])


class TestGeminiSchemaProjection(unittest.TestCase):
    """v1beta rejects the JSON Schema keywords every generator emits."""

    def test_unsupported_keywords_are_dropped(self):
        schema = {"type": "object", "$schema": "http://json-schema.org/schema#",
                  "additionalProperties": False,
                  "properties": {"a": {"type": "string", "default": "x",
                                       "description": "an a"}},
                  "required": ["a"]}
        self.assertEqual(sanitize_schema(schema), {
            "type": "object",
            "properties": {"a": {"type": "string", "description": "an a"}},
            "required": ["a"]})

    def test_nested_arrays_are_projected_too(self):
        schema = {"type": "array",
                  "items": {"type": "object", "additionalProperties": True,
                            "properties": {"b": {"type": "number"}}}}
        self.assertEqual(sanitize_schema(schema), {
            "type": "array",
            "items": {"type": "object", "properties": {"b": {"type": "number"}}}})

    def test_anyof_collapses_to_a_typed_branch(self):
        schema = {"anyOf": [{"type": "null"}, {"type": "string"}]}
        self.assertEqual(sanitize_schema(schema)["type"], "null")

    def test_a_parameterless_tool_omits_the_parameters_key(self):
        spec = ToolSpec(name="now", description="clock",
                        parameters={"type": "object", "properties": {}})
        provider = GeminiProvider(api_key="k")
        declaration = provider._tools([spec])[0]["functionDeclarations"][0]
        self.assertNotIn("parameters", declaration)
        self.assertEqual(declaration["name"], "now")


if __name__ == "__main__":
    unittest.main()

