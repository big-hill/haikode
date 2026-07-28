# haikode — working agreements

This is the haikode source tree: an opencode-derived coding agent that runs
natively on Haiku OS. Python 3.10, **stdlib only** — no pip, no YAML library.
The package is `haikode/`, the tests are `tests/`.

## What we are building

The goal: a functionally 1:1 opencode experience native on Haiku — TUI first,
a BeAPI desktop app second — standalone, no server, talking directly to cloud
providers (ChatGPT subscription via device OAuth, SuperGrok, Ollama Cloud,
zen). The reference implementation is opencode (`sst/opencode`); when a design
question comes up, look at what opencode does before inventing something.
Divergences are allowed but deliberate: document why.

Who you work with, language and tone live in the global AGENTS.md
(`~/config/settings/haikode/AGENTS.md` on the Haiku machine). In short:
answer the maintainer in their own language; everything in this tree stays in English.

The bar for "done" is explicit: a feature does not exist until it is proven
working on the Haiku machine — the suite green, and the behaviour shown
end-to-end. "The code looks right" has repeatedly turned out to be false in
this project; the field session that shaped the current fixes failed five
times on things that "looked right".

## Source of truth

The Mac tree (`the development checkout`) is the source of truth. `~/haikode` on
the Haiku machine is a deploy target: anything edited directly there can be
overwritten by the next deploy. If you change code on the Haiku box, say so
explicitly at the end of the session so the change can be ported back.

## Verification

Run the full suite before calling any change done:

    python3 -m unittest discover -s tests -t . -p "test_*.py"

Expected baseline: **13 failures, all in `tests/test_wiring_audit.py`** — they
are executable bug reports for subsystems that exist but are not wired up yet
(MCP, LSP, skills catalogue, automatic compaction). Any other red test is a
regression you introduced. Fixing an underlying gap may turn an audit test
green; never weaken that file to get there.

To see the real TUI without restarting the app, use the pty harness:

    python3 tests/render_tui.py --rows 24 --cols 80 --settle 1.0 --timeout 10 \
      --keys "1.2:/help\r" -- python3 -m haikode -p zen

`--keys` delays are absolute from run start, not relative; `\x1b` escapes are
accepted. The `zen` provider is free and needs no key — use it for E2E runs.

## Editing the app you are running in

Your own process keeps the modules it loaded at start: source edits take
effect on the *next* start, and config edits need `/reload` or a restart. A
broken edit breaks the next launch of the very tool you are using — make
small verified steps, run the suite before finishing, and remember `/undo`
keeps per-turn file snapshots.

## Hard rules on the Haiku machine

- Never launch the BeAPI desktop GUI — it opens on the owner's physical
  screen.
- The keystore helper binary stays named `hai-keystore`: Haiku ties keyring
  approval to the binary's path and signature; renaming it re-triggers the
  physical approval dialog and orphans stored keys.
- Bind services to loopback only, never 0.0.0.0.
- Kill every process you start and prove it with `ps` before finishing.

## Style

Match the surrounding code: type hints on public functions, docstrings that
explain why, comments only where the reason is non-obvious, no emoji. The
prompt files under `haikode/prompts/` and the guidance blocks in
`haikode/prompt.py` are code: a wording change there changes the model's
behaviour and needs the same care and regression tests as a function change.
