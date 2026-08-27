"""Canonical ReasonArtifact parsing, content verification, and version identity.

``protocol-lock.json`` is the public SDK's single protocol lock.  The wire
artifact has exactly nine fields.  ``source`` is SDK resolution context and is
therefore kept on :class:`ReasonArtifact` without being serialized onto the
wire artifact.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Dict, List, Optional, Tuple

import rfc8785


def _load_protocol_lock() -> Dict[str, Any]:
    raw = resources.files("rdn").joinpath("protocol-lock.json").read_text(encoding="utf-8")
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise RuntimeError("rdn/protocol-lock.json must contain one JSON object")
    return loaded


PROTOCOL_LOCK = _load_protocol_lock()
PROTOCOL_LOCK_ID = str(PROTOCOL_LOCK["$id"])
EVENT_RECORD_SCHEMA = str(PROTOCOL_LOCK["eventRecordSchema"])
REGISTRY_VALIDATION_METHODS: Tuple[str, ...] = tuple(
    str(value) for value in PROTOCOL_LOCK["registryValidationMethods"]
)
CANONICAL_ARTIFACT_FIELDS: Tuple[str, ...] = tuple(
    str(value) for value in PROTOCOL_LOCK["artifact"]["fields"]
)
CONTENT_DIGEST_ALGORITHM = str(
    PROTOCOL_LOCK["artifact"]["contentDigestAlgorithm"]
)
STRING_ENCODING = str(PROTOCOL_LOCK["artifact"]["stringEncoding"])
STRUCTURED_ENCODING = str(PROTOCOL_LOCK["artifact"]["structuredEncoding"])
VERSION_PREFIX = str(PROTOCOL_LOCK["artifact"]["versionPrefix"])
MEDIA_TYPE_MAX_LENGTH = int(PROTOCOL_LOCK["artifact"]["mediaTypeMaxLength"])
REGISTRY_VALIDATION_MIN_ITEMS = int(
    PROTOCOL_LOCK["artifact"]["registryValidationMinItems"]
)
RESOLUTION_SOURCES: Tuple[str, ...] = tuple(PROTOCOL_LOCK["sdk"]["resolutionSources"])
DEFAULT_RESOLUTION_SOURCE = str(PROTOCOL_LOCK["sdk"]["defaultResolutionSource"])
MCP_ADVERTISED_TOOLS: Tuple[str, ...] = tuple(
    PROTOCOL_LOCK["mcp"]["advertisedTools"]
)
PROTOCOL_LOCK_DIGEST = hashlib.sha256(rfc8785.dumps(PROTOCOL_LOCK)).hexdigest()

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REASON_URI_RE = re.compile(
    r"^reason://[a-z][a-z0-9-]{0,62}/"
    r"[a-z][a-z0-9-]{0,62}/[a-z][a-z0-9-]{0,62}$"
)


class ArtifactValidationError(ValueError):
    """Raised when a ReasonArtifact violates the public protocol lock."""


def validate_reason_address(address: str) -> str:
    """Return an exact canonical reason URI or raise ``ArtifactValidationError``."""
    if not isinstance(address, str) or not _REASON_URI_RE.fullmatch(address):
        raise ArtifactValidationError(
            "address must be reason:// plus exactly three lowercase 1-63 character "
            "segments matching [a-z][a-z0-9-]*"
        )
    return address


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return rfc8785.dumps(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(
            "structured artifact content must be valid RFC8785/JCS JSON"
        ) from exc


def content_identity(content: Any) -> Tuple[str, str]:
    """Return ``(canonical_encoding, sha256_hex)`` for inline JSON content."""
    if isinstance(content, str):
        encoding = STRING_ENCODING
        canonical = content.encode("utf-8")
    else:
        if isinstance(content, float) and not math.isfinite(content):
            raise ArtifactValidationError("artifact content cannot contain NaN or infinity")
        encoding = STRUCTURED_ENCODING
        canonical = _canonical_json_bytes(content)
    return encoding, hashlib.sha256(canonical).hexdigest()


def sort_validation_records(records: Any) -> List[Dict[str, Any]]:
    """Validate and canonically order validation records for version identity."""
    if records is None:
        return []
    if not isinstance(records, list):
        raise ArtifactValidationError("validation must be a JSON array")
    prepared: List[Tuple[bytes, Dict[str, Any]]] = []
    for record in records:
        if not isinstance(record, dict):
            raise ArtifactValidationError("each validation record must be a JSON object")
        copied = dict(record)
        canonical = _canonical_json_bytes(copied)
        prepared.append((canonical, copied))
    prepared.sort(key=lambda item: item[0])
    return [record for _, record in prepared]


def artifact_version(
    *,
    address: str,
    media_type: str,
    content_digest: str,
    canonical_encoding: str,
    validation: Any,
) -> str:
    """Derive the locked version identity; ``admitted_at`` is intentionally absent."""
    validate_reason_address(address)
    if (
        not isinstance(media_type, str)
        or not media_type.strip()
        or len(media_type) > MEDIA_TYPE_MAX_LENGTH
    ):
        raise ArtifactValidationError(
            f"media_type must be a non-empty string of at most {MEDIA_TYPE_MAX_LENGTH} characters"
        )
    if not isinstance(content_digest, str) or not _SHA256_RE.fullmatch(content_digest):
        raise ArtifactValidationError(
            "content_digest must be a lowercase 64-character SHA-256 digest"
        )
    if canonical_encoding not in {STRING_ENCODING, STRUCTURED_ENCODING}:
        raise ArtifactValidationError(
            f"canonical_encoding must be {STRING_ENCODING!r} or {STRUCTURED_ENCODING!r}"
        )
    material = {
        "address": address,
        "media_type": media_type,
        "content_digest": content_digest,
        "canonical_encoding": canonical_encoding,
        "validation": sort_validation_records(validation),
    }
    return VERSION_PREFIX + hashlib.sha256(_canonical_json_bytes(material)).hexdigest()


@dataclass(frozen=True)
class ReasonArtifact(Mapping[str, Any]):
    """One verified artifact shape for local and Reason Registry resolution.

    Mapping compatibility keeps existing ``artifact["address"]`` and
    ``artifact.get(...)`` callers working.  Use :meth:`to_dict` for the exact
    nine-field wire representation.  ``source`` is local SDK context.
    """

    address: str
    version: str
    media_type: str
    content: Any
    content_digest: str
    content_digest_algorithm: str
    canonical_encoding: str
    validation: Tuple[Dict[str, Any], ...]
    admitted_at: Optional[str]
    source: str = field(compare=False)
    compatibility: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        validate_reason_address(self.address)
        if self.source not in RESOLUTION_SOURCES:
            raise ArtifactValidationError(
                f"source must be one of {', '.join(RESOLUTION_SOURCES)}"
            )
        if self.content_digest_algorithm != CONTENT_DIGEST_ALGORITHM:
            raise ArtifactValidationError(
                f"content_digest_algorithm must be {CONTENT_DIGEST_ALGORITHM!r}"
            )
        if not _SHA256_RE.fullmatch(self.content_digest):
            raise ArtifactValidationError(
                "content_digest must be a lowercase 64-character SHA-256 digest"
            )
        if not _VERSION_RE.fullmatch(self.version):
            raise ArtifactValidationError("version must be sha256:<64 lowercase hex>")
        if not self.verify_content_digest():
            raise ArtifactValidationError("content_digest does not match inline content")
        if not self.verify_version():
            raise ArtifactValidationError("version does not match canonical artifact identity")

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        source: str = DEFAULT_RESOLUTION_SOURCE,
    ) -> "ReasonArtifact":
        """Parse canonical or retained local data into the locked artifact shape."""
        if not isinstance(payload, Mapping):
            raise ArtifactValidationError("artifact payload must be a mapping")

        data = dict(payload)
        if source == "registry":
            supplied = set(data)
            expected = set(CANONICAL_ARTIFACT_FIELDS)
            if supplied != expected:
                missing = sorted(expected - supplied)
                extra = sorted(supplied - expected)
                raise ArtifactValidationError(
                    f"registry artifact must contain exactly the nine locked fields; "
                    f"missing={missing}, extra={extra}"
                )
            admitted_at = data.get("admitted_at")
            if not isinstance(admitted_at, str) or not admitted_at.strip():
                raise ArtifactValidationError(
                    "registry artifact admitted_at must be a non-empty string"
                )
            registry_validation = data.get("validation")
            if (
                not isinstance(registry_validation, list)
                or len(registry_validation) < REGISTRY_VALIDATION_MIN_ITEMS
            ):
                raise ArtifactValidationError(
                    "registry artifact must include at least one validation record"
                )
            if registry_validation != sort_validation_records(registry_validation):
                raise ArtifactValidationError(
                    "registry validation records are not in canonical JCS byte order"
                )
            if any(
                not isinstance(record, dict)
                or record.get("method") not in REGISTRY_VALIDATION_METHODS
                for record in registry_validation
            ):
                raise ArtifactValidationError(
                    "registry validation method must be one of "
                    + ", ".join(repr(value) for value in REGISTRY_VALIDATION_METHODS)
                )

        meta_value = data.get("meta")
        meta = dict(meta_value) if isinstance(meta_value, Mapping) else {}
        address = data.get("address") or data.get("uri") or meta.get("reason_address")
        address = validate_reason_address(str(address or ""))

        content = data["content"] if "content" in data else meta.get("content")
        computed_encoding, computed_digest = content_identity(content)
        encoding = str(data.get("canonical_encoding") or computed_encoding)
        digest = str(data.get("content_digest") or computed_digest)
        algorithm = str(
            data.get("content_digest_algorithm") or CONTENT_DIGEST_ALGORITHM
        )
        validation = sort_validation_records(data.get("validation", []))
        media_type = str(
            data.get("media_type")
            or ("text/plain; charset=utf-8" if isinstance(content, str) else "application/json")
        )
        expected_version = artifact_version(
            address=address,
            media_type=media_type,
            content_digest=digest,
            canonical_encoding=encoding,
            validation=validation,
        )
        version = str(data.get("version") or expected_version)
        admitted_at_value = (
            data.get("admitted_at")
            if "admitted_at" in data
            else data.get("deposited_at", meta.get("stored_at"))
        )
        admitted_at = None if admitted_at_value is None else str(admitted_at_value)

        compatibility = {
            key: value
            for key, value in data.items()
            if key not in CANONICAL_ARTIFACT_FIELDS
        }
        if meta:
            compatibility.setdefault("meta", meta)
        compatibility.setdefault("uri", address)

        return cls(
            address=address,
            version=version,
            media_type=media_type,
            content=content,
            content_digest=digest,
            content_digest_algorithm=algorithm,
            canonical_encoding=encoding,
            validation=tuple(validation),
            admitted_at=admitted_at,
            source=source,
            compatibility=compatibility,
        )

    def verify_content_digest(self) -> bool:
        """Verify exact UTF-8 string bytes or RFC8785/JCS structured JSON bytes."""
        if self.content_digest_algorithm != CONTENT_DIGEST_ALGORITHM:
            return False
        try:
            encoding, digest = content_identity(self.content)
        except ArtifactValidationError:
            return False
        return encoding == self.canonical_encoding and hmac.compare_digest(
            digest, self.content_digest
        )

    def verify_version(self) -> bool:
        """Verify the immutable version identity independently of admission time."""
        try:
            expected = artifact_version(
                address=self.address,
                media_type=self.media_type,
                content_digest=self.content_digest,
                canonical_encoding=self.canonical_encoding,
                validation=list(self.validation),
            )
        except ArtifactValidationError:
            return False
        return hmac.compare_digest(expected, self.version)

    def to_dict(self) -> Dict[str, Any]:
        """Return the exact nine-field canonical wire artifact."""
        return {
            "address": self.address,
            "version": self.version,
            "media_type": self.media_type,
            "content": self.content,
            "content_digest": self.content_digest,
            "content_digest_algorithm": self.content_digest_algorithm,
            "canonical_encoding": self.canonical_encoding,
            "validation": [dict(record) for record in self.validation],
            "admitted_at": self.admitted_at,
        }

    def resolution_dict(self) -> Dict[str, Any]:
        """Return the wire artifact plus the SDK-only resolution source."""
        return {"source": self.source, "artifact": self.to_dict()}

    def __getitem__(self, key: str) -> Any:
        if key == "source":
            return self.source
        if key in CANONICAL_ARTIFACT_FIELDS:
            return self.to_dict()[key]
        if key in self.compatibility:
            return self.compatibility[key]
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(CANONICAL_ARTIFACT_FIELDS)

    def __len__(self) -> int:
        return len(CANONICAL_ARTIFACT_FIELDS)


def parse_reason_artifact(
    payload: Mapping[str, Any],
    *,
    source: str = DEFAULT_RESOLUTION_SOURCE,
) -> ReasonArtifact:
    """Public parser shared by all SDK, CLI, and MCP resolution paths."""
    return ReasonArtifact.from_dict(payload, source=source)
