"""
tests/test_coherent_memory.py

Real tests for the coherent unified ReasonRDN memory system (post-restructure).

These exercise the actual stack:
- rdn.node.server (in-process)
- rdn.client.RDNClient (unified, node + local fallback)
- rdn.handoff.ReasonRDN + local artifact hash metadata
- Cross visibility (deposit via high-level, recall/resolve via client)
- HarnessMetrics + public record_handoff/record_recall (for real token accounting
  when using the harness as on-ramp to the external reason:// network)
"""

import json
import os
import re
import tempfile
import threading
import time

import pytest

from rdn.addressing import project_address, project_label
from rdn.cli import _network_enabled
from rdn.client import WARF_GATEWAY_URL, RDNClient
from rdn.config import env_flag
from rdn.handoff import ArtifactFingerprint, ReasonRDN
from rdn.mcp.server import _network_share_envelope
from rdn.node.server import PrivateWARFNodeServer, PrivateWARFRequestHandler, default_db_path

# Token accounting / harness metrics records for network participation.


@pytest.fixture
def in_process_node():
    """Start a real private node in a temp dir and yield (node_url, db_path)."""
    with tempfile.TemporaryDirectory() as td:
        storage = os.path.join(td, "node")
        db_path = default_db_path(storage)
        os.makedirs(storage, exist_ok=True)

        server = PrivateWARFNodeServer(("127.0.0.1", 0), PrivateWARFRequestHandler, db_path=db_path)
        port = server.server_address[1]
        node_url = f"http://127.0.0.1:{port}"

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.25)

        try:
            yield node_url, db_path
        finally:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
            thread.join(timeout=2)


def test_artifact_fingerprint_basic():
    eng = ArtifactFingerprint()
    tokens = ["main", "src/foo.py", "fix the thing", "Main", "src/foo.py"]
    fp = eng.compute(tokens)
    assert isinstance(fp, str) and len(fp) == 64
    assert eng.verify(tokens, fp) is True
    assert eng.verify(tokens + ["extra"], fp) is False
    assert eng.strength(tokens) == 3


def test_end_to_end_node_handoff_and_resolve(in_process_node):
    node_url, _ = in_process_node

    # High-level deposit via handoff + client -> HTTP node.
    rdn = ReasonRDN(node_url=node_url)
    tokens = ["repo:ReasonRDN", "branch:coherent", "touched:rdn/client.py", "infra-fix"]
    res = rdn.deposit_handoff(
        project="ReasonRDN",
        summary="Made the memory system coherent with a unified client and clean handoff metadata.",
        state_tokens=tokens,
        tags=["infra", "handoff", "coherent"]
    )
    assert res["status"] == "remembered"
    addr = res["address"]
    assert addr.startswith("reason://reasonrdn/handoff/")

    # Unified client recall (node path)
    c = RDNClient(node_url=node_url)
    results = c.recall(query="coherent", project="ReasonRDN", limit=10)
    assert len(results) >= 1
    assert "coherent" in results[0]["content"].lower()
    assert results[0]["source"] == "node"

    # Resolve (contract is now "artifact")
    art = c.resolve(addr)
    assert art is not None
    assert art["address"] == addr
    assert "artifact_hash" in (art.get("meta") or {})

    eng = ArtifactFingerprint()
    stored = (art["meta"] or {}).get("artifact_hash")
    assert eng.verify(tokens, stored) is True


def test_local_fallback_when_no_node():
    c = RDNClient(node_url="http://127.0.0.1:1")  # will fail health
    c.available = False
    c.node_url = None

    res = c.remember("Pure local path still works", tags=["fallback-test"], project="ReasonRDN")
    assert res["status"] == "remembered"
    assert res["source"] == "local"

    rec = c.recall(query="local path", project="ReasonRDN", limit=5)
    assert len(rec) >= 1
    assert "fallback" in rec[0].get("content", "").lower() or "local" in rec[0].get("content", "").lower()


