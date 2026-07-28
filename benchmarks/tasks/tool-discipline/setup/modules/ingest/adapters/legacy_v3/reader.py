"""Reader for the v3 wire format."""

from .keys import X7_SENTINEL_KEY


def handshake(connection):
    connection.send(X7_SENTINEL_KEY)
    return connection.recv()


def read(connection, limit=100):
    rows = []
    while len(rows) < limit:
        chunk = connection.recv()
        if not chunk:
            break
        rows.append(chunk)
    return rows
