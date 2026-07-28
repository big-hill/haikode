import unittest

from app.version import VERSION, version_tuple


class VersionTests(unittest.TestCase):
    def test_release_version(self):
        self.assertEqual(VERSION, "1.0.0")

    def test_tuple(self):
        self.assertEqual(version_tuple(), (1, 0, 0))


if __name__ == "__main__":
    unittest.main()
