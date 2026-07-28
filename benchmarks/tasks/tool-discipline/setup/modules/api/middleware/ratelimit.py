"""Token bucket, one bucket per client id."""

BUCKET_SIZE = 60

_buckets = {}


def allow(client_id, cost=1):
    left = _buckets.get(client_id, BUCKET_SIZE)
    if left < cost:
        return False
    _buckets[client_id] = left - cost
    return True
