"""
Token and context accounting.

The user asked for "oversikt over token og kontekst". opencode shows this next
to the prompt — packages/tui/src/component/prompt/index.tsx computes the share
as (input + output + reasoning + cache.read + cache.write) / model.limit.context
— so the same numbers are produced here, as data, and formatted as plain
strings. Deliberately pure: no curses, no printing, nothing that raises, so the
TUI can call measure_context() while drawing a frame.

Counts are estimates until the provider reports real usage. Haiku has no
tokenizer available, so context.estimate_tokens (~4 chars per token) covers
what has not been billed yet, while UsageTracker keeps whatever the provider
actually charged for.
"""

import json
from dataclasses import dataclass, replace
from typing import Any, Dict, List, Optional, Union

from .context import estimate_tokens, message_tokens

# Percent of the window at which the UI should start warning / shouting.
WARN_PERCENT = 60.0
CRITICAL_PERCENT = 85.0

# Meter glyphs, ordered by ink so the bar still reads on a monochrome vt100
# where the TUI cannot colour it: '#' < '%' < '@' in an ASCII density ramp.
BAR_FILL = {"ok": "#", "warn": "%", "critical": "@"}
BAR_EMPTY = "-"
BAR_MIN_WIDTH = 3  # "[x]" is the smallest meter that still means something

LABEL_WIDTH = 15  # widest label in detail_lines is "System prompt:"

# Provider payloads are not uniform: the OpenAI adapter sends prompt/completion,
# Anthropic sends input/output plus cache creation and read counters, and
# opencode nests the cache pair. Every spelling maps onto one field.
_ALIASES = {
    "input_tokens": ("input", "input_tokens", "prompt_tokens"),
    "output_tokens": ("output", "output_tokens", "completion_tokens"),
    "reasoning_tokens": ("reasoning", "reasoning_tokens"),
    "cache_read": ("cache_read", "cache_read_tokens", "cache_read_input_tokens"),
    "cache_write": ("cache_write", "cache_write_tokens", "cache_creation",
                    "cache_creation_input_tokens", "cache_write_input_tokens"),
}


def _safe_int(value: Any) -> int:
    """Never trust a provider counter: junk and negatives become 0."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def _safe_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    # NaN and infinities would poison every sum they touch.
    if number != number or number in (float("inf"), float("-inf")):
        return 0.0
    return number if number > 0 else 0.0


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """getattr that also survives a property raising.

    getattr's default only absorbs AttributeError, but measure_context runs
    from the draw loop against half-built agents; a raising property must not
    take the screen down.
    """
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


# --------------------------------------------------------------------------
# usage
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """One billing snapshot. Accumulated by replacement, never in place, so a
    tracker can hand the same object to the UI without it changing underneath.

    Frozen so that promise is enforced rather than merely documented: the
    tracker returns its live counters, and a caller poking a field would
    silently corrupt the session total.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost: float = 0.0

    @property
    def total(self) -> int:
        """What opencode's prompt footer counts against the context limit."""
        return (self.input_tokens + self.output_tokens + self.reasoning_tokens
                + self.cache_read + self.cache_write)

    def add(self, other: Optional["Usage"]) -> "Usage":
        """Sum of both, as a new Usage. Neither operand is touched."""
        if other is None:
            return replace(self)
        if not isinstance(other, Usage):
            # Silently returning self would hide a miswired caller's tokens.
            raise TypeError("Usage.add expects a Usage, got %s"
                            % type(other).__name__)
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
            cost=self.cost + other.cost,
        )

    def __add__(self, other: Optional["Usage"]) -> "Usage":
        if other is not None and not isinstance(other, Usage):
            return NotImplemented
        return self.add(other)

    def __radd__(self, other: Any) -> "Usage":
        # sum() seeds with 0, so folding a list of Usage needs this.
        if other is None or (isinstance(other, int) and other == 0):
            return replace(self)
        return NotImplemented

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "Usage":
        """Parse a provider-shaped usage dict; unknown keys are ignored."""
        if not isinstance(raw, dict):
            return cls()
        values = {field: 0 for field in _ALIASES}
        for field, names in _ALIASES.items():
            for name in names:
                if name in raw:
                    values[field] = _safe_int(raw[name])
                    break
        # opencode nests the cache pair as {"cache": {"read": n, "write": n}}.
        cache = raw.get("cache")
        if isinstance(cache, dict):
            values["cache_read"] = values["cache_read"] or _safe_int(cache.get("read"))
            values["cache_write"] = values["cache_write"] or _safe_int(cache.get("write"))
        return cls(cost=_safe_float(raw.get("cost")), **values)


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------


