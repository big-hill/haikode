"""
Provider contract plus the one error shape every dialect maps onto.

A provider never raises out of ``stream()``: the agent loop consumes an
iterator, and an exception there would take the whole run down. Instead a
failure arrives as a terminal ``CompletionChunk(stop_reason="error")`` whose
``usage["error"]`` carries a :class:`ProviderError` as a plain dict.
"""
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from ..net import NetError
from ..schema import CompletionChunk, Msg, ToolSpec

#: The closed set of kinds the UI may switch on.
ERROR_KINDS = ("auth", "rate_limit", "context_overflow", "model_not_found",
               "content_filter", "server", "unknown")

#: Raw provider bodies are kept for debugging but never dumped whole.
MAX_BODY = 500

# Errors used to be smuggled into CompletionChunk.text, where agent.py folds
# them into the assistant message and replays them to the model next turn as
# if it had said them. usage["error"] is now the authoritative channel; the
# text line is kept because repl.py prints it and desktop_worker.py detects
# provider failures by its "[stream error]" prefix. Flip this off in the same
# commit that teaches those two to read chunk.usage["error"].
ERROR_TEXT_COMPAT = True
ERROR_TEXT_MARKER = "[stream error]"


@dataclass
class ProviderError:
    """One structured failure, renderable without reading a traceback."""

    kind: str = "unknown"
    message: str = ""
    retryable: bool = False
    status: Optional[int] = None
    body: str = ""          # raw provider payload, truncated
    provider: str = ""
    model: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "message": self.message,
                "retryable": self.retryable, "status": self.status,
                "body": self.body, "provider": self.provider,
                "model": self.model}

    def __str__(self) -> str:
        return self.message or self.kind


# --------------------------------------------------------------------------
# Context overflow detection
# --------------------------------------------------------------------------
# Every provider words this differently and most report it as a plain 400, so
# the only portable signal is the message text. Patterns lifted from
# opencode's provider-error.ts, which collected them the hard way.

_OVERFLOW_PATTERNS = [re.compile(p, re.I) for p in (
    r"prompt is too long",
    r"request_too_large",
    r"input is too long for requested model",
    r"exceeds the context window",
    r"exceeds (?:the )?(?:model'?s )?maximum context length",
    r"input token count.*exceeds the maximum",
    r"tokens in request more than max tokens allowed",
    r"maximum prompt length is \d+",
    r"reduce the length of the messages",
    r"maximum context length is \d+ tokens",
    r"exceeds (?:the )?maximum allowed input length",
    r"is longer than the model'?s context length",
    r"exceeds the available context size",
    r"greater than the context length",
    r"context window exceeds limit",
    r"exceeded model token limit",
    r"context[_ ]length[_ ]exceeded",
    r"request entity too large",
    r"context length is only \d+ tokens",
    r"input length.*exceeds.*context length",
    r"prompt too long",
    r"too large for model with \d+ maximum context length",
    r"but the configured context size is",
    r"model_context_window_exceeded",
    r"too many tokens",
    r"token limit exceeded",
    r"string_above_max_length",
)]

# "rate limit" wording sometimes mentions tokens; never read that as overflow.
_OVERFLOW_EXCLUSIONS = [re.compile(p, re.I) for p in (
    r"^(throttling error|service unavailable):",
    r"rate limit",
    r"too many requests",
    r"quota",
)]


def is_context_overflow(text: str) -> bool:
    if not text:
        return False
    if any(p.search(text) for p in _OVERFLOW_EXCLUSIONS):
        return False
    return any(p.search(text) for p in _OVERFLOW_PATTERNS)


# --------------------------------------------------------------------------
# Payload shapes
# --------------------------------------------------------------------------

