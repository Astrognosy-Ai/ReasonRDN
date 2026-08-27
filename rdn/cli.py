"""
rdn — friendly CLI for the ReasonRDN memory substrate.

Designed to be useful both for humans and for agents that can shell out.

Examples:
    rdn remember "Fixed the critical race in the handoff protocol" --tags infra,bugfix
    rdn recall "race condition" --project ReasonRDN --limit 5
    rdn resolve reason://ReasonRDN/handoff/abc12345
    rdn status
"""

from __future__ import annotations

import argparse
import json
import os  # for env fallback for tokens
import sys
from pathlib import Path
from typing import Optional

from rdn import __version__
from rdn.artifact import PROTOCOL_LOCK_DIGEST, PROTOCOL_LOCK_ID
from rdn.client import REASON_REGISTRY_URL, WARF_GATEWAY_URL, RDNClient
from rdn.config import env_flag
from rdn.doclang_adapter import prepare_doclang_contribution
from rdn.handoff import ReasonRDN

_NETWORK_ENV_KEYS = (
    "REASON_USE_NETWORK",
    "REASON_USE_XCHANGE",
    "USE_WARF_XCHANGE",
    "REASON_XCHANGE",
    "XCHANGE",
)


def _resolve_node_url(args) -> Optional[str]:
    """Pick an explicit local/custom memory node."""
    return getattr(args, "node", None)


def _network_enabled(args, client: Optional[RDNClient] = None) -> bool:
    """Honor only a per-command flag or explicit environment selection."""
    if bool(getattr(args, "local", False)):
        return False
    if bool(getattr(args, "xchange", False)):
        return True
    if any(name in os.environ for name in _NETWORK_ENV_KEYS):
        return env_flag(*_NETWORK_ENV_KEYS)
    return False


def _resolve_source(args) -> str:
    """Choose only an explicit resolve source; ambient configuration cannot change it."""
    return getattr(args, "source", None) or (
        "registry" if bool(getattr(args, "xchange", False)) else "local"
    )


def cmd_remember(args):
    node_url = _resolve_node_url(args)
    rdn = ReasonRDN(node_url=node_url)
    tags = args.tags.split(",") if args.tags else None
    res = rdn.deposit_handoff(
        project=args.project,
        summary=args.content,
        state_tokens=(args.tokens or "").split() if args.tokens else [args.content[:30]],
        tags=tags,
    )
    if _network_enabled(args, rdn.client) and res.get("status") == "remembered":
        res["network"] = {
            "status": "admission-required",
            "reason": "requires-arbitration",
            "next": ["arbitrate", "admit"],
        }
    # Real token accounting: use public record helper (avoids double-deposit while ensuring
    # HarnessMetrics gets the tokens_used for accurate savings/velocity/ship-rate).
    tokens_used = getattr(args, "tokens_used", None) or os.environ.get("RDN_TOKENS_USED")
    if tokens_used:
        try:
            import rdn as reason
            reason.record_handoff(args.content, tags, tokens_used=int(tokens_used))
        except Exception:
            pass
    print(json.dumps(res, indent=2))


def cmd_recall(args):
    node_url = _resolve_node_url(args)
    client = RDNClient(node_url=node_url)
    results = client.recall(
        query=args.query,
        project=args.project,
        limit=args.limit,
    )
    # Real token accounting for recalls (savings when an agent uses prior high-quality artifact).
    tokens_saved = getattr(args, "tokens_saved", None) or os.environ.get("RDN_TOKENS_SAVED")
    if tokens_saved:
        try:
            import rdn as reason
            reason.record_recall(args.query, tokens_saved=int(tokens_saved))
        except Exception:
            pass
    print(json.dumps({"results": results}, indent=2))


def cmd_resolve(args):
    node_url = _resolve_node_url(args)
    client = RDNClient(node_url=node_url)
    source = _resolve_source(args)
    art = client.resolve(
        args.address,
        source=source,
        scope=getattr(args, "scope", None),
        version=args.version,
        bypass_cache=bool(args.bypass_cache),
    )
    if art:
        print(json.dumps(art.resolution_dict(), indent=2))
    else:
        print(json.dumps({"status": "not_found", "address": args.address}, indent=2))
        sys.exit(1)

    # Real token accounting for resolves (savings from using the current canonical from Xport).
    tokens_saved = getattr(args, "tokens_saved", None) or os.environ.get("RDN_TOKENS_SAVED")
    if tokens_saved:
        try:
            import rdn as reason
            reason.record_recall(args.address, tokens_saved=int(tokens_saved))
        except Exception:
            pass


