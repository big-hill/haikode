#!/usr/bin/env python3
"""Deterministic performance/architecture probes for haikode.

No external network and no credentials. Output contains counts, durations and
synthetic request ids only -- never prompts, model output, tokens or headers.
Run from the repository root:

    python3 benchmarks/performance_audit.py --pretty
"""

import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from haikode.agent import Agent  # noqa: E402
from haikode.context import (clear_summary_cache, compact_messages,
                             message_tokens)  # noqa: E402
from haikode.mcp import DEFAULT_STARTUP_WAIT, MCPManager  # noqa: E402
from haikode.net import NetError, RetryPolicy, stream_sse_events  # noqa: E402
from haikode.permission import Permissions  # noqa: E402
from haikode.providers.base import Provider  # noqa: E402
from haikode.providers.subscription import ChatGPTSubscriptionProvider  # noqa: E402
from haikode.schema import CompletionChunk, Msg  # noqa: E402
from haikode.session import BACKUP_MIN_INTERVAL, SessionStore  # noqa: E402
from haikode.usage import tool_specs_tokens  # noqa: E402


def _ms(seconds):
    return round(float(seconds) * 1000.0, 3)


def _median(values):
    return round(statistics.median(values), 3) if values else 0.0


class RequestLog:
    def __init__(self):
        self.serial = 0
        self.rows = []

    def begin(self, kind):
        self.serial += 1
        return "req-%04d" % self.serial, kind, time.monotonic()

    def end(self, started, first=None):
        request_id, kind, began = started
        ended = time.monotonic()
        self.rows.append({
            "request_id": request_id,
            "kind": kind,
            "time_to_first_event_ms": _ms((first or ended) - began),
            "duration_ms": _ms(ended - began),
        })


class ScriptedProvider(Provider):
    name = "audit-fake"

    def __init__(self, turns, requests):
        self.turns = list(turns)
        self.requests = requests

    def stream(self, messages, tools, model, max_tokens):
        started = self.requests.begin("main")
        first = None
        try:
            chunks = self.turns.pop(0)
            for chunk in chunks:
                if first is None:
                    first = time.monotonic()
                yield chunk
        finally:
            self.requests.end(started, first)


class SummaryProvider(Provider):
    name = "audit-summary"

    def __init__(self, requests):
        self.requests = requests
        self.summary_calls = 0

    def stream(self, messages, tools, model, max_tokens):
        self.summary_calls += 1
        started = self.requests.begin("summary")
        first = time.monotonic()
        try:
            yield CompletionChunk(text="## Objective\n- preserve state")
            yield CompletionChunk(stop_reason="stop",
                                  usage={"input": 101, "output": 7})
        finally:
            self.requests.end(started, first)


def _tool_turn(index, path):
    return [
        CompletionChunk(tool_call_delta={
            "index": 0, "id": "call-%d" % index, "name": "read",
            "arguments": json.dumps({"filePath": str(path)}),
        }),
        CompletionChunk(stop_reason="tool_calls"),
    ]


