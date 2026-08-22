import json
from typing import Any, Dict, Iterator, List, Optional

from ..net import (DEFAULT_TIMEOUT, Aborted, NetError, RetryPolicy,
                   stream_sse_events)
from ..schema import CompletionChunk, Msg, ToolSpec
from .base import (Provider, ThinkTagSplitter, classify_error, error_chunk,
                   error_from_exception, reasoning_from_delta)
from .base import data_url, image_note


# Requests to make without stream_options before testing the back-off again.
# Twenty turns is often enough that a wrong inference costs a session its
# token counts only briefly, and rare enough that an endpoint which really
# does reject the parameter pays for one refused request in twenty.
USAGE_REPROBE_AFTER = 20


class _UnsupportedStreamOptions(Exception):
    """Internal signal: this endpoint rejected stream_options. Never escapes
    stream(), which retries once without the parameter."""


def _rejects_stream_options(error: NetError) -> bool:
    """True when a 4xx blames the stream_options parameter.

    Deliberately narrow. A 400 that says nothing about the parameter is a
    real error about the request and must reach the user unchanged.
    """
    status = getattr(error, "status", None)
    if status not in (400, 404, 422):
        return False
    body = "%s %s" % (getattr(error, "body", "") or "", error)
    return "stream_options" in body or "include_usage" in body


# Reasoning-effort levels this transport may offer, measured against the live
# endpoints on 2026-08-18 rather than read off documentation: the answer
# differs per family and has changed across generations, and offering a level
# the model refuses turns every single turn into a 400.
#
# "xhigh" was missing from the first version of this table because the probe
# that built it never tried the value -- xAI documents it, and all three grok
# families accept it. A table built by asking the endpoint is only as good as
# the questions asked, so the probe now sweeps every level any provider in
# this file uses. "max" is rejected here ("Invalid reasoning effort") though
# Ollama takes it, which is exactly why the tables are per-endpoint.
#
# Matched on the longest model-id prefix. A family that rejects the parameter
# outright is listed with an empty tuple, which is not the same as "unknown"
# -- it is a measured "no", and it stops an effort set on a sibling model from
# riding along after a /model switch.
_EFFORTS_BY_MODEL = {
    "grok-4.20": (),                    # "does not support parameter reasoningEffort"
    "grok-4.3": ("none", "minimal", "low", "medium", "high", "xhigh"),
    "grok-4.5": ("minimal", "low", "medium", "high", "xhigh"),
    "grok-4.6": ("minimal", "low", "medium", "high", "xhigh"),
}

# Endpoints whose enum is the endpoint's own, whatever model is named. Ollama
# validates the value and says so: 'invalid reasoning value: "zzz" (must be
# "high", "medium", "low", "max", or "none")'.
_EFFORTS_BY_HOST = {
    "ollama.com": ("none", "low", "medium", "high", "max"),
}


def _longest_prefix(model: str, table) -> str:
    name = (model or "").lower()
    hits = [key for key in table if name.startswith(key)]
    return max(hits, key=len) if hits else ""