def cmd_status(args):
    node_url = _resolve_node_url(args)
    client = RDNClient(node_url=node_url)
    print("ReasonRDN status")
    print("  Node URL :", client.node_url or "local fallback only")
    network_enabled = _network_enabled(args, client)
    print("  Network  :", "selected" if network_enabled else "local only")
    print(
        "  Gateway  :",
        (client.broker_url or WARF_GATEWAY_URL) if network_enabled else "not selected",
    )
    print(
        "  Registry :",
        (client.xport_url or REASON_REGISTRY_URL) if network_enabled else "not selected",
    )
    print("  Available:", client.available)
    print("  Local DB :", client.db_path)
    runtime = client.runtime_status()
    resolvers = runtime["resolvers"]
    queue = runtime["contributions"]
    print("  Scope    :", resolvers["default_scope"])
    print(
        "  Org      :",
        resolvers["layers"]["organization"]["endpoint"] or "not configured",
    )
    print("  Shared   :", resolvers["layers"]["shared"]["endpoint"])
    print(
        "  Queue    :",
        f"{queue['ready']} ready, {queue['states'].get('delivered', 0)} delivered, "
        f"{queue['states'].get('failed', 0)} failed, "
        f"{queue['states'].get('rejected', 0)} rejected",
    )
    print("  SDK lock :", PROTOCOL_LOCK_ID)
    print("  Lock SHA :", PROTOCOL_LOCK_DIGEST)
    projects = client.get_recent_projects(8)
    print("  Recent projects:", ", ".join(projects) if projects else "(none yet)")
    hb = client.get_heartbeat()
    print("  7-day heartbeat:", hb)
    try:
        import rdn as reason
        recent = reason.get_recent_uris()
        print("  Recent URIs:", ", ".join(recent[:5]) if recent else "(none yet)")
        s = reason.status()
        top = s.get("top_prefixes", [])
        if top:
            print("  Top prefixes:", ", ".join(top))
    except Exception:
        pass


def cmd_xchange_arbitrate(args):
    node_url = _resolve_node_url(args)
    client = RDNClient(node_url=node_url)

    packages = []
    for p in args.package:
        if ":" not in p:
            print(f"Bad package format (need agent_id:answer): {p}")
            return
        aid, ans = p.split(":", 1)
        packages.append({"agent_id": aid.strip(), "answer_text": ans.strip()})

    if len(packages) < 2:
        print(
            "WARF arbitration needs at least two --package values; "
            "use 'rdn --network remember' to share one handoff.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    result = client.network_arbitrate(
        query_text=args.query,
        packages=packages,
        reason_address=args.uri
    )
    print(json.dumps(result, indent=2))


def _load_json_argument(value: str):
    """Load inline JSON, ``@path`` JSON, or stdin via ``-``."""
    if value == "-":
        raw = sys.stdin.read()
    elif value.startswith("@"):
        raw = Path(value[1:]).read_text(encoding="utf-8")
    else:
        raw = value
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError("JSON argument must contain one object")
    return loaded


def cmd_contribute(args):
    doclang_enabled = bool(getattr(args, "doclang", False))
    if args.file and args.content is not None:
        raise SystemExit("Use either positional content or --file, not both")
    if doclang_enabled and not args.file:
        raise SystemExit("--doclang requires --file")
    if doclang_enabled and args.adapter:
        raise SystemExit("--doclang supplies adapter metadata; do not also use --adapter")
    if doclang_enabled:
        prepared = prepare_doclang_contribution(
            args.file,
            validation=getattr(args, "doclang_validation", "auto"),
        )
        content = prepared.content
        media_type = prepared.media_type
        adapter = prepared.adapter
    elif args.file:
        content = Path(args.file).read_bytes()
        media_type = args.media_type
        adapter = _load_json_argument(args.adapter) if args.adapter else None
    elif args.content is not None:
        content = args.content
        media_type = args.media_type
        adapter = _load_json_argument(args.adapter) if args.adapter else None
    else:
        raise SystemExit("contribute requires positional content or --file")
    client = RDNClient(node_url=_resolve_node_url(args))
    tags = [tag.strip() for tag in (args.tags or "").split(",") if tag.strip()]
    result = client.contribute(
        content,
        reason_address=args.uri,
        scope=args.scope,
        media_type=media_type,
        project=args.project,
        tags=tags,
        metadata=_load_json_argument(args.metadata) if args.metadata else None,
        context=_load_json_argument(args.context) if args.context else None,
        adapter=adapter,
        background=False,
        flush=bool(args.flush),
    )
    print(json.dumps(result, indent=2))


