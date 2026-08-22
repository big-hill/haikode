# Spec: crash triage for your own projects

Status: proposed, not implemented. Target: `haikode/crash.py` (new, engine),
`desktop/src/` (BeAPI watcher), one config key.

Designed against measurements taken on the physical x86_64 machine on
2026-08-18, not against recollection of how Haiku behaves. An independent
design pass by Fable 5 is folded in; where this document disagrees with it,
it says so and why.

## The idea in one sentence

When something you are building crashes, haikode notices, says so once,
quietly, and can explain the stack trace against your own source — because
the evidence Haiku already writes is good, and today it rots on the desktop
unread.

## What Haiku actually does on a crash — measured

- `debug_server` writes a plain-text report to the **desktop directory**,
  named `<App>-<team>-debug-<DD-MM-YYYY-HH-MM-SS>.report`.
- The report holds: binary path and team id, CPU, memory, `Haiku revision`,
  every thread with its state, the exception (`Exception (Segment
  violation)`), a **symbolized** stack, register dumps and frame-memory
  hexdumps. About 25 kB.
- Reports carry **no BFS attributes** and are typed `text/plain`.
- `query "name=*-debug-*.report"` finds them volume-wide. The machine had
  15, including several in `/boot/trash`.
- `/var/log/syslog` separately carries `KERN: debug_server: Thread N entered
  the debugger: Segment violation` and `Killing team N (<path>)`, and
  rotates to `syslog.old`.
- `notification_server` runs; `notify` posts from the shell.

The reports are worth reading. Two real examples from that machine:

```
thread 4191 (Pippo (main)) - Exception (Segment violation)
    BApplication::Run() + 0x1c
    PippoApp::_HandleAppLaunched(BMessage*) + 0xa7

thread 4194 (http_accept) - Exception (Invalid opcode exception)
    HttpServer::_CleanupSession(std::string const&) + 0x2c8
    HttpServer::_CleanupSession(std::string const&) + 0x296
```

That is a third-party MCP server with two threads down at once and a
self-recursive cleanup path — an actionable bug report nobody had read. The
same machine also holds `haikode-36975-debug-04-08-2026`: **haikode's own
desktop app segfaulted in a thread called `haikode history loader`**, and
its maintainer never found out. That is the gap this feature fills.

## The load-bearing decision: your projects only

A general crash reporter — "anything on this machine crashed, want AI
help?" — is the wrong product. It fires for software the user did not
write and cannot fix, and it reads reports belonging to other people's
sessions on the machine.

So: **haikode reacts only to crashes of binaries inside a project root the
user has already trusted.** Everything else is stamped as seen and passed
over in silence, without being parsed beyond the binary-path line. That one
rule turns the feature from ambient surveillance into a developer tool, and
every other decision below follows from it.

## Architecture

### Detection: live BQuery, push, no polling

A live `BQuery` with predicate `name="*-debug-*.report"`, held open with a
`BMessenger` target, so matches arrive as `BMessage`s. The kernel pushes;
there is no timer, no thread, no tail. `/boot/trash` is excluded.

A directory node monitor on the desktop directory is the fallback if live
name-queries turn out not to deliver `B_ENTRY_CREATED` on the installed
hrev — that must be proven on hardware before this is built.

**Quiescing.** A 25 kB report is unlikely to be atomic. On the created
entry, watch `B_WATCH_STAT` until the size is stable for ~2 s, then stop.
Whether `debug_server` writes incrementally is an open question worth one
measurement; the logic is cheap either way.

**Syslog is deliberately not read.** It is the only polling-shaped source,
it rotates, and it says less than the report. One consequence must be
stated rather than hidden: crashes where the report fails to write are
invisible to this design. That happens — the same syslog showed `Failed to
write core file for team 151281: Bad port ID`. Those crashes will not be
triaged, and the feature must not pretend otherwise.

### Where the watcher lives — the one open disagreement

The request was for something that lives in Deskbar. Fable argued against a
replicant: a permanent tray widget for a rare event is visual debt, and the
notification is itself a Deskbar presence.

Both readings are defensible, and the trade-off is real:

