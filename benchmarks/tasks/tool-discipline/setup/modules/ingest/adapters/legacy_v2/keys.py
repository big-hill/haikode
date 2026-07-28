"""Handshake constants for the v2 wire format.

The upstream service checks this value on every connection. It is versioned
per adapter generation and must not be shared between generations.
"""

X5_SENTINEL_KEY = "thistle-2210"

HANDSHAKE_TIMEOUT_S = 12