def _as_json(raw: Any) -> Optional[dict]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_error_payload(raw: Any) -> Dict[str, str]:
    """Pull (message, code, type) out of whichever dialect this body is.

    OpenAI/Zen  {"error": {"message":..., "type":..., "code":...}}
    Anthropic   {"type":"error","error":{"type":"overloaded_error","message":...}}
    Ollama      {"error": "model 'x' not found"}
    Gemini      {"error": {"code":404,"message":...,"status":"NOT_FOUND"}}
    FastAPI     {"detail": "..."}
    """
    body = _as_json(raw)
    if body is None:
        return {"message": raw.strip() if isinstance(raw, str) else "",
                "code": "", "type": ""}

    error = body.get("error")
    if isinstance(error, str):
        return {"message": error, "code": "", "type": str(body.get("type") or "")}
    if not isinstance(error, dict):
        error = {}

    message = (error.get("message") or body.get("message") or body.get("detail")
               or body.get("error_message") or "")
    if isinstance(message, dict):  # some gateways double-encode
        message = message.get("message") or json.dumps(message)
    code = error.get("code") or body.get("code") or ""
    # Gemini reports the symbolic status here; OpenAI uses "code".
    status_word = error.get("status") or ""
    kind = error.get("type") or body.get("type") or ""
    if kind == "error" and isinstance(body.get("error"), dict):
        kind = body["error"].get("type") or kind
    return {"message": str(message or ""), "code": str(code or status_word or ""),
            "type": str(kind or "")}


_AUTH_WORDS = ("authentication", "invalid_api_key", "invalid api key",
               "unauthenticated", "permission_denied", "permission_error",
               "invalid_authentication", "unauthorized", "api key")
_RATE_WORDS = ("rate_limit", "rate limit", "too many requests",
               "resource_exhausted", "quota", "insufficient_quota",
               "free_usage_limit", "usage limit", "concurrency limit")
_NOT_FOUND_WORDS = ("model_not_found", "not_found", "does not exist",
                    "not found", "unknown model", "no such model",
                    "unsupported model")
# Some gateways answer an unknown model id with 401 rather than 404 (opencode
# Zen does). Believe the words over the status code when they name a model.
_MODEL_MISSING = re.compile(
    r"model\b[^.\n]{0,80}?\b(is not supported|not supported|not found|"
    r"does not exist|unknown|unavailable|invalid|deprecated)", re.I)
_FILTER_WORDS = ("content_filter", "content_policy", "responsible_ai",
                 "safety", "prohibited_content", "blocklist", "recitation",
                 "spii", "blocked_reason", "invalid_prompt")
_SERVER_WORDS = ("server_error", "internal", "unavailable", "api_error",
                 "overloaded", "overloaded_error", "bad_gateway", "upstream",
                 "capacity")
# Quota exhaustion is a rate-limit *kind* but retrying will not fix it.
_TERMINAL_RATE_WORDS = ("insufficient_quota", "billing", "credit",
                        "exceeded your current quota")


def classify_error(status: Optional[int] = None, body: str = "",
                   message: str = "", provider: str = "", model: str = "",
                   retryable: Optional[bool] = None) -> ProviderError:
    """Fold a status code plus a provider body into one ProviderError."""
    parsed = parse_error_payload(body)
    detail = parsed["message"] or message or ""
    haystack = " ".join(x for x in (parsed["code"], parsed["type"], detail,
                                    message) if x).lower()

    def build(kind: str, text: str, retry: bool) -> ProviderError:
        return ProviderError(
            kind=kind, message=text,
            retryable=retry if retryable is None else bool(retryable),
            status=status, body=(body or "")[:MAX_BODY],
            provider=provider, model=model)

    where = f" ({provider})" if provider else ""
    tail = f": {detail}" if detail else ""

    if status == 413 or is_context_overflow(haystack):
        return build("context_overflow",
                     "Context window exceeded"
                     f"{' for ' + model if model else ''}{where}. Compact or "
                     f"trim the conversation and try again{tail}", False)

    if _MODEL_MISSING.search(detail or ""):
        return build("model_not_found",
                     f"Model {model or '?'!r} is not available{where}. Check "
                     f"the model id and base URL{tail}", False)

    if status in (401, 403) or any(w in haystack for w in _AUTH_WORDS):
        return build("auth",
                     f"Authentication failed{where}"
                     f"{' (HTTP ' + str(status) + ')' if status else ''}. The "
                     f"API key is missing, invalid or not allowed to use "
                     f"{model or 'this model'}{tail}", False)

    if status == 429 or any(w in haystack for w in _RATE_WORDS):
        terminal = any(w in haystack for w in _TERMINAL_RATE_WORDS)
        return build("rate_limit",
                     f"Rate limited{where}"
                     f"{' (HTTP ' + str(status) + ')' if status else ''}"
                     f"{'; quota or credit exhausted' if terminal else ''}"
                     f"{tail}", not terminal)

    if status == 404 or any(w in haystack for w in _NOT_FOUND_WORDS):
        return build("model_not_found",
                     f"Model {model or '?'!r} is not available{where}. Check "
                     f"the model id and base URL{tail}", False)

    if any(w in haystack for w in _FILTER_WORDS):
        return build("content_filter",
                     f"The provider blocked this request{where}{tail}", False)

    if (status is not None and status >= 500) or \
            any(w in haystack for w in _SERVER_WORDS):
        return build("server",
                     f"Provider error{where}"
                     f"{' (HTTP ' + str(status) + ')' if status else ''}"
                     f"{tail or '. The provider is unavailable'}", True)

    text = detail or message or (f"HTTP {status}" if status else "Unknown error")
    return build("unknown", f"{text}" if not where else f"{text}{where}", False)


