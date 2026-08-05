# Spec: persistent provider connections (v3)

Status: proposed, twice reviewed. Target: `haikode/net.py` and
`haikode/providers/subscription.py`. Issue: #42.

v1 was reviewed by Kimi K3 (max effort); v2 folded that in and was
reviewed by Fable (see `persistent-connections-review-2.md`), which
falsified three of the first review's conclusions by experiment and
found that v2's own rules quietly precluded its headline benefit. Both
reviews are archived beside this file. The experiments referenced as
E1/E2/E3 were independently reproduced before v3 was written.

## What actually failed in the field — and the one-line fix that shipped

The refusals were attributed to fresh connects during a wifi
reassociation window. Partly true — but the second review found that
`net.py` classified **every** `socket.gaierror` as fatal, including
`EAI_AGAIN`, the resolver's own "try again". During a link-down window
DNS fails first, so the retry ladder never engaged on exactly the
failure it was built for: the turn died on the first DNS miss.

That is fixed and shipped (EAI_AGAIN is retryable; EAI_NONAME — a typo
in a base_url — stays fatal). **The measurement gate below must run
AFTER this fix**, because it may have been the dominant field mode, at
1/100th the risk of a connection pool.

## The problem, honestly bounded

haikode opens a fresh DNS + TCP + TLS connection per request; vendor
clients hold one HTTP/2 connection open across turns. But the vendor
comparison must not oversell what HTTP/1.1 pooling can deliver: vendors
keep cross-turn connections alive with HTTP/2 PING keepalives, which a
no-dependency stdlib client cannot do. An idle HTTP/1.1 connection must
die young (rule 5) because providers idle-close at 30–120 s — so a
connection that sat through a "tens of seconds" reassociation window is
*over the idle cap and discarded at acquire anyway*.

What pooling CAN deliver:

* survival of **short flaps** (under the idle cap) that land inside an
  active tool loop — where requests are seconds apart, which is exactly
  where a coding agent spends its time;
* handshake latency and CPU savings on every reused request;
* TLS session resumption working with, not against, the transport.

What it CANNOT deliver: cross-turn survival of long idle gaps. That was
v2's headline and it was wrong. Nobody gets to reintroduce it without
bringing HTTP/2.

## Gate: measure the right things, after the DNS fix

A single organic session has no statistical power. The gate is a
scripted measurement on the Haiku laptop with the flaky driver,
with induced link flaps (down/up cycles at varied lengths), recording:

1. **Failure-mode classification**, not just counts: for each
   `failures.log` entry, was it DNS (`EAI_AGAIN` — should now retry),
   connect-refused, or mid-stream? Pooling only addresses the middle
   class.
2. **Turn-start latency distribution** around flaps — the ladder's
   "success" is a silent 10–120 s stall, which is the actual UX gap
   versus vendor clients; zero failures does not mean zero problem.
3. **Inter-request gap distribution at failure time**: what fraction of
   failures hit within 15 s of the previous request? That fraction is
   the ceiling on what pooling can help. If it is small, stop here.
4. **Broken committed turns** (stream died after first byte, unretryable
   by design): the class pooling could plausibly *worsen* (see review 2
   §2) and the one the layer above cannot heal. It must not rise.

## Chosen design: one sticky stream connection per host

* The reuse hook lives at **`sse_json_events`** (net.py:648) — NOT at
  `stream_sse_events`, which is only a wrapper some providers bypass:
  the ChatGPT subscription provider calls `sse_json_events` directly
  (subscription.py:237, `stop_on_done=False`). Hooked one level too
  high, the flagship provider would get nothing.
* **`post_json` keeps the current code path verbatim.** Its no-replay
  contract (net.py:601-607, proven by test_net.py:515-520) stays
  provably intact because the pool never touches it. (v2 named a
  `request_json` that does not exist; the callers are `post_json` and
  the two SSE entry points.)
