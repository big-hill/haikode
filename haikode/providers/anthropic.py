import json
from typing import Any, Dict, Iterator, List, Optional

from ..net import DEFAULT_TIMEOUT, Aborted, RetryPolicy, stream_sse_events
from ..schema import CompletionChunk, Msg, ToolSpec
from .base import Provider, classify_error, error_chunk, error_from_exception

# Anthropic rejects a request whose max_tokens exceeds the model's output
# ceiling with a 400 — which reads as "bad request" and tells the user
# nothing. Clamp instead. Longest prefix wins; an unlisted model is left
# alone so a newer one is never capped by a stale table.
MAX_OUTPUT_TOKENS = {
    "claude-3-haiku": 4096,
    "claude-3-opus": 4096,
    "claude-3-sonnet": 4096,
    "claude-3-5-haiku": 8192,
    "claude-3-5-sonnet": 8192,
    "claude-3-7-sonnet": 64000,
    "claude-haiku-4": 64000,
    "claude-sonnet-4": 64000,
    "claude-opus-4": 32000,
    "claude-opus-4-1": 32000,
}

# Prompt caching is generally available; `cache_control` needs no beta header.
# A breakpoint below the provider minimum (1024 tokens, 2048 on Haiku models)
# is ignored rather than rejected, but marking a short prefix just burns one
# of the four available breakpoints — so only mark blocks big enough to win.
CACHE_MIN_CHARS = 4000
CACHE_CONTROL = {"type": "ephemeral"}

# Effort on Anthropic models is `output_config.effort`, not a thinking
# budget. This matters more than it looks: `thinking: {"type": "enabled",
# budget_tokens}` is deprecated on the 4.6 generation and REJECTED WITH 400
# on 4.7 and later — which includes claude-sonnet-5, the default model of
# the shipped profile. Effort also beats a budget on its own terms: it
# shapes every token in the response, tool calls included, and needs no
# thinking block at all.
#
# Longest matching prefix wins; an unlisted model gets no effort control
# rather than a guess, because guessing wrong here is a 400 on every turn.
_MAX = ("low", "medium", "high", "max")
_XHIGH = ("low", "medium", "high", "xhigh", "max")
EFFORT_SUPPORT = {
    "claude-opus-4-5": ("low", "medium", "high"),
    "claude-opus-4-6": _MAX,
    "claude-sonnet-4-6": _MAX,
    "claude-opus-4-7": _XHIGH,
    "claude-opus-4-8": _XHIGH,
    "claude-opus-5": _XHIGH,
    "claude-sonnet-5": _XHIGH,
    "claude-fable-5": _XHIGH,
    "claude-mythos-5": _XHIGH,
}

# The older families where a token budget IS the mechanism — extended
# thinking is the only mode they have. Opus 4.5 appears in both tables: it
# takes effort *and* a budget, and the documentation says to set both.
THINKING_BUDGETS = {"low": 4096, "medium": 16384, "high": 65536}
THINKING_HEADROOM = 1024  # the answer needs room after the thinking does
BUDGET_THINKING_MODELS = (
    "claude-3-7", "claude-sonnet-4", "claude-opus-4", "claude-opus-4-1",
    "claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-4-5",
)


def _longest_prefix(model: str, table) -> Optional[str]:
    normalized = (model or "").lower()
    best = None
    for prefix in table:
        if normalized.startswith(prefix) and (best is None
                                              or len(prefix) > len(best)):
            best = prefix
    return best


def max_output_tokens(model: str) -> Optional[int]:
    best: Optional[int] = None
    best_len = -1
    for prefix, limit in MAX_OUTPUT_TOKENS.items():
        if model.startswith(prefix) and len(prefix) > best_len:
            best, best_len = limit, len(prefix)
    return best