def test_harness_metrics_record_functions(tmp_path, monkeypatch):
    """Test the public record_handoff / record_recall helpers.

    These ensure that direct/low-level deposit paths (e.g. CLI handoff layer)
    and high-level paths correctly feed HarnessMetrics with tokens_used /
    tokens_saved so the on-ramp can show accurate savings from reason:// artifacts.
    """
    metrics_file = tmp_path / "harness_metrics.json"

    import rdn.reason as reason_mod

    # Point the module at a temp file and recreate the singleton so tests
    # don't touch ~/.reason-rdn and are isolated.
    monkeypatch.setattr(reason_mod, "METRICS_FILE", metrics_file)
    reason_mod._harness_metrics = reason_mod.HarnessMetrics()

    # Start clean
    m0 = reason_mod.harness_metrics()
    assert m0["total_handoffs"] == 0
    assert m0["estimated_tokens_saved"] == 0
    # total_recalls is internal only (not exposed in public summary/harness_metrics())

    # Record a handoff with tokens + "positive" tag (should bump vibe_stars)
    reason_mod.record_handoff(
        "Fixed critical race using prior handoff context",
        tags=["infra", "positive", "coherent"],
        tokens_used=1450,
    )
    m1 = reason_mod.harness_metrics()
    assert m1["total_handoffs"] == 1
    assert m1["estimated_tokens_saved"] == 0
    assert m1["vibe_stars"] >= 1   # from positive tag

    # Record a recall with saved tokens.
    reason_mod.record_recall(
        "reason://ReasonRDN/handoff/abc12345",
        tokens_saved=920,
    )
    m2 = reason_mod.harness_metrics()
    # total_recalls is internal (not in public summary); check via the backing data for the test
    assert reason_mod._harness_metrics.data["total_recalls"] == 1
    assert m2["estimated_tokens_saved"] == 920

    # Summary shape (what status / dashboard / MCP harness_status expose)
    summary = reason_mod.harness_metrics()
    assert "velocity" in summary
    assert "ship_rate" in summary
    assert "suggestions" in summary
    assert "total_handoffs" in summary

    # Also exercise the high-level wrappers (they call the records internally)
    # This path is what `import rdn as reason; reason.remember(..., tokens_used=...)` uses.
    reason_mod.remember("High-level remember with tokens", tokens_used=300, tags=["test"])
    m3 = reason_mod.harness_metrics()
    assert m3["total_handoffs"] == 2

    reason_mod.resolve("reason://test/memory/uri", tokens_saved=150)
    m4 = reason_mod.harness_metrics()
    assert reason_mod._harness_metrics.data["total_recalls"] == 2
    assert m4["estimated_tokens_saved"] == 920 + 150


def test_gateway_health_uses_public_health_route(tmp_path, monkeypatch):
    import rdn.client as client_mod

    requested = []

    class Response:
        ok = True

        @staticmethod
        def json():
            return {"status": "ok"}

    class Requests:
        @staticmethod
        def get(url, timeout):
            requested.append(url)
            return Response()

    monkeypatch.setattr(client_mod, "requests", Requests())
    client = RDNClient(
        node_url="https://warf.example",
        db_path=tmp_path / "health.db",
    )

    assert client.available is True
    assert requested == ["https://warf.example/health"]


def test_network_arbitration_uses_public_gateway_route(tmp_path, monkeypatch):
    client = RDNClient(db_path=tmp_path / "arbitrate.db")
    client.broker_url = "https://warf.example"
    captured = {}

    def fake_post(url, payload):
        captured["url"] = url
        captured["payload"] = payload
        return {"winner": {"agent_id": "agent-a"}, "audit_hash": "abc"}

    monkeypatch.setattr(client, "_http_post", fake_post)
    result = client.network_arbitrate(
        "Choose the safest plan",
        [
            {"agent_id": "agent-a", "answer_text": "verify then switch"},
            {"agent_id": "agent-b", "answer_text": "switch immediately"},
        ],
    )

    assert captured["url"] == "https://warf.example/arbitrate"
    assert captured["payload"]["query_id"].startswith("rdn-")
    assert result["winner"]["agent_id"] == "agent-a"