def agent_scenarios(root):
    target = root / "fixture.txt"
    target.write_text("fixture\n")

    requests = RequestLog()
    simple = Agent(
        ScriptedProvider([[CompletionChunk(text="done", stop_reason="stop")]],
                         requests),
        "m", cwd=str(root), tool_names=[],
        permissions=Permissions(auto_approve=True))
    started = time.monotonic()
    simple.run("synthetic")
    simple_ms = _ms(time.monotonic() - started)

    tool_requests = RequestLog()
    turns = [_tool_turn(index, target) for index in range(3)]
    turns.append([CompletionChunk(text="done", stop_reason="stop")])
    tools = Agent(ScriptedProvider(turns, tool_requests), "m", cwd=str(root),
                  tool_names=["read"],
                  permissions=Permissions(auto_approve=True))
    tool_started = []
    tool_durations = []

    def event(kind, payload):
        if kind == "tool":
            tool_started.append(time.monotonic())
        elif kind in ("tool_result", "tool_error", "tool_denied") and tool_started:
            tool_durations.append(_ms(time.monotonic() - tool_started.pop(0)))

    started = time.monotonic()
    tools.run("synthetic", on_event=event)
    tool_total_ms = _ms(time.monotonic() - started)

    summary_requests = RequestLog()
    summary_provider = SummaryProvider(summary_requests)
    compacted = Agent(summary_provider, "m", cwd=str(root),
                      context_window=20_000,
                      permissions=Permissions(auto_approve=True))
    compacted.messages = [
        Msg(role="user" if index % 2 == 0 else "assistant",
            content=("x" * 1200) + str(index))
        for index in range(80)
    ]
    planning = []
    started = time.monotonic()
    compacted._messages_for_llm()
    planning.append(_ms(time.monotonic() - started))
    for index in range(12):
        compacted.messages.extend([
            Msg(role="user", content="tail-%d" % index),
            Msg(role="assistant", content="answer-%d" % index),
        ])
        started = time.monotonic()
        compacted._messages_for_llm()
        planning.append(_ms(time.monotonic() - started))

    # Control: the removed architecture compacted the lossless raw list on
    # every request but never adopted the resulting summary. Recreate that
    # exact stateless decision with today's pure helper so the before/after
    # provider-call count remains reproducible from one checkout.
    stateless_requests = RequestLog()
    stateless_provider = SummaryProvider(stateless_requests)
    stateless = [
        Msg(role="user" if index % 2 == 0 else "assistant",
            content=("x" * 1200) + str(index))
        for index in range(80)
    ]
    stateless_namespace = object()
    compact_messages(stateless, 20_000, provider=stateless_provider,
                     model="m", cache_namespace=stateless_namespace)
    for index in range(12):
        stateless.extend([
            Msg(role="user", content="tail-%d" % index),
            Msg(role="assistant", content="answer-%d" % index),
        ])
        compact_messages(stateless, 20_000, provider=stateless_provider,
                         model="m", cache_namespace=stateless_namespace)

    effective = compacted._context_messages()
    started = time.monotonic()
    estimated_messages = sum(message_tokens(message) for message in effective)
    token_estimate_ms = _ms(time.monotonic() - started)
    started = time.monotonic()
    estimated_tools = tool_specs_tokens(compacted.specs)
    schema_estimate_ms = _ms(time.monotonic() - started)

    checkpoint_store = SessionStore(root / "checkpoint-sessions.db")
    try:
        checkpoint_session = checkpoint_store.new_session(str(root), "fake", "m")
        for message in compacted.messages:
            checkpoint_session.append(message)
        checkpoint_history, checkpoint_count = compacted.context_checkpoint()
        started = time.monotonic()
        checkpoint_session.save_context_checkpoint(checkpoint_history,
                                                   checkpoint_count)
        checkpoint_write_ms = _ms(time.monotonic() - started)

        # A desktop Send starts a new process, so clear the process-local LRU
        # before restoring exactly as the next worker must.
        clear_summary_cache()
        resumed = checkpoint_store.load(checkpoint_session.id)
        fresh_requests = RequestLog()
        fresh_provider = SummaryProvider(fresh_requests)
        fresh = Agent(fresh_provider, "m", cwd=str(root),
                      context_window=20_000,
                      permissions=Permissions(auto_approve=True))
        fresh.messages = list(resumed.messages)
        started = time.monotonic()
        checkpoint = resumed.load_context_checkpoint()
        restored = bool(checkpoint and fresh.restore_context_checkpoint(*checkpoint))
        checkpoint_restore_ms = _ms(time.monotonic() - started)
        started = time.monotonic()
        fresh._messages_for_llm()
        fresh_planning_ms = _ms(time.monotonic() - started)
    finally:
        checkpoint_store.close()
        clear_summary_cache()

    return {
        "simple_turn": {
            "provider_calls": len(requests.rows),
            "total_ms": simple_ms,
            "requests": requests.rows,
        },
        "three_tool_rounds": {
            "provider_calls": len(tool_requests.rows),
            "tool_calls": len(tool_durations),
            "tool_execution_ms": tool_durations,
            "total_ms": tool_total_ms,
            "requests": tool_requests.rows,
        },
        "compaction_latch": {
            "provider_rounds_after_first_fold": 12,
            "summary_calls": summary_provider.summary_calls,
            "raw_messages": len(compacted.messages),
            "effective_messages": len(effective),
            "first_planning_ms": planning[0],
            "later_planning_median_ms": _median(planning[1:]),
            "hidden_input_tokens": compacted.usage.hidden_session.input_tokens,
            "hidden_output_tokens": compacted.usage.hidden_session.output_tokens,
            "requests": summary_requests.rows,
        },
        "stateless_compaction_reference": {
            "provider_rounds_after_first_fold": 12,
            "summary_calls": stateless_provider.summary_calls,
            "requests": stateless_requests.rows,
            "note": "control path: raw transcript re-planned without adopting summary",
        },
        "fresh_worker_checkpoint": {
            "restored": restored,
            "summary_calls": fresh_provider.summary_calls,
            "checkpoint_write_ms": checkpoint_write_ms,
            "checkpoint_restore_ms": checkpoint_restore_ms,
            "planning_ms": fresh_planning_ms,
            "raw_messages": len(fresh.messages),
            "requests": fresh_requests.rows,
        },
        "estimation": {
            "message_tokens": estimated_messages,
            "message_estimate_ms": token_estimate_ms,
            "tool_schema_tokens": estimated_tools,
            "tool_schema_estimate_ms": schema_estimate_ms,
        },
    }


