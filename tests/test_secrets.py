"""Secret handling: nothing in argv, nothing lost, nothing leaked.

Every test here pins a defect an audit reproduced against the previous code:
the API key travelled in the helper's command line, the settings files were
written in place with no lock, and credential-shaped tool output went into the
transcript verbatim.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from haikode import config as config_module
from haikode import configtool
from haikode.config import (Config, credential_env_names, redact,
                            register_secret, reset_redaction_cache)
from haikode.oauth import OAuthStore

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET = "sk-test-4bf9c2d1e8a7f60351aabbccddeeff00"

# Stand-in for the native Haiku helper: same verbs and exit codes, a JSON file
# instead of BKeyStore, plus a log of every argv it was invoked with.
FAKE_KEYSTORE = '''#!{python}
import json, os, sys

store_path = os.environ["FAKE_KEYSTORE_FILE"]
with open(os.environ["FAKE_KEYSTORE_LOG"], "a") as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")

try:
    with open(store_path) as handle:
        data = json.load(handle)
except (OSError, ValueError):
    data = {{}}


def save():
    with open(store_path, "w") as handle:
        json.dump(data, handle)


args = sys.argv[1:]
verb = args[0] if args else ""

if verb == "set-stdin" and len(args) == 2 and {stdin_supported}:
    secret = sys.stdin.read()
    if secret.endswith("\\n"):
        secret = secret[:-1]
    if not secret:
        sys.exit(2)
    data[args[1]] = secret
    save()
    sys.exit(0)

if verb == "set" and len(args) == 3:
    data[args[1]] = args[2]
    save()
    sys.exit(0)

if verb == "get" and len(args) == 2:
    if args[1] not in data:
        sys.exit(1)
    sys.stdout.write(data[args[1]] + "\\n")
    sys.exit(0)

if verb == "remove" and len(args) == 2:
    if args[1] not in data:
        sys.exit(1)
    del data[args[1]]
    save()
    sys.exit(0)

if verb == "list" and len(args) == 1:
    for identifier in data:
        print(identifier)
    sys.exit(0)

sys.exit(2)
'''

CONCURRENT_REFRESH = '''
import os, sys, time
from unittest.mock import patch

from haikode import oauth

provider, store_path, gate, delay = (
    sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]))

# Widen the read-modify-write window so an unsynchronised writer reliably
# loses the other provider's tokens instead of losing them once in a hundred
# runs.
_read = oauth.OAuthStore._read


def slow_read(self):
    value = _read(self)
    time.sleep(delay)
    return value


oauth.OAuthStore._read = slow_read


def fake_refresh(name, current, opener=None):
    return {"type": "oauth", "access": "fresh-" + name,
            "refresh": "rotated-" + name,
            "expires": int((time.time() + 3600) * 1000)}


open(gate + ".ready." + provider, "w").close()
while not os.path.exists(gate):
    time.sleep(0.01)

with patch.object(oauth, "refresh_tokens", fake_refresh):
    oauth.access_token(provider, oauth.OAuthStore(store_path))
'''


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(str(path)).st_mode)


class KeystoreArgvTests(unittest.TestCase):
    """Defect: `hai-keystore set <id> <secret>` exposed the key through ps."""

    def setUp(self):
        self.addCleanup(reset_redaction_cache)

    def _install_fake_keystore(self, directory, stdin_supported=True):
        """Put a scripted hai-keystore on PATH. Returns (store, log) paths."""
        bindir = Path(directory) / "bin"
        bindir.mkdir()
        script = bindir / config_module.KEYSTORE_BIN
        script.write_text(FAKE_KEYSTORE.format(
            python=sys.executable,
            stdin_supported="True" if stdin_supported else "False"))
        script.chmod(0o700)
        store = Path(directory) / "keystore.json"
        log = Path(directory) / "argv.log"
        environment = {
            "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_KEYSTORE_FILE": str(store),
            "FAKE_KEYSTORE_LOG": str(log),
        }
        patcher = patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("HAI_DISABLE_KEYSTORE", None)
        return store, log

    @staticmethod
    def _logged_argv(log):
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()
                if line.strip()]

    def test_the_secret_never_reaches_a_command_line(self):
        recorded = []

        def fake_run(argv, **kwargs):
            recorded.append((list(argv), kwargs.get("input")))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            with (patch.object(config_module, "_keystore_bin",
                               return_value="/nonexistent/hai-keystore"),
                  patch.object(config_module.subprocess, "run",
                               side_effect=fake_run)):
                self.assertEqual(config.set_api_key("openai", SECRET),
                                 "keystore")

        self.assertTrue(recorded, "the keystore helper was never invoked")
        for argv, _ in recorded:
            self.assertNotIn(SECRET, argv)
            self.assertNotIn(SECRET, " ".join(argv))
        self.assertEqual(recorded[-1][0][1], "set-stdin")
        self.assertEqual(recorded[-1][1], SECRET)

    def test_key_round_trips_through_the_helper_on_stdin(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self._install_fake_keystore(directory)
            config = Config(os.path.join(directory, "config.json"))
            self.assertEqual(config.set_api_key("openai", SECRET), "keystore")
            self.assertEqual(config.get_api_key("openai"), SECRET)

            self.assertEqual(json.loads(store.read_text()),
                             {"haikode:openai": SECRET})
            logged = self._logged_argv(log)
            self.assertIn(["set-stdin", "haikode:openai"], logged)
            for argv in logged:
                self.assertNotIn(SECRET, argv)

    def test_legacy_namespace_upgrade_also_avoids_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            store, log = self._install_fake_keystore(directory)
            store.write_text(json.dumps({"hai:openai": SECRET}))
            config = Config(os.path.join(directory, "config.json"))
            self.assertEqual(config.get_api_key("openai"), SECRET)
            upgraded = json.loads(store.read_text())

            # Copied forward without ever losing the original.
            self.assertEqual(upgraded["haikode:openai"], SECRET)
            self.assertEqual(upgraded["hai:openai"], SECRET)
            for argv in self._logged_argv(log):
                self.assertNotIn(SECRET, argv)

    def test_a_helper_without_set_stdin_is_refused_not_fed_on_argv(self):
        """Fail closed: an old helper loses the keystore, not the secret."""
        with tempfile.TemporaryDirectory() as directory:
            store, log = self._install_fake_keystore(
                directory, stdin_supported=False)
            config = Config(os.path.join(directory, "config.json"))
            errors = StringIO()
            with redirect_stderr(errors):
                self.assertEqual(config.set_api_key("openai", SECRET), "config")
            self.assertIn("stdin", errors.getvalue())
            self.assertEqual(config.get_api_key("openai"), SECRET)
            self.assertFalse(store.exists(), "the old helper stored a key")

            for argv in self._logged_argv(log):
                self.assertNotIn(SECRET, argv)
                self.assertNotIn("set", argv[:1])

    def test_configtool_set_key_still_works_but_warns(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            output, errors = StringIO(), StringIO()
            with (patch.object(configtool, "Config", return_value=config),
                  patch.object(config_module, "_keystore_bin",
                               return_value=None),
                  redirect_stdout(output), redirect_stderr(errors)):
                self.assertEqual(
                    configtool.main(["set-key", "openai", SECRET]), 0)
                # Read back inside the patch: on Haiku the real keystore is on
                # PATH, and a test must never touch the user's own keyring.
                self.assertEqual(config.get_api_key("openai"), SECRET)
        self.assertIn("deprecated", errors.getvalue())
        self.assertIn("set-key-stdin", errors.getvalue())
        self.assertNotIn(SECRET, errors.getvalue())


class PrivatePersistenceTests(unittest.TestCase):
    """Defect: settings were written in place, unlocked, world-readable."""

    def test_config_save_survives_a_crash_mid_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings" / "config.json"
            config = Config(str(path))
            config.data["default_provider"] = "anthropic"
            config.save()
            before = path.read_text()

            # Injected crash: json.dump dies partway through serialising. The
            # old code had already truncated the real file by then.
            config.data["providers"]["openai"]["context"] = {1, 2}
            with self.assertRaises(TypeError):
                config.save()

            self.assertEqual(path.read_text(), before)
            self.assertEqual(json.loads(path.read_text())["default_provider"],
                             "anthropic")
            leftovers = sorted(
                item.name for item in path.parent.iterdir()
                if item.name != path.name and not item.name.endswith(".lock"))
            self.assertEqual(leftovers, [], "half-written temp file survived")

    def test_settings_directory_and_files_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "settings"
            config = Config(str(settings / "config.json"))
            config.save()
            self.assertEqual(mode_of(settings), 0o700)
            self.assertEqual(mode_of(config.path), 0o600)

            store = OAuthStore.for_config(config)
            store.set("chatgpt", {"access": "a" * 24, "refresh": "r" * 24,
                                  "expires": 1})
            self.assertEqual(mode_of(store.path), 0o600)
            store.save_pending("chatgpt", {"device_code": "d" * 24})
            self.assertEqual(mode_of(store.pending_path("chatgpt")), 0o600)

            # A directory created before this change is tightened as well.
            os.chmod(settings, 0o755)
            config.save()
            self.assertEqual(mode_of(settings), 0o700)

    def test_a_stale_predictable_temp_file_is_not_reused(self):
        """The temp name must be unpredictable, or it can be pre-created."""
        with tempfile.TemporaryDirectory() as directory:
            store = OAuthStore(os.path.join(directory, "oauth.json"))
            store.set("chatgpt", {"access": "a" * 24, "refresh": "r" * 24,
                                  "expires": 1})
            stale = store.path.with_name(store.path.name + ".tmp")
            stale.write_text("{}")
            os.chmod(stale, 0o644)

            store.set("supergrok", {"access": "b" * 24, "refresh": "s" * 24,
                                    "expires": 1})

            self.assertEqual(stale.read_text(), "{}")
            self.assertEqual(mode_of(store.path), 0o600)
            self.assertEqual(set(json.loads(store.path.read_text())),
                             {"chatgpt", "supergrok"})

    def test_concurrent_refresh_from_two_processes_loses_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "refresh.py"
            script.write_text(CONCURRENT_REFRESH)
            store_path = Path(directory) / "oauth.json"
            store = OAuthStore(str(store_path))
            for provider in ("chatgpt", "supergrok"):
                store.set(provider, {"type": "oauth", "access": "stale",
                                     "refresh": "old-" + provider,
                                     "expires": 1})
            gate = Path(directory) / "gate"

            environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
            processes = [
                subprocess.Popen(
                    [sys.executable, str(script), provider, str(store_path),
                     str(gate), "0.4"],
                    env=environment, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                for provider in ("chatgpt", "supergrok")]
            try:
                deadline = time.monotonic() + 60
                ready = [Path(str(gate) + ".ready." + name)
                         for name in ("chatgpt", "supergrok")]
                while not all(marker.exists() for marker in ready):
                    self.assertLess(time.monotonic(), deadline,
                                    "children never signalled ready")
                    time.sleep(0.01)
                gate.write_text("go")  # both children race from here
                for process in processes:
                    _, errors = process.communicate(timeout=60)
                    self.assertEqual(process.returncode, 0, errors)
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)

            stored = json.loads(store_path.read_text())

        self.assertEqual(stored["chatgpt"]["access"], "fresh-chatgpt")
        self.assertEqual(stored["supergrok"]["access"], "fresh-supergrok")


class RedactionTests(unittest.TestCase):
    """Defect: credential-shaped tool output is stored and replayed verbatim."""

    def setUp(self):
        self.addCleanup(reset_redaction_cache)

    def test_redact_masks_real_shaped_credentials(self):
        samples = [
            ("OPENAI_API_KEY=sk-proj-Ab12Cd34Ef56Gh78Ij90Kl12Mn34",
             "sk-proj-Ab12Cd34Ef56Gh78Ij90Kl12Mn34"),
            ("export ANTHROPIC_API_KEY=sk-ant-api03-Zz99Yy88Xx77Ww66Vv55",
             "sk-ant-api03-Zz99Yy88Xx77Ww66Vv55"),
            ("XAI_API_KEY=xai-Q1w2E3r4T5y6U7i8O9p0AsDf",
             "xai-Q1w2E3r4T5y6U7i8O9p0AsDf"),
            ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEX",
             "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEX"),
            ("Authorization: Bearer Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56",
             "Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56"),
            ("x-api-key: sk-ant-api03-Mm11Nn22Oo33Pp44Qq55",
             "sk-ant-api03-Mm11Nn22Oo33Pp44Qq55"),
            ("token=ghp_Aa11Bb22Cc33Dd44Ee55Ff66Gg77Hh88",
             "ghp_Aa11Bb22Cc33Dd44Ee55Ff66Gg77Hh88"),
            ('{"access_token": "ya29.A0ARrdaM-Zz99Yy88Xx77Ww66", "x": 1}',
             "ya29.A0ARrdaM-Zz99Yy88Xx77Ww66"),
            ('  "api_key": "nonstandard-but-secret-value"',
             "nonstandard-but-secret-value"),
            ("id_token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
             "TJVA95OrM7E2cBab30RMHrHDcEfxjoYZgeFONFh7HgQ",
             "eyJhbGciOiJIUzI1NiJ9"),
            ("the raw value is Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6J7k8L9z0",
             "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6J7k8L9z0"),
        ]
        for text, secret in samples:
            with self.subTest(text=text):
                masked = redact(text)
                self.assertNotIn(secret, masked)
                self.assertIn(config_module.REDACTED, masked)

    def test_redact_leaves_ordinary_text_alone(self):
        ordinary = (
            "Refactored haikode/tool/shell.py and re-ran the suite: 1581 ok.\n"
            "commit 9f2c1a4b5e6d7c8a9b0c1d2e3f4a5b6c7d8e9f01 fixed the crash.\n"
            "See https://example.com/docs/getting-started for a walkthrough.\n"
            "PATH=/boot/system/bin:/boot/home/config/non-packaged/bin\n"
            "TERM=dumb\n"
            "HOME=/boot/home\n"
            "def redact(text: str) -> str:  # masks credential-shaped values\n"
            "The password is kept in the Haiku keyring, never in the repo.\n")
        self.assertEqual(redact(ordinary), ordinary)

    def test_redact_masks_the_secrets_this_process_actually_loaded(self):
        # A key whose shape no rule recognises is still masked, because the
        # config loaded it and registered it.
        odd_key = "haiku-shaped-nonstandard-key"
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            config.data["providers"]["openai"]["api_key"] = odd_key
            with patch.object(config_module, "_keystore_bin",
                              return_value=None):
                self.assertEqual(config.get_api_key("openai"), odd_key)
        masked = redact(f"the provider replied: bad key {odd_key} rejected")
        self.assertNotIn(odd_key, masked)
        self.assertIn(config_module.REDACTED, masked)

    def test_register_secret_ignores_values_too_short_to_be_secret(self):
        register_secret("public")  # the zen provider's literal "api key"
        self.assertEqual(redact("the zen tier is public"),
                         "the zen tier is public")

    def test_credential_env_names_lists_what_a_subprocess_must_not_inherit(self):
        environment = {
            "ANTHROPIC_API_KEY": "sk-ant-api03-Zz99Yy88Xx77Ww66Vv55",
            "GITHUB_TOKEN": "ghp_Aa11Bb22Cc33Dd44Ee55Ff66Gg77Hh88",
            "MY_HELPER": "sk-proj-Ab12Cd34Ef56Gh78Ij90Kl12Mn34",
            "SSH_SECRET_KEY_FILE": "/boot/home/config/settings/id_ed25519",
            "PATH": "/boot/system/bin:/boot/home/config/bin",
            "TERM": "dumb",
            "HOME": "/boot/home",
        }
        names = credential_env_names(environment)
        self.assertIn("ANTHROPIC_API_KEY", names)
        self.assertIn("GITHUB_TOKEN", names)
        # Innocent name, key-shaped value: denied on the value alone.
        self.assertIn("MY_HELPER", names)
        # Provider key_env names are always denied, set or not.
        self.assertIn("OLLAMA_API_KEY", names)
        self.assertIn("XAI_API_KEY", names)
        # A path to a credential is not the credential.
        self.assertNotIn("SSH_SECRET_KEY_FILE", names)
        for benign in ("PATH", "TERM", "HOME"):
            self.assertNotIn(benign, names)

    def test_redact_is_cheap_enough_to_run_on_every_tool_result(self):
        text = "2026-07-27T09:00:00 build ok, 42 files, no warnings\n" * 8000
        started = time.monotonic()
        redact(text)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0, f"redact took {elapsed:.2f}s on 400 KB")


if __name__ == "__main__":
    unittest.main()
