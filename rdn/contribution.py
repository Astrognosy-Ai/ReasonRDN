"""Format-neutral contribution envelopes for the Reason resolver network.

The envelope deliberately treats artifact content as opaque bytes.  Text is
encoded once as UTF-8; bytes are retained exactly.  ReasonRDN does not parse,
normalize, or reserialize XML, DocLang, or any other document format.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple, Union

import rfc8785

from .artifact import ArtifactValidationError, PROTOCOL_LOCK, validate_reason_address


_CONTRIBUTION_LOCK = PROTOCOL_LOCK["contribution"]
CONTRIBUTION_SCHEMA = str(_CONTRIBUTION_LOCK["schema"])
CONTRIBUTION_FIELDS: Tuple[str, ...] = tuple(
    str(value) for value in _CONTRIBUTION_LOCK["fields"]
)
CONTRIBUTION_ARTIFACT_FIELDS: Tuple[str, ...] = tuple(
    str(value) for value in _CONTRIBUTION_LOCK["artifactFields"]
)
CONTRIBUTION_SCOPES: Tuple[str, ...] = tuple(
    str(value) for value in _CONTRIBUTION_LOCK["scopes"]
)
CONTRIBUTION_NETWORK_SCOPES: Tuple[str, ...] = tuple(
    str(value) for value in _CONTRIBUTION_LOCK["networkScopes"]
)
CONTRIBUTION_CONTENT_ENCODING = str(_CONTRIBUTION_LOCK["contentEncoding"])
CONTRIBUTION_DIGEST_ALGORITHM = str(_CONTRIBUTION_LOCK["contentDigestAlgorithm"])
CONTRIBUTION_ID_PREFIX = str(_CONTRIBUTION_LOCK["idPrefix"])
CONTRIBUTION_ID_MATERIAL_FIELDS: Tuple[str, ...] = tuple(
    str(value) for value in _CONTRIBUTION_LOCK["idMaterialFields"]
)
CONTRIBUTION_NETWORK_ROUTE = str(_CONTRIBUTION_LOCK["networkRoute"])
CONTRIBUTION_IDEMPOTENCY_HEADER = str(_CONTRIBUTION_LOCK["idempotencyHeader"])
CONTRIBUTION_RECEIPT_FIELDS: Tuple[str, ...] = tuple(
    str(value) for value in _CONTRIBUTION_LOCK["receiptFields"]
)
CONTRIBUTION_RECEIPT_STATUSES: Tuple[str, ...] = tuple(
    str(value) for value in _CONTRIBUTION_LOCK["receiptStatuses"]
)
CONTRIBUTION_RECEIPT_DECISIONS: Tuple[str, ...] = tuple(
    str(value) for value in _CONTRIBUTION_LOCK["receiptDecisions"]
)
CONTRIBUTION_MEDIA_TYPE_MAX_LENGTH = int(_CONTRIBUTION_LOCK["mediaTypeMaxLength"])
CONTRIBUTION_CONTENT_MAX_BYTES = int(_CONTRIBUTION_LOCK["contentMaxBytes"])
CONTRIBUTION_CONTEXT_MAX_BYTES = int(_CONTRIBUTION_LOCK["contextMaxBytes"])
CONTRIBUTION_ADAPTER_MAX_BYTES = int(_CONTRIBUTION_LOCK["adapterMaxBytes"])


ContentInput = Union[str, bytes, bytearray, memoryview]
_SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_VERSION_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def normalize_scope(scope: Optional[str], *, default: str = "local") -> str:
    """Return one locked resolver scope."""
    value = str(scope or default).strip().lower()
    if value not in CONTRIBUTION_SCOPES:
        raise ArtifactValidationError(
            f"scope must be one of {', '.join(CONTRIBUTION_SCOPES)}"
        )
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(
            "contribution metadata must be valid RFC8785/JCS JSON"
        ) from exc


def _opaque_content_bytes(content: ContentInput) -> bytes:
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, (bytes, bytearray, memoryview)):
        return bytes(content)
    raise ArtifactValidationError("contribution content must be str or bytes-like")


def _validate_media_type(media_type: str) -> str:
    if not isinstance(media_type, str):
        raise ArtifactValidationError("media_type must be a string")
    value = media_type.strip()
    if (
        not value
        or len(value) > CONTRIBUTION_MEDIA_TYPE_MAX_LENGTH
        or "\r" in value
        or "\n" in value
    ):
        raise ArtifactValidationError(
            "media_type must be a non-empty single-line value of at most "
            f"{CONTRIBUTION_MEDIA_TYPE_MAX_LENGTH} characters"
        )
    return value


def _contribution_material(payload: Mapping[str, Any]) -> Dict[str, Any]:
    return {field: payload[field] for field in CONTRIBUTION_ID_MATERIAL_FIELDS}


def contribution_id(payload: Mapping[str, Any]) -> str:
    """Derive the idempotent contribution identity without custody time."""
    digest = hashlib.sha256(
        _canonical_json_bytes(_contribution_material(payload))
    ).hexdigest()
    return CONTRIBUTION_ID_PREFIX + digest


@dataclass(frozen=True)
class ContributionEnvelope:
    """One validated, JSON-transportable contribution envelope."""

    schema: str
    contribution_id: str
    reason_address: str
    scope: str
    artifact: Dict[str, Any]
    context: Dict[str, Any]
    adapter: Dict[str, Any]
    created_at: str

    @classmethod
    def create(
        cls,
        content: ContentInput,
        *,
        reason_address: str,
        scope: str = "local",
        media_type: str = "text/plain; charset=utf-8",
        project: str = "astrognosy",
        tags: Optional[Tuple[str, ...]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        context: Optional[Mapping[str, Any]] = None,
        adapter: Optional[Mapping[str, Any]] = None,
        created_at: Optional[str] = None,
    ) -> "ContributionEnvelope":
        address = validate_reason_address(reason_address)
        normalized_scope = normalize_scope(scope)
        normalized_media_type = _validate_media_type(media_type)
        raw = _opaque_content_bytes(content)
        if len(raw) > CONTRIBUTION_CONTENT_MAX_BYTES:
            raise ArtifactValidationError(
                f"contribution content exceeds {CONTRIBUTION_CONTENT_MAX_BYTES} bytes"
            )
        digest = hashlib.sha256(raw).hexdigest()
        artifact = {
            "media_type": normalized_media_type,
            "content_encoding": CONTRIBUTION_CONTENT_ENCODING,
            "content_base64": base64.b64encode(raw).decode("ascii"),
            "content_digest": {
                "algorithm": CONTRIBUTION_DIGEST_ALGORITHM,
                "value": digest,
            },
        }
        normalized_tags = sorted(
            {
                str(tag).strip()
                for tag in (tags or ())
                if str(tag).strip()
            }
        )
        context_data = (
            dict(context)
            if context is not None
            else {
                "project": str(project or "astrognosy").strip() or "astrognosy",
                "tags": normalized_tags,
                "metadata": dict(metadata or {}),
            }
        )
        adapter_data = dict(adapter or {})
        context_bytes = _canonical_json_bytes(context_data)
        if len(context_bytes) > CONTRIBUTION_CONTEXT_MAX_BYTES:
            raise ArtifactValidationError(
                f"context exceeds {CONTRIBUTION_CONTEXT_MAX_BYTES} canonical JSON bytes"
            )
        adapter_bytes = _canonical_json_bytes(adapter_data)
        if len(adapter_bytes) > CONTRIBUTION_ADAPTER_MAX_BYTES:
            raise ArtifactValidationError(
                f"adapter exceeds {CONTRIBUTION_ADAPTER_MAX_BYTES} canonical JSON bytes"
            )
        payload: Dict[str, Any] = {
            "schema": CONTRIBUTION_SCHEMA,
            "contribution_id": "",
            "reason_address": address,
            "scope": normalized_scope,
            "artifact": artifact,
            "context": context_data,
            "adapter": adapter_data,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        }
        payload["contribution_id"] = contribution_id(payload)
        return cls.from_dict(payload)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ContributionEnvelope":
        if not isinstance(payload, Mapping):
            raise ArtifactValidationError("contribution envelope must be a JSON object")
        data = dict(payload)
        if set(data) != set(CONTRIBUTION_FIELDS):
            missing = sorted(set(CONTRIBUTION_FIELDS) - set(data))
            extra = sorted(set(data) - set(CONTRIBUTION_FIELDS))
            raise ArtifactValidationError(
                f"contribution envelope fields do not match the protocol lock; "
                f"missing={missing}, extra={extra}"
            )
        if data.get("schema") != CONTRIBUTION_SCHEMA:
            raise ArtifactValidationError(
                f"contribution schema must be {CONTRIBUTION_SCHEMA!r}"
            )
        address = validate_reason_address(str(data.get("reason_address") or ""))
        scope = normalize_scope(str(data.get("scope") or ""))

        artifact_value = data.get("artifact")
        if not isinstance(artifact_value, Mapping):
            raise ArtifactValidationError("contribution artifact must be a JSON object")
        artifact = dict(artifact_value)
        if set(artifact) != set(CONTRIBUTION_ARTIFACT_FIELDS):
            raise ArtifactValidationError(
                "contribution artifact fields do not match the protocol lock"
            )
        media_type = _validate_media_type(artifact.get("media_type"))
        if artifact.get("content_encoding") != CONTRIBUTION_CONTENT_ENCODING:
            raise ArtifactValidationError(
                f"content_encoding must be {CONTRIBUTION_CONTENT_ENCODING!r}"
            )
        encoded = artifact.get("content_base64")
        if not isinstance(encoded, str):
            raise ArtifactValidationError("content_base64 must be a string")
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        except Exception as exc:
            raise ArtifactValidationError("content_base64 is not valid base64") from exc
        if base64.b64encode(raw).decode("ascii") != encoded:
            raise ArtifactValidationError("content_base64 must use canonical base64")
        if len(raw) > CONTRIBUTION_CONTENT_MAX_BYTES:
            raise ArtifactValidationError(
                f"contribution content exceeds {CONTRIBUTION_CONTENT_MAX_BYTES} bytes"
            )

        content_digest_value = artifact.get("content_digest")
        if not isinstance(content_digest_value, Mapping):
            raise ArtifactValidationError("content_digest must be a JSON object")
        content_digest = dict(content_digest_value)
        if set(content_digest) != {"algorithm", "value"}:
            raise ArtifactValidationError(
                "content_digest must contain exactly algorithm and value"
            )
        if content_digest.get("algorithm") != CONTRIBUTION_DIGEST_ALGORITHM:
            raise ArtifactValidationError(
                f"content_digest.algorithm must be {CONTRIBUTION_DIGEST_ALGORITHM!r}"
            )
        digest_value = content_digest.get("value")
        actual_digest = hashlib.sha256(raw).hexdigest()
        if not isinstance(digest_value, str) or not hmac.compare_digest(
            digest_value, actual_digest
        ):
            raise ArtifactValidationError(
                "content_digest.value does not match the exact contributed bytes"
            )
        artifact = {
            "media_type": media_type,
            "content_encoding": CONTRIBUTION_CONTENT_ENCODING,
            "content_base64": encoded,
            "content_digest": content_digest,
        }

        context_value = data.get("context")
        if not isinstance(context_value, Mapping):
            raise ArtifactValidationError("context must be a JSON object")
        context = dict(context_value)
        context_bytes = _canonical_json_bytes(context)
        if len(context_bytes) > CONTRIBUTION_CONTEXT_MAX_BYTES:
            raise ArtifactValidationError(
                f"context exceeds {CONTRIBUTION_CONTEXT_MAX_BYTES} canonical JSON bytes"
            )

        adapter_value = data.get("adapter")
        if not isinstance(adapter_value, Mapping):
            raise ArtifactValidationError("adapter must be a JSON object")
        adapter = dict(adapter_value)
        adapter_bytes = _canonical_json_bytes(adapter)
        if len(adapter_bytes) > CONTRIBUTION_ADAPTER_MAX_BYTES:
            raise ArtifactValidationError(
                f"adapter exceeds {CONTRIBUTION_ADAPTER_MAX_BYTES} canonical JSON bytes"
            )

        created_at = data.get("created_at")
        if not isinstance(created_at, str) or not created_at.strip():
            raise ArtifactValidationError("created_at must be a non-empty string")

        normalized = {
            "schema": CONTRIBUTION_SCHEMA,
            "contribution_id": data.get("contribution_id"),
            "reason_address": address,
            "scope": scope,
            "artifact": artifact,
            "context": context,
            "adapter": adapter,
            "created_at": created_at,
        }
        expected_id = contribution_id(normalized)
        supplied_id = normalized["contribution_id"]
        if not isinstance(supplied_id, str) or not hmac.compare_digest(
            supplied_id, expected_id
        ):
            raise ArtifactValidationError(
                "contribution_id does not match the canonical envelope identity"
            )
        return cls(
            schema=CONTRIBUTION_SCHEMA,
            contribution_id=supplied_id,
            reason_address=address,
            scope=scope,
            artifact=artifact,
            context=context,
            adapter=adapter,
            created_at=created_at,
        )

    def content_bytes(self) -> bytes:
        """Return the exact source bytes after digest verification."""
        return base64.b64decode(self.artifact["content_base64"].encode("ascii"))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "contribution_id": self.contribution_id,
            "reason_address": self.reason_address,
            "scope": self.scope,
            "artifact": {
                "media_type": self.artifact["media_type"],
                "content_encoding": self.artifact["content_encoding"],
                "content_base64": self.artifact["content_base64"],
                "content_digest": dict(self.artifact["content_digest"]),
            },
            "context": dict(self.context),
            "adapter": dict(self.adapter),
            "created_at": self.created_at,
        }


def parse_contribution_envelope(payload: Mapping[str, Any]) -> ContributionEnvelope:
    """Validate a contribution envelope against the public protocol lock."""
    return ContributionEnvelope.from_dict(payload)


def parse_contribution_receipt(
    payload: Mapping[str, Any],
    *,
    envelope: Optional[ContributionEnvelope] = None,
) -> Dict[str, Any]:
    """Validate a closed Registry receipt and optionally bind it to a request.

    A queue item is deliverable only after the Registry confirms the same
    contribution identity, reason address, and network scope.  This prevents a
    successful proxy response or a receipt for another request from clearing
    the durable retry path.
    """
    if not isinstance(payload, Mapping):
        raise ArtifactValidationError("contribution receipt must be a JSON object")
    data = dict(payload)
    if set(data) != set(CONTRIBUTION_RECEIPT_FIELDS):
        missing = sorted(set(CONTRIBUTION_RECEIPT_FIELDS) - set(data))
        extra = sorted(set(data) - set(CONTRIBUTION_RECEIPT_FIELDS))
        raise ArtifactValidationError(
            "contribution receipt fields do not match the protocol lock; "
            f"missing={missing}, extra={extra}"
        )

    contribution_value = data.get("contribution_id")
    if not isinstance(contribution_value, str) or not _VERSION_PATTERN.fullmatch(
        contribution_value
    ):
        raise ArtifactValidationError("receipt contribution_id must be sha256:<64 lowercase hex>")
    address = validate_reason_address(str(data.get("reason_address") or ""))
    scope = str(data.get("scope") or "").strip().lower()
    if scope not in CONTRIBUTION_NETWORK_SCOPES:
        raise ArtifactValidationError(
            "receipt scope must be one of " + ", ".join(CONTRIBUTION_NETWORK_SCOPES)
        )
    status = data.get("status")
    if status not in CONTRIBUTION_RECEIPT_STATUSES:
        raise ArtifactValidationError(
            "receipt status must be one of " + ", ".join(CONTRIBUTION_RECEIPT_STATUSES)
        )
    replayed = data.get("replayed")
    if not isinstance(replayed, bool):
        raise ArtifactValidationError("receipt replayed must be a boolean")
    for field in ("epoch_digest", "convergence_event_digest"):
        value = data.get(field)
        if not isinstance(value, str) or not _SHA256_HEX_PATTERN.fullmatch(value):
            raise ArtifactValidationError(
                f"receipt {field} must be 64 lowercase hexadecimal characters"
            )
    decision = data.get("decision")
    if decision not in CONTRIBUTION_RECEIPT_DECISIONS:
        raise ArtifactValidationError(
            "receipt decision must be one of "
            + ", ".join(CONTRIBUTION_RECEIPT_DECISIONS)
        )
    current_version = data.get("current_version")
    if current_version is not None and (
        not isinstance(current_version, str)
        or not _VERSION_PATTERN.fullmatch(current_version)
    ):
        raise ArtifactValidationError(
            "receipt current_version must be null or sha256:<64 lowercase hex>"
        )

    if envelope is not None:
        expected = {
            "contribution_id": envelope.contribution_id,
            "reason_address": envelope.reason_address,
            "scope": envelope.scope,
        }
        actual = {
            "contribution_id": contribution_value,
            "reason_address": address,
            "scope": scope,
        }
        for field, expected_value in expected.items():
            actual_value = actual[field]
            if not hmac.compare_digest(str(actual_value), str(expected_value)):
                raise ArtifactValidationError(
                    f"receipt {field} does not match the submitted contribution"
                )

    return {
        "contribution_id": contribution_value,
        "reason_address": address,
        "scope": scope,
        "status": status,
        "replayed": replayed,
        "epoch_digest": data["epoch_digest"],
        "convergence_event_digest": data["convergence_event_digest"],
        "decision": decision,
        "current_version": current_version,
    }