def _status(code, body="{}", headers=None, delay=0.0):
    def serve(handler):
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
    return serve


def _sse(handler):
    body = b'data: {"ok":1}\n\ndata: [DONE]\n\n'
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class LocalServer:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.count = 0
        self.request_ids = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _serve(self):
                length = int(self.headers.get("Content-Length", 0))
                if length:
                    self.rfile.read(length)
                index = min(outer.count, len(outer.steps) - 1)
                outer.count += 1
                outer.request_ids.append("audit-http-%04d" % outer.count)
                try:
                    outer.steps[index](self)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

            do_POST = _serve
            do_GET = _serve

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
        return "http://%s:%d" % (host, port)


def network_scenarios():
    policy = RetryPolicy(max_attempts=4, initial_delay=0.0, factor=1.0,
                         max_delay=0.0, max_elapsed=5.0, jitter=0.0)
    with LocalServer(_status(500, '{"error":{"message":"down"}}')) as server:
        provider = ChatGPTSubscriptionProvider(
            object(), base_url=server.url, retry=policy,
            timeout=2, connect_timeout=1, stall_timeout=1)
        provider._headers = lambda: {}
        chunks = list(provider.stream([Msg(role="user", content="x")], [],
                                      "m", 10))
        exhausted_count = server.count
        exhausted_ids = list(server.request_ids)
        errors = sum(1 for chunk in chunks if (chunk.usage or {}).get("error"))

    refusal_body = json.dumps({"type": "error", "error": {
        "type": "rate_limit_error", "message": "Error"}})
    with LocalServer(_status(429, refusal_body,
                             {"X-Should-Retry": "true",
                              "Anthropic-Organization-Id": "audit-org"})) as server:
        retryable = None
        try:
            list(stream_sse_events(server.url, {}, retry=policy))
        except NetError as exc:
            retryable = exc.retryable
        refusal_count = server.count
        refusal_ids = list(server.request_ids)

    phases = {"dns_ms": 0.0, "connect_including_dns_ms": 0.0,
              "tls_ms": 0.0, "tls_exercised": False}
    real_dns = socket.getaddrinfo
    real_connect = socket.create_connection

    def dns(*args, **kwargs):
        started = time.monotonic()
        try:
            return real_dns(*args, **kwargs)
        finally:
            phases["dns_ms"] += _ms(time.monotonic() - started)

    def connect(*args, **kwargs):
        started = time.monotonic()
        try:
            return real_connect(*args, **kwargs)
        finally:
            phases["connect_including_dns_ms"] += _ms(
                time.monotonic() - started)

    with LocalServer(_sse) as server, \
            patch.object(socket, "getaddrinfo", dns), \
            patch.object(socket, "create_connection", connect):
        started = time.monotonic()
        events = stream_sse_events(server.url, {}, retry=policy)
        try:
            next(events)
            first_event_ms = _ms(time.monotonic() - started)
        except StopIteration:
            first_event_ms = _ms(time.monotonic() - started)
        list(events)
        total = _ms(time.monotonic() - started)
        loopback_ids = list(server.request_ids)
    phases["status_and_stream_total_ms"] = total
    phases["time_to_first_event_ms"] = first_event_ms
    phases["request_ids"] = loopback_ids
    phases["tcp_approx_ms"] = round(max(
        0.0, phases["connect_including_dns_ms"] - phases["dns_ms"]), 3)

    return {
        "persistent_500": {
            "net_policy_attempts": policy.max_attempts,
            "http_requests": exhausted_count,
            "request_ids": exhausted_ids,
            "terminal_error_chunks": errors,
        },
        "terminal_429": {
            "http_requests": refusal_count,
            "request_ids": refusal_ids,
            "retryable": retryable,
        },
        "loopback_transport": phases,
        "note": "TLS and live-provider first-token latency require the Haiku/live gate",
    }