@dataclass
class ContextState:
    """How full the next request would be.

    used/window/tools/system/history are token counts; `messages` is the number
    of conversation messages behind `history`, which the status dialog reports
    alongside it.
    """

    used: int = 0
    window: int = 0
    messages: int = 0
    tools: int = 0
    system: int = 0
    history: int = 0

    @property
    def percent(self) -> float:
        """Share of the window used. An unknown window is 0, not a division
        error.

        Not capped at 100: opencode's prompt footer is a plain
        Math.round(tokens / limit * 100), and a history that has outgrown the
        window before compaction must say so rather than sit at a reassuring
        100%. Meters clamp themselves — see context_bar.
        """
        if self.window <= 0:
            return 0.0
        return max(0.0, self.used * 100.0 / self.window)

    @property
    def remaining(self) -> int:
        if self.window <= 0:
            return 0
        return max(0, self.window - self.used)

    @property
    def pressure(self) -> str:
        percent = self.percent
        if percent >= CRITICAL_PERCENT:
            return "critical"
        if percent >= WARN_PERCENT:
            return "warn"
        return "ok"


def _system_tokens(agent: Any) -> int:
    """The assembled system message if the agent can build one, else the raw
    prompt. Assembly walks the filesystem for AGENTS.md, so it may fail."""
    builder = _attr(agent, "_system_message", None)
    if callable(builder):
        try:
            built = builder()
        except Exception:
            built = None
        if isinstance(built, str):
            return estimate_tokens(built)
        if built is not None:
            try:
                return message_tokens(built)
            except Exception:
                pass
    prompt = _attr(agent, "system_prompt", "")
    return estimate_tokens(prompt) if isinstance(prompt, str) and prompt else 0


