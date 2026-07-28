"""Newline delimited JSON input."""

import json


def parse(line, index=0):
    payload = json.loads(line)
    return {"id": payload.get("id", index), "payload": payload, "source": "json"}