def mcp_startup_scenario(probe_wait=0.05):
    """Measure the bounded startup wait without launching a child process."""
    release = threading.Event()

    class Config:
        data = {"mcp": {"slow": {"command": ["audit-fake-mcp"]}}}

    class Client:
        def list_tools(self):
            return []

        def close(self):
            pass

    def connect(manager, entry):
        release.wait(2.0)
        return Client()

    manager = MCPManager(Config(), cwd=str(ROOT))
    try:
        with patch.object(MCPManager, "_connect", connect):
            started = time.monotonic()
            manager.start_all(wait=probe_wait)
            elapsed_ms = _ms(time.monotonic() - started)
            connecting_after_return = manager.status().get("slow") == "connecting"
            release.set()
            for thread in list(manager._threads):
                thread.join(1.0)
        return {
            "configured_default_wait_seconds": DEFAULT_STARTUP_WAIT,
            "probe_wait_seconds": probe_wait,
            "probe_elapsed_ms": elapsed_ms,
            "connecting_after_return": connecting_after_return,
            "desktop_processes_per_send": 1,
        }
    finally:
        release.set()
        manager.shutdown_all()


def persistence_scenario(root):
    store = SessionStore(root / "audit-sessions.db")
    try:
        session = store.new_session(str(root), "fake", "m")
        started = time.monotonic()
        for index in range(100):
            session.append(Msg(role="assistant", content="x" * 200 + str(index)))
        elapsed = time.monotonic() - started

        manual_requests = RequestLog()
        manual_provider = SummaryProvider(manual_requests)
        raw_before = len(session.messages)
        started = time.monotonic()
        folded = session.compact_now(keep_last=10, provider=manual_provider,
                                     model="m", trigger="manual")
        compact_ms = _ms(time.monotonic() - started)
        started = time.monotonic()
        restored = session.restore_compaction()
        restore_ms = _ms(time.monotonic() - started)
        return {
            "append": {"messages": 100, "append_transactions": 100,
                       "total_ms": _ms(elapsed)},
            "manual_compact_and_undo": {
                "raw_messages_before": raw_before,
                "folded_messages": folded.folded,
                "summary_calls": manual_provider.summary_calls,
                "compact_ms": compact_ms,
                "restored_messages": restored,
                "restore_ms": restore_ms,
                "raw_messages_after_undo": len(session.messages),
                "requests": manual_requests.rows,
            },
        }
    finally:
        store.close()


def backup_startup_scenario(root, payload_mb=10):
    path = root / "backup-startup.db"
    store = SessionStore(path)
    session = store.new_session(str(root), "fake", "m")
    conn = store.connect()
    conn.execute(
        "INSERT INTO messages (session_id, seq, role, content) "
        "VALUES (?, 1, 'tool', ?)",
        (session.id, "x" * (int(payload_mb) * 1024 * 1024)))
    conn.commit()
    store.close()

    samples = []
    for _ in range(2):
        started = time.monotonic()
        opened = SessionStore(path)
        opened.connect()
        samples.append(_ms(time.monotonic() - started))
        opened.close()
    return {
        "database_mb": int(payload_mb),
        "backup_min_interval_seconds": BACKUP_MIN_INTERVAL,
        "first_due_open_ms": samples[0],
        "next_worker_open_ms": samples[1],
    }


def desktop_startup(runs):
    samples = []
    env = dict(os.environ)
    env["HAI_DESKTOP_TEST_REPLY"] = "ok"
    for _ in range(max(0, int(runs))):
        started = time.monotonic()
        completed = subprocess.run(
            [sys.executable, "-m", "haikode.desktop_worker",
             "--provider", "zen"], cwd=str(ROOT), input=b"synthetic",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            check=False)
        samples.append(_ms(time.monotonic() - started))
        if completed.returncode != 0:
            return {"runs": len(samples), "error": "worker exit %d" %
                    completed.returncode}
    return {"runs": len(samples), "median_ms": _median(samples),
            "min_ms": min(samples) if samples else 0.0,
            "max_ms": max(samples) if samples else 0.0,
            "processes_per_send": 1}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--desktop-runs", type=int, default=5)
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)
    root = Path(tempfile.mkdtemp(prefix="haikode-performance-audit-"))
    try:
        result = {
            "schema": 1,
            "clock": "time.monotonic",
            "external_network": False,
            "agent": agent_scenarios(root),
            "network": network_scenarios(),
            "mcp_startup": mcp_startup_scenario(),
            "persistence": persistence_scenario(root),
            "database_open": backup_startup_scenario(root),
            "desktop_worker": desktop_startup(args.desktop_runs),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
    print(json.dumps(result, indent=2 if args.pretty else None,
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
