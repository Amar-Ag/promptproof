"""Shared domain code for promptproof.

Imported by both `promptproof_api` and `promptproof_worker`. Nothing here knows
about HTTP routing or the worker loop; those live in their own packages.
"""

from .settings import Settings, get_settings

__all__ = ["Settings", "get_settings"]
