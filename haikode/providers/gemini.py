"""
Native Gemini provider (generativelanguage.googleapis.com, v1beta).

Why native rather than Google's OpenAI-compatible shim:

* The shim drops ``thought`` parts entirely, so reasoning from the 2.5/3.x
  thinking models is invisible — and worse, the thinking token count is
  silently folded into completion_tokens, so cost and context accounting lie.
* The shim rewrites tool JSON Schema on the way through and rejects schemas
  the native endpoint accepts.
* ``thinkingConfig`` (budget / includeThoughts) has no shim equivalent.

The native wire format costs nothing extra to drive from stdlib: it is a plain
JSON POST to ``:streamGenerateContent?alt=sse`` with an ``x-goog-api-key``
header and ordinary SSE frames.

One structural difference from the OpenAI dialect: a ``functionCall`` part
arrives complete, not as a fragment stream, and carries no id. Ids are
synthesised here so the tool-result round trip can find its way back to a
function name — Gemini identifies tool results by name, while the neutral
schema only carries ``tool_call_id``.
"""
import json
from typing import Any, Dict, Iterator, List, Optional

from ..net import DEFAULT_TIMEOUT, Aborted, RetryPolicy, stream_sse_events
from ..schema import CompletionChunk, Msg, ToolSpec
from .base import Provider, classify_error, error_chunk, error_from_exception

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# Keys the v1beta schema validator accepts. Anything else (notably
# $schema / additionalProperties, which every JSON Schema generator emits)
# is rejected with a 400 INVALID_ARGUMENT.
_SCHEMA_KEYS = ("type", "format", "description", "nullable", "enum", "items",
                "properties", "required", "minimum", "maximum", "minItems",
                "maxItems", "pattern", "example")

_BLOCK_REASONS = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT",
                  "SPII", "IMAGE_SAFETY"}


def sanitize_schema(schema: Any) -> Any:
    """Project a JSON Schema onto the subset Gemini's validator accepts."""
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    for key in _SCHEMA_KEYS:
        if key not in schema:
            continue
        value = schema[key]
        if key == "properties" and isinstance(value, dict):
            out[key] = {k: sanitize_schema(v) for k, v in value.items()}
        elif key == "items":
            out[key] = sanitize_schema(value)
        else:
            out[key] = value
    if "type" not in out:
        # anyOf/oneOf branches are unsupported; fall back to the first one
        # that does declare a type rather than sending an untyped schema.
        for combinator in ("anyOf", "oneOf", "allOf"):
            branches = schema.get(combinator)
            if isinstance(branches, list):
                for branch in branches:
                    projected = sanitize_schema(branch)
                    if isinstance(projected, dict) and "type" in projected:
                        return projected
    return out


