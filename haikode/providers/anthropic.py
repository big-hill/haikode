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
                 abort=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.name = "anthropic"
        self.retry = retry
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.stall_timeout = stall_timeout
        self.cache = cache
        self.abort = abort

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream",
                   "anthropic-version": "2023-06-01"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    @staticmethod
    def _encode(messages: List[Msg]):
        """Map the neutral schema onto Anthropic's content-block format."""
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

            if m.role == "assistant" and m.tool_calls:
                blocks = []
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
        system, encoded = self._encode(messages)

        ceiling = max_output_tokens(model)
        if ceiling is not None:
            max_tokens = max(1, min(max_tokens, ceiling))

        payload = {"model": model, "max_tokens": max_tokens,
                   "messages": encoded, "stream": True}
        if system:
            payload["system"] = self._system_field(system)
        if tools:
            payload["tools"] = self._tool_field(tools)

        index_map: Dict[Any, int] = {}   # content block index -> tool call index
        next_index = 0
        input_tokens: Optional[int] = None
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
                    if block.get("type") == "tool_use":
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
                        yield CompletionChunk(reasoning=delta["thinking"])
                    elif dtype == "signature_delta":
                        # Signs the thinking block; never answer text.
                        continue
                    elif dtype == "input_json_delta":
                        call_index = index_map.get(event.get("index"))
                        if call_index is not None:
                            yield CompletionChunk(tool_call_delta={
                                "index": call_index, "id": None, "name": None,
                                "arguments": delta.get("partial_json") or ""})

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
