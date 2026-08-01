# Contributing

## The one constraint that shapes everything

**Python 3.10, standard library only.** No pip, no vendored packages, no
optional imports that quietly enable a feature. Haiku has no usable pip
ecosystem, and a dependency is a thing that has to build there — which is the
whole reason this project exists rather than a wrapper around something else.

A patch that adds a dependency will be declined however good it is. If you
need one, open an issue first and say what it buys; there may be a stdlib
route, or the feature may be worth living without.

## First, turn the pre-push hook on

    git config core.hooksPath scripts/hooks

It refuses a push that would publish a private LAN or Tailscale address, an
API key, your machine's name, or a commit authored under a personal email
rather than the project's. Please also give the clone its own identity:

    git config user.name haikode
    git config user.email haikode@localhost

This is not hypothetical tidiness. Both of those got into this repository
and had to be scrubbed out of the history afterwards: a test written with a
real machine's addresses instead of documentation ones, and — because the
clone had no identity of its own — commits carrying a laptop's host name.
Both were caught by a scan run *after* the push. The hook runs it before.

It looks up the machine's own name at run time rather than storing anything,
so the hook file itself is publishable. `git push --no-verify` skips it.

## Before a pull request

Run the suite:

    python3 -m unittest discover -s tests -t . -p "test_*.py"

Expected: **13 failures, all in `tests/test_wiring_audit.py`** — see the
README for why. Anything else is yours.

New behaviour needs a test that fails before your change and passes after.
Say in the PR that you checked that, because it is the difference between a
test that documents the fix and one that documents nothing.

If you fix one of the wiring-audit gaps, its test should pass without the
test being changed. That file is a specification, not a scoreboard.

## Style

Match what is already there rather than a linter's opinion:

- type hints on public functions
- docstrings that explain **why**, not what — the what is readable from the
  code
- comments only where the reason is not obvious, and then the reason, not a
  restatement
- no emoji, ASCII by default in source
- the prompt files under `haikode/prompts/` and the guidance blocks in
  `haikode/prompt.py` are code: a wording change there changes the model's
  behaviour and needs the same care and the same regression test

## Testing on Haiku

Much of this can be developed anywhere, but anything touching the terminal,
the filesystem or the keystore has to be proven on real Haiku. Behaviour has
diverged there in ways no amount of reasoning predicted: `dim` is not
rendered, `A_DIM`-based hierarchy vanishes, SQLite reports "locking protocol"
under concurrency, and a stable file has been observed returning an empty
read. If you cannot test on Haiku, say so in the PR — it is not
disqualifying, it just tells the maintainer what still needs checking.

`tests/render_tui.py` drives the real curses TUI through a pty and returns the
screen, which is how to test terminal output without a person watching it.

## AI-assisted contributions

This project was largely written with AI assistance and says so in the README,
so there is no double standard here: patches written with an agent are
welcome. The bar is the same as for anything else — you understand what it
does, it is tested, and you can defend it in review.

What is not welcome is a patch nobody has read. It is usually obvious, and it
costs the reviewer more than writing the change would have.

## Security

Do not open a public issue for a flaw in permissions, secret redaction, the
repository trust boundary, or credential handling. Use the private advisory
link on the issue page. haikode holds OAuth tokens and runs shell commands;
those bugs deserve a fix before an audience.
