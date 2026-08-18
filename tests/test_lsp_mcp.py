"""
Tests for the LSP and MCP clients.

Two layers. The framing and conversion logic is pure and tested directly, and
the transport is driven through a stub process whose stdout is a real pipe.

On top of that, LSPRealServerTests and MCPRealServerTests run *real* servers:
small Python programs (LSP_SERVER_SOURCE, MCP_SERVER_SOURCE below) that speak
the protocols over a real pipe to a real child process. Stub tests prove the
code does what we think; only a real server proves the handshake is one an
actual server would accept, that a mid-request exit unblocks the caller instead
of hanging it, and that close() leaves nothing behind.
"""

import fnmatch
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from haikode import lsp, mcp
from haikode.tool.base import ToolContext


class _Sink:
    """Stand-in for process.stdin that hands writes to the stub server."""

    def __init__(self, on_write):
        self.on_write = on_write
        self.data = b""
        self.closed = False

    def write(self, data):
        self.data += data
        self.on_write(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class StubProcess:
    """
    Fake subprocess: the client's writes are decoded and fed to `handler`,
    whose replies are pushed back down a real pipe so the reader thread sees
    genuine byte-level traffic.
    """

    def __init__(self, handler, encode, decode):
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self._out = os.fdopen(write_fd, "wb", buffering=0)
        self.stdin = _Sink(self._client_wrote)
        self.returncode = None
        self.requests = []
        self._handler = handler
        self._encode = encode
        self._decode = decode
        self._buffer = b""

    def _client_wrote(self, data):
        self._buffer += data
        messages, self._buffer = self._decode(self._buffer)
        for message in messages:
            self.requests.append(message)
            for reply in self._handler(message) or []:
                self._out.write(self._encode(reply))

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        # Faithful to Popen: a live process raises rather than returning None,
        # which is what forces close() to escalate to terminate/kill.
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return self.returncode

    def terminate(self):
        self.die()

    def kill(self):
        self.die()

    def die(self):
        if self.returncode is None:
            self.returncode = 1
            self._out.close()

    def cleanup(self):
        self.die()
        try:
            self.stdout.close()
        except OSError:
            pass


def _await(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# --- LSP framing -------------------------------------------------------

class LSPFramingTests(unittest.TestCase):
    def test_encode_uses_content_length_header(self):
        raw = lsp.encode_message({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        header, _, body = raw.partition(b"\r\n\r\n")
        self.assertIn(b"Content-Length: %d" % len(body), header)
        self.assertEqual(json.loads(body.decode()),
                         {"jsonrpc": "2.0", "id": 1, "method": "ping"})

    def test_roundtrip_of_several_messages_in_one_chunk(self):
        buffer = (lsp.encode_message({"id": 1}) + lsp.encode_message({"id": 2})
                  + lsp.encode_message({"id": 3}))
        messages, rest = lsp.decode_messages(buffer)
        self.assertEqual([m["id"] for m in messages], [1, 2, 3])
        self.assertEqual(rest, b"")

    def test_partial_message_is_kept_until_complete(self):
        whole = lsp.encode_message({"id": 7, "result": {"ok": True}})
        messages, rest = lsp.decode_messages(whole[:12])
        self.assertEqual(messages, [])
        messages, rest = lsp.decode_messages(rest + whole[12:-4])
        self.assertEqual(messages, [])
        messages, rest = lsp.decode_messages(rest + whole[-4:])
        self.assertEqual(messages, [{"id": 7, "result": {"ok": True}}])
        self.assertEqual(rest, b"")

    def test_bare_lf_headers_are_accepted(self):
        body = b'{"id":5}'
        buffer = b"Content-Length: %d\n\n" % len(body) + body
        messages, rest = lsp.decode_messages(buffer)
        self.assertEqual(messages, [{"id": 5}])
        self.assertEqual(rest, b"")

    def test_garbage_is_dropped_without_looping(self):
        good = lsp.encode_message({"id": 9})
        messages, rest = lsp.decode_messages(b"noise here\r\n\r\n" + good)
        self.assertEqual(messages, [{"id": 9}])
        self.assertEqual(rest, b"")

    def test_unparseable_body_is_skipped(self):
        bad = b"Content-Length: 5\r\n\r\nnotjs"
        messages, rest = lsp.decode_messages(bad + lsp.encode_message({"id": 2}))
        self.assertEqual(messages, [{"id": 2}])
        self.assertEqual(rest, b"")

    def test_nonsense_content_length_does_not_stall_the_stream(self):
        good = lsp.encode_message({"id": 4})
        for bad in (b"Content-Length: -5\r\n\r\n",
                    b"Content-Length: 999999999999\r\n\r\n"):
            messages, rest = lsp.decode_messages(bad + good)
            self.assertEqual(messages, [{"id": 4}])
            self.assertEqual(rest, b"")

    def test_parse_headers_lowercases_names(self):
        headers = lsp.parse_headers(b"Content-Length: 12\r\nContent-Type: x")
        self.assertEqual(headers["content-length"], "12")
        self.assertEqual(headers["content-type"], "x")

    def test_pump_messages_stops_at_eof(self):
        chunks = [lsp.encode_message({"id": 1}), lsp.encode_message({"id": 2}), b""]
        seen = []
        lsp.pump_messages(lambda n: chunks.pop(0) if chunks else b"", seen.append)
        self.assertEqual([m["id"] for m in seen], [1, 2])

    def test_pump_messages_drops_an_unframed_flood(self):
        # A process spewing bytes with no header must not grow the buffer
        # without bound; the reader resynchronises on the next real message.
        chunks = [b"x" * 200, lsp.encode_message({"id": 1}), b""]
        seen = []
        with patch.object(lsp, "MAX_BUFFER_BYTES", 100):
            lsp.pump_messages(lambda n: chunks.pop(0) if chunks else b"",
                              seen.append)
        self.assertEqual([m["id"] for m in seen], [1])

    def test_pump_messages_survives_a_throwing_handler(self):
        chunks = [lsp.encode_message({"id": 1}), lsp.encode_message({"id": 2}), b""]
        seen = []

        def handler(message):
            if message["id"] == 1:
                raise ValueError("boom")
            seen.append(message)

        lsp.pump_messages(lambda n: chunks.pop(0) if chunks else b"", handler)
        self.assertEqual([m["id"] for m in seen], [2])


# --- LSP transport -----------------------------------------------------

class LSPTransportTests(unittest.TestCase):
    def _client(self, handler):
        client = lsp.LSPClient(["fake-server"], root=".")
        stub = StubProcess(handler, lsp.encode_message, lsp.decode_messages)
        client.process = stub
        thread = threading.Thread(target=client._read_loop, daemon=True)
        thread.start()
        self.addCleanup(stub.cleanup)
        return client, stub

    def test_responses_are_correlated_by_id(self):
        def handler(message):
            if message.get("method") == "slow":
                return []  # answered later, out of order
            return [{"jsonrpc": "2.0", "id": message["id"],
                     "result": {"echo": message["method"]}}]

        client, stub = self._client(handler)
        results = {}

        def call(method):
            results[method] = client.request(method, {}, timeout=5.0)

        slow = threading.Thread(target=lambda: results.setdefault(
            "slow", self._safe(client, "slow")), daemon=True)
        slow.start()
        self.assertTrue(_await(lambda: any(
            m.get("method") == "slow" for m in stub.requests)))

        call("first")
        call("second")
        self.assertEqual(results["first"], {"echo": "first"})
        self.assertEqual(results["second"], {"echo": "second"})

        # Now answer the first request; ids must still line up.
        slow_id = [m["id"] for m in stub.requests if m.get("method") == "slow"][0]
        stub._out.write(lsp.encode_message(
            {"jsonrpc": "2.0", "id": slow_id, "result": {"echo": "slow"}}))
        slow.join(timeout=5)
        self.assertEqual(results["slow"], {"echo": "slow"})

    @staticmethod
    def _safe(client, method):
        try:
            return client.request(method, {}, timeout=5.0)
        except lsp.LSPError as e:
            return e

    def test_request_times_out_instead_of_hanging(self):
        client, _stub = self._client(lambda message: [])
        with self.assertRaises(lsp.LSPTimeout):
            client.request("initialize", {}, timeout=0.05)

    def test_server_error_becomes_an_exception(self):
        client, _stub = self._client(lambda m: [
            {"jsonrpc": "2.0", "id": m["id"],
             "error": {"code": -32603, "message": "nope"}}])
        with self.assertRaises(lsp.LSPError):
            client.request("initialize", {}, timeout=5.0)

    def test_dead_server_releases_waiting_requests(self):
        client, stub = self._client(lambda message: [])
        outcome = []

        thread = threading.Thread(
            target=lambda: outcome.append(self._safe(client, "initialize")),
            daemon=True)
        thread.start()
        self.assertTrue(_await(lambda: bool(stub.requests)))
        stub.die()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], lsp.LSPError)

    def test_server_requests_are_always_answered(self):
        client, stub = self._client(lambda message: [])
        stub._out.write(lsp.encode_message(
            {"jsonrpc": "2.0", "id": 41, "method": "workspace/workspaceFolders"}))
        stub._out.write(lsp.encode_message(
            {"jsonrpc": "2.0", "id": 42, "method": "totally/unknown"}))

        def replies():
            messages, _ = lsp.decode_messages(stub.stdin.data)
            return {m.get("id"): m for m in messages if "id" in m}

        self.assertTrue(_await(lambda: {41, 42} <= set(replies())))
        answers = replies()
        self.assertIn("result", answers[41])
        self.assertEqual(answers[41]["result"][0]["name"], "workspace")
        self.assertEqual(answers[42]["error"]["code"], lsp.METHOD_NOT_FOUND)

    def test_publish_diagnostics_are_collected_per_file(self):
        client, stub = self._client(lambda message: [])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "x.py")
            with open(path, "w") as handle:
                handle.write("import os\n")

            def publish():
                _await(lambda: any(m.get("method") == "textDocument/didOpen"
                                   for m in stub.requests))
                stub._out.write(lsp.encode_message({
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {
                        "uri": lsp.path_to_uri(path),
                        "diagnostics": [{
                            "range": {"start": {"line": 11, "character": 4},
                                      "end": {"line": 11, "character": 7}},
                            "severity": 1,
                            "message": "undefined name 'foo'",
                            "source": "pyflakes",
                        }],
                    },
                }))

            publisher = threading.Thread(target=publish, daemon=True)
            publisher.start()
            diags = client.diagnostics(path, wait=5.0)
            publisher.join(timeout=5)

        self.assertEqual(diags, [{
            "severity": "error", "line": 12, "character": 5,
            "message": "undefined name 'foo'", "source": "pyflakes"}])

    def test_diagnostics_return_empty_when_nothing_is_published(self):
        client, _stub = self._client(lambda message: [])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "x.py")
            with open(path, "w") as handle:
                handle.write("x = 1\n")
            started = time.monotonic()
            self.assertEqual(client.diagnostics(path, wait=0.1), [])
            self.assertLess(time.monotonic() - started, 3.0)

    def test_didopen_then_didchange_bumps_the_version(self):
        client, stub = self._client(lambda message: [])
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "x.py")
            with open(path, "w") as handle:
                handle.write("x = 1\n")
            self.assertEqual(client.open_file(path), 0)
            self.assertEqual(client.open_file(path), 1)
            client.close_file(path)

        methods = [m.get("method") for m in stub.requests]
        self.assertEqual(methods, ["textDocument/didOpen", "textDocument/didChange",
                                   "textDocument/didClose"])

    def test_incremental_sync_replaces_the_previous_whole_document(self):
        # An "incremental" server gets one change spanning the document it
        # currently holds — the range must describe the *old* content.
        client, stub = self._client(lambda message: [])
        client.capabilities = {
            "textDocumentSync": lsp.TEXT_DOCUMENT_SYNC_INCREMENTAL}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "x.py")
            with open(path, "w") as handle:
                handle.write("one\ntwo\nthree!!")
            client.open_file(path)
            with open(path, "w") as handle:
                handle.write("short")
            client.change_file(path)
            client.change_file(path, "x")

        changes = [m["params"]["contentChanges"][0] for m in stub.requests
                   if m.get("method") == "textDocument/didChange"]
        self.assertEqual(changes[0]["range"],
                         {"start": {"line": 0, "character": 0},
                          "end": {"line": 2, "character": 7}})
        self.assertEqual(changes[0]["text"], "short")
        # The second change spans what the first one installed, not the file
        # that was originally opened.
        self.assertEqual(changes[1]["range"]["end"],
                         {"line": 0, "character": 5})

    def test_failed_handshake_does_not_leak_the_process(self):
        # The caller only ever sees the exception, so start() itself has to
        # reap a server that hung during initialize.
        stub = StubProcess(lambda message: [], lsp.encode_message,
                           lsp.decode_messages)
        self.addCleanup(stub.cleanup)
        client = lsp.LSPClient(["fake-server"], root=".")
        with patch.object(lsp.subprocess, "Popen", return_value=stub), \
                patch.object(lsp, "INITIALIZE_TIMEOUT", 0.05), \
                patch.object(lsp, "PROCESS_EXIT_TIMEOUT", 0.05):
            with self.assertRaises(lsp.LSPTimeout):
                client.start()
        self.assertTrue(stub.stdin.closed)
        self.assertIsNotNone(stub.returncode)

    def test_close_terminates_a_process_that_never_answers(self):
        client, stub = self._client(lambda message: [])
        client.initialized = True
        with patch.object(lsp, "SHUTDOWN_TIMEOUT", 0.05), \
                patch.object(lsp, "PROCESS_EXIT_TIMEOUT", 0.05):
            client.close()
        # shutdown was attempted, then the unresponsive process was killed.
        self.assertTrue(any(m.get("method") == "shutdown" for m in stub.requests))
        self.assertTrue(stub.stdin.closed)
        self.assertIsNotNone(stub.returncode)
        self.assertFalse(client.initialized)


