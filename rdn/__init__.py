"""
rdn — The Coherent reason:// Substrate

One clean, local-first package that unifies:
- Persistent handoffs and project memory with a simple API
- Explicit WARF arbitration and selected-result Registry admission
- Local artifact integrity metadata
- Clean resolution from the Reason Registry

The simplest coherent API agents and humans should reach for:

    import rdn as reason
    reason.remember("Fixed the race with prior handoff context", tags=["infra"])
    art = reason.resolve("reason://ops/ecs/failures")

This package is the open public on-ramp to the reason:// ecosystem.
"""

from __future__ import annotations

__version__ = "0.6.0"

from .artifact import (
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
    parse_reason_artifact,
    validate_reason_address,
)
from .client import (
    REASON_REGISTRY_URL,
    WARF_GATEWAY_URL,
    RDNAuthorizationError,
    RDNClient,
    RDNConflictError,
    RDNHTTPError,
    RDNNotFoundError,
    RDNRequestError,
    RDNTransportError,
    RDNUnavailableError,
)
from .contribution import (
    CONTRIBUTION_SCHEMA,
    CONTRIBUTION_SCOPES,
    CONTRIBUTION_RECEIPT_FIELDS,
    CONTRIBUTION_RECEIPT_DECISIONS,
    CONTRIBUTION_RECEIPT_STATUSES,
    ContributionEnvelope,
    contribution_id,
    parse_contribution_envelope,
    parse_contribution_receipt,
)
from .doclang_adapter import (
    DOCLANG_MEDIA_TYPE,
    DocLangAdapterError,
    DocLangDependencyError,
    PreparedDocLangContribution,
    prepare_doclang_contribution,
)
from .client import (
    REASON_XPORT_URL as REASON_XPORT_URL,
)
from .client import (
    XCHANGE_BROKER_URL as XCHANGE_BROKER_URL,
)
from .client import (
    XCHANGE_URL as XCHANGE_URL,
)
from .client import (
    XPORT_URL as XPORT_URL,
)
from .handoff.protocol import ReasonRDN
from .node.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_STORAGE_DIR,
    default_db_path,
    port_file_path,
    serve,
)
from .node.server import (
    main as node_main,
)
from .reason import (
    Reason,  # THE coherent high-level object
    ReasonClient,
    WARFClient,
    add_recent_uri,
    admit,
    contribute,
    get_recent_uris,
    harness_metrics,
    list_prefix,
    network_arbitrate,
    record_handoff,
    record_recall,
    remember,  # module-level simplest API
    resolve,
    status,
)
from .reason import (
    xchange_arbitrate as xchange_arbitrate,
)

__all__ = [
    "RDNClient",
    "ReasonRDN",
    "ReasonArtifact",
    "ArtifactValidationError",
    "parse_reason_artifact",
    "artifact_version",
    "validate_reason_address",
    "PROTOCOL_LOCK",
    "PROTOCOL_LOCK_ID",
    "PROTOCOL_LOCK_DIGEST",
    "CANONICAL_ARTIFACT_FIELDS",
    "EVENT_RECORD_SCHEMA",
    "REGISTRY_VALIDATION_METHODS",
    "MCP_ADVERTISED_TOOLS",
    "CONTRIBUTION_SCHEMA",
    "CONTRIBUTION_SCOPES",
    "CONTRIBUTION_RECEIPT_FIELDS",
    "CONTRIBUTION_RECEIPT_DECISIONS",
    "CONTRIBUTION_RECEIPT_STATUSES",
    "ContributionEnvelope",
    "contribution_id",
    "parse_contribution_envelope",
    "parse_contribution_receipt",
    "DOCLANG_MEDIA_TYPE",
    "DocLangAdapterError",
    "DocLangDependencyError",
    "PreparedDocLangContribution",
    "prepare_doclang_contribution",
    "Reason",
    "remember",
    "resolve",
    "list_prefix",
    "add_recent_uri",
    "get_recent_uris",
    "network_arbitrate",
    "contribute",
    "admit",
    "status",
    "harness_metrics",
    "record_handoff",
    "record_recall",
    "ReasonClient",
    "WARFClient",
    "WARF_GATEWAY_URL",
    "REASON_REGISTRY_URL",
    "RDNRequestError",
    "RDNTransportError",
    "RDNHTTPError",
    "RDNNotFoundError",
    "RDNConflictError",
    "RDNAuthorizationError",
    "RDNUnavailableError",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_STORAGE_DIR",
    "port_file_path",
    "default_db_path",
    "serve",
    "node_main",
]
