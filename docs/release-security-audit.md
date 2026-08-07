# v0.1.0 release security audit

**Audit date:** 2026-08-07

**Scope:** current tracked and untracked files, ignored build/runtime output,
all reachable Git history, commit identities, remotes, package contents,
licensing and provenance.

## Result

The source tree is ready for a private v0.1.0 release and a later explicit
public-visibility decision. The repository must remain private until that
decision is made.

## Evidence

- The project privacy scanner found no publishable identity, private-address
  or secret finding in the full reachable history.
- Gitleaks completed a full-history scan with no unallowlisted findings. The
  exact fingerprints in `.gitleaksignore` are synthetic credential canaries
  used by the redaction and benchmark tests; a new finding still fails.
- The broader current-tree audit also inspected ignored files. Its matches
  were classified as those same synthetic canaries, scanner patterns,
  bytecode, ignored benchmark output or local-only Git configuration. None is
  tracked release material.
- Tracked files contain no HPKG, build tree, benchmark result, session store,
  OAuth state, cache or bytecode artifact.
- Repository-local author identity and the pre-push privacy hook are active.
  Remote configuration contains no embedded password and is not packaged.
- MIT licensing and opencode-derived prompt/design attribution are present;
  the HPKG staging script includes both `README.md` and `LICENSE`.

No credential value, private address or local account name is reproduced in
this report.
