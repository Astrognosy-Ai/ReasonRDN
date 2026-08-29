"""Focused 0.6.1 contract tests for the ReasonRDN SDK and MCP lock."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import rfc8785

from rdn.artifact import (
    CANONICAL_ARTIFACT_FIELDS,
    EVENT_RECORD_SCHEMA,
    MCP_ADVERTISED_TOOLS,
    PROTOCOL_LOCK,
    PROTOCOL_LOCK_DIGEST,
    PROTOCOL_LOCK_ID,
    REGISTRY_VALIDATION_METHODS,
    ArtifactValidationError,
    ReasonArtifact,
    artifact_version,
    content_identity,
    parse_reason_artifact,
    sort_validation_records,
)
from rdn.cli import _resolve_source
from rdn.client import (
    RDNAuthorizationError,
    RDNClient,
    RDNConflictError,
    RDNNotFoundError,
    RDNUnavailableError,
)


def _registry_artifact(
    *,
    address: str = "reason://ops/deployment/rollback-plan",
    content="verify then switch",
    validation=None,
    admitted_at="2026-08-15T15:00:00Z",
):
    validation = validation or [
        {
            "method": EVENT_RECORD_SCHEMA,
            "audit_hash": "a" * 64,
            "query_id": "rollback-001",
        }
    ]
    validation = sort_validation_records(validation)
    encoding, digest = content_identity(content)
    media_type = "text/plain; charset=utf-8" if isinstance(content, str) else "application/json"
    version = artifact_version(
        address=address,
        media_type=media_type,
        content_digest=digest,
        canonical_encoding=encoding,
        validation=validation,
    )
    return {
        "address": address,
        "version": version,
        "media_type": media_type,
        "content": content,
        "content_digest": digest,
        "content_digest_algorithm": "SHA-256",
        "canonical_encoding": encoding,
        "validation": validation,
        "admitted_at": admitted_at,
    }


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(RDNClient, "_discover_local_node_via_port", lambda self: None)
    return RDNClient(db_path=tmp_path / "reason.db")


def test_protocol_lock_is_the_imported_single_source():
    assert PROTOCOL_LOCK_ID == "astrognosy.reason-artifact/v1"
    assert PROTOCOL_LOCK["packageVersion"] == "0.6.1"
    assert PROTOCOL_LOCK["eventRecordSchema"] == EVENT_RECORD_SCHEMA
    assert tuple(PROTOCOL_LOCK["artifact"]["fields"]) == CANONICAL_ARTIFACT_FIELDS
    assert tuple(PROTOCOL_LOCK["mcp"]["advertisedTools"]) == MCP_ADVERTISED_TOOLS
    contribution = PROTOCOL_LOCK["contribution"]
    assert contribution["scopes"] == ["local", "organization", "shared"]
    assert contribution["networkScopes"] == ["organization", "shared"]
    assert contribution["receiptFields"] == [
        "contribution_id",
        "reason_address",
        "scope",
        "status",
        "replayed",
        "epoch_digest",
        "convergence_event_digest",
        "decision",
        "current_version",
    ]
    assert contribution["receiptStatuses"] == ["converged", "held"]
    assert contribution["receiptDecisions"] == [
        "bootstrap-provisional",
        "provisional-rebase",
        "advance",
        "hold",
        "incumbent-retained",
        "abstained",
        "adapter-required",
    ]
    assert "profile" not in rfc8785.dumps(PROTOCOL_LOCK).decode("utf-8").lower()
    assert len(PROTOCOL_LOCK_DIGEST) == 64
    assert PROTOCOL_LOCK_DIGEST == hashlib.sha256(rfc8785.dumps(PROTOCOL_LOCK)).hexdigest()


def test_local_legacy_data_adapts_to_exact_wire_artifact():
    artifact = parse_reason_artifact(
        {
            "address": "reason://ops/deployment/rollback-plan",
            "content": "verify then switch",
            "deposited_at": "2026-08-15T15:00:00Z",
            "meta": {"artifact_hash": "legacy-retained", "tags": ["ops"]},
            "project": "ops",
        },
        source="local",
    )

    assert isinstance(artifact, ReasonArtifact)
    assert tuple(artifact.to_dict()) == CANONICAL_ARTIFACT_FIELDS
    assert artifact.source == "local"
    assert artifact.verify_content_digest() is True
    assert artifact.verify_version() is True
    assert artifact.get("meta")["artifact_hash"] == "legacy-retained"
    assert artifact.resolution_dict() == {
        "source": "local",
        "artifact": artifact.to_dict(),
    }


def test_structured_content_and_validation_order_have_stable_identity():
    records = [
        {"method": EVENT_RECORD_SCHEMA, "query_id": "z", "audit_hash": "b" * 64},
        {"method": EVENT_RECORD_SCHEMA, "query_id": "a", "audit_hash": "a" * 64},
    ]
    first = _registry_artifact(content={"b": 2, "a": 1}, validation=records)
    second = _registry_artifact(
        content={"a": 1, "b": 2}, validation=list(reversed(records))
    )

    assert first["content_digest"] == second["content_digest"]
    assert first["version"] == second["version"]
    assert parse_reason_artifact(first, source="registry").verify_version()

    later = dict(first)
    later["admitted_at"] = "2027-01-01T00:00:00Z"
    assert parse_reason_artifact(later, source="registry").version == first["version"]


@pytest.mark.parametrize("missing", ["version", "content_digest", "canonical_encoding"])
def test_registry_parser_rejects_missing_locked_fields(missing):
    payload = _registry_artifact()
    payload.pop(missing)
    with pytest.raises(ArtifactValidationError, match="exactly the nine locked fields"):
        parse_reason_artifact(payload, source="registry")


def test_registry_parser_rejects_extra_fields_and_bad_admission_time():
    extra = _registry_artifact()
    extra["source"] = "registry"
    with pytest.raises(ArtifactValidationError, match="extra"):
        parse_reason_artifact(extra, source="registry")

    bad_time = _registry_artifact(admitted_at="")
    with pytest.raises(ArtifactValidationError, match="admitted_at"):
        parse_reason_artifact(bad_time, source="registry")


def test_registry_parser_rejects_changed_content_or_version():
    changed_content = _registry_artifact()
    changed_content["content"] = "switch without verification"
    with pytest.raises(ArtifactValidationError, match="content_digest"):
        parse_reason_artifact(changed_content, source="registry")

    changed_version = _registry_artifact()
    changed_version["version"] = "sha256:" + "0" * 64
    with pytest.raises(ArtifactValidationError, match="version"):
        parse_reason_artifact(changed_version, source="registry")


def test_registry_parser_rejects_empty_or_unsorted_validation():
    empty = _registry_artifact()
    empty["validation"] = []
    empty["version"] = artifact_version(
        address=empty["address"],
        media_type=empty["media_type"],
        content_digest=empty["content_digest"],
        canonical_encoding=empty["canonical_encoding"],
        validation=[],
    )
    with pytest.raises(ArtifactValidationError, match="at least one"):
        parse_reason_artifact(empty, source="registry")

    records = [
        {"method": EVENT_RECORD_SCHEMA, "query_id": "a", "audit_hash": "a" * 64},
        {"method": EVENT_RECORD_SCHEMA, "query_id": "z", "audit_hash": "b" * 64},
    ]
    canonical = sort_validation_records(records)
    unsorted = list(reversed(canonical))
    payload = _registry_artifact(validation=canonical)
    payload["validation"] = unsorted
    with pytest.raises(ArtifactValidationError, match="canonical JCS byte order"):
        parse_reason_artifact(payload, source="registry")


def test_registry_accepts_warf_or_convergence_validation_and_rejects_unknown():
    convergence_method = "https://reason.astrognosy.com/schemas/convergence-event/v1"
    assert REGISTRY_VALIDATION_METHODS == (EVENT_RECORD_SCHEMA, convergence_method)

    warf = parse_reason_artifact(_registry_artifact(), source="registry")
    convergence = parse_reason_artifact(
        _registry_artifact(
            validation=[
                {
                    "method": convergence_method,
                    "event_id": "convergence-1",
                    "profile": "bootstrap-v1",
                }
            ]
        ),
        source="registry",
    )
    assert warf.validation[0]["method"] == EVENT_RECORD_SCHEMA
    assert convergence.validation[0]["method"] == convergence_method

    unknown = _registry_artifact(
        validation=[{"method": "https://example.invalid/unknown", "event_id": "x"}]
    )
    with pytest.raises(ArtifactValidationError, match="must be one of"):
        parse_reason_artifact(unknown, source="registry")


def test_local_and_registry_resolve_return_one_verified_type(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    remembered = client.remember(
        "verify then switch",
        project="ops",
        reason_address="reason://ops/deployment/rollback-plan",
    )
    local = client.resolve(remembered["address"])

    registry_payload = _registry_artifact()
    monkeypatch.setattr(client, "_http_get_strict", lambda url, params=None: registry_payload)
    registry = client.resolve(remembered["address"], source="registry")

    assert isinstance(local, ReasonArtifact)
    assert isinstance(registry, ReasonArtifact)
    assert local.source == "local"
    assert registry.source == "registry"
    assert tuple(local.to_dict()) == tuple(registry.to_dict()) == CANONICAL_ARTIFACT_FIELDS


def test_registry_error_never_falls_back_to_existing_local_copy(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    address = "reason://ops/deployment/rollback-plan"
    client.remember("local copy", project="ops", reason_address=address)

    def not_found(url, params=None):
        raise RDNNotFoundError(404, "unknown address", {"detail": "unknown"})

    monkeypatch.setattr(client, "_http_get_strict", not_found)
    with pytest.raises(RDNNotFoundError) as exc_info:
        client.resolve(address, source="registry")
    assert exc_info.value.status_code == 404
    assert client.resolve(address, source="local").content == "local copy"


def test_admit_sends_nested_custody_create_only_and_registry_api_key(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    client.xport_url = "https://reason.example"
    monkeypatch.setenv("REASON_REGISTRY_API_KEY", "registry-secret")
    monkeypatch.setenv("WARF_API_KEY", "wrong-bearer")
    candidate = parse_reason_artifact(
        {
            "address": "reason://ops/deployment/rollback-plan",
            "content": "verify then switch",
        },
        source="local",
    )
    event_record = {
        "schema": EVENT_RECORD_SCHEMA,
        "query_id": "rollback-001",
        "winner": "agent-a",
    }
    audit_hash = hashlib.sha256(rfc8785.dumps(event_record)).hexdigest()
    arbitration = {
        "query_id": "rollback-001",
        "winner_submission_id": "agent-a",
        "event_record": event_record,
        "audit_hash": audit_hash,
    }
    response = _registry_artifact(
        validation=[
            {
                "method": EVENT_RECORD_SCHEMA,
                "query_id": "rollback-001",
                "audit_hash": audit_hash,
            }
        ]
    )
    captured = {}

    def fake_post(url, payload, *, headers=None):
        captured.update(url=url, payload=payload, headers=headers)
        return response

    monkeypatch.setattr(client, "_http_post_strict", fake_post)
    admitted = client.admit(candidate, arbitration)

    assert admitted.source == "registry"
    assert captured["url"] == "https://reason.example/admissions"
    assert captured["headers"] == {"X-API-Key": "registry-secret"}
    assert set(captured["payload"]) == {
        "artifact",
        "arbitration",
        "expected_current_version",
    }
    assert captured["payload"]["expected_current_version"] is None
    assert set(captured["payload"]["artifact"]) == {
        "address",
        "media_type",
        "content",
        "content_digest",
        "content_digest_algorithm",
        "canonical_encoding",
    }
    assert captured["payload"]["arbitration"] == arbitration


def test_http_error_classes_preserve_authorization_conflict_and_availability(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    assert isinstance(client._http_error(401, {"detail": "no"}), RDNAuthorizationError)
    assert isinstance(client._http_error(409, {"detail": "cas"}), RDNConflictError)
    assert isinstance(client._http_error(503, {"detail": "later"}), RDNUnavailableError)


def test_mcp_advertises_exactly_the_six_locked_tools():
    import rdn.mcp.server as mcp_server

    if mcp_server.Tool is None:
        pytest.skip("optional MCP dependency is not installed")
    server = mcp_server.WARFMCPServer.__new__(mcp_server.WARFMCPServer)
    names = tuple(tool.name for tool in server._tool_schemas())
    assert names == MCP_ADVERTISED_TOOLS
    assert names == ("remember", "recall", "resolve", "contribute", "arbitrate", "status")
    assert "admit" not in names
    assert not {
        "network_resolve",
        "network_share",
        "xchange_resolve",
        "xchange_share",
        "harness_status",
    }.intersection(names)


def test_mcp_extras_stay_on_the_compatible_major_version():
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert pyproject.count('"mcp>=1.0.0,<2"') == 2
    assert '"mcp>=1.0.0"' not in pyproject


def test_cli_plain_resolve_stays_local_despite_ambient_network(monkeypatch):
    class Args:
        source = None
        xchange = False

    monkeypatch.setenv("REASON_USE_NETWORK", "1")
    assert _resolve_source(Args()) == "local"

    explicit = Args()
    explicit.source = "registry"
    assert _resolve_source(explicit) == "registry"

    compatibility = Args()
    compatibility.xchange = True
    assert _resolve_source(compatibility) == "registry"
