"""
rdn/mcp/server.py

MCP server exposing the unified ReasonRDN memory API as tools for Claude/Grok/Codex/etc.

Tools:
  - remember / recall: Retain and search local memory
  - resolve: Fetch one verified artifact from an explicit source
  - arbitrate: Submit competing packages to the WARF Gateway
  - admit: Publish one selected, event-backed artifact to the Reason Registry
  - status: Inspect local state and configured endpoints without network action

Usage (after `pip install -e .`):
  python -m rdn.mcp.server
  rdn-mcp

Environment / config (for explicit WARF network use or a custom node):
  REASON_USE_NETWORK=1          # preferred; legacy Xchange variables remain supported
  REASON_NODE_URL=...
  RDN_NODE_URL=...
  The unified client loads from env + ~/.reason-ecosystem.cfg + local port file.

Network mutation is available only through explicit arbitrate and admit calls.
Local ReasonRDN memory remains the default.
"""

from __future__ import annotations

import asyncio
import json
import sys

from rdn.artifact import MCP_ADVERTISED_TOOLS, ReasonArtifact
from rdn.client import RDNClient, RDNHTTPError

# Lazy MCP import so the core rdn package works without the optional extra
_mcp_available = False
try:
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import CallToolResult, TextContent, Tool
    _mcp_available = True
except ImportError:
    Server = None
    stdio_server = None
    CallToolResult = TextContent = Tool = None


def _network_share_envelope(result: object) -> dict:
    """Expose the actual network outcome without hiding a retained local copy."""
    if not isinstance(result, dict):
        return {"status": "error", "result": result}

    reported = str(result.get("status") or "").strip().lower()
    if reported in {"shared", "accepted", "ok", "success"}:
        status = "shared"
    elif reported:
        status = reported
    elif result.get("audit_hash") and result.get("winner"):
        status = "shared"
    else:
        status = "unknown"
    return {"status": status, "result": result}


