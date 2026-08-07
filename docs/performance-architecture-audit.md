# haikode performance and architecture audit

**Audit date:** 2026-08-07

**Starting commit:** `9ee016c` (`main`)

**Primary question:** does haikode perform avoidable work per turn, provider
round or desktop Send, especially repeated compaction?

## Verdict

The complaint was valid. Automatic compaction was not merely displaying a
status line too often: after the history crossed its threshold, the request
path repeatedly bought a new summariser call because the result was never
adopted as the provider-facing history. In the deterministic control, the
removed stateless path makes **13 summary requests for the first fold plus 12
later provider rounds**. The corrected path makes **1**, and a fresh
desktop-style worker restores the checkpoint with **0** new summary requests.

No P0 correctness or data-loss defect was found in the audited performance
paths. The audit did find and fix several P1 request multipliers and accounting
errors, plus low-risk P2 overhead. The important remaining work is conditional:
the desktop app still creates one Python process per Send, so a configured slow
MCP server can consume its bounded startup wait on every Send. Fixing that well
requires a persistent desktop worker and native Haiku validation; shortening
the wait blindly would make MCP tools disappear from short desktop runs.

The changes preserve the lossless SQLite transcript, undo semantics, provider
tool-call pairing, authorization behavior and the stdlib-only constraint. No
credential, endpoint, auth flow, provider routing or remote was changed. The
release pass also updated the rotating keyless Zen default after a live 401;
the replacement model completed the same smoke turn at zero reported cost.

## Scope and method

The sweep covered:

- one-shot CLI, plain REPL, curses TUI and native desktop paths;
- request assembly, provider adapters, retry ladders and SSE error handling;
- automatic and manual compaction, context accounting and prompt caches;
- tool-loop behavior, permission boundaries and tool schema serialization;
- SQLite session load, append, backup, checkpoint, compaction and undo;
- MCP startup, lazy LSP startup and desktop process lifecycle;
- current OpenCode source where a comparison was meaningful;
- Gemini's official usage-counter semantics.

The main benchmark is [`benchmarks/performance_audit.py`](../benchmarks/performance_audit.py).
It uses `time.monotonic()`, fake providers, loopback HTTP, temporary databases
and deterministic request ids (`req-0001`, `audit-http-0001`, ...). It performs
no external network request, reads no credentials, and emits no prompt or model
content. Counts are the primary evidence; small local durations are useful for
relative shape but are not provider-latency claims.

The old compaction and retry behavior was also executed from an isolated
`git archive HEAD` copy. This avoids describing the old code from memory after
it has been changed.

Environment notes:

- Mac measurement host: arm64 Darwin 25.5.0, system Python 3.9.6.
- Haiku measurement host: 32-bit x86_gcc2, R1/beta6, Python 3.10.
- Live-provider latency is not mixed into the deterministic tables. A separate
  release smoke test exercised the real Zen HTTPS/SSE path and exact response.

## Runtime map

```text
one-shot / REPL -----------+
curses TUI worker thread --+--> TurnController.run_turn()
desktop C++ worker thread -+         |
  fork+exec Python/Send -------------+
                                      v
                           restore context checkpoint
                           checkpoint files/session
                                      |
                                      v
                                Agent.run()
                           user -> provider round
                                      |
                         zero or more tool calls
                         (emitted order, sequential)
                                      |
                           next provider round ...
                                      |
                                      v
                         persist raw tail + checkpoint
```

The TUI does not block its curses/UI thread: it runs the complete turn in a
worker thread and pumps events back through a queue (`tui.py:2826-2851`). The
REPL is synchronous by design. The desktop C++ app also keeps its UI responsive,
but `_StartRun` forks and execs `haikode.desktop_worker` for each Send
(`desktop/src/domain/AppController.cpp:621-730`). The Python worker then uses
the same `TurnController` as the other frontends (`desktop_worker.py:473`).

Every provider round runs `_messages_for_llm`, streams one adapter request, and
then either returns or executes the emitted tool calls in order
(`agent.py:897-938`, `agent.py:1293-1405`). This order is deterministic and
preserves permission prompts and write dependencies; it is not concurrent tool
execution.

