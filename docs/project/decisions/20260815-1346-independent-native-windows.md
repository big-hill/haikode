---
status: accepted
date: 2026-08-15
decision: Give every native desktop window an independent process-local route and session
---

# Independent native desktop windows

## Context and problem

Users open several coding conversations simultaneously and expect different
models in different windows. A single-launch application and global writes from
the model menu made an additional window invisible or coupled live windows to
one shared route.

## Alternatives considered

1. Restrict the desktop to one window.
2. Keep one process with many windows and introduce a shared routing manager.
3. Use Haiku `B_MULTIPLE_LAUNCH`; let each process own one window, controller,
   session, and provider/model/reasoning route.

## Decision

Choose option 3. `File > New Window` launches the same executable entry, not a
possibly stale copy with the same signature. Window-local choices affect the
next reply in that process only. Global Settings remain defaults for new
windows. Initial frames cascade within current screen bounds so a launch is
visible.

## Rationale

The process boundary gives simple ownership and failure isolation with no
artificial window cap. It also matches the existing one-controller/one-session
desktop lifecycle and avoids mutating global config during ordinary window
selection.

## Consequences

- SQLite and config reads must tolerate several processes.
- Every worker receives immutable per-run routing through its environment.
- Cross-window coordination is intentionally absent unless a later feature
  defines it explicitly.

## Reversal conditions

A single-process multi-window design may replace this only after proving
per-window sessions/routes, concurrent cancellation and approvals, crash
isolation, visible placement, clean shutdown, and compatibility with existing
launch behavior on physical Haiku.
