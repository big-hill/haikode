"""
Claude subscription OAuth: the PKCE paste-code flow and its provider.

Unlike chatgpt/supergrok there is no device flow to poll — claude.ai shows a
`code#state` string the user carries back by hand. What is proven here: the
authorization URL is a valid PKCE request, the pasted code survives the ways
users actually paste it, refresh speaks JSON to Anthropic's token endpoint,
and the provider authenticates with Bearer + the OAuth beta header while
inheriting the whole Anthropic wire format.
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode.config import Config  # noqa: E402
from haikode.oauth import (CLAUDE_CLIENT_ID, CLAUDE_OAUTH_BETA,  # noqa: E402
                           CLAUDE_REDIRECT_URI, CLAUDE_TOKEN_URL, OAuthError,
                           OAuthStore, begin_claude_authorization,
                           exchange_claude_code, refresh_tokens)
from haikode.providers.anthropic import AnthropicProvider  # noqa: E402
from haikode.providers.subscription import ClaudeSubscriptionProvider  # noqa: E402


class FakeResponse:
    def __init__(self, value, status=200):
        self.value = value
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self):
        return json.dumps(self.value).encode()


TOKENS = {"access_token": "access-claude", "refresh_token": "refresh-claude",
          "expires_in": 3600}


class TheAuthorizationUrlIsAValidPkceRequest(unittest.TestCase):
    def test_challenge_is_the_verifier_hashed(self):
        pending = begin_claude_authorization()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(pending["url"]).query)
        expected = base64.urlsafe_b64encode(hashlib.sha256(
            pending["verifier"].encode()).digest()).decode().rstrip("=")
        self.assertEqual(query["code_challenge"], [expected])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["client_id"], [CLAUDE_CLIENT_ID])
        self.assertEqual(query["redirect_uri"], [CLAUDE_REDIRECT_URI])
        # `code=true` is what makes claude.ai display the code to paste.
        self.assertEqual(query["code"], ["true"])
        self.assertEqual(query["state"], [pending["verifier"]])

    def test_each_call_gets_a_fresh_verifier(self):
        self.assertNotEqual(begin_claude_authorization()["verifier"],
                            begin_claude_authorization()["verifier"])


class TheExchangeSurvivesHowUsersPaste(unittest.TestCase):
    def exchange(self, pasted):
        seen = {}

        def opener(request, timeout=0):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data)
            return FakeResponse(TOKENS)

        tokens = exchange_claude_code(pasted, "the-verifier", opener=opener)
        return seen, tokens

    def test_the_whole_string_with_state(self):
        seen, tokens = self.exchange("  the-code#the-state \n")
        self.assertEqual(seen["url"], CLAUDE_TOKEN_URL)
        self.assertEqual(seen["body"]["code"], "the-code")
        self.assertEqual(seen["body"]["state"], "the-state")
        self.assertEqual(seen["body"]["code_verifier"], "the-verifier")
        self.assertEqual(seen["body"]["grant_type"], "authorization_code")
        self.assertEqual(tokens["access"], "access-claude")
        self.assertEqual(tokens["type"], "oauth")

    def test_just_the_code_half_falls_back_to_the_verifier_state(self):
        seen, _tokens = self.exchange("the-code")
        self.assertEqual(seen["body"]["state"], "the-verifier")

    def test_empty_paste_is_a_clear_error_not_a_request(self):
        with self.assertRaises(OAuthError):
            exchange_claude_code("   ", "v",
                                 opener=lambda *a, **k: self.fail("no request"))

    def test_a_rejected_code_names_the_reason(self):
        def opener(request, timeout=0):
            return FakeResponse({"error": "invalid_grant"}, status=400)

        with self.assertRaises(OAuthError) as caught:
            exchange_claude_code("bad#state", "v", opener=opener)
        self.assertIn("invalid_grant", str(caught.exception))


class RefreshSpeaksJsonToAnthropic(unittest.TestCase):
    def test_refresh_posts_json_and_keeps_the_old_refresh_token(self):
        seen = {}

        def opener(request, timeout=0):
            seen["url"] = request.full_url
            seen["content_type"] = request.headers.get("Content-type")
            seen["body"] = json.loads(request.data)
            return FakeResponse({"access_token": "rotated", "expires_in": 60})

        refreshed = refresh_tokens("claude", {"refresh": "refresh-claude"},
                                   opener=opener)
        self.assertEqual(seen["url"], CLAUDE_TOKEN_URL)
        self.assertEqual(seen["content_type"], "application/json")
        self.assertEqual(seen["body"]["grant_type"], "refresh_token")
        self.assertEqual(refreshed["access"], "rotated")
        # No refresh_token in the answer means the old one is still good.
        self.assertEqual(refreshed["refresh"], "refresh-claude")


class TheProviderAuthenticatesAsASubscription(unittest.TestCase):
    def provider(self):
        directory = tempfile.mkdtemp()
        store = OAuthStore(os.path.join(directory, "oauth.json"))
        store.set("claude", {"type": "oauth", "access": "access-claude",
                             "refresh": "refresh-claude",
                             "expires": int((__import__("time").time() + 3600) * 1000)})
        return ClaudeSubscriptionProvider(store)

    def test_bearer_plus_beta_header_and_no_api_key(self):
        headers = self.provider()._headers()
        self.assertEqual(headers["Authorization"], "Bearer access-claude")
        self.assertEqual(headers["anthropic-beta"], CLAUDE_OAUTH_BETA)
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("x-api-key", headers)

    def test_the_wire_format_is_inherited_not_reimplemented(self):
        provider = self.provider()
        self.assertIsInstance(provider, AnthropicProvider)
        self.assertEqual(provider.name, "claude")
        self.assertEqual(provider.base_url, "https://api.anthropic.com")

    def test_auth_is_cached_for_the_turn_and_invalidated_between(self):
        provider = self.provider()
        provider._headers()
        first = provider._auth
        self.assertIsNotNone(first)
        provider._headers()
        self.assertIs(provider._auth, first)      # no re-read inside a turn
        provider.invalidate_auth()
        self.assertIsNone(provider._auth)


class TheProfileIsWiredEverywhere(unittest.TestCase):
    def config(self, directory):
        return Config(os.path.join(directory, "config.json"))

    def test_default_config_carries_the_claude_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            prov = self.config(directory).data["providers"]["claude"]
        self.assertEqual(prov["dialect"], "claude")
        self.assertEqual(prov["oauth_provider"], "claude")
        self.assertFalse(prov["requires_key"])

    def test_build_provider_dispatches_the_claude_dialect(self):
        from haikode.runtime import build_provider
        with tempfile.TemporaryDirectory() as directory:
            provider = build_provider(self.config(directory), "claude")
        self.assertIsInstance(provider, ClaudeSubscriptionProvider)

    def test_device_authorization_still_refuses_claude(self):
        # The device path must never be reachable for the paste-code flow:
        # a refactor that routes claude into it would hang polling a URL
        # that does not exist.
        from haikode.oauth import begin_device_authorization
        with self.assertRaises(OAuthError):
            begin_device_authorization("claude")


if __name__ == "__main__":
    unittest.main()