def test_registry_resolution_uses_registry_not_gateway(tmp_path, monkeypatch):
    client = RDNClient(db_path=tmp_path / "resolve.db")
    client.broker_url = "https://warf.example"
    client.node_url = client.broker_url
    client.xport_url = "https://reason.example"
    captured = {}

    def fake_resolve(target, uri, bypass_cache=False):
        captured.update(target=target, uri=uri, bypass_cache=bypass_cache)
        return {"address": uri, "content_digest": "abc"}

    monkeypatch.setattr(client, "_resolve_reason_uri_at", fake_resolve)
    result = client.resolve_from_registry(
        "reason://ops/deployment/rollback-plan",
        bypass_cache=True,
    )

    assert captured == {
        "target": "https://reason.example",
        "uri": "reason://ops/deployment/rollback-plan",
        "bypass_cache": True,
    }
    assert result["address"] == "reason://ops/deployment/rollback-plan"


def test_reason_client_uses_registry_route_for_reason_uri(monkeypatch):
    from rdn.reason import ReasonClient

    client = ReasonClient(endpoint="https://reason.example")
    captured = {}

    def fake_registry_resolve(uri, bypass_cache=False, version=None):
        captured.update(uri=uri, bypass_cache=bypass_cache, version=version)
        return {"address": uri}

    monkeypatch.setattr(client._client, "resolve_from_registry", fake_registry_resolve)
    address = "reason://ops/deployment/rollback-plan"

    assert client.resolve(address) == {"address": address}
    assert captured == {"uri": address, "bypass_cache": False, "version": None}


def test_explicit_network_request_retains_local_copy_and_requires_admission(monkeypatch):
    from rdn.reason import Reason

    reason = Reason(network=False)
    calls = []

    def fake_remember(content, **kwargs):
        calls.append(("local", content, kwargs))
        return {
            "status": "remembered",
            "address": kwargs["reason_address"],
            "artifact_id": "local-1",
        }

    monkeypatch.setattr(reason._client, "remember", fake_remember)
    monkeypatch.setattr(
        reason._client,
        "share_to_network",
        lambda **kwargs: pytest.fail("remember must not call the legacy /share route"),
    )

    result = reason.remember(
        "Reusable handoff",
        uri="reason://demo/handoff/one",
        tags=["handoff"],
        project="demo",
        network_share=True,
    )

    assert [call[0] for call in calls] == ["local"]
    assert calls[0][2]["reason_address"] == "reason://demo/handoff/one"
    assert result["status"] == "admission-required"
    assert result["reason"] == "requires-arbitration"
    assert result["next"] == ["arbitrate", "admit"]
    assert result["local_copy_retained"] is True
    assert result["local"]["artifact_id"] == "local-1"


def test_per_use_false_keeps_handoff_local_even_in_network_mode(monkeypatch):
    from rdn.reason import Reason

    reason = Reason(network=True)
    monkeypatch.setattr(
        reason._client,
        "remember",
        lambda content, **kwargs: {"status": "remembered", "address": "reason://demo/handoff/h-1"},
    )
    monkeypatch.setattr(
        reason._client,
        "share_to_network",
        lambda **kwargs: pytest.fail("network share should not be called"),
    )

    result = reason.remember("Local only", network_share=False)

    assert result["status"] == "remembered"


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "unexpected"])
def test_network_env_flags_require_explicit_true(value, monkeypatch):
    monkeypatch.setenv("REASON_USE_NETWORK", value)
    monkeypatch.delenv("REASON_USE_XCHANGE", raising=False)
    assert env_flag("REASON_USE_NETWORK", "REASON_USE_XCHANGE") is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_network_env_flags_accept_documented_true_values(value, monkeypatch):
    monkeypatch.setenv("REASON_USE_NETWORK", value)
    assert env_flag("REASON_USE_NETWORK") is True