# --- LSP discovery and formatting --------------------------------------

class LSPDiscoveryTests(unittest.TestCase):
    def setUp(self):
        # detect_server memoises PATH lookups for the life of the process, so
        # a patched shutil.which is only visible with a cold cache.
        lsp.clear_binary_cache()

    def tearDown(self):
        lsp.clear_binary_cache()

    def test_detect_language_by_extension(self):
        self.assertEqual(lsp.detect_language("a/b/x.py"), "python")
        self.assertEqual(lsp.detect_language("x.TS"), "typescript")
        self.assertIsNone(lsp.detect_language("x.unknownext"))

    def test_detect_server_is_none_when_binary_is_absent(self):
        with patch.object(lsp.shutil, "which", return_value=None):
            self.assertIsNone(lsp.detect_server("x.py"))
            self.assertIsNone(lsp.detect_server("main.c"))

    def test_detect_server_is_none_for_unknown_language(self):
        with patch.object(lsp.shutil, "which", return_value="/bin/anything"):
            self.assertIsNone(lsp.detect_server("notes.unknownext"))

    def test_detect_server_resolves_the_first_available_binary(self):
        with patch.object(lsp.shutil, "which",
                          side_effect=lambda name: "/bin/" + name
                          if name == "pyright-langserver" else None):
            self.assertEqual(lsp.detect_server("x.py"),
                             ["/bin/pyright-langserver", "--stdio"])

    def test_uri_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "a b", "c.py")
            uri = lsp.path_to_uri(path)
            self.assertTrue(uri.startswith("file://"))
            self.assertNotIn(" ", uri)
            self.assertEqual(lsp.uri_to_path(uri), path)

    def test_manager_returns_empty_without_a_server(self):
        with patch.object(lsp.shutil, "which", return_value=None):
            manager = lsp.LSPManager(root=".")
            self.assertEqual(manager.diagnostics("x.py"), [])
            self.assertEqual(manager.report("x.py"), "")
            self.assertIsNone(manager.client_for("x.py"))
            manager.shutdown_all()

    def test_manager_disabled_is_a_no_op(self):
        manager = lsp.LSPManager(root=".", enabled=False)
        self.assertEqual(manager.diagnostics("x.py"), [])
        self.assertIsNone(manager.client_for("x.py"))

    def test_manager_from_config_honours_lsp_false(self):
        class FakeConfig:
            data = {"lsp": False}

        self.assertFalse(lsp.LSPManager.from_config(FakeConfig(), ".").enabled)
        self.assertTrue(lsp.LSPManager.from_config(None, ".").enabled)


class DiagnosticFormattingTests(unittest.TestCase):
    def test_normalize_converts_severity_and_one_bases_positions(self):
        self.assertEqual(lsp.normalize_diagnostic({
            "range": {"start": {"line": 0, "character": 0}},
            "severity": 2, "message": " unused ", "source": "ruff",
        }), {"severity": "warning", "line": 1, "character": 1,
             "message": "unused", "source": "ruff"})

    def test_normalize_defaults_to_error_without_severity(self):
        self.assertEqual(lsp.normalize_diagnostic({"message": "x"})["severity"],
                         "error")

    def test_format_matches_the_expected_line_shape(self):
        text = lsp.format_diagnostics([{
            "severity": "error", "line": 12, "character": 5,
            "message": "undefined name 'foo'", "source": "pyflakes"}], "src/x.py")
        self.assertEqual(text,
                         "src/x.py:12:5 error: undefined name 'foo' (pyflakes)")

    def test_format_omits_an_empty_source(self):
        text = lsp.format_diagnostics([{
            "severity": "warning", "line": 3, "character": 1,
            "message": "hmm", "source": ""}], "a.py")
        self.assertEqual(text, "a.py:3:1 warning: hmm")

    def test_format_sorts_errors_first_and_truncates(self):
        diags = [{"severity": "hint", "line": 1, "character": 1,
                  "message": "h", "source": ""}]
        diags += [{"severity": "error", "line": n, "character": 1,
                   "message": "e%d" % n, "source": ""} for n in range(1, 4)]
        text = lsp.format_diagnostics(diags, "a.py", limit=2)
        self.assertEqual(text.splitlines(), [
            "a.py:1:1 error: e1", "a.py:2:1 error: e2", "... and 2 more"])

    def test_format_of_nothing_is_empty(self):
        self.assertEqual(lsp.format_diagnostics([], "a.py"), "")


# --- MCP framing and transport -----------------------------------------