class GeminiProvider(Provider):
    """Google Gemini :streamGenerateContent with native function calling."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 api_key: Optional[str] = None, name: str = "gemini",
                 retry: Optional[RetryPolicy] = None,
                 timeout: int = DEFAULT_TIMEOUT,
                 connect_timeout: Optional[float] = None,
                 stall_timeout: Optional[float] = None,
                 include_thoughts: bool = True,
                 abort=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.name = name
        self.retry = retry
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.stall_timeout = stall_timeout
        self.include_thoughts = include_thoughts
        self.abort = abort

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream"}
        if self.api_key:
            # Header rather than ?key= so the secret stays out of logs.
            headers["x-goog-api-key"] = self.api_key
        return headers

    # -- encoding ---------------------------------------------------------

    @staticmethod
    def _encode(messages: List[Msg]):
        """Return (systemInstruction, contents).

        Tool results must be addressed by function name, which Msg(role="tool")
        does not carry, so ids are resolved against the assistant turn that
        requested them.
        """
        system_parts: List[str] = []
        contents: List[dict] = []
        names: Dict[str, str] = {}

        for m in messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                continue

            if m.role == "tool":
                part = {"functionResponse": {
                    "name": names.get(m.tool_call_id, m.tool_call_id or "tool"),
                    "response": {"content": m.content or ""}}}
                # Gemini wants every result for one model turn in a single
                # user turn, mirroring the Anthropic rule.
                if contents and contents[-1]["role"] == "user" and \
                        contents[-1]["parts"] and \
                        "functionResponse" in contents[-1]["parts"][0]:
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue

            if m.role == "assistant":
                parts: List[dict] = []
                if m.content:
                    parts.append({"text": m.content})
                for tc in m.tool_calls or []:
                    names[tc.id] = tc.name
                    parts.append({"functionCall": {"name": tc.name,
                                                   "args": tc.arguments or {}}})
                if not parts:
                    continue
                contents.append({"role": "model", "parts": parts})
                continue

            contents.append({"role": "user", "parts": [{"text": m.content or ""}]})

        return "\n\n".join(system_parts), contents

    def _tools(self, tools: List[ToolSpec]) -> List[dict]:
        declarations = []
        for tool in tools:
            declaration = {"name": tool.name, "description": tool.description}
            parameters = sanitize_schema(tool.parameters)
            # An object schema with no properties is rejected; omit it.
            if isinstance(parameters, dict) and parameters.get("properties"):
                declaration["parameters"] = parameters
            declarations.append(declaration)
        return [{"functionDeclarations": declarations}]

    # -- streaming --------------------------------------------------------

    def stream(self, messages: List[Msg], tools: List[ToolSpec], model: str,
               max_tokens: int) -> Iterator[CompletionChunk]:
        url = f"{self.base_url}/models/{model}:streamGenerateContent?alt=sse"
        system, contents = self._encode(messages)

        generation: Dict[str, Any] = {"maxOutputTokens": max_tokens}
        if self.include_thoughts:
            generation["thinkingConfig"] = {"includeThoughts": True}

        payload: Dict[str, Any] = {"contents": contents,
                                   "generationConfig": generation}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if tools:
            payload["tools"] = self._tools(tools)
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        next_index = 0
        saw_tool_call = False
        # usageMetadata is cumulative on every frame, so only the last one
        # may be reported or the agent's token counter multiplies it.
        latest_usage: Optional[dict] = None
        try:
            for event in stream_sse_events(
                    url, payload, headers=self._headers(),
                    timeout=self.timeout, connect_timeout=self.connect_timeout,
                    stall_timeout=self.stall_timeout, retry=self.retry,
                    abort=self.abort):

                if event.get("error"):
                    yield error_chunk(classify_error(
                        body=json.dumps(event), provider=self.name, model=model))
                    return

                blocked = (event.get("promptFeedback") or {}).get("blockReason")
                if blocked:
                    yield error_chunk(classify_error(
                        body=json.dumps({"error": {"status": "BLOCKED",
                                                   "message": str(blocked)}}),
                        message=f"content filter: {blocked}",
                        provider=self.name, model=model))
                    return

                latest_usage = event.get("usageMetadata") or latest_usage
                candidates = event.get("candidates") or []
                candidate = candidates[0] if candidates else {}
                for part in ((candidate.get("content") or {}).get("parts") or []):
                    if "functionCall" in part:
                        call = part["functionCall"] or {}
                        saw_tool_call = True
                        yield CompletionChunk(tool_call_delta={
                            "index": next_index,
                            # Gemini has no call id; synthesise a stable one so
                            # the tool result can be matched back to a name.
                            "id": f"{call.get('name', 'call')}-{next_index}",
                            "name": call.get("name"),
                            # Args arrive whole, so one fragment is the call.
                            "arguments": json.dumps(call.get("args") or {}),
                        })
                        next_index += 1
                        continue
                    text = part.get("text")
                    if not text:
                        continue
                    if part.get("thought"):
                        yield CompletionChunk(reasoning=text)
                    else:
                        yield CompletionChunk(text=text)

                finish = candidate.get("finishReason")
                if finish:
                    if finish in _BLOCK_REASONS:
                        yield error_chunk(classify_error(
                            body=json.dumps({"error": {"status": finish}}),
                            message=f"content filter: {finish}",
                            provider=self.name, model=model))
                        return
                    if finish == "MALFORMED_FUNCTION_CALL":
                        yield error_chunk(classify_error(
                            message="the model emitted a malformed function call",
                            provider=self.name, model=model))
                        return
                    yield CompletionChunk(
                        stop_reason=self._stop_reason(finish, saw_tool_call),
                        usage=self._usage(latest_usage))
                    latest_usage = None
        except Aborted:
            return
        except Exception as e:
            yield error_chunk(error_from_exception(e, self.name, model))

    @staticmethod
    def _stop_reason(finish: str, saw_tool_call: bool) -> str:
        if finish == "STOP":
            return "tool_calls" if saw_tool_call else "stop"
        if finish == "MAX_TOKENS":
            return "length"
        return finish.lower()

    @staticmethod
    def _usage(raw: Optional[dict]) -> Optional[dict]:
        if not raw:
            return None
        # Gemini publishes disjoint counters: totalTokenCount is prompt +
        # tool-use prompt + candidates + thoughts.  promptTokenCount itself
        # includes cachedContentTokenCount.  The old adapter added thoughts to
        # output *and* reported them as reasoning, then added cached tokens to
        # input *and* cache_read -- double-counting both in the context meter
        # and cost estimate.  Keep the neutral Usage fields disjoint, like the
        # OpenAI adapters do.
        cached = max(0, raw.get("cachedContentTokenCount", 0))
        prompt = max(0, raw.get("promptTokenCount", 0))
        tool_input = max(0, raw.get("toolUsePromptTokenCount", 0))
        thoughts = max(0, raw.get("thoughtsTokenCount", 0))
        usage = {
            "input": max(0, prompt - cached) + tool_input,
            "output": max(0, raw.get("candidatesTokenCount", 0)),
        }
        if thoughts:
            usage["reasoning"] = thoughts
        if cached:
            usage["cache_read"] = cached
        return usage