def _spec_tokens(spec: Any) -> int:
    """Tool schemas are resent on every request, so they are not free."""
    name = _attr(spec, "name", "") or ""
    description = _attr(spec, "description", "") or ""
    parameters = _attr(spec, "parameters", None)
    try:
        rendered = json.dumps(parameters, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(parameters)
    return estimate_tokens("%s%s%s" % (name, description, rendered))


def _tool_specs(agent: Any) -> List[Any]:
    """Agent.specs normally; a bare tools mapping is accepted as well."""
    specs = _attr(agent, "specs", None)
    # A mapping of specs must yield the specs, not its keys -- list(dict)
    # would hand back names and price every tool schema at one token.
    if isinstance(specs, dict):
        specs = list(specs.values())
    if specs:
        try:
            return list(specs)
        except Exception:
            return []
    tools = _attr(agent, "tools", None)
    if isinstance(tools, dict):
        return [tool for tool in tools.values() if hasattr(tool, "parameters")]
    return []


def _loose_tokens(message: Any) -> int:
    """Fallback for anything message_tokens cannot read.

    A dict- or string-shaped message still occupies real context, so its text
    is measured; dropping to a bare token would make a restored or wire-shaped
    history look nearly free.
    """
    try:
        if isinstance(message, str):
            text = message
        elif isinstance(message, dict):
            text = str(message.get("content") or "")
        else:
            content = _attr(message, "content", None)
            text = content if isinstance(content, str) else str(message)
        return estimate_tokens(text) + 4  # same per-message overhead as message_tokens
    except Exception:
        return 0


def measure_context(agent: Any) -> ContextState:
    """Latest provider-measured exchange, or a pre-response local estimate.

    Every component degrades to 0 rather than raising: this runs from the draw
    loop, and a half-built agent must not take the screen down.
    """
    if agent is None:
        return ContextState()

    system = _system_tokens(agent)

    tools = 0
    for spec in _tool_specs(agent):
        try:
            tools += _spec_tokens(spec)
        except Exception:
            continue

    history = 0
    count = 0
    messages = _attr(agent, "messages", None)
    try:
        iterator = [] if messages is None else list(messages)
    except Exception:
        iterator = []
    for message in iterator:
        count += 1
        try:
            history += message_tokens(message)
        except Exception:
            history += _loose_tokens(message)

    estimated = system + tools + history
    tracker = _attr(agent, "usage", None)
    latest = _attr(tracker, "latest", None)
    observed = latest.total if isinstance(latest, Usage) else 0
    window = _safe_int(_attr(agent, "context_window", 0))
    return ContextState(used=observed or estimated, window=window,
                        messages=count, tools=tools, system=system,
                        history=history)


# --------------------------------------------------------------------------
# tracking
# --------------------------------------------------------------------------


class UsageTracker:
    """Per-run and per-session token totals.

    The run counter is what the spinner reports for the turn in flight; the
    session counter is what the footer and /status report for the whole
    conversation. Both see every record() — only the run counter is reset.
    """

    def __init__(self):
        self._run = Usage()
        self._session = Usage()
        self._latest = Usage()

    @property
    def run(self) -> Usage:
        return self._run

    @property
    def session(self) -> Usage:
        return self._session

    @property
    def latest(self) -> Usage:
        """The provider's latest request/response size, used by the context meter."""
        return self._latest

    def start_run(self):
        """Begin a new turn. The session total keeps everything recorded so far."""
        self._run = Usage()

    def record(self, usage: Union[Dict[str, Any], Usage, None]) -> Usage:
        """Fold one provider usage payload into both counters; returns the delta.

        An already-parsed Usage is taken as-is: from_dict would reject it as
        "not a dict" and record a silent zero.
        """
        delta = usage if isinstance(usage, Usage) else Usage.from_dict(usage)
        self._run = self._run.add(delta)
        self._session = self._session.add(delta)
        self._latest = delta
        return delta

    def invalidate_latest(self):
        """Drop the last exchange size so the context meter re-estimates.

        A `latest` recorded under one provider/model is meaningless against
        another's window (120k observed vs a 32k window reads as 366%);
        provider or model switches call this and the meter estimates until
        the next real exchange.
        """
        self._latest = Usage()

    def reset(self):
        """Forget everything — used when the session is cleared or switched."""
        self._run = Usage()
        self._session = Usage()
        self._latest = Usage()

    def estimate_cost(self, pricing: Optional[Dict[str, Any]],
                      usage: Optional[Usage] = None) -> float:
        """Cost in dollars for `usage` (the session by default).

        `pricing` is per million tokens, as models.dev publishes it. Unknown
        pricing returns 0.0: a wrong number on screen is worse than none, so
        nothing is ever inferred from a rate we were not given. Reasoning is
        billed at the output rate, matching opencode's getUsage().
        """
        if not isinstance(pricing, dict):
            return 0.0
        rates = {key: _safe_float(pricing.get(key))
                 for key in ("input", "output", "cache_read", "cache_write")}
        # models.dev publishes cache_read/cache_write flat, but opencode's
        # Provider.Model normalises them to cost.cache.{read,write}.
        cache = pricing.get("cache")
        if isinstance(cache, dict):
            rates["cache_read"] = rates["cache_read"] or _safe_float(cache.get("read"))
            rates["cache_write"] = rates["cache_write"] or _safe_float(cache.get("write"))
        if not any(rates.values()):
            return 0.0

        totals = usage if usage is not None else self._session
        cost = (totals.input_tokens * rates["input"]
                + totals.output_tokens * rates["output"]
                + totals.reasoning_tokens * rates["output"]
                + totals.cache_read * rates["cache_read"]
                + totals.cache_write * rates["cache_write"])
        return cost / 1_000_000.0


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------


def format_tokens(count: Any) -> str:
    """Compact token count: 985, 1.2k, 128k, 1.4M.

    A whole unit drops its ".0" so a 128000-token window reads as the "128k"
    everyone writes it as.
    """
    try:
        value = int(count)
    except (TypeError, ValueError, OverflowError):
        return "0"
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value < 1000:
        return sign + str(value)

    scaled = value / 1000.0
    unit = "k"
    if scaled >= 999.95:  # would print as "1000.0k"
        scaled /= 1000.0
        unit = "M"
    text = "%.1f" % scaled
    if text.endswith(".0"):
        text = text[:-2]
    return sign + text + unit


def format_cost(cost: float) -> str:
    """Sub-cent runs still deserve a number, so they get more decimals."""
    value = _safe_float(cost)
    if 0 < value < 0.01:
        return "$%.4f" % value
    return "$%.2f" % value


def _percent_text(state: ContextState) -> str:
    # Half-up: Python's round() is banker's rounding, and 0.5% must not vanish.
    return "%d%%" % int(state.percent + 0.5)


def format_context(state: ContextState) -> str:
    """"12.3k/128k (10%)", or just the used count when the limit is unknown."""
    used = format_tokens(state.used)
    if state.window <= 0:
        return used
    return "%s/%s (%s)" % (used, format_tokens(state.window), _percent_text(state))


def context_bar(state: ContextState, width: int = 12) -> str:
    """An ASCII meter of exactly `width` columns, brackets included.

    Fill is floored: the bar only shows a cell once it is genuinely used, and
    only fills the last cell at 100%.
    """
    try:
        width = int(width)
    except (TypeError, ValueError):
        return ""
    if width < BAR_MIN_WIDTH:
        return ""
    cells = width - 2
    filled = int(cells * state.percent / 100.0)
    filled = max(0, min(cells, filled))
    glyph = BAR_FILL.get(state.pressure, BAR_FILL["ok"])
    return "[" + glyph * filled + BAR_EMPTY * (cells - filled) + "]"


def summary_line(tracker: Optional[UsageTracker],
                 state: Optional[ContextState]) -> str:
    """One footer line: "12.3k/128k (10%) - 4.1k in / 892 out".

    Session totals, not the run: the footer is always on screen, and a run
    counter would sit at "0 in / 0 out" between turns.
    """
    usage = tracker.session if tracker is not None else Usage()
    parts = [format_context(state if state is not None else ContextState()),
             "%s in / %s out" % (format_tokens(usage.input_tokens),
                                 format_tokens(usage.output_tokens))]
    if usage.cost > 0:
        parts.append(format_cost(usage.cost))
    return " - ".join(parts)


def _row(label: str, value: str) -> str:
    return "%-*s %s" % (LABEL_WIDTH, (label + ":") if label else "", value)


def _plural(count: int, word: str) -> str:
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def detail_lines(tracker: Optional[UsageTracker],
                 state: Optional[ContextState]) -> List[str]:
    """The breakdown behind the context dialog, one field per line."""
    run = tracker.run if tracker is not None else Usage()
    session = tracker.session if tracker is not None else Usage()
    state = state if state is not None else ContextState()

    known = state.window > 0
    rows = [
        _row("Window", format_tokens(state.window) if known else "(unknown)"),
        # No window means no share: "(0%)" next to "Window: (unknown)" reads as
        # an empty context rather than an unmeasurable one.
        _row("Used", "%s (%s)" % (format_tokens(state.used), _percent_text(state))
             if known else format_tokens(state.used)),
        _row("Remaining", format_tokens(state.remaining) if known else "(unknown)"),
        _row("Pressure", state.pressure),
        _row("System prompt", format_tokens(state.system)),
        _row("Tool schemas", format_tokens(state.tools)),
        _row("Conversation", "%s (%s)" % (format_tokens(state.history),
                                          _plural(state.messages, "message"))),
        _row("Run", "%s in / %s out" % (format_tokens(run.input_tokens),
                                        format_tokens(run.output_tokens))),
        _row("Session", "%s in / %s out" % (format_tokens(session.input_tokens),
                                            format_tokens(session.output_tokens))),
    ]
    if session.reasoning_tokens:
        rows.append(_row("Reasoning", format_tokens(session.reasoning_tokens)))
    if session.cache_read or session.cache_write:
        rows.append(_row("Cache", "%s read / %s written" % (
            format_tokens(session.cache_read), format_tokens(session.cache_write))))
    if session.cost > 0:
        rows.append(_row("Cost", format_cost(session.cost)))
    return rows


__all__ = ["Usage", "ContextState", "UsageTracker", "measure_context",
           "format_tokens", "format_cost", "format_context", "context_bar",
           "summary_line", "detail_lines", "WARN_PERCENT", "CRITICAL_PERCENT"]
