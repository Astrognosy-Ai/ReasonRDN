"""
rdn.client - Unified memory client for ReasonRDN.

This package is the public local-first on-ramp for reason:// memory. It works
entirely offline with a local SQLite-backed node. Explicit calls can arbitrate
competing packages through the WARF Gateway, resolve through configured layers,
and queue organization or shared contributions durably. Local memory and local
contributions never trigger network writes. Low-level admission remains for
compatibility.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

import rfc8785

from .addressing import project_address as _project_address
from .artifact import (
    EVENT_RECORD_SCHEMA,
    PROTOCOL_LOCK,
    ArtifactValidationError,
    ReasonArtifact,
    parse_reason_artifact,
    validate_reason_address,
)
from .config import env_flag
from .contribution import (
    CONTRIBUTION_IDEMPOTENCY_HEADER,
    CONTRIBUTION_NETWORK_SCOPES,
    CONTRIBUTION_NETWORK_ROUTE,
    ContentInput,
    ContributionEnvelope,
    normalize_scope,
    parse_contribution_envelope,
    parse_contribution_receipt,
)

try:
    import requests  # Preferred for robustness
except ImportError:
    requests = None

try:
    from urllib import error as urllib_error
    from urllib import request as urllib_request
    from urllib.parse import quote as url_quote
except Exception:
    urllib_request = None  # type: ignore
    urllib_error = None  # type: ignore

    def url_quote(value):  # type: ignore
        return value

logger = logging.getLogger("rdn.client")

DEFAULT_DB_DIR = Path.home() / ".reason-rdn" / "private-node"
DEFAULT_DB_PATH = DEFAULT_DB_DIR / "warf-node.db"
PORT_FILE = DEFAULT_DB_DIR / "private-node.port"
CONFIG_FILE = Path.home() / ".reason-ecosystem.cfg"

_RESOLVER_SCOPE_ORDER = tuple(
    str(value) for value in PROTOCOL_LOCK["sdk"]["resolverChainOrder"]
)
_CONTRIBUTION_STALE_SENDING_SECONDS = 300.0

# Public front door for deposits, shares, and arbitration.
WARF_GATEWAY_URL = "https://warf.astrognosy.com"
XCHANGE_BROKER_URL = WARF_GATEWAY_URL

# Reason Registry / reason:// public resolver.
# Primary: https://reason.astrognosy.com and https://xport.astrognosy.com
# Use for resolve("reason://...") to get the current best-known reasoning.
REASON_REGISTRY_URL = "https://reason.astrognosy.com"
XPORT_URL = REASON_REGISTRY_URL

# Backwards-compatible alias for the broker (the main place you send deposits/shares/arbitration).
XCHANGE_URL = XCHANGE_BROKER_URL

# Convenience alias
REASON_XPORT_URL = XPORT_URL

# Environment variable fallbacks for node URL (order of precedence)
ENV_NODE_KEYS = ("RDN_NODE_URL", "REASON_NODE_URL", "WARF_NODE_URL", "XCHANGE_NODE_URL")

class RDNRequestError(RuntimeError):
    """Base error for an explicit Reason Registry or WARF request."""


class RDNTransportError(RDNRequestError):
    """The selected service could not be reached."""


class RDNHTTPError(RDNRequestError):
    """The selected service returned a non-success HTTP response."""

    def __init__(self, status_code: int, message: str, payload: Any = None):
        super().__init__(f"HTTP {status_code}: {message}")
        self.status_code = int(status_code)
        self.payload = payload


class RDNNotFoundError(RDNHTTPError):
    """A reason address or version is unknown to the selected Registry."""


class RDNConflictError(RDNHTTPError):
    """A version or current-pointer precondition did not match."""


class RDNAuthorizationError(RDNHTTPError):
    """The selected Registry rejected authorization."""


class RDNUnavailableError(RDNHTTPError):
    """The selected service is temporarily unavailable."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_payload(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _first_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


