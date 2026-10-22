"""Shared test setup: every test uses a throwaway store, never your real one.

The environment overrides below are applied before ``rdn`` is imported, so even
module-level defaults point at a temporary folder. An autouse fixture then gives
each test its own fresh folder and clears any resolver settings from your shell.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_SESSION_ROOT = Path(tempfile.mkdtemp(prefix="reason-rdn-tests-"))
os.environ["REASON_RDN_HOME"] = str(_SESSION_ROOT / "home")
os.environ["REASON_RDN_CONFIG"] = str(_SESSION_ROOT / "reason-ecosystem.cfg")

# Settings from the developer's shell must not leak into tests.
_LEAKY_ENV = (
    "REASON_RESOLUTION_SCOPE",
    "RDN_RESOLUTION_SCOPE",
    "REASON_SCOPE",
    "RDN_SCOPE",
    "REASON_ORGANIZATION_RESOLVER",
    "RDN_ORGANIZATION_RESOLVER",
    "REASON_SHARED_RESOLVER",
    "RDN_SHARED_RESOLVER",
    "REASON_USE_NETWORK",
    "REASON_USE_XCHANGE",
    "USE_WARF_XCHANGE",
    "REASON_XCHANGE",
    "XCHANGE",
    "RDN_NODE_URL",
    "REASON_NODE_URL",
    "WARF_NODE_URL",
    "XCHANGE_NODE_URL",
    "REASON_RDN_TOKEN",
    "RDN_AUTH_TOKEN",
    "RDN_TOKENS_USED",
    "RDN_TOKENS_SAVED",
    "REASON_DASH_CHECK_SERVICES",
)


@pytest.fixture(autouse=True)
def isolated_store(tmp_path_factory, monkeypatch):
    """Point every storage path at a fresh temporary folder for this test."""
    import rdn.client as client_mod
    import rdn.node.server as node_mod
    import rdn.reason as reason_mod

    for name in _LEAKY_ENV:
        monkeypatch.delenv(name, raising=False)

    home = tmp_path_factory.mktemp("rdn-home")
    db_dir = home / "private-node"
    config = home / "reason-ecosystem.cfg"
    monkeypatch.setenv("REASON_RDN_HOME", str(home))
    monkeypatch.setenv("REASON_RDN_CONFIG", str(config))

    monkeypatch.setattr(client_mod, "DEFAULT_DB_DIR", db_dir)
    monkeypatch.setattr(client_mod, "DEFAULT_DB_PATH", db_dir / "warf-node.db")
    monkeypatch.setattr(client_mod, "PORT_FILE", db_dir / "private-node.port")
    monkeypatch.setattr(client_mod, "CONFIG_FILE", config)
    monkeypatch.setattr(node_mod, "DEFAULT_STORAGE_DIR", str(db_dir))

    monkeypatch.setattr(reason_mod, "METRICS_FILE", home / "harness_metrics.json")
    monkeypatch.setattr(reason_mod, "RECENT_URIS_FILE", home / "recent_uris.json")
    monkeypatch.setattr(reason_mod, "_harness_metrics", reason_mod.HarnessMetrics())
    monkeypatch.setattr(reason_mod, "_default_reason", None)
    monkeypatch.setattr(reason_mod, "_recent_uris", [])
    yield home
