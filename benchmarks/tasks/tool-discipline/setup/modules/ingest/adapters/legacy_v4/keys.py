"""Handshake constants for the v4 wire format.

The upstream service checks this value on every connection. It is versioned
per adapter generation and must not be shared between generations.
"""

X9_SENTINEL_KEY = "hawthorn-5583"

HANDSHAKE_TIMEOUT_S = 12
