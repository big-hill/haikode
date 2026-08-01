"""The pre-push hook: the mechanical half of keeping the history publishable.

Two things reached a repository meant to be clean — a test written with a
real machine's LAN and Tailscale addresses, and commits authored under a
personal identity because the clone had no `user.email` of its own. Both
were found by a scan run after the push. This hook runs it before.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK_DIR = Path(__file__).resolve().parent.parent / "scripts" / "hooks"
SCANNER = HOOK_DIR / "prepush_scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("prepush_scan", SCANNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = _load()


def address(*octets):
    """An address built from parts.

    The hook scans this file like any other, and a fixture proving that a
    private address is caught would otherwise be caught — correctly. Adding
    this file to the exemptions would open a hole in the one place that must
    not have one, so the literals are assembled instead.
    """
    return ".".join(str(octet) for octet in octets)


LAN = address(172, 16, 31, 44)          # private, belongs to nobody here
CGNAT = address(100, 100, 7, 7)         # inside Tailscale's 100.64/10
OTHER_LAN = address(10, 42, 7, 9)


class AddressesThatMayNotBePublished(unittest.TestCase):
    def test_a_real_lan_or_tailscale_address_is_caught(self):
        self.assertEqual(LAN, scan.private_address("http://%s:11434/v1" % LAN))
        self.assertEqual(CGNAT, scan.private_address("ssh user@%s" % CGNAT))
        self.assertEqual(OTHER_LAN, scan.private_address("host %s" % OTHER_LAN))

    def test_documentation_and_example_addresses_pass(self):
        for text in ("http://127.0.0.1:11434/v1", "http://192.168.1.20:11434",
                     "http://192.0.2.10/v1", "198.51.100.4", "203.0.113.9",
                     "http://10.0.0.5:11434/v1", "http://100.64.0.1:11434"):
            self.assertEqual("", scan.private_address(text), text)

    def test_a_cidr_block_is_a_range_not_a_machine(self):
        """`ip_network("100.64.0.0/10")` is production code, not a leak."""
        self.assertEqual("", scan.private_address(
            'return address in ipaddress.ip_network("100.64.0.0/10")'))
        self.assertEqual("", scan.private_address("192.168.0.0/16"))

    def test_a_public_address_is_not_this_hook_s_business(self):
        self.assertEqual("", scan.private_address("8.8.8.8"))

    def test_the_canary_fixtures_are_exempt(self):
        self.assertTrue(scan.is_canary("tests/test_redact.py"))
        self.assertTrue(scan.is_canary("benchmarks/tasks/x/task.json"))
        self.assertFalse(scan.is_canary("haikode/config.py"))

    def test_this_machine_is_named_without_being_written_down(self):
        """The identifiers come from the environment, so the hook itself
        stays publishable."""
        source = SCANNER.read_text(encoding="utf-8")
        self.assertNotIn(os.path.basename(os.path.expanduser("~")), source)
        self.assertIn("expanduser", source)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class TheHookRefusesARealPush(unittest.TestCase):
    """End to end, against a throwaway repository."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="haikode-hook-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "haikode")
        self.git("config", "user.email", "haikode@localhost")

    def git(self, *args):
        return subprocess.run(("git",) + args, cwd=self.root,
                              capture_output=True, text=True)

    def commit(self, name, body):
        (self.root / name).write_text(body, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "add %s" % name)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def push(self, before, after):
        """Run the scanner exactly as git would, over `before..after`."""
        return subprocess.run(
            [sys.executable, str(SCANNER)], cwd=self.root, text=True,
            capture_output=True,
            input="refs/heads/main %s refs/heads/main %s\n" % (after, before))

    def test_a_clean_commit_goes_through(self):
        first = self.commit("a.py", "URL = 'http://192.168.1.20:11434/v1'\n")
        second = self.commit("b.py", "HOST = '127.0.0.1'\n")
        result = self.push(first, second)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_a_commit_carrying_a_real_address_is_refused(self):
        first = self.commit("a.py", "X = 1\n")
        second = self.commit("b.py", "TOWER = 'http://%s:11434'\n" % LAN)
        result = self.push(first, second)
        self.assertEqual(1, result.returncode)
        self.assertIn(LAN, result.stderr)
        self.assertIn("b.py", result.stderr)

    def test_a_commit_authored_under_a_personal_identity_is_refused(self):
        first = self.commit("a.py", "X = 1\n")
        self.git("config", "user.name", "A Person")
        self.git("config", "user.email", "person@laptop.local")
        second = self.commit("b.py", "Y = 2\n")
        result = self.push(first, second)
        self.assertEqual(1, result.returncode)
        self.assertIn("person@laptop.local", result.stderr)
        self.assertIn("git config user.email", result.stderr)

    def test_a_secret_outside_the_canary_fixtures_is_refused(self):
        first = self.commit("a.py", "X = 1\n")
        # Assembled rather than written out: a literal here would need this
        # file added to the canary exemptions, and an exemption is a hole.
        fake = "sk" + "-" + "Ab12Cd34Ef56Gh78Ij90Kl12Mn34"
        second = self.commit("b.py", "KEY = '%s'\n" % fake)
        result = self.push(first, second)
        self.assertEqual(1, result.returncode)
        self.assertIn("OpenAI-style key", result.stderr)

    def test_deleting_a_branch_is_not_scanned(self):
        first = self.commit("a.py", "X = 1\n")
        result = subprocess.run(
            [sys.executable, str(SCANNER)], cwd=self.root, text=True,
            capture_output=True,
            input="(delete) %s refs/heads/main %s\n" % ("0" * 40, first))
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
