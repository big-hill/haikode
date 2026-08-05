# Spec: persistent provider connections (v2)

Status: proposed, revised after adversarial review. Target:
`haikode/net.py`. Issue: #42.

v1 was reviewed by Kimi K3 at max effort against the actual transport
code. The review refuted v1's own stated risk, found a worse one, and
found a contract violation v1 had introduced. Both are now load-bearing
parts of this design; every claim below carries a line reference that was
checked, not remembered.

## The problem, measured not assumed

haikode opens a fresh DNS + TCP + TLS connection for **every** request:
`_open()` (net.py:421) builds an opener per call, and urllib's HTTPS
handler creates a new `HTTPSConnection` per request. No pooling, no
keep-alive.

The vendor clients this project is compared against (codex CLI, the
ChatGPT desktop app) hold **one HTTP/2 connection open across turns**.
The difference decides who survives a flaky link:

* An established, idle TCP connection **survives** a wifi
  down/reassociation window — no packets need to flow while it is gone.
* Every **new connect** in that window fails immediately, in
  milliseconds: ECONNREFUSED / EHOSTUNREACH.

Two independent field investigations (2026-08-04 shredder64, 2026-08-05
haikubook1) reached this from opposite directions.

Already shipped, and NOT this spec: the shared `SSLContext` and the
widened retry ladder — 6 attempts over 120 s (net.py:124-128). They
reduce exposure without removing it, **and they raise the bar this spec
must clear** (see "Gate" below).

## Gate: measure before building

The 120 s ladder already survives most reassociation windows. Before any
of this is written, run the same work on the Haiku laptop with the flaky
driver and count `failures.log` entries over a session. If the ladder
alone has closed the gap, this spec is not worth its risk to the abort
path. The review made this point and it is correct.

## Chosen design: one sticky stream connection per host

v1 proposed a general pool shared by all three callers. The review's
simplest alternative is adopted instead, because the uniformity v1 wanted
is exactly what created its worst defect:

* **`stream_sse_events` (the turn path) reuses one connection per host.**
  This is where the field failures happen, and where the benefit is.
* **`post_json` and `request_json` keep the current code path verbatim,
  fresh connection each time.** Their no-replay contract (net.py:601-607,
  proven by test_net.py:515-520) then stays provably intact: the pool
  never touches them. MCP `tools/call` is not idempotent, and a
  pool-layer retry could run a shell command twice.

At most one idle connection per host is kept. No lists, no per-host
bookkeeping beyond a single slot.

## The rules that must not be broken

Each is a bug this project has already paid for, or one the review found
before we could.

1. **An aborted connection is never reused.** `_hard_close()` destroys
   the socket and parks its descriptor on /dev/null (net.py:294-368)
   because a reader may still be inside OpenSSL. Such a connection is
   discarded, never released. Its next user would otherwise read the tail
   of somebody else's answer.

2. **Release only on the pump thread's own end-of-body signal.** This is
   the review's most valuable rule and v1 did not have it. "Fully
   consumed" is a property of the pump (net.py:507), not of the caller,
   which only drains a queue. `stream_sse_events` always passes
   `stop_on_done=True` (net.py:722), so `_sse_events` returns at `[DONE]`
   **without draining to end-of-body** (net.py:562-563): the chunked
   terminator may still be on the wire. Whether a completed stream leaves
   a releasable connection is therefore a *thread race*. The pump sets a
   flag after its loop and its truncation check (net.py:512-519); the
   connection is released only if that flag is set. Nothing else may
   inspect the response object from another thread to decide — on Haiku
   that is how a caller ends up blocked behind the BufferedReader lock
   for the whole read timeout (net.py:361-368).

3. **Time to first byte on a reused connection is bounded by a
   connect-scale budget (10–20 s), not the stall budget.** The review's
   kill shot, and it is decisive. When the link dies during the idle
   window, the write to a pooled connection **succeeds instantly** (the
   kernel queues and retransmits; no RST comes back), and the first read
   then blocks for the full stall budget — `DEFAULT_TIMEOUT = 180`
   (net.py:41), wired through net.py:665 into `_iter_lines` at
   net.py:679. Rule 4 would classify that failure only after three
   minutes of silence. Fresh-connect haikode gets ECONNREFUSED in
   milliseconds and shows the retry ladder working. **Without this rule,
   pooling makes the motivating scenario worse, not better.** The stall
   budget applies only after the first byte has arrived.

4. **A pre-first-byte failure on a reused connection retries once on a
   fresh connection — for the stream path only.** Nothing was delivered,
   so the no-resend rule (net.py:673, net.py:682, net.py:689) is not
   engaged. This retry does not consume a `RetryPolicy` attempt. It
   applies to nothing else: see the design decision above.

5. **Idle connections die young.** Providers close idle keep-alive
   connections after 30–120 s, so a 30 s cap sits exactly on the floor
   and guarantees the FIN-in-flight race at the fastest providers. Cap at
   **15 s**, and probe the socket with `MSG_PEEK | MSG_DONTWAIT` on
   acquire: a peer that has closed returns `b""` and the connection is
   discarded before it is ever used.

6. **Never release a response whose `will_close` is set.** The server
   asked for the connection to end; honouring keep-alive against its
   wishes desynchronises the next request.

7. **Thread safety, without blocking under the lock.** Subagents run on
   other threads with their own clients. Every mutation of the slot takes
   the lock; a handed-out connection is owned exclusively until released
   or discarded; and the lock is never held across a close, a join, or
   any response method.

8. **Cancellation stays instant, and teardown parks rather than closes.**
   The abort path must gain no code path that waits. At process exit,
   idle pooled connections are disposed through the same descriptor
   parking `_hard_close` uses — a plain close at exit re-opens the
   fd-reuse window that once wrote TLS bytes into a database.

9. **The pooled path abandons urllib.** `AbstractHTTPHandler.do_open`
   sabotages persistence twice (it sets `Connection: close` and wraps the
   response so the connection cannot be handed back), so the stream path
   moves to `http.client` directly. Redirects and proxy support come from
   urllib today; the stream path uses neither against provider endpoints,
   and that must be stated in code rather than discovered later.

## Explicitly out of scope

HTTP/2 (no stdlib client, and this project takes no dependencies),
pre-warming, health-check pings, and any change to provider code, retry
semantics or the abort contract.

## How it will be proven

The local server harness in `tests/test_net.py` already counts requests;
it gains a count of *distinct accepted connections*.

* Two sequential streams produce **one** accepted connection.
* An aborted stream, then a request: **two** — nothing reusable survives
  an abort (rule 1).
* A completed `stop_on_done` stream leaves a **releasable** connection.
  The review pointed out that today's tests prove only the negative; this
  is the property that decides whether pooling helps the stream path at
  all (rule 2).
* The server closes the connection between requests: the client recovers
  transparently, consuming no `RetryPolicy` attempt (rules 4, 5).
* A reused connection whose peer never answers fails within the
  connect-scale budget, not the stall budget (rule 3) — the kill shot,
  as an executable test.
* `post_json` still makes exactly one attempt under `NO_RETRY`
  (test_net.py:515-520 unchanged, and a new test that it never touches
  the pool).
* All 47 existing transport tests pass unchanged. They encode the three
  hard-won facts at the top of net.py; a pooling change that needs any of
  them weakened is wrong.
* On hardware, before and after, the measurement from the Gate section.
