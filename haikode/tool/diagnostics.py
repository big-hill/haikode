"""
LSP diagnostics for edit/write/apply_patch results.

opencode reports diagnostics right after a file changes so the model sees the
error it just introduced without having to run a build. Haiku almost never has
a language server installed, so every path through here has to be a silent
no-op when there is nothing to talk to.

Three rules, in order of importance:

1. never turn a successful edit into a failure — every exception is swallowed
2. never block the UI — the whole lookup is bounded by DIAGNOSTICS_BUDGET
   seconds, enforced from outside the call so a wedged server cannot exceed it
3. opt out by leaving `ctx.lsp` unset (or setting it to None/False)
"""

import threading
from typing import Any, List, Optional

DIAGNOSTICS_BUDGET = 2.0


def _manager(ctx: Any):
    """The LSPManager to use, or None when diagnostics are switched off."""
    manager = getattr(ctx, "lsp", None)
    if not manager:
        return None
    if not hasattr(manager, "diagnostics"):
        return None
    return manager


def _call_with_budget(fn, budget: float):
    """
    Run `fn()` on a daemon thread and give up after `budget` seconds.

    LSPManager.diagnostics() already bounds its *wait* for a publish, but
    client_for() may spawn and hand-shake a server on first use, which has no
    such bound. Joining a daemon thread bounds our side of it unconditionally:
    if the server is slow we return nothing and the process still exits.
    """
    box: List[Any] = []

    def target():
        try:
            box.append(fn())
        except Exception:
            pass

    thread = threading.Thread(target=target, daemon=True,
                              name="haikode-lsp-diagnostics")
    thread.start()
    thread.join(budget)
    return box[0] if box else None


def diagnostics_block(ctx: Any, path: Any,
                      budget: float = DIAGNOSTICS_BUDGET) -> str:
    """
    Formatted diagnostics for one file, or "" when there is nothing to say.

    Returns "" for: diagnostics disabled, no server installed, server too slow,
    server broken, or a clean file. The caller never has to check anything.
    """
    manager = _manager(ctx)
    if manager is None:
        return ""
    if getattr(ctx, "aborted", False):
        return ""

    budget = max(0.0, float(budget))
    target = str(path)

    def lookup() -> str:
        # Prefer the manager's own formatter (it renders workspace-relative
        # paths); fall back to raw diagnostics + the module formatter.
        report = getattr(manager, "report", None)
        if callable(report):
            return report(target, wait=budget) or ""
        diags = manager.diagnostics(target, wait=budget) or []
        if not diags:
            return ""
        from ..lsp import format_diagnostics
        return format_diagnostics(diags, target)

    try:
        result = _call_with_budget(lookup, budget + 0.5)
    except Exception:
        return ""
    return result if isinstance(result, str) else ""


def append_diagnostics(ctx: Any, path: Any, output: str,
                       label: Optional[str] = None,
                       budget: float = DIAGNOSTICS_BUDGET) -> str:
    """`output` with an LSP block appended, or `output` unchanged."""
    block = diagnostics_block(ctx, path, budget=budget)
    if not block:
        return output
    name = label if label is not None else str(path)
    return "%s\n\nLSP errors detected in %s, please fix:\n%s" % (
        output, name, block)
