"""Local ChatGPT and SuperGrok subscription providers."""
import json
import time
import uuid
from typing import Iterator, List, Optional

from ..net import (DEFAULT_TIMEOUT, Aborted, RetryPolicy, USER_AGENT,
                   sse_json_events)
from ..oauth import (CHATGPT_API_BASE, OAuthStore, _is_expired,
                     access_token)
from ..schema import CompletionChunk, Msg, ToolSpec
from .base import (Provider, classify_error, error_chunk, error_from_exception,
                   reasoning_from_delta)
from .openai_compat import OpenAICompatProvider

# The backend answers `server_error` intermittently on requests that succeed
# when repeated. Three attempts with a short, growing pause: enough to ride
# out a blip, few enough that a genuine outage still fails promptly.
RETRYABLE_STREAM_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.5


class ChatGPTSubscriptionProvider(Provider):
    """OpenAI Responses API over a locally stored ChatGPT OAuth token."""

    name = "chatgpt"
    GPT56_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")
    LEGACY_EFFORTS = ("low", "medium", "high")

    def __init__(self, store: OAuthStore, base_url: str = CHATGPT_API_BASE,
                 retry: Optional[RetryPolicy] = None,
                 timeout: int = DEFAULT_TIMEOUT,
                 connect_timeout: Optional[float] = None,
                 stall_timeout: Optional[float] = None,
                 abort=None, reasoning_effort: str = "medium"):
        self.store = store
        self.base_url = base_url.rstrip("/")
        self.session_id = str(uuid.uuid4())
        # Credentials for this turn; see invalidate_auth().
        self._auth = None
        self.retry = retry
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.stall_timeout = stall_timeout
        self.abort = abort
        self.reasoning_effort = reasoning_effort

    def context_limit(self, model: str, configured: int) -> tuple:
        """ChatGPT's Codex backend has lower effective limits than public API."""
        normalized = (model or "").lower()
        if "gpt-5.6" in normalized:
            return 500000, "ChatGPT backend profile"
        if "gpt-5.5" in normalized:
            return 400000, "ChatGPT backend profile"
        return configured, "configuration"

    def input_limit(self, model: str, window: int) -> tuple:
        """The backend's input share of the window.

        opencode records the split this backend enforces (codex.ts):
        gpt-5.6 is 500k context = 372k input + 128k output, gpt-5.5 is
        400k = 272k + 128k. Budgeting a prompt against the full window let
        it grow 128k past what a request may be, and the refusal came back
        as a generic server_error — nothing that looked like size.
        """
        normalized = (model or "").lower()
        if "gpt-5.6" in normalized:
            return 372000, "ChatGPT backend profile"
        if "gpt-5.5" in normalized:
            return 272000, "ChatGPT backend profile"
        return window, "context window"

    def reasoning_efforts(self, model: str) -> tuple:
        if "gpt-5.6" in (model or "").lower():
            return self.GPT56_EFFORTS
        return self.LEGACY_EFFORTS

    def set_reasoning_effort(self, effort: str, model: str) -> str:
        value = (effort or "").strip().lower()
        allowed = self.reasoning_efforts(model)
        if value not in allowed:
            raise ValueError("reasoning effort must be one of "
                             + ", ".join(allowed))
        self.reasoning_effort = value
        return value

    def invalidate_auth(self) -> None:
        """Force the next request to re-read the credential file.

        Called at the start of every user turn, and by /logout. That bounds
        how long this process can keep using credentials another process has
        replaced or removed to a single turn, which is the price of not
        reading the file hundreds of times.
        """
        self._auth = None

    def _headers(self) -> dict:
        # Read once per turn, not once per request. haikode asks the model
        # many times inside one turn — 346 requests in a single observed
        # session — and each one used to re-read oauth.json from disk. That
        # is why haikode, and not other programs, kept hitting a filesystem
        # anomaly on Haiku: a read that comes back empty roughly once in
        # thousands is invisible to a program that reads the file once at
        # startup, and near-certain for one that reads it 346 times a
        # session. Three such reads were captured in 14 hours.
        #
        # The access token is cached; the refresh token deliberately is not
        # used from here. When the cached token nears expiry this falls
        # through to access_token(), which re-reads under the store lock so a
        # rotation performed by another process is not lost.
        auth = self._auth
        if auth is None or _is_expired(auth):
            auth = access_token("chatgpt", self.store)
            self._auth = auth
        headers = {
            "Authorization": f"Bearer {auth['access']}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": USER_AGENT,
            "originator": "hai",
            "session-id": self.session_id,
        }
        if auth.get("account_id"):
            headers["ChatGPT-Account-Id"] = auth["account_id"]
        return headers

    @staticmethod
    def _request_messages(messages: List[Msg]):
        """Encode the history as Responses API input items.

        Tool traffic must use the API's own item types: a tool result
        flattened into a user message reads, to the model, as the user
        pasting JSON at it — with its own function_call absent from the
        replay it has no memory of calling the tool, answers the "user's"
        JSON, and loops. This burned a full step budget in the field twice
        before the encoding was fixed.
        """
        instructions = []
        items = []
        for message in messages:
            if message.role == "system":
                instructions.append(message.content)
            elif message.role == "tool":
                items.append({
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content or "",
                })
            elif message.role == "assistant":
                if message.content:
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text",
                                     "text": message.content}],
                    })
                for call in message.tool_calls:
                    items.append({
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    })
            elif message.content:
                items.append({
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text",
                                 "text": message.content}],
                })
        return "\n\n".join(instructions), items

    def stream(self, messages: List[Msg], tools: List[ToolSpec], model: str,
               max_tokens: int) -> Iterator[CompletionChunk]:
        """Stream one turn, retrying a transient backend failure.

        The backend intermittently answers `server_error` — a generic 500
        with a request id and nothing else — on requests that succeed when
        repeated. Measured while chasing it: a 308k-token history that had
        failed earlier went through unchanged, an over-long prompt returns a
        clean `context_overflow` instead, and a tool chain with no reasoning
        items is accepted. So it is not size, not shape, and not ours.

        net.py already retries transport failures, but an error delivered as
        an SSE event never reached that path: `classify_error` marked it
        retryable and the agent raised on it anyway. The rule here is the same
        one net.py uses — retry only while nothing has been handed to the
        caller, because re-sending a turn whose tokens or tool calls are
        already out would duplicate them.
        """
        attempts = max(1, RETRYABLE_STREAM_ATTEMPTS)
        for attempt in range(attempts):
            delivered = False
            for chunk in self._stream_once(messages, tools, model, max_tokens):
                failure = (chunk.usage or {}).get("error") if chunk.usage else None
                if (failure and not delivered and attempt < attempts - 1
                        and failure.get("retryable")):
                    time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    break                       # same request, new attempt
                if chunk.text or chunk.tool_call_delta or chunk.reasoning:
                    delivered = True
                yield chunk
            else:
                return                          # ran to completion

    def _stream_once(self, messages: List[Msg], tools: List[ToolSpec],
                     model: str, max_tokens: int) -> Iterator[CompletionChunk]:
        instructions, items = self._request_messages(messages)
        payload = {
            "model": model,
            "instructions": instructions,
            "input": items,
            "tools": [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "strict": False,
                }
                for tool in tools
            ],
            "tool_choice": "auto" if tools else "none",
            "parallel_tool_calls": False,
            "reasoning": {"effort": self.reasoning_effort, "summary": "auto"},
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
        }
        # Responses API numbers its output items; tool calls are keyed by that
        # index so fragments reassemble in the agent's accumulator.
        calls: dict = {}
        try:
            for event in sse_json_events(
                    f"{self.base_url}/responses", payload,
                    headers=self._headers(), timeout=self.timeout,
                    connect_timeout=self.connect_timeout,
                    stall_timeout=self.stall_timeout, retry=self.retry,
                    abort=self.abort):
                kind = event.get("type", "")

                if kind == "response.output_text.delta" and event.get("delta"):
                    yield CompletionChunk(text=str(event["delta"]))

                elif kind in ("response.reasoning_summary_text.delta",
                              "response.reasoning_text.delta"):
                    text = event.get("delta") or reasoning_from_delta(event)
                    if text:
                        yield CompletionChunk(reasoning=str(text))

                elif kind == "response.output_item.added":
                    item = event.get("item") or {}
                    if item.get("type") == "function_call":
                        index = event.get("output_index", len(calls))
                        calls[index] = len(calls)
                        yield CompletionChunk(tool_call_delta={
                            "index": calls[index],
                            "id": item.get("call_id") or item.get("id"),
                            "name": item.get("name"),
                            "arguments": "",
                        })

                elif kind == "response.function_call_arguments.delta":
                    index = event.get("output_index")
                    if index in calls and event.get("delta"):
                        yield CompletionChunk(tool_call_delta={
                            "index": calls[index], "id": None, "name": None,
                            "arguments": str(event["delta"])})

                elif kind == "response.completed":
                    usage = ((event.get("response") or {}).get("usage") or {})
                    yield CompletionChunk(
                        stop_reason="tool_calls" if calls else "stop",
                        usage=self._usage(usage))
                    return

                elif kind in ("response.failed", "response.incomplete", "error"):
                    yield error_chunk(classify_error(
                        body=json.dumps(self._failure_body(event, kind)),
                        provider=self.name, model=model))
                    return
        except Aborted:
            return
        except Exception as exc:
            yield error_chunk(error_from_exception(exc, self.name, model))

    @staticmethod
    def _usage(raw: dict) -> Optional[dict]:
        """Normalize totals so cache/reasoning are counted exactly once."""
        if not raw:
            return None
        in_details = raw.get("input_tokens_details") or {}
        out_details = raw.get("output_tokens_details") or {}
        cached = (in_details.get("cached_tokens", 0)
                  if isinstance(in_details, dict) else 0)
        reasoning = (out_details.get("reasoning_tokens", 0)
                     if isinstance(out_details, dict) else 0)
        return {
            "input": max(0, raw.get("input_tokens", 0) - cached),
            "output": max(0, raw.get("output_tokens", 0) - reasoning),
            "cache_read": cached,
            "reasoning": reasoning,
        }

    @staticmethod
    def _failure_body(event: dict, kind: str) -> dict:
        """Normalise the Responses API's three failure shapes onto one body."""
        response = event.get("response") or {}
        error = response.get("error") or event.get("error") or {}
        if isinstance(error, str):
            error = {"message": error}
        message = (error.get("message")
                   or (response.get("incomplete_details") or {}).get("reason")
                   or kind)
        return {"error": {"message": message,
                          "code": error.get("code") or "",
                          "type": error.get("type") or ""}}


class SuperGrokSubscriptionProvider(OpenAICompatProvider):
    """xAI Chat Completions using a locally refreshed SuperGrok token."""

    def __init__(self, store: OAuthStore,
                 base_url: str = "https://api.x.ai/v1", **kwargs):
        super().__init__(base_url=base_url, api_key=None, name="supergrok",
                         **kwargs)
        self.store = store

    def stream(self, messages: List[Msg], tools: List[ToolSpec], model: str,
               max_tokens: int) -> Iterator[CompletionChunk]:
        try:
            self.api_key = access_token("supergrok", self.store)["access"]
        except Exception as exc:
            yield error_chunk(error_from_exception(exc, self.name, model))
            return
        yield from super().stream(messages, tools, model, max_tokens)
