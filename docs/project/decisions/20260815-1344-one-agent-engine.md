---
status: accepted
date: 2026-08-15
decision: Keep one Python agent engine behind all three front ends
---

# One agent engine behind every front end

## Context and problem

haikode must offer a TUI, a plain/scriptable REPL, and a native Haiku desktop
application without three implementations drifting in permissions, sessions,
tool semantics, provider behavior, or undo.

## Alternatives considered

1. Run an opencode or haikode server and make every UI a network client.
2. Move policy, tools, persistence, and mutations into the C++ desktop host.
3. Keep the Python engine authoritative and adapt each front end to the same
   `Agent` and `TurnController`; let the C++ UI use a narrow NDJSON worker.

## Decision

Choose option 3. `haikode/runtime.py` is the composition root,
`haikode/agent.py` owns the loop, and `haikode/turn.py` owns the durable turn
lifecycle. The desktop app owns native presentation and worker lifecycle, not
a parallel policy or persistence engine. No localhost server is introduced.

## Rationale

It preserves the stdlib-only, standalone product while allowing a genuinely
native BeAPI interface. The shared engine makes command-line and desktop
sessions, permissions, tools, and provider behavior testable as one contract.

## Consequences

- Desktop protocol changes must be coordinated across C++ and Python.
- A short-lived worker may have performance costs; optimize that boundary
  without moving authority into the UI accidentally.
- Front-end-only behavior is acceptable only when it is presentation or native
  lifecycle, not a fork of agent semantics.

## Reversal conditions

Replace the engine boundary only with an explicit migration that demonstrates
feature and persistence parity, preserves existing sessions/configuration, and
passes the same cross-front-end and physical-Haiku acceptance gates.
