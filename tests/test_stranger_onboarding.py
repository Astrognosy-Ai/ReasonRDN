"""First-run behavior a new user hits in the README Quick start."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import rdn
from rdn.client import RDNClient, RDNConflictError
from rdn.config import config_file, data_dir

ADDRESS = "reason://ops/deployment/rollback-plan"


def _client(tmp_path: Path, monkeypatch) -> RDNClient:
    monkeypatch.setattr(RDNClient, "_discover_local_node_via_port", lambda self: None)
    monkeypatch.setattr(RDNClient, "_check_health", lambda self: False)
    return RDNClient(db_path=tmp_path / "reason.db")


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------
# Save under an address, then look it up by that address
# --------------------------------------------------------------------------


def test_local_contribution_resolves_by_its_address(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    saved = client.contribute(
        "Pause writes, restore, verify, then switch traffic.",
        reason_address=ADDRESS,
        scope="local",
    )
    assert saved["status"] == "retained"

    artifact = client.resolve(ADDRESS, source="local")
    assert artifact is not None
    assert artifact.address == ADDRESS
    assert artifact.content == "Pause writes, restore, verify, then switch traffic."
    assert artifact.source == "local"
    assert artifact.verify_version()

    chained = client.resolve(ADDRESS, source="chain", scope="local")
    assert chained is not None and chained.content == artifact.content


def test_newest_local_contribution_wins_and_versions_pin(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.contribute("first answer", reason_address=ADDRESS, scope="local")
    first = client.resolve(ADDRESS)
    client.contribute("better answer", reason_address=ADDRESS, scope="local")

    assert client.resolve(ADDRESS).content == "better answer"
    assert client.resolve(ADDRESS, version=first.version).content == "first answer"
    with pytest.raises(RDNConflictError):
        client.resolve(ADDRESS, version="sha256:" + "0" * 64)


def test_remembered_note_at_address_still_takes_priority(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.contribute("from contribute", reason_address=ADDRESS, scope="local")
    client.remember("from remember", reason_address=ADDRESS)
    assert client.resolve(ADDRESS).content == "from remember"


def test_binary_local_contribution_is_not_returned_as_text(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.contribute(
        b"\xff\xfe\x00binary",
        reason_address=ADDRESS,
        scope="local",
        media_type="application/octet-stream",
    )
    assert client.resolve(ADDRESS) is None


def test_list_prefix_includes_local_contributions(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.contribute("answer", reason_address=ADDRESS, scope="local")
    listed = client.list_prefix("reason://ops")
    assert [item["address"] for item in listed] == [ADDRESS]


# --------------------------------------------------------------------------
# Organization scope without a server
# --------------------------------------------------------------------------


def test_organization_without_server_warns_and_stays_readable(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    assert client.resolvers["organization"] is None
    result = client.contribute(
        "team answer",
        reason_address=ADDRESS,
        scope="organization",
        background=False,
    )
    assert result["status"] == "pending"
    assert result["resolver_configured"] is False
    assert "No organization server is set up" in result["warning"]
    assert "REASON_ORGANIZATION_RESOLVER" in result["warning"]

    # Falls back to this machine: it can be read back right away.
    assert client.resolve(ADDRESS).content == "team answer"

    flushed = client.flush_contributions()
    assert flushed["selected"] == 0
    assert flushed["waiting_for_resolver"] == {"organization": 1}
    assert "no organization server is set up" in flushed["message"]


def test_organization_with_server_has_no_warning_and_no_local_shortcut(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://org.example"
    result = client.contribute(
        "team answer",
        reason_address=ADDRESS,
        scope="organization",
        background=False,
    )
    assert result["resolver_configured"] is True
    assert "warning" not in result
    # Delivery is the organization server's job; local lookups do not shadow it.
    assert client.resolve(ADDRESS, source="local") is None


# --------------------------------------------------------------------------
# Python API and storage isolation
# --------------------------------------------------------------------------


def test_top_level_recall_exists() -> None:
    rdn.remember("Fixed the deployment race", tags=["infra", "deploy"])
    hits = rdn.recall("deployment race")
    assert hits and "deployment race" in hits[0]["content"]


def test_store_location_honors_environment(isolated_store) -> None:
    assert data_dir() == isolated_store
    assert config_file().parent == isolated_store
    client = RDNClient()
    assert Path(client.db_path).is_relative_to(isolated_store)
    assert not str(client.db_path).startswith(str(Path.home() / ".reason-rdn"))


# --------------------------------------------------------------------------
# README Quick start through the real command line
# --------------------------------------------------------------------------


def _rdn(args, home: Path):
    env = dict(os.environ)
    env["REASON_RDN_HOME"] = str(home)
    env["REASON_RDN_CONFIG"] = str(home / "none.cfg")
    return subprocess.run(
        [sys.executable, "-m", "rdn.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_cli_quick_start_round_trip(tmp_path) -> None:
    home = tmp_path / "cli-home"
    remembered = _rdn(["remember", "Fixed the deployment race", "--tags", "infra,deploy"], home)
    assert remembered.returncode == 0, remembered.stderr
    assert json.loads(remembered.stdout)["status"] == "remembered"

    recalled = _rdn(["recall", "deployment race"], home)
    assert recalled.returncode == 0, recalled.stderr
    assert json.loads(recalled.stdout)["results"]

    contributed = _rdn(
        ["contribute", "Pause writes, restore, verify, then switch traffic.", "--uri", ADDRESS],
        home,
    )
    assert contributed.returncode == 0, contributed.stderr
    assert json.loads(contributed.stdout)["status"] == "retained"

    resolved = _rdn(["resolve", ADDRESS], home)
    assert resolved.returncode == 0, resolved.stderr
    payload = json.loads(resolved.stdout)
    assert payload["artifact"]["content"] == "Pause writes, restore, verify, then switch traffic."

    org = _rdn(["contribute", "team note", "--uri", ADDRESS, "--scope", "organization"], home)
    assert org.returncode == 0, org.stderr
    assert "No organization server is set up" in org.stderr

    queue = _rdn(["queue", "--flush"], home)
    assert queue.returncode == 0, queue.stderr
    assert "no organization server is set up" in queue.stderr

    missing = _rdn(["resolve", "reason://ops/deployment/unknown-task"], home)
    assert missing.returncode == 1
    assert "Nothing is saved under that address yet" in missing.stderr


# --------------------------------------------------------------------------
# Dashboard status and launch settings (no Streamlit needed)
# --------------------------------------------------------------------------


def test_dashboard_reports_offline_when_services_are_down(tmp_path, monkeypatch) -> None:
    import rdn.dash as dash

    client = _client(tmp_path, monkeypatch)
    client.node_url = _closed_port_url()
    client.resolvers["organization"] = _closed_port_url()
    client.resolvers["shared"] = _closed_port_url()
    client.broker_url = _closed_port_url()

    unchecked = {item["name"]: item["state"] for item in dash.service_statuses(client)}
    assert unchecked["Local node"] == dash.OFFLINE
    assert unchecked["Organization server"] == dash.NOT_CHECKED
    assert unchecked["Public Reason Registry"] == dash.NOT_CHECKED
    assert unchecked["WARF arbitration service"] == dash.NOT_CHECKED

    checked = {
        item["name"]: item["state"]
        for item in dash.service_statuses(client, check_hosted=True)
    }
    assert set(checked.values()) == {dash.OFFLINE}

    client.node_url = None
    client.resolvers["organization"] = None
    states = {item["name"]: item["state"] for item in dash.service_statuses(client)}
    assert states["Local node"] == dash.OFFLINE
    assert states["Organization server"] == dash.NOT_SET_UP


def test_dashboard_reports_online_for_a_running_node(tmp_path, monkeypatch) -> None:
    import rdn.dash as dash
    from rdn.node.server import PrivateWARFNodeServer, PrivateWARFRequestHandler

    server = PrivateWARFNodeServer(
        ("127.0.0.1", 0), PrivateWARFRequestHandler, db_path=str(tmp_path / "node.db")
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert dash.probe(url) == dash.ONLINE
        client = _client(tmp_path, monkeypatch)
        client.node_url = url
        states = {item["name"]: item["state"] for item in dash.service_statuses(client)}
        assert states["Local node"] == dash.ONLINE
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_counts_are_real(tmp_path, monkeypatch) -> None:
    import rdn.dash as dash

    client = _client(tmp_path, monkeypatch)
    assert dash.local_counts(client) == {
        "notes": 0,
        "contributions": 0,
        "waiting_to_send": 0,
        "delivered": 0,
    }
    client.remember("one", project="demo")
    client.remember("two", project="demo")
    client.contribute("answer", reason_address=ADDRESS, scope="local")
    counts = dash.local_counts(client)
    assert counts["notes"] == 2
    assert counts["contributions"] == 1


def test_dashboard_binds_locally_and_skips_prompts_by_default() -> None:
    import rdn.dash as dash

    args = dash.build_launch_args()
    assert args[args.index("--server.address") + 1] == "127.0.0.1"
    assert args[args.index("--server.port") + 1] == "8501"
    assert args[args.index("--server.headless") + 1] == "true"
    assert args[args.index("--browser.gatherUsageStats") + 1] == "false"

    network = dash.build_launch_args(port=9000, network=True)
    assert network[network.index("--server.address") + 1] == "0.0.0.0"
    assert network[network.index("--server.port") + 1] == "9000"


def test_mcp_server_reports_package_version() -> None:
    pytest.importorskip("mcp")
    from rdn.mcp.server import WARFMCPServer

    options = WARFMCPServer().server.create_initialization_options()
    assert options.server_version == rdn.__version__
