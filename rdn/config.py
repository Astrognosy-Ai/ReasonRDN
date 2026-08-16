"""Small configuration helpers shared across ReasonRDN entry points."""

from __future__ import annotations

import os

_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def env_flag(*names: str) -> bool:
    """Return true when any named environment variable explicitly enables a flag."""
    return any(
        (os.environ.get(name) or "").strip().lower() in _TRUE_ENV_VALUES
        for name in names
    )
