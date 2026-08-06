# Compaction review: latch the LLM history

Reviewer: gpt-5.6-sol at max effort, 6 August 2026, reading the code and
running read-only experiments against `plan_compaction`. Kept verbatim from
the answer section onward. The brief is beside this file.

Headline: the fold boundary is stable until the tail budget saturates, and
slides one pair per step forever after — one synchronous, **unmetered**
summariser call before nearly every visible provider round. It also
corrects the framing that prompted the review: after compaction the prompt
is summary + 8k tail, not the raw history, so the cost is the summariser
call rather than a 100k-token cache miss.

Verdict: change-with-this-design.

---


1. While recent history fits inside 8k, appended pairs do not necessarily move the effective fold. The exact fingerprint remains cached.
2. Once the newest turn itself exceeds 8k, `_split_tail()` maintains a sliding suffix. With similarly sized pairs, each new pair evicts approximately one old pair. Tool-pair closure makes the effective unit a complete assistant/result exchange; a raw boundary move within one pair may therefore leave `plan.folded` unchanged. [context.py:750–788](haikode/context.py:750) [context.py:791–848](haikode/context.py:791)

Concrete example using the real estimator: an assistant message with 1,000 content characters, one `read({"filePath": "/p/0"})` call, and a 1,000-character tool result is about 621 estimated tokens. Twelve such pairs fit; around the thirteenth, the newest turn saturates the 8k tail. In a homogeneous trace, every pair after that advances the effective fold, producing one summariser call per provider step.

The cache only helps exact folds. It hashes the folded messages, model and anchor; a new folded set gets a new key and therefore a real call. The LRU contains only eight entries. [context.py:640–676](haikode/context.py:640) [context.py:949–978](haikode/context.py:949) The existing test proves both halves: identical history costs one call; appending a message costs another. [test_context.py:576–595](tests/test_context.py:576)

The prefix also changes whenever the effective fold grows: even if the model returned byte-identical summary prose, `summary_message()` embeds the new folded-message count in its content. [context.py:496–507](haikode/context.py:496) A token-prefix cache can still reuse the preceding system prompt and any earlier pinned messages, but not the sequence from the changed summary onward.

The important correction: the main post-compaction history is ordinarily summary plus an 8k tail—not the raw 100k history—plus unbounded pinned/system messages and tool-pair partners. The summariser is requested with at most 4,096 output tokens. [context.py:405–413](haikode/context.py:405) [context.py:825–869](haikode/context.py:825) [context.py:985–988](haikode/context.py:985)

The hidden expensive call is the summariser. It rereads up to 120,000 characters—about 36.4k estimator tokens—of folded transcript; individual tool results are truncated to 2,000 characters. [context.py:407–413](haikode/context.py:407) [context.py:527–570](haikode/context.py:527) Its usage is not recorded: `summarize_with_reason()` ignores usage chunks, whereas `_step()` records only the following main request. [context.py:597–622](haikode/context.py:597) [agent.py:899–910](haikode/agent.py:899)

Worse, an auto-only session has no previous-summary anchor: the generated summary never enters `self.messages`, so a changed fold is summarised again from raw history. Once the 120k-character transcript cap is reached, the oldest material is simply cut. [context.py:557–570](haikode/context.py:557) [context.py:685–697](haikode/context.py:685) [context.py:949–954](haikode/context.py:949) [agent.py:797–804](haikode/agent.py:797)

So the reported progressive slowdown is plausible: a growing, synchronous, unmetered summariser call can occur before nearly every visible provider round.

## 2. Stable-prefix design

A latched LLM context is better. Blindly replacing `self.messages` is not.

The cheapest safe design is:

- Keep `self.messages` as the raw transcript for existing persistence/front-end contracts.
- Add a latched `_llm_history` plus a raw-message cursor.
- Append new raw messages to `_llm_history`.
- Compact `_llm_history`, and latch only a successful summary.
- Send a failed drop-with-notice result transiently; do not permanently latch it, or a temporary summariser failure destroys the material needed for a later retry.
- Invalidate/rebuild `_llm_history` after resume, undo, manual compaction, history replacement or relevant mutation.
- Optionally persist `{summary, folded_through_seq}` as a context checkpoint without deleting raw message rows.

After a successful latch, the prior summary becomes part of the next fold; `last_summary()` supplies it as `<previous-summary>`, while `serialize_for_summary()` omits the summary from the raw transcript. That is the anchored-update design the current automatic path claims but does not achieve. [context.py:557–583](haikode/context.py:557) [context.py:685–697](haikode/context.py:685) [context.py:949–974](haikode/context.py:949)

At scale 1, no pins, and a worst-case 4,096-token summary plus 8k tail, the next summary would wait for roughly:

- 96k new history tokens on a 128k window
- 238k on a 272k input window
- 330k on a 372k input window

That is categorically better than resummarising whenever the 8k tail slides.

