"""
The Anthropic dialect: effort, refusals, and one Haiku rule.

The Claude subscription OAuth profile these tests were born with is gone —
Anthropic serves subscription credentials only to approved clients, so the
profile could not work and only cluttered the model picker. What survives
here outlived it: the effort mapping (which broke every turn on
claude-sonnet-5 before it was fixed), the refusal-shaped 429, and the rule
that no login flow may launch a browser on Haiku — still live, because the
ChatGPT and SuperGrok device flows call the same function.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

class EffortUsesTheMechanismEachModelAccepts(unittest.TestCase):
    """Effort on Anthropic is output_config.effort — not a thinking budget.

    `thinking: {"type": "enabled", budget_tokens}` is deprecated on the 4.6
    generation and returns 400 on 4.7 and later, which includes
    claude-sonnet-5 — the shipped profile's own default model. Sending it
    there broke every turn.
    """

    def provider(self, effort="high"):
        from haikode.providers.anthropic import AnthropicProvider
        return AnthropicProvider(api_key="k", reasoning_effort=effort)

    def payload_for(self, provider, model, messages=None):
        from unittest.mock import patch
        from haikode.providers import anthropic as anthropic_module
        from haikode.schema import Msg
        captured = {}

        def fake_stream(url, payload, **kwargs):
            captured.update(payload)
            return iter(())

        with patch.object(anthropic_module, "stream_sse_events", fake_stream):
            list(provider.stream(messages or [Msg(role="user", content="x")],
                                 [], model, 8000))
        return captured

    def test_modern_models_get_effort_and_adaptive_thinking(self):
        for model in ("claude-sonnet-5", "claude-opus-5", "claude-opus-4-7"):
            payload = self.payload_for(self.provider("high"), model)
            self.assertEqual(payload.get("output_config"), {"effort": "high"},
                             model)
            self.assertEqual(payload.get("thinking"),
                             {"type": "adaptive", "display": "summarized"},
                             model)

    def test_older_families_still_get_a_thinking_budget(self):
        payload = self.payload_for(self.provider("high"), "claude-3-7-sonnet")
        self.assertEqual(payload["thinking"]["budget_tokens"], 64000 - 1024)
        self.assertNotIn("output_config", payload)

    def test_levels_differ_per_model(self):
        provider = self.provider()
        self.assertIn("xhigh", provider.reasoning_efforts("claude-sonnet-5"))
        self.assertNotIn("xhigh",
                         provider.reasoning_efforts("claude-sonnet-4-6"))
        self.assertIn("max", provider.reasoning_efforts("claude-sonnet-4-6"))
        self.assertNotIn("max", provider.reasoning_efforts("claude-opus-4-5"))
        self.assertEqual(provider.reasoning_efforts("claude-3-5-sonnet"), ())

    def test_an_unsupported_level_is_refused_not_sent(self):
        with self.assertRaises(ValueError):
            self.provider().set_reasoning_effort("xhigh", "claude-sonnet-4-6")

    def test_an_unlisted_model_gets_no_effort_rather_than_a_guess(self):
        payload = self.payload_for(self.provider("high"), "claude-future-9")
        self.assertNotIn("output_config", payload)
        self.assertNotIn("thinking", payload)

    def test_the_tool_loop_keeps_its_effort(self):
        # The old code suppressed thinking mid-loop; effort has no such
        # rule and is exactly where an agent loop needs it.
        from haikode.schema import Msg
        messages = [Msg(role="user", content="x"),
                    Msg(role="assistant", content="y"),
                    Msg(role="tool", content="out", tool_call_id="t1")]
        payload = self.payload_for(self.provider("high"), "claude-sonnet-5",
                                   messages)
        self.assertEqual(payload.get("output_config"), {"effort": "high"})

    def test_off_sends_nothing(self):
        payload = self.payload_for(self.provider("off"), "claude-sonnet-5")
        self.assertNotIn("output_config", payload)
        self.assertNotIn("thinking", payload)

    def test_current_output_limits_are_not_clamped_to_legacy_caps(self):
        from haikode.providers.anthropic import max_output_tokens
        self.assertEqual(max_output_tokens("claude-sonnet-5"), 128000)
        self.assertEqual(max_output_tokens("claude-opus-4-7"), 128000)
        self.assertEqual(max_output_tokens("claude-opus-4-5"), 64000)


class NoBrowserIsEverLaunchedOnHaiku(unittest.TestCase):
    """A browser launch mid-login put the reference machine in KDL.

    vm_page_fault in kernel space, thread "IPC Launch", inside
    enter_userspace — triggered the moment login spawned WebPositive.
    The kernel bug is Haiku's; not stepping on it is ours.
    """

    def test_open_authorization_url_is_inert_on_haiku(self):
        from unittest.mock import patch
        from haikode import oauth as oauth_module
        with patch.object(oauth_module.sys, "platform", "haiku1"), \
                patch.object(oauth_module.webbrowser, "open") as browser, \
                patch.object(oauth_module.subprocess, "Popen") as popen:
            oauth_module.open_authorization_url("https://claude.ai/x")
        browser.assert_not_called()
        popen.assert_not_called()

    def test_it_still_opens_elsewhere(self):
        from unittest.mock import patch
        from haikode import oauth as oauth_module
        with patch.object(oauth_module.sys, "platform", "darwin"), \
                patch.object(oauth_module.webbrowser, "open",
                             return_value=True) as browser:
            oauth_module.open_authorization_url("https://claude.ai/x")
        browser.assert_called_once()


class A429WithoutQuotaMetadataIsNotRetried(unittest.TestCase):
    """The refusal that arrives dressed as a rate limit.

    Measured against api.anthropic.com with a valid subscription token:
    HTTP 429 in 0.49s, org and workspace echoed back, body
    {"type":"error","error":{"type":"rate_limit_error","message":"Error"}},
    and not one anthropic-ratelimit-* header. A real limit says how much
    of what is left; this one refuses the caller. Retrying it cannot
    succeed and only hammers a credential already being refused.
    """

    def classify(self, headers):
        from haikode.providers.base import classify_error
        return classify_error(
            status=429,
            body=json.dumps({"type": "error", "error": {
                "type": "rate_limit_error", "message": "Error"}}),
            provider="claude", model="claude-sonnet-5", headers=headers)

    def test_no_ratelimit_headers_means_terminal(self):
        error = self.classify({"x-should-retry": "true",
                               "anthropic-organization-id": "org-1"})
        self.assertFalse(error.retryable)
        self.assertEqual(error.kind, "auth")
        self.assertIn("not entitled", error.message)

    def test_a_real_rate_limit_is_still_retried(self):
        error = self.classify({"anthropic-ratelimit-requests-remaining": "0",
                               "retry-after": "30"})
        self.assertTrue(error.retryable)
        self.assertEqual(error.kind, "rate_limit")

    def test_retry_after_alone_is_enough_to_stay_a_rate_limit(self):
        self.assertTrue(self.classify({"retry-after": "12"}).retryable)

    def test_unknown_headers_do_not_guess(self):
        # Nothing observed: keep the old, retrying behaviour.
        self.assertTrue(self.classify(None).retryable)



class ThinkingBlocksSurviveTheRoundTrip(unittest.TestCase):
    """The API requires its own thinking blocks handed back unmodified.

    Rebuilding an assistant turn without them — which is what haikode did
    until the blocks had somewhere to live — drops the model's reasoning
    from earlier steps of the same tool loop, and filtering out a
    redacted_thinking block is a 400 rather than a degradation.
    """

    def stream_events(self, provider, events, model="claude-sonnet-5"):
        from unittest.mock import patch
        from haikode.providers import anthropic as anthropic_module
        from haikode.schema import Msg
        with patch.object(anthropic_module, "stream_sse_events",
                          lambda *a, **k: iter(events)):
            return list(provider.stream([Msg(role="user", content="x")], [],
                                        model, 4000))

    def provider(self):
        from haikode.providers.anthropic import AnthropicProvider
        return AnthropicProvider(api_key="k")

    def test_a_signed_thinking_block_is_captured_whole(self):
        chunks = self.stream_events(self.provider(), [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "step one, "}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "thinking_delta", "thinking": "step two"}},
            {"type": "content_block_delta", "index": 0,
             "delta": {"type": "signature_delta", "signature": "SIGabc"}},
            {"type": "content_block_stop", "index": 0},
        ])
        blocks = [c.reasoning_block for c in chunks if c.reasoning_block]
        self.assertEqual(blocks, [{"type": "thinking",
                                   "thinking": "step one, step two",
                                   "signature": "SIGabc"}])
        # The readable copy still reaches the screen.
        self.assertEqual("".join(c.reasoning for c in chunks if c.reasoning),
                         "step one, step two")

    def test_redacted_thinking_is_carried_not_dropped(self):
        chunks = self.stream_events(self.provider(), [
            {"type": "content_block_start", "index": 0,
             "content_block": {"type": "redacted_thinking", "data": "ENC"}},
            {"type": "content_block_stop", "index": 0},
        ])
        blocks = [c.reasoning_block for c in chunks if c.reasoning_block]
        self.assertEqual(blocks, [{"type": "redacted_thinking",
                                   "data": "ENC"}])

    def encode(self, message, model="claude-sonnet-5"):
        from haikode.providers.anthropic import AnthropicProvider
        from haikode.schema import Msg
        _system, out = AnthropicProvider._encode(
            [Msg(role="user", content="q"), message], model)
        return out[-1]

    def assistant(self, model="claude-sonnet-5", dialect="anthropic"):
        from haikode.schema import Msg, ToolCall
        return Msg(role="assistant", content="working",
                   tool_calls=[ToolCall(id="t1", name="read",
                                        arguments={"path": "x"})],
                   reasoning={"dialect": dialect, "model": model,
                              "blocks": [{"type": "thinking",
                                          "thinking": "why",
                                          "signature": "SIG"}]})

    def test_thinking_goes_back_first_and_unmodified(self):
        encoded = self.encode(self.assistant())
        kinds = [b["type"] for b in encoded["content"]]
        self.assertEqual(kinds, ["thinking", "text", "tool_use"])
        self.assertEqual(encoded["content"][0],
                         {"type": "thinking", "thinking": "why",
                          "signature": "SIG"})

    def test_another_dialects_blocks_are_never_replayed(self):
        # A signature is issued by one dialect. Posting it to another is
        # handing an opaque blob to a provider that never signed it.
        encoded = self.encode(self.assistant(dialect="openai"))
        self.assertEqual([b["type"] for b in encoded["content"]],
                         ["text", "tool_use"])

    def test_a_model_switch_drops_them(self):
        encoded = self.encode(self.assistant(model="claude-opus-4-7"),
                              model="claude-sonnet-5")
        self.assertEqual([b["type"] for b in encoded["content"]],
                         ["text", "tool_use"])

    def test_an_assistant_turn_with_only_thinking_still_carries_it(self):
        from haikode.schema import Msg
        message = Msg(role="assistant", content="done",
                      reasoning={"dialect": "anthropic",
                                 "model": "claude-sonnet-5",
                                 "blocks": [{"type": "thinking",
                                             "thinking": "w",
                                             "signature": "S"}]})
        encoded = self.encode(message)
        self.assertEqual([b["type"] for b in encoded["content"]],
                         ["thinking", "text"])

    def test_a_plain_turn_is_unchanged(self):
        from haikode.schema import Msg
        encoded = self.encode(Msg(role="assistant", content="hello"))
        self.assertEqual(encoded, {"role": "assistant", "content": "hello"})


class ReasoningSurvivesTheDatabase(unittest.TestCase):
    def test_blocks_round_trip_through_a_session(self):
        import os
        import tempfile
        from haikode.session import SessionStore
        from haikode.schema import Msg
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(os.path.join(directory, "s.db"))
            session = store.new_session(cwd=directory, provider="anthropic",
                                        model="claude-sonnet-5")
            reasoning = {"dialect": "anthropic", "model": "claude-sonnet-5",
                         "blocks": [{"type": "thinking", "thinking": "w",
                                     "signature": "S"}]}
            session.append(Msg(role="assistant", content="a",
                               reasoning=reasoning))
            session.reload()
            self.assertEqual(session.messages[-1].reasoning, reasoning)

    def test_a_row_without_the_column_reads_as_empty(self):
        from haikode.session import _deserialize_reasoning
        for raw in (None, "", "not json", "[1,2]"):
            self.assertEqual(_deserialize_reasoning(raw), {})



class TheReplayCannotBrickASession(unittest.TestCase):
    """A refused block lives in the history, so its 400 would repeat forever.

    That is the failure mode pair_tool_messages exists to prevent for tool
    calls; reasoning blocks needed the same escape hatch.
    """

    def test_an_unsigned_thinking_block_is_never_sent(self):
        from haikode.providers.anthropic import AnthropicProvider
        from haikode.schema import Msg
        message = Msg(role="assistant", content="a",
                      reasoning={"dialect": "anthropic",
                                 "model": "claude-sonnet-5",
                                 "blocks": [
                                     {"type": "thinking", "thinking": "x",
                                      "signature": ""},
                                     {"type": "redacted_thinking",
                                      "data": "ENC"}]})
        kept = AnthropicProvider._preserved_blocks(message, "claude-sonnet-5")
        # The redacted block has no signature of its own and must survive;
        # the unsigned thinking block must not.
        self.assertEqual([b["type"] for b in kept], ["redacted_thinking"])

    def test_the_model_gate_ignores_casing(self):
        from haikode.providers.anthropic import AnthropicProvider
        from haikode.schema import Msg
        message = Msg(role="assistant", reasoning={
            "dialect": "anthropic", "model": "Claude-Sonnet-5",
            "blocks": [{"type": "thinking", "thinking": "x",
                        "signature": "S"}]})
        self.assertEqual(len(AnthropicProvider._preserved_blocks(
            message, "claude-sonnet-5")), 1)

    def agent_with_history(self):
        from unittest.mock import MagicMock
        from haikode.agent import Agent
        from haikode.schema import Msg
        agent = Agent.__new__(Agent)
        agent.messages = [
            Msg(role="assistant", content="a",
                reasoning={"dialect": "anthropic", "model": "m",
                           "blocks": [{"type": "thinking", "thinking": "x",
                                       "signature": "S"}]}),
            Msg(role="user", content="b"),
        ]
        return agent

    def failure(self, status, message):
        from haikode.agent import ProviderFailure
        return ProviderFailure({"status": status, "message": message,
                                "body": ""})

    def test_a_thinking_400_drops_the_blocks(self):
        agent = self.agent_with_history()
        dropped = agent._forget_reasoning(
            self.failure(400, "Invalid signature on thinking block"))
        self.assertTrue(dropped)
        self.assertEqual(agent.messages[0].reasoning, {})

    def test_an_unrelated_failure_keeps_them(self):
        # A provider outage must not be mistaken for a poisoned block and
        # cost a second request.
        agent = self.agent_with_history()
        self.assertFalse(agent._forget_reasoning(
            self.failure(500, "internal server error")))
        self.assertFalse(agent._forget_reasoning(
            self.failure(400, "max_tokens is too large")))
        self.assertTrue(agent.messages[0].reasoning)


class CompactionKeepsTheBlocks(unittest.TestCase):
    def test_compact_and_undo_round_trips_reasoning(self):
        import os
        import tempfile
        from haikode.session import SessionStore
        from haikode.schema import Msg
        reasoning = {"dialect": "anthropic", "model": "claude-sonnet-5",
                     "blocks": [{"type": "thinking", "thinking": "w",
                                 "signature": "S"}]}
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(os.path.join(directory, "s.db"))
            session = store.new_session(cwd=directory, provider="anthropic",
                                        model="claude-sonnet-5")
            session.append(Msg(role="user", content="q"))
            session.append(Msg(role="assistant", content="a",
                               reasoning=reasoning))
            session.append(Msg(role="user", content="later"))
            session.compact_now(keep_last=1, summary="summary of the above")
            session.restore_compaction()
            session.reload()
            restored = [m for m in session.messages if m.role == "assistant"]
            self.assertTrue(restored, "the folded turn came back")
            self.assertEqual(restored[0].reasoning, reasoning)


class TheContextMeterCountsThem(unittest.TestCase):
    def test_blocks_add_to_the_estimate(self):
        from haikode.context import message_tokens
        from haikode.schema import Msg
        plain = Msg(role="assistant", content="hello")
        with_blocks = Msg(role="assistant", content="hello", reasoning={
            "dialect": "anthropic", "model": "m",
            "blocks": [{"type": "thinking", "thinking": "w" * 400,
                        "signature": "S"}]})
        self.assertGreater(message_tokens(with_blocks),
                           message_tokens(plain) + 50)


if __name__ == "__main__":
    unittest.main()