class AnthropicProvider(Provider):
    """Anthropic /v1/messages with native tool_use blocks."""

    def __init__(self, base_url: str = "https://api.anthropic.com",
                 api_key: Optional[str] = None,
                 retry: Optional[RetryPolicy] = None,
                 timeout: int = DEFAULT_TIMEOUT,
                 connect_timeout: Optional[float] = None,
                 stall_timeout: Optional[float] = None,
                 cache: bool = True,
                 abort=None, reasoning_effort: str = "off"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.name = "anthropic"
        self.retry = retry
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.stall_timeout = stall_timeout
        self.cache = cache
        self.abort = abort
        self.reasoning_effort = reasoning_effort

    def reasoning_efforts(self, model: str):
        """The effort levels `model` accepts; empty hides the picker.

        The levels differ per model — xhigh and max are not everywhere —
        and offering one the model rejects turns every turn into a 400.
        """
        prefix = _longest_prefix(model, EFFORT_SUPPORT)
        if prefix:
            return EFFORT_SUPPORT[prefix]
        if _longest_prefix(model, BUDGET_THINKING_MODELS):
            # Budget-thinking families: our own scale, mapped to budgets.
            return ("off", "low", "medium", "high")
        return ()

    def set_reasoning_effort(self, effort: str, model: str) -> str:
        value = (effort or "").strip().lower()
        allowed = self.reasoning_efforts(model)
        if not allowed:
            raise ValueError("%s takes no reasoning effort"
                             % (model or "this model"))
        if value not in allowed:
            raise ValueError("reasoning effort must be one of "
                             + ", ".join(allowed))
        self.reasoning_effort = value
        return value

    def _effort_field(self, model: str):
        """`output_config` for models that take effort natively."""
        effort = self.reasoning_effort
        if not effort or effort == "off":
            return None
        prefix = _longest_prefix(model, EFFORT_SUPPORT)
        if not prefix or effort not in EFFORT_SUPPORT[prefix]:
            return None
        return {"effort": effort}

    def _thinking(self, model: str, max_tokens: int):
        """A thinking budget, for the families where that is the mechanism.

        Only for those: `type: "enabled"` is deprecated on the 4.6
        generation and returns 400 on 4.7 and later, so sending it by
        default would break the shipped profile's own model. The budget
        must also sit strictly inside max_tokens — it is shrunk to the
        model's output ceiling when one is known, and skipped rather than
        sent invalid if no meaningful budget fits.
        """
        effort = self.reasoning_effort
        if not effort or effort == "off":
            return None, max_tokens
        budget_prefix = _longest_prefix(model, BUDGET_THINKING_MODELS)
        if not budget_prefix:
            return None, max_tokens
        # "claude-opus-4-7" also starts with "claude-opus-4". The more
        # specific table wins, or a newer model would be handed the very
        # block it rejects with a 400. Opus 4.5 is in both tables at equal
        # length and legitimately takes both.
        effort_prefix = _longest_prefix(model, EFFORT_SUPPORT)
        if effort_prefix and len(effort_prefix) > len(budget_prefix):
            return None, max_tokens
        budget = THINKING_BUDGETS.get(effort)
        if budget is None:
            return None, max_tokens
        ceiling = max_output_tokens(model)
        if ceiling is not None and budget + THINKING_HEADROOM > ceiling:
            budget = ceiling - THINKING_HEADROOM
            if budget < 1024:      # provider minimum for a thinking budget
                return None, max_tokens
        return ({"type": "enabled", "budget_tokens": budget},
                max(max_tokens, budget + THINKING_HEADROOM))

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream",
                   "anthropic-version": "2023-06-01"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    @staticmethod
    def _preserved_blocks(message: Msg, model: str) -> List[dict]:
        """This turn's own thinking blocks, if they are ours to send back.

        The dialect and model tags are checked, not trusted: a signature is
        issued by one model on one dialect, and replaying it anywhere else
        means posting an opaque blob to a provider that never signed it. A
        session that switched provider or model mid-conversation therefore
        drops them rather than carrying them across.
        """
        reasoning = getattr(message, "reasoning", None) or {}
        if reasoning.get("dialect") != "anthropic":
            return []
        if str(reasoning.get("model") or "").lower() != (model or "").lower():
            return []
        blocks = reasoning.get("blocks")
        if not blocks:
            return []
        kept = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            # An unsigned thinking block is refused rather than sent. The
            # API validates the signature, and a rejected replay is not a
            # one-off error: the block lives in the history, so the 400
            # returns on every later request and the session is bricked
            # with no way out from inside. Dropping it costs the model one
            # turn's reasoning; sending it costs the session.
            if block.get("type") == "thinking" and not block.get("signature"):
                continue
            kept.append(block)
        return kept

    @staticmethod
    def _encode(messages: List[Msg], model: str):
        """Map the neutral schema onto Anthropic's content-block format.

        Thinking blocks go back first and unmodified. The API requires the
        thinking blocks of an assistant turn to accompany the tool_use they
        preceded, and rebuilding the turn without them — or filtering out a
        redacted_thinking block — is an error rather than a degradation. It
        is also what makes interleaved thinking worth anything: without the
        replay the model cannot see its own reasoning from earlier steps of
        the same tool loop.
        """
        system = ""
        out: List[dict] = []
        for m in messages:
            if m.role == "system":
                system = (system + "\n\n" + m.content).strip() if system else m.content
                continue

            if m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id,
                         "content": m.content}
                # Consecutive tool results belong in one user turn.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue

            if m.role == "assistant":
                thinking = AnthropicProvider._preserved_blocks(m, model)
                if not (m.tool_calls or thinking):
                    out.append({"role": m.role, "content": m.content or ""})
                    continue
                # Thinking first: the API reads the turn in order, and a
                # thinking block that follows its own tool_use is not the
                # turn the model produced.
                blocks = list(thinking)
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append({"type": "tool_use", "id": tc.id,
                                   "name": tc.name, "input": tc.arguments})
                out.append({"role": "assistant", "content": blocks})
                continue

            out.append({"role": m.role, "content": m.content or ""})
        return system, out

    def _system_field(self, system: str):
        """Plain string below the caching threshold, blocks above it.

        The system prompt plus project instructions is the single largest
        stable prefix in a coding agent, so a breakpoint here is close to free.
        """
        if not self.cache or len(system) < CACHE_MIN_CHARS:
            return system
        return [{"type": "text", "text": system, "cache_control": CACHE_CONTROL}]

    def _tool_field(self, tools: List[ToolSpec]):
        encoded = [{"name": t.name, "description": t.description,
                    "input_schema": t.parameters} for t in tools]
        if not self.cache or not encoded:
            return encoded
        if len(json.dumps(encoded)) < CACHE_MIN_CHARS:
            return encoded
        # One breakpoint on the last tool caches the whole tools array.
        encoded[-1] = dict(encoded[-1], cache_control=CACHE_CONTROL)
        return encoded

    def stream(self, messages: List[Msg], tools: List[ToolSpec], model: str,
               max_tokens: int) -> Iterator[CompletionChunk]:
        url = f"{self.base_url}/v1/messages"
        system, encoded = self._encode(messages, model)

        ceiling = max_output_tokens(model)
        if ceiling is not None:
            max_tokens = max(1, min(max_tokens, ceiling))
        thinking, max_tokens = self._thinking(model, max_tokens)

        payload = {"model": model, "max_tokens": max_tokens,
                   "messages": encoded, "stream": True}
        if thinking:
            payload["thinking"] = thinking
        output_config = self._effort_field(model)
        if output_config:
            payload["output_config"] = output_config
        if system:
            payload["system"] = self._system_field(system)
        if tools:
            payload["tools"] = self._tool_field(tools)

        index_map: Dict[Any, int] = {}   # content block index -> tool call index
        next_index = 0
        input_tokens: Optional[int] = None
        # Thinking blocks under construction, by content block index. They
        # are assembled here rather than by the agent because only this
        # dialect knows that a signature arrives in its own delta type,
        # after the text it signs.
        pending_thinking: Dict[Any, dict] = {}
        try:
            for event in stream_sse_events(
                    url, payload, headers=self._headers(),
                    timeout=self.timeout, connect_timeout=self.connect_timeout,
                    stall_timeout=self.stall_timeout, retry=self.retry,
                    abort=self.abort):
                etype = event.get("type")

                if etype == "message_start":
                    # Anthropic reports prompt tokens here and (usually) not in
                    # message_delta, so reading only the delta loses them —
                    # along with the cache hit counters that prove caching works.
                    usage = ((event.get("message") or {}).get("usage") or {})
                    chunk = self._start_usage(usage)
                    if chunk is not None:
                        input_tokens = chunk.usage.get("input", 0)
                        yield chunk

                elif etype == "content_block_start":
                    block = event.get("content_block") or {}
                    kind = block.get("type")
                    if kind == "thinking":
                        pending_thinking[event.get("index")] = {
                            "type": "thinking",
                            "thinking": block.get("thinking") or "",
                            "signature": block.get("signature") or ""}
                    elif kind == "redacted_thinking":
                        # Encrypted and unreadable to us. It must still go
                        # back untouched: filtering one out is a 400, and
                        # the model needs the reasoning it stands for.
                        pending_thinking[event.get("index")] = {
                            "type": "redacted_thinking",
                            "data": block.get("data") or ""}
                    elif kind == "tool_use":
                        index_map[event.get("index")] = next_index
                        yield CompletionChunk(tool_call_delta={
                            "index": next_index, "id": block.get("id"),
                            "name": block.get("name"), "arguments": ""})
                        next_index += 1

                elif etype == "content_block_delta":
                    delta = event.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta" and delta.get("text"):
                        yield CompletionChunk(text=delta["text"])
                    elif dtype == "thinking_delta" and delta.get("thinking"):
                        block = pending_thinking.get(event.get("index"))
                        if block is not None:
                            block["thinking"] = (block.get("thinking", "")
                                                 + delta["thinking"])
                        yield CompletionChunk(reasoning=delta["thinking"])
                    elif dtype == "signature_delta":
                        # Signs the thinking block, and is never answer text.
                        # Kept, not dropped: without it the block cannot be
                        # handed back, and the API requires it back.
                        block = pending_thinking.get(event.get("index"))
                        if block is not None and delta.get("signature"):
                            block["signature"] = (block.get("signature", "")
                                                  + delta["signature"])
                    elif dtype == "input_json_delta":
                        call_index = index_map.get(event.get("index"))
                        if call_index is not None:
                            yield CompletionChunk(tool_call_delta={
                                "index": call_index, "id": None, "name": None,
                                "arguments": delta.get("partial_json") or ""})

                elif etype == "content_block_stop":
                    finished = pending_thinking.pop(event.get("index"), None)
                    if finished is not None:
                        yield CompletionChunk(reasoning_block=finished)

                elif etype == "message_delta":
                    reason = (event.get("delta") or {}).get("stop_reason")
                    usage = event.get("usage") or {}
                    if reason:
                        yield CompletionChunk(
                            stop_reason=self._stop_reason(reason),
                            usage={
                                # Do not double count: message_start already
                                # reported the prompt side when it was present.
                                "input": (0 if input_tokens is not None
                                          else usage.get("input_tokens", 0)),
                                "output": usage.get("output_tokens", 0)})

                elif etype == "error":
                    yield error_chunk(classify_error(
                        body=json.dumps(event), provider=self.name, model=model))
                    return
        except Aborted:
            return
        except Exception as e:
            yield error_chunk(error_from_exception(e, self.name, model))

    @staticmethod
    def _stop_reason(reason: str) -> str:
        if reason == "tool_use":
            return "tool_calls"
        if reason == "max_tokens":
            return "length"
        if reason == "refusal":
            return "content_filter"
        return reason

    @staticmethod
    def _start_usage(usage: Dict[str, Any]) -> Optional[CompletionChunk]:
        if not usage:
            return None
        out = {"input": usage.get("input_tokens", 0), "output": 0}
        if usage.get("cache_read_input_tokens"):
            out["cache_read"] = usage["cache_read_input_tokens"]
        if usage.get("cache_creation_input_tokens"):
            out["cache_write"] = usage["cache_creation_input_tokens"]
        return CompletionChunk(usage=out)
