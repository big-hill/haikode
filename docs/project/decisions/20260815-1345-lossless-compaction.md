---
status: accepted
date: 2026-08-15
decision: Preserve a lossless raw transcript and derive a latched provider history
---

# Lossless transcript with derived provider compaction

## Context and problem

Recomputing a sliding compaction fold before provider rounds caused repeated,
hidden summarizer calls. Replacing the stored message list with a summary would
remove the material required for exact persistence, undo, replay, and later
recovery. Turn persistence also appends relative to the durable session length,
so shrinking the raw list can lose new turns.

## Alternatives considered

1. Recompute a compacted view from the raw transcript for every request.
2. Replace the raw transcript with the compacted summary.
3. Keep raw `Agent.messages` lossless and maintain a separate latched
   provider-facing history, checkpointed across desktop worker processes.

## Decision

Choose option 3. Raw messages remain the authority for storage, resume, undo,
and audit. The provider history is derived, latches only a successful summary,
and is invalidated by transcript replacement, resume, undo, or manual
compaction as required. A validated SQLite checkpoint may restore the derived
view in a later worker.

## Rationale

This removes repeated model work without weakening recovery or pretending a
summary is the original conversation. Failed summaries cannot destroy source
material, and synthetic tool-pair repair never becomes raw truth.

## Consequences

- `agent.py`, `context.py`, `turn.py`, and `session.py` form one coupled seam.
- Token usage, checkpoint identity, invalidation, tool-pair integrity, and
  concurrent SQLite behavior need regression coverage.
- UI text may describe compaction, but cannot imply raw history was deleted.

## Reversal conditions

Any replacement must prove lower or equal provider work, exact transcript and
undo preservation, safe failed-summary recovery, cross-process continuity, and
no lost messages at the turn persistence boundary.