def cmd_queue(args):
    client = RDNClient(node_url=_resolve_node_url(args))
    if args.flush or args.retry_failed:
        result = client.flush_contributions(
            limit=args.limit,
            retry_failed=bool(args.retry_failed),
        )
    else:
        result = {
            "status": client.contribution_queue_status(),
            "items": client.inspect_contributions(
                limit=args.limit,
                state=args.state,
                include_envelope=bool(args.include_envelope),
            ),
        }
    print(json.dumps(result, indent=2))


def cmd_admit(args):
    client = RDNClient(node_url=_resolve_node_url(args))
    artifact = _load_json_argument(args.artifact)
    arbitration = _load_json_argument(args.arbitration)
    admitted = client.admit(
        artifact,
        arbitration,
        expected_current_version=args.expected_current_version,
    )
    print(json.dumps(admitted.resolution_dict(), indent=2))


def cmd_list(args):
    node_url = _resolve_node_url(args)
    client = RDNClient(node_url=node_url)
    results = client.list_prefix(args.prefix, limit=args.limit)
    print(json.dumps({"results": results}, indent=2))


def main():
    parser = argparse.ArgumentParser(
        prog="rdn",
        description="ReasonRDN local-first memory with optional WARF network arbitration."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"rdn {__version__}",
    )
    parser.add_argument("--node", default=None, help="Override node URL (default: auto-discover local private node)")
    parser.add_argument(
        "--network", "--use-network", "--xchange", "--use-xchange",
        dest="xchange", action="store_true",
        help="Explicitly select the WARF Gateway or Reason Registry for this command. "
             "The xchange names remain compatibility aliases."
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Keep this command local even when a network environment flag is set.",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_rem = sub.add_parser("remember", help="Deposit a handoff / memory")
    p_rem.add_argument("content", help="The summary or content to remember")
    p_rem.add_argument("--project", default="astrognosy")
    p_rem.add_argument("--tags", default=None, help="Comma-separated tags")
    p_rem.add_argument("--tokens", default=None, help="Extra context tokens for the local artifact hash (space separated)")
    p_rem.add_argument("--tokens-used", type=int, default=None, dest="tokens_used",
                       help="Tokens consumed to produce this handoff (for accurate harness metrics / token savings tracking)")
    p_rem.set_defaults(func=cmd_remember)

    p_rec = sub.add_parser("recall", help="Search memory")
    p_rec.add_argument("query", help="Search query")
    p_rec.add_argument("--project", default=None)
    p_rec.add_argument("--limit", type=int, default=10)
    p_rec.add_argument("--tokens-saved", type=int, default=None, dest="tokens_saved",
                       help="Tokens saved by using this recall (for accurate harness metrics)")
    p_rec.set_defaults(func=cmd_recall)

    p_res = sub.add_parser("resolve", help="Fetch exact artifact by reason:// address")
    p_res.add_argument("address")
    p_res.add_argument(
        "--source",
        choices=("local", "registry", "chain"),
        default=None,
        help="Resolve from one source or an explicit scoped chain (default: local).",
    )
    p_res.add_argument(
        "--scope",
        choices=("local", "organization", "shared"),
        default=None,
        help="Maximum resolver layer for --source chain (default: configured scope).",
    )
    p_res.add_argument("--version", default=None, help="Exact sha256: artifact version")
    p_res.add_argument("--bypass-cache", action="store_true")
    p_res.add_argument("--tokens-saved", type=int, default=None, dest="tokens_saved",
                       help="Tokens saved by resolving and using this artifact (for accurate harness metrics)")
    p_res.set_defaults(func=cmd_resolve)

    p_stat = sub.add_parser("status", help="Show current node/client status + heartbeat")
    p_stat.set_defaults(func=cmd_status)

    p_contribute = sub.add_parser(
        "contribute",
        help="Durably queue reusable content in local, organization, or shared scope",
    )
    p_contribute.add_argument("content", nargs="?", help="Exact UTF-8 text content")
    p_contribute.add_argument(
        "--file",
        help="Read exact bytes from a file (Windows and POSIX paths supported)",
    )
    p_contribute.add_argument(
        "--doclang",
        action="store_true",
        help="Prepare --file as exact DocLang bytes, using optional Docling conversion when needed",
    )
    p_contribute.add_argument(
        "--doclang-validation",
        choices=("auto", "structural", "reference"),
        default="auto",
        help="DocLang validation mode (default: auto)",
    )
    p_contribute.add_argument("--uri", required=True, help="Canonical reason:// address")
    p_contribute.add_argument(
        "--scope",
        choices=("local", "organization", "shared"),
        default=None,
        help="Contribution scope (default: configured scope, initially local)",
    )
    p_contribute.add_argument(
        "--media-type",
        default="text/plain; charset=utf-8",
        help="Exact media type, for example application/xml",
    )
    p_contribute.add_argument("--project", default="astrognosy")
    p_contribute.add_argument("--tags", default=None, help="Comma-separated tags")
    p_contribute.add_argument("--metadata", help="Context metadata JSON, @path, or -")
    p_contribute.add_argument("--context", help="Full context JSON, @path, or -")
    p_contribute.add_argument("--adapter", help="Adapter metadata JSON, @path, or -")
    p_contribute.add_argument(
        "--flush",
        action="store_true",
        help="Attempt one synchronous delivery after the durable queue write",
    )
    p_contribute.set_defaults(func=cmd_contribute)

    p_queue = sub.add_parser("queue", help="Inspect or flush the contribution queue")
    p_queue.add_argument("--limit", type=int, default=20)
    p_queue.add_argument(
        "--state",
        choices=(
            "local",
            "pending",
            "retry",
            "sending",
            "delivered",
            "failed",
            "rejected",
        ),
    )
    p_queue.add_argument("--include-envelope", action="store_true")
    p_queue.add_argument("--flush", action="store_true")
    p_queue.add_argument("--retry-failed", action="store_true")
    p_queue.set_defaults(func=cmd_queue)

    p_xarb = sub.add_parser(
        "arbitrate",
        aliases=["xchange-arbitrate"],
        help="Submit competing packages for explicit WARF arbitration",
    )
    p_xarb.add_argument("query", help="The query / problem being arbitrated")
    p_xarb.add_argument("--package", action="append", required=True,
                        help="agent_id:answer_text (repeatable). Example: --package agent1:the answer")
    p_xarb.add_argument("--uri", help="Optional reason:// address recorded in the event")
    p_xarb.set_defaults(func=cmd_xchange_arbitrate)

    p_admit = sub.add_parser(
        "admit",
        help="Compatibility: low-level arbitration-backed Registry admission",
    )
    p_admit.add_argument(
        "artifact",
        help="Canonical artifact JSON, @path, or - for stdin",
    )
    p_admit.add_argument(
        "arbitration",
        help="Gateway arbitration custody JSON, @path, or - for stdin",
    )
    p_admit.add_argument(
        "--expected-current-version",
        default=None,
        help="Exact current sha256: version for update; omitted means create-only",
    )
    p_admit.set_defaults(func=cmd_admit)

    # List / browse prefix (new for namespace exploration)
    p_list = sub.add_parser("list", help="List artifacts under a reason:// prefix (browse the namespace with partial URIs)")
    p_list.add_argument("prefix", help="e.g. reason://grok or grok/build or warf")
    p_list.add_argument("--limit", type=int, default=20, help="Max results to show")
    p_list.set_defaults(func=cmd_list)

    # One-liner experience
    p_start = sub.add_parser("start", help="Start the ReasonRDN dashboard and local memory harness.")
    p_start.set_defaults(func=lambda args: launch_harness())

    args = parser.parse_args()
    if hasattr(args, "cmd") and args.cmd == "start":
        launch_harness()
    else:
        args.func(args)


def launch_harness():
    """The one-liner 'start using rdn' experience."""
    print("\n" + "="*70)
    print("START USING RDN - THE LOCAL MEMORY HARNESS IS NOW LIVE")
    print("="*70)
    print("""
Instant features you just gained:
• Persistent local mirror (private node at 8765 + SQLite) that works offline and survives agent sessions
• Real token accounting: report --tokens-used when depositing; get accurate savings when you (or agents) resolve prior external winners
• Durable contribution queue: retain locally or deliver bounded organization/shared contributions in the background
• Explicit scoped resolution: local, organization, and shared layers with exact version pinning preserved
• WARF bridge: arbitrate competing packages; low-level admit remains compatible
• Public-boundary design: this package stores local memory and talks to the broker without exposing private scoring internals
• Live harness metrics (token savings, velocity, ship rate, positive signals) + suggestions that nudge you to produce useful handoffs
• Unified `import rdn as reason` + CLI/MCP/dash on the same coherent surface

This is the practical on-ramp to reason:// memory and WARF arbitration. Local first, network only when selected.

Launching the full visual harness now...
""")
    try:
        import rdn.dash as dash
        dash.launch()
    except Exception:
        print("\nTo launch the dashboard:")
        print("  pip install 'reason-rdn[dash]'")
        print("  rdn start")
        print("  # or: python -m rdn.dash")


if __name__ == "__main__":
    main()