class RDNClient:
    """
    Unified client for depositing and retrieving ReasonRDN handoff artifacts.

    Prefers a running HTTP node (local embedded or remote). Falls back to local SQLite.
    Both paths produce artifacts with identical shape and integrity fields.
    """

    def __init__(
        self,
        node_url: Optional[str] = None,
        db_path: Optional[Union[str, Path]] = None,
        timeout: float = 8.0,
        mirror_local: bool = True,
    ):
        self.timeout = timeout
        self.mirror_local = mirror_local
        self.logger = logger

        # Storage
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._ensure_local_schema()
        config = self._load_config()
        resolver_config_value = config.get("resolvers")
        resolver_config = (
            dict(resolver_config_value)
            if isinstance(resolver_config_value, Mapping)
            else {}
        )
        self.default_scope = normalize_scope(
            _first_env(
                "REASON_RESOLUTION_SCOPE",
                "RDN_RESOLUTION_SCOPE",
                "REASON_SCOPE",
                "RDN_SCOPE",
            )
            or config.get("resolution_scope")
            or config.get("scope")
            or "local"
        )
        self.resolvers: Dict[str, Optional[str]] = {
            "organization": (
                _first_env(
                    "REASON_ORGANIZATION_RESOLVER",
                    "RDN_ORGANIZATION_RESOLVER",
                )
                or resolver_config.get("organization")
                or config.get("organization_resolver")
                or None
            ),
            "shared": (
                _first_env("REASON_SHARED_RESOLVER", "RDN_SHARED_RESOLVER")
                or resolver_config.get("shared")
                or config.get("shared_resolver")
                or None
            ),
        }
        self.resolvers = {
            key: str(value).rstrip("/") if value else None
            for key, value in self.resolvers.items()
        }
        try:
            configured_attempts = int(config.get("contribution_max_attempts", 8))
        except (TypeError, ValueError):
            configured_attempts = 8
        self.contribution_max_attempts = max(1, min(32, configured_attempts))
        self._background_flush_lock = threading.Lock()
        self._background_flush_wakeup = threading.Event()
        self._background_flush_thread: Optional[threading.Thread] = None
        self._last_resolution: Optional[Dict[str, Any]] = None

        # Node discovery priority:
        # 1. Explicit node_url param
        # 2. Network mode -> configure WARF Gateway plus Reason Registry while
        #    preserving the local node/storage path.
        # 3. Env vars
        # 4. ~/.reason-ecosystem.cfg
        # 5. Local port file
        # 6. Pure local
        self.node_url: Optional[str] = node_url
        self.broker_url: Optional[str] = None
        self.xport_url: Optional[str] = None

        network_env_keys = (
            "REASON_USE_NETWORK",
            "REASON_USE_XCHANGE",
            "USE_WARF_XCHANGE",
            "REASON_XCHANGE",
            "XCHANGE",
        )
        network_env_is_explicit = any(name in os.environ for name in network_env_keys)
        configured_node = str(config.get("node_url") or "").rstrip("/")
        configured_network = bool(config.get("network_enabled")) or (
            configured_node == WARF_GATEWAY_URL
        )

        if not self.node_url:
            use_network = (
                env_flag(*network_env_keys)
                if network_env_is_explicit
                else configured_network
            )
            if use_network:
                self.broker_url = str(
                    config.get("gateway_url") or XCHANGE_BROKER_URL
                ).rstrip("/")
                self.xport_url = str(
                    config.get("registry_url") or XPORT_URL
                ).rstrip("/")

        if not self.node_url:
            for key in ENV_NODE_KEYS:
                val = os.environ.get(key)
                if val:
                    self.node_url = val.rstrip("/")
                    break

        if not self.node_url and configured_node != WARF_GATEWAY_URL:
            self.node_url = configured_node or None

        if not self.node_url:
            self.node_url = self._discover_local_node_via_port()

        # If we have explicit broker/xport in config later, they can override. For now single node_url is the main path.

        self.available = False
        if self.node_url:
            self.available = self._check_health()

        # Shared is a known public resolver, but it is contacted only when the
        # caller or configuration selects shared scope.  Local remains default.
        self.resolvers["shared"] = (
            self.resolvers.get("shared")
            or self.xport_url
            or REASON_REGISTRY_URL
        )

        # For GUI / advanced features
        self._last_heartbeat_cache: Dict[str, Any] = {}

    # ---------------- Discovery & Health ----------------

    def _discover_local_node_via_port(self) -> Optional[str]:
        try:
            if PORT_FILE.exists():
                port = int(PORT_FILE.read_text(encoding="utf-8").strip())
                if port:
                    return f"http://127.0.0.1:{port}"
        except Exception:
            pass
        return None

    def _load_config(self) -> Dict[str, Any]:
        """Load the installer config while keeping local and network routes distinct."""
        try:
            if CONFIG_FILE.exists():
                cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(cfg, dict):
                    return cfg
        except Exception:
            pass
        return {}

    def _check_health(self) -> bool:
        if not self.node_url:
            return False
        for path in ("/health", "/api/health"):
            try:
                if requests:
                    r = requests.get(f"{self.node_url}{path}", timeout=self.timeout)
                    if r.ok:
                        data = r.json()
                        return data.get("status") in {"ok", "healthy"}
                elif urllib_request:
                    with urllib_request.urlopen(
                        f"{self.node_url}{path}", timeout=self.timeout
                    ) as resp:
                        data = json.loads(resp.read().decode("utf-8"))
                        if data.get("status") in {"ok", "healthy"}:
                            return True
            except Exception:
                continue
        return False

    def refresh_availability(self) -> bool:
        """Re-probe the node. Useful after starting a private node."""
        if self.node_url:
            self.available = self._check_health()
        else:
            # Try discovery again (node may have just started)
            self.node_url = self._discover_local_node_via_port()
            self.available = self._check_health() if self.node_url else False
        return self.available

    # ---------------- Local Schema (matches node/server.py exactly) ----------------

    def _ensure_local_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS warf_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    address TEXT NOT NULL UNIQUE,
                    domain TEXT NOT NULL,
                    category TEXT NOT NULL,
                    task TEXT NOT NULL,
                    deposited_at TEXT NOT NULL,
                    audit_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_warf_domain_time ON warf_artifacts(domain, deposited_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rdn_contribution_queue (
                    contribution_id TEXT PRIMARY KEY,
                    queue_sequence INTEGER,
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
            queue_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(rdn_contribution_queue)"
                ).fetchall()
            }
            if "queue_sequence" not in queue_columns:
                conn.execute(
                    "ALTER TABLE rdn_contribution_queue ADD COLUMN queue_sequence INTEGER"
                )
            conn.execute(
                """
                UPDATE rdn_contribution_queue
                SET queue_sequence = rowid
                WHERE queue_sequence IS NULL
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_rdn_contribution_sequence
                ON rdn_contribution_queue(queue_sequence)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rdn_contribution_queue_meta (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    next_sequence INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO rdn_contribution_queue_meta
                (singleton, next_sequence) VALUES (1, 1)
                """
            )
            conn.execute(
                """
                UPDATE rdn_contribution_queue_meta
                SET next_sequence = MAX(
                    next_sequence,
                    COALESCE(
                        (SELECT MAX(queue_sequence) + 1 FROM rdn_contribution_queue),
                        1
                    )
                )
                WHERE singleton = 1
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_rdn_contribution_ready_sequence
                ON rdn_contribution_queue(state, next_attempt_at, queue_sequence)
                """
            )
            conn.commit()
        finally:
            conn.close()

    def _get_conn(self):
        return sqlite3.connect(str(self.db_path), timeout=30.0)

    # ---------------- HTTP helpers ----------------

    def _http_get(self, url: str, params: Optional[Dict] = None) -> Optional[Dict]:
        try:
            headers = self._auth_headers()
            if requests:
                r = requests.get(url, params=params, headers=headers, timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            elif urllib_request:
                if params:
                    q = "&".join(f"{k}={url_quote(str(v))}" for k, v in params.items())
                    url = f"{url}?{q}"
                req = urllib_request.Request(url)
                for k, v in (headers or {}).items():
                    req.add_header(k, v)
                with urllib_request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            self.logger.debug("HTTP GET failed: %s", exc)
            return None

    def _http_post(self, url: str, payload: Dict[str, Any]) -> Optional[Dict]:
        try:
            route = url.split("?", 1)[0].rstrip("/")
            request_headers = (
                self._warf_headers()
                if route.endswith(("/arbitrate", "/share"))
                else self._auth_headers()
            )
            if requests:
                r = requests.post(
                    url, json=payload, headers=request_headers, timeout=self.timeout
                )
                r.raise_for_status()
                return r.json()
            elif urllib_request:
                data = json.dumps(payload).encode("utf-8")
                req = urllib_request.Request(url, data=data, method="POST")
                req.add_header("Content-Type", "application/json")
                for k, v in request_headers.items():
                    req.add_header(k, v)
                with urllib_request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            self.logger.debug("HTTP POST failed: %s", exc)
            return None

    @staticmethod
    def _decode_response_payload(response: Any) -> Any:
        try:
            return response.json()
        except Exception:
            text_value = getattr(response, "text", None)
            return text_value if text_value not in {None, ""} else None

    @staticmethod
    def _http_error(status_code: int, payload: Any) -> RDNHTTPError:
        if isinstance(payload, dict):
            message = payload.get("detail") or payload.get("message") or payload.get("error")
        else:
            message = payload
        message = str(message or "request failed")
        error_types = {
            401: RDNAuthorizationError,
            403: RDNAuthorizationError,
            404: RDNNotFoundError,
            409: RDNConflictError,
            503: RDNUnavailableError,
        }
        error_type = error_types.get(int(status_code), RDNHTTPError)
        return error_type(int(status_code), message, payload)

    def _http_get_strict(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """GET JSON without collapsing HTTP status or transport failures."""
        request_headers = {} if headers is None else dict(headers)
        if requests:
            try:
                response = requests.get(
                    url, params=params, headers=request_headers, timeout=self.timeout
                )
            except Exception as exc:
                raise RDNTransportError(f"GET {url} failed: {exc}") from exc
            payload = self._decode_response_payload(response)
            status_code = int(getattr(response, "status_code", 0) or 0)
            if not 200 <= status_code < 300:
                raise self._http_error(status_code, payload)
            if not isinstance(payload, dict):
                raise RDNTransportError(f"GET {url} returned a non-object JSON response")
            return payload

        if urllib_request:
            from urllib.parse import urlencode

            if params:
                url = f"{url}?{urlencode(params)}"
            request = urllib_request.Request(url, headers=request_headers)
            try:
                with urllib_request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib_error.HTTPError as exc:  # type: ignore[union-attr]
                try:
                    error_payload = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    error_payload = None
                raise self._http_error(int(exc.code), error_payload) from exc
            except Exception as exc:
                raise RDNTransportError(f"GET {url} failed: {exc}") from exc
            if not isinstance(payload, dict):
                raise RDNTransportError(f"GET {url} returned a non-object JSON response")
            return payload
        raise RDNTransportError("No HTTP client is available")

    def _http_post_strict(
        self,
        url: str,
        payload: Dict[str, Any],
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """POST JSON without collapsing HTTP status or transport failures."""
        request_headers = self._auth_headers() if headers is None else dict(headers)
        if requests:
            try:
                response = requests.post(
                    url, json=payload, headers=request_headers, timeout=self.timeout
                )
            except Exception as exc:
                raise RDNTransportError(f"POST {url} failed: {exc}") from exc
            response_payload = self._decode_response_payload(response)
            status_code = int(getattr(response, "status_code", 0) or 0)
            if not 200 <= status_code < 300:
                raise self._http_error(status_code, response_payload)
            if not isinstance(response_payload, dict):
                raise RDNTransportError(f"POST {url} returned a non-object JSON response")
            return response_payload

        if urllib_request:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request = urllib_request.Request(
                url, data=data, method="POST", headers=request_headers
            )
            request.add_header("Content-Type", "application/json")
            try:
                with urllib_request.urlopen(request, timeout=self.timeout) as response:
                    response_payload = json.loads(response.read().decode("utf-8"))
            except urllib_error.HTTPError as exc:  # type: ignore[union-attr]
                try:
                    error_payload = json.loads(exc.read().decode("utf-8"))
                except Exception:
                    error_payload = None
                raise self._http_error(int(exc.code), error_payload) from exc
            except Exception as exc:
                raise RDNTransportError(f"POST {url} failed: {exc}") from exc
            if not isinstance(response_payload, dict):
                raise RDNTransportError(f"POST {url} returned a non-object JSON response")
            return response_payload
        raise RDNTransportError("No HTTP client is available")

    def _auth_headers(self) -> Dict[str, str]:
        """Return only the private-node bearer credential.

        Reason Registry, contribution intake, and WARF are separate service
        boundaries and therefore use their own header builders below.
        """
        token = _first_env("REASON_RDN_TOKEN", "RDN_AUTH_TOKEN")
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    @staticmethod
    def _registry_headers(scope: str = "shared") -> Dict[str, str]:
        """Return one scope's resolver credential, never a WARF secret."""
        normalized_scope = normalize_scope(scope, default="shared")
        if normalized_scope == "local":
            return {}
        scope_name = normalized_scope.upper()
        token = _first_env(
            f"REASON_{scope_name}_REGISTRY_API_KEY",
            f"REASON_{scope_name}_API_KEY",
            "REASON_REGISTRY_API_KEY",
            "XPORT_API_KEY",
        )
        return {"X-API-Key": token} if token else {}

    @staticmethod
    def _contribution_headers(scope: str) -> Dict[str, str]:
        """Return one scope's network-intake credential in explicit precedence."""
        normalized_scope = normalize_scope(scope)
        if normalized_scope == "local":
            return {}
        scope_name = normalized_scope.upper()
        token = _first_env(
            f"REASON_{scope_name}_CONTRIBUTION_API_KEY",
            f"REASON_{scope_name}_API_KEY",
            "REASON_CONTRIBUTION_API_KEY",
            "REASON_REGISTRY_API_KEY",
            "XPORT_API_KEY",
        )
        return {"X-API-Key": token} if token else {}

    @staticmethod
    def _warf_headers() -> Dict[str, str]:
        """Return only a WARF Gateway bearer credential."""
        token = _first_env("WARF_API_KEY")
        return {"Authorization": f"Bearer {token}"} if token else {}

    @staticmethod
    def _admission_headers() -> Dict[str, str]:
        """Use the Registry write credential without conflating WARF bearer auth."""
        token = _first_env("REASON_REGISTRY_API_KEY", "XPORT_API_KEY")
        return {"X-API-Key": token} if token else {}

    # ------------------------------------------------------------------
    # WARF / reason:// helpers (public Gateway and Reason Registry bridge)
    # ------------------------------------------------------------------

    def resolve_reason_uri(
        self,
        uri: str,
        bypass_cache: bool = False,
        version: Optional[str] = None,
    ) -> ReasonArtifact:
        """Compatibility alias for explicit Reason Registry resolution."""
        return self.resolve_from_registry(
            uri, bypass_cache=bypass_cache, version=version
        )

    def _resolve_reason_uri_at(
        self,
        target: str,
        uri: str,
        bypass_cache: bool = False,
        version: Optional[str] = None,
        scope: str = "shared",
    ) -> ReasonArtifact:
        """Resolve and verify one artifact at one exact Registry endpoint."""
        canonical_address = validate_reason_address(uri)
        params: Dict[str, Any] = {"address": canonical_address}
        if version is not None:
            params["version"] = version
        if bypass_cache:
            params["bypass_cache"] = True
        resolve_url = f"{target.rstrip('/')}/resolve"
        headers = self._registry_headers(scope)
        data = (
            self._http_get_strict(resolve_url, params=params, headers=headers)
            if headers
            else self._http_get_strict(resolve_url, params=params)
        )
        artifact = parse_reason_artifact(data, source="registry")
        if artifact.address != canonical_address:
            raise ArtifactValidationError(
                "Registry returned an artifact for a different reason address"
            )
        if version is not None and artifact.version != version:
            raise ArtifactValidationError(
                "Registry returned a different artifact version than requested"
            )
        return artifact

    def share_to_network(
        self,
        content: str,
        uri: Optional[str] = None,
        tags: Optional[List[str]] = None,
        project: str = "astrognosy",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Deprecated 0.5 compatibility call for the legacy single-handoff route.

        New code uses :meth:`network_arbitrate` followed by :meth:`admit`.
        This method is not advertised by the CLI or MCP surface.
        """
        target = self.broker_url or WARF_GATEWAY_URL
        details = dict(meta or {})
        payload: Dict[str, Any] = {
            "query_text": details.get("query_text") or "Retain this agent handoff for reuse.",
            "agent_id": project,
            "answer_text": content,
            "corpus": details.get("corpus") or [],
        }
        if uri:
            payload["reason_address"] = uri
        result = self._http_post(f"{target.rstrip('/')}/share", payload)
        return result or {
            "status": "unavailable",
            "target": target,
            "route": "/share",
            "local_copy_retained": False,
        }

    def share_to_xchange(
        self,
        content: str,
        uri: Optional[str] = None,
        tags: Optional[List[str]] = None,
        project: str = "astrognosy",
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Deprecated 0.5 compatibility alias for :meth:`share_to_network`."""
        return self.share_to_network(
            content=content,
            uri=uri,
            tags=tags,
            project=project,
            meta=meta,
        )

    def network_arbitrate(
        self,
        query_text: str,
        packages: List[Dict[str, Any]],
        reason_address: Optional[str] = None,
        query_id: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """Submit competing packages to the public WARF Gateway."""
        if len(packages) < 2:
            raise ValueError(
                "WARF arbitration requires at least two submissions; "
                "retain one handoff locally until it has a competing arbitration package"
            )
        target = self.broker_url or WARF_GATEWAY_URL
        semantic_request: Dict[str, Any] = {
            "query_text": query_text,
            "packages": packages,
        }
        if reason_address:
            semantic_request["reason_address"] = reason_address

        reserved = {"query_id", "query_text", "packages", "reason_address"}
        semantic_request.update(
            {
                key: value
                for key, value in kwargs.items()
                if key not in reserved and value is not None
            }
        )
        stable_query_id = query_id or "rdn-" + hashlib.sha256(
            json.dumps(
                semantic_request,
                sort_keys=True,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        payload: Dict[str, Any] = {
            "query_id": stable_query_id,
            **semantic_request,
        }

        result = self._http_post(f"{target.rstrip('/')}/arbitrate", payload)
        return result or {
            "status": "unavailable",
            "target": target,
            "route": "/arbitrate",
            "query_id": stable_query_id,
        }

    def admit(
        self,
        artifact: Union[ReasonArtifact, Mapping[str, Any]],
        arbitration: Mapping[str, Any],
        *,
        expected_current_version: Optional[str] = None,
    ) -> ReasonArtifact:
        """Explicitly request arbitration-backed Reason Registry admission.

        Remembering, resolving, and arbitrating never call this method.  The
        caller supplies the exact selected Gateway event and its audit hash.
        """
        candidate = (
            artifact
            if isinstance(artifact, ReasonArtifact)
            else parse_reason_artifact(artifact, source="local")
        )
        if not isinstance(arbitration, Mapping):
            raise ArtifactValidationError("arbitration must be a JSON object")
        arbitration_data = dict(arbitration)
        required_arbitration_fields = {
            "query_id",
            "winner_submission_id",
            "event_record",
            "audit_hash",
        }
        if set(arbitration_data) != required_arbitration_fields:
            raise ArtifactValidationError(
                "arbitration must contain exactly query_id, winner_submission_id, "
                "event_record, and audit_hash"
            )
        query_id = arbitration_data.get("query_id")
        winner_submission_id = arbitration_data.get("winner_submission_id")
        event_record = arbitration_data.get("event_record")
        audit_hash = arbitration_data.get("audit_hash")
        if not isinstance(query_id, str) or not query_id:
            raise ArtifactValidationError("arbitration.query_id must be a non-empty string")
        if not isinstance(winner_submission_id, str) or not winner_submission_id:
            raise ArtifactValidationError(
                "arbitration.winner_submission_id must be a non-empty string"
            )
        if not isinstance(event_record, dict):
            raise ArtifactValidationError("arbitration.event_record must be a JSON object")
        if event_record.get("schema") != EVENT_RECORD_SCHEMA:
            raise ArtifactValidationError(
                f"arbitration.event_record.schema must be {EVENT_RECORD_SCHEMA!r}"
            )
        if event_record.get("query_id") != query_id:
            raise ArtifactValidationError(
                "arbitration.query_id must match event_record.query_id"
            )
        if event_record.get("winner") != winner_submission_id:
            raise ArtifactValidationError(
                "arbitration.winner_submission_id must match event_record.winner"
            )
        if not isinstance(audit_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", audit_hash):
            raise ArtifactValidationError(
                "arbitration.audit_hash must be a lowercase 64-character SHA-256 digest"
            )
        computed_audit = hashlib.sha256(rfc8785.dumps(event_record)).hexdigest()
        if not hmac.compare_digest(computed_audit, audit_hash):
            raise ArtifactValidationError(
                "arbitration.audit_hash does not match the canonical event record"
            )

        canonical = candidate.to_dict()
        artifact_request = {
            key: canonical[key]
            for key in (
                "address",
                "media_type",
                "content",
                "content_digest",
                "content_digest_algorithm",
                "canonical_encoding",
            )
        }
        payload: Dict[str, Any] = {
            "artifact": artifact_request,
            "arbitration": arbitration_data,
        }
        if expected_current_version is not None and not re.fullmatch(
            r"sha256:[0-9a-f]{64}", str(expected_current_version)
        ):
            raise ArtifactValidationError(
                "expected_current_version must be null or sha256:<64 lowercase hex>"
            )
        payload["expected_current_version"] = expected_current_version

        target = self.xport_url or REASON_REGISTRY_URL
        response = self._http_post_strict(
            f"{target.rstrip('/')}/admissions",
            payload,
            headers=self._admission_headers(),
        )
        admitted = parse_reason_artifact(response, source="registry")
        if admitted.address != candidate.address:
            raise ArtifactValidationError(
                "Registry admitted a different reason address than requested"
            )
        if admitted.content_digest != candidate.content_digest:
            raise ArtifactValidationError(
                "Registry admitted different content than requested"
            )
        return admitted

    def xchange_arbitrate(
        self,
        query_text: str,
        packages: List[Dict[str, Any]],
        reason_address: Optional[str] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Compatibility alias for :meth:`network_arbitrate`."""
        return self.network_arbitrate(
            query_text=query_text,
            packages=packages,
            reason_address=reason_address,
            **kwargs,
        )

    def resolve_from_registry(
        self,
        uri: str,
        *,
        scope: Optional[str] = None,
        **kwargs,
    ) -> ReasonArtifact:
        """Resolve through one selected network layer with its own credential.

        An explicitly configured organization scope never falls through to the
        public shared Registry.  For 0.5 compatibility, an explicit Registry
        read with no network default still selects the shared reference layer.
        """
        selected_scope = normalize_scope(scope, default=self.default_scope)
        if selected_scope == "local":
            selected_scope = "shared"
        target = self.resolvers.get(selected_scope)
        if selected_scope == "shared":
            if self.xport_url and (not target or target == REASON_REGISTRY_URL):
                target = self.xport_url
            target = target or REASON_REGISTRY_URL
        if not target:
            raise RDNUnavailableError(
                503,
                f"{selected_scope} resolver is not configured",
            )
        if selected_scope == "shared":
            return self._resolve_reason_uri_at(target, uri, **kwargs)
        return self._resolve_reason_uri_at(target, uri, scope=selected_scope, **kwargs)

    def resolve_from_xport(self, uri: str, **kwargs) -> ReasonArtifact:
        """
        Resolve a reason:// URI from the registry side after broker processing.

        Uses the xport_url if configured (reason.astrognosy.com), otherwise falls back.
        This is the public registry path in the full architecture.
        """
        return self.resolve_from_registry(uri, **kwargs)

    # ---------------- Scoped resolution and durable contributions ----------------

    def resolver_status(self) -> Dict[str, Any]:
        """Describe configured resolver layers without probing the network."""
        return {
            "default_scope": self.default_scope,
            "order": list(_RESOLVER_SCOPE_ORDER),
            "layers": {
                "local": {
                    "configured": True,
                    "kind": "local-memory",
                    "endpoint": self.node_url,
                    "node_available": self.available,
                },
                "organization": {
                    "configured": bool(self.resolvers.get("organization")),
                    "kind": "reason-resolver",
                    "endpoint": self.resolvers.get("organization"),
                },
                "shared": {
                    "configured": bool(self.resolvers.get("shared")),
                    "kind": "reason-resolver",
                    "endpoint": self.resolvers.get("shared"),
                },
            },
            "last_resolution": self._last_resolution,
        }

    def resolve_chain(
        self,
        address: str,
        *,
        scope: Optional[str] = None,
        version: Optional[str] = None,
        bypass_cache: bool = False,
    ) -> Optional[ReasonArtifact]:
        """Resolve local, then organization, then shared up to one selected scope.

        This is an explicit chain mode.  The established ``source='local'`` and
        ``source='registry'`` paths remain single-source operations.
        """
        canonical_address = validate_reason_address(address)
        selected_scope = normalize_scope(scope, default=self.default_scope)
        chain = _RESOLVER_SCOPE_ORDER[
            : _RESOLVER_SCOPE_ORDER.index(selected_scope) + 1
        ]
        attempts: List[Dict[str, Any]] = []
        for layer in chain:
            if layer == "local":
                try:
                    artifact = self.resolve(
                        canonical_address,
                        source="local",
                        version=version,
                        bypass_cache=bypass_cache,
                    )
                except RDNConflictError as exc:
                    attempts.append(
                        {"scope": layer, "outcome": "version_miss", "detail": str(exc)}
                    )
                    continue
                if artifact is not None:
                    attempts.append({"scope": layer, "outcome": "resolved"})
                    self._last_resolution = {
                        "address": canonical_address,
                        "selected_scope": selected_scope,
                        "resolved_scope": layer,
                        "version": artifact.version,
                        "attempts": attempts,
                    }
                    return artifact
                attempts.append({"scope": layer, "outcome": "not_found"})
                continue

            endpoint = self.resolvers.get(layer)
            if not endpoint:
                attempts.append({"scope": layer, "outcome": "not_configured"})
                continue
            try:
                artifact = self._resolve_reason_uri_at(
                    endpoint,
                    canonical_address,
                    version=version,
                    bypass_cache=bypass_cache,
                    scope=layer,
                )
            except RDNNotFoundError:
                attempts.append({"scope": layer, "outcome": "not_found"})
                continue
            except RDNRequestError as exc:
                attempts.append(
                    {
                        "scope": layer,
                        "outcome": "unavailable",
                        "detail": str(exc),
                    }
                )
                continue
            attempts.append({"scope": layer, "outcome": "resolved"})
            self._last_resolution = {
                "address": canonical_address,
                "selected_scope": selected_scope,
                "resolved_scope": layer,
                "resolver": endpoint,
                "version": artifact.version,
                "attempts": attempts,
            }
            return artifact

        self._last_resolution = {
            "address": canonical_address,
            "selected_scope": selected_scope,
            "resolved_scope": None,
            "version": version,
            "attempts": attempts,
        }
        return None

    def _store_contribution(self, envelope: ContributionEnvelope) -> Dict[str, Any]:
        now = time.time()
        initial_state = "local" if envelope.scope == "local" else "pending"
        encoded = json.dumps(
            envelope.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        with self._get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM rdn_contribution_queue WHERE contribution_id = ?",
                (envelope.contribution_id,),
            ).fetchone()
            if row is None:
                queue_sequence = int(
                    conn.execute(
                        """
                        SELECT next_sequence FROM rdn_contribution_queue_meta
                        WHERE singleton = 1
                        """
                    ).fetchone()[0]
                )
                conn.execute(
                    """
                    INSERT INTO rdn_contribution_queue
                    (contribution_id, queue_sequence, scope, envelope_json, state,
                     attempts, next_attempt_at, last_error, response_json,
                     created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 0, 0, NULL, NULL, ?, ?)
                    """,
                    (
                        envelope.contribution_id,
                        queue_sequence,
                        envelope.scope,
                        encoded,
                        initial_state,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    UPDATE rdn_contribution_queue_meta
                    SET next_sequence = ? WHERE singleton = 1
                    """,
                    (queue_sequence + 1,),
                )
            elif row[0] == "failed" and envelope.scope in CONTRIBUTION_NETWORK_SCOPES:
                conn.execute(
                    """
                    UPDATE rdn_contribution_queue
                    SET state = 'retry', attempts = 0, next_attempt_at = 0,
                        last_error = NULL, updated_at = ?
                    WHERE contribution_id = ?
                    """,
                    (now, envelope.contribution_id),
                )
        return self.inspect_contributions(
            contribution_id=envelope.contribution_id, limit=1
        )[0]

    def contribute(
        self,
        content: ContentInput,
        *,
        reason_address: str,
        scope: Optional[str] = None,
        media_type: str = "text/plain; charset=utf-8",
        project: str = "astrognosy",
        tags: Optional[Iterable[str]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        context: Optional[Mapping[str, Any]] = None,
        adapter: Optional[Mapping[str, Any]] = None,
        background: bool = True,
        flush: bool = False,
    ) -> Dict[str, Any]:
        """Durably retain one reusable artifact and optionally deliver it.

        Local scope is retained only in the local contribution ledger and never
        performs an HTTP write. Organization and shared scopes are written to
        SQLite before a bounded background or explicit synchronous flush.
        """
        envelope = ContributionEnvelope.create(
            content,
            reason_address=reason_address,
            scope=normalize_scope(scope, default=self.default_scope),
            media_type=media_type,
            project=project,
            tags=tuple(tags or ()),
            metadata=metadata,
            context=context,
            adapter=adapter,
        )
        queued = self._store_contribution(envelope)
        if envelope.scope == "local":
            return {
                "status": "retained",
                "contribution_id": envelope.contribution_id,
                "reason_address": envelope.reason_address,
                "scope": envelope.scope,
                "queue": queued,
                "network_write": False,
            }

        if flush:
            flush_result = self.flush_contributions(
                limit=1, contribution_id=envelope.contribution_id
            )
            current = self.inspect_contributions(
                contribution_id=envelope.contribution_id, limit=1
            )[0]
            return {
                "status": current["state"],
                "contribution_id": envelope.contribution_id,
                "reason_address": envelope.reason_address,
                "scope": envelope.scope,
                "queue": current,
                "flush": flush_result,
                "network_write": flush_result["attempted"] > 0,
            }

        scheduled = self._schedule_background_flush() if background else False
        return {
            "status": queued["state"],
            "contribution_id": envelope.contribution_id,
            "reason_address": envelope.reason_address,
            "scope": envelope.scope,
            "queue": queued,
            "background_flush_scheduled": scheduled,
            "network_write": False,
        }

    def _schedule_background_flush(self) -> bool:
        with self._background_flush_lock:
            self._background_flush_wakeup.set()
            if (
                self._background_flush_thread is not None
                and self._background_flush_thread.is_alive()
            ):
                return False
            thread = threading.Thread(
                target=self._background_flush_worker,
                name="reason-rdn-contribution-flush",
                daemon=True,
            )
            self._background_flush_thread = thread
            thread.start()
            return True

    def _background_flush_worker(self) -> None:
        """Drain ready work and wake bounded retries until no deliverable work remains."""
        current_thread = threading.current_thread()
        try:
            while True:
                self._background_flush_wakeup.clear()
                result = self.flush_contributions(limit=10)
                queue = result.get("queue") or self.contribution_queue_status()
                states = queue.get("states") or {}
                pending = int(states.get("pending", 0) or 0)
                retrying = int(states.get("retry", 0) or 0)
                selected = int(result.get("selected", 0) or 0)
                outcomes = result.get("outcomes") or []
                all_not_configured = bool(outcomes) and all(
                    item.get("status") == "not_configured" for item in outcomes
                )

                # Drain additional immediate batches, including contributions
                # queued while the previous network request was in flight.
                if pending and selected and not all_not_configured:
                    continue

                # Retry backoff is bounded by the queue attempt cap and the
                # five-minute per-attempt ceiling in flush_contributions().
                if retrying:
                    next_retry = queue.get("next_retry_at")
                    if next_retry is None:
                        break
                    delay = max(0.0, min(300.0, float(next_retry) - time.time()))
                    self._background_flush_wakeup.wait(timeout=delay)
                    continue

                if self._background_flush_wakeup.is_set():
                    continue
                with self._background_flush_lock:
                    if self._background_flush_wakeup.is_set():
                        continue
                    if self._background_flush_thread is current_thread:
                        self._background_flush_thread = None
                    return
        finally:
            restart = False
            with self._background_flush_lock:
                if self._background_flush_thread is current_thread:
                    self._background_flush_thread = None
                    restart = self._background_flush_wakeup.is_set()
            if restart:
                self._schedule_background_flush()

    def inspect_contributions(
        self,
        *,
        limit: int = 20,
        state: Optional[str] = None,
        contribution_id: Optional[str] = None,
        include_envelope: bool = False,
    ) -> List[Dict[str, Any]]:
        """Inspect durable queue state without performing a network action."""
        limit = max(1, min(200, int(limit or 20)))
        sql = """
            SELECT contribution_id, queue_sequence, scope, envelope_json, state,
                   attempts, next_attempt_at, last_error, response_json,
                   created_at, updated_at
            FROM rdn_contribution_queue WHERE 1=1
        """
        params: List[Any] = []
        if state is not None:
            sql += " AND state = ?"
            params.append(str(state))
        if contribution_id is not None:
            sql += " AND contribution_id = ?"
            params.append(str(contribution_id))
        sql += " ORDER BY queue_sequence ASC LIMIT ?"
        params.append(limit)
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            item: Dict[str, Any] = {
                "contribution_id": row["contribution_id"],
                "queue_sequence": int(row["queue_sequence"]),
                "scope": row["scope"],
                "state": row["state"],
                "attempts": int(row["attempts"]),
                "next_attempt_at": float(row["next_attempt_at"]),
                "last_error": row["last_error"],
                "response": (
                    json.loads(row["response_json"])
                    if row["response_json"]
                    else None
                ),
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
            }
            if include_envelope:
                item["envelope"] = parse_contribution_envelope(
                    json.loads(row["envelope_json"])
                ).to_dict()
            results.append(item)
        return results

    def contribution_queue_status(self) -> Dict[str, Any]:
        """Return compact queue counts for CLI, SDK, and MCP status."""
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT scope, state, COUNT(*)
                FROM rdn_contribution_queue
                GROUP BY scope, state
                ORDER BY scope, state
                """
            ).fetchall()
            next_retry = conn.execute(
                """
                SELECT MIN(next_attempt_at) FROM rdn_contribution_queue
                WHERE state = 'retry'
                """
            ).fetchone()[0]
        by_scope: Dict[str, Dict[str, int]] = {
            scope: {} for scope in _RESOLVER_SCOPE_ORDER
        }
        totals: Dict[str, int] = {}
        for scope, state, count in rows:
            by_scope.setdefault(scope, {})[state] = int(count)
            totals[state] = totals.get(state, 0) + int(count)
        return {
            "total": sum(totals.values()),
            "ready": totals.get("pending", 0) + totals.get("retry", 0),
            "states": totals,
            "by_scope": by_scope,
            "next_retry_at": float(next_retry) if next_retry is not None else None,
            "max_attempts": self.contribution_max_attempts,
        }

    def flush_contributions(
        self,
        *,
        limit: int = 10,
        retry_failed: bool = False,
        contribution_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Attempt a bounded queue batch and schedule deterministic retries."""
        limit = max(1, min(100, int(limit or 10)))
        current_time = float(time.time() if now is None else now)
        with self._get_conn() as conn:
            if retry_failed:
                retry_sql = """
                    UPDATE rdn_contribution_queue
                    SET state = 'retry', attempts = 0, next_attempt_at = 0,
                        last_error = NULL, updated_at = ?
                    WHERE state = 'failed'
                """
                retry_params: List[Any] = [current_time]
                if contribution_id is not None:
                    retry_sql += " AND contribution_id = ?"
                    retry_params.append(contribution_id)
                conn.execute(retry_sql, retry_params)
            conn.execute(
                """
                UPDATE rdn_contribution_queue
                SET state = 'retry', next_attempt_at = 0,
                    last_error = 'recovered stale in-flight attempt', updated_at = ?
                WHERE state = 'sending' AND updated_at <= ?
                """,
                (current_time, current_time - _CONTRIBUTION_STALE_SENDING_SECONDS),
            )
            configured_scopes = [
                scope
                for scope in CONTRIBUTION_NETWORK_SCOPES
                if self.resolvers.get(scope)
            ]
            select_sql = f"""
                SELECT contribution_id, queue_sequence, scope, envelope_json,
                       state, attempts
                FROM rdn_contribution_queue
                WHERE state IN ('pending', 'retry')
                  AND next_attempt_at <= ?
                  AND scope IN ({", ".join("?" for _ in configured_scopes)})
            """
            select_params: List[Any] = [current_time, *configured_scopes]
            if contribution_id is not None:
                select_sql += " AND contribution_id = ?"
                select_params.append(contribution_id)
            select_sql += " ORDER BY queue_sequence ASC LIMIT ?"
            select_params.append(limit)
            conn.row_factory = sqlite3.Row
            rows = (
                conn.execute(select_sql, select_params).fetchall()
                if configured_scopes
                else []
            )
            conn.commit()

        outcomes: List[Dict[str, Any]] = []
        for row in rows:
            contribution_key = str(row["contribution_id"])
            prior_state = str(row["state"])
            with self._get_conn() as conn:
                claimed = conn.execute(
                    """
                    UPDATE rdn_contribution_queue
                    SET state = 'sending', updated_at = ?
                    WHERE contribution_id = ? AND state = ?
                    """,
                    (current_time, contribution_key, prior_state),
                ).rowcount
                conn.commit()
            if not claimed:
                continue

            endpoint = self.resolvers.get(str(row["scope"]))
            if not endpoint:
                with self._get_conn() as conn:
                    conn.execute(
                        """
                        UPDATE rdn_contribution_queue
                        SET state = 'pending', last_error = 'resolver not configured',
                            updated_at = ? WHERE contribution_id = ?
                        """,
                        (current_time, contribution_key),
                    )
                    conn.commit()
                outcomes.append(
                    {
                        "contribution_id": contribution_key,
                        "status": "not_configured",
                    }
                )
                continue

            try:
                envelope = parse_contribution_envelope(
                    json.loads(str(row["envelope_json"]))
                )
                headers = self._contribution_headers(str(row["scope"]))
                headers[CONTRIBUTION_IDEMPOTENCY_HEADER] = contribution_key
                response_payload = self._http_post_strict(
                    f"{endpoint.rstrip('/')}{CONTRIBUTION_NETWORK_ROUTE}",
                    envelope.to_dict(),
                    headers=headers,
                )
                response = parse_contribution_receipt(
                    response_payload,
                    envelope=envelope,
                )
            except Exception as exc:
                attempts = int(row["attempts"]) + 1
                terminal_rejection = (
                    isinstance(exc, RDNHTTPError) and exc.status_code == 413
                )
                exhausted = terminal_rejection or (
                    attempts >= self.contribution_max_attempts
                )
                next_attempt = (
                    0.0
                    if exhausted
                    else current_time + min(300.0, float(2 ** max(0, attempts - 1)))
                )
                state_value = (
                    "rejected"
                    if terminal_rejection
                    else "failed"
                    if exhausted
                    else "retry"
                )
                with self._get_conn() as conn:
                    conn.execute(
                        """
                        UPDATE rdn_contribution_queue
                        SET state = ?, attempts = ?, next_attempt_at = ?,
                            last_error = ?, updated_at = ?
                        WHERE contribution_id = ?
                        """,
                        (
                            state_value,
                            attempts,
                            next_attempt,
                            str(exc),
                            current_time,
                            contribution_key,
                        ),
                    )
                    conn.commit()
                outcomes.append(
                    {
                        "contribution_id": contribution_key,
                        "status": state_value,
                        "attempts": attempts,
                        "next_attempt_at": next_attempt or None,
                        "retryable": not exhausted,
                    }
                )
                continue

            attempts = int(row["attempts"]) + 1
            with self._get_conn() as conn:
                conn.execute(
                    """
                    UPDATE rdn_contribution_queue
                    SET state = 'delivered', attempts = ?, next_attempt_at = 0,
                        last_error = NULL, response_json = ?, updated_at = ?
                    WHERE contribution_id = ?
                    """,
                    (
                        attempts,
                        json.dumps(response, ensure_ascii=True, sort_keys=True),
                        current_time,
                        contribution_key,
                    ),
                )
                conn.commit()
            outcomes.append(
                {
                    "contribution_id": contribution_key,
                    "status": "delivered",
                    "attempts": attempts,
                    "response": response,
                }
            )

        return {
            "selected": len(rows),
            "attempted": sum(
                1 for item in outcomes if item["status"] != "not_configured"
            ),
            "delivered": sum(
                1 for item in outcomes if item["status"] == "delivered"
            ),
            "outcomes": outcomes,
            "queue": self.contribution_queue_status(),
        }

    def runtime_status(self) -> Dict[str, Any]:
        """Return queue and resolver state without network side effects."""
        return {
            "resolvers": self.resolver_status(),
            "contributions": self.contribution_queue_status(),
        }

    def list_prefix(self, prefix: str, limit: int = 50) -> List[Dict[str, Any]]:
        """
        List artifacts whose address starts with the given prefix (e.g. 'reason://warf'
        or 'warf/build'). Works locally and against a node that supports /api/recall.
        Results are useful for browsing the reason:// namespace with partial URIs.
        """
        if not prefix:
            prefix = "reason://"
        if not prefix.startswith("reason://"):
            prefix = "reason://" + prefix.lstrip("/")

        limit = max(1, min(200, int(limit or 50)))

        # Try remote node first (broad recall + client-side filter)
        if self.node_url and self.available:
            try:
                results = self.recall(query="", limit=limit * 3)
                matches = [r for r in results if (r.get("address") or "").startswith(prefix)]
                return matches[:limit]
            except Exception:
                pass

        # Local fallback
        return self._list_prefix_local(prefix, limit)

    def _list_prefix_local(self, prefix: str, limit: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        conn = self._get_conn()
        try:
            conn.row_factory = sqlite3.Row
            sql = """
                SELECT address, domain, deposited_at, metadata_json 
                FROM warf_artifacts 
                WHERE address LIKE ? 
                ORDER BY deposited_at DESC 
                LIMIT ?
            """
            like = prefix + "%"
            rows = conn.execute(sql, (like, limit * 2)).fetchall()

            for row in rows:
                try:
                    meta = json.loads(row["metadata_json"])
                except Exception:
                    meta = {}
                results.append({
                    "address": row["address"],
                    "project": row["domain"],
                    "deposited_at": row["deposited_at"],
                    "content": meta.get("content", ""),
                    "tags": meta.get("tags", []),
                    "artifact_hash": meta.get("artifact_hash") or meta.get("audit_hash"),
                    "meta": meta,
                    "source": "local",
                })
        except Exception as e:
            logger.error("Local list_prefix failed: %s", e)
        finally:
            conn.close()
        return results[:limit]

    # ---------------- Core Operations ----------------

    def remember(
        self,
        content: str,
        tags: Optional[Iterable[str]] = None,
        project: str = "astrognosy",
        meta: Optional[Dict[str, Any]] = None,
        reason_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Deposit content. Uses HTTP node when available, otherwise local DB.
        Always returns a result with address + artifact_id.
        """
        tags = list(tags) if tags else []
        meta = dict(meta) if meta else {}
        content = (content or "").strip()
        if not content:
            return {"status": "error", "message": "Missing content"}

        project = (project or "unknown").strip()
        reason_address = reason_address or _project_address(project, content)

        # Build canonical metadata (content lives inside metadata_json)
        metadata: Dict[str, Any] = {
            "content": content,
            "tags": [str(t).strip() for t in tags if str(t).strip()],
            "project": project,
            "reason_address": reason_address,
            "stored_at": _now_iso(),
        }
        if meta:
            metadata.update(meta)

        audit_hash = _hash_payload(metadata)
        artifact_id = hashlib.sha1(
            f"{reason_address}:{audit_hash}".encode("utf-8")
        ).hexdigest()
        deposited_at = metadata["stored_at"]

        payload = {
            "content": content,
            "tags": metadata["tags"],
            "project": project,
            "reason_address": reason_address,
            "meta": meta,
        }

        # Prefer node
        if self.node_url and self.available:
            node_res = self._http_post(f"{self.node_url}/api/remember", payload)
            if node_res and node_res.get("status") == "remembered":
                if self.mirror_local:
                    self._remember_local_direct(
                        artifact_id, reason_address, project, deposited_at, audit_hash, metadata
                    )
                    node_res["local_mirrored"] = True
                node_res.setdefault("source", "node")
                return node_res

        # Fallback / mirror path
        return self._remember_local_direct(
            artifact_id, reason_address, project, deposited_at, audit_hash, metadata
        )

    def _remember_local_direct(
        self,
        artifact_id: str,
        address: str,
        domain: str,
        deposited_at: str,
        audit_hash: str,
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO warf_artifacts
                    (artifact_id, address, domain, category, task, deposited_at, audit_hash, metadata_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        address,
                        domain,
                        "handoff",
                        address.rsplit("/", 1)[-1],
                        deposited_at,
                        audit_hash,
                        json.dumps(metadata, ensure_ascii=True),
                    ),
                )
                conn.commit()
            return {
                "status": "remembered",
                "address": address,
                "artifact_id": artifact_id,
                "project": domain,
                "source": "local",
            }
        except Exception as e:
            logger.error("Local remember failed: %s", e)
            return {"status": "error", "message": str(e)}

    def recall(
        self,
        query: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        project: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Search artifacts. When node is healthy, query the node (broad recall) then
        optionally post-filter by tags. Falls back to local DB with substring + tag filter.
        """
        tags = list(tags) if tags else None
        limit = max(1, min(100, int(limit or 20)))

        if self.node_url and self.available:
            params = {"query": query or "", "project": project or "", "limit": limit}
            node_res = self._http_get(f"{self.node_url}/api/recall", params=params)
            if node_res and node_res.get("status") == "ok":
                results = node_res.get("results", []) or []
                if tags:
                    results = [
                        r
                        for r in results
                        if any(t in (r.get("tags") or []) for t in tags)
                        or any(t in (r.get("address") or "") for t in tags)
                    ]
                return results[:limit]

        # Local fallback
        return self._recall_local(query=query, tags=tags, project=project, limit=limit)

    def _recall_local(
        self,
        query: Optional[str],
        tags: Optional[List[str]],
        project: Optional[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        needle = (query or "").lower().strip()

        conn = self._get_conn()
        try:
            conn.row_factory = sqlite3.Row
            sql = "SELECT address, domain, deposited_at, metadata_json FROM warf_artifacts WHERE 1=1"
            params: List[Any] = []

            if project:
                sql += " AND domain = ?"
                params.append(project)

            # We fetch a bit more then filter in Python for simplicity and correctness
            sql += " ORDER BY deposited_at DESC LIMIT ?"
            params.append(limit * 3)

            rows = conn.execute(sql, params).fetchall()

            for row in rows:
                try:
                    meta = json.loads(row["metadata_json"])
                except Exception:
                    continue

                haystack = " ".join(
                    [
                        row["address"] or "",
                        row["domain"] or "",
                        row["deposited_at"] or "",
                        json.dumps(meta, ensure_ascii=True),
                    ]
                ).lower()

                if needle and needle not in haystack:
                    continue

                if tags:
                    entry_tags = meta.get("tags", []) or []
                    if not any(t in entry_tags for t in tags) and not any(
                        t in (row["address"] or "") for t in tags
                    ):
                        continue

                results.append(
                    {
                        "address": row["address"],
                        "project": row["domain"],
                        "deposited_at": row["deposited_at"],
                        "content": meta.get("content", ""),
                        "tags": meta.get("tags", []),
                        "meta": meta,
                        "source": "local",
                    }
                )
                if len(results) >= limit:
                    break
        except Exception as e:
            logger.error("Local recall failed: %s", e)
        finally:
            conn.close()

        return results

    def resolve(
        self,
        address: str,
        *,
        source: str = "local",
        scope: Optional[str] = None,
        version: Optional[str] = None,
        bypass_cache: bool = False,
    ) -> Optional[ReasonArtifact]:
        """Resolve from one source or an explicitly selected scoped chain."""
        canonical_address = validate_reason_address(address)
        if source == "chain":
            return self.resolve_chain(
                canonical_address,
                scope=scope,
                version=version,
                bypass_cache=bypass_cache,
            )
        if source == "registry":
            registry_kwargs: Dict[str, Any] = {
                "version": version,
                "bypass_cache": bypass_cache,
            }
            if scope is not None:
                registry_kwargs["scope"] = scope
            return self.resolve_from_registry(canonical_address, **registry_kwargs)
        if source != "local":
            raise ValueError("source must be 'local', 'registry', or 'chain'")

        if self.node_url and self.available:
            res = self._http_get(
                f"{self.node_url}/api/resolve", params={"address": canonical_address}
            )
            if res and res.get("status") == "ok":
                # Server now consistently returns "artifact"
                art = res.get("artifact") or res.get("result")
                if art:
                    parsed = parse_reason_artifact(art, source="local")
                    if version is not None and parsed.version != version:
                        raise RDNConflictError(
                            409,
                            "local artifact version does not match the requested version",
                            {"address": canonical_address, "version": parsed.version},
                        )
                    return parsed

        # Local
        conn = self._get_conn()
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT address, domain, deposited_at, metadata_json FROM warf_artifacts WHERE address = ?",
                (canonical_address,),
            ).fetchone()
            if not row:
                return None
            meta = json.loads(row["metadata_json"])
            parsed = parse_reason_artifact({
                "address": row["address"],
                "project": row["domain"],
                "deposited_at": row["deposited_at"],
                "content": meta.get("content", ""),
                "tags": meta.get("tags", []),
                "meta": meta,
            }, source="local")
            if version is not None and parsed.version != version:
                raise RDNConflictError(
                    409,
                    "local artifact version does not match the requested version",
                    {"address": canonical_address, "version": parsed.version},
                )
            return parsed
        except (ArtifactValidationError, RDNConflictError):
            raise
        except Exception as e:
            logger.error("Local resolve failed: %s", e)
            return None
        finally:
            conn.close()

    # ---------------- GUI / Advanced helpers (heartbeat, recent projects) ----------------

    def get_heartbeat(self, project: Optional[str] = None) -> str:
        """ASCII sparkline of activity over the last 7 days (░ ▒ ▓ █)."""
        try:
            if self.node_url and self.available:
                # Ask for a broad recent set
                node_results = self._http_get(
                    f"{self.node_url}/api/recall",
                    params={"query": "handoff", "project": project or "", "limit": 300},
                )
                entries = (node_results or {}).get("results", []) if node_results else []
            else:
                entries = []

            if not entries:
                # local fallback
                conn = self._get_conn()
                try:
                    conn.row_factory = sqlite3.Row
                    sql = "SELECT deposited_at FROM warf_artifacts WHERE deposited_at > date('now', '-7 days')"
                    p = []
                    if project:
                        sql += " AND domain = ?"
                        p.append(project)
                    rows = conn.execute(sql, p).fetchall()
                    entries = [{"deposited_at": r["deposited_at"]} for r in rows]
                finally:
                    conn.close()

            counts: Dict[str, int] = {}
            for e in entries:
                ts = e.get("deposited_at", "")
                day = (ts or "")[:10]
                if day:
                    counts[day] = counts.get(day, 0) + 1

            spark = ""
            for i in range(6, -1, -1):
                day = (datetime.now(timezone.utc).date() - __import__("datetime").timedelta(days=i)).isoformat()
                c = counts.get(day, 0)
                if c == 0:
                    spark += "░"
                elif c < 3:
                    spark += "▒"
                elif c < 6:
                    spark += "▓"
                else:
                    spark += "█"
            return spark
        except Exception:
            return "░░░░░░░"

    def get_recent_projects(self, limit: int = 10) -> List[str]:
        """Most recently active projects (domains)."""
        try:
            if self.node_url and self.available:
                res = self._http_get(
                    f"{self.node_url}/api/recall",
                    params={"query": "", "project": "", "limit": 200},
                )
                entries = (res or {}).get("results", []) if res else []
                seen: set[str] = set()
                projects: List[str] = []
                for e in entries:
                    p = e.get("project") or e.get("domain")
                    if p and p not in seen:
                        seen.add(p)
                        projects.append(p)
                        if len(projects) >= limit:
                            break
                if projects:
                    return projects
            # local
            conn = self._get_conn()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT domain FROM warf_artifacts ORDER BY deposited_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                return [r[0] for r in rows if r[0]]
            finally:
                conn.close()
        except Exception:
            return ["astrognosy"]

    # ---------------- Convenience ----------------

    def __repr__(self) -> str:
        return f"<RDNClient node={self.node_url or 'local'} available={self.available} db={self.db_path}>"
