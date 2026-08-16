---
status: accepted
date: 2026-08-16
decision: Keep the session database out of WAL mode on Haiku, converting once while provably alone
---

# The session store leaves WAL on Haiku

## Context and problem

Two haikode instances on one Haiku machine cannot share the session database.
The second reports `session not saved - undo unavailable (could not open a
session: locking protocol)` and lists no sessions at all, while the first runs
normally. The sessions are not lost; the second process simply never reaches
them.

The cause is below haikode. In WAL mode every connection coordinates through
the `-shm` write-ahead-log index, and on Haiku that coordination is unreliable
across processes. `CONTRIBUTING.md` has recorded the symptom since before this
investigation: "SQLite reports 'locking protocol' under concurrency".

Multiple processes are a designed condition, not an edge case. The native
desktop starts one Python worker per Send, so the desktop beside a terminal is
two processes by construction, and `CONTEXT.md` invariant 6 makes independent
windows a product promise.

The store already carries years of scar tissue for this: a cross-process guard,
a retry ladder, WAL recovery, and rotating backups. Those reduce the damage.
None removes the cause, because the cause is that the database is in WAL mode.

### Evidence

Two probes on Haiku hrev57937+129, x86_64, SQLite 3.50.4. Read them together:
the first has production's shape, the second has its worst case.

**Near-idle, the shape a user actually creates.** One process holds a
connection and commits; a second repeatedly opens the database from scratch.
Twelve seconds, three identical runs:

| journal_mode | opens completed | failures |
|---|---|---|
| `wal` | 5 | 1 x `locking protocol` |
| `truncate` | ~159 | none |

**Saturated, to separate the modes under stress.** One process commits in a
tight loop the way `Session.append` writes — `BEGIN IMMEDIATE`, insert, update,
commit, `synchronous=FULL`. A second reads the session list exactly as the
picker now does, `mode=ro` with a 0.25 s lock timeout. A third opens the
database and replays the schema. Fifteen seconds per mode:

| journal_mode | commits | picker reads | schema-replaying opens |
|---|---|---|---|
| `wal` | 44491, p50 0.000s | **1**, 5.004s, plus `locking protocol` | **1**, 4.832s, plus `locking protocol` |
| `delete` | 1185, p50 0.013s | 297, p50 0.000s, p95 0.002s, none failed | 73, p50 0.006s, none failed |
| `truncate` | 1206, p50 0.012s | 297, p50 0.000s, p95 0.001s, none failed | 73, p50 0.007s, none failed |

Read the second table for what it is. Its writer commits about 3000 times a
second; production commits a handful per turn, spaced by model latency. It
proves the modes differ under stress and that the rollback journals cost about
13 ms a commit. It does not by itself prove lockout at production load — the
near-idle probe and the field report do that. Its third column is a raw connect
plus schema replay, not a real `connect()`: a real one also migrates, runs
`quick_check` and rotates a backup, which measured 27.5 s and 37.9 s under
contention, and it would have set `journal_mode=WAL` and undone the very
conversion being measured.

**More than two, under `DELETE`.** Three processes committing in tight loops
and three reading the session list, twelve seconds, all against one store:

| | result |
|---|---|
| commits | 957, p50 0.013s, p95 0.014s, **max 2.054s**, **4 x `database is locked`** |
| picker reads | 711, p50 0.001s, p95 0.002s, max 0.004s, none failed |

Nothing here limits the store to two processes, and readers do not compete with
each other at all — the picker was untouched by three simultaneous writers.
Writers serialise, which is the real constraint: under this saturated load a
few commits exceeded the five-second busy timeout and failed outright. That is
several times production's write rate, but it sets the implementation's
homework, below.

## Alternatives considered

1. Keep WAL and keep hardening around it — a longer retry ladder, more
   recovery, more backups.
2. Keep WAL and serialise haikode to one process per machine.
3. Keep WAL but open it with `locking_mode=EXCLUSIVE`, which avoids `-shm`.
4. Keep the database out of WAL mode on Haiku, converting it once.
5. Keep the database out of WAL mode on every platform.

Option 1 is what the store already does; the tables show the ceiling. Option 2
abandons invariant 6, and option 3 is option 2 wearing a pragma — an exclusive
lock makes the store single-process by construction, so the desktop worker
could not open it beside a terminal.

