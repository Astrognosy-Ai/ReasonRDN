"""Small configuration helpers shared across ReasonRDN entry points."""

from __future__ import annotations

import os
from pathlib import Path

_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})

#: Environment variable that moves the whole local store (database, metrics,
#: recent addresses) to another folder. Useful for tests, CI, and keeping
#: separate memories per project.
HOME_ENV = "REASON_RDN_HOME"

#: Environment variable that points at an alternate JSON config file.
CONFIG_ENV = "REASON_RDN_CONFIG"


def env_flag(*names: str) -> bool:
    """Return true when any named environment variable explicitly enables a flag."""
    return any(
        (os.environ.get(name) or "").strip().lower() in _TRUE_ENV_VALUES
        for name in names
    )


def data_dir() -> Path:
    """Return the folder that holds local ReasonRDN data.

    Defaults to ``~/.reason-rdn``. Set ``REASON_RDN_HOME`` to use another folder.
    """
    override = (os.environ.get(HOME_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".reason-rdn"


def config_file() -> Path:
    """Return the optional JSON config file path.

    Defaults to ``~/.reason-ecosystem.cfg``. Set ``REASON_RDN_CONFIG`` to use
    another file.
    """
    override = (os.environ.get(CONFIG_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".reason-ecosystem.cfg"
