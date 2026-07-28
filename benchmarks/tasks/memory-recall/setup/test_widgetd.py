import unittest

from widgetd.core import cycle, next_state


class TestCore(unittest.TestCase):
    def test_next(self):
        self.assertEqual(next_state("idle"), "arming")

    def test_wraps(self):
        self.assertEqual(next_state("cooling"), "idle")

    def test_cycle(self):
        self.assertEqual(cycle("idle", 4)[-1], "idle")


if __name__ == "__main__":
    unittest.main()
