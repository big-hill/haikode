import base64
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from haikode.config import Config
from haikode import configtool, oauth as oauth_module
from haikode.oauth import (CHATGPT_CLIENT_ID, OAuthStore, XAI_CLIENT_ID,
                       XAI_DEVICE_GRANT, access_token,
                       begin_device_authorization,
                       poll_device_authorization, refresh_tokens)
from haikode.providers.subscription import (ChatGPTSubscriptionProvider,
                                        SuperGrokSubscriptionProvider)
from haikode.schema import Msg


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


def http_error(request, status, value):
    return urllib.error.HTTPError(
        request.full_url, status, "fake", {},
        io.BytesIO(json.dumps(value).encode()))


def jwt(claims):
    encoded = base64.urlsafe_b64encode(
        json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


class ChatGPTResponsesHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.payload = json.loads(self.rfile.read(length))
        self.server.authorization = self.headers.get("Authorization")
        self.server.account = self.headers.get("ChatGPT-Account-Id")
        self.server.originator = self.headers.get("originator")
        events = [
            {"type": "response.output_text.delta", "delta": "LOCAL_"},
            {"type": "response.output_text.delta", "delta": "OAUTH_OK"},
            {"type": "response.completed", "response": {}},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


class SuperGrokHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.server.payload = json.loads(self.rfile.read(length))
        self.server.authorization = self.headers.get("Authorization")
        chunks = [
            {"choices": [{"delta": {"content": "SUPER_"}}]},
            {"choices": [{"delta": {"content": "LOCAL_OK"}}]},
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        body += "data: [DONE]\n\n"
        encoded = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


class OAuthTests(unittest.TestCase):
    def test_chatgpt_device_flow_and_private_store(self):
        calls = []
        account_token = jwt({
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-local"
            }
        })

        def opener(request, timeout=0):
            calls.append(request)
            if request.full_url.endswith("/deviceauth/usercode"):
                self.assertEqual(json.loads(request.data),
                                 {"client_id": CHATGPT_CLIENT_ID})
                return FakeResponse({
                    "device_auth_id": "device-id",
                    "user_code": "ABCD-EFGH",
                    "interval": 1,
                    "expires_in": 600,
                })
            if request.full_url.endswith("/deviceauth/token"):
                poll_count = sum(r.full_url.endswith("/deviceauth/token")
                                 for r in calls)
                if poll_count == 1:
                    raise http_error(request, 403, {"error": "pending"})
                return FakeResponse({
                    "authorization_code": "authorization-code",
                    "code_verifier": "verifier",
                })
            values = urllib.parse.parse_qs(request.data.decode())
            self.assertEqual(values["grant_type"], ["authorization_code"])
            self.assertEqual(values["code_verifier"], ["verifier"])
            return FakeResponse({
                "access_token": "access-local",
                "refresh_token": "refresh-local",
                "id_token": account_token,
                "expires_in": 3600,
            })

        pending = begin_device_authorization("chatgpt", opener=opener)
        tokens = poll_device_authorization(
            "chatgpt", pending, opener=opener, sleep=lambda _seconds: None)
        self.assertEqual(tokens["account_id"], "acct-local")
        self.assertEqual(tokens["access"], "access-local")

        with tempfile.TemporaryDirectory() as directory:
            store = OAuthStore(os.path.join(directory, "oauth.json"))
            store.set("chatgpt", tokens)
            self.assertEqual(store.get("chatgpt")["refresh"], "refresh-local")
            self.assertEqual(os.stat(store.path).st_mode & 0o777, 0o600)

    def test_supergrok_device_flow_and_refresh_rotation(self):
        calls = []

        def opener(request, timeout=0):
            values = urllib.parse.parse_qs(request.data.decode())
            calls.append((request.full_url, values))
            if request.full_url.endswith("/device/code"):
                self.assertEqual(values["client_id"], [XAI_CLIENT_ID])
                return FakeResponse({
                    "device_code": "grok-device",
                    "user_code": "GROK-CODE",
                    "verification_uri": "https://auth.x.ai/device",
                    "expires_in": 300,
                    "interval": 1,
                })
            if values.get("grant_type") == [XAI_DEVICE_GRANT]:
                polls = sum(v.get("grant_type") == [XAI_DEVICE_GRANT]
                            for _, v in calls)
                if polls == 1:
                    raise http_error(request, 400,
                                     {"error": "authorization_pending"})
                return FakeResponse({
                    "access_token": "grok-access",
                    "refresh_token": "grok-refresh",
                    "expires_in": 3600,
                })
            self.assertEqual(values["grant_type"], ["refresh_token"])
            return FakeResponse({
                "access_token": "grok-refreshed",
                "refresh_token": "grok-refresh-rotated",
                "expires_in": 3600,
            })

        pending = begin_device_authorization("supergrok", opener=opener)
        tokens = poll_device_authorization(
            "supergrok", pending, opener=opener, sleep=lambda _seconds: None)
        refreshed = refresh_tokens("supergrok", tokens, opener=opener)
        self.assertEqual(refreshed["access"], "grok-refreshed")
        self.assertEqual(refreshed["refresh"], "grok-refresh-rotated")

    def test_expired_access_token_is_refreshed_and_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OAuthStore(os.path.join(directory, "oauth.json"))
            store.set("chatgpt", {
                "access": "old", "refresh": "refresh", "expires": 1,
                "account_id": "acct",
            })
            replacement = {
                "access": "new", "refresh": "rotated",
                "expires": int((time.time() + 3600) * 1000),
                "account_id": "acct",
            }
            with patch("haikode.oauth.refresh_tokens", return_value=replacement):
                actual = access_token("chatgpt", store)
            self.assertEqual(actual["access"], "new")
            self.assertEqual(store.get("chatgpt")["refresh"], "rotated")

    def test_refresh_yields_to_the_process_that_got_the_lock_first(self):
        """Re-reading inside the lock is what stops a double token rotation."""
        with tempfile.TemporaryDirectory() as directory:
            store = OAuthStore(os.path.join(directory, "oauth.json"))
            store.set("chatgpt", {"access": "expired", "refresh": "refresh",
                                  "expires": 1})
            fresh = {"type": "oauth", "access": "from-the-other-process",
                     "refresh": "rotated-once",
                     "expires": int((time.time() + 3600) * 1000)}

            @contextlib.contextmanager
            def lock_held_by_someone_who_refreshed(_path):
                # _write bypasses set(), which would re-enter the lock.
                store._write({"chatgpt": fresh})
                yield

            def must_not_refresh(*_args, **_kwargs):
                raise AssertionError("rotated a refresh token that was fresh")

            with (patch.object(oauth_module, "settings_lock",
                               lock_held_by_someone_who_refreshed),
                  patch.object(oauth_module, "refresh_tokens",
                               must_not_refresh)):
                actual = access_token("chatgpt", store)

            self.assertEqual(actual["access"], "from-the-other-process")
            self.assertEqual(store.get("chatgpt")["refresh"], "rotated-once")

    def test_chatgpt_responses_contract_uses_local_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), ChatGPTResponsesHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = OAuthStore(os.path.join(directory, "oauth.json"))
                store.set("chatgpt", {
                    "access": "local-access",
                    "refresh": "local-refresh",
                    "expires": int((time.time() + 3600) * 1000),
                    "account_id": "acct-local",
                })
                base = f"http://127.0.0.1:{server.server_port}"
                provider = ChatGPTSubscriptionProvider(store, base)
                chunks = list(provider.stream([
                    Msg(role="system", content="Be concise"),
                    Msg(role="user", content="hello"),
                ], tools=[], model="gpt-5.4", max_tokens=128))
            self.assertEqual("".join(chunk.text for chunk in chunks),
                             "LOCAL_OAUTH_OK")
            self.assertEqual(server.authorization, "Bearer local-access")
            self.assertEqual(server.account, "acct-local")
            self.assertEqual(server.originator, "hai")
            self.assertEqual(server.payload["model"], "gpt-5.4")
            self.assertEqual(server.payload["instructions"], "Be concise")
            self.assertEqual(server.payload["input"][0]["content"][0], {
                "type": "input_text", "text": "hello"})
            self.assertTrue(server.payload["stream"])
            self.assertFalse(server.payload["store"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_supergrok_chat_contract_uses_local_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), SuperGrokHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = OAuthStore(os.path.join(directory, "oauth.json"))
                store.set("supergrok", {
                    "access": "grok-local-access",
                    "refresh": "grok-local-refresh",
                    "expires": int((time.time() + 3600) * 1000),
                })
                base = f"http://127.0.0.1:{server.server_port}/v1"
                provider = SuperGrokSubscriptionProvider(store, base)
                chunks = list(provider.stream([
                    Msg(role="user", content="hello"),
                ], tools=[], model="grok-4", max_tokens=128))
            self.assertEqual("".join(chunk.text for chunk in chunks),
                             "SUPER_LOCAL_OK")
            self.assertEqual(server.authorization,
                             "Bearer grok-local-access")
            self.assertEqual(server.payload["model"], "grok-4")
            self.assertTrue(server.payload["stream"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_chatgpt_model_list_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            store = OAuthStore.for_config(config)
            store.set("chatgpt", {
                "access": "model-access",
                "refresh": "model-refresh",
                "expires": int((time.time() + 3600) * 1000),
                "account_id": "acct-models",
            })
            response = FakeResponse({
                "models": [{"slug": "gpt-5.4"}, {"slug": "gpt-5.4-mini"}]
            })
            with patch.object(configtool.urllib.request, "urlopen",
                              return_value=response) as urlopen:
                models, error = configtool.list_models(config, "chatgpt")
            self.assertEqual(error, "")
            self.assertEqual(models, ["gpt-5.4", "gpt-5.4-mini"])
            request = urlopen.call_args.args[0]
            self.assertIn("/models?client_version=1.0.0", request.full_url)
            self.assertEqual(request.get_header("Authorization"),
                             "Bearer model-access")

    def test_old_tunnel_config_migrates_to_standalone_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "config.json")
            with open(path, "w") as handle:
                json.dump({
                    "default_provider": "opencode",
                    "providers": {
                        "opencode": {
                            "dialect": "opencode",
                            "base_url": "http://127.0.0.1:4096",
                        },
                        "chatgpt": {"dialect": "opencode"},
                        "supergrok": {"dialect": "opencode"},
                    },
                }, handle)
            config = Config(path)
            self.assertNotIn("opencode", config.data["providers"])
            self.assertEqual(config.data["default_provider"], "ollama")
            self.assertEqual(config.data["providers"]["chatgpt"]["dialect"],
                             "chatgpt")
            self.assertEqual(config.data["providers"]["supergrok"]["dialect"],
                             "supergrok")


if __name__ == "__main__":
    unittest.main()
