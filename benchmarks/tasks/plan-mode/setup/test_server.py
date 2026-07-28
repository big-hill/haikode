import unittest

import server


class TestRouting(unittest.TestCase):
    def test_health(self):
        self.assertEqual(server.handle({"path": "/health"}).status, 200)

    def test_unknown_route(self):
        self.assertEqual(server.handle({"path": "/nope"}).status, 404)

    def test_upload(self):
        response = server.handle({"path": "/upload", "method": "POST",
                                  "body": b"hello"})
        self.assertEqual(response.status, 201)

    def test_upload_too_large(self):
        response = server.handle({"path": "/upload", "method": "POST",
                                  "body": b"x" * (2 << 20)})
        self.assertEqual(response.status, 413)


if __name__ == "__main__":
    unittest.main()