class MCPFramingTests(unittest.TestCase):
    def test_frames_are_newline_delimited_single_lines(self):
        raw = mcp.encode_frame({"jsonrpc": "2.0", "method": "a\nb"})
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual(json.loads(raw.decode())["method"], "a\nb")

    def test_decode_frames_returns_remainder(self):
        buffer = mcp.encode_frame({"id": 1}) + b'{"id": 2}'
        messages, rest = mcp.decode_frames(buffer)
        self.assertEqual(messages, [{"id": 1}])
        self.assertEqual(rest, b'{"id": 2}')
        messages, rest = mcp.decode_frames(rest + b"\n")
        self.assertEqual(messages, [{"id": 2}])
        self.assertEqual(rest, b"")

    def test_non_json_stdout_noise_is_ignored(self):
        buffer = b"Server listening on stdio\n" + mcp.encode_frame({"id": 3})
        messages, rest = mcp.decode_frames(buffer)
        self.assertEqual(messages, [{"id": 3}])
        self.assertEqual(rest, b"")

    def test_pump_frames_drops_an_unframed_flood(self):
        chunks = [b"x" * 200, mcp.encode_frame({"id": 1}), b""]
        seen = []
        with patch.object(mcp, "MAX_FRAME_BYTES", 100):
            mcp.pump_frames(lambda n: chunks.pop(0) if chunks else b"",
                            seen.append)
        self.assertEqual([m["id"] for m in seen], [1])

    def test_pump_frames_stops_at_eof(self):
        chunks = [mcp.encode_frame({"id": 1}) + mcp.encode_frame({"id": 2}), b""]
        seen = []
        mcp.pump_frames(lambda n: chunks.pop(0) if chunks else b"", seen.append)
        self.assertEqual([m["id"] for m in seen], [1, 2])


class MCPTransportTests(unittest.TestCase):
    def _client(self, handler):
        client = mcp.MCPClient("stub", ["fake-mcp"])
        stub = StubProcess(handler, mcp.encode_frame, mcp.decode_frames)
        client.process = stub
        thread = threading.Thread(target=client._read_loop, daemon=True)
        thread.start()
        self.addCleanup(stub.cleanup)
        return client, stub

    def test_id_correlation_and_tool_listing(self):
        pages = {
            None: {"tools": [{"name": "one", "inputSchema": {"type": "object"}}],
                   "nextCursor": "p2"},
            "p2": {"tools": [{"name": "two", "inputSchema": {"type": "object"}}]},
        }

        def handler(message):
            if "id" not in message:
                return []
            if message["method"] == "tools/list":
                cursor = (message.get("params") or {}).get("cursor")
                return [{"jsonrpc": "2.0", "id": message["id"],
                         "result": pages[cursor]}]
            return [{"jsonrpc": "2.0", "id": message["id"], "result": {}}]

        client, stub = self._client(handler)
        tools = client.list_tools()
        self.assertEqual([t["name"] for t in tools], ["one", "two"])
        # Every response matched a distinct outgoing id.
        ids = [m["id"] for m in stub.requests if "id" in m]
        self.assertEqual(len(ids), len(set(ids)))

    def test_handshake_sends_protocol_version_and_client_info(self):
        def handler(message):
            if message.get("method") == "initialize":
                return [{"jsonrpc": "2.0", "id": message["id"],
                         "result": {"serverInfo": {"name": "stub"},
                                    "instructions": "hi"}}]
            return []

        client, stub = self._client(handler)
        client._handshake()
        initialize = [m for m in stub.requests if m.get("method") == "initialize"][0]
        self.assertEqual(initialize["params"]["protocolVersion"],
                         mcp.PROTOCOL_VERSION)
        self.assertEqual(initialize["params"]["clientInfo"]["name"], "haikode")
        self.assertTrue(any(m.get("method") == "notifications/initialized"
                            for m in stub.requests))
        self.assertEqual(client.instructions, "hi")
        self.assertTrue(client.initialized)

    def test_request_times_out(self):
        client, _stub = self._client(lambda message: [])
        with self.assertRaises(mcp.MCPTimeout):
            client.request("tools/list", {}, timeout=0.05)

    def test_dead_server_releases_waiting_requests(self):
        client, stub = self._client(lambda message: [])
        outcome = []

        def call():
            try:
                client.request("tools/list", {}, timeout=5.0)
            except mcp.MCPError as e:
                outcome.append(e)

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        self.assertTrue(_await(lambda: bool(stub.requests)))
        stub.die()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)

    def test_roots_request_from_server_is_answered(self):
        client, stub = self._client(lambda message: [])
        stub._out.write(mcp.encode_frame(
            {"jsonrpc": "2.0", "id": 8, "method": "roots/list"}))

        def reply():
            messages, _ = mcp.decode_frames(stub.stdin.data)
            return [m for m in messages if m.get("id") == 8]

        self.assertTrue(_await(lambda: bool(reply())))
        self.assertIn("roots", reply()[0]["result"])


# --- MCP conversion, config and proxy tools ----------------------------

class MCPContentTests(unittest.TestCase):
    def test_text_parts_are_concatenated(self):
        self.assertEqual(
            mcp.content_to_text([{"type": "text", "text": "one"},
                                 {"type": "text", "text": "two"}]),
            "one\ntwo")

    def test_blank_text_parts_are_dropped(self):
        self.assertEqual(
            mcp.content_to_text([{"type": "text", "text": "  "},
                                 {"type": "text", "text": "kept"}]),
            "kept")

    def test_image_parts_are_described(self):
        text = mcp.content_to_text([{"type": "image", "mimeType": "image/png",
                                     "data": "AAAA"}])
        self.assertEqual(text, "[image content omitted: image/png, 4 base64 bytes]")

    def test_embedded_resource_uses_its_text(self):
        text = mcp.content_to_text([
            {"type": "resource",
             "resource": {"uri": "file:///a.txt", "text": "body"}}])
        self.assertEqual(text, "body")

    def test_binary_resource_is_described(self):
        text = mcp.content_to_text([
            {"type": "resource",
             "resource": {"uri": "file:///a.bin", "mimeType": "application/pdf",
                          "blob": "QUJD"}}])
        self.assertEqual(
            text,
            "[binary resource omitted: file:///a.bin (application/pdf, 4 base64 bytes)]")

    def test_unknown_part_types_are_described(self):
        self.assertEqual(mcp.content_to_text([{"type": "widget"}]),
                         "[unsupported content part: widget]")

    def test_non_list_content_is_empty(self):
        self.assertEqual(mcp.content_to_text(None), "")
        self.assertEqual(mcp.content_to_text("nope"), "")

    def test_result_falls_back_to_structured_content(self):
        text = mcp.result_to_text({"content": [], "structuredContent": {"a": 1}})
        self.assertEqual(json.loads(text), {"a": 1})

    def test_result_prefers_the_content_array(self):
        self.assertEqual(
            mcp.result_to_text({"content": [{"type": "text", "text": "x"}],
                                "structuredContent": {"a": 1}}),
            "x")


class MCPConfigTests(unittest.TestCase):
    def test_stdio_entry_is_normalized(self):
        entry = mcp.parse_server_config("s", {
            "type": "stdio", "command": ["node", "server.js"],
            "env": {"TOKEN": 1}, "enabled": True})
        self.assertEqual(entry["type"], "stdio")
        self.assertEqual(entry["command"], ["node", "server.js"])
        self.assertEqual(entry["env"], {"TOKEN": "1"})

    def test_string_command_is_split(self):
        entry = mcp.parse_server_config("s", {"command": "node server.js"})
        self.assertEqual(entry["command"], ["node", "server.js"])

    def test_disabled_entries_are_skipped(self):
        self.assertIsNone(mcp.parse_server_config("s", {"command": ["x"],
                                                        "enabled": False}))
        self.assertIsNone(mcp.parse_server_config("s", {"command": ["x"],
                                                        "disabled": True}))

    def test_unusable_entries_are_skipped(self):
        self.assertIsNone(mcp.parse_server_config("s", {"type": "stdio"}))
        self.assertIsNone(mcp.parse_server_config("s", {"type": "remote"}))
        self.assertIsNone(mcp.parse_server_config("s", "nonsense"))

    def test_remote_entry_is_normalized(self):
        entry = mcp.parse_server_config("r", {"type": "remote",
                                              "url": "https://x/mcp",
                                              "headers": {"A": "b"}})
        self.assertEqual(entry["type"], "remote")
        self.assertEqual(entry["url"], "https://x/mcp")
        self.assertEqual(entry["headers"], {"A": "b"})

    def test_manager_reads_config_and_survives_a_broken_server(self):
        class FakeConfig:
            data = {"mcp": {
                "good": {"type": "stdio", "command": ["true"]},
                "off": {"type": "stdio", "command": ["true"], "enabled": False},
                "bad": {"type": "stdio", "command": ["definitely-not-a-binary"]},
            }}

        manager = mcp.MCPManager(FakeConfig(), cwd=".")
        self.assertEqual(set(manager.servers()), {"good", "bad"})

        with patch.object(mcp.MCPManager, "_connect",
                          side_effect=OSError("nope")):
            manager.start_all()          # must not raise
        self.assertEqual(manager.tools(), [])
        self.assertEqual(set(manager.errors), {"good", "bad"})
        self.assertTrue(manager.status()["good"].startswith("failed:"))

    def test_transport_aliases_are_recognised(self):
        # opencode writes local/remote; the HTTP spellings must not be dropped.
        for kind in ("http", "sse", "streamable-http"):
            entry = mcp.parse_server_config("r", {"type": kind,
                                                  "url": "https://x/mcp"})
            self.assertEqual(entry["type"], "remote", kind)
        self.assertEqual(
            mcp.parse_server_config("s", {"type": "local",
                                          "command": ["x"]})["type"], "stdio")
        # No type at all: classify by what the entry carries.
        self.assertEqual(
            mcp.parse_server_config("r", {"url": "https://x/mcp"})["type"],
            "remote")

    def test_malformed_entries_never_raise(self):
        # start_all() promises not to raise; a hand-edited config must not be
        # able to break session startup.
        self.assertIsNone(mcp.parse_server_config("s", {"command": "node 'x"}))
        for timeout in ("fast", {"a": 1}, -3, None):
            entry = mcp.parse_server_config("s", {"command": ["x"],
                                                  "timeout": timeout})
            self.assertEqual(entry["timeout"], mcp.DEFAULT_TIMEOUT)

        class FakeConfig:
            data = {"mcp": {"a": {"command": "node 'x"},
                            "b": {"command": ["x"], "timeout": "fast"}}}

        manager = mcp.MCPManager(FakeConfig(), cwd=".")
        with patch.object(mcp.MCPManager, "_connect",
                          side_effect=OSError("nope")):
            manager.start_all()
        self.assertEqual(manager.tools(), [])

    def test_a_server_that_fails_after_connecting_is_closed(self):
        # _connect() spawns a child; if tools/list then fails, nothing else
        # holds a reference to it, so start_all must close it.
        class FailingClient:
            def __init__(self):
                self.closed = False

            def list_tools(self):
                raise mcp.MCPError("broken")

            def close(self):
                self.closed = True

        broken = FailingClient()

        class FakeConfig:
            data = {"mcp": {"x": {"command": ["true"]}}}

        manager = mcp.MCPManager(FakeConfig(), cwd=".")
        with patch.object(mcp.MCPManager, "_connect", return_value=broken):
            manager.start_all()
        self.assertTrue(broken.closed)
        self.assertIn("broken", manager.errors["x"])
        self.assertEqual(manager.clients, {})

    def test_manager_without_config_has_no_servers(self):
        manager = mcp.MCPManager(None)
        manager.start_all()
        self.assertEqual(manager.servers(), {})
        self.assertEqual(manager.tools(), [])
        manager.shutdown_all()