class WARFMCPServer:
    """MCP server for ReasonRDN plus optional WARF networking.

    Provides local persistent memory plus explicit WARF arbitration, selected
    result admission, and Reason Registry resolution.

    Agents are instructed to:
      - resolve before re-reasoning on known problems
      - remember decisions worth preserving
      - share selected high-value work through the Gateway (when configured)
    """

    def __init__(self):
        if not _mcp_available or Server is None:
            raise RuntimeError(
                "MCP support not installed. Install with: pip install 'reason-rdn[mcp]' or 'reason-rdn[full]'"
            )
        self.server = Server("ReasonRDN")
        self.memory = RDNClient()  # honors preferred and compatibility env names
        self._register_tools()

    def _text_result(self, payload, is_error: bool = False) -> CallToolResult:
        if isinstance(payload, ReasonArtifact):
            payload = payload.resolution_dict()
        text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            isError=is_error,
        )

    def _tool_schemas(self) -> list[Tool]:
        tools = [
            Tool(
                name="remember",
                description="Remember content in local ReasonRDN memory. This never publishes or admits content.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The content / summary to remember"},
                        "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags for organization"},
                        "project": {"type": "string", "description": "Project / domain scope"},
                        "meta": {"type": "object", "description": "Optional metadata for the artifact."},
                        "tokens_used": {"type": "integer", "description": "Optional token count for harness metrics"},
                    },
                    "required": ["content"],
                },
            ),
            Tool(
                name="recall",
                description="Search local ReasonRDN memory by free-text query.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query"},
                        "project": {"type": "string", "description": "Limit to a specific project/domain"},
                        "limit": {"type": "integer", "description": "Maximum results to return"},
                        "tokens_saved": {"type": "integer", "description": "Optional token-savings count for harness metrics"},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="resolve",
                description="Resolve one reason:// artifact from exactly local memory or the Reason Registry. Local is the default and there is no automatic fallback.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "The reason:// address"},
                        "source": {
                            "type": "string",
                            "enum": ["local", "registry"],
                            "default": "local",
                        },
                        "version": {
                            "type": "string",
                            "pattern": "^sha256:[0-9a-f]{64}$",
                        },
                        "bypass_cache": {"type": "boolean", "default": False},
                    },
                    "required": ["address"],
                },
            ),
            Tool(
                name="arbitrate",
                description="Explicitly submit competing evidence-bearing packages to the WARF Gateway. This is a network action.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query_text": {"type": "string"},
                        "packages": {
                            "type": "array",
                            "minItems": 2,
                            "items": {"type": "object"},
                        },
                        "reason_address": {"type": "string"},
                        "query_id": {"type": "string"},
                    },
                    "required": ["query_text", "packages"],
                },
            ),
            Tool(
                name="admit",
                description="Explicitly publish a selected, arbitration-backed artifact to the Reason Registry. This is a network write and defaults to create-only.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "artifact": {"type": "object"},
                        "arbitration": {"type": "object"},
                        "expected_current_version": {
                            "type": ["string", "null"],
                            "pattern": "^sha256:[0-9a-f]{64}$",
                            "default": None,
                        },
                    },
                    "required": ["artifact", "arbitration"],
                },
            ),
            Tool(
                name="status",
                description="Show local harness state and configured network endpoints without performing a network action.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            ),
        ]
        if tuple(tool.name for tool in tools) != MCP_ADVERTISED_TOOLS:
            raise RuntimeError("MCP tool schemas drifted from rdn/protocol-lock.json")
        return tools

    def _register_tools(self):
        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict) -> CallToolResult:
            arguments = arguments or {}
            try:
                if name == "remember":
                    import rdn as reason
                    raw_tags = arguments.get("tags", [])
                    if isinstance(raw_tags, str):
                        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
                    elif isinstance(raw_tags, (list, tuple)):
                        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
                    else:
                        tags = []
                    tokens_used = arguments.get("tokens_used")
                    result = reason.remember(
                        content=arguments.get("content", ""),
                        tags=tags,
                        project=arguments.get("project", "astrognosy"),
                        meta=arguments.get("meta", {}) or {},
                        tokens_used=int(tokens_used) if tokens_used else None,
                    )
                    return self._text_result(result)

                if name == "recall":
                    query = arguments.get("query", "")
                    limit = int(arguments.get("limit", 10) or 10)
                    results = self.memory.recall(
                        query=query,
                        project=arguments.get("project", "astrognosy"),
                        limit=limit,
                    )
                    tokens_saved = arguments.get("tokens_saved")
                    try:
                        import rdn as reason
                        reason.record_recall(
                            query,
                            tokens_saved=int(tokens_saved) if tokens_saved else None,
                        )
                    except Exception:
                        pass
                    return self._text_result({"status": "ok", "results": results})

                if name == "resolve":
                    address = arguments.get("address", "")
                    artifact = self.memory.resolve(
                        address,
                        source=arguments.get("source", "local"),
                        version=arguments.get("version"),
                        bypass_cache=bool(arguments.get("bypass_cache", False)),
                    )
                    if not artifact:
                        return self._text_result({"status": "not_found", "address": address})
                    return self._text_result(artifact)

                if name in {"network_resolve", "xchange_resolve"}:
                    uri = arguments.get("uri", "")
                    result = self.memory.resolve(
                        uri,
                        source="registry",
                        version=arguments.get("version"),
                        bypass_cache=bool(arguments.get("bypass_cache", False)),
                    )
                    return self._text_result(result)

                if name in {"status", "harness_status"}:
                    import rdn as reason
                    return self._text_result(
                        {
                            "status": "ok",
                            "harness": reason.harness_metrics(),
                            "stack": reason.status(),
                        }
                    )

                if name in {"arbitrate", "network_arbitrate", "xchange_arbitrate"}:
                    kwargs = {
                        key: arguments[key]
                        for key in ("reason_address", "query_id")
                        if arguments.get(key) is not None
                    }
                    result = self.memory.network_arbitrate(
                        arguments.get("query_text", ""),
                        arguments.get("packages", []),
                        **kwargs,
                    )
                    return self._text_result(result)

                if name == "admit":
                    result = self.memory.admit(
                        arguments.get("artifact", {}),
                        arguments.get("arbitration", {}),
                        expected_current_version=arguments.get(
                            "expected_current_version"
                        ),
                    )
                    return self._text_result(result)

                if name in {"network_share", "xchange_share"}:
                    import rdn as reason
                    raw_tags = arguments.get("tags", [])
                    if isinstance(raw_tags, str):
                        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
                    else:
                        tags = raw_tags
                    tokens_used = arguments.get("tokens_used")
                    result = reason.remember(
                        content=arguments.get("content", ""),
                        uri=arguments.get("uri"),
                        tags=tags,
                        project=arguments.get("project", "astrognosy"),
                        tokens_used=int(tokens_used) if tokens_used else None,
                        network_share=True,
                    )
                    return self._text_result(_network_share_envelope(result))

                return self._text_result(f"Unknown tool: {name}", is_error=True)
            except RDNHTTPError as exc:
                return self._text_result(
                    {
                        "status": "request_error",
                        "status_code": exc.status_code,
                        "detail": str(exc),
                        "payload": exc.payload,
                    },
                    is_error=True,
                )
            except Exception as exc:
                return self._text_result(str(exc), is_error=True)

        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return self._tool_schemas()

    async def _run_stdio(self):
        async with stdio_server() as (read_stream, write_stream):
            await self.server.run(
                read_stream,
                write_stream,
                self.server.create_initialization_options(),
            )

    def run(self):
        print("ReasonRDN MCP Server starting (unified client)...", file=sys.stderr)
        print(f"  Node available: {self.memory.available}", file=sys.stderr)
        print(f"  Node URL: {self.memory.node_url}", file=sys.stderr)
        print(f"  Local DB: {self.memory.db_path}", file=sys.stderr)
        if self.memory.broker_url:
            print(
                f"  WARF Gateway: {self.memory.broker_url} (selected network actions enabled)",
                file=sys.stderr,
            )

        asyncio.run(self._run_stdio())


def main():
    server = WARFMCPServer()
    server.run()


if __name__ == "__main__":
    main()