* One idle connection per host, one slot, no lists.
* Note the easy case: under `stop_on_done=False` a stream ends at clean
  EOF and the pump provably drains the body — release is trivially
  sound there. `stop_on_done=True` (the Anthropic-dialect path) is the
  racy case rule 2 exists for.

## The rules

Each rule now states its enforcement point. An implementer following
the letter must arrive at the same code as one following the intent.

1. **The release predicate is a single conjunction, stated once:**
   release iff *pump end-of-body flag set* ∧ *consumer exited by clean
   StopIteration (not Aborted, not NetError)* ∧ *response `will_close`
   is false* ∧ *the slot is free*. Anything else is a discard through
   `_hard_close`'s parking discipline. This resolves the v2 rule-1/rule-2
   precedence conflict (pump drained, then user aborted before the
   consumer finished: the abort arm of the conjunction wins — discard).

2. **The pump's end-of-body flag is the only release evidence, and both
   disposal sites must consult it.** The flag is a `threading.Event`
   set by the pump strictly after its last socket touch (after the loop
   and the truncation check, net.py:512-519). The consumer reads it in
   its finally — and there are TWO finallys that today unconditionally
   `_hard_close`: `_iter_lines`' (net.py:548-550) and
   `sse_json_events`' (net.py:692-702). Missing the second one works
   today only by an accident of `http.client` internals (`fp` already
   severed after a drained body) — that accident is not load-bearing in
   the new code. Nothing ever inspects the response object from another
   thread: on Haiku that blocks behind the BufferedReader lock for the
   whole read timeout (net.py:361-368). A released connection may be
   re-acquired while the old pump thread still exists (it is
   socket-quiescent after the flag); that is safe only because of the
   flag-after-last-touch ordering, which is therefore mandatory, not
   stylistic.

3. **Time-to-first-byte on a reused connection is bounded at the status
   line, on the caller thread, before the pump exists.** The first byte
   of the response is the status line, read inside `getresponse()` —
   which runs on the caller thread; the pump is created only after
   `_open` returns (net.py:678-679). So the enforcement is simply
   `sock.settimeout(TTFB)` before `getresponse()`, widened to the stall
   budget after it returns. TTFB is connect-scale (15–20 s). Nothing
   interrupts a pump readline cross-thread — no such mechanism exists,
   and no rule may require one. (The misreading "first SSE event" is
   explicitly dead: by event time the pump owns the socket and the
   budget is the stall budget.) Known cost, accepted: a gateway that
   defers the status line past TTFB (some proxies do) fails fast on a
   reused connection; it then gets a fresh connection with the full
   stall budget via rule 4's *visible* arm.

4. **Retry after a reused-connection failure is split by what the
   failure proves.**
   * *Deterministically dead before any byte* — EOF/RST/refused on the
     write or before the status line: retry once on a fresh connection,
     invisibly (no RetryPolicy attempt consumed), after an explicit
     abort check. The request body is the immutable `bytes` built at
     net.py:664; if anyone ever passes a file-like body, the invisible
     retry is forbidden (a consumed reader would silently re-send
     empty).
   * *TTFB timeout* — proves nothing (black hole? slow gateway?): fail
     the reused connection, then retry on a fresh connection **through
     the visible RetryPolicy ladder** (consumes an attempt, backs off,
     logs). Systematic double-submission must never be invisible.
   * Both arms apply to the SSE path only; `post_json` is out of scope
     by design.