def error_from_exception(exc: BaseException, provider: str = "",
                         model: str = "") -> ProviderError:
    """Classify anything that escaped a stream, transport failures included."""
    if isinstance(exc, NetError):
        if exc.status is not None:
            # Classify from the provider's own words only: NetError's message
            # is a hint this module wrote, and feeding it back in would let
            # our own phrasing decide the kind.
            return classify_error(status=exc.status, body=exc.body,
                                  provider=provider, model=model)
        # Transport-level: no body to read, but net already knows whether
        # another attempt could have helped.
        return ProviderError(kind="server" if exc.retryable else "unknown",
                             message=str(exc), retryable=exc.retryable,
                             provider=provider, model=model)
    return ProviderError(kind="unknown", message=str(exc) or exc.__class__.__name__,
                         provider=provider, model=model)


def error_chunk(err: ProviderError) -> CompletionChunk:
    """The terminal chunk a provider yields instead of raising."""
    usage: Dict[str, Any] = {"input": 0, "output": 0, "error": err.as_dict()}
    text = f"\n{ERROR_TEXT_MARKER} {err.message}" if ERROR_TEXT_COMPAT else ""
    return CompletionChunk(text=text, stop_reason="error", usage=usage)


def chunk_error(chunk: CompletionChunk) -> Optional[Dict[str, Any]]:
    """Extract the structured error from a chunk, if it carries one."""
    usage = chunk.usage or {}
    error = usage.get("error") if isinstance(usage, dict) else None
    return error if isinstance(error, dict) else None


# --------------------------------------------------------------------------
# Reasoning
# --------------------------------------------------------------------------
# Reasoning models disagree about which key carries thinking. Getting this
# wrong is not cosmetic: reasoning rendered as answer text ends up in the
# transcript and gets replayed to the model as its own output.
REASONING_KEYS = ("reasoning_content", "reasoning", "thinking", "thought",
                  "thinking_content", "reasoning_text")


def reasoning_from_delta(delta: Dict[str, Any]) -> str:
    """Pull reasoning out of an OpenAI-shaped delta, whichever dialect it is.

    Seen in the wild:
      reasoning_content   DeepSeek, GLM (glm-5.2), Qwen (qwen3-coder), vLLM
      reasoning           Ollama Cloud (gpt-oss:120b), xAI, OpenRouter
      thinking            Kimi (kimi-*), some Zen models
      reasoning: {...}    gateways that wrap the string in an object
      reasoning_details   OpenRouter's newer list-of-blocks form

    First match wins, deliberately. Several gateways — opencode Zen among
    them — send the same token under two keys in the same delta, and
    concatenating the channels renders every thought twice.
    """
    for key in REASONING_KEYS:
        text = _reasoning_text(delta.get(key))
        if text:
            return text
    return _reasoning_text(delta.get("reasoning_details"))