## Reproducible measurements

Final Mac sample (`--desktop-runs 5`):

| Probe | Result | Interpretation |
|---|---:|---|
| Stateless compaction control, first fold + 12 rounds | 13 summary calls | Exact shape of the removed architecture |
| Latched compaction, same work | 1 summary call | No per-round re-summarization |
| Fresh worker after durable checkpoint | 0 summary calls | Process boundary no longer defeats the latch |
| First / later request planning | 12.226 ms / 0.022 ms median | Later rounds use the adopted view |
| Raw / effective messages | 104 / 35 | Raw history remains lossless while provider view is compact |
| Persistent HTTP 500, four-attempt net policy | 4 HTTP requests | One retry ladder, ids 0001-0004 |
| Same old ChatGPT path from `HEAD` | 12 HTTP requests | Four transport attempts multiplied by three provider attempts |
| Measured terminal 429, four-attempt policy | 1 HTTP request | Early precise terminal classification |
| Same old net path from `HEAD` | 4 HTTP requests | Classification used to happen after the retry ladder |
| Loopback SSE time to first event | 1.159 ms | Includes local status/open/read; no TLS |
| 10 MiB DB open, backup due / immediate next worker | 29.532 / 0.819 ms | Full backup no longer repeats on every Send |
| 100 persisted messages | 10.105 ms | 100 transactions; acceptable on Mac, measured separately on Haiku |
| Manual compact / undo, 90 folded rows | 1.054 / 1.185 ms | Undo path is cheap and exact in this fixture |
| Desktop smoke process startup, five runs | 103.319 ms median | Process/config smoke only, not real provider latency |
| MCP 50 ms startup-budget probe | 55.099 ms | Wait is linear; configured default remains 5 s |
| Tool schemas | 6590 estimated tokens in 0.231 ms | Stable bundle is cached by identity |

Final 32-bit Haiku sample:

| Probe | Result |
|---|---:|
| Stateless / latched / fresh-worker summary calls | 13 / 1 / 0 |
| First / later planning | 67.490 ms / 0.537 ms median |
| 10 MiB DB open, backup due / next worker | 312.733 / 11.608 ms |
| 100 message transactions | 152.581 ms |
| Desktop worker smoke startup | 1319.639 ms (one sample) |
| Loopback SSE first event | 13.369 ms |
| MCP 50 ms wait probe | 50.804 ms |
| Leftover audit/worker/MCP processes | 0 |

The Haiku numbers justify the changes that remove process-amplified work. They
do not justify pooling network connections or batching durable message commits
without a separate correctness design.

## Fixed findings

### F01 — P1: automatic compaction repeated model work

**Evidence.** The baseline control made one summariser call on every tested
provider round. The successful summary returned only as a temporary request
view; raw `Agent.messages` was planned again on the next round. The problem was
especially expensive after the preserved tail saturated, but an already-large
fixture reproduced it immediately.

**Fix.** `Agent` now separates the lossless `_messages` transcript from the
latched `_llm_history`. New raw messages are appended into the provider view;
only a successful summary replaces its fold. A failed summariser yields a
transient drop notice and leaves the raw material available for another try
(`agent.py:321-337`, `agent.py:423-494`, `agent.py:897-934`).

**Accounting.** Summary usage is recorded as background work without replacing
the main request's `latest` context size (`usage.py:423-436`).

**Regression evidence.** The benchmark's 13 -> 1 control and agent-loop tests
cover repeated tool rounds, failed turns, summary failure, raw-history
preservation and cross-Agent cache isolation.

### F02 — P1: a desktop process boundary discarded the compaction benefit

An in-memory latch alone would still summarize again because desktop creates a
fresh Python process for every Send. A new `context_checkpoints` table stores
the successful provider projection, the raw-prefix boundary and an O(1)
boundary digest. The raw messages remain canonical. Timeline rewrites clear the
projection transactionally (`session.py:133-146`, `session.py:1420-1486`,
`session.py:1594-1695`).

