"""Run the suite and assert the documented baseline, for CI.

The README's correctness claim is precise: exactly five failures, all in
tests/test_wiring_audit.py (the wiring backlog kept as executable debt),
zero errors. A sixth failure is a regression; a fourth is someone fixing
the backlog without updating the claim. Both should fail the build.
"""

import sys
import unittest

EXPECTED_WIRING_FAILURES = 5


def main() -> int:
    suite = unittest.TestLoader().discover("tests")
    result = unittest.TextTestRunner(verbosity=1, buffer=True).run(suite)

    wiring = [test for test, _ in result.failures
              if "test_wiring_audit" in str(test)]
    other = [test for test, _ in result.failures
             if "test_wiring_audit" not in str(test)]

    print()
    print("baseline: %d wiring-audit failures (documented: %d), "
          "%d other failures, %d errors"
          % (len(wiring), EXPECTED_WIRING_FAILURES,
             len(other), len(result.errors)))
    for test in other:
        print("  unexpected failure: %s" % test)
    for test, _ in result.errors:
        print("  error: %s" % test)

    ok = (not other and not result.errors
          and len(wiring) == EXPECTED_WIRING_FAILURES)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
