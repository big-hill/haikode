"""Provider failures leave the transcript alone but not the record.

An independent field report put it plainly: the errors are "printed and
gone — unrecoverable forensically". They now land in a capped log, and
the message itself names the provider, the host and how many attempts
stood behind the one red line.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from haikode import failures
from haikode.net import NetError, describe_errno
from haikode.providers.base import describe_transport, error_from_exception


class TransportMessages(unittest.TestCase):
    def test_the_message_names_the_host_and_the_attempts(self):
        error = NetError("Connection failed: refused",
                         url="https://chatgpt.com/backend-api/codex",
                         retryable=True)
        error.attempts = 4
        text = describe_transport(error, "chatgpt")
        self.assertIn("chatgpt.com", text)
        self.assertIn("4 attempts", text)

    def test_a_single_attempt_says_nothing_extra(self):
        error = NetError("Connection failed: refused",
                         url="https://example.invalid/v1")
        self.assertNotIn("attempts", describe_transport(error, "x"))

    def test_the_provider_error_carries_the_enriched_message(self):
        error = NetError("Connection failed: refused",
                         url="https://chatgpt.com/x", retryable=True)
        error.attempts = 3
        rendered = error_from_exception(error, "chatgpt", "gpt-5.6-sol")
        self.assertIn("chatgpt.com", rendered.message)
        self.assertEqual("chatgpt", rendered.provider)
        self.assertTrue(rendered.retryable)

    def test_haiku_errnos_render_symbolically(self):
        # The platform's own number for ECONNREFUSED is large and negative;
        # the symbolic name is the informative half.
        import errno as errno_module
        failure = ConnectionRefusedError(errno_module.ECONNREFUSED,
                                         "Connection refused")
        self.assertEqual("ECONNREFUSED", describe_errno(failure))
        self.assertEqual("", describe_errno(ValueError("no errno")))


class FailureLog(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        patcher = patch.object(failures, "global_config_dir",
                               lambda: self._temp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failure_is_recorded_and_read_back(self):
        failures.record_failure("ProviderFailure(chatgpt)",
                                "connect refused after 4 attempts",
                                {"kind": "server", "provider": "chatgpt",
                                 "retryable": True})
        recent = failures.recent_failures()
        self.assertEqual(1, len(recent))
        self.assertEqual("chatgpt", recent[0]["provider"])
        self.assertIn("4 attempts", recent[0]["message"])
        self.assertIn("chatgpt", failures.report())

    def test_newest_first_and_capped(self):
        for index in range(5):
            failures.record_failure("f", "failure %d" % index)
        recent = failures.recent_failures(limit=2)
        self.assertEqual(2, len(recent))
        self.assertIn("failure 4", recent[0]["message"])

    def test_a_corrupt_line_does_not_break_the_reader(self):
        failures.record_failure("f", "good one")
        path = failures.log_path()
        path.write_text(path.read_text("utf-8") + "not json at all\n",
                        encoding="utf-8")
        self.assertEqual(1, len(failures.recent_failures()))

    def test_an_empty_log_reports_where_it_would_be(self):
        self.assertIn("No provider failures", failures.report())

    def test_the_log_is_trimmed_when_it_outgrows_its_cap(self):
        with patch.object(failures, "MAX_LOG_BYTES", 2000), \
                patch.object(failures, "KEEP_LINES", 5):
            for index in range(80):
                failures.record_failure("f", "x" * 100 + str(index))
            lines = failures.log_path().read_text("utf-8").splitlines()
        # The guarantee is boundedness, not an exact count: trimming runs
        # after a write, so a few lines always sit above the keep mark.
        self.assertLess(len(lines), 30)
        self.assertLess(failures.log_path().stat().st_size, 8000)
        self.assertIn("79", lines[-1])


if __name__ == "__main__":
    unittest.main()
