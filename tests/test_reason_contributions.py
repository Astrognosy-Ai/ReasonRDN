from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from rdn.artifact import EVENT_RECORD_SCHEMA, ArtifactValidationError, ReasonArtifact
from rdn.client import RDNClient, RDNHTTPError, RDNNotFoundError, RDNTransportError
from rdn.contribution import (
    CONTRIBUTION_CONTENT_MAX_BYTES,
    CONTRIBUTION_SCHEMA,
    ContributionEnvelope,
    parse_contribution_envelope,
    parse_contribution_receipt,
)


ADDRESS = "reason://agents/memory/resolve-first"


def _client(tmp_path: Path, monkeypatch) -> RDNClient:
    monkeypatch.setattr(RDNClient, "_discover_local_node_via_port", lambda self: None)
    monkeypatch.setattr(RDNClient, "_check_health", lambda self: False)
    return RDNClient(db_path=tmp_path / "reason.db")


def _registry_artifact(address: str = ADDRESS, content: str = "resolved") -> ReasonArtifact:
    candidate = ReasonArtifact.from_dict(
        {
            "address": address,
            "media_type": "text/plain; charset=utf-8",
            "content": content,
            "validation": [
                {
                    "method": EVENT_RECORD_SCHEMA,
                    "event_id": "event-1",
                }
            ],
            "admitted_at": "2026-08-22T00:00:00Z",
        },
        source="local",
    )
    return ReasonArtifact.from_dict(candidate.to_dict(), source="registry")


def _receipt(payload, **changes):
    receipt = {
        "contribution_id": payload["contribution_id"],
        "reason_address": payload["reason_address"],
        "scope": payload["scope"],
        "status": "converged",
        "replayed": False,
        "epoch_digest": "a" * 64,
        "convergence_event_digest": "b" * 64,
        "decision": "bootstrap-provisional",
        "current_version": "sha256:" + "c" * 64,
    }
    receipt.update(changes)
    return receipt


def test_envelope_preserves_exact_xml_bytes_and_digest() -> None:
    exact = b'\xef\xbb\xbf<?xml version="1.0"?>\r\n<doclang>  value </doclang>\r\n'
    envelope = ContributionEnvelope.create(
        exact,
        reason_address=ADDRESS,
        scope="organization",
        media_type="application/xml",
        created_at="2026-08-22T00:00:00Z",
    )

    assert envelope.schema == CONTRIBUTION_SCHEMA
    assert envelope.content_bytes() == exact
    assert envelope.artifact["media_type"] == "application/xml"
    assert envelope.artifact["content_digest"] == {
        "algorithm": "SHA-256",
        "value": hashlib.sha256(exact).hexdigest(),
    }
    assert envelope.adapter == {}
    assert parse_contribution_envelope(envelope.to_dict()) == envelope


def test_contribution_identity_excludes_created_at_but_includes_scope() -> None:
    first = ContributionEnvelope.create(
        "same",
        reason_address=ADDRESS,
        scope="organization",
        created_at="2026-08-22T00:00:00Z",
    )
    later = ContributionEnvelope.create(
        "same",
        reason_address=ADDRESS,
        scope="organization",
        created_at="2027-01-01T00:00:00Z",
    )
    shared = ContributionEnvelope.create(
        "same",
        reason_address=ADDRESS,
        scope="shared",
        created_at="2026-08-22T00:00:00Z",
    )

    assert first.contribution_id == later.contribution_id
    assert first.contribution_id != shared.contribution_id
    assert first.contribution_id.startswith("sha256:")
    assert len(first.contribution_id) == len("sha256:") + 64


def test_envelope_rejects_tampering_and_oversized_content() -> None:
    envelope = ContributionEnvelope.create(
        b"exact",
        reason_address=ADDRESS,
        scope="organization",
    ).to_dict()
    envelope["artifact"]["content_base64"] = "Y2hhbmdlZA=="
    with pytest.raises(ArtifactValidationError, match="content_digest"):
        parse_contribution_envelope(envelope)

    with pytest.raises(ArtifactValidationError, match="exceeds"):
        ContributionEnvelope.create(
            b"x" * (CONTRIBUTION_CONTENT_MAX_BYTES + 1),
            reason_address=ADDRESS,
            scope="organization",
        )