- **Inside the desktop app** (Fable's choice): no new process, nothing to
  crash but haikode itself, and coverage only while haikode runs. Missed
  crashes are recovered by a silent catch-up query at launch.
- **A Deskbar replicant**: always-on coverage without a daemon, because
  Deskbar is already running — which is the honest Haiku answer to "watch
  continuously without a service process". The cost is sharp: a replicant
  executes **inside Deskbar's team**, so a bug in the crash watcher takes
  the user's Deskbar down with it. For a feature whose subject is crashes,
  that is not an acceptable default.

Recommendation: **start inside the desktop app.** If coverage while haikode
is closed proves to matter in use, add an opt-in replicant later that does
nothing but hold the query and post the notification, with the parser and
everything else out of Deskbar's address space.

### The pipeline

1. Query fires; file quiesces.
2. **Local, deterministic parse** in the engine (Python 3.10 stdlib, shared
   by all front ends). No model involved, no network.
3. Binary path outside a trusted project → stamp as seen, silence. This is
   the common case.
4. Inside a trusted project → **one** `BNotification`, information class,
   group "haikode": *"Pippo crashed (Segment violation). Click to
   investigate."* Never important-class, never a `BAlert`, never raising a
   window. Haiku's own Notifications preferences can then mute haikode like
   any other app.
5. Click opens haikode on that project with a **previewed digest** staged as
   the prompt. Nothing has left the machine.
6. The user presses send, or does not.

The crash already produces Haiku's own alert. haikode must add **no second
interruption at crash time** — its contribution is a quiet line the user
comes to when ready.

### The digest: whitelist, never blacklist

Frame-memory hexdumps are raw process memory. A crashed program's stack can
hold whatever it was holding — a password, a token, a customer's document.
So the digest is built by naming what goes in, never by trying to scrub what
comes out.

**Kept:** binary path relativised to the project root, `Haiku revision` and
architecture, the exception and its fault address if present, the crashing
thread's symbolized stack, other threads' names and top frames only when the
crashing stack is entirely unresolved, and the crash count when coalesced.

**Never sent:** frame memory, register dumps, environment and argument
sections, CPU model and memory totals, and anything at all from a report
outside a trusted project.

Measured on the real Pippo report: **24 974 bytes in, 834 bytes out —
3.3%**, with no register or memory dump carried through. The digest was also
*more* useful than the report's own head, which showed only the main thread;
the second crashed thread appeared only after extraction.

The preview shown to the user **is** the wire content, byte for byte — not a
summary of it.

### Diagnosis

With `PippoApp::_HandleAppLaunched(BMessage*)` in hand, haikode does what it
already does: grep the project, read the function, reason about the path.
Existing tools, existing permission gates, no new capabilities.

Unresolved frames (`<binary> + 0x1de70`) could later be resolved with
`addr2line` against the project's own binary — an ordinary, previewed shell
call. Worth doing; not worth blocking on.

Pippo's screenshot and window tools stay out of this flow. A screenshot at
diagnosis time shows the desktop, not the crash, and drags in the exact
privacy surface the digest walls off.

## State: BFS attributes, with one caveat

After handling, write `HAIKODE:status` (`new`/`triaged`/`diagnosed`),
`HAIKODE:app` and `HAIKODE:exception` onto the report file. The filesystem
becomes the index: restarts are idempotent, an attributed file is never
re-notified, and the user can query their own crash history with `query`.
This is the most Haiku-native part of the design.

The caveat Fable's proposal does not raise: **this writes to files the user
owns, without asking.** The project's contract says mutations are previewed
and permission-checked. Attribute stamps on a crash report are about as
benign as a mutation gets, and the alternative — a private seen-list in
haikode's config directory — throws away the elegance and the queryability.
The maintainer should decide knowingly rather than discover it later.

## Not pestering: the rules

| Failure mode | Rule |
|---|---|
| Notifying about software the user did not write | Trusted-project filter runs before any visible effect |
| Reading other people's crash data | Non-project reports are never parsed past the path line |
| Crash-loop storm | Per-binary cooldown; the same notification is *replaced* via `SetMessageID`, so thirty crashes are one ever-current line |
| Backlog nag at launch | Catch-up is silent; old crashes appear only in an in-app list |
| Nagging about an ignored crash | `HAIKODE:status` stamp; one notification per report, ever |
| Silent exfiltration or token spend | No network without an explicit send on a byte-identical preview |
| haikode crash recursion | haikode's own binaries excluded from the notify path |
| Resource creep | No daemon, no polling, no syslog tail; steady state is one kernel query and zero threads |
| User wants it gone | One checkbox, or mute haikode in Haiku's Notifications preferences |
| Reports littering the desktop | Never auto-deleted; the diagnosis window may *offer* to trash a handled report, previewed like any mutation |

## Setup

The watcher is on by default and needs nothing, because everything it does
by itself is local: kernel query, local parse, one notification, attribute
stamps. The privacy-relevant step — sending — is never automatic and is not
configurable to be automatic. That split is what lets the default be "on"
without the default being telemetry.

One engine-owned key, `crash_watch: on | off`, surfaced as a single checkbox
in the desktop Settings window. Per-project opt-out rides the existing
project-config layer, which may only narrow. No launch daemon unit, no
wizard, no second settings file.

## Haiku-native touches worth having

- **A real file type.** Register `text/x-vnd.haiku-debugreport` with a
  sniffer rule and a published attribute layout. Tracker then shows app and
  exception as columns, and **dropping any `.report` on a haikode window**
  runs the same digest flow — which is also the answer for the 15 reports
  already on that disk, and for a report copied from another machine.
- **BMessage all the way.** Query updates, node-monitor events, notification
  clicks and refs all arrive in the existing `HaiApplication` looper. The
  feature is a message filter, not a subsystem.

## Open questions for the maintainer

1. **Deskbar or not** — the request said Deskbar; this spec recommends
   starting inside the desktop app because a replicant crash takes Deskbar
   with it. Confirm or overrule.
2. **Live-query delivery on the installed hrev** — does a live `BQuery` on a
   `name` predicate deliver `B_ENTRY_CREATED`? One afternoon on hardware
   decides whether the node-monitor fallback is needed.
3. **Attribute stamping without asking** — acceptable, or keep the seen-list
   private to haikode's own config directory?
4. **Default on or off** — this spec argues on, since nothing leaves the
   machine without a click.
5. **Membership test** — is "binary inside a trusted project root" enough,
   or must binaries *built from* a project but installed elsewhere (as Pippo
   is, under `/boot/system/non-packaged/apps/`) also match? Note that the
   motivating example itself fails the simple test.
6. **MIME retyping** — may haikode retype `debug_server`'s files away from
   `text/plain`, or only add attributes? Upstreaming the type to Haiku is
   the better long-term answer.
7. **CLI parity** — `haikode crash <file>` in v1, or desktop-first with the
   engine API in place?