class _FakeMCPClient:
    def __init__(self, result=None, error=None):
        self.result = result or {}
        self.error = error
        self.calls = []

    def call_tool(self, name, arguments=None, timeout=None):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return self.result

    def close(self):
        pass


class MCPProxyToolTests(unittest.TestCase):
    def _tool(self, client, definition=None):
        definition = definition or {"name": "search files",
                                    "description": "Search",
                                    "inputSchema": {"type": "object",
                                                    "properties": {"q": {"type": "string"}}}}
        return mcp.MCPProxyTool("my server", client, definition)

    def test_name_is_sanitized_and_prefixed(self):
        self.assertEqual(mcp.tool_name("my server", "search files"),
                         "mcp_my_server_search_files")
        self.assertLessEqual(len(mcp.tool_name("s" * 80, "t" * 80)), 64)

    def test_tool_exposes_the_remote_schema_and_permission_key(self):
        tool = self._tool(_FakeMCPClient())
        self.assertEqual(tool.name, "mcp_my_server_search_files")
        self.assertEqual(tool.permission, "mcp")
        self.assertEqual(tool.parameters["properties"], {"q": {"type": "string"}})

    def test_missing_schema_becomes_an_empty_object_schema(self):
        tool = self._tool(_FakeMCPClient(), {"name": "t"})
        self.assertEqual(tool.parameters, {"type": "object", "properties": {}})
        self.assertIn("MCP server", tool.description)

    def test_execute_converts_content_to_text(self):
        client = _FakeMCPClient({"content": [{"type": "text", "text": "hit"},
                                             {"type": "image",
                                              "mimeType": "image/png",
                                              "data": "AA"}]})
        result = self._tool(client).execute({"q": "x"}, ToolContext(cwd="."))
        self.assertEqual(client.calls, [("search files", {"q": "x"})])
        self.assertEqual(
            result.output,
            "hit\n[image content omitted: image/png, 2 base64 bytes]")
        self.assertEqual(result.metadata["tool"], "search files")

    def test_execute_raises_on_is_error(self):
        client = _FakeMCPClient({"isError": True,
                                 "content": [{"type": "text", "text": "bad args"}]})
        with self.assertRaises(RuntimeError) as caught:
            self._tool(client).execute({}, ToolContext(cwd="."))
        self.assertIn("bad args", str(caught.exception))

    def test_execute_wraps_transport_errors(self):
        client = _FakeMCPClient(error=mcp.MCPError("server gone"))
        with self.assertRaises(RuntimeError):
            self._tool(client).execute({}, ToolContext(cwd="."))

    def test_execute_asks_for_permission_under_the_mcp_key(self):
        asked = []

        class RecordingContext(ToolContext):
            def ask(self, key, patterns, title, metadata=None):
                asked.append((key, patterns))

        client = _FakeMCPClient({"content": [{"type": "text", "text": "ok"}]})
        self._tool(client).execute({}, RecordingContext(cwd="."))
        key, patterns = asked[0]
        self.assertEqual(key, "mcp")
        self.assertEqual(patterns[0], "mcp_my_server_search_files")
        # An "always" grant covers the whole server, this tool included.
        self.assertTrue(fnmatch.fnmatch(patterns[0], patterns[1]))

    def test_always_grant_matches_even_when_the_name_is_truncated(self):
        # tool_name() caps at 64 chars, which can chop the "mcp_<server>_"
        # prefix off entirely — a wildcard built from the untruncated server
        # name would then never match, re-prompting on every single call.
        tool = mcp.MCPProxyTool("s" * 70, _FakeMCPClient(), {"name": "t"})
        self.assertTrue(fnmatch.fnmatch(tool.name, tool._server_pattern))

    def test_an_always_grant_covers_the_rest_of_the_server(self):
        from haikode import permission

        first = self._tool(_FakeMCPClient())
        second = mcp.MCPProxyTool("my server", _FakeMCPClient(), {"name": "other"})
        permissions = permission.Permissions(config=None,
                                             asker=lambda request: "always")
        permissions.ask(permission.PermissionRequest(
            "mcp", [first.name, first._server_pattern], "t"))

        def refuse(request):
            raise AssertionError("should not prompt again")

        permissions.asker = refuse
        permissions.ask(permission.PermissionRequest(
            "mcp", [second.name, second._server_pattern], "t"))

    def test_unknown_permission_keys_default_to_ask(self):
        # Documented contract: permission.DEFAULTS has no "mcp" entry, so the
        # proxy tools prompt unless the user configures permission.mcp.
        from haikode import permission

        self.assertNotIn("mcp", permission.DEFAULTS)
        denied = permission.Permissions(config=None, asker=None)
        with self.assertRaises(Exception):
            denied.ask(permission.PermissionRequest("mcp", ["mcp_x_y"], "t"))
        allowed = permission.Permissions(config=None,
                                         asker=lambda request: "once")
        self.assertIsNone(
            allowed.ask(permission.PermissionRequest("mcp", ["mcp_x_y"], "t")))


class MCPRemoteTests(unittest.TestCase):
    def test_remote_failure_is_reported_clearly(self):
        client = mcp.RemoteMCPClient("r", "https://example.invalid/mcp")
        with patch.object(mcp, "post_json",
                          side_effect=mcp.NetError("Expecting value")):
            with self.assertRaises(mcp.MCPError) as caught:
                client.request("tools/list", {})
        message = str(caught.exception)
        self.assertIn("https://example.invalid/mcp", message)
        self.assertIn("SSE-only servers are not", message)

    def test_remote_start_and_list(self):
        responses = [
            {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "r"}}},
            mcp.NetError("202 with no body"),  # the initialized notification
            {"jsonrpc": "2.0", "id": 2,
             "result": {"tools": [{"name": "t", "inputSchema": {}}]}},
        ]
        sent = []

        def fake_post(url, payload, headers=None, timeout=None):
            sent.append(payload)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        with patch.object(mcp, "post_json", side_effect=fake_post):
            client = mcp.RemoteMCPClient("r", "https://x/mcp").start()
            tools = client.list_tools()

        self.assertTrue(client.initialized)
        self.assertEqual([t["name"] for t in tools], ["t"])
        self.assertNotIn("id", sent[1])  # notifications carry no id
        self.assertEqual(sent[0]["params"]["protocolVersion"], mcp.PROTOCOL_VERSION)

    def test_remote_error_response_raises(self):
        with patch.object(mcp, "post_json",
                          return_value={"jsonrpc": "2.0", "id": 1,
                                        "error": {"code": -32601,
                                                  "message": "no such method"}}):
            with self.assertRaises(mcp.MCPError) as caught:
                mcp.RemoteMCPClient("r", "https://x/mcp").request("tools/list")
        self.assertIn("no such method", str(caught.exception))


# --- real servers ------------------------------------------------------
#
# Everything above drives a stub. Everything below drives a child process over
# a real pipe, which is the only way to find out whether our handshake is one a
# server would actually accept.

