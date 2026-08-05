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

    def test_modern_models_get_output_config_and_never_a_budget(self):
        for model in ("claude-sonnet-5", "claude-opus-5", "claude-opus-4-7"):
            payload = self.payload_for(self.provider("high"), model)
            self.assertEqual(payload.get("output_config"), {"effort": "high"},
                             model)
            self.assertNotIn("thinking", payload, model)

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


if __name__ == "__main__":
    unittest.main()
