"""Embedded private WARF node for ReasonRDN (coherent memory system)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ..addressing import project_address as _project_address

LOGGER = logging.getLogger("rdn.node")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_STORAGE_DIR = os.path.join(
    os.path.expanduser("~"),
    ".reason-rdn",
    "private-node",
)
PORT_FILE_NAME = "private-node.port"

# Optional bearer token for remote / shared deployments.
# Set REASON_RDN_TOKEN in the environment of both the node and all clients.
# When set, all non-health endpoints require Authorization: Bearer <token>.
# This is the minimum to make remote use safe. For production team use,
# put the node behind a reverse proxy with proper TLS + stronger auth.
REQUIRED_TOKEN = os.environ.get("REASON_RDN_TOKEN") or os.environ.get("RDN_AUTH_TOKEN")


def default_db_path(storage_dir: Optional[str] = None) -> str:
    storage_root = storage_dir or DEFAULT_STORAGE_DIR
    return os.path.join(storage_root, "warf-node.db")


def port_file_path(storage_dir: Optional[str] = None) -> str:
    storage_root = storage_dir or DEFAULT_STORAGE_DIR
    return os.path.join(storage_root, PORT_FILE_NAME)


def ensure_schema(db_path: str) -> None:
    db_dir = os.path.dirname(db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = sqlite3.connect(db_path)
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


def _hash_payload(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


class PrivateWARFNodeServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address, RequestHandlerClass, db_path: str):
        self.db_path = db_path
        ensure_schema(self.db_path)
        super().__init__(server_address, RequestHandlerClass)


class PrivateWARFRequestHandler(BaseHTTPRequestHandler):
    server_version = "ReasonRDNPrivateWARF/1.0"

    def _send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    @property
    def _db_path(self) -> str:
        return getattr(self.server, "db_path")

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.address_string(), format % args)

    def _check_auth(self) -> bool:
        """Return True if the request is authorized (or no token is required)."""
        if not REQUIRED_TOKEN:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {REQUIRED_TOKEN}":
            return True
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"status": "error", "message": "Missing or invalid Authorization token"}
        )
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/api/health":
            # Health is always public (useful for discovery and load balancers)
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "node": "private",
                    "db_path": self._db_path,
                    "uptime": "ready",
                    "auth_required": bool(REQUIRED_TOKEN),
                },
            )
            return

        if not self._check_auth():
            return

        if parsed.path == "/api/recall":
            params = parse_qs(parsed.query)
            query = (params.get("query", [""])[0] or "").strip()
            project = (params.get("project", [""])[0] or "").strip() or None
            limit_raw = params.get("limit", ["20"])[0]
            try:
                limit = max(1, min(100, int(limit_raw)))
            except ValueError:
                limit = 20

            results = self._recall(query=query, project=project, limit=limit)
            self._send_json(HTTPStatus.OK, {"status": "ok", "results": results})
            return

        if parsed.path == "/api/resolve":
            if not self._check_auth():
                return
            params = parse_qs(parsed.query)
            address = (params.get("address", [""])[0] or "").strip()
            if not address:
                self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": "Missing address"})
                return

            result = self._resolve(address)
            if result is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Artifact not found"})
                return

            # Coherent contract: use "artifact" key (matches unified client + MCP expectations)
            self._send_json(HTTPStatus.OK, {"status": "ok", "artifact": result})
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Unknown endpoint"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/remember":
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Unknown endpoint"})
            return

        if not self._check_auth():
            return

        try:
            payload = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})
            return

        content = str(payload.get("content", "")).strip()
        tags = payload.get("tags", [])
        project = str(payload.get("project", "")).strip() or "unknown"
        reason_address = str(payload.get("reason_address") or _project_address(project, content))
        meta = payload.get("meta", {})

        if not content:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": "Missing content"})
            return

        if not isinstance(tags, list):
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": "tags must be a list"})
            return

        metadata = {
            "content": content,
            "tags": [str(tag) for tag in tags if str(tag).strip()],
            "project": project,
            "reason_address": reason_address,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        }
        if isinstance(meta, dict):
            metadata.update(meta)

        audit_hash = _hash_payload(metadata)
        artifact_id = hashlib.sha1(f"{reason_address}:{audit_hash}".encode("utf-8")).hexdigest()
        deposited_at = metadata["stored_at"]
        task_slug = reason_address.rsplit("/", 1)[-1]

        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO warf_artifacts
                (artifact_id, address, domain, category, task, deposited_at, audit_hash, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    reason_address,
                    project,
                    "handoff",
                    task_slug,
                    deposited_at,
                    audit_hash,
                    json.dumps(metadata, ensure_ascii=True),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        self._send_json(
            HTTPStatus.OK,
            {
                "status": "remembered",
                "address": reason_address,
                "artifact_id": artifact_id,
                "project": project,
                "source": "node",
            },
        )

    def _fetch_rows(self, project: Optional[str], limit: int) -> List[sqlite3.Row]:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            sql = "SELECT address, domain, deposited_at, metadata_json FROM warf_artifacts WHERE 1=1"
            params: List[Any] = []
            if project:
                sql += " AND domain = ?"
                params.append(project)
            sql += " ORDER BY deposited_at DESC LIMIT ?"
            params.append(max(1, limit) * 5)
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _recall(self, query: str, project: Optional[str], limit: int) -> List[Dict[str, Any]]:
        needle = query.lower().strip()
        results: List[Dict[str, Any]] = []

        for row in self._fetch_rows(project, limit):
            try:
                meta = json.loads(row["metadata_json"])
            except json.JSONDecodeError:
                continue

            haystack = " ".join(
                [
                    row["address"],
                    row["domain"],
                    row["deposited_at"],
                    json.dumps(meta, ensure_ascii=True),
                ]
            ).lower()

            if needle and needle not in haystack:
                continue

            results.append(
                {
                    "address": row["address"],
                    "project": row["domain"],
                    "deposited_at": row["deposited_at"],
                    "content": meta.get("content", ""),
                    "tags": meta.get("tags", []),
                    "meta": meta,
                    "source": "node",
                }
            )

            if len(results) >= limit:
                break

        return results

    def _resolve(self, address: str) -> Optional[Dict[str, Any]]:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT address, domain, deposited_at, metadata_json FROM warf_artifacts WHERE address = ?",
                (address,),
            ).fetchone()
            if row is None:
                return None

            meta = json.loads(row["metadata_json"])
            return {
                "address": row["address"],
                "project": row["domain"],
                "deposited_at": row["deposited_at"],
                "content": meta.get("content", ""),
                "tags": meta.get("tags", []),
                "meta": meta,
                "source": "node",
            }
        finally:
            conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the embedded ReasonRDN private WARF node")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind")
    parser.add_argument("--storage", default=DEFAULT_STORAGE_DIR, help="Storage directory for the node database")
    return parser


def serve(host: str, port: int, storage_dir: Optional[str] = None) -> None:
    db_path = default_db_path(storage_dir)
    storage_root = storage_dir or DEFAULT_STORAGE_DIR
    os.makedirs(storage_root, exist_ok=True)

    server = None
    bind_attempts = [port]
    if port != 0:
        bind_attempts.append(0)

    last_error = None
    for candidate_port in bind_attempts:
        try:
            server = PrivateWARFNodeServer((host, candidate_port), PrivateWARFRequestHandler, db_path=db_path)
            break
        except OSError as exc:
            last_error = exc
            LOGGER.warning("Failed to bind %s:%s (%s); retrying", host, candidate_port, exc)

    if server is None:
        raise last_error  # type: ignore[misc]

    actual_port = server.server_address[1]
    with open(port_file_path(storage_root), "w", encoding="utf-8") as handle:
        handle.write(str(actual_port))

    auth_note = " (auth required)" if REQUIRED_TOKEN else ""
    LOGGER.info("ReasonRDN node ready at http://%s:%s%s", host, actual_port, auth_note)
    if REQUIRED_TOKEN:
        LOGGER.info("Remote access is protected by REASON_RDN_TOKEN (or RDN_AUTH_TOKEN).")
    else:
        LOGGER.info("Running in local-only mode (recommended). Set REASON_RDN_TOKEN to enable secure remote/team use.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


def main(argv: Optional[List[str]] = None) -> None:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args(argv)
    serve(args.host, args.port, args.storage)


if __name__ == "__main__":
    main()
