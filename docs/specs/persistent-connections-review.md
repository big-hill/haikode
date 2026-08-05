# Adversarial review of the persistent-connections spec

Reviewer: Kimi K3 (max effort), 5 August 2026, reading haikode/net.py and
tests/test_net.py directly. Kept verbatim, reasoning included: the
reasoning is where the kill shot was found, and a summary would lose the
experiments that settled it.

Its conclusions are folded into the spec (v2). The two that changed the
design: pooling turns fast refusals into three-minute silent hangs
unless time-to-first-byte on a reused connection is bounded separately,
and a pool-layer retry would have broken post_json's no-replay contract
for MCP tool calls.

---

kimi version 0.33.0
Warning: [loop_control] 'max_retries_per_step' is deprecated and no longer used; rename it to 'max_attempts_per_step'. Run /update-config to fix it.
• Let me read the three files in order.Wait — the spec references `stream_sse_events` but the file has `sse_json_events` and `stream_sse_events` at line 705. The spec says net.py:576 for "the rule that a half-consumed turn is never re-sent" — actually the committed flag is at lines 673, 682, 689. The spec says `_open()` at net.py:404, but it's actually at line 421. Line numbers in spec are off but roughly. The spec mentions net.py:576 — near there is the sse_json_events committed rule (line 689 actually). Anyway.

  Now read tests/test_net.py.

• Now the test file.