def test_cli_network_mode_honors_documented_environment_flag(monkeypatch):
    class Args:
        xchange = False

    monkeypatch.setenv("REASON_USE_NETWORK", "1")
    assert _network_enabled(Args()) is True

    monkeypatch.setenv("REASON_USE_NETWORK", "0")
    assert _network_enabled(Args()) is False


def test_cli_configured_endpoints_are_availability_only(monkeypatch):
    class Args:
        xchange = False
        local = False

    class Client:
        broker_url = "https://warf.example"
        xport_url = "https://reason.example"

    for name in (
        "REASON_USE_NETWORK",
        "REASON_USE_XCHANGE",
        "USE_WARF_XCHANGE",
        "REASON_XCHANGE",
        "XCHANGE",
    ):
        monkeypatch.delenv(name, raising=False)

    assert _network_enabled(Args(), Client()) is False

    args = Args()
    args.local = True
    assert _network_enabled(args, Client()) is False


def test_reason_defaults_stay_local_when_env_is_zero(monkeypatch):
    import rdn.reason as reason_mod

    class StubClient:
        broker_url = None
        xport_url = None
        available = False

    monkeypatch.setattr(reason_mod, "RDNClient", StubClient)
    monkeypatch.setattr(reason_mod, "_default_reason", None)
    monkeypatch.setenv("REASON_USE_NETWORK", "0")
    monkeypatch.setenv("REASON_USE_XCHANGE", "false")

    direct = reason_mod.Reason()
    default = reason_mod._get_default()

    assert direct.status["network_mode"] is False
    assert default.status["network_mode"] is False


def test_client_network_default_stays_off_when_env_is_zero(tmp_path, monkeypatch):
    for name in (
        "REASON_USE_NETWORK",
        "REASON_USE_XCHANGE",
        "USE_WARF_XCHANGE",
        "REASON_XCHANGE",
        "XCHANGE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REASON_USE_NETWORK", "0")

    client = RDNClient(db_path=tmp_path / "local-default.db")

    assert client.broker_url is None
    assert client.xport_url is None