`TurnController` restores the projection before all frontend turns and saves it
only after the raw tail persisted (`turn.py:280-371`, `turn.py:468-529`). A
fresh worker with its process cache cleared makes zero summary calls in the
benchmark.

### F03 — P1: nested retry ladders multiplied an outage

ChatGPT subscription streaming had a three-attempt retry loop outside the
transport's retry policy. A persistent HTTP 500 therefore made 12 requests with
a four-attempt test policy and would make 18 with the default six-attempt
policy. An exhausted `NetError` is now marked `transport_exhausted`, so only
retryable inline SSE error events use the outer policy
(`providers/subscription.py:191-202`, `providers/subscription.py:288-301`).

The deterministic result is 12 -> 4 requests with request ids proving the
actual attempt count. Partial output was already a no-replay boundary and
remains one.

### F04 — P1: a measured terminal 429 was retried as if transient

The observed Anthropic refusal had an organization/workspace echo, no rate
metadata, and the exact `rate_limit_error: Error` placeholder. Classifying it
only in the provider adapter was too late: the transport had already retried.

The early classifier now recognizes only that measured shape, plus explicit
billing/quota markers (`net.py:199-257`). A generic gateway 429 without rate
headers remains retryable; broad "no headers means terminal" logic was tested
and rejected because it would suppress legitimate recovery. Measured result:
4 -> 1 requests under the four-attempt policy.

### F05 — P1: provider-reported context overflow had no recovery path

A locally estimated prompt can still disagree with the provider. A pre-output
`context_overflow` now forces one summary and exactly one main-request retry.
Once text, reasoning or any tool-call delta has been delivered, the request is
never replayed (`agent.py:1014-1124`, `agent.py:1323-1353`). Failed-request,
summary and recovered-request usage are all counted. The regression fixture is
three provider calls with 23 input and 3 output tokens; a partial-output case is
one call and an error.

### F06 — P1: token accounting caused wrong compaction and cost decisions

Three independent errors were corrected:

1. Prompt calibration omitted serialized tool schemas. A tool-heavy prompt
   therefore looked like tokenizer undercount and inflated `token_scale`, which
   could trigger compaction early. Schema tokens now use the same estimator as
   the context meter and are cached by the stable specs-object identity
   (`agent.py:942-950`, `usage.py:227-332`).
2. The context meter scanned/reported raw history after the provider view was
   compacted and used the combined context window rather than the provider's
   input limit. It now reads the effective view and `input_window`.