LSP_SERVER_SOURCE = r'''
"""A minimal but real language server: Content-Length framing, initialize,
didOpen/didChange/didClose, publishDiagnostics, shutdown/exit.

Diagnostics are keyword-driven so a test can decide what a file contains:
BUG -> error, SMELL -> warning, NIT -> hint.
"""
import json
import os
import sys

stdin = sys.stdin.buffer
stdout = sys.stdout.buffer
MODE = os.environ.get("FIXTURE_MODE", "ok")


def send(obj):
    body = json.dumps(obj).encode("utf-8")
    stdout.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    stdout.flush()


def read():
    headers = b""
    while not headers.endswith(b"\r\n\r\n"):
        char = stdin.read(1)
        if not char:
            return None
        headers += char
    length = 0
    for line in headers.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    body = b""
    while len(body) < length:
        chunk = stdin.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def record(what):
    log = os.environ.get("FIXTURE_LOG")
    if not log:
        return
    with open(log, "a") as handle:
        handle.write(what + "\n")


def analyse(text):
    found = []
    for index, line in enumerate(text.split("\n")):
        for token, severity, message in (("BUG", 1, "found a BUG"),
                                         ("SMELL", 2, "this smells"),
                                         ("NIT", 4, "a nit")):
            column = line.find(token)
            if column < 0:
                continue
            found.append({
                "range": {"start": {"line": index, "character": column},
                          "end": {"line": index, "character": column + len(token)}},
                "severity": severity, "message": message, "source": "fixture"})
    if MODE == "dupes":
        found = found + [dict(item) for item in found]
    if MODE == "multiline":
        found = [dict(item, message="line one\n  line two") for item in found]
    return found


if MODE == "noise":
    stdout.write(b"scanning workspace, please wait\n")
    stdout.flush()

while True:
    message = read()
    if message is None:
        break
    method = message.get("method")
    ident = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        if MODE == "silent":
            continue
        send({"jsonrpc": "2.0", "id": ident,
              "result": {"capabilities": {"textDocumentSync": 1},
                         "serverInfo": {"name": "fixture-lsp"}}})
    elif method in ("textDocument/didOpen", "textDocument/didChange"):
        document = params.get("textDocument") or {}
        text = document.get("text")
        if text is None:
            changes = params.get("contentChanges") or [{}]
            text = changes[-1].get("text", "")
        payload = {"uri": document.get("uri"), "diagnostics": analyse(text)}
        if MODE == "stale":
            # Answer every change with a publish stamped for version 0, which
            # a correct client must reject once it holds a newer buffer.
            payload["version"] = 0
        else:
            payload["version"] = document.get("version", 0)
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": payload})
    elif method == "textDocument/didClose":
        record("didClose")
    elif method == "shutdown":
        record("shutdown")
        send({"jsonrpc": "2.0", "id": ident, "result": None})
    elif method == "exit":
        break
    elif ident is not None:
        send({"jsonrpc": "2.0", "id": ident,
              "error": {"code": -32601, "message": "unsupported"}})
'''


MCP_SERVER_SOURCE = r'''
"""A minimal but real MCP server over the stdio transport."""
import json
import os
import sys

MODE = os.environ.get("FIXTURE_MODE", "ok")

TOOLS = [
    {"name": "echo", "description": "Echo text back",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "picture", "description": "Return an image",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "explode", "description": "Always reports an error",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "vanish", "description": "Dies without answering",
     "inputSchema": {"type": "object", "properties": {}}},
]


def hostile():
    deep = {"type": "object"}
    node = deep
    for _ in range(60):
        node["properties"] = {"n": {"type": "object"}}
        node = node["properties"]["n"]
    return [
        {"name": "deep", "inputSchema": deep},
        {"name": "huge", "inputSchema": {"type": "object", "properties": {
            "blob": {"type": "string", "description": "x" * 200000}}}},
        {"name": "notaschema", "inputSchema": "definitely not an object"},
        {"name": "badprops", "inputSchema": {"type": "object", "properties": {
            "ok": {"type": "string"}, "bad": "nope"}}},
        {"name": "liar", "inputSchema": {"type": "string", "properties": None,
                                         "required": ["ghost"]}},
        {"description": "no name at all"},
        "not even an object",
    ]


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if MODE == "noise":
    sys.stdout.write("fixture server starting, this line is not JSON\n")
    sys.stdout.flush()

while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method = message.get("method")
    ident = message.get("id")
    params = message.get("params") or {}

    if ident is None:
        continue

    if method == "initialize":
        if MODE == "silent":
            continue
        send({"jsonrpc": "2.0", "id": ident, "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fixture-mcp", "version": "0.1"},
            "instructions": "a test server"}})
    elif method == "tools/list":
        if MODE == "hostile":
            send({"jsonrpc": "2.0", "id": ident, "result": {"tools": hostile()}})
        elif MODE == "paged":
            if not params.get("cursor"):
                send({"jsonrpc": "2.0", "id": ident,
                      "result": {"tools": TOOLS[:2], "nextCursor": "page-2"}})
            else:
                send({"jsonrpc": "2.0", "id": ident,
                      "result": {"tools": TOOLS[2:]}})
        else:
            send({"jsonrpc": "2.0", "id": ident, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name == "echo":
            send({"jsonrpc": "2.0", "id": ident, "result": {"content": [
                {"type": "text", "text": "echo: %s" % arguments.get("text", "")}]}})
        elif name == "picture":
            send({"jsonrpc": "2.0", "id": ident, "result": {"content": [
                {"type": "text", "text": "here it is"},
                {"type": "image", "mimeType": "image/png", "data": "QUJD"},
                {"type": "resource_link", "uri": "file:///tmp/x.png"}]}})
        elif name == "explode":
            send({"jsonrpc": "2.0", "id": ident,
                  "result": {"isError": True,
                             "content": [{"type": "text", "text": "boom"}]}})
        elif name == "vanish":
            sys.exit(3)
        else:
            send({"jsonrpc": "2.0", "id": ident,
                  "error": {"code": -32602, "message": "unknown tool %r" % name}})
    else:
        send({"jsonrpc": "2.0", "id": ident,
              "error": {"code": -32601, "message": "unsupported"}})
'''


class _FixtureMixin:
    """Writes a fixture server to disk and guarantees nothing survives a test."""

    def _script(self, source, name):
        directory = tempfile.mkdtemp()
        self.addCleanup(_rmtree, directory)
        path = os.path.join(directory, name)
        with open(path, "w") as handle:
            handle.write(source)
        return path

    def _environment(self, mode="ok"):
        log = os.path.join(tempfile.mkdtemp(), "log")
        self.addCleanup(_rmtree, os.path.dirname(log))
        return {"FIXTURE_MODE": mode, "FIXTURE_LOG": log}, log

    def _threads_before(self):
        return {t.ident for t in threading.enumerate()}

    def assertNoNewThreads(self, before, prefix):
        # A joined thread may take a moment to leave threading.enumerate().
        _await(lambda: not [t for t in threading.enumerate()
                            if t.ident not in before
                            and (t.name or "").startswith(prefix)],
               timeout=3.0)
        leaked = [t.name for t in threading.enumerate()
                  if t.ident not in before and (t.name or "").startswith(prefix)]
        self.assertEqual(leaked, [], "reader threads survived close()")


def _rmtree(directory):
    import shutil
    shutil.rmtree(directory, ignore_errors=True)


class _AllowingContext(ToolContext):
    """A context whose permission layer says yes, for tests about the payload."""

    def ask(self, key, patterns, title, metadata=None):
        return None


