> **Archived design brief.** The latched provider history and durable desktop
> checkpoint were implemented by the 2026-08-06 performance audit. This file
> records the pre-implementation question; it does not describe current code.

You are reviewing a design decision in haikode, a from-scratch coding agent
(Python stdlib only) that runs natively on Haiku OS. Be adversarial and
concrete. Cite file:line for every claim about current behaviour — read the
code, do not infer it. Do not modify any file.

Read in full:
- haikode/context.py — especially compact_messages, compact_history,
  plan_compaction, _tail_start, _fold_fingerprint, _recall_summary,
  needs_compaction, summarize_with_reason
- haikode/agent.py — _messages_for_llm (the call site) and _step
- docs/PARITY.md lines 45-55 and 218-224

THE DESIGN UNDER REVIEW

haikode does NOT latch compaction. `self.messages` always holds the full
history; the folded view is recomputed on EVERY provider round inside
_messages_for_llm(). A summary cache keyed by a fingerprint of the folded
set avoids re-summarising an unchanged fold.

By contrast, Claude Code latches: the summary replaces the context and
becomes the prefix going forward.

THE CONCERN I WANT ATTACKED

`plan_compaction` selects the tail from the END (`_tail_start` by turns and
tokens), so the fold boundary SLIDES as the conversation grows. My worry:

1. Each boundary move changes the folded set -> new fingerprint -> a real
   summariser call, not a cache hit.
2. The new summary text changes the PROMPT PREFIX -> the provider's prompt
   cache is invalidated -> a ~100k-token prompt is reprocessed from scratch
   instead of being read from cache.

If true, a long session gets progressively slower and more expensive in a
way that looks like "the provider got slower". A user reports exactly that.

ANSWER THESE

1. Is the concern real? Trace it concretely: for a session that grows by one
   assistant+tool pair per step, how often does the boundary actually move,
   how often is the summariser actually called, and how often does the
   prefix change? Quantify with the real constants in the code
   (DEFAULT_TAIL_TURNS, keep_tokens, the estimator). If it does NOT happen,
   say so and show why — the caching or the boundary logic may already make
   it stable, and I would rather be wrong than fix a non-problem.

2. Is there a cheaper stable-prefix design that keeps what matters? The
   database holds every message independently of the in-memory list, and
   `restore_compaction()` has no caller (PARITY.md:221), so "keep the full
   list for recovery" may be a justification that does not survive contact
   with the facts. Judge honestly whether latching (replace self.messages
   with the folded view once, then append forward) is strictly better here,
   and name what would be LOST.

3. What breaks if we latch? Be specific about: pinned messages, tool-pair
   closure, /compact's explicit keep_last, the token estimator's
   recalibration, session_history, and any test that asserts the current
   behaviour.

4. Independent of latching: is the notify/announce placement now correct?
   It was just moved to fire only immediately before an actual summariser
   call. Is there any path where a summary is written without announcing,
   or announced without writing?

5. VERDICT: keep-as-is / change-with-this-design / needs-measurement-first.
   If measurement first, name the exact instrumentation to add and what
   number would decide it.

Answer as markdown. Be concise and specific. No preamble.