def test_envelope_bounds_forward_compatible_context_and_adapter() -> None:
    with pytest.raises(ArtifactValidationError, match="context exceeds"):
        ContributionEnvelope.create(
            "small",
            reason_address=ADDRESS,
            scope="organization",
            context={"future": "x" * 17000},
        )
    with pytest.raises(ArtifactValidationError, match="adapter exceeds"):
        ContributionEnvelope.create(
            "small",
            reason_address=ADDRESS,
            scope="organization",
            adapter={"future": "x" * 9000},
        )


def test_local_contribution_is_retained_and_never_posts(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        client,
        "_http_post_strict",
        lambda *args, **kwargs: pytest.fail("local contribution must never POST"),
    )

    result = client.contribute(
        "local knowledge",
        reason_address=ADDRESS,
        scope="local",
        background=True,
        flush=True,
    )

    assert result["status"] == "retained"
    assert result["network_write"] is False
    rows = client.inspect_contributions(include_envelope=True)
    assert len(rows) == 1
    assert rows[0]["state"] == "local"
    assert rows[0]["envelope"]["adapter"] == {}
    assert client.flush_contributions(limit=10)["attempted"] == 0


def test_network_contribution_is_durable_and_idempotent_before_flush(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://reason.example"

    first = client.contribute(
        "organization knowledge",
        reason_address=ADDRESS,
        scope="organization",
        background=False,
    )
    second = client.contribute(
        "organization knowledge",
        reason_address=ADDRESS,
        scope="organization",
        background=False,
    )

    assert first["status"] == "pending"
    assert second["contribution_id"] == first["contribution_id"]
    assert len(client.inspect_contributions()) == 1
    assert client.contribution_queue_status()["ready"] == 1


def test_existing_queue_migrates_to_explicit_sequence_without_reordering(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "reason.db"
    first = ContributionEnvelope.create(
        "old first", reason_address=ADDRESS, scope="organization"
    )
    second = ContributionEnvelope.create(
        "old second", reason_address=ADDRESS, scope="organization"
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE rdn_contribution_queue (
                contribution_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                response_json TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        for index, envelope in enumerate((first, second), start=1):
            conn.execute(
                """
                INSERT INTO rdn_contribution_queue
                (contribution_id, scope, envelope_json, state, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (
                    envelope.contribution_id,
                    envelope.scope,
                    json.dumps(envelope.to_dict()),
                    float(index),
                    float(index),
                ),
            )

    client = _client(tmp_path, monkeypatch)
    migrated = client.inspect_contributions()
    assert [row["contribution_id"] for row in migrated] == [
        first.contribution_id,
        second.contribution_id,
    ]
    assert [row["queue_sequence"] for row in migrated] == [1, 2]

    third = client.contribute(
        "new third",
        reason_address=ADDRESS,
        scope="organization",
        background=False,
    )
    assert third["queue"]["queue_sequence"] == 3


def test_flush_posts_locked_envelope_and_idempotency_key(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://reason.example"
    monkeypatch.setenv("REASON_CONTRIBUTION_API_KEY", "contribution-secret")
    monkeypatch.setenv("REASON_REGISTRY_API_KEY", "registry-secret")
    monkeypatch.setenv("WARF_API_KEY", "warf-secret")
    queued = client.contribute(
        b"\x00exact\xff",
        reason_address=ADDRESS,
        scope="organization",
        media_type="application/octet-stream",
        background=False,
    )
    captured = {}

    def fake_post(url, payload, *, headers=None):
        captured.update(url=url, payload=payload, headers=headers)
        return _receipt(payload)

    monkeypatch.setattr(client, "_http_post_strict", fake_post)
    flushed = client.flush_contributions(limit=1, now=100.0)

    assert flushed["delivered"] == 1
    assert captured["url"] == "https://reason.example/contributions"
    assert captured["headers"] == {
        "X-API-Key": "contribution-secret",
        "Idempotency-Key": queued["contribution_id"],
    }
    envelope = parse_contribution_envelope(captured["payload"])
    assert envelope.content_bytes() == b"\x00exact\xff"
    row = client.inspect_contributions()[0]
    assert row["state"] == "delivered"
    assert row["attempts"] == 1
    assert row["response"]["status"] == "converged"


@pytest.mark.parametrize(
    "mutate, error",
    [
        (lambda payload: {"status": "accepted"}, "fields do not match"),
        (
            lambda payload: _receipt(payload, contribution_id="sha256:" + "f" * 64),
            "contribution_id does not match",
        ),
        (
            lambda payload: _receipt(payload, reason_address="reason://agents/memory/other"),
            "reason_address does not match",
        ),
        (lambda payload: _receipt(payload, scope="shared"), "scope does not match"),
        (lambda payload: _receipt(payload, status="accepted"), "status must be"),
        (lambda payload: _receipt(payload, decision="unknown"), "decision must be"),
    ],
)
def test_malformed_or_mismatched_receipt_stays_retryable(
    tmp_path, monkeypatch, mutate, error
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://reason.example"
    client.contribute(
        "receipt-bound knowledge",
        reason_address=ADDRESS,
        scope="organization",
        background=False,
    )

    monkeypatch.setattr(
        client,
        "_http_post_strict",
        lambda _url, payload, **kwargs: mutate(payload),
    )
    result = client.flush_contributions(now=100.0)

    assert result["delivered"] == 0
    assert result["outcomes"][0]["status"] == "retry"
    row = client.inspect_contributions()[0]
    assert row["state"] == "retry"
    assert error in row["last_error"]


def test_receipt_parser_accepts_both_locked_statuses() -> None:
    envelope = ContributionEnvelope.create(
        "receipt",
        reason_address=ADDRESS,
        scope="organization",
    )

    converged = parse_contribution_receipt(_receipt(envelope.to_dict()), envelope=envelope)
    held = parse_contribution_receipt(
        _receipt(
            envelope.to_dict(),
            status="held",
            decision="hold",
            current_version=None,
        ),
        envelope=envelope,
    )

    assert converged["status"] == "converged"
    assert held["status"] == "held"


def test_reason_and_warf_credentials_are_isolated_with_explicit_precedence(
    monkeypatch,
) -> None:
    credential_names = (
        "REASON_RDN_TOKEN",
        "RDN_AUTH_TOKEN",
        "REASON_CONTRIBUTION_API_KEY",
        "REASON_ORGANIZATION_CONTRIBUTION_API_KEY",
        "REASON_SHARED_CONTRIBUTION_API_KEY",
        "REASON_ORGANIZATION_API_KEY",
        "REASON_SHARED_API_KEY",
        "REASON_REGISTRY_API_KEY",
        "XPORT_API_KEY",
        "WARF_API_KEY",
    )
    for name in credential_names:
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("WARF_API_KEY", "warf-secret")
    assert RDNClient._registry_headers() == {}
    assert RDNClient._contribution_headers("organization") == {}
    assert RDNClient._warf_headers() == {
        "Authorization": "Bearer warf-secret"
    }

    monkeypatch.setenv("XPORT_API_KEY", "legacy-registry")
    monkeypatch.setenv("REASON_REGISTRY_API_KEY", "registry-secret")
    monkeypatch.setenv("REASON_CONTRIBUTION_API_KEY", "contribution-secret")
    assert RDNClient._registry_headers() == {"X-API-Key": "registry-secret"}
    assert RDNClient._contribution_headers("organization") == {
        "X-API-Key": "contribution-secret"
    }

    monkeypatch.setenv("REASON_ORGANIZATION_API_KEY", "organization-secret")
    monkeypatch.setenv("REASON_SHARED_API_KEY", "shared-secret")
    assert RDNClient._registry_headers("organization") == {
        "X-API-Key": "organization-secret"
    }
    assert RDNClient._registry_headers("shared") == {
        "X-API-Key": "shared-secret"
    }
    assert RDNClient._contribution_headers("organization") == {
        "X-API-Key": "organization-secret"
    }
    assert RDNClient._contribution_headers("shared") == {
        "X-API-Key": "shared-secret"
    }


def test_configured_warf_key_is_sent_only_by_warf_routes(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("WARF_API_KEY", "warf-secret")
    monkeypatch.setenv("REASON_RDN_TOKEN", "node-secret")
    calls = []

    class Response:
        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"status": "ok"}

    class Requests:
        @staticmethod
        def post(url, json=None, headers=None, timeout=None):
            calls.append((url, dict(headers or {})))
            return Response()

    monkeypatch.setattr("rdn.client.requests", Requests)
    client._http_post("https://warf.example/arbitrate", {"query_text": "choose"})
    client._http_post("http://127.0.0.1:8765/api/remember", {"content": "local"})

    assert calls == [
        (
            "https://warf.example/arbitrate",
            {"Authorization": "Bearer warf-secret"},
        ),
        (
            "http://127.0.0.1:8765/api/remember",
            {"Authorization": "Bearer node-secret"},
        ),
    ]


def test_org_and_shared_delivery_use_only_their_scope_credentials(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://org.example"
    client.resolvers["shared"] = "https://shared.example"
    monkeypatch.setenv("REASON_ORGANIZATION_API_KEY", "organization-secret")
    monkeypatch.setenv("REASON_SHARED_API_KEY", "shared-secret")
    monkeypatch.setenv("WARF_API_KEY", "warf-secret")
    client.contribute(
        "organization knowledge",
        reason_address=ADDRESS,
        scope="organization",
        background=False,
    )
    client.contribute(
        "shared knowledge",
        reason_address=ADDRESS,
        scope="shared",
        background=False,
    )
    calls = []

    def fake_post(url, payload, *, headers=None):
        calls.append((url, dict(headers or {})))
        return _receipt(payload)

    monkeypatch.setattr(client, "_http_post_strict", fake_post)
    assert client.flush_contributions(limit=10, now=100.0)["delivered"] == 2
    assert [url for url, _headers in calls] == [
        "https://org.example/contributions",
        "https://shared.example/contributions",
    ]
    assert calls[0][1]["X-API-Key"] == "organization-secret"
    assert calls[1][1]["X-API-Key"] == "shared-secret"
    assert calls[0][1]["Idempotency-Key"].startswith("sha256:")
    assert calls[1][1]["Idempotency-Key"].startswith("sha256:")
    assert "Authorization" not in calls[0][1]
    assert "Authorization" not in calls[1][1]


def test_direct_organization_resolution_uses_only_org_url_and_key(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://org.example"
    client.resolvers["shared"] = "https://shared.example"
    monkeypatch.setenv("REASON_ORGANIZATION_API_KEY", "organization-secret")
    monkeypatch.setenv("REASON_SHARED_API_KEY", "shared-secret")
    monkeypatch.setenv("WARF_API_KEY", "warf-secret")
    captured = {}

    def fake_get(url, params=None, *, headers=None):
        captured.update(url=url, params=params, headers=headers)
        return _registry_artifact().to_dict()

    monkeypatch.setattr(client, "_http_get_strict", fake_get)
    artifact = client.resolve(
        ADDRESS,
        source="registry",
        scope="organization",
    )

    assert artifact.content == "resolved"
    assert captured == {
        "url": "https://org.example/resolve",
        "params": {"address": ADDRESS},
        "headers": {"X-API-Key": "organization-secret"},
    }


def test_shared_key_is_never_inherited_by_uncredentialed_org_resolver(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://org.example"
    client.resolvers["shared"] = "https://shared.example"
    monkeypatch.setenv("REASON_SHARED_API_KEY", "shared-secret")
    monkeypatch.delenv("REASON_ORGANIZATION_API_KEY", raising=False)
    monkeypatch.delenv("REASON_REGISTRY_API_KEY", raising=False)
    monkeypatch.delenv("XPORT_API_KEY", raising=False)
    calls = []

    def fake_get(url, params=None, *, headers=None):
        calls.append((url, headers))
        return _registry_artifact().to_dict()

    monkeypatch.setattr(client, "_http_get_strict", fake_get)
    client.resolve(ADDRESS, source="registry", scope="organization")
    client.resolve(ADDRESS, source="registry", scope="shared")

    assert calls == [
        ("https://org.example/resolve", None),
        ("https://shared.example/resolve", {"X-API-Key": "shared-secret"}),
    ]


def test_queue_sequence_preserves_concurrent_equal_timestamp_enqueue_order(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://reason.example"
    monkeypatch.setattr("rdn.client.time.time", lambda: 50.0)
    barrier = threading.Barrier(3)
    failures = []

    def enqueue(content):
        try:
            barrier.wait(timeout=2)
            client.contribute(
                content,
                reason_address=ADDRESS,
                scope="organization",
                background=False,
            )
        except Exception as exc:  # pragma: no cover - surfaced by assertion below
            failures.append(exc)

    threads = [
        threading.Thread(target=enqueue, args=("first",)),
        threading.Thread(target=enqueue, args=("second",)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=2)
    for thread in threads:
        thread.join(timeout=2)
    assert not any(thread.is_alive() for thread in threads)
    assert failures == []

    rows = client.inspect_contributions(include_envelope=True)
    expected_ids = [row["contribution_id"] for row in rows]
    assert [row["created_at"] for row in rows] == [50.0, 50.0]
    assert [row["queue_sequence"] for row in rows] == [1, 2]

    delivered_ids = []

    def fake_post(_url, payload, **kwargs):
        delivered_ids.append(payload["contribution_id"])
        return _receipt(payload)

    monkeypatch.setattr(client, "_http_post_strict", fake_post)
    assert client.flush_contributions(limit=10, now=100.0)["delivered"] == 2
    assert delivered_ids == expected_ids


def test_shared_scope_routes_only_to_shared_resolver(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://org.example"
    client.resolvers["shared"] = "https://shared.example"
    client.contribute(
        "shared knowledge",
        reason_address=ADDRESS,
        scope="shared",
        background=False,
    )
    called = []

    def fake_post(url, payload, *, headers=None):
        called.append(url)
        return _receipt(payload, status="held", decision="hold")

    monkeypatch.setattr(client, "_http_post_strict", fake_post)
    assert client.flush_contributions(now=100.0)["delivered"] == 1
    assert called == ["https://shared.example/contributions"]


def test_flush_retries_with_bounded_backoff_then_delivers(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://reason.example"
    client.contribute(
        "retry me",
        reason_address=ADDRESS,
        scope="organization",
        background=False,
    )
    calls = []

    def flaky_post(url, payload, *, headers=None):
        calls.append(payload["contribution_id"])
        if len(calls) == 1:
            raise RDNTransportError("temporary")
        return _receipt(payload)

    monkeypatch.setattr(client, "_http_post_strict", flaky_post)
    first = client.flush_contributions(now=100.0)
    not_ready = client.flush_contributions(now=100.5)
    second = client.flush_contributions(now=101.0)

    assert first["outcomes"][0]["status"] == "retry"
    assert first["outcomes"][0]["next_attempt_at"] == 101.0
    assert not_ready["selected"] == 0
    assert second["delivered"] == 1
    row = client.inspect_contributions()[0]
    assert row["state"] == "delivered"
    assert row["attempts"] == 2


def test_failed_queue_item_can_be_explicitly_retried(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://reason.example"
    client.contribution_max_attempts = 1
    client.contribute(
        "retry after repair",
        reason_address=ADDRESS,
        scope="organization",
        background=False,
    )
    monkeypatch.setattr(
        client,
        "_http_post_strict",
        lambda *args, **kwargs: (_ for _ in ()).throw(RDNTransportError("down")),
    )
    failed = client.flush_contributions(now=100.0)
    assert failed["outcomes"][0]["status"] == "failed"

    monkeypatch.setattr(
        client,
        "_http_post_strict",
        lambda _url, payload, **kwargs: _receipt(payload),
    )
    retried = client.flush_contributions(retry_failed=True, now=101.0)
    assert retried["delivered"] == 1
    assert client.inspect_contributions()[0]["state"] == "delivered"


def test_http_413_is_terminal_and_not_selected_by_retry_failed(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://reason.example"
    client.contribute(
        "immutable oversized candidate set",
        reason_address=ADDRESS,
        scope="organization",
        background=False,
    )
    calls = []

    def reject(_url, payload, **kwargs):
        calls.append(payload["contribution_id"])
        raise RDNHTTPError(
            413,
            "reason_scoring_candidate_set_too_large",
            {"detail": "reason_scoring_candidate_set_too_large"},
        )

    monkeypatch.setattr(client, "_http_post_strict", reject)
    rejected = client.flush_contributions(now=100.0)
    retried = client.flush_contributions(retry_failed=True, now=101.0)

    assert rejected["outcomes"][0]["status"] == "rejected"
    assert rejected["outcomes"][0]["retryable"] is False
    assert retried["selected"] == 0
    assert calls == [client.inspect_contributions()[0]["contribution_id"]]
    assert client.inspect_contributions()[0]["state"] == "rejected"


def test_stale_in_flight_item_is_recovered_and_delivered(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://reason.example"
    queued = client.contribute(
        "recover me",
        reason_address=ADDRESS,
        scope="organization",
        background=False,
    )
    with client._get_conn() as conn:
        conn.execute(
            """
            UPDATE rdn_contribution_queue SET state = 'sending', updated_at = 0
            WHERE contribution_id = ?
            """,
            (queued["contribution_id"],),
        )
        conn.commit()
    monkeypatch.setattr(
        client,
        "_http_post_strict",
        lambda _url, payload, **kwargs: _receipt(payload),
    )

    result = client.flush_contributions(now=1000.0)

    assert result["delivered"] == 1
    assert client.inspect_contributions()[0]["state"] == "delivered"


def test_background_flush_is_bounded_and_scheduled_after_queue_write(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://reason.example"
    called = threading.Event()
    observations = []

    def fake_flush(*, limit=10, **kwargs):
        rows = client.inspect_contributions()
        observations.append((limit, rows[0]["state"]))
        called.set()
        return {
            "selected": 1,
            "attempted": 1,
            "delivered": 1,
            "outcomes": [{"status": "delivered"}],
            "queue": {
                "ready": 0,
                "states": {"delivered": 1},
                "next_retry_at": None,
            },
        }

    monkeypatch.setattr(client, "flush_contributions", fake_flush)
    result = client.contribute(
        "background knowledge",
        reason_address=ADDRESS,
        scope="organization",
        background=True,
    )

    assert result["background_flush_scheduled"] is True
    assert called.wait(timeout=2)
    assert observations == [(10, "pending")]


def test_background_worker_retries_transient_failure_without_another_call(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://reason.example"
    delivered = threading.Event()
    calls = []

    def flaky_post(_url, payload, **kwargs):
        calls.append(payload["contribution_id"])
        if len(calls) == 1:
            raise RDNTransportError("temporary")
        delivered.set()
        return _receipt(payload)

    monkeypatch.setattr(client, "_http_post_strict", flaky_post)
    result = client.contribute(
        "retry automatically",
        reason_address=ADDRESS,
        scope="organization",
        background=True,
    )

    assert result["background_flush_scheduled"] is True
    worker = client._background_flush_thread
    assert delivered.wait(timeout=3)
    assert worker is not None
    worker.join(timeout=3)
    assert not worker.is_alive()
    rows = client.inspect_contributions()
    assert rows[0]["state"] == "delivered"
    assert rows[0]["attempts"] == 2


def test_enqueue_during_active_background_flush_is_not_missed(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://reason.example"
    first_started = threading.Event()
    release_first = threading.Event()
    both_delivered = threading.Event()
    calls = []

    def blocking_post(_url, payload, **kwargs):
        calls.append(payload["contribution_id"])
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
        if len(calls) == 2:
            both_delivered.set()
        return _receipt(payload)

    monkeypatch.setattr(client, "_http_post_strict", blocking_post)
    first = client.contribute(
        "first background contribution",
        reason_address=ADDRESS,
        scope="organization",
        background=True,
    )
    assert first["background_flush_scheduled"] is True
    worker = client._background_flush_thread
    assert first_started.wait(timeout=2)

    second = client.contribute(
        "second while active",
        reason_address=ADDRESS,
        scope="organization",
        background=True,
    )
    assert second["background_flush_scheduled"] is False
    release_first.set()

    assert both_delivered.wait(timeout=3)
    assert worker is not None
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert [row["state"] for row in client.inspect_contributions()] == [
        "delivered",
        "delivered",
    ]


def test_unconfigured_org_rows_do_not_starve_later_shared_background_delivery(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = None
    client.resolvers["shared"] = "https://shared.example"
    for index in range(12):
        client.contribute(
            f"organization pending {index}",
            reason_address=ADDRESS,
            scope="organization",
            background=False,
        )
    shared_delivered = threading.Event()

    def shared_post(url, payload, **kwargs):
        assert url == "https://shared.example/contributions"
        assert payload["scope"] == "shared"
        shared_delivered.set()
        return _receipt(payload)

    monkeypatch.setattr(client, "_http_post_strict", shared_post)
    shared = client.contribute(
        "shared remains deliverable",
        reason_address=ADDRESS,
        scope="shared",
        background=True,
    )

    worker = client._background_flush_thread
    assert shared["background_flush_scheduled"] is True
    assert shared_delivered.wait(timeout=2)
    assert worker is not None
    worker.join(timeout=3)
    assert not worker.is_alive()
    rows = client.inspect_contributions(limit=20)
    assert [row["state"] for row in rows[:12]] == ["pending"] * 12
    assert rows[12]["state"] == "delivered"


def test_resolver_chain_short_circuits_on_local_hit(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://org.example"
    client.remember("local answer", reason_address=ADDRESS)
    monkeypatch.setattr(
        client,
        "_resolve_reason_uri_at",
        lambda *args, **kwargs: pytest.fail("remote resolver must not be called"),
    )

    result = client.resolve_chain(ADDRESS, scope="shared")

    assert result is not None
    assert result.content == "local answer"
    assert client.resolver_status()["last_resolution"]["resolved_scope"] == "local"


def test_resolver_chain_short_circuits_on_organization_hit(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://org.example"
    client.resolvers["shared"] = "https://shared.example"
    calls = []

    def fake_resolve(endpoint, address, **kwargs):
        calls.append(endpoint)
        return _registry_artifact(content="organization answer")

    monkeypatch.setattr(client, "_resolve_reason_uri_at", fake_resolve)
    result = client.resolve_chain(ADDRESS, scope="shared")

    assert result.content == "organization answer"
    assert calls == ["https://org.example"]
    assert client.resolver_status()["last_resolution"]["resolved_scope"] == "organization"


def test_resolver_chain_falls_through_org_miss_to_shared(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://org.example"
    client.resolvers["shared"] = "https://shared.example"
    calls = []

    def fake_resolve(endpoint, address, **kwargs):
        calls.append(endpoint)
        if endpoint == "https://org.example":
            raise RDNNotFoundError(404, "missing")
        return _registry_artifact(content="shared answer")

    monkeypatch.setattr(client, "_resolve_reason_uri_at", fake_resolve)
    result = client.resolve_chain(ADDRESS, scope="shared")

    assert result.content == "shared answer"
    assert calls == ["https://org.example", "https://shared.example"]


def test_resolver_chain_falls_through_unavailable_org_to_shared(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://org.example"
    client.resolvers["shared"] = "https://shared.example"

    def fake_resolve(endpoint, address, **kwargs):
        if endpoint == "https://org.example":
            raise RDNTransportError("temporarily unavailable")
        return _registry_artifact(content="shared after outage")

    monkeypatch.setattr(client, "_resolve_reason_uri_at", fake_resolve)
    result = client.resolve_chain(ADDRESS, scope="shared")

    assert result.content == "shared after outage"
    attempts = client.resolver_status()["last_resolution"]["attempts"]
    assert [attempt["outcome"] for attempt in attempts] == [
        "not_found",
        "unavailable",
        "resolved",
    ]


def test_chain_preserves_exact_pinned_resolution_through_local_version_miss(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    client.resolvers["organization"] = "https://org.example"
    client.remember("different local current", reason_address=ADDRESS)
    pinned = _registry_artifact(content="exact pinned")
    captured = {}

    def fake_resolve(endpoint, address, **kwargs):
        captured.update(endpoint=endpoint, address=address, kwargs=kwargs)
        return pinned

    monkeypatch.setattr(client, "_resolve_reason_uri_at", fake_resolve)
    result = client.resolve(
        ADDRESS,
        source="chain",
        scope="organization",
        version=pinned.version,
        bypass_cache=True,
    )

    assert result == pinned
    assert captured == {
        "endpoint": "https://org.example",
        "address": ADDRESS,
        "kwargs": {
            "version": pinned.version,
            "bypass_cache": True,
            "scope": "organization",
        },
    }


def test_resolver_chain_honors_configured_maximum_scope(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    client.default_scope = "organization"
    client.resolvers["organization"] = "https://org.example"
    client.resolvers["shared"] = "https://shared.example"
    calls = []

    def missing(endpoint, address, **kwargs):
        calls.append(endpoint)
        raise RDNNotFoundError(404, "missing")

    monkeypatch.setattr(client, "_resolve_reason_uri_at", missing)
    assert client.resolve(ADDRESS, source="chain") is None
    assert calls == ["https://org.example"]
    status = client.runtime_status()
    assert status["resolvers"]["default_scope"] == "organization"
    assert "contributions" in status


def test_cli_file_contribution_passes_exact_bytes(tmp_path, monkeypatch, capsys) -> None:
    from rdn import cli

    source = tmp_path / "artifact.xml"
    source.write_bytes(b"<root>\r\n exact </root>\r\n")
    captured = {}

    class Client:
        def __init__(self, node_url=None):
            pass

        def contribute(self, content, **kwargs):
            captured.update(content=content, kwargs=kwargs)
            return {"status": "pending"}

    class Args:
        file = str(source)
        content = None
        uri = ADDRESS
        scope = "organization"
        media_type = "application/xml"
        project = "test"
        tags = "xml,doc"
        metadata = None
        context = None
        adapter = None
        flush = False
        node = None

    monkeypatch.setattr(cli, "RDNClient", Client)
    cli.cmd_contribute(Args())

    assert captured["content"] == source.read_bytes()
    assert captured["kwargs"]["media_type"] == "application/xml"
    assert captured["kwargs"]["background"] is False
    assert json.loads(capsys.readouterr().out)["status"] == "pending"


def test_cli_doclang_prepares_file_then_uses_ordinary_contribute(
    tmp_path, monkeypatch, capsys
) -> None:
    from rdn import cli
    from rdn.doclang_adapter import DOCLANG_MEDIA_TYPE

    source = tmp_path / "runbook.dclg"
    exact = b'<doclang xmlns="https://www.doclang.ai/ns/v0" version="0.7">\r\n<text>exact</text>\r\n</doclang>'
    source.write_bytes(exact)
    captured = {}

    class Client:
        def __init__(self, node_url=None):
            pass

        def contribute(self, content, **kwargs):
            captured.update(content=content, kwargs=kwargs)
            return {"status": "pending"}

    class Args:
        file = str(source)
        content = None
        doclang = True
        doclang_validation = "structural"
        uri = ADDRESS
        scope = "organization"
        media_type = "text/plain; charset=utf-8"
        project = "test"
        tags = None
        metadata = None
        context = None
        adapter = None
        flush = False
        node = None

    monkeypatch.setattr(cli, "RDNClient", Client)
    cli.cmd_contribute(Args())

    assert captured["content"] == exact
    assert captured["kwargs"]["media_type"] == DOCLANG_MEDIA_TYPE
    assert captured["kwargs"]["adapter"]["format"] == "doclang"
    assert json.loads(capsys.readouterr().out)["status"] == "pending"


def test_optional_document_extras_remain_outside_core() -> None:
    import tomllib

    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    assert not any("doclang" in dependency for dependency in project["dependencies"])
    assert not any("docling" in dependency for dependency in project["dependencies"])
    assert project["optional-dependencies"]["doclang"] == [
        "doclang>=0.7.3,<0.8; python_version >= '3.10'"
    ]
    assert project["optional-dependencies"]["documents"] == [
        "doclang>=0.7.3,<0.8; python_version >= '3.10'",
        "docling>=2.119,<3; python_version >= '3.10'"
    ]


def test_mcp_lock_replaces_admit_with_contribute() -> None:
    import rdn.mcp.server as mcp_server
    from rdn.artifact import MCP_ADVERTISED_TOOLS

    if mcp_server.Tool is None:
        pytest.skip("optional MCP dependency is not installed")
    server = mcp_server.WARFMCPServer.__new__(mcp_server.WARFMCPServer)
    names = tuple(tool.name for tool in server._tool_schemas())

    assert names == ("remember", "recall", "resolve", "contribute", "arbitrate", "status")
    assert names == MCP_ADVERTISED_TOOLS
    assert "admit" not in names