The database is not yet sufficient justification for directly discarding `self.messages`: persistence happens only after `agent.run()` finishes, and it appends `agent.messages[len(session.messages):]`. Shrinking `agent.messages` below that baseline can persist none of the new turn. [turn.py:283–342](haikode/turn.py:283) [turn.py:472–505](haikode/turn.py:472) During the run—and whenever persistence fails—the in-memory list is the only complete transcript.

Also, `session_history` is not exact recovery despite the compaction footer’s wording: it omits all tool-result messages, truncates user/assistant lines to 600 characters, and keeps only 40 lines. [context.py:503–505](haikode/context.py:503) [session.py:1709–1713](haikode/session.py:1709) [session.py:1727–1765](haikode/session.py:1727)

## 3. What breaks under a literal latch?

| Area | Result |
|---|---|
| Pinned messages | Already-pinned messages survive because the planner always keeps them. Continue using `plan_compaction()`; slicing manually would break this. A folded raw message can no longer be pinned later without recovering it from raw storage. [context.py:481–488](haikode/context.py:481) [context.py:825–848](haikode/context.py:825) |
| Tool-pair closure | The planner preserves complete call/result exchanges. But latching the output of `pair_tool_messages()` into the raw transcript would also persist synthetic repairs and dropped orphans; the present contract deliberately repairs only the wire view. [agent.py:126–165](haikode/agent.py:126) [context.py:791–848](haikode/context.py:791) |
| `/compact N` | With a session, `N` is a message count, closure may keep more, and the agent reloads the persistently compacted session afterward. A shadow LLM latch must be reset then. [repl.py:1264–1291](haikode/repl.py:1264) [context.py:835–848](haikode/context.py:835) Without a session, the parsed number is already ignored because that branch calls `compact_history()` without `keep_last`. [repl.py:1264–1278](haikode/repl.py:1264) |
| Recalibration | Run `needs_compaction()` against the latched LLM history and retain `token_scale`. Large-window tail size remains 8k despite scale changes; smaller windows can move the tail when scale changes. [context.py:939–944](haikode/context.py:939) [agent.py:648–666](haikode/agent.py:648) Independently, current recalibration compares provider input—including tool schemas—with an estimate that sums only messages, so it is not a pure tokenizer correction. [agent.py:861–868](haikode/agent.py:861) [agent.py:899–910](haikode/agent.py:899) |
| Session recovery | Automatic compaction currently leaves raw completed turns in the message table. Persistent `Session.compact_now()` instead deletes those rows, stores them as JSON in `compactions`, and inserts a summary; `session_history` reads only `session.messages`. [session.py:1397–1448](haikode/session.py:1397) [session.py:1819–1833](haikode/session.py:1819) `restore_compaction()` can restore the JSON, but PARITY correctly records that it has no caller. [session.py:1483–1526](haikode/session.py:1483) [PARITY.md:218–224](docs/PARITY.md:218) |
| Tests | A direct assignment inside `_messages_for_llm()` breaks the test requiring wire repair not to mutate stored history and likely the audit comparing a shorter sent view with the full agent list. [test_wiring_review.py:182–188](tests/test_wiring_review.py:182) [test_wiring_audit.py:430–438](tests/test_wiring_audit.py:430) The helper-level non-mutation and exact-fold cache tests should remain unchanged under a separate latched view. [test_context.py:576–595](tests/test_context.py:576) [test_context.py:654–661](tests/test_context.py:654) |

## 4. Notify placement

It is correct for the meaning “a fresh, potentially slow summariser attempt is starting.” Cache hits, no-provider fallback, no-fold and below-threshold paths do not announce; the fresh-call branch announces immediately before `summarize_with_reason()`. [context.py:935–978](haikode/context.py:935)

It is not a one-to-one “summary was written” signal:

- Announced without a successful summary: the provider can error, raise or return empty after notification; the result then contains a drop notice. [context.py:603–622](haikode/context.py:603) [context.py:967–984](haikode/context.py:967)
- Summary applied without announcing: a cache hit inserts the remembered summary without a fresh call, intentionally. [context.py:954–957](haikode/context.py:954) [context.py:985–988](haikode/context.py:985)
- Fresh summary without visible announcing: `_messages_for_llm(on_event=None)` supplies no callback, and notification exceptions are swallowed. [agent.py:781–800](haikode/agent.py:781) [context.py:967–974](haikode/context.py:967)
- Manual persistent `/compact` calls `Session.compact_now()`’s summariser directly and has no notification hook. [session.py:1352–1393](haikode/session.py:1352) [repl.py:1279–1291](haikode/repl.py:1279)

So the placement is right; the event should continue to say “compacting,” not “compacted.”

## 5. Verdict

**Change-with-this-design:** latch a separate LLM-history view, preserve the raw transcript/persistence contract, latch only successful summaries, and update future summaries from the previous latched summary.

Measurement is still useful for attributing the specific field report, but it should not block the change: the code and 8k sliding-tail arithmetic already prove a steady state with one synchronous summariser call per provider step. The current design also hides those calls from usage accounting.

Six focused cache, notification, non-mutation and automatic-compaction tests passed read-only; no files were modified.