class OpenAICompatProvider(Provider):
    """OpenAI /chat/completions — used by Ollama Cloud, xAI, Zen, OpenAI."""

    def __init__(self, base_url: str, api_key: Optional[str] = None,
                 name: str = "openai", retry: Optional[RetryPolicy] = None,
                 timeout: int = DEFAULT_TIMEOUT,
                 connect_timeout: Optional[float] = None,
                 stall_timeout: Optional[float] = None,
                 abort=None, reasoning_effort: str = "",
                 reasoning_efforts=None):
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
        self.reasoning_effort = (reasoning_effort or "").strip().lower()
        # A profile may declare its own levels: a local server or a provider
        # this build has never met knows its own endpoint, and haikode should
        # not have to ship an opinion about it. None means "use the tables".
        self._declared_efforts = (
            tuple(str(value).strip().lower() for value in reasoning_efforts)
            if reasoning_efforts else None)
        # Whether to ask for token counts. A streaming /chat/completions is
        # not required to report usage unless the request opts in, and the
        # endpoints differ: measured on 1 August 2026, an Ollama server —
        # local and cloud alike — reports nothing at all unasked and full
        # counts when asked, while an OpenRouter-style gateway reports them
        # either way. That is why the footer sat at "0 in 0 out" for some
        # providers and not others, and why the session stored no token
        # counts for those runs.
        #
        # Cleared when an endpoint rejects the parameter — some gateways and
        # older llama.cpp builds 400 on it — and re-tested periodically; see
        # _ask_for_usage().
        self.stream_usage = True
        self._usage_declined = 0

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
                if m.images:
                    # This API accepts images only from the user; ride them
                    # in a follow-up user message, labelled so the model
                    # knows the tool sent them (see base.image_note).
                    parts = [{"type": "text",
                              "text": image_note(m.tool_call_id)}]
                    parts += [{"type": "image_url",
                               "image_url": {"url": data_url(image)}}
                              for image in m.images]
                    out.append({"role": "user", "content": parts})
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
        """Stream one request, retrying once without stream_options.

        Asking for usage is the only way to get token counts out of a
        streaming /chat/completions, but it is a newer parameter than the
        endpoint itself: a strict gateway answers 400 rather than ignoring
        what it does not know. Retrying without it costs one rejected request
        per process and keeps such an endpoint working.
        """
        ask = self._ask_for_usage()
        for _ in (0, 1):
            delivered = False
            try:
                for chunk in self._stream_once(messages, tools, model,
                                               max_tokens, ask):
                    delivered = True
                    yield chunk
                if ask:
                    # Nothing rejected the parameter this time, so asking is
                    # safe here — which also ends a back-off entered on a 400
                    # that turned out to be about something else.
                    self.stream_usage = True
                return
            except _UnsupportedStreamOptions as exc:
                # A provider never raises at its caller: every failure is a
                # chunk, or a half-written turn dies where the agent cannot
                # report it. Anything arriving after the first byte, or on the
                # attempt that no longer carries the parameter, is a real
                # error wearing this exception.
                if delivered or not ask:
                    yield error_chunk(error_from_exception(
                        exc.__cause__ or exc, self.name, model))
                    return
                self.stream_usage = False
                self._usage_declined = 0
                ask = False

    def _ask_for_usage(self) -> bool:
        """Whether this request carries stream_options, re-testing a back-off.

        The evidence for a back-off is one 4xx that named the parameter, and
        that is weaker than it looks: an endpoint which quotes the request
        body back inside an unrelated 400 names it too. Trusting a single
        such reading for the life of the process would silently cost every
        later turn its token counts, so the inference is re-tested. The cost
        of being wrong the other way is one rejected request per
        USAGE_REPROBE_AFTER turns.
        """
        if self.stream_usage:
            return True
        self._usage_declined += 1
        if self._usage_declined < USAGE_REPROBE_AFTER:
            return False
        self._usage_declined = 0
        return True

    def reasoning_efforts(self, model: str) -> tuple:
        """Levels `model` accepts here; empty hides the control entirely.

        A declared list wins: the operator's endpoint is the authority on
        the operator's endpoint. Otherwise the measured tables answer, and
        an endpoint nobody has measured offers nothing rather than guessing
        a 400 into every turn.
        """
        if self._declared_efforts is not None:
            return self._declared_efforts
        prefix = _longest_prefix(model, _EFFORTS_BY_MODEL)
        if prefix:
            return _EFFORTS_BY_MODEL[prefix]
        host = (self.base_url or "").lower()
        for hint, levels in _EFFORTS_BY_HOST.items():
            if hint in host:
                return levels
        return ()

    def set_reasoning_effort(self, effort: str, model: str) -> str:
        value = (effort or "").strip().lower()
        allowed = self.reasoning_efforts(model)
        if not allowed:
            raise ValueError("%s takes no reasoning effort through %s"
                             % (model or "this model", self.name))
        if value not in allowed:
            raise ValueError("reasoning effort must be one of "
                             + ", ".join(allowed))
        self.reasoning_effort = value
        return value

    def _payload(self, messages: List[Msg], tools: List[ToolSpec],
                 model: str, max_tokens: int,
                 ask_usage: bool = False) -> dict:
        payload = {
            "model": model,
            "messages": self._encode(messages),
            "max_tokens": max_tokens,
            "stream": True,
        }
        # Re-checked against *this* model, not the one it was set on: a
        # /model switch to a family that refuses the parameter must drop it
        # rather than fail every request.
        effort = self.reasoning_effort
        if effort and effort in self.reasoning_efforts(model):
            payload["reasoning_effort"] = effort
        if ask_usage:
            payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = [
                {"type": "function",
                 "function": {"name": t.name, "description": t.description,
                              "parameters": t.parameters}}
                for t in tools
            ]
            payload["tool_choice"] = "auto"
        return payload

    def _stream_once(self, messages: List[Msg], tools: List[ToolSpec],
                     model: str, max_tokens: int,
                     ask_usage: bool = True) -> Iterator[CompletionChunk]:
        url = f"{self.base_url}/chat/completions"
        payload = self._payload(messages, tools, model, max_tokens,
                                ask_usage=ask_usage)

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
        except NetError as e:
            # Only the request that carried the parameter can be blamed on it.
            if ask_usage and _rejects_stream_options(e):
                raise _UnsupportedStreamOptions() from e
            yield from self._flush(splitter)
            yield error_chunk(error_from_exception(e, self.name, model))
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
