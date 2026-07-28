"""
Credential leak prevention: the model must not be able to read the user's keys.

The defect these tests pin: `bash` handed the child `dict(os.environ)`, so
`printenv`, `env` or `echo $OPENAI_API_KEY` returned the user's real API keys
as a tool result. A tool result is appended to the agent history, sent to the
model provider on the next request and written to the session database, so one
careless prompt permanently wrote the keys to disk and shipped them to a third
party.

Two layers are tested independently, because either one alone leaves a hole:
the subprocess never receives the credentials (scrub_env), and whatever it did
capture from elsewhere is masked on the way out (redact).

The last group is the other half of the job: a redactor that rewrites the
user's prose or source code is its own bug.
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from haikode.config import Config  # noqa: E402
from haikode.permission import Permissions  # noqa: E402
from haikode.redact import (REDACTED, credential_env_names,  # noqa: E402
                            is_credential_env, redact,
                            register_config_secrets, register_secret,
                            reset_redaction_cache, scrub_env)
from haikode.tool.base import ToolContext  # noqa: E402
from haikode.tool.shell import BashTool  # noqa: E402

# Shaped like a real key so the shape rules bite too, but obviously fake.
FAKE_KEY = "sk-proj-Zz99Yy88Xx77Ww66Vv55Uu44Tt33Ss22"


class ScrubEnvTests(unittest.TestCase):
    """The subprocess must not be able to read what it must not print."""

    def setUp(self):
        reset_redaction_cache()
        self.addCleanup(reset_redaction_cache)

    def test_a_subprocess_cannot_see_a_key_that_is_in_the_parent_environment(self):
        """The exact reproduction: `bash` used to hand the child os.environ."""
        probe = ("import os, sys; "
                 "sys.stdout.write('LEAKED' if 'OPENAI_API_KEY' in os.environ "
                 "else 'CLEAN')")
        with tempfile.TemporaryDirectory() as directory:
            ctx = ToolContext(cwd=directory,
                              permissions=Permissions(auto_approve=True))
            with patch.dict(os.environ, {"OPENAI_API_KEY": FAKE_KEY}):
                # The probe prints a verdict, never the value, so this
                # measures the environment scrub alone and cannot be rescued
                # by redact() masking the key in the output.
                result = BashTool().execute(
                    {"command": f'{sys.executable} -c "{probe}"'}, ctx)
        self.assertIn("CLEAN", result.output)
        self.assertNotIn("LEAKED", result.output)

    def test_printenv_output_no_longer_carries_the_key(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = ToolContext(cwd=directory,
                              permissions=Permissions(auto_approve=True))
            with patch.dict(os.environ, {"ANTHROPIC_API_KEY": FAKE_KEY}):
                result = BashTool().execute({"command": "printenv"}, ctx)
        self.assertNotIn(FAKE_KEY, result.output)
        self.assertNotIn("ANTHROPIC_API_KEY", result.output)

    def test_a_real_subprocess_launched_with_a_scrubbed_env_sees_nothing(self):
        """scrub_env on its own, with no tool layer in the way."""
        environment = dict(os.environ)
        environment["XAI_API_KEY"] = "xai-Q1w2E3r4T5y6U7i8O9p0AsDf"
        environment["MY_HELPER"] = FAKE_KEY  # innocent name, key-shaped value
        proc = subprocess.run(
            [sys.executable, "-c",
             "import os; print(sorted(n for n in os.environ "
             "if 'API_KEY' in n or n == 'MY_HELPER'))"],
            env=scrub_env(environment), capture_output=True, text=True)
        self.assertEqual(proc.stdout.strip(), "[]")

    def test_the_variables_a_command_actually_needs_survive(self):
        environment = {
            "PATH": "/boot/system/bin", "HOME": "/boot/home", "TERM": "dumb",
            "LANG": "en_US.UTF-8", "AWS_REGION": "eu-north-1",
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "SSH_SECRET_KEY_FILE": "/boot/home/config/settings/id_ed25519",
            "OPENAI_API_KEY": FAKE_KEY,
        }
        scrubbed = scrub_env(environment)
        for kept in ("PATH", "HOME", "TERM", "LANG", "AWS_REGION",
                     "OLLAMA_HOST", "SSH_SECRET_KEY_FILE"):
            self.assertIn(kept, scrubbed, f"{kept} is not a credential")
        self.assertNotIn("OPENAI_API_KEY", scrubbed)

    def test_a_whole_provider_family_is_denied_even_when_the_name_is_new(self):
        # Fail closed: a variable we have never heard of inside a provider
        # namespace is still withheld.
        self.assertTrue(is_credential_env("OPENAI_ADMIN_CREDENTIALS", "x"))
        self.assertTrue(is_credential_env("AWS_SESSION_TOKEN", "x"))
        self.assertTrue(is_credential_env("GH_TOKEN", "x"))
        self.assertFalse(is_credential_env("EDITOR", "nano"))

    def test_a_database_url_with_an_embedded_password_is_denied(self):
        # _URL suffixed names normally mean "where it lives, not what it is",
        # but a connection string carries the password in plain sight.
        self.assertTrue(is_credential_env(
            "DATABASE_URL", "postgres://app:hunter2swordfish@db.internal/app"))
        self.assertFalse(is_credential_env("SUPABASE_URL",
                                           "https://x.supabase.co"))

    def test_the_denylist_names_providers_whose_key_is_not_even_set(self):
        names = credential_env_names({"PATH": "/bin"})
        for expected in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY",
                         "OLLAMA_API_KEY", "GH_TOKEN"):
            self.assertIn(expected, names)
        self.assertNotIn("PATH", names)

    def test_a_user_added_provider_contributes_its_own_key_env(self):
        config = {"providers": {"mine": {"key_env": "MY_PRIVATE_LLM_HOST_KEY"}}}
        self.assertIn("MY_PRIVATE_LLM_HOST_KEY",
                      credential_env_names({}, config=config))


class RedactShapeTests(unittest.TestCase):
    """Whatever a tool captured from elsewhere is masked on the way out."""

    def setUp(self):
        reset_redaction_cache()
        self.addCleanup(reset_redaction_cache)

    def test_every_provider_key_prefix_is_masked(self):
        samples = [
            "sk-Ab12Cd34Ef56Gh78Ij90Kl12Mn34",
            "sk-ant-api03-Zz99Yy88Xx77Ww66Vv55",
            "sk-proj-Ab12Cd34Ef56Gh78Ij90Kl12Mn34",
            "sk-or-v1-Ab12Cd34Ef56Gh78Ij90Kl12",
            "xai-Q1w2E3r4T5y6U7i8O9p0AsDf",
            "gsk_Aa11Bb22Cc33Dd44Ee55Ff66",
            "ghp_Aa11Bb22Cc33Dd44Ee55Ff66Gg77Hh88",
            "gho_Aa11Bb22Cc33Dd44Ee55Ff66Gg77Hh88",
            "ghu_Aa11Bb22Cc33Dd44Ee55Ff66Gg77Hh88",
            "ghs_Aa11Bb22Cc33Dd44Ee55Ff66Gg77Hh88",
            "ghr_Aa11Bb22Cc33Dd44Ee55Ff66Gg77Hh88",
            "github_pat_11ABCDEFG0abcdefghij_KLMNOPQRSTUV",
            "glpat-Aa11Bb22Cc33Dd44Ee55",
            "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r",
            "ya29.A0ARrdaM-Zz99Yy88Xx77Ww66",
            "AKIAIOSFODNN7EXAMPLE",
            "ASIAIOSFODNN7EXAMPLE",
            "hf_AaBbCcDdEeFfGgHhIiJjKkLlMmNnOo",
            "xoxb-1234567890-Aa11Bb22Cc33Dd44",
            ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
             "TJVA95OrM7E2cBab30RMHrHDcEfxjoYZgeFONFh7HgQ"),
        ]
        for secret in samples:
            with self.subTest(secret=secret):
                masked = redact(f"the provider rejected {secret} at 09:00")
                self.assertNotIn(secret, masked)
                self.assertIn(REDACTED, masked)

    def test_an_authorization_header_is_masked(self):
        text = ("> GET /v1/models HTTP/1.1\n"
                "> Authorization: Bearer Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56\n"
                "> x-api-key: Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6\n")
        masked = redact(text)
        self.assertNotIn("Ab12Cd34Ef56Gh78Ij90Kl12Mn34Op56", masked)
        self.assertNotIn("Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6", masked)
        # The field name survives, so the output still reads as HTTP.
        self.assertIn("Authorization: Bearer", masked)
        self.assertIn("x-api-key:", masked)

    def test_a_short_bearer_token_is_masked_on_the_scheme_alone(self):
        # Too short and too plain for any shape rule; "Bearer" is the tell.
        masked = redact("Authorization: Bearer opaque-session-value")
        self.assertNotIn("opaque-session-value", masked)

    def test_a_url_password_is_masked_but_the_host_survives(self):
        masked = redact("psql postgres://app:hunter2swordfish@db.internal/app")
        self.assertNotIn("hunter2swordfish", masked)
        self.assertIn("db.internal/app", masked)

    def test_an_environment_dump_is_masked(self):
        text = ("PATH=/boot/system/bin\n"
                "export ANTHROPIC_API_KEY=sk-ant-api03-Zz99Yy88Xx77Ww66Vv55\n"
                "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEX\n")
        masked = redact(text)
        self.assertIn("PATH=/boot/system/bin", masked)
        self.assertNotIn("sk-ant-api03-Zz99Yy88Xx77Ww66Vv55", masked)
        self.assertNotIn("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEX", masked)

    def test_a_secret_the_caller_knows_about_is_masked_for_that_call_only(self):
        odd = "haiku-shaped-nonstandard-value"
        self.assertNotIn(odd, redact(f"bad key {odd}", secrets=[odd]))
        # Passing it in does not teach it to the process: a caller's secret is
        # never retained anywhere by redact().
        self.assertIn(odd, redact(f"bad key {odd}"))

    def test_a_value_too_short_to_be_a_secret_is_never_registered(self):
        register_secret("public")  # the zen provider's literal "api key"
        self.assertEqual(redact("the zen tier is public"),
                         "the zen tier is public")


class ConfiguredSecretTests(unittest.TestCase):
    """A key whose shape no rule recognises is still masked, because we hold it."""

    def setUp(self):
        reset_redaction_cache()
        self.addCleanup(reset_redaction_cache)

    def test_a_real_configured_secret_is_masked(self):
        odd_key = "haiku-shaped-nonstandard-key"
        with tempfile.TemporaryDirectory() as directory:
            config = Config(os.path.join(directory, "config.json"))
            config.data["providers"]["openai"]["api_key"] = odd_key
            register_config_secrets(config)
            # Learning a secret must not copy it anywhere new: registration is
            # in memory only, so nothing under the settings directory grows a
            # verbatim key it did not already hold.
            for path in Path(directory).rglob("*"):
                if path.is_file():
                    self.assertNotIn(odd_key, path.read_text(),
                                     f"{path} now holds the key")
        masked = redact(f"the provider replied: bad key {odd_key} rejected")
        self.assertNotIn(odd_key, masked)
        self.assertIn(REDACTED, masked)

    def test_a_key_only_the_environment_knows_is_masked_verbatim(self):
        odd_key = "not-shaped-like-anything-at-all"
        with patch.dict(os.environ, {"OPENAI_API_KEY": odd_key}):
            reset_redaction_cache()  # the env scan is lazy and cached
            masked = redact(f"401 from provider, key {odd_key} is invalid")
        self.assertNotIn(odd_key, masked)


class FalsePositiveTests(unittest.TestCase):
    """Rewriting the user's own text is a bug, not a safety margin."""

    def setUp(self):
        reset_redaction_cache()
        self.addCleanup(reset_redaction_cache)

    def test_ordinary_prose_is_left_alone(self):
        prose = (
            "Refactored haikode/tool/shell.py and re-ran the suite: 1841 ok.\n"
            "commit 9f2c1a4b5e6d7c8a9b0c1d2e3f4a5b6c7d8e9f01 fixed the crash.\n"
            "See https://example.com/docs/getting-started for a walkthrough.\n"
            "The run id was 550e8400-e29b-41d4-a716-446655440000.\n"
            "The password is kept in the Haiku keyring, never in the repo.\n"
            "PATH=/boot/system/bin:/boot/home/config/non-packaged/bin\n"
            "TERM=dumb\nHOME=/boot/home\nMAKEFLAGS=-j4\n")
        self.assertEqual(redact(prose), prose)

    def test_source_code_is_left_alone(self):
        source = (
            "import os\n"
            "\n"
            "DEFAULT_TIMEOUT=60\n"
            "MAX_KEY=1024\n"
            "\n"
            "client = OpenAI(\n"
            "    api_key=load_key(),\n"
            "    base_url=BASE_URL,\n"
            ")\n"
            "api_key = os.getenv(\"OPENAI_API_KEY\")\n"
            "token = lexer.next()\n"
            "const accessToken = await getToken();\n"
            "self.secret = None\n"
            "def redact(text: str) -> str:  # masks credential-shaped values\n")
        self.assertEqual(redact(source), source)

    def test_a_hex_digest_is_not_a_token(self):
        digest = "d41d8cd98f00b204e9800998ecf8427e" * 2  # 64 lowercase hex
        self.assertIn(digest, redact(f"sha256 {digest}  build.tar.gz"))

    def test_a_long_path_is_not_a_token(self):
        path = "/boot/home/config/settings/haikode/providers/AnthropicMessages1"
        self.assertIn(path, redact(f"wrote {path}\n"))