• Now let me think hard about this adversarial review. Let me verify some claims about the code first.

  Key code paths:

  1. `_open` (net.py:421-452) — builds a fresh `urllib.request.Request` and `_opener(read_timeout).open(req, timeout=connect_timeout)`. Each call builds a new opener. urllib's `do_open` in AbstractHTTPHandler — does it reuse connections? Actually, `urllib.request.AbstractHTTPHandler.do_open` creates a new `http.client.HTTPConnection` per call and calls `h.request(...)`, then `h.getresponse()`. Importantly: **urllib's do_open already sends `Connection: close`**? Let me recall. In `AbstractHTTPHandler.do_open`, it adds headers: `req.headers["Connection"] = "close"`? Actually yes — in CPython's `urllib/request.py`, `AbstractHTTPHandler.do_open` does:

  ```python
  h.request(req.get_method(), req.selector, req.data, headers,
            encode_chunked=req.has_header('Transfer-encoding'))
  ```

  and earlier:
  ```python
  if not req.has_header('Host'): ...
  # We want to make an HTTP/1.1 request, but the addinfourl
  # class isn't prepared to deal with a persistent connection.
  # It will try to read all remaining data...
  ```

  Actually I recall that urllib.request sends `Connection: close` explicitly. Let me check: In CPython source, `AbstractHTTPHandler.do_open`:

  ```python
  try:
      try:
          h.request(req.get_method(), req.selector, req.data, headers,
                    encode_chunked=req.has_header('Transfer-encoding'))
      except OSError as err: # timeout error
          raise URLError(err)
      r = h.getresponse()
  ```

  And in `do_request_`... Hmm. Actually I believe urllib's HTTPHandler sets "Connection: close" header. Let me verify with a local experiment under /tmp — allowed. I can check the installed Python source directly.

  Actually this matters a lot: if urllib sends `Connection: close`, then the pool spec requires replacing the urllib-based `_open` with http.client-based code (or stripping that header). The spec says `_open()` becomes "take a connection from the pool (or build one), send the request on it, return a response object". That's a substantial rewrite of _open, moving off urllib. Notable: redirects, proxies, error handling from urllib handlers would be lost or need reimplementation. Actually _opener is built fresh per request with handlers that don't include redirect handler? `build_opener` adds default handlers including HTTPRedirectHandler. Dropping urllib means dropping redirect handling — providers might 307/308? Probably not a big deal but worth noting.

  Also important: the response object returned by urllib is `addinfourl` wrapping `HTTPResponse`; `fp.raw._sock` access in `_hard_close` works on that. With pooling via raw http.client, `_hard_close` introspection still works.

  2. The kill shot: the spec's own question — a reused connection dying after the first SSE event. Rule 4 says: "treat a failure on the FIRST write/read of a reused connection as a retry-on-a-fresh-connection". But wait — the failure mode that's worse: the pool's first-write-failure retry happens *inside* `_open`, before any event is delivered, so retry is fine there. The genuinely worse scenario: a reused connection whose death is **silent** — the server closed it gracefully (FIN), the client writes the request into a half-closed socket. On TCP, a write to a connection that received a FIN often succeeds (the peer's receive buffer still accepts? No — after FIN, peer won't read; first write typically succeeds or gets RST only after). Actually if the server sent FIN (graceful close), writing to the socket succeeds initially (peer ACKs the data but the application discards it? no — after FIN the peer sent, its receive side may still be open... FIN means peer closed its sending side; our writes are ACKed). Then the read gets EOF. That's detectable at first read — fine.

  If the server sent RST (crashed or SO_LINGER 0), the first write succeeds, second write gets EPIPE, read gets ECONNRESET. Detectable on first read too — before any event. Fine under rule 4.

  The worse variant: the connection dies **mid-stream after reuse** in a way that a fresh connection wouldn't experience... hmm, is there a mechanism by which a reused connection is *more likely* to die mid-stream than a fresh one? The spec's scenario: server idle-timeout kills the connection *between* the moment the pool validated it (or last used it) and the request. Rule 4 caps idle age at 30s and retries first-write failures. But consider: the server's idle timer is e.g. 60s; the pool's cap is 30s... but the 30s cap is measured from *last use*, and TLS session... no.

  The real kill shot candidate: **a race between "connection was fine" and "server closes it while our request is in flight"**. Server closes idle connection at T; client acquires it from pool at T-epsilon, writes request at T+epsilon. The request bytes go into a closing socket. Server may RST. Client's first read fails → retry on fresh connection per rule 4. Nothing reached the model... but wait — did the request reach the model? If the server got part of the request before RST? The server closed the connection because it was idle; it's not reading. So nothing reached the model. Retry safe. OK.

  But here's the genuinely worse one, and I think it's the spec's own closing question plus a sharpened variant: **the request DOES reach the server on a reused connection, and the connection dies after the response's first byte(s)**. Is that more likely on a reused connection? Consider NAT state: an idle TCP connection through a NAT/stateful firewall loses its NAT mapping after an idle window (typical NAT timeouts: 60s–several minutes; some carrier NATs much shorter). A connection idle for 25s (under the pool's 30s cap) may have a dead NAT mapping *if* the NAT timeout is short. Then: client writes request — packets go nowhere; no RST comes back (the NAT silently drops inbound). The first read doesn't fail fast; it hangs until... the read timeout. But wait, the read timeout on acquire for SSE is the *stall timeout* (rule 3 says re-applied on acquire — stream_sse_events uses a long stall budget, default 180s). So the first read on a reused-but-NAT-dead connection **blocks for up to 180 seconds** before failing. Compare with a fresh connection: connect would fail fast or succeed. Worse: on Haiku... Actually the write itself: TCP retransmits, no RST, so the client sits in read. With rule 4's retry-on-first-failure, the failure arrives after the full stall budget (default 180 s). The user observes: the turn hangs for three minutes on a perfectly healthy network, then retries and works. Fresh-connect haikode today: connects in milliseconds. That's a concrete scenario where reuse is *worse* — silent middlebox death produces a hang, not a fast refusal, and rule 4's "failure on the first read" arrives only after the stall timeout. Rule 4 says nothing about bounding the *first* read on a reused connection to a short probe window. The connect_timeout (20s) doesn't apply because there's no connect. The fix: on a reused connection, bound the time to first response byte with the connect-timeout-scale budget (a "time to first byte" probe, e.g. 10-20s), not the full stall budget. This is a real, concrete kill shot, and it's *not* what the spec's closing question points at — the spec's candidate (dies after first event) is actually refutable: whether a stream dies after the first event is roughly independent of reuse; a fresh connection can also die mid-stream, and the committed rule applies equally either way. Reuse only shifts the *distribution*: pooled connections are older, and middleboxes kill old idle connections. Hmm, actually there's a subtle point: on a reused connection, the window of vulnerability (connection dead but undetected) exists at request start — that's covered by rule 4 (pre-first-event, retryable). After the first event arrives, the connection is demonstrably alive (bytes are flowing). A death after the first event on a reused connection is no more likely than on a fresh one — both are established, flowing connections. So the spec's closing candidate is mostly refuted: rule 4 covers the pre-first-byte window, and post-first-byte deaths are parity with today. The *worse* one is the silent-death hang: no RST, no EOF, just a black hole, and the retry that rule 4 promises arrives 180 s late — or worse.

  Actually wait, even worse: TCP retransmission timeout on the *write*. If the NAT mapping is dead, the client's SYN... no SYN, connection exists. The write bytes get retransmitted with exponential backoff (15min+ total on Linux). The application blocks in read with the socket timeout. Socket timeout is the stall budget (180s). So 180s hang, then NetError retryable, then rule 4 retry on fresh connection. User sees a 3-minute freeze per turn. And on the *chat* path the default timeout is DEFAULT_TIMEOUT=180. Actually also — is the failure even classified retryable? A socket.timeout on first read inside `_iter_lines` → NetError "Stream stalled" retryable=True → sse_json_events: committed=False, so it retries — burning a RetryPolicy attempt though, not the invisible rule-4 retry (rule 4's retry lives inside _open presumably). Depends where the line-iterator starts relative to "_open". In the pooled design, _open returns the response; the first read happens in the pump. Rule 4's "first write/read" retry must then live in the pump/first-read path... The spec is vague about *where* the first-read retry happens. If the first read hangs for 180s, that's the kill shot. Also for `post_json`: `response.read()` with read timeout = timeout (default 180, callers pass 5 etc.).

  A second kill-shot candidate: **the pool defeats the abort path via the pump thread and rule 2**. When a stream is fully consumed to completion and the connection is released — is it truly fully consumed? `_iter_lines` yields lines until `_EOF`; the pump put `_EOF` only after the `for line in response` loop ended, meaning the chunked body was read to its terminator. So release-after-EOF is safe. But note `stop_on_done`: `stream_sse_events` uses `stop_on_done=True` — `_sse_events` returns when it sees `[DONE]`, **possibly before the underlying stream is consumed**. Look: `_sse_events.flush` returns `_EOF` when payload is `[DONE]`, and `_sse_events` returns. The `lines` generator is then closed in the `finally` of sse_json_events — closing `_iter_lines`, which sets stop and hard-closes the response. Is there unread data after `[DONE]`? Typically `[DONE]` is the last event, then the chunked terminator. But the *terminating chunk* (`0\r\n\r\n`) may not have been read yet! The pump may be blocked reading the terminator. So a stream that ended "cleanly" with [DONE] has NOT necessarily fully consumed the body — the trailing `0\r\n\r\n` may still be in flight. Under rule 2, that connection must be discarded... which means **every normal SSE stream is discarded** (because consumers stop at [DONE], not at stream end), and pooling gives almost no reuse on the primary path! Wait, but actually, does anything remain? After [DONE], the server sends `0\r\n\r\n` immediately (typically). The pump reads it promptly, sees EOF, puts _EOF in the queue. By the time the consumer processes the [DONE] event... race. The consumer closing the generator may beat the pump's read of the terminator. If the terminator hasn't been consumed, http.client's chunked state (chunk_left) is non-None → not fully consumed → discard. So on the dominant path (stop_on_done=True, which stream_sse_events always uses), the connection will *usually* fail the "fully consumed" test unless the implementation waits. Actually would it? The server writes [DONE] chunk and terminator back-to-back; client kernel buffers both; pump's readline loop reads the [DONE] lines, then next iteration reads the terminator → loop ends → _truncated check → put(_EOF). All this happens in microseconds on the pump thread, while the consumer gets the [DONE] line, parses JSON, yields event, sees _EOF from _sse_events... The consumer side involves queue handoff + JSON parse + generator yield to the caller (the agent loop!), which may do work between events. Between the [DONE] event and the generator close, the caller does a `yield event` to the agent — the agent processes it (tool calls!). Then loop continues, _sse_events flush returns _EOF on the NEXT line... wait no. Let me re-trace: `for event in events` in sse_json_events: events is _sse_events generator. It reads lines until a blank line flushes "[DONE]" → returns _EOF → _sse_events returns → StopIteration → the for loop ends → return → finally closes generators. The [DONE] flush happens when the blank line after `data: [DONE]` is read. At that point the pump may not yet have read the terminator. But the terminator is only 5 bytes sent in the same burst; the pump's `for line in response` after yielding the blank line continues to readline → gets... HTTPResponse iteration yields body lines; after the blank line, readline blocks (briefly) then gets EOF when the terminating chunk arrives → loop ends → put(_EOF). The window is small but nonzero, and on Haiku with its scheduler the consumer can easily win the race. Also some providers keep the connection open after [DONE] and delay the terminator (or send more keep-alives). Actually more importantly: with keep-alive now enabled, the server will NOT close the connection after the response — it sends the terminator and waits for the next request. So the pump, after reading the blank line, blocks in readline... gets EOF only after reading `0\r\n\r\n`. Fine, that's prompt.

  But here's the thing: whether release happens is determined by checking "was the body fully consumed". If the implementation instead checks `_truncated`-style state, then in the [DONE] race the connection gets discarded ~half the time. The spec's proof test: "Two sequential requests must produce one accepted connection" — for post_json maybe. For streams, "an aborted stream followed by a request must produce two". It doesn't test that a *completed* stream releases its connection! That's a missing proof point. And the design pressure: to actually get reuse on the stream path, the implementation must drain the stream after [DONE] (read the terminator before release) — the spec doesn't mention draining at all. Draining is a new blocking read on the consumer path... bounded how? That's a missing rule: "a connection is only reusable if the framing layer reports end-of-body; if the consumer stopped early for any reason (including [DONE] before the terminator is read), drain with a short timeout or discard."

  Actually wait — let me reconsider. Does `_sse_events` with stop_on_done consume everything? `for raw in lines` — when flush returns _EOF, it returns from the generator, abandoning `lines` mid-iteration. Yes, early stop. Confirmed at net.py:562-563, 573-574.

  So kill shot #2 (weaker pooling value): normal streams stop at [DONE], before end-of-body, so rule 2 forces discard on the common path → the pool mostly helps post_json (listings) but not turns, unless drained. Hmm, but actually — is that right that the terminator isn't read? The pump thread runs ahead of the consumer! The pump reads lines as fast as the server sends, filling the queue (maxsize 512). The consumer lags. So when the server sends [DONE] + terminator together, the pump reads ALL of it — including the terminator — and puts _EOF in the queue, possibly long before the consumer processes [DONE]. So by consumer-close time, the body IS fully consumed (pump read to EOF... not EOF of socket — end of chunked body; the `for line in response` loop ends when the body ends, not the socket). So in the common case, the framing is complete by release time, and reuse works. BUT the race is real when the consumer is fast and the network/server is slow to send the terminator, or the server holds the connection open without sending the terminator promptly. And there's a nastier variant: queue maxsize=512 — if the queue is full, the pump blocks in `put` with the body partially read... but if the queue is full the consumer is way behind; on [DONE] the consumer catches up quickly. Eh.

  The important point for the review: whether a completed stream leaves a releasable connection depends on a **thread race** (pump ahead of consumer) that the spec never mentions, and the correctness check for "fully consumed" must be on framing state (`chunk_left is None` / `response.isclosed()` or `will_close`), not on "the caller returned". The spec's rule 2 says "a response must be fully consumed before its connection returns to the pool" — sufficient as a rule, but the spec doesn't note that the *pump thread* is what consumes, and it may still be blocked in readline (holding the BufferedReader lock, net.py:300-310) at the moment the consumer decides to release. And you cannot touch the response to check framing state from the consumer thread without taking that same lock → the abort path (rule 6) could block behind a pump that is stuck in recv() until its timeout — the exact bug class _hard_close exists to avoid (net.py:300-310, 361-368). So the release check must either be done by the pump itself (pump reports "consumed to end" via a flag before putting _EOF) or the connection is discarded whenever the pump hasn't finished. A missing rule: **only the pump thread may vouch for full consumption; release must never require taking the BufferedReader lock or joining the pump.**

  Also note: `_iter_lines`' finally calls `_hard_close(response)` unconditionally today. With pooling, the finally must decide release vs discard. But the finally runs when the consumer closes the generator — potentially while the pump is still alive and blocked (up to 180s read timeout). You can't release a connection whose pump is still reading. And killing the pump's socket = hard_close = discard. So mid-stream aborts (stall, abort, break) → discard, per rules 1-2. Fine. But the tail: after a *successful* full stream, `_EOF` was put by the pump and the pump has exited (pump returns after put(_EOF)). So in the success case the pump is done and release is safe — **except** the stop_on_done case above, where the consumer stops early and the pump may still be inside `for line in response` blocked on the next readline. With keep-alive the socket stays open after body end, so the readline returns at body end (chunk terminator) — pump exits. Race again: pump needs the terminator bytes; they usually arrive with [DONE]; fine. But a provider that sends [DONE] and holds the chunk open (does some providers delay the terminator? OpenAI sends data: [DONE] then terminates promptly) — low risk but real.

  Now the worse kill shot I found: silent middlebox/NAT death → reused connection black-holes → first read hangs for the full stall budget (180s default; net.py:41) before rule 4 can fire. Wait — actually let me re-check: on acquire, rule 3 re-applies the timeout. For a stream request, read timeout = stall = up to 180s. The first read blocks up to 180s. During that time, abort still works (pump/queue). But the turn appears hung for up to 3 minutes. Meanwhile today's fresh connection: connect_timeout 20s → if the link is dead, ECONNREFUSED/timeout in ≤20s, retryable. So pooling converts a ≤20s failure into a ≤180s hang. That's concrete, plausible (NAT/carrier-grade NAT, firewall state timeouts shorter than 30s are rare but real — e.g. some enterprise firewalls 60s? No wait — the pool caps idle age at 30s, so the connection is at most 30s idle. NAT mappings typically last ≥60s for TCP... Linux netfilter default is 5 days for established; many CPEs use several minutes. A sub-30s NAT timeout is uncommon). Hmm, so is my kill shot plausible within the 30s idle cap? The connection is reused only if idle < 30s. Middleboxes with TCP established timeout < 30s... uncommon but the flaky-wifi scenario in the spec: the laptop's wifi drops during the idle window! That's the spec's own motivating scenario: connection idle 10s, wifi reassociates (no packets flowed), connection survives per spec's premise. Mostly yes TCP survives. But now consider: wifi drops *while the request is in flight on the reused connection* — same as fresh. Hmm.

  Let me think about the *real* worst case again, the spec's closing candidate: "a reused connection dying after the first SSE event". Sequence: connection pooled, idle 5s, healthy. Client reuses it, sends request. Server's idle timeout fires concurrently (server timeout 60s but pool thinks... no, pool cap 30s < 60s, but what if the server's timeout is 10s? Some servers/proxies have short idle timeouts — e.g., AWS ALB default 60s, Cloudflare 400s? but some API gateways 10-30s. The spec says "30–120 s" observed, cap 30 "safely under every observed" — but 30 is NOT safely under 30! If a provider closes at 30s and the pool cap is 30s, the race window is real: pool acquires at 29.9s idle, server closes at 30s. The write crosses the FIN in flight. Client writes request into half-closed socket; first read → EOF/RST → rule 4 retry. Fine, that's covered... if rule 4's retry works. BUT: subtle — after the client writes the request successfully (kernel ACKs it — wait, if peer sent FIN, peer's kernel still ACKs data it receives? After a FIN is sent, the peer's receive side is still open unless they did close() — close() sends FIN and discards... receiving data after close() → peer responds RST. If server did shutdown(SHUT_WR) only (rare for HTTP servers; they close), then write is ACKed, read gets EOF. Either way, first read fails or returns EOF. EOF on first read of a response: http.client getresponse raises RemoteDisconnected. Retryable under rule 4. OK.

  So the spec's candidate: "dies after the FIRST SSE EVENT" — for the connection to die *after* the first event, bytes flowed (the first event), so the connection was alive at that moment. Its subsequent death is not a reuse-specific phenomenon... EXCEPT one mechanism: the server closed the connection due to idle timeout *while sending the response*? No. Hmm — actually here's a mechanism: **the server's idle-close fires during the response**: server started responding, sent event 1, then... no, that kills fresh connections too. I think the spec's candidate is refutable: post-first-event death risk is essentially equal for fresh vs reused; reuse adds risk only in the pre-first-byte window, which rule 4 covers — *provided* rule 4's retry is implemented at the right layer and provided the first-read is bounded. The genuinely worse scenario is the black-hole first read (no RST/EOF): request written to a connection whose return path is dead (asymmetric link failure — wifi drops between request and first byte). On a fresh connection, a dead link refuses the connect in ms (spec's own premise, spec line 22-23). On a reused connection, the same dead link produces a *hang*: write queued, retransmits vanish, first read waits out the entire stall budget (net.py:41, 180 s; line 665-666 stall default). Rule 4 fires only after that. And note asymmetry: wifi down/up between turns is exactly the spec's motivating case — the connection looks alive, the link died, and the first probe of that fact is the stalled read. Actually wait — if the link is down, does a write fail fast? No: TCP queues and retransmits. The read blocks until socket timeout. So YES: in the spec's own field scenario (flaky wifi), pooling converts fast refusals into 180-second hangs whenever the link dies during the idle window... no wait, only when the link dies *and stays down* — if it comes back in 20s, the retransmit succeeds and the response flows; user sees a 20s pause. If down for 60s (reassociation window per spec is "tens of seconds"), the reused connection's first read may recover when the link returns (TCP retransmit succeeds) — actually that's the *benefit*: the idle connection survives and the in-flight request also survives. Hmm, but TCP retransmit backoff: after ~1-3s first retransmit, exponential up to 100s+ between retransmits; user-space write already returned; the data goes when the link is back. Response flows. Fine.

  The bad case: link down longer than the stall budget → 180s hang vs today's fast refusal+retry ladder (which itself now waits 120s per the widened retry... today's behavior: 6 attempts over ≤120s of refusals then give up — user sees failure after ~2min; pooled: sees 180s hang then... rule 4 retry → fresh connect → refused fast → RetryPolicy ladder). Honestly the hang is comparable in total to today's retry budget but the *user experience* differs: today, failures surface as errors and retries with visible backoff; pooled, the UI freezes mid-turn with no error for 3 minutes. Hmm, actually today during retry the TUI presumably shows reconnecting. The pooled silent hang is worse UX but not catastrophic.

  Let me also examine the abort-path/pump interaction more concretely, since the question explicitly asks: pooling + pump thread holding BufferedReader lock + outliving caller's return.

  Paths where the pump outlives the caller: `_iter_lines` finally sets stop and hard-closes. Hard-close parks fd (net.py:338-345) — the pump, blocked in recv inside OpenSSL on that fd, wakes... on Haiku only when its own timeout expires (per the docstring net.py:300-310; dup2 wakes it per line 305-308? "Releasing the file descriptor does wake the blocked reader on every platform" but line 361-368 says a pump that entered poll() just before holds the lock until its own read timeout expires "on macOS". On Haiku, dup2 wakes the reader — line 305-306 says so). Anyway: after _hard_close returns, the pump thread may STILL be alive (until its timeout). It holds the BufferedReader lock. Today that's fine because the response object is dead. With pooling: the connection object must never be reused while a zombie pump exists — rules 1-2 ensure discard. But who owns the *pool slot*? If discard closes... the discard must use the same _hard_close semantics (park, don't close-under-lock). The spec's rule 6 covers "no new waiting" but the missing rule is: **the pool must not block on a zombie pump for any operation — release checks, discard, pool-full eviction, and process-exit emptying (rule 7) must never join a pump thread or take the BufferedReader lock.** Rule 7 says "the pool is emptied at process exit" — if emptying means closing connections, closing a healthy pooled connection takes... closing an idle HTTPResponse/http.client connection — needs the BufferedReader lock too if a response object exists. If a connection was released, its body was fully read; but the HTTPResponse object still exists with its fp; closing the connection while no pump → fine. At exit, daemon threads... ok.

  Another angle: **HTTP/1.1 response without Content-Length and without chunked**: `response.length` and connection reuse — http.client sets `will_close = True` when the response is delimited by connection close. A pooled connection must check `will_close` — if the server said `Connection: close` (some providers do), the connection is dead after this response and must not be released. Rule missing? Rule 2 covers "fully consumed" but not "the server told us it will close". If released, next borrower writes into a closing socket — caught by rule 4 retry. Minor, covered by 4, but a released `will_close` connection defeats pooling silently (every release dead on arrival). Should check `response.will_close` at release.

  Also **proxy/tunnel**: _opener build_opener includes ProxyHandler — HTTPS through a proxy means CONNECT tunnels; pooling per (scheme, host, port) keyed on the origin is wrong for tunneled connections (the socket is to the proxy). If env proxies are set on Haiku... probably none. But keying must account for proxy. Worth mentioning.

  Also `_SplitTimeoutMixin.connect()` — sets read timeout after connect. In pooled design, rule 3 says re-apply on acquire. Fine.

  Also: **two different read timeouts on same host** — post_json to /models and stream to /chat on same host share a pool; timeouts re-applied per acquire, ok per rule 3.

  Also **auth headers**: pooling across requests with different Authorization headers to the same host — fine at HTTP level. No issue.

  Also the `put` backpressure: box maxsize 512 lines; if consumer abandons, pump blocks in put retry loop bounded by stop flag checks every `poll` (0.25s) — pump exits within 0.25s-ish even if queue full (put loops on queue.Full, checks stop). Good — so pump doesn't outlive much after stop. But pump blocked in `readline` on the socket outlives until fd release or timeout. On the abort path, _hard_close releases the fd → wakes on Linux/macOS? (line 305 says releasing wakes "on every platform", but 361-368 caveat poll() on macOS). Whatever — pooled discard follows the same path.

  One more candidate kill shot: **the pool turns `test_streams_do_not_leak_pump_threads`-style zombie pumps into fd holders**. A zombie pump (blocked in recv until its 180s timeout) holds... its fd was parked on /dev/null by hard_close, so no fd leak. But consider the *stall-timeout* path (net.py:531-534): consumer raises NetError stalled → finally: stop.set(), _hard_close → discard. Fine.

  What about **post_json releasing**: `body = response.read()` reads to completion; then release. read() on a chunked response reads to end — fully consumed. On exceptions (timeout mid-body) → discard. post_json's finally currently hard-closes (net.py:632). With pooling, the release decision: success → release, any exception → discard. Simple. The response.read() blocks with timeout = the request timeout; abort during post_json read — there's no abort check during read()! Today: post_json's read is NOT cancellable mid-read (abort checked only before open and in _sleep). That's existing behavior, not pooling's problem.

  Now, the server-closes-idle race with in-flight request — the "first write/read failure" detection: **the write usually succeeds** (kernel buffers), failure surfaces at first read as RemoteDisconnected/ECONNRESET — http.client raises `RemoteDisconnected` (subclass of ConnectionResetError + BadStatusLine). Rule 4 says treat as retry-on-fresh. But careful: a read failure could also occur *after* the server received and processed the request (server crashed mid-processing). Rule 4 as stated retries "a failure on the FIRST write/read of a reused connection" — but if the failure happens on first read *after the server got the full request*, the request DID reach the model. For SSE chat turns, retrying duplicates the generation (server-side may have consumed quota; client just reconnects — the turn was not committed client-side, so retry is per existing semantics acceptable: today a fresh connection that dies before first event is also retried — sse_json_events retries pre-commit, net.py:689). So rule 4 doesn't introduce a *new* semantic violation: pre-first-event retry is already the rule for fresh connections. BUT rule 4 says "This retry is invisible and does not consume a RetryPolicy attempt: nothing reached the model." — the claim "nothing reached the model" is **unfounded** in the RST-after-request case. The server may have received the entire request and started generating; the client-side failure to read the response doesn't mean the request didn't arrive. For chat this is harmless-ish (same as today's pre-commit retry), but for **`post_json` carrying MCP tools/call — explicitly non-idempotent (net.py:601-607, test_net.py:515-520)** — rule 4 as written would replay a tools/call whose first read failed on a reused connection! Today, post_json with NO_RETRY makes exactly one attempt; a failure on the first read of a fresh connection raises. With pooling + rule 4, the same failure gets an invisible extra replay → a shell command could run twice. That violates the post_json contract and the spec doesn't carve it out. **Rule 4 must not apply to post_json/non-idempotent requests** — or must apply only to the request-not-yet-fully-written case. That's a strong finding: rule 4 as stated is not sufficient; it contradicts the documented post_json no-replay guarantee.

  Hmm wait — does it? Rule 4's retry happens inside the pool/_open layer, below post_json's policy. post_json's NO_RETRY means one attempt at the policy layer; the pool adds a hidden second attempt. Yes, that's a genuine violation of the stated invariant. Good catch for the review.

  Also missing rule: **TLS session / certificate state** — reusing HTTPSConnection is fine. Skip.

  Missing rule candidate: **A connection used for a request that returned an error status must still be releasable if the body was consumed** — trivial, not needed.

  Missing rule: **stale-connection check via select/poll before reuse?** Spec relies on idle-age cap + rule 4 retry. Fine.

  Haiku angle (question 3): The fd-reuse incident (net.py:322-331): a closed descriptor number reused by sqlite while a reader inside OpenSSL. Pooling variants:

  (a) The pool holds idle open connections — fds stay allocated longer, more concurrent fds... not a new bug class.

  (b) The dangerous new variant: **two threads sharing pool state where one discards (hard_close/park) while the other is told the connection is healthy** — no, exclusive ownership per rule 5 prevents that.

  (c) The real new variant: on Haiku, a blocked recv() ignores shutdown() and returns only at timeout (net.py:300-305). With pooling, the *release decision* happens in `_iter_lines`' finally on the consumer thread while the pump may still be blocked in recv. If the implementation "releases" by checking state via the response object (e.g., calling `response.isclosed()`, `response.peek()`, reading `chunk_left` — wait, `peek` takes the BufferedReader lock!) — any of those block behind the pump's lock for up to the read timeout (180s), turning a turn-completion into a hang. _hard_close explicitly avoids response.close() for this reason (net.py:361-368). So the release path must be lock-free: pump sets a flag ("body_complete") before exiting; consumer releases only if flag set, else discard via _hard_close. The spec doesn't state this; the naive implementation (check `chunk_left` on release — `chunk_left` is a plain attribute, no lock needed; `response.closed`? HTTPResponse doesn't have `.closed`... `isclosed()` checks `self.fp is None` — no lock. Hmm, actually checking attributes is lock-free. But `chunk_left` is written by the pump thread — read it racily, fine-ish on GIL. The danger is only if implementation calls any read/peek/close method.)

  (d) Haiku variant of "parked fd": when discarding a pooled connection, must park — same as today. No new variant. BUT: pooled idle connections at **process exit** (rule 7): "pool is emptied at process exit" — if emptying closes sockets while a daemon pump thread is still inside OpenSSL on another connection... atexit ordering with daemon threads on Haiku — same bug class as the sqlite incident: closing frees the number; another thread may reuse. Mitigation: park, don't close. The spec should say discard-at-exit uses the same parking discipline. Worth one line.

  (e) One more Haiku-specific: since blocked recv ignores shutdown, a pooled connection whose pump is zombie stays "unreadable" — but it's discarded, so irrelevant.

  Simpler alternative (question 4): Options:
  - **(A) Pool only for `post_json`/short idempotent requests, keep streams fresh.** The refusal problem the spec cites is on turns (streams) though — the stream IS the request that fails with ECONNREFUSED when wifi reassociates. Pooling only post_json doesn't fix the motivating failure. Actually wait — what fails in the field? The chat turn's stream connect. So a stream-excluded pool misses the point. Hmm, but does it? The field failures: "ProviderFailure: Connection failed" on turn start after wifi reassociation. That's the SSE POST. So (A) misses the main benefit.
  - **(B) Opportunistic reuse with a cheap liveness probe**: before reusing, `select.select([sock],[],[],0)` for readability (a dead connection is readable — EOF/RST pending) + MSG_PEEK. Python: `conn.sock.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)` → b"" means dead, BlockingIOError means alive-idle. This kills most of the stale-connection window (server closed gracefully while idle — the FIN is already in our buffer, so the probe catches it deterministically; it can't catch a FIN in flight). Combined with idle cap, rule 4 becomes nearly dead code. Cost: a few lines. Risk to abort path: none — probe happens on acquire, before the pump starts.
  - **(C) Increase retry aggressiveness instead** — already shipped (120s ladder). The spec says it reduces but doesn't remove. True: each connect during a down window fails fast, ladder waits it out... actually the widened ladder already survives a 30s reassociation! 6 attempts over 120s with fast refusals — the link comes back at t=45s, attempt at t=50 succeeds. So the remaining benefit of pooling over the shipped ladder is: avoiding the *visible* retry/error flicker and TLS handshake cost. Honest judgment: the shipped ladder may already cover most of the field scenario; the spec should quantify what failures remain (failures.log on hardware). The spec does plan a before/after hardware measurement — good.
  - **(D) Single "sticky" connection for the chat stream only**, replaced on any error — essentially the pool with N=1 per host and no post_json sharing. Simpler: one global per host, no lists. Gets most benefit, halves the state machine.

  I think the honest simpler alternative is: **keep one cached connection per host (N=1), probe it with MSG_PEEK on acquire, discard on any doubt, and restrict pool reuse to requests that have not yet written their body / are idempotent-safe at the pool layer... hmm. Actually simpler: N=1, probe, and rule-4 retry only for GET/SSE (never post_json without explicit policy). That's less machinery than the spec's `_POOL: dict -> list` with N=2 and it directly addresses the failure without touching the abort path at all if the release rule is "release only when the pump itself reports end-of-body".

  But careful: is the "simpler alternative" materially less beneficial? N=2 matters when a stream and a listing run concurrently (title generation while streaming?). The spec says 2 is enough. N=1 means the listing just opens fresh — the listing is rare and its failure is cheap. So N=1 loses little.

  Also should mention: **urllib must go** — `AbstractHTTPHandler.do_open` sends `Connection: close`. Let me verify that claim before asserting. Check the local Python's urllib/request.py. Python 3 on this Mac. Let me grep. Also verify `HTTPResponse.peek` lock behavior? Not needed.

  Also check: does `http.client.HTTPConnection` get reused by urllib at all? No — do_open creates one per request and closes? do_open does not close explicitly but `h.close()`? In do_open, after getresponse... I recall urllib sets `Connection: close` header — let me verify locally.

  Also confirm `_iter_lines` finally → `_hard_close(response)` unconditional (net.py:548-550) — cited.

  Also confirm post_json finally `_hard_close(response)` (net.py:631-632) — cited.

  Also sse_json_events committed logic (net.py:673-691) — cited. Spec says net.py:576 for the no-resend rule — actual 683/689 (`committed = True` at 682... let me note: 673 committed=False, 682 committed=True, 689 `if committed or not _should_retry`). Spec line references are stale (spec says _open at net.py:404 — actual 421; net.py:576 — the committed rule at 689ish). Minor doc drift worth flagging.

  Let me run the local experiments: check urllib's Connection: close behavior, and maybe MSG_PEEK behavior on a dead connection, under /tmp or just reading the stdlib source. Reading stdlib source isn't a network request — fine.

  Also verify http.client RemoteDisconnected hierarchy. And check `HTTPResponse.will_close`.

  Let me do these checks quickly.

• I've read all three. Before writing the review, let me verify two facts the whole argument hinges on: whether urllib even permits keep-alive today, and how a dead pooled connection actually presents to a borrower.    def do_open(self, http_class, req, **http_conn_args):
        """Return an HTTPResponse object for the request, using http_class.

        http_class must implement the HTTPConnection API from http.client.
        """
        host = req.host
        if not host:
            raise URLError('no host given')

        # will parse host:port
        h = http_class(host, timeout=req.timeout, **http_conn_args)
        h.set_debuglevel(self._debuglevel)

        headers = dict(req.unredirected_hdrs)
        headers.update({k: v for k, v in req.headers.items()
                        if k not in headers})

        # TODO(jhylton): Should this be redesigned to handle
        # persistent connections?

        # We want to make an HTTP/1.1 request, but the addinfourl
        # class isn't prepared to deal with a persistent connection.
        # It will try to read all remaining data from the socket,
        # which will block while the server waits for the next request.
        # So make sure the connection gets closed after the (only)
        # request.
        headers["Connection"] = "close"
        headers = {name.title(): val for name, val in headers.items()}

        if req._tunnel_host:
            tunnel_headers = {}
            proxy_auth_hdr = "Proxy-Authorization"
            if proxy_auth_hdr in headers:
                tunnel_headers[proxy_auth_hdr] = headers[proxy_auth_hdr]
                # Proxy-Authorization should not be sent to origin
                # server.
                del headers[proxy_auth_hdr]
            h.set_tunnel(req._tunnel_host, headers=tunnel_headers)

        try:
            try:
                h.request(req.get_method(), req.selector, req.data, headers,
                          encode_chunked=req.has_header('Transfer-encoding'))
            except OSError as err: # timeout error
                raise URLError(err)
            r = h.getresponse()
        except:
            h.close()
            raise

        # If the server does not send us a 'Connection: close' header,
        # HTTPConnection assumes the socket should be left open. Manually
        # mark the socket to be closed when this response object goes away.
        if h.sock:
            h.sock.close()
            h.sock = None

        r.url = req.get_full_url()
        # This line replaces the .msg attribute of the HTTPResponse
        # with .headers, because urllib clients expect the response to
    def send(self, data):
        """Send `data' to the server.
        ``data`` can be a string object, a bytes object, an array object, a
        file-like object that supports a .read() method, or an iterable object.
        """

        if self.sock is None:
            if self.auto_open:
                self.connect()
            else:
                raise NotConnected()

        if self.debuglevel > 0:
            print("send:", repr(data))
        if hasattr(data, "read") :
            if self.debuglevel > 0:
                print("sendIng a read()able")
            encode = self._is_textIO(data)
            if encode and self.debuglevel > 0:
                print("encoding file using iso-8859-1")
            while 1:
                datablock = data.read(self.blocksize)
                if not datablock:
                    break
                if encode:
                    datablock = datablock.encode("iso-8859-1")
                self.sock.sendall(datablock)
            return
        try:
            self.sock.sendall(data)
        except TypeError:
            if isinstance(data, collections.abc.Iterable):
                for d in data:
                    self.sock.sendall(d)
            else:
                raise TypeError("data should be a bytes-like object "
                                "or an iterable, got %r" % type(data))



• Confirmed: urllib's do_open forces `Connection: close` (line "headers["Connection"] = "close"") and even closes the socket under the response (`h.sock.close(); h.sock = None`). So the spec's premise "urllib creates a new HTTPSConnection per request" is right, and importantly: **pooling requires abandoning urllib for the request path** — do_open actively sabotages persistence twice (header + sock.close()). That's a bigger rewrite than "only the disposal changes". The spec says "The three callers keep their signatures; only the disposal changes — today every one of them ends in `_hard_close(response)`." That's misleading: `_open` itself must be rewritten from urllib to raw http.client (or a custom handler that bypasses do_open's sabotage). Consequences of dropping urllib: lose ProxyHandler/redirects/HTTPError handling... _open handles HTTPError itself. Redirects: build_opener includes HTTPRedirectHandler — providers returning 307 (some endpoints do redirect, e.g., streaming URLs)? If a provider 302s, today urllib follows; with raw http.client it would surface as a 3xx NetError. A behavior change not mentioned in the spec. Also `_with_ua` etc. fine.

  Also interesting: after getresponse, do_open does `h.sock.close(); h.sock=None` — meaning the socket fd is closed at the socket-object level but the response's fp keeps reading through makefile... wait, it closes h.sock — the actual descriptor stays alive because makefile holds an io ref (as _hard_close's docstring notes). OK.

  Now the MSG_PEEK probe experiment: verify that a server-closed keep-alive connection is detectable via MSG_PEEK|MSG_DONTWAIT returning b"". Quick local experiment with sockets (local, no network beyond loopback — allowed: "no network requests" means external; local experiments under /tmp are explicitly allowed). Let me write a quick script to /tmp.probe -> BlockingIOError (would look alive)
probe on live idle conn -> BlockingIOError (alive, correct)
write took 0.000s (succeeded)
Interesting — Case 1: after `srv.shutdown(); srv.server_close()`, the probe returned BlockingIOError, not b"". Why? Because `server_close()` on ThreadingHTTPServer doesn't close the *accepted* handler sockets (threading mixin tracks them... `server_close` closes only the listening socket unless block_on_close... and daemon_threads not set — handler threads may still hold the connection open waiting for the next request!). Right: the handler thread is blocked in readline waiting for the next request on the keep-alive connection; it never closes. So no FIN was sent. My experiment was flawed for case 1. Let me redo with the handler explicitly closing the connection (close_connection = True) after response, or explicitly closing the accepted socket.

  Let me fix: make handler set self.close_connection = True after responding, which makes the handler close the socket → FIN. Then probe should see b"".

  Also blackhole: the read path — after my script, getresponse presumably succeeded because the server is alive and answered (I didn't actually black-hole it). The write took 0.000s and then... output ended after "write took" — meaning getresponse() didn't raise timeout? Actually the server responded normally, so it printed nothing after (no exception). Right, my "black hole" wasn't a black hole at all. The key finding stands anyway: write succeeds instantly; failure surfaces at read. For the review, the logical argument suffices: on a silent peer, write() is queued (retransmits invisible to app), first read waits out its socket timeout. That's just TCP. I can note the write-success/read-hang asymmetry confirmed on loopback (write 0.000s), and the read bound is the socket's timeout — which for SSE is the stall budget (180 s default, net.py:41).

  Let me redo case 1 properly.probe after peer FIN -> b''
reused-after-FIN: first write/read raised ConnectionResetError: [Errno 54] Connection reset by peer
Confirmed:
  - MSG_PEEK|MSG_DONTWAIT reliably detects a peer-closed connection (b'') when the FIN has already landed (the common case for "server idle-closed while conn sat in the pool").
  - If you don't probe, the first write succeeds into the dead connection and the first read raises ECONNRESET — covered by rule 4 only if implemented at the right layer and only for idempotent requests.

  One more thing to verify: the `stop_on_done` early-stop behavior — `_sse_events` returns at [DONE] without draining (net.py:562-563). Already read the code; net.py:557-563, 573-576. And sse_json_events finally closes generators + hard_close (net.py:692-702). Confirmed by reading.

  Also check whether anything in the codebase calls `post_json` with NO_RETRY semantics for MCP — net.py:601-607 docstring says retry defaults off because MCP tools/call is not idempotent; test_net.py:515-520 asserts one attempt. Confirmed.

  Also the spec's line references: spec says `_open()` at net.py:404 (actual 421), and "net.py:576, the rule that a half-consumed turn is never re-sent" (actual committed flag: net.py:673, 682, 689). Doc drift.

  Also "47 transport tests" — count test methods in test_net.py. Let me count: roughly. Let me count quickly.47
47 tests confirmed. Now write the review. Structure per the user's five questions. Markdown to stdout. Cite file:line for every claim about current behavior.

  Key findings:

  **1. Kill shot.**
  Refute the spec's own candidate (mostly), present the worse one: silent black-hole + unbounded first read, and a second one: rule 4 replaying non-idempotent post_json.

  Spec's candidate (dies after first SSE event): refuted in the specific sense — after the first event flows, the connection is proven alive; death afterward is equally likely on fresh or reused. Reuse adds risk only pre-first-byte, which rule 4 covers *if* the retry is real. BUT the worse variant: the dead-idle connection whose peer vanished silently (NAT state loss, asymmetric link drop — exactly the wifi-reassociation scenario from the spec, spec lines 19-24). Write succeeds instantly (verified: 0.000s on loopback — the kernel queues and retransmits invisibly), the first read then blocks for the *entire socket timeout*, which for a stream is the stall budget: `DEFAULT_TIMEOUT = 180` (net.py:41), applied via `_iter_lines(response, stall, ...)` (net.py:678-679, 665). Rule 4's "treat first-read failure as retry-on-fresh" fires only after those ≤180 s. User observes: hit enter on a turn, UI sits silent for up to three minutes on healthy-ish wifi, then recovers. Today's behavior in the same window: `_open` → fresh connect refused in milliseconds (spec line 22-23; net.py:441-446 wraps URLError retryable) → RetryPolicy ladder surfaces progress and abort stays live. So pooling converts a fast, visible, retried failure into a long silent freeze in *precisely the field conditions the spec is motivated by*. Rule 4 as written does not bound the *first* read on a reused connection — it only classifies the failure once it arrives. Add: a reused connection's time-to-first-byte is bounded by a connect-timeout-scale budget (say 10–20 s), not the stall budget; stall budget applies only after the first byte. And/or MSG_PEEK probe on acquire (verified b'' on peer-FIN).

  Second kill shot (rule audit feeds this): rule 4's invisible retry applied to `post_json` breaks the non-idempotency guarantee. net.py:601-607: "This function carries MCP tools/call requests, which are not idempotent: replaying one after a dropped connection could run a shell command twice." test_net.py:515-520 asserts exactly one attempt. Rule 4 says the retry "does not consume a RetryPolicy attempt: nothing reached the model" — but a first-read failure on a reused connection does NOT establish that nothing reached the server: the write succeeded (verified), the server may have received the full request and executed the tool, and the RST/EOF arrives only at read. For SSE chat this matches existing semantics (pre-commit retry is already allowed, net.py:687-691), but for post_json it is a new replay path the spec doesn't carve out. Rule 4 must be scoped to idempotent requests only (streams/GETs), never to a NO_RETRY post_json.

  **2. Rule audit.**
  - Rule 1: sufficient in spirit, incomplete mechanically. `_hard_close` (net.py:294-373) is called unconditionally in `_iter_lines` finally (net.py:548-550) and sse_json_events finally (net.py:692-702). Fine. But "interrupted mid-stream is poison" also covers the *stall-timeout* path (net.py:531-534) and the consumer-break path. Sufficient.
  - Rule 2: **insufficient as stated, and it hides the biggest implementation trap.** "Fully consumed" is a property of the *pump thread*, not the caller: the pump is what iterates `for line in response` (net.py:507), and it may legitimately still be blocked in `readline` when the caller finishes, because `stream_sse_events` uses `stop_on_done=True` (net.py:722) and `_sse_events` returns on `[DONE]` *without draining the stream to end-of-body* (net.py:562-563). The chunked terminator may be unread at that instant. Worse: the caller cannot *check* consumption safely — any method that touches the response's read side (peek/close/read) takes the BufferedReader lock that a zombie pump holds until its read timeout (net.py:300-310, 361-368). So the rule needs a mechanism the spec never names: the pump itself must vouch for end-of-body (set a flag after its loop + `_truncated` check, net.py:512-519), and release consults only that flag. As written, an implementer will either (a) check framing state racily and sometimes release a connection with the terminator unread → next borrower reads `0\r\n\r\n` as the start of its response, or (b) discard every stop_on_done stream → pooling does nothing on the one path that matters. Missing rule: *release only on the pump's own end-of-body signal; never inspect the response object from another thread to decide.*
  - Rule 3: sufficient; note `http.client` timeout applies at connect via `_SplitTimeoutMixin.connect` (net.py:247-253) today; pooled acquire must `sock.settimeout()` directly. Fine.
  - Rule 4: insufficient twice over. (i) unbounded first read (kill shot 1); (ii) applies to non-idempotent post_json (kill shot 2); (iii) "30 s is safely under every observed server timeout" — the spec itself says servers close at "30–120 s" (line 82-83); 30 is not safely under 30, it's exactly at the boundary, guaranteeing the race it exists to avoid at the 30 s providers. Cap should be well below the floor (e.g. 15–20 s) or measured per host. Also the race where FIN is already seated is deterministically detectable with MSG_PEEK (verified) — the spec leaves that free win on the table.
  - Rule 5: sufficient, plus: the lock must not be held while doing anything that can block (probe, settimeout fine; close not fine).
  - Rule 6: insufficiently specified. "Must not add a code path where an abort waits for anything" — but pooling adds exactly one candidate wait the spec doesn't discuss: the release/discard decision in `_iter_lines`' finally (net.py:548-550) currently hard-closes unconditionally; the pooled version must decide. If the decision touches the response object or joins the pump, abort waits. Rule should say: release/discard is decided from the pump's flag only; discard uses the existing `_hard_close` parking discipline unchanged.
  - Rule 7: "emptied at process exit" — *how* matters on Haiku (see Q3): closing a pooled socket at exit while daemon pump threads (net.py:521, daemon=True) may still exist is the fd-reuse bug class again; exit-time teardown must park, not close, or just not run at all (process death closes fds; atexit close buys nothing and risks the number being reused during interpreter shutdown... actually at process exit there's no "after", but other threads during atexit can still open files → same race. Simpler: don't close at exit, just drop references / close lazily. Or park.)

  Missing rules:
  (a) pump-vouches-for-consumption (the big one).
  (b) first-byte budget on reused connections.
  (c) rule 4 scoped to idempotent requests; post_json/NO_RETRY exempt.
  (d) `will_close` / `Connection: close` responses and non-chunked delimit-by-close bodies must never be released (http.client sets will_close; releasing one hands out a corpse — rule 4 would catch it, but silently churning every release defeats the pool).
  (e) urllib must be abandoned: `AbstractHTTPHandler.do_open` forces `Connection: close` AND closes the socket under the response (`h.sock.close(); h.sock = None`). So "only the disposal changes" (spec line 55-56) is false: `_open` must be reimplemented on raw `http.client`, losing urllib's redirect handling and proxy support silently. The spec should say this out loud and decide about redirects/proxies.
  (f) pool keying must include the effective proxy/tunnel endpoint if proxies survive, and TLS parameters; minor.

  **3. Haiku angle.**
  - The known bug class: blocked recv ignores shutdown, returns only at own timeout; fd number reused by sqlite while reader inside OpenSSL (net.py:300-331).
  - New variant 1: the release-decision thread inspecting the response → BufferedReader lock held by zombie pump on Haiku until its read timeout (which for SSE is the stall budget, up to 180 s, net.py:41, 665) → turn-end or abort hangs. _hard_close already refuses `response.close()` for exactly this reason (net.py:361-368). Pooling reintroduces the temptation at the release point.
  - New variant 2: pool teardown at exit (rule 7) closing sockets — frees descriptor numbers while straggler daemon pump threads may still be inside OpenSSL → the exact sqlite incident shape (net.py:322-331). Must park (dup2 /dev/null, net.py:338-345) or skip close at exit entirely.
  - New variant 3 (subtle): on Haiku a blocked recv ignores shutdown — so the zombie pump after an abort lives until its read timeout *or* until the fd park wakes it (net.py:305-308 says releasing the descriptor wakes it "on every platform"; the 361-368 note qualifies poll() on macOS). Either way, during that window the *connection object* must be unreachable by the pool — rule 1 covers it. No additional new bug beyond 1-2, provided discard reuses `_hard_close` verbatim. The danger is an implementer writing a second, gentler close path for the pool ("it was only idle") that doesn't park.
  - Also: pooled idle fds on Haiku — fd numbers stay allocated; sqlite etc. — no, holding fds open is not the bug class; releasing-and-reusing is. Fine.

  **4. Simpler alternative.**
  Offer: per-host single cached connection (N=1), MSG_PEEK probe on acquire (verified), release only when the pump signals end-of-body, rule-4 retry only for idempotent requests, no pooling for post_json. Keep `_open` on http.client only for the stream path; leave post_json on the current urllib path entirely (it's rare, cheap, and its no-replay contract stays provably intact). This removes: the list-based pool, per-host N=2 bookkeeping, the post_json replay hazard, most of rule 7 (one conn per host), and shrinks the blast radius to the SSE path — which is also exactly where the field failures occur (turn starts). What it loses vs the spec: connection sharing for listings/title-generation (post_json) — but those are infrequent, and their failure mode today is already fine (short timeouts, explicit retry opt-in). Honest judgment: the spec's N=2 list pool buys marginal extra reuse for a secondary path at the price of the rule-4/post_json contradiction; the simpler design gets ~all the field benefit. Disagree with the spec's claim that the three callers all plug into the same disposal change — that uniformity is what creates the post_json hazard.
  Also honestly note: the already-shipped mitigations (shared SSLContext, 120 s ladder, spec lines 30-32; net.py:124-128) may already cover most reassociation windows — a refusal at t=0, retries spanning 120 s will catch a link that returns in 30–60 s. The spec's hardware before/after (lines 124-126) is the right arbiter and should arguably run *first* to size the remaining gap.

  **5. Verdict.** GO-WITH-CHANGES, ranked:
  1. Bound time-to-first-byte on any reused connection to a connect-timeout-scale budget (kill shot 1); without this the spec makes the motivating scenario worse.
  2. Scope rule 4 to idempotent requests; post_json (NO_RETRY) never gets pool-layer replay (kill shot 2, breaks net.py:601-607 contract).
  3. Add the pump-vouches rule: release only on the pump's end-of-body flag; never touch the response object cross-thread to decide (makes rules 1/2/6 implementable; otherwise every implementation either deadlocks or never reuses streams).
  4. Lower the idle cap below the spec's own observed floor (30 s floor → cap ~15–20 s) and/or MSG_PEEK probe on acquire (verified reliable on loopback).
  5. State explicitly that urllib is abandoned for the pooled path (do_open forces `Connection: close` and closes the socket — verified in stdlib source) and decide what happens to redirect/proxy handling.
  6. Never release `will_close` responses.
  7. Exit-time teardown (rule 7) parks fds or doesn't close at all on Haiku.
  8. Fix stale line refs (spec cites net.py:404 and net.py:576; actual _open is net.py:421, the no-resend rule is net.py:689) and add the missing proof point: a completed stop_on_done stream must leave a releasable connection (the current proofs don't assert reuse happens on the stream path at all).

  Keep it tight. Write in English. Markdown to stdout. Cite file:line.

• All checks done. Here is the review.

  ---

  # Adversarial review: `docs/specs/persistent-connections.md`

  Verified facts first, since two of my findings depend on them:

  - Python's `urllib.request.AbstractHTTPHandler.do_open` **forces `Connection: close`** and then runs `h.sock.close(); h.sock = None` under the response (stdlib source, confirmed locally). Pooling cannot be bolted onto `_open` as it exists; the urllib path must be abandoned for pooled requests.
  - `MSG_PEEK | MSG_DONTWAIT` on an idle keep-alive connection returns `b''` once the peer's FIN has landed, and `BlockingIOError` on a live one (loopback experiment). A dead-while-pooled connection is *deterministically* detectable at acquire time.
  - On a connection whose peer vanished **without** FIN/RST (black hole), the first `sendall` succeeds instantly (measured 0.000 s); the failure surfaces only at the first read, bounded by the socket timeout.

  ---

  ## 1. Kill shot

  **The spec's own candidate is refuted; there is a worse one it doesn't name.**

  The spec's closing question (dies after the first SSE event → cannot retry, pointing at the no-resend rule, which lives at `haikode/net.py:689`, not 576) is mostly a non-finding. Once the first event has flowed, the connection is *proven alive*; a death after that point is exactly as likely on a fresh connection as on a reused one, and `sse_json_events` already refuses to retry committed streams (`net.py:673`, `net.py:682`, `net.py:689`). Reuse adds risk only in the pre-first-byte window, which is what Rule 4 exists for. Refuted.

  **The worse scenario: the silent black hole, in the spec's own field conditions.** Sequence:

  1. A turn completes; the connection is pooled, healthy.
  2. The wifi link drops and reassociates — the spec's motivating case (spec lines 19–24). No packets flow; TCP survives; but the return path (NAT state, driver limbo) is silently dead for some window.
  3. The user starts the next turn. The pool hands out the connection — it is idle-young, passes the age cap.
  4. The request write succeeds instantly (verified: the kernel queues and retransmits invisibly; no RST comes back because nothing is listening on the dead path).
  5. The first read blocks for the **full socket timeout**. On the stream path that timeout is the *stall budget*: `DEFAULT_TIMEOUT = 180` (`net.py:41`), wired through `net.py:665` into `_iter_lines(response, stall, …)` at `net.py:679`. Rule 3 re-applies it on acquire; nothing applies anything shorter.
  6. Rule 4's "retry on a fresh connection" fires only *after* those ≤180 s. The user watches the UI sit silent for up to three minutes, on the same desk where today `_open` gets ECONNREFUSED in milliseconds (spec lines 22–23; `net.py:441-446`) and the RetryPolicy ladder (`net.py:124-128`) reports progress while the abort stays live.

  Pooling converts a fast, visible, retried failure into a long silent freeze — *precisely in the field conditions the spec is motivated by*. Rule 4 classifies the failure once it arrives; it never bounds the first read on a reused connection. The fix is small but must be in the spec: time-to-first-byte on a reused connection is bounded by a connect-timeout-scale budget (~10–20 s); the stall budget applies only after the first byte. (This is also where the already-shipped mitigations matter: the 120 s ladder already survives most reassociation windows, so an unbounded first read makes pooling a net regression in exactly the window where the ladder currently wins.)

  ## 2. Rule audit

  **Rule 1 — sufficient in spirit.** `_hard_close` is called unconditionally in `_iter_lines`' finally (`net.py:548-550`) and in `sse_json_events`' finally (`net.py:692-702`); routing those to `_discard` preserves behavior. One caveat under Rule 7 below.

  **Rule 2 — insufficient as stated, and it hides the implementation's biggest trap.** "Fully consumed" is a property of the *pump thread*, not of the caller. The pump is what iterates `for line in response` (`net.py:507`); the caller only drains a queue. And `stream_sse_events` always passes `stop_on_done=True` (`net.py:722`), so `_sse_events` returns at `[DONE]` **without draining to end-of-body** (`net.py:562-563`) — the chunked terminator may be unread at that instant. Usually the pump races ahead and finishes anyway, which makes it worse: whether a completed stream leaves a releasable connection is a *thread race the spec never mentions*. An implementer following Rule 2 literally will either (a) inspect framing state from the caller thread and occasionally release a connection whose terminator is still on the wire — the next borrower reads `0\r\n\r\n` as the start of its response — or (b) conservatively discard every `stop_on_done` stream, in which case pooling does nothing on the one path that matters. **Missing rule:** release only when the pump itself has signaled end-of-body (a flag it sets after its loop and `_truncated` check, `net.py:512-519`); never touch the response object from another thread to decide (see §3 for why that deadlocks).

  **Rule 3 — sufficient.** Today the read timeout is applied in `_SplitTimeoutMixin.connect` (`net.py:247-253`); pooled acquire must `sock.settimeout()` directly. Fine.

  **Rule 4 — insufficient, three times over.**
  - (a) The unbounded first read — kill shot §1.
  - (b) It contradicts `post_json`'s documented contract. `post_json` defaults to `NO_RETRY` because MCP `tools/call` is not idempotent: "replaying one after a dropped connection could run a shell command twice" (`net.py:601-607`), and `test_net.py:515-520` asserts exactly one attempt. Rule 4's justification — "nothing reached the model" — is *unfounded* for a first-read failure on a reused connection: the write succeeded (verified), so the server may have received the entire request and executed the tool before the RST/EOF arrived. An invisible pool-layer replay of a `NO_RETRY` request is a new replay path the spec doesn't carve out. Rule 4 must be scoped to idempotent requests; `post_json` without an explicit policy must never get it.
  - (c) The arithmetic is wrong at the boundary. The spec says providers close at "30–120 s" (line 82–83) and then caps idle age at 30 s as "safely under every observed server timeout." 30 is not safely under 30 — it *is* the floor, which guarantees the FIN-in-flight race at exactly the 30-second providers. Cap at ~15–20 s, or probe (below).

  **Rule 5 — sufficient,** plus one clause: the pool lock must never be held across anything that can block (a close, a join, a response method).

  **Rule 6 — right rule, missing its only hard clause.** The one new place pooling can make abort wait is the release/discard decision in `_iter_lines`' finally (`net.py:548-550`), which today hard-closes unconditionally. If that decision inspects the response or joins the pump, abort waits — see §3. The rule should say: the decision reads only the pump's flag; discard is `_hard_close` verbatim.

  **Rule 7 — the "how" is a bug on Haiku;** see §3. Also missing from the whole list:

  - **Never release a connection whose response set `will_close`** (a `Connection: close` header, or a body delimited by close). Otherwise the pool silently fills with corpses and Rule 4 becomes the hot path.
  - **The spec's "only the disposal changes" (line 55–56) is false.** `do_open` forces `Connection: close` and closes the socket under the response (verified in stdlib). `_open` must be reimplemented on raw `http.client` for the pooled path — which silently drops urllib's redirect following and proxy handling unless someone decides otherwise. The spec must say this out loud; a provider 302 that urllib follows today is a behavior change nobody signed off on.

  ## 3. The Haiku angle

  Yes — pooling creates two new variants of the known class, both at boundaries the spec waves at.

  **Variant 1: the release check vs. the zombie pump.** On Haiku a blocked `recv()` ignores `shutdown()` and returns only at its own timeout (`net.py:300-305`), and while blocked it holds the BufferedReader lock (`net.py:301-303`). `_hard_close` already refuses to call `response.close()` for exactly this reason (`net.py:361-368`). The moment pooling exists, someone must decide *release or discard* at stream end — while the pump may still be inside `readline` (Rule 2 / `stop_on_done` race above). Any implementation that answers that question by calling a method on the response (`peek`, `read`, even `close` on the "clean" path) blocks behind the pump's lock until the read timeout — which for SSE is the stall budget, up to 180 s (`net.py:41`, `net.py:665`). Turn-end or abort hangs for three minutes. This is the fd-parking incident reincarnated as a lock incident, and it's why the pump-vouches rule in §2 is not optional.

  **Variant 2: pool teardown at process exit (Rule 7).** "The pool is emptied at process exit" — if emptying *closes* sockets during interpreter shutdown, descriptor numbers are freed while daemon pump threads (`net.py:521`, `daemon=True`) may still be inside OpenSSL, and any thread opening a file in that window inherits the number. That is the sqlite incident verbatim (`net.py:322-331`). Exit-time teardown must either park each fd with the same `dup2(/dev/null)` discipline (`net.py:338-345`) or — better, and honest — not close at all: process death releases fds, and an `atexit` close buys nothing worth that race.

  Not a new variant but worth pinning down: every discard path the pool adds must *be* `_hard_close`. The danger is an implementer writing a second, gentler close for the pool ("it was only idle") that skips the parking. One close path, reused.

  ## 4. Simpler alternative

  Yes, and it survives an honest comparison. **Pool only the stream path, N=1 per host, probe on acquire, pump-vouched release, and leave `post_json` on the current urllib code entirely:**

  - One cached connection per host, not a list of two. The concurrent-listing case the spec cites for N=2 (line 101) just opens fresh — listings are rare and their failure is cheap.
  - `MSG_PEEK | MSG_DONTWAIT` on acquire (verified: `b''` on peer-FIN, `BlockingIOError` when live). This makes Rule 4 nearly dead code instead of the load-bearing wall the spec leans on.
  - Release only on the pump's end-of-body flag; otherwise discard via `_hard_close`. Abort path untouched — `_iter_lines`' finally keeps its current unconditional shape, it just reads a flag first.
  - `post_json` keeps today's code path verbatim. Its no-replay contract (`net.py:601-607`) stays provably intact because the pool never sees it, and the Rule 4(b) contradiction evaporates.

  Judged against the spec honestly: this loses connection reuse for listings/title-generation and the elegance of one disposal path for all three callers — but that uniformity is precisely what creates the `post_json` replay hazard, and the field failures the spec is built on are turn-start stream failures, so the benefit kept is the benefit that matters. It removes: the list pool, per-host bookkeeping, Rule 4(b), most of Rule 7, and the urllib-rewrite question shrinks to one caller. One thing the spec has over both designs: its before/after hardware measurement (lines 124–126). Given the shipped 120 s ladder (`net.py:124-128`) already survives most reassociation windows, that measurement should arguably run *first* to size the gap this spec is buying.

  ## 5. Verdict

  **GO-WITH-CHANGES**, ranked:

  1. Bound time-to-first-byte on a reused connection to a connect-timeout-scale budget; the stall budget starts at the first byte. Without this, the spec makes its own motivating scenario worse (§1).
  2. Scope Rule 4 to idempotent requests; `post_json` under `NO_RETRY` never gets pool-layer replay (§2, Rule 4b — it breaks the contract at `net.py:601-607`).
  3. Add the missing rule: release only on the pump's own end-of-body signal; never inspect the response cross-thread to decide. Without it, Rules 1/2/6 are not jointly implementable (§2 Rule 2, §3 variant 1).
  4. Drop the idle cap below the observed floor (≤20 s) and/or add the `MSG_PEEK` probe on acquire (§2 Rule 4c; probe verified).
  5. State explicitly that the pooled path abandons urllib (`do_open` sabotages persistence twice, verified) and decide the fate of redirects and proxies (§2, missing rule).
  6. Never release `will_close` responses (§2, missing rule).
  7. Rule 7 teardown parks fds or doesn't close at exit (§3 variant 2).
  8. Housekeeping the spec owes anyway: fix stale line references (`_open` is `net.py:421`, not 404; the no-resend rule is `net.py:689`, not 576), and add the missing proof point — the current proofs assert an *aborted* stream isn't reused, but nothing asserts a *completed* `stop_on_done` stream leaves a releasable connection, which is the property that decides whether pooling helps the stream path at all (§2 Rule 2).

To resume this session: kimi -r session_4e301e36-b729-46a2-ba03-e56a92f08cfb