5. **Idle cap 15 s, and a probe that works on TLS.** The cap: providers
   idle-close from ~30 s, and the FIN may be in flight; 15 s keeps
   clear of the floor. Accepting the consequence is part of the rule:
   this cap is what limits pooling to short flaps (see "honestly
   bounded"). The probe, at acquire, is on the socket **beneath** the
   SSLSocket — the SSLSocket itself refuses `recv` flags with
   `ValueError` (E1) — with `settimeout(0)` first: with a timeout set,
   a `MSG_PEEK` on a healthy idle peer blocks for the full leftover
   timeout (E2, measured), which would turn every healthy acquire into
   a stall. No `MSG_DONTWAIT` (redundant in non-blocking mode,
   unverified on Haiku's stack). Verdict logic: **anything other than
   `BlockingIOError` discards** — `b''`, data, or an OSError. A
   graceful TLS close arrives as a close_notify *record*, i.e. data,
   never `b''` (E3), so "data = dirty" is the only correct reading; the
   rare healthy KeyUpdate discarded is churn, not loss. A silently dead
   peer (no FIN) passes the probe — that is structural, the probe
   cannot see it, and it is precisely what rule 3 exists for.

6. **Never release a response whose `will_close` is set.** The server
   asked for the connection to end.

7. **Thread safety, and the slot-collision rule.** Every slot mutation
   under the lock; a handed-out connection is exclusively owned; the
   lock is never held across a close, a join, or any response method.
   When a release finds the slot occupied, the incoming connection is
   disposed — and because a released connection by definition has a
   finished pump (rule 1), a plain `close()` is safe there; parking
   (rule 8) is required only where a reader may still exist. This is
   the one sanctioned plain close.

8. **Cancellation stays instant in fact, not just in letter.** The new
   abort-blind windows are enumerated and each gets an abort check:
   before the invisible retry (rule 4), and after a TTFB failure before
   the ladder arm. At process exit, idle pooled connections are
   disposed through `_hard_close`'s descriptor parking — a plain close
   at exit re-opens the fd-reuse window that once wrote TLS bytes into
   a database (the rule-7 collision case is exempt: no reader can
   exist).

9. **The pooled path abandons urllib, and rebuilds its error handling
   deliberately.** `AbstractHTTPHandler.do_open` forces
   `Connection: close` and closes the socket under the response —
   verified, twice sabotaged. The `http.client` rewrite must also:
   * read 4xx/5xx **bodies**, as net.py:429-440 does today, then
     **drain and release** the connection — a 429 arrives on a healthy
     connection, and discarding it re-handshakes in a retry storm at
     the exact moment the provider asked for calm;
   * check proxy environment variables at runtime and bypass the pool
     entirely when one applies — a comment does not reroute a corporate
     user's traffic;
   * surface 3xx as an actionable NetError naming the Location — custom
     `openai_compat` base URLs do hit real redirects.

## Explicitly out of scope

HTTP/2 (and with it, cross-turn keepalive — see "honestly bounded"),
pre-warming, health-check pings, any change to provider code semantics,
retry policy semantics, or the abort contract.

## How it will be proven

The `tests/test_net.py` harness gains a count of distinct accepted
connections — **and a TLS variant of the scripted server**, because
every probe/release behaviour above differs between plaintext and TLS
(E1/E3), and a plaintext-only proof would pass green around a probe
that throws on every production connection.

* Two sequential streams → one accepted connection (both
  `stop_on_done` modes).
* Aborted stream, then a stream → two connections (rule 1).
* Completed stream leaves a releasable connection — the positive case,
  proven under TLS (rule 2).
* Server closes between requests → transparent fresh-connect, no
  RetryPolicy attempt consumed (rule 4, deterministic arm).
* Reused connection, server accepts but never answers → failure within
  TTFB, then a *visible* ladder retry (rules 3, 4).
* Probe on a TLS connection: healthy-idle acquires instantly (E2's
  stall must not exist); close_notify pending → discard (E3).
* Abort during the TTFB window → no second request ever leaves the
  machine (rule 8).
* `post_json` under `NO_RETRY`: exactly one attempt, pool never
  touched (test_net.py:515-520 unchanged, plus a pool-isolation test).
* All existing transport tests pass unchanged.
* On hardware: the four-metric gate, after the EAI_AGAIN fix, before
  any of this merges.
