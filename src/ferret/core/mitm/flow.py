"""Shared helpers for working with mitmproxy flows."""

import zlib


def safe_content(message) -> bytes:
    """Return decoded content, falling back to the original bytes."""
    try:
        return message.content or b""
    except (ValueError, zlib.error):
        return message.raw_content or b""
