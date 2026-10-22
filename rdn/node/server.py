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

from .. import notes as _notes
from ..addressing import project_address as _project_address
from ..config import data_dir

LOGGER = logging.getLogger("rdn.node")
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_STORAGE_DIR = str(data_dir() / "private-node")
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
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        # Same tables, upgrades, and search index as the client (rdn.notes).
        _notes.ensure_schema(conn)
    finally:
        conn.close()


def _hash_payload(payload: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


# Search helpers are shared with rdn.client through rdn.notes.
_RECALL_STOPWORDS: frozenset = _notes.RECALL_STOPWORDS
_tokenize_query = _notes.tokenize_query
_idf_score = _notes.idf_score


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
            query = (params.get("query", params.get("q", [""]))[0] or "").strip()
            project = (params.get("project", [""])[0] or "").strip() or None
            limit_raw = params.get("limit", ["20"])[0]
            try:
                limit = max(1, min(100, int(limit_raw)))
            except ValueError:
                limit = 20
            since = (params.get("since", [""])[0] or "").strip() or None
            until = (params.get("until", [""])[0] or "").strip() or None
            include_hidden = (params.get("include_hidden", [""])[0] or "").strip().lower() in {
                "1", "true", "yes", "on"
            }

            try:
                results = self._recall(
                    query=query,
                    project=project,
                    limit=limit,
                    since=since,
                    until=until,
                    include_hidden=include_hidden,
                )
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})
                return
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
        if parsed.path not in {"/api/remember", "/api/forget", "/api/update"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": "Unknown endpoint"})
            return

        if not self._check_auth():
            return

        try:
            payload = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})
            return

        if parsed.path in {"/api/forget", "/api/update"}:
            self._change_note(parsed.path, payload)
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

    def _change_note(self, path: str, payload: Dict[str, Any]) -> None:
        """Handle POST /api/forget and POST /api/update."""
        note = str(payload.get("note") or payload.get("id") or payload.get("address") or "").strip()
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        try:
            if path == "/api/forget":
                result = _notes.forget_note(conn, note, purge=bool(payload.get("purge")))
            else:
                tags = payload.get("tags")
                if tags is not None and not isinstance(tags, list):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"status": "error", "message": "tags must be a list"}
                    )
                    return
                result = _notes.update_note(
                    conn, note, str(payload.get("content") or ""), tags=tags
                )
        except _notes.NoteLookupError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"status": "error", "message": str(exc.args[0])})
            return
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"status": "error", "message": str(exc)})
            return
        finally:
            conn.close()
        result["source"] = "node"
        self._send_json(HTTPStatus.OK, result)

    def _recall(
        self,
        query: str,
        project: Optional[str],
        limit: int,
        since: Optional[str] = None,
        until: Optional[str] = None,
        include_hidden: bool = False,
    ) -> List[Dict[str, Any]]:
        """Search every saved note; raises ValueError for a bad date."""
        conn = sqlite3.connect(self._db_path, timeout=30.0)
        try:
            return _notes.search_notes(
                conn,
                query,
                project=project,
                limit=limit,
                since=since,
                until=until,
                include_hidden=include_hidden,
                source="node",
            )
        finally:
            conn.close()

    def _resolve(self, address: str) -> Optional[Dict[str, Any]]:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT address, domain, deposited_at, metadata_json FROM warf_artifacts "
                "WHERE address = ? AND forgotten_at IS NULL",
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