## Decision

Choose option 4, with `DELETE` as the mode, and convert in place.

**Which mode.** `DELETE` and `TRUNCATE` are indistinguishable above. `DELETE`
wins on a property no benchmark shows: it is what a fresh connection already
reports, so no connection has to set a journal pragma at all. That is
simplicity, not a contention win — switching among rollback modes needs no
file-format transition and no exclusive lock, unlike entering or leaving WAL.
One fewer thing every connection must remember to do is reason enough.

Note what "converting to DELETE" really means: leaving WAL. The one bit that
persists in the file is the WAL flag; the rollback mode itself is per
connection.

**How the conversion runs.** On the connection this process already holds,
while holding the cross-process guard exclusively. Not by closing the
connection and re-acquiring the guard from an unlocked state: that enters the
unguarded window `_claim_shared` exists to prevent, and a neighbour running an
older build could take the guard during it and run the old WAL recovery.
Converting in place also earns a second, independent net — SQLite refuses to
leave WAL while any other connection is attached.

1. Read the session and message counts through the held connection. They are
   the baseline step 5 checks, and they must be read after the guard is won,
   or a neighbour's write between reading and converting reads as damage.
2. Take the guard exclusively from the shared hold, non-blocking.
3. Verify the mode is still WAL, having won the guard.
4. Take a verified backup through the SQLite backup API.
5. `PRAGMA journal_mode=DELETE` and **read the value it returns**. SQLite
   returns the old mode when the transition did not happen; a conversion that
   silently did not occur must never be recorded as one.
6. Verify with `quick_check` and re-read the counts from step 1.
7. Return the guard to its shared hold.

**Every exit from this sequence re-secures the shared hold before the store is
used again, with `_claim_shared`'s patience, and refuses to run unguarded if it
cannot.** This is the step that is easy to get wrong: `flock` conversion drops
the held lock before taking the new one, so the loser of a simultaneous upgrade
is left holding an open guard file with no lock on it. Two instances of a new
build starting together is the ordinary case here, not a corner. A process that
cannot re-secure the guard must fail the way `_claim_shared` already fails,
because continuing unguarded is the state this store's history says cost users
committed turns.

**Failure branches.** Before step 5 reports `delete`, any failure leaves the
database in WAL, the backup in place, and the store usable; the conversion is
never required for haikode to run. After it, the database is converted, so a
step 6 failure must revert with `PRAGMA journal_mode=WAL` — which succeeds,
since the exclusive guard is still held — keep the backup, and report. It must
not leave a converted store whose verification did not pass.

**Where it applies.** Haiku only. Elsewhere WAL's readers never block on the
writer, which is what keeps a development TUI and per-Send desktop workers off
each other's backs on a platform where the shared-memory index works.

**New databases.** Created outside WAL on Haiku, so a fresh install never
converts.

## Rationale

The measurements decide it. WAL's advantage is write throughput this workload
does not need: a turn commits a handful of messages, so 13 ms each is a few
tenths of a second, spent after the model has already answered. WAL's cost is
that a second process cannot reliably reach the store, which is not a
performance characteristic but a broken product promise.

Converting in place rather than through a release-and-reacquire dance keeps the
store guarded throughout. Reading the pragma's return value matters because
SQLite reports a refusal to leave WAL by returning the unchanged mode rather
than by raising.

## Consequences

- Commit latency on Haiku goes from effectively free to about 13 ms. A turn
  pays a few tenths of a second.
- **Writers now block behind readers**, the direction WAL made impossible.
  `search()` reads every message body of every session, so a cold browse from a
  second process holds shared locks for as long as that takes on a large store,
  and a concurrent `Session.append` waits. If it times out it raises `database
  is locked`, which is *not* in `_append_wedge_tokens`, so it surfaces to the
  user as a failed save rather than healing. That token list must be revisited
  with this change.
- Readers no longer see a snapshot while a write is in flight; they wait for
  it. At p95 2 ms that is not felt, but it is a different concurrency model.
- **Writers serialise against each other, and the current five-second busy
  timeout is not always enough.** With three processes committing continuously,
  four commits failed with `database is locked` and the worst took 2.05 s while
  the median stayed at 13 ms. Readers were unaffected throughout. Production
  writes are bursty rather than continuous, so this is a stress bound and not a
  prediction — but it means the implementation must raise the write busy
  timeout and decide, deliberately, that `database is locked` is a wait rather
  than a lost turn. Those two are the same work as the `_append_wedge_tokens`
  item above and must land with the conversion, not after it.
