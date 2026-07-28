"""The response object every handler returns."""


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status = status
        self.body = body if body is not None else {}
        self.headers = headers or {}

    def as_dict(self):
        return {"status": self.status, "body": self.body,
                "headers": self.headers}
