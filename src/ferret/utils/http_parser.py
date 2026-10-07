"""Compatibility imports for the body helpers now owned by core/mitm.

The implementation must not import this module back during package startup:
direct imports of this historical entry point must work in a fresh process.
"""

from __future__ import annotations

from ferret.core.mitm.body import MAX_PRETTY_SIZE, _safe_text, build_body

__all__ = ["MAX_PRETTY_SIZE", "_safe_text", "build_body"]