- **Bulk writes are already batched, with one trap.** `compact_now`,
  `restore_compaction` and `revert_to` each commit once, and the tree has no
  import path. The only per-row committer is `Session.extend`, which has no
  callers — at 13 ms a row it is a loaded trap for whoever writes the first
  bulk path, and it should be made to batch before it acquires one.
- The two consequences above pull against each other: batching a large write
  into one transaction is the right answer to commit latency, and it creates
  exactly the multi-second exclusive window during which a concurrent picker
  read dies at its 0.25 s timeout. Neither can be optimised without the other.
- **A hot journal and read-only readers do not mix.** After a crash, SQLite
  rolls back `sessions.db-journal` by itself only for a writable connection. The
  hottest read paths are now `mode=ro`, so `quick_session_count` degrades to a
  silent zero and a cold browse raises until something opens the store
  writably. That interaction is new and needs its own handling.
- haikode must never delete or truncate a `sessions.db-journal` itself.
- `-wal` and `-shm` stop existing on Haiku. Recovery code that reasons about
  them becomes dead there and must be gated on actually finding a WAL database
  rather than on the shape of an error message.
- **The acceptance platform now runs a different concurrency model from the one
  CI and the workstation exercise.** Under invariant 5 every store-adjacent
  change afterwards needs Haiku evidence that `ci_baseline.py` cannot provide.
  This is the standing cost of choosing option 4 over option 5.
- While the converter holds the exclusive guard through a whole-database
  backup, a concurrently starting instance retries `_claim_shared` for five
  seconds and then raises "another haikode is recovering this database". On
  slow storage with a large store that is a plausible one-off failed start.
- Databases already in WAL convert on first open by a build carrying this
  change, once, while alone. A user who never runs haikode alone keeps WAL and
  today's behaviour — which is why this must not be the only fix shipped.
- **Haiku does not release byte-range locks when a process dies, and that can
  block the conversion indefinitely.** Measured on a machine whose haikode had
  been killed: the SQLite SHARED range of the live `sessions.db` answered
  `HELD` from every later process, while a byte-identical copy answered
  `free`; the same was true of `sessions.db-shm` and of `sessions.db.guard`.
  Reads still work, because shared locks do not exclude each other — the store
  lists and opens normally. Nothing can ever take the exclusive lock that
  leaving WAL requires, so the conversion is refused on every start until the
  machine is rebooted. The design already handles this correctly: the pragma's
  return value is checked, the store stays in WAL, and it remains fully
  usable. It does mean a conversion that never happens is an expected
  outcome on a machine that has had an unclean exit, not a bug to chase.
- The same leaked lock disables `_clear_stale_wal` outright, because recovery
  claims the guard exclusively. A store wedged by an unclean exit therefore
  cannot be recovered by haikode at all until a reboot — the guard model
  assumes a dead process's locks are released, and on Haiku they are not.
  Making the guard prove the holder is alive, rather than inferring it from
  the lock, is follow-up work this decision does not cover.
- **Downgrade re-enables WAL.** Every released v0.1.x runs an unconditional
  `PRAGMA journal_mode=WAL` on open, so installing an older build, or a
  non-packaged developer copy shadowing the installed one, silently converts
  the file back and reinstates the fault. No data is lost, but the reversal is
  invisible and the release notes must say so.
- Whether WAL's measured write advantage is real or an artefact of how this
  hardware honours `fsync` is unresolved; 0.34 ms per `synchronous=FULL` commit
  is fast enough to warrant doubt. It does not change the decision, which does
  not rest on write throughput.

## Reversal conditions

Return to WAL on Haiku when its shared-memory index is demonstrated to
coordinate correctly across processes — proven by rerunning both probes above
on the target build, not by an upstream changelog. Reconsider the mode choice
if commit latency proves unacceptable in use, in which case `TRUNCATE` with an
explicit per-connection pragma is the measured alternative.

Any replacement must keep: a conversion that runs only while provably alone, a
verified backup taken before it, a checked pragma return value, verified counts
after it, a failure path that leaves the store usable, and a guarded store at
every exit.