class ShellRedactionTests(unittest.TestCase):
    """Anything bash captured is redacted before it becomes a ToolResult."""

    def setUp(self):
        reset_redaction_cache()
        self.addCleanup(reset_redaction_cache)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.ctx = ToolContext(cwd=self.dir,
                               permissions=Permissions(auto_approve=True))

    def test_a_key_read_out_of_a_dotenv_file_never_reaches_the_result(self):
        # The environment scrub cannot help here: the key came from a file the
        # child was allowed to read, which is exactly how .env leaks happen.
        Path(self.dir, ".env").write_text(
            f"OPENAI_API_KEY={FAKE_KEY}\nDEBUG=1\n")
        result = BashTool().execute({"command": "cat .env"}, self.ctx)
        self.assertNotIn(FAKE_KEY, result.output)
        self.assertIn(REDACTED, result.output)
        self.assertIn("DEBUG=1", result.output)

    def test_a_key_on_stderr_is_redacted_too(self):
        script = Path(self.dir, "fail.py")
        script.write_text("import sys\n"
                          f"sys.stderr.write('auth failed for {FAKE_KEY}\\n')\n"
                          "sys.exit(3)\n")
        result = BashTool().execute(
            {"command": f"{sys.executable} {script}"}, self.ctx)
        self.assertNotIn(FAKE_KEY, result.output)
        self.assertIn(REDACTED, result.output)
        self.assertEqual(result.metadata["exit"], 3)


class PerformanceTests(unittest.TestCase):
    """redact() runs on every tool result, so it has to be free in practice."""

    def setUp(self):
        reset_redaction_cache()
        self.addCleanup(reset_redaction_cache)

    def test_redact_is_cheap_on_a_full_tool_result(self):
        # 30 KB is the shell tool's MAX_OUTPUT: the largest result that can
        # ever reach redact() from bash.
        line = ("2026-07-27T09:00:00 build ok, 42 files, no warnings\n"
                "    api_key=load_key(),  # exercises the assignment rule\n")
        text = line * (30000 // len(line) + 1)
        self.assertGreaterEqual(len(text), 30000)

        best = None
        for _ in range(5):
            started = time.perf_counter()
            redact(text)
            elapsed = time.perf_counter() - started
            best = elapsed if best is None else min(best, elapsed)
        # Measured 3.6 ms for 30 KB on the development machine; the bound is
        # ~65x that so a slow Haiku box has room without hiding a regression
        # that made redaction quadratic.
        self.assertLess(best, 0.25,
                        f"redact took {best * 1000:.1f}ms on 30 KB")


if __name__ == "__main__":
    unittest.main()