3. Gemini thinking and cached tokens were double-counted. Google's response
   schema defines total usage as prompt + tool-use prompt + candidates +
   thoughts, while prompt includes cached content. The adapter now exposes
   disjoint `input`, `cache_read`, `output` and `reasoning` fields whose sum
   matches `totalTokenCount` (`providers/gemini.py:264-286`). See the official
   [GenerateContentResponse usage metadata](https://cloud.google.com/vertex-ai/generative-ai/docs/reference/rest/v1/GenerateContentResponse)
   and [Gemini thinking documentation](https://ai.google.dev/gemini-api/docs/generate-content/thinking).

### F07 — P1: a cosmetic title doubled a short interactive session

After the first successful interactive turn, haikode opened a hidden main-model
stream to produce a 3-5 word tab title. It added latency and unreported usage,
and could race the next ChatGPT stream. The title is now shortened locally from
the stored subject; only explicit `/farewell` composition calls a model, and
that usage is counted (`turn.py:373-448`).

Current OpenCode source uses a small model for title generation where one is
available, but haikode has neither a need nor a safe latency budget for this
cosmetic request. Reference:
[OpenCode session prompt source](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/session/prompt.ts).

### F08 — P1 on Haiku: desktop resolved an API key twice per Send

Desktop preflight called `Config.get_api_key`, then provider construction called
it again. On Haiku each lookup may launch the native keystore helper. Preflight
now checks OAuth state before construction and checks the already-built
provider's `api_key` afterward (`desktop_worker.py:329-350`,
`desktop_worker.py:425-437`). The integration test spies on the actual
`get_api_key` method and proves exactly one resolution.

### F09 — P1 on short-lived desktop workers: full DB backup on every Send

"Once per process" meant once per Send. `PRAGMA quick_check` plus SQLite backup
scales with the whole store: before the fix a 10 MiB store took about 25.7 ms on
every audit-Mac open and a 50 MiB store about 100 ms. Backups now have a
cross-process five-minute minimum interval; a due 10 MiB backup took 29.532 ms,
the next worker 0.819 ms on Mac, and 312.733 / 11.608 ms on 32-bit Haiku
(`session.py:58-66`, `session.py:666-724`). Rotation and corruption safety
remain tested.

### F10 — P2: per-round OAuth file reads and disabled tool batching

SuperGrok now reads/refreshes OAuth once per user turn rather than once per
provider round (`providers/subscription.py:343-370`). ChatGPT now advertises
`parallel_tool_calls: true`, allowing the model to emit independent calls in one
response; the agent still executes them in deterministic order
(`providers/subscription.py:224-233`, `agent.py:1388-1405`). This removes
avoidable model round trips without introducing concurrent filesystem or
permission side effects.

### F11 — P2: smaller synchronous costs and misleading recovery UI

- Retry jitter is sampled once for both deadline admission and the actual sleep
  (`net.py:738-815`).
- `TurnController._persist` indexes only the new raw tail rather than copying
  the complete transcript first (`turn.py:468-529`).
- MCP tool changes invalidate the system-prompt cache, so a late tool is not
  invisible behind stale prompt text (`agent.py:616-665`).
- `/compact undo` and `/compact restore` now expose the existing exact restore
  path; manual no-session compaction honors its keep count
  (`repl.py:1264-1322`).
- The TUI no longer claims the truncated `session_history` tool can recover
  exact folded detail (`tui.py:2960-2965`).

## Reviewed and deliberately not changed

### R01 — P2 conditional: one desktop process per Send

The Mac smoke median is about 103 ms; the one 32-bit Haiku sample is 1.32 s.
The smoke path exits before real session/provider work, so it is a lower-bound
process/config measurement, not a full Send benchmark. A persistent worker
would also let LSP/MCP clients and caches survive between Sends.

This remains because it crosses the native C++ protocol, cancellation,
ownership, session switching and crash-recovery boundaries. It requires a
design and a locally opened desktop validation; GUI validation is explicitly
forbidden over SSH.

### R02 — P2 conditional: MCP can wait up to five seconds per desktop Send

`MCPManager.start_all()` runs servers concurrently but waits for the slowest up
to `DEFAULT_STARTUP_WAIT = 5.0` seconds (`mcp.py:60`, `mcp.py:953-987`). That is
paid once per CLI process, but desktop's process-per-Send architecture can pay
it every turn. A 50 ms controlled wait measured 55.099 ms on Mac and 50.804 ms
on Haiku, demonstrating the linear boundary.

Reducing the wait without persistence would trade latency for missing MCP
tools: a late client joins at the next turn, but the desktop worker has exited
by then. The correct fix is the persistent-worker work in R01.

### R03 — P2 measurement gate: fresh DNS/TCP/TLS per provider request

The stdlib layer creates a fresh HTTP connection per request; only the
`SSLContext` is shared. Pooling could reduce handshakes in a fast tool loop, but
replaying a non-idempotent POST after a stale pooled connection can duplicate a
model request or MCP action. Loopback TCP was only 0.174 ms on Mac; real Haiku
TLS/provider measurements were intentionally not made. The existing
`docs/specs/persistent-connections*.md` gate should be completed before a
pooling implementation.

### R04 — P2 accepted: tool execution is sequential

The provider may emit several calls in one response, but haikode runs them in
order. Blind concurrency is unsafe for edits, bash, permissions, session
context and calls where later arguments assume earlier output. Parallelizing a
proven read-only subset could be a future optimization; the current benchmark
shows the first `read` setup cost (9.633 ms Mac / 160.102 ms Haiku) dominates
the subsequent two calls (about 0.17 ms / 2.8 ms), so provider round-trip
batching was the lower-risk win.

### R05 — P2 accepted: one SQLite commit per message

One hundred appends took 10.105 ms on Mac and 152.581 ms on 32-bit Haiku.
Batching a whole turn could be faster, but partial-turn durability, exact
sequence assignment, cross-process contention and undo checkpoints are
correctness boundaries. No change was made without an end-to-end transactional
design.

### R06 — P2: compaction model and generic inline SSE errors

The summariser still uses the active model and effort. Current OpenCode allows a
configured compaction agent/model and otherwise falls back to the user model:
[compaction source](https://github.com/sst/opencode/blob/dev/packages/opencode/src/session/compaction.ts).
Changing haikode to a smaller model without a configured, compatible provider
would be a quality and routing guess, so it remains explicit future work.

Generic non-ChatGPT inline transient SSE errors are not replayed automatically.
After an event has been accepted, replay can duplicate billed work; this remains
a hypothesis rather than a verified latency defect.

## Rejected hypotheses and over-broad fixes

- **"The raw 100k transcript is sent on every round."** False. The old main
  request used summary + tail. The expensive repeated work was the hidden
  summariser request that rebuilt that view.
- **"Every 429 without rate headers is terminal."** False. Generic gateways
  emit ordinary transient 429s with generic headers. Only the measured
  Anthropic placeholder shape and explicit quota/billing markers stop early.
- **"The TUI blocks while the provider runs."** False. The turn runs on a
  worker thread; the UI queue remains active.
- **"Enable concurrent tool execution globally."** Rejected. Tool batching in
  one provider response is enabled, but execution order remains a correctness
  property.
- **"Connection pooling is obviously the main difference from OpenCode,
  Codex or Claude."** Unproven. No claim is made about proprietary Codex or
  Claude internals, and no live comparative latency benchmark was run.
- **"A shorter MCP timeout fixes desktop."** Incomplete. It hides tools that
  have no later turn in which to join that short-lived worker.
- **"Desktop smoke measures Send latency."** False. It measures process/config
  startup with a test reply, not SQLite load, MCP/LSP startup, provider TTFE or
  model generation.

## Validation

Mac acceptance:

```text
2427 tests in 46.790 s
4 expected failures, all tests/test_wiring_audit.py
4 skips
0 unexpected failures, 0 errors
scripts/ci_baseline.py: exit 0
benchmarks/run.py --validate: exit 0 (two platform-command skips)
performance_audit.py: exit 0
compileall, shell syntax and git diff --check: exit 0
```

One non-multiplexed SSH session was used on `shredder32`; no GUI was launched.
The temp archive accidentally omitted `README.md` and
`scripts/hooks/prepush_scan.py`, so two tests failed to load their fixture files.
This makes that full-suite attempt harness-inconclusive rather than green:

```text
2413 tests in 191.153 s
4 expected wiring failures
2 missing-file harness errors
0 other loaded-test failures
compileall: exit 0
performance_audit.py: exit 0
leftover worker/MCP/audit processes: 0
```

The one-SSH rule prevented a corrective rerun. The missing files and exact
tracebacks are recorded here rather than misreported as Haiku regressions.

## Commands

```sh
HAI_DISABLE_KEYSTORE=1 python3 scripts/ci_baseline.py
python3 benchmarks/run.py --validate
HAI_DISABLE_KEYSTORE=1 python3 benchmarks/performance_audit.py --pretty
python3 -m compileall -q haikode benchmarks/performance_audit.py
git diff --check
```

## Follow-up gates

1. Design a persistent desktop worker with explicit session switching,
   cancellation, crash restart and native UI validation. Re-measure process,
   MCP, LSP, DB-open and first-event time together.
2. Run the existing persistent-connection measurement gate with live, redacted
   timing only; implement pooling only if short-interval failures/handshakes are
   material and non-idempotent replay remains impossible.
3. Add an explicit optional compaction provider/model setting before changing
   summariser quality or price.
4. On the next permitted Haiku session, include the complete repository test
   resources and repeat `scripts/ci_baseline.py`; do not launch the desktop GUI
   over SSH.
