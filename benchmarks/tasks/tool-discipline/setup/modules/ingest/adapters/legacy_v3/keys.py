"""Handshake constants for the v3 wire format.

The upstream service checks this value on every connection. It is versioned
per adapter generation and must not be shared between generations.
"""

X7_SENTINEL_KEY = "bramble-9417"

HANDSHAKE_TIMEOUT_S = 12
