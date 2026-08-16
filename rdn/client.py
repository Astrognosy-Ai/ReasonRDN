"""
rdn.client - Unified memory client for ReasonRDN.

This package is the public local-first on-ramp for reason:// memory. It works
entirely offline with a local SQLite-backed node. Explicit calls can arbitrate
competing packages through the WARF Gateway and admit the selected result to
the Reason Registry; local memory operations never trigger those network acts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

import rfc8785

from .addressing import project_address as _project_address
from .artifact import (
    EVENT_RECORD_SCHEMA,
    ArtifactValidationError,
    ReasonArtifact,
    parse_reason_artifact,
    validate_reason_address,
)
from .config import env_flag

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

        config = self._load_config()
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
            conn.commit()
        finally:
            conn.close()

    def _get_conn(self):
        return sqlite3.connect(str(self.db_path))

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
            headers = self._auth_headers()
            if requests:
                r = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
                r.raise_for_status()
                return r.json()
            elif urllib_request:
                data = json.dumps(payload).encode("utf-8")
                req = urllib_request.Request(url, data=data, method="POST")
                req.add_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
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
        self, url: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """GET JSON without collapsing HTTP status or transport failures."""
        headers = self._auth_headers()
        if requests:
            try:
                response = requests.get(
                    url, params=params, headers=headers, timeout=self.timeout
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
            request = urllib_request.Request(url, headers=headers)
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
        token = (
            os.environ.get("REASON_RDN_TOKEN")
            or os.environ.get("RDN_AUTH_TOKEN")
            or os.environ.get("WARF_API_KEY")
            or os.environ.get("XPORT_API_KEY")
        )
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    @staticmethod
    def _admission_headers() -> Dict[str, str]:
        """Use the Registry write credential without conflating WARF bearer auth."""
        token = os.environ.get("REASON_REGISTRY_API_KEY") or os.environ.get(
            "XPORT_API_KEY"
        )
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
    ) -> ReasonArtifact:
        """Resolve and verify one artifact at one exact Registry endpoint."""
        canonical_address = validate_reason_address(uri)
        params: Dict[str, Any] = {"address": canonical_address}
        if version is not None:
            params["version"] = version
        if bypass_cache:
            params["bypass_cache"] = True
        data = self._http_get_strict(f"{target.rstrip('/')}/resolve", params=params)
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

    def resolve_from_registry(self, uri: str, **kwargs) -> ReasonArtifact:
        """Resolve a reason:// URI through the configured Reason Registry."""
        target = self.xport_url or REASON_REGISTRY_URL
        return self._resolve_reason_uri_at(target, uri, **kwargs)

    def resolve_from_xport(self, uri: str, **kwargs) -> ReasonArtifact:
        """
        Resolve a reason:// URI from the registry side after broker processing.

        Uses the xport_url if configured (reason.astrognosy.com), otherwise falls back.
        This is the public registry path in the full architecture.
        """
        return self.resolve_from_registry(uri, **kwargs)

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
        version: Optional[str] = None,
        bypass_cache: bool = False,
    ) -> Optional[ReasonArtifact]:
        """Resolve from exactly one selected source; local is the default."""
        canonical_address = validate_reason_address(address)
        if source == "registry":
            return self.resolve_from_registry(
                canonical_address,
                version=version,
                bypass_cache=bypass_cache,
            )
        if source != "local":
            raise ValueError("source must be 'local' or 'registry'")

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
