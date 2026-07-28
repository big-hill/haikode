import json
from typing import Any, Dict, Iterator, List, Optional

from ..net import (DEFAULT_TIMEOUT, Aborted, RetryPolicy, stream_sse_events)
from ..schema import CompletionChunk, Msg, ToolSpec
from .base import (Provider, ThinkTagSplitter, classify_error, error_chunk,
                   error_from_exception, reasoning_from_delta)


class OpenAICompatProvider(Provider):
    """OpenAI /chat/completions — used by Ollama Cloud, xAI, Zen, OpenAI."""

    def __init__(self, base_url: str, api_key: Optional[str] = None,
                 name: str = "openai", retry: Optional[RetryPolicy] = None,
                 timeout: int = DEFAULT_TIMEOUT,
                 connect_timeout: Optional[float] = None,
                 stall_timeout: Optional[float] = None,
                 abort=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self.retry = retry
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.stall_timeout = stall_timeout
        # Set by the caller (agent/TUI) to a threading.Event or a predicate;
        # tripping it tears the connection down within a poll interval.
        self.abort = abort

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _encode(messages: List[Msg]) -> List[dict]:
        out = []
        for m in messages:
            if m.role == "tool":
                out.append({"role": "tool", "tool_call_id": m.tool_call_id,
                            "content": m.content})
                continue
            entry = {"role": m.role, "content": m.content or ""}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name,
                                  "arguments": json.dumps(tc.arguments)}}
                    for tc in m.tool_calls
                ]
            out.append(entry)
        return out

    def stream(self, messages: List[Msg], tools: List[ToolSpec], model: str,
               max_tokens: int) -> Iterator[CompletionChunk]:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": self._encode(messages),
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description,
                              "parameters": t.parameters}}
                for t in tools
            ]
            payload["tool_choice"] = "auto"

        splitter = ThinkTagSplitter()
        try:
            for event in stream_sse_events(
                    url, payload, headers=self._headers(),
                    timeout=self.timeout, connect_timeout=self.connect_timeout,
                    stall_timeout=self.stall_timeout, retry=self.retry,
                    abort=self.abort):

                # Some gateways report failures as an SSE frame rather than a
                # status code, mid-200-response.
                failure = self._inline_error(event, model)
                if failure is not None:
                    yield from self._flush(splitter)
                    yield failure
                    return

                choices = event.get("choices") or []
                if not choices:
                    if event.get("usage"):
                        yield CompletionChunk(usage=self._usage(event["usage"]))
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}

                # Reasoning first: a model that emits both in one delta has
                # thought before it spoke.
                reasoning = reasoning_from_delta(delta)
                if reasoning:
                    yield CompletionChunk(reasoning=reasoning)

                content = delta.get("content")
                if content:
                    for channel, piece in splitter.feed(str(content)):
                        if channel == "reasoning":
                            yield CompletionChunk(reasoning=piece)
                        else:
                            yield CompletionChunk(text=piece)

                for call in delta.get("tool_calls") or []:
                    function = call.get("function") or {}
                    yield CompletionChunk(tool_call_delta={
                        "index": call.get("index", 0),
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "arguments": function.get("arguments") or "",
                    })

                finish = choice.get("finish_reason")
                if finish:
                    yield from self._flush(splitter)
                    yield self._finish(finish, event, model)
        except Aborted:
            return
        except Exception as e:
            yield from self._flush(splitter)
            yield error_chunk(error_from_exception(e, self.name, model))

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _flush(splitter: ThinkTagSplitter) -> Iterator[CompletionChunk]:
        for channel, piece in splitter.flush():
            if channel == "reasoning":
                yield CompletionChunk(reasoning=piece)
            else:
                yield CompletionChunk(text=piece)

    def _finish(self, finish: str, event: dict, model: str) -> CompletionChunk:
        usage = self._usage(event.get("usage")) or {}
        if finish == "content_filter":
            err = classify_error(body=json.dumps({"error": {"code": "content_filter"}}),
                                 message="the response was blocked by a content filter",
                                 provider=self.name, model=model)
            usage = dict(usage)
            usage.setdefault("input", 0)
            usage.setdefault("output", 0)
            usage["error"] = err.as_dict()
            return CompletionChunk(stop_reason="content_filter", usage=usage)
        return CompletionChunk(
            stop_reason="tool_calls" if finish == "tool_calls" else finish,
            usage=usage or None)

    def _inline_error(self, event: Dict[str, Any], model: str):
        raw = event.get("error")
        if not raw and event.get("object") == "error":
            raw = event
        if not raw:
            return None
        err = classify_error(body=json.dumps(
            raw if isinstance(raw, dict) else {"error": raw}),
            provider=self.name, model=model)
        return error_chunk(err)

    @staticmethod
    def _usage(raw) -> Optional[dict]:
        if not raw:
            return None
        details = raw.get("prompt_tokens_details") or {}
        cached = (details.get("cached_tokens", 0)
                  if isinstance(details, dict) else 0)
        out_details = raw.get("completion_tokens_details") or {}
        reasoning = (out_details.get("reasoning_tokens", 0)
                     if isinstance(out_details, dict) else 0)
        usage = {
            "input": max(0, raw.get("prompt_tokens", 0) - cached),
            "output": max(0, raw.get("completion_tokens", 0) - reasoning),
        }
        if cached:
            usage["cache_read"] = cached
        if reasoning:
            usage["reasoning"] = reasoning
        return usage