def _reasoning_text(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content", "thinking", "reasoning", "summary"):
            found = value.get(key)
            if isinstance(found, str) and found:
                return found
            if isinstance(found, (list, dict)):
                nested = _reasoning_text(found)
                if nested:
                    return nested
        return ""
    if isinstance(value, list):
        return "".join(_reasoning_text(item) for item in value)
    return ""


THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


@dataclass
class ThinkTagSplitter:
    """Route ``<think>…</think>`` content out of the answer stream.

    Endpoints that do not implement a separate reasoning channel (llama.cpp,
    plain Ollama, several Zen models) inline the tags in ``content``. Left
    alone, the chain of thought is rendered as the answer and stored in the
    transcript. Tags may be split across deltas, so a suffix that could still
    grow into a tag is held back until the next delta or the final flush.
    """

    inside: bool = False
    buffer: str = ""
    seen_open: bool = field(default=False)

    def feed(self, text: str):
        """Yield ``(channel, text)`` pairs where channel is text|reasoning."""
        self.buffer += text
        out = []
        while self.buffer:
            if self.inside:
                index = self.buffer.find(THINK_CLOSE)
                if index == -1:
                    keep = _tag_suffix(self.buffer, THINK_CLOSE)
                    emit, self.buffer = self.buffer[:len(self.buffer) - keep], \
                        self.buffer[len(self.buffer) - keep:]
                    if emit:
                        out.append(("reasoning", emit))
                    break
                if index:
                    out.append(("reasoning", self.buffer[:index]))
                self.buffer = self.buffer[index + len(THINK_CLOSE):]
                self.inside = False
                continue
            index = self.buffer.find(THINK_OPEN)
            if index == -1:
                keep = _tag_suffix(self.buffer, THINK_OPEN)
                emit, self.buffer = self.buffer[:len(self.buffer) - keep], \
                    self.buffer[len(self.buffer) - keep:]
                if emit:
                    out.append(("text", emit))
                break
            if index:
                out.append(("text", self.buffer[:index]))
            self.buffer = self.buffer[index + len(THINK_OPEN):]
            self.inside = True
            self.seen_open = True
        return out

    def flush(self):
        """Emit whatever is still held back once the stream ends."""
        if not self.buffer:
            return []
        channel = "reasoning" if self.inside else "text"
        out = [(channel, self.buffer)]
        self.buffer = ""
        return out


def _tag_suffix(text: str, tag: str) -> int:
    """Length of the trailing run of ``text`` that could still become ``tag``."""
    limit = min(len(text), len(tag) - 1)
    for size in range(limit, 0, -1):
        if tag.startswith(text[-size:]):
            return size
    return 0


class Provider(ABC):
    name: str = "base"

    @abstractmethod
    def stream(
        self,
        messages: List[Msg],
        tools: List[ToolSpec],
        model: str,
        max_tokens: int,
    ) -> Iterator[CompletionChunk]:
        ...

    def count_hint(self, text: str) -> int:
        # Very rough token estimate
        return max(1, len(text) // 4)

    def context_limit(self, model: str, configured: int) -> tuple:
        """Effective context window and its source for this provider/model."""
        return configured, "configuration"

    def input_limit(self, model: str, window: int) -> tuple:
        """What a single prompt may be, and where that number came from.

        Distinct from the context window: `context` is input plus output,
        and the binding constraint on a request is the input share. A
        provider that publishes the split overrides this; everyone else
        keeps the window, which is what the old behaviour assumed.
        """
        return window, "context window"

    def reasoning_efforts(self, model: str) -> tuple:
        """Effort values this transport can send; empty means no control."""
        return ()

    def set_reasoning_effort(self, effort: str, model: str) -> str:
        """Apply an effort to later requests, or reject an unsupported control."""
        if effort:
            raise ValueError(
                f"reasoning effort is not supported by provider {self.name}")
        return ""

    # Shared helpers -------------------------------------------------------

    def error(self, exc: BaseException, model: str = "") -> CompletionChunk:
        return error_chunk(error_from_exception(exc, self.name, model))