def test_installer_network_config_is_consumed_without_replacing_local_node(
    tmp_path, monkeypatch
):
    import rdn.client as client_mod

    config_path = tmp_path / "reason.cfg"
    config_path.write_text(
        json.dumps(
            {
                "node_url": "http://127.0.0.1:9876",
                "network_enabled": True,
                "gateway_url": "https://warf.example",
                "registry_url": "https://reason.example",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(client_mod, "CONFIG_FILE", config_path)
    for name in (
        "REASON_USE_NETWORK",
        "REASON_USE_XCHANGE",
        "USE_WARF_XCHANGE",
        "REASON_XCHANGE",
        "XCHANGE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(client_mod.RDNClient, "_check_health", lambda self: True)

    client = client_mod.RDNClient(db_path=tmp_path / "configured.db")

    assert client.node_url == "http://127.0.0.1:9876"
    assert client.broker_url == "https://warf.example"
    assert client.xport_url == "https://reason.example"


def test_high_level_reason_preserves_configured_network_endpoints(monkeypatch):
    import rdn.reason as reason_mod

    class ConfiguredClient:
        broker_url = "https://warf.internal.example"
        xport_url = "https://reason.internal.example"
        available = True

    monkeypatch.setattr(reason_mod, "RDNClient", ConfiguredClient)
    reason = reason_mod.Reason()

    assert reason.status["gateway"] == "https://warf.internal.example"
    assert reason.status["registry"] == "https://reason.internal.example"


def test_network_arbitration_never_uses_discovered_local_node(tmp_path, monkeypatch):
    client = RDNClient(db_path=tmp_path / "gateway-only.db")
    client.broker_url = None
    client.node_url = "http://127.0.0.1:8765"
    captured = {}

    def fake_post(url, payload):
        captured["url"] = url
        return {"winner": {"agent_id": "agent-a"}, "audit_hash": "abc"}

    monkeypatch.setattr(client, "_http_post", fake_post)
    client.network_arbitrate(
        "Choose a plan",
        [
            {"agent_id": "agent-a", "answer_text": "verify then switch"},
            {"agent_id": "agent-b", "answer_text": "switch immediately"},
        ],
    )

    assert captured["url"] == f"{WARF_GATEWAY_URL}/arbitrate"


def test_arbitration_query_id_covers_address_and_profile(tmp_path, monkeypatch):
    client = RDNClient(db_path=tmp_path / "query-id.db")
    client.broker_url = "https://warf.example"
    payloads = []

    def fake_post(url, payload):
        payloads.append(payload)
        return {"winner": {"agent_id": "agent-a"}, "audit_hash": "abc"}

    monkeypatch.setattr(client, "_http_post", fake_post)
    packages = [
        {"agent_id": "agent-a", "answer_text": "one"},
        {"agent_id": "agent-b", "answer_text": "two"},
    ]
    client.network_arbitrate(
        "Choose",
        packages,
        reason_address="reason://demo/decision/one",
        profile={"id": "portable-handoff", "version": "1"},
    )
    client.network_arbitrate(
        "Choose",
        packages,
        reason_address="reason://demo/decision/two",
        profile={"id": "portable-handoff", "version": "1"},
    )
    client.network_arbitrate(
        "Choose",
        packages,
        reason_address="reason://demo/decision/one",
        profile={"id": "portable-handoff", "version": "2"},
    )

    query_ids = [payload["query_id"] for payload in payloads]
    assert len(set(query_ids)) == 3
    assert payloads[0]["profile"] == {"id": "portable-handoff", "version": "1"}
    assert payloads[0]["reason_address"] == "reason://demo/decision/one"


def test_arbitration_requires_two_competing_submissions(tmp_path):
    client = RDNClient(db_path=tmp_path / "one-submission.db")

    with pytest.raises(ValueError, match="at least two submissions"):
        client.network_arbitrate(
            "Choose",
            [{"agent_id": "agent-a", "answer_text": "only one"}],
        )


def test_direct_network_share_fallback_does_not_claim_local_retention(tmp_path, monkeypatch):
    client = RDNClient(db_path=tmp_path / "share-fallback.db", mirror_local=True)
    monkeypatch.setattr(client, "_http_post", lambda url, payload: None)

    result = client.share_to_network("Selected handoff")

    assert result["status"] == "unavailable"
    assert result["local_copy_retained"] is False


def test_mcp_network_share_envelope_preserves_actual_outcome():
    unavailable = _network_share_envelope(
        {"status": "unavailable", "local_copy_retained": True}
    )
    successful = _network_share_envelope(
        {"winner": {"agent_id": "agent-a"}, "audit_hash": "abc"}
    )

    assert unavailable["status"] == "unavailable"
    assert unavailable["result"]["local_copy_retained"] is True
    assert successful["status"] == "shared"


def test_network_success_metric_uses_the_actual_result():
    from rdn.reason import _network_result_succeeded

    assert _network_result_succeeded({"status": "unavailable"}) is False
    assert _network_result_succeeded({"status": "error"}) is False
    assert _network_result_succeeded(
        {"winner": {"agent_id": "agent-a"}, "audit_hash": "abc"}
    ) is True


@pytest.mark.parametrize(
    ("project", "expected"),
    [
        ("ReasonRDN", "reasonrdn"),
        ("Reason RDN!", "reason-rdn"),
        ("123 project", "project"),
        ("123", "local"),
        ("Astrognôsy", "astrognosy"),
    ],
)
def test_project_address_uses_valid_lowercase_ascii_label(project, expected):
    assert project_label(project) == expected
    address = project_address(project, "same content")
    assert address.startswith(f"reason://{expected}/handoff/h-")
    assert re.fullmatch(
        r"reason://[a-z][a-z0-9-]*/[a-z][a-z0-9-]*/[a-z][a-z0-9-]*",
        address,
    )


def test_project_address_covers_the_full_handoff_content():
    shared_prefix = "x" * 50
    first = project_address("ReasonRDN", f"{shared_prefix}-first")
    second = project_address("ReasonRDN", f"{shared_prefix}-second")

    assert first != second
