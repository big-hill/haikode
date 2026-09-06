# Resume picker review and acceptance boundary

Status: implemented locally; physical-Haiku acceptance pending.

## Scope

An independent Astra review inspected the CLI startup, REPL/TUI session
adoption, forks, permission isolation, persistence and nearby agent lifecycle.
The review was based on current code, without live provider calls or hardware
access. This record is not release or deployment approval.

## Findings addressed

- `--resume` / `-r` opens the existing searchable TUI session dialog. The plain
  terminal fallback offers a numbered list; scripting uses `--session ID`.
- Resume restores saved provider/model/agent routing. Explicit startup options
  override it once; a provider-only override takes that provider's model.
- Each persisted turn records its actual model and agent. The additive
  `agent_name` column has an empty default for older sessions. Forks and JSON
  export/import preserve it; global configuration is not rewritten.
- Failed resume/fork does not replace the current TUI history or poison the
  next `/new`. Failed startup restoration/fork stops before any prompt runs.
- Resume/fork rebuilds execute off the curses thread. Their adoption cannot
  be cancelled midway while the REPL and TUI refer to different agents.
- Resume replaces temporary permission grants and closes the previous
  conversation's MCP/LSP resources. The displayed agent label follows the
  restored agent, and TUI agent selection updates the REPL command layer.
- Explicit-ID/latest startup reads the saved route before resolving default
  provider credentials. Database-read failures are reported without a traceback.

## Remaining improvements from the broader review

1. **Provider-independent picker startup.** Interactive `--resume` still builds
   the initial agent before entering the TUI. A broken default provider or
   stalled credential lookup can prevent reaching the picker. A separate
   selection phase should precede runtime construction. Explicit `--session`
   and `--continue` now resolve their saved route first.
2. **Agent-defined model changes.** `Agent._apply_agent_model()` assigns the
   model directly. It should coordinate context-window and reasoning-effort
   validation with `set_model()` without losing the base-model restoration
   semantics. This affects general agent switching, beyond resume.
3. **Uniform runtime disposal.** Resume now disposes its replaced resources;
   the older general `REPL._rebuild_agent()` path still needs an explicit
   lifecycle contract for MCP/LSP managers across provider/config changes.

## Verification

- macOS, system Python 3.9: full baseline ran 2541 tests with exactly the four
  documented wiring-audit failures, four skips, no other failures or errors.
- Nineteen new regression tests cover route restoration, grants, legacy schema
  migration and old-reader compatibility, explicit options, plain selection,
  UI adoption, resource disposal, and failed resume/fork/startup paths.
- Offline project preflight and `git diff --check` passed.
- Local PTY captures using `tests/render_tui.py` and a scripted provider showed
  the startup Sessions dialog and, after Enter, the saved transcript with
  `restored-model` and `plan` in the status line. No live model call was made.
- Physical Haiku checks are still required for the terminal picker, existing
  session database upgrade, concurrent sessions and real-provider continuation.
  Local tests do not establish that acceptance boundary.