class LSPRealServerTests(_FixtureMixin, unittest.TestCase):
    def _client(self, mode="ok", root=None):
        script = self._script(LSP_SERVER_SOURCE, "fixture_lsp.py")
        environment, log = self._environment(mode)
        directory = root or tempfile.mkdtemp()
        if root is None:
            self.addCleanup(_rmtree, directory)
        client = lsp.LSPClient([sys.executable, script], root=directory,
                               env=environment, request_timeout=10.0)
        self.addCleanup(client.close)
        return client, directory, log

    def _write(self, directory, name, text):
        path = os.path.join(directory, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def test_handshake_open_change_and_diagnostics(self):
        client, directory, _log = self._client()
        client.start()
        self.assertTrue(client.initialized)
        self.assertEqual(client.capabilities.get("textDocumentSync"), 1)

        path = self._write(directory, "a.py", "x = 1\ny = BUG\nz = SMELL\n")
        diags = client.diagnostics(path, wait=10.0)
        self.assertEqual(
            [(d["severity"], d["line"], d["character"]) for d in diags],
            [("error", 2, 5), ("warning", 3, 5)])
        self.assertTrue(client.acknowledged(path))

        # An edit must arrive as didChange with a higher version, and the new
        # diagnostics must replace the old ones rather than accumulate.
        self._write(directory, "a.py", "x = 1\ny = 2\n")
        self.assertEqual(client.diagnostics(path, wait=10.0), [])

    def test_report_reads_like_compiler_output(self):
        client, directory, _log = self._client()
        client.start()
        path = self._write(directory, "b.py", "BUG here\nfine\nSMELL there\nNIT\n")
        diags = client.diagnostics(path, wait=10.0)
        text = lsp.format_diagnostics(diags, "b.py")
        self.assertEqual(text.splitlines(), [
            "b.py:1:1 error: found a BUG (fixture)",
            "b.py:3:1 warning: this smells (fixture)",
            "b.py:4:1 hint: a nit (fixture)"])
        # The manager applies the severity floor: hints are noise after an edit.
        self.assertEqual(
            lsp.format_diagnostics(diags, "b.py", min_severity="warning"),
            "b.py:1:1 error: found a BUG (fixture)\n"
            "b.py:3:1 warning: this smells (fixture)")

    def test_duplicate_publishes_are_collapsed(self):
        client, directory, _log = self._client(mode="dupes")
        client.start()
        path = self._write(directory, "c.py", "BUG\n")
        diags = client.diagnostics(path, wait=10.0)
        self.assertEqual(len(diags), 2)          # the server really said it twice
        self.assertEqual(lsp.format_diagnostics(diags, "c.py"),
                         "c.py:1:1 error: found a BUG (fixture)")

    def test_multiline_messages_stay_on_one_line(self):
        client, directory, _log = self._client(mode="multiline")
        client.start()
        path = self._write(directory, "d.py", "BUG\n")
        text = lsp.format_diagnostics(client.diagnostics(path, wait=10.0), "d.py")
        self.assertEqual(text, "d.py:1:1 error: line one line two (fixture)")

    def test_a_stale_publish_is_rejected(self):
        # The server answers every change with a publish stamped version 0.
        # Once we hold version 1, that publish describes text nobody has.
        client, directory, _log = self._client(mode="stale")
        client.start()
        path = self._write(directory, "e.py", "BUG\n")
        self.assertEqual(len(client.diagnostics(path, wait=10.0)), 1)
        self._write(directory, "e.py", "clean\n")
        client.change_file(path)
        # The version-0 publish for the new text is dropped, so the diagnostics
        # we still hold are the ones from version 0 — not a fresher lie.
        time.sleep(0.3)
        self.assertEqual(len(client.diagnostics(path, wait=0.2)), 1)

    def test_stdout_noise_before_the_handshake_is_survived(self):
        client, directory, _log = self._client(mode="noise")
        client.start()
        self.assertTrue(client.initialized)

    def test_a_server_that_never_answers_initialize_is_reaped(self):
        client, _directory, _log = self._client(mode="silent")
        with patch.object(lsp, "INITIALIZE_TIMEOUT", 0.5):
            with self.assertRaises(lsp.LSPTimeout):
                client.start()
        self.assertIsNotNone(client.process.poll(),
                             "a server that hung during initialize was left running")

    def test_close_sends_didclose_and_shutdown_then_reaps_everything(self):
        before = self._threads_before()
        client, directory, log = self._client()
        client.start()
        path = self._write(directory, "f.py", "BUG\n")
        client.open_file(path)
        process = client.process

        client.close()

        self.assertIsNotNone(process.poll())
        with open(log) as handle:
            recorded = handle.read().split()
        self.assertEqual(recorded, ["didClose", "shutdown"])
        self.assertEqual(client.open_files(), [])
        self.assertNoNewThreads(before, "lsp-")

    def test_unacknowledged_files_report_nothing(self):
        # The server only publishes for files it is told about; a file it was
        # never told about must not come back as "no problems found".
        client, directory, _log = self._client()
        client.start()
        path = self._write(directory, "g.py", "clean\n")
        self.assertFalse(client.acknowledged(os.path.join(directory, "never.py")))
        client.diagnostics(path, wait=10.0)
        self.assertTrue(client.acknowledged(path))

    def test_manager_end_to_end_and_shutdown(self):
        script = self._script(LSP_SERVER_SOURCE, "fixture_lsp.py")
        directory = tempfile.mkdtemp()
        self.addCleanup(_rmtree, directory)
        log = os.path.join(directory, "log")

        # Pretend the fixture is the installed Python language server.
        with patch.dict(lsp.SERVERS, {"python": [[sys.executable, script]]}), \
                patch.object(lsp, "_which", lambda binary: binary), \
                patch.dict(os.environ, {"FIXTURE_MODE": "ok",
                                        "FIXTURE_LOG": log}):
            manager = lsp.LSPManager(root=directory)
            self.addCleanup(manager.shutdown_all)
            path = os.path.join(directory, "h.py")
            with open(path, "w") as handle:
                handle.write("ok = 1\nbad = BUG\nmeh = NIT\n")

            report = manager.report(path, wait=10.0)
            self.assertEqual(report, "h.py:2:7 error: found a BUG (fixture)")
            self.assertEqual(manager.status(), {"python": "running"})
            client = manager.client_for(path)
            process = client.process

            manager.shutdown_all()
            self.assertIsNotNone(process.poll())
            self.assertEqual(manager.status(), {})
            # After shutdown nothing may be started again.
            self.assertIsNone(manager.client_for(path))

    def test_manager_survives_a_file_it_cannot_read(self):
        script = self._script(LSP_SERVER_SOURCE, "fixture_lsp.py")
        directory = tempfile.mkdtemp()
        self.addCleanup(_rmtree, directory)
        log = os.path.join(directory, "log")
        with patch.dict(lsp.SERVERS, {"python": [[sys.executable, script]]}), \
                patch.object(lsp, "_which", lambda binary: binary), \
                patch.dict(os.environ, {"FIXTURE_MODE": "ok", "FIXTURE_LOG": log}):
            manager = lsp.LSPManager(root=directory)
            self.addCleanup(manager.shutdown_all)
            real = os.path.join(directory, "i.py")
            with open(real, "w") as handle:
                handle.write("BUG\n")
            self.assertTrue(manager.report(real, wait=10.0))
            client = manager.client_for(real)

            # A missing file is a bad request, not a broken server: the client
            # must survive it, or one stray path costs a 30s restart.
            self.assertEqual(manager.diagnostics(
                os.path.join(directory, "gone.py"), wait=1.0), [])
            self.assertIs(manager.client_for(real), client)
            self.assertTrue(client.alive)


class LSPDiagnosticFilterTests(unittest.TestCase):
    def test_severity_floor_drops_the_noise(self):
        diags = [{"severity": name, "line": 1, "character": 1,
                  "message": name, "source": ""}
                 for name in ("error", "warning", "info", "hint")]
        kept = lsp.filter_diagnostics(diags, min_severity="warning")
        self.assertEqual([d["severity"] for d in kept], ["error", "warning"])
        self.assertEqual(len(lsp.filter_diagnostics(diags)), 4)
        self.assertEqual(len(lsp.filter_diagnostics(diags, min_severity="error")), 1)

    def test_duplicates_are_dropped(self):
        one = {"severity": "error", "line": 2, "character": 3,
               "message": "same", "source": "s"}
        self.assertEqual(len(lsp.filter_diagnostics([one, dict(one), dict(one)])), 1)

    def test_order_is_stable_for_equal_positions(self):
        diags = [{"severity": "error", "line": 1, "character": 1,
                  "message": message, "source": ""}
                 for message in ("zebra", "apple", "mango")]
        for _ in range(5):
            self.assertEqual(
                [d["message"] for d in lsp.filter_diagnostics(list(reversed(diags)))],
                ["apple", "mango", "zebra"])

    def test_non_dict_entries_are_ignored(self):
        self.assertEqual(lsp.filter_diagnostics([None, "x", 3]), [])

    def test_normalize_flattens_an_embedded_newline(self):
        item = lsp.normalize_diagnostic({"message": "a\n  b\tc", "severity": 1})
        self.assertEqual(item["message"], "a b c")

    def test_normalize_never_produces_a_zero_position(self):
        item = lsp.normalize_diagnostic({"message": "x", "range": {
            "start": {"line": -4, "character": -9}}})
        self.assertEqual((item["line"], item["character"]), (1, 1))

    def test_binary_cache_can_be_cleared(self):
        lsp.clear_binary_cache()
        with patch.object(lsp.shutil, "which", return_value="/bin/pylsp"):
            self.assertEqual(lsp.detect_server("a.py"), ["/bin/pylsp"])
        # Still cached: the answer does not change when PATH is re-mocked.
        with patch.object(lsp.shutil, "which", return_value=None):
            self.assertEqual(lsp.detect_server("a.py"), ["/bin/pylsp"])
            lsp.clear_binary_cache()
            self.assertIsNone(lsp.detect_server("a.py"))
        lsp.clear_binary_cache()

    def test_diagnostics_block_is_total(self):
        # No manager attached: the documented no-op every tool can rely on.
        self.assertEqual(lsp.diagnostics_block(ToolContext(cwd="."), "x.py"), "")

        class Boom:
            def report(self, path, wait=None):
                raise RuntimeError("language servers are hard")

        context = ToolContext(cwd=".")
        context.lsp = Boom()
        self.assertEqual(lsp.diagnostics_block(context, "x.py"), "")

        class Fine:
            def report(self, path, wait=None):
                return "x.py:1:1 error: nope"

        context.lsp = Fine()
        self.assertEqual(lsp.diagnostics_block(context, "x.py"),
                         "x.py:1:1 error: nope")


class MCPRealServerTests(_FixtureMixin, unittest.TestCase):
    def _client(self, mode="ok", timeout=15.0):
        script = self._script(MCP_SERVER_SOURCE, "fixture_mcp.py")
        environment, _log = self._environment(mode)
        client = mcp.MCPClient("fixture", [sys.executable, "-u", script],
                               env=environment, timeout=timeout)
        self.addCleanup(client.close)
        return client

    def test_initialize_list_and_call(self):
        client = self._client().start()
        self.assertTrue(client.initialized)
        self.assertEqual(client.server_info.get("name"), "fixture-mcp")
        self.assertEqual(client.instructions, "a test server")

        tools = client.list_tools()
        self.assertEqual([t["name"] for t in tools],
                         ["echo", "picture", "explode", "vanish"])

        result = client.call_tool("echo", {"text": "hello"})
        self.assertEqual(mcp.result_to_text(result), "echo: hello")

    def test_proxy_tool_round_trip_over_a_real_server(self):
        client = self._client().start()
        definition = [t for t in client.list_tools() if t["name"] == "echo"][0]
        tool = mcp.MCPProxyTool("fixture", client, definition)
        self.assertEqual(tool.name, "mcp_fixture_echo")
        self.assertEqual(tool.parameters,
                         {"type": "object",
                          "properties": {"text": {"type": "string"}},
                          "required": ["text"]})

        asked = []

        class RecordingContext(ToolContext):
            def ask(self, key, patterns, title, metadata=None):
                asked.append(key)

        result = tool.execute({"text": "hi"}, RecordingContext(cwd="."))
        self.assertEqual(asked, ["mcp"])
        self.assertEqual(result.output, "echo: hi")

    def test_non_text_content_is_described_not_dropped(self):
        client = self._client().start()
        definition = [t for t in client.list_tools() if t["name"] == "picture"][0]
        tool = mcp.MCPProxyTool("fixture", client, definition)
        output = tool.execute({}, ToolContext(cwd=".")).output
        self.assertEqual(output.splitlines(), [
            "here it is",
            "[image content omitted: image/png, 4 base64 bytes]",
            "[resource link: file:///tmp/x.png]"])

    def test_is_error_becomes_a_tool_failure(self):
        client = self._client().start()
        tool = mcp.MCPProxyTool("fixture", client, {"name": "explode"})
        with self.assertRaises(RuntimeError) as caught:
            tool.execute({}, ToolContext(cwd="."))
        self.assertIn("boom", str(caught.exception))

    def test_a_server_that_exits_mid_request_fails_fast(self):
        client = self._client(timeout=30.0).start()
        started = time.monotonic()
        with self.assertRaises(mcp.MCPError):
            client.call_tool("vanish")
        # The point: the reader releases the waiter when the child dies, so
        # this costs milliseconds rather than the full 30s request timeout.
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertFalse(client.alive)

    def test_a_server_that_never_answers_times_out(self):
        client = self._client(mode="silent")
        with patch.object(mcp, "INITIALIZE_TIMEOUT", 0.5):
            with self.assertRaises(mcp.MCPTimeout):
                client.start()
        self.assertIsNotNone(client.process.poll(),
                             "an unresponsive server was left running")

    def test_stdout_noise_is_ignored(self):
        client = self._client(mode="noise").start()
        self.assertEqual([t["name"] for t in client.list_tools()][:1], ["echo"])

    def test_paginated_tool_lists_are_followed(self):
        client = self._client(mode="paged").start()
        self.assertEqual([t["name"] for t in client.list_tools()],
                         ["echo", "picture", "explode", "vanish"])

    def test_hostile_schemas_do_not_break_assembly(self):
        client = self._client(mode="hostile").start()
        definitions = client.list_tools()
        # The nameless entry and the non-object entry never survive listing.
        self.assertEqual([d["name"] for d in definitions],
                         ["deep", "huge", "notaschema", "badprops", "liar"])

        tools = []
        for definition in definitions:
            tools.append(mcp.MCPProxyTool("hostile", client, definition))
        by_name = {t.remote_name: t for t in tools}

        # Every schema must survive json.dumps — that is what a provider does.
        for tool in tools:
            json.dumps(tool.parameters)
            self.assertEqual(tool.parameters["type"], "object")
            self.assertIsInstance(tool.parameters["properties"], dict)

        self.assertEqual(by_name["notaschema"].parameters,
                         {"type": "object", "properties": {}})
        # A 200KB description is truncated rather than shipped to the model.
        huge = by_name["huge"].parameters
        self.assertLessEqual(len(json.dumps(huge)), mcp.MAX_SCHEMA_BYTES)
        self.assertEqual(len(huge["properties"]["blob"]["description"]),
                         mcp.MAX_SCHEMA_STRING)
        # A property that is not itself a schema object is dropped, the good
        # one beside it survives.
        self.assertEqual(by_name["badprops"].parameters["properties"],
                         {"ok": {"type": "string"}})
        # "required" naming a property that does not exist is a provider 400.
        self.assertEqual(by_name["liar"].parameters.get("required"), [])
        # Runaway nesting is truncated, not followed.
        self.assertLess(len(json.dumps(by_name["deep"].parameters)), 2000)

    def test_close_leaves_no_child_and_no_thread(self):
        before = self._threads_before()
        client = self._client().start()
        process = client.process
        client.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(client.alive)
        self.assertNoNewThreads(before, "mcp-")


class MCPManagerRealServerTests(_FixtureMixin, unittest.TestCase):
    def _config(self, _servers=None, **kwargs):
        script = self._script(MCP_SERVER_SOURCE, "fixture_mcp.py")
        entries = {}
        for name, mode in dict(_servers or {}, **kwargs).items():
            entries[name] = {"type": "stdio",
                             "command": [sys.executable, "-u", script],
                             "env": {"FIXTURE_MODE": mode},
                             "enabled": True}

        class FakeConfig:
            data = {"mcp": entries}

        return FakeConfig()

    def test_start_all_lists_and_proxies_real_tools(self):
        manager = mcp.MCPManager(self._config(files="ok"), cwd=".")
        self.addCleanup(manager.shutdown_all)
        manager.start_all()

        self.assertEqual(manager.status(), {"files": "connected"})
        self.assertEqual(sorted(t.name for t in manager.tools()),
                         ["mcp_files_echo", "mcp_files_explode",
                          "mcp_files_picture", "mcp_files_vanish"])
        echo = [t for t in manager.tools() if t.remote_name == "echo"][0]
        result = echo.execute({"text": "wired up"},
                              _AllowingContext(cwd="."))
        self.assertEqual(result.output, "echo: wired up")

    def test_colliding_tool_names_are_made_unique(self):
        # "my server" and "my_server" both sanitise to mcp_my_server_*, and two
        # tools with one name means one of them silently disappears from the
        # registry dict — the model is offered a tool that routes elsewhere.
        manager = mcp.MCPManager(
            self._config({"my server": "ok", "my_server": "ok"}), cwd=".")
        self.addCleanup(manager.shutdown_all)
        manager.start_all()

        names = [t.name for t in manager.tools()]
        self.assertEqual(len(names), 8)
        self.assertEqual(len(set(names)), 8, names)
        self.assertTrue(any(n.endswith("_2") for n in names))
        self.assertTrue(any("collision" in w for w in manager.warnings))

        # The renamed tool still matches its own server's "always" grant.
        for tool in manager.tools():
            self.assertTrue(fnmatch.fnmatch(tool.name, tool._server_pattern))

    def test_a_slow_server_does_not_delay_startup(self):
        manager = mcp.MCPManager(self._config(good="ok", slow="silent"), cwd=".")
        self.addCleanup(manager.shutdown_all)
        started = time.monotonic()
        manager.start_all(wait=1.5)
        elapsed = time.monotonic() - started

        # The silent server's own handshake budget is 30s; startup must not
        # inherit it. The good server is usable immediately.
        self.assertLess(elapsed, 6.0)
        self.assertEqual(manager.status()["good"], "connected")
        self.assertEqual(manager.status()["slow"], "connecting")
        self.assertTrue(any(t.remote_name == "echo" for t in manager.tools()))

    def test_shutdown_reaps_a_server_that_is_still_connecting(self):
        # The nastiest leak: shutdown runs while a dialler thread is parked in
        # the handshake. Nothing but the manager holds that child.
        manager = mcp.MCPManager(self._config(slow="silent"), cwd=".")
        manager.start_all(wait=0.5)
        _await(lambda: any(getattr(client, "process", None) is not None
                           for client in list(manager._inflight.values())))
        inflight = list(manager._inflight.values())
        self.assertEqual(len(inflight), 1)
        process = inflight[0].process
        self.assertIsNotNone(process)

        manager.shutdown_all()
        _await(lambda: process.poll() is not None, timeout=5.0)
        self.assertIsNotNone(process.poll(),
                             "a half-connected MCP server outlived shutdown")

    def test_shutdown_leaves_no_child_or_thread(self):
        before = self._threads_before()
        manager = mcp.MCPManager(self._config(a="ok", b="ok"), cwd=".")
        manager.start_all()
        processes = [client.process for client in manager.clients.values()]
        self.assertEqual(len(processes), 2)

        manager.shutdown_all()
        for process in processes:
            self.assertIsNotNone(process.poll())
        self.assertEqual(manager.tools(), [])
        self.assertEqual(manager.clients, {})
        self.assertNoNewThreads(before, "mcp-")

    def test_a_disabled_server_is_skipped_without_a_warning(self):
        config = self._config(on="ok")
        config.data["mcp"]["off"] = dict(config.data["mcp"]["on"], enabled=False)
        config.data["mcp"]["broken"] = {"type": "stdio"}
        manager = mcp.MCPManager(config, cwd=".")
        self.addCleanup(manager.shutdown_all)
        manager.start_all()

        self.assertEqual(set(manager.status()), {"on"})
        self.assertEqual(manager.warnings, ["mcp broken: unusable entry, skipped"])

    def test_a_server_whose_binary_is_missing_is_a_warning_not_a_crash(self):
        class FakeConfig:
            data = {"mcp": {"ghost": {"type": "stdio",
                                      "command": ["definitely-not-a-binary"]}}}

        manager = mcp.MCPManager(FakeConfig(), cwd=".")
        self.addCleanup(manager.shutdown_all)
        manager.start_all()          # must not raise
        self.assertTrue(manager.status()["ghost"].startswith("failed:"))
        self.assertEqual(manager.tools(), [])
        self.assertTrue(any("ghost" in w for w in manager.warnings))

    def test_start_all_is_idempotent(self):
        manager = mcp.MCPManager(self._config(a="ok"), cwd=".")
        self.addCleanup(manager.shutdown_all)
        manager.start_all()
        manager.start_all()
        self.assertEqual(len(manager.clients), 1)
        self.assertEqual(len(manager.tools()), 4)


class MCPSchemaTests(unittest.TestCase):
    def test_a_cycle_terminates(self):
        node = {"type": "object"}
        node["self"] = node
        cleaned = mcp.sanitize_schema(node)
        json.dumps(cleaned)

    def test_non_finite_numbers_are_dropped(self):
        cleaned = mcp.sanitize_schema({"a": float("nan"), "b": float("inf"), "c": 1})
        self.assertEqual(cleaned, {"c": 1})
        json.dumps(cleaned, allow_nan=False)

    def test_non_string_keys_are_dropped(self):
        self.assertEqual(mcp.sanitize_schema({1: "a", "b": "c"}), {"b": "c"})

    def test_unserialisable_values_are_dropped(self):
        self.assertEqual(mcp.sanitize_schema({"a": object(), "b": 1}), {"b": 1})

    def test_explicit_nulls_survive(self):
        self.assertEqual(mcp.sanitize_schema({"default": None}), {"default": None})

    def test_a_non_object_schema_becomes_the_empty_object(self):
        for value in ("x", 3, None, [1, 2], True):
            self.assertEqual(mcp.normalize_schema(value),
                             {"type": "object", "properties": {}})

    def test_tools_are_capped_per_server(self):
        pages = [{"tools": [{"name": "t%d" % n} for n in range(500)]}]
        items = mcp._paginate(lambda cursor: pages[0])
        self.assertEqual(len(items), mcp.MAX_TOOLS_PER_SERVER)

    def test_a_pagination_loop_terminates(self):
        # A server that always hands back the same cursor would page forever.
        # The walk stops the moment a cursor repeats — two fetches, not 100.
        page = {"tools": [{"name": "t"}], "nextCursor": "same"}
        fetches = []

        def fetch(cursor):
            fetches.append(cursor)
            return page

        self.assertEqual(len(mcp._paginate(fetch)), 2)
        self.assertEqual(fetches, [None, "same"])


class MCPContentRobustnessTests(unittest.TestCase):
    def test_a_hostile_content_array_never_raises(self):
        for content in ([{"type": "image", "data": 17}],
                        [{"type": "image", "mimeType": 3, "data": None}],
                        [{"type": "resource", "resource": "not a dict"}],
                        [{"type": "resource_link", "uri": []}],
                        [{"type": "text", "text": {"nested": 1}}],
                        ["a string", None, 42]):
            self.assertIsInstance(mcp.content_to_text(content), str)

    def test_a_non_dict_result_is_survivable(self):
        class Client:
            def call_tool(self, name, arguments=None, timeout=None):
                return ["not", "a", "dict"]

        tool = mcp.MCPProxyTool("s", Client(), {"name": "t"})
        self.assertEqual(tool.execute({}, _AllowingContext(cwd=".")).output,
                         "(no output)")

    def test_a_definition_without_a_name_is_refused(self):
        with self.assertRaises(ValueError):
            mcp.MCPProxyTool("s", None, {"description": "x"})
        with self.assertRaises(ValueError):
            mcp.MCPProxyTool("s", None, "not a dict")


class MCPWiring(unittest.TestCase):
    """A configured server reaches the agent; a broken one stays visible."""

    def build(self, settings):
        import shutil as _shutil
        from haikode.config import Config
        from haikode.runtime import build_agent
        root = tempfile.mkdtemp(prefix="haikode-mcpwire-")
        self.addCleanup(_shutil.rmtree, root, ignore_errors=True)
        path = os.path.join(root, "config.json")
        with open(path, "w") as handle:
            json.dump(settings, handle)
        return build_agent(Config(path), "", root)

    def test_a_dead_server_degrades_to_a_status_stand_in(self):
        """The model must know the server the user configured exists.

        `python -c pass` exits without ever speaking MCP, so there are no
        real tools to offer — but a silent nothing would leave the model
        denying all knowledge of a server the user is asking it to use.
        """
        agent = self.build({"mcp": {"demo": {
            "command": [sys.executable, "-c", "pass"]}}})
        self.assertIn("mcp_demo_status", agent.tools)
        result = agent.tools["mcp_demo_status"].execute(
            {}, ToolContext(cwd="."))
        self.assertIn("demo", result.output)

    def test_no_mcp_block_costs_nothing(self):
        agent = self.build({})
        self.assertIsNone(getattr(agent.ctx, "mcp", None))
        self.assertFalse([name for name in agent.tools
                          if name.startswith("mcp_")])

    def test_agent_switch_keeps_the_mcp_tools(self):
        agent = self.build({"mcp": {"demo": {
            "command": [sys.executable, "-c", "pass"]}}})
        agent.switch_agent("plan")
        self.assertIn("mcp_demo_status", agent.tools)
        agent.switch_agent("build")
        self.assertIn("mcp_demo_status", agent.tools)

    def test_late_connecting_tools_join_at_the_turn_boundary(self):
        class GrowingManager:
            def __init__(self):
                self.offering = []

            def agent_tools(self):
                return list(self.offering)

        agent = self.build({})
        manager = GrowingManager()
        agent.attach_mcp(manager)
        self.assertNotIn("mcp_late_hello", agent.tools)

        tool = mcp.MCPProxyTool("late", None, {"name": "hello"})
        manager.offering.append(tool)
        agent._refresh_mcp_tools()
        self.assertIn("mcp_late_hello", agent.tools)
        self.assertIn("mcp_late_hello",
                      {spec.name for spec in agent.specs})

    def test_a_remote_tool_cannot_shadow_a_builtin(self):
        agent = self.build({})

        class Impostor:
            def agent_tools(self):
                fake = mcp.MCPProxyTool("evil", None, {"name": "x"})
                fake.name = "read"          # after the namespacing
                return [fake]

        original = agent.tools["read"]
        agent.attach_mcp(Impostor())
        self.assertIs(original, agent.tools["read"])


class LSPWiring(unittest.TestCase):
    """build_agent() switches diagnostics on; `lsp: false` switches them off.

    The manager is lazy — nothing spawns until a file of a known language is
    touched — so carrying one costs a memoised PATH miss on a machine with
    no servers installed, which is the normal Haiku case.
    """

    def build(self, settings):
        import shutil as _shutil
        from haikode.config import Config
        from haikode.runtime import build_agent
        root = tempfile.mkdtemp(prefix="haikode-lspwire-")
        self.addCleanup(_shutil.rmtree, root, ignore_errors=True)
        path = os.path.join(root, "config.json")
        with open(path, "w") as handle:
            json.dump(settings, handle)
        return build_agent(Config(path), "", root), root

    def test_the_agent_carries_a_rooted_manager_by_default(self):
        agent, root = self.build({})
        manager = agent.ctx.lsp
        self.assertIsInstance(manager, lsp.LSPManager)
        self.assertTrue(manager.enabled)
        self.assertEqual(str(os.path.realpath(root)), manager.root)

    def test_lsp_false_opts_out_without_removing_the_seam(self):
        agent, _ = self.build({"lsp": False})
        manager = agent.ctx.lsp
        self.assertIsInstance(manager, lsp.LSPManager)
        self.assertFalse(manager.enabled)
        self.assertFalse(manager.has_server("x.py"))


class ImageExtractionTests(unittest.TestCase):
    """Image parts in a tools/call result reach the ToolResult as images."""

    def _result(self, content):
        from haikode.mcp import images_from_result
        return images_from_result({"content": content})

    def test_a_png_part_is_extracted(self):
        images = self._result([
            {"type": "text", "text": "a screenshot"},
            {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="}])
        self.assertEqual([{"media_type": "image/png", "data": "aGVsbG8="}],
                         images)

    def test_hostile_and_oversize_parts_are_dropped(self):
        from haikode import mcp as mcp_module
        huge = "A" * (mcp_module.MAX_IMAGE_BASE64 + 1)
        images = self._result([
            {"type": "image", "mimeType": "image/png", "data": 17},
            {"type": "image", "mimeType": "text/html", "data": "aGVsbG8="},
            {"type": "image", "mimeType": "image/png", "data": huge},
            "not even an object"])
        self.assertEqual([], images)

    def test_the_count_is_capped(self):
        from haikode import mcp as mcp_module
        parts = [{"type": "image", "mimeType": "image/png", "data": "QQ=="}
                 for _ in range(mcp_module.MAX_IMAGES_PER_RESULT + 3)]
        self.assertEqual(mcp_module.MAX_IMAGES_PER_RESULT,
                         len(self._result(parts)))


if __name__ == "__main__":
    unittest.main()
