"""
rdn.dash - local web dashboard for ReasonRDN.

Launch with:
  rdn start                 (or: rdn-dash)
  rdn start --port 8502     (pick another port)
  rdn start --network       (also listen on your local network; off by default)

Install the extra first: pip install "reason-rdn[dash]"
(from a source checkout: pip install -e ".[dash]").

By default the dashboard only listens on 127.0.0.1, so only this computer can
open it. It starts without Streamlit's first-run email prompt and with usage
statistics turned off.

Structure note: all Streamlit UI code lives inside _app(). launch() is a thin
launcher used only by the console scripts. Status and counting helpers are plain
functions so they can be tested without Streamlit.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

import rdn as reason  # the high-level API
from rdn.client import WARF_GATEWAY_URL, RDNClient

try:
    import requests
except ImportError:  # pragma: no cover - requests is a core dependency
    requests = None  # type: ignore

DEFAULT_PORT = 8501
LOCAL_HOST = "127.0.0.1"
NETWORK_HOST = "0.0.0.0"
CHECK_SERVICES_ENV = "REASON_DASH_CHECK_SERVICES"

ONLINE = "online"
OFFLINE = "offline"
NOT_SET_UP = "not set up"
NOT_CHECKED = "not checked"


# ============================================================
#  STATUS AND COUNTS (plain functions, no Streamlit)
# ============================================================

def probe(base_url: Optional[str], timeout: float = 2.0) -> str:
    """Return ``online`` only when the service answers its health check."""
    if not base_url or requests is None:
        return OFFLINE
    base = base_url.rstrip("/")
    for path in ("/health", "/api/health"):
        try:
            response = requests.get(f"{base}{path}", timeout=timeout)
        except Exception:
            continue
        if 200 <= response.status_code < 300:
            return ONLINE
    return OFFLINE


def service_statuses(client: RDNClient, check_hosted: bool = False) -> List[Dict[str, Any]]:
    """Describe every service the dashboard knows about, using real checks.

    The local node is always checked. Online services are contacted only when
    ``check_hosted`` is true, so opening the dashboard never reaches the
    internet unless you ask it to.
    """
    statuses: List[Dict[str, Any]] = []

    if client.node_url:
        node_state = probe(client.node_url)
        node_detail = client.node_url
    else:
        node_state = OFFLINE
        node_detail = "not running (start it with rdn-node). Notes are still saved to the local database."
    statuses.append({"name": "Local node", "state": node_state, "detail": node_detail})

    org = client.resolvers.get("organization")
    if not org:
        org_state, org_detail = NOT_SET_UP, "set REASON_ORGANIZATION_RESOLVER to share with your team"
    elif check_hosted:
        org_state, org_detail = probe(org), org
    else:
        org_state, org_detail = NOT_CHECKED, org
    statuses.append({"name": "Organization server", "state": org_state, "detail": org_detail})

    shared = client.resolvers.get("shared")
    if check_hosted:
        statuses.append({"name": "Public Reason Registry", "state": probe(shared), "detail": shared})
        gateway = client.broker_url or WARF_GATEWAY_URL
        statuses.append({"name": "WARF arbitration service", "state": probe(gateway), "detail": gateway})
    else:
        statuses.append({"name": "Public Reason Registry", "state": NOT_CHECKED, "detail": shared})
        statuses.append({
            "name": "WARF arbitration service",
            "state": NOT_CHECKED,
            "detail": client.broker_url or WARF_GATEWAY_URL,
        })
    return statuses


def local_counts(client: RDNClient) -> Dict[str, int]:
    """Count what is actually stored in the local database."""
    counts = {"notes": 0, "contributions": 0, "waiting_to_send": 0, "delivered": 0}
    try:
        with sqlite3.connect(str(client.db_path)) as conn:
            counts["notes"] = int(conn.execute("SELECT COUNT(*) FROM warf_artifacts").fetchone()[0])
    except Exception:
        pass
    try:
        queue = client.contribution_queue_status()
        states = queue.get("states") or {}
        counts["contributions"] = int(queue.get("total", 0))
        counts["waiting_to_send"] = int(queue.get("ready", 0))
        counts["delivered"] = int(states.get("delivered", 0))
    except Exception:
        pass
    return counts


def _state_badge(state: str) -> str:
    return {
        ONLINE: "🟢 Online",
        OFFLINE: "🔴 Offline",
        NOT_SET_UP: "⚪ Not set up",
        NOT_CHECKED: "⚪ Not checked",
    }.get(state, state)


# ============================================================
#  UI HELPERS (safe to call from _app only)
# ============================================================

def _st():
    import streamlit as st

    return st

def render_fingerprint_viz(artifact_hash: str, label: str = "ARTIFACT HASH") -> None:
    """Simple visualization of an artifact integrity hash."""
    st = _st()
    if not artifact_hash:
        st.write("No fingerprint")
        return

    cols = st.columns(8)
    for i, char in enumerate(artifact_hash[:32]):
        intensity = int(char, 16) / 15.0
        hue = (i * 17) % 360
        color = f"hsl({hue}, 70%, {30 + intensity * 40}%)"
        with cols[i % 8]:
            st.markdown(
                f'<div style="background:{color}; width:28px; height:28px; border-radius:4px; margin:2px;"></div>',
                unsafe_allow_html=True,
            )
    st.caption(f"{label} - {artifact_hash[:16]}...")


# ============================================================
#  THE ACTUAL DASHBOARD — ALL STREAMLIT CODE LIVES INSIDE _app()
# ============================================================

def _app():
    """The Streamlit application. Streamlit executes this when it runs the script."""
    st = _st()

    st.set_page_config(
        page_title="ReasonRDN dashboard",
        page_icon="🧠",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    st.markdown(
        """
        <style>
        .reason-uri { font-family: 'SF Mono', monospace; }
        [data-testid="stMetricValue"] { color: inherit; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.title("ReasonRDN")
    st.markdown(
        "A local memory for your AI agents. Save notes and decisions, find them "
        "again later, and give the answers worth reusing a fixed `reason://` address. "
        "Everything here stays on this computer unless you choose to share it."
    )

    client = RDNClient()
    status = reason.status()

    check_default = os.environ.get(CHECK_SERVICES_ENV, "").strip().lower() in {"1", "true", "yes", "on"}
    check_hosted = st.sidebar.checkbox(
        "Check online services",
        value=check_default,
        help="Contact the organization server, the public Reason Registry, and the WARF "
             "arbitration service to see if they answer. Off by default so the dashboard "
             "stays fully local.",
    )
    use_network = st.sidebar.checkbox(
        "Prepare notes for sharing",
        value=bool(status.get("network_mode")),
        help="Notes are still saved locally first. Sharing needs a separate arbitration and admission step.",
    )

    # --- Status (real checks) ---
    st.subheader("Services")
    statuses = service_statuses(client, check_hosted=check_hosted)
    cols = st.columns(len(statuses))
    for col, item in zip(cols, statuses):
        with col:
            st.metric(item["name"], _state_badge(item["state"]))
            if item.get("detail"):
                st.caption(str(item["detail"]))
    if not check_hosted:
        st.caption("Online services were not contacted. Turn on \"Check online services\" in the sidebar to test them.")

    # --- Counts (real numbers only) ---
    st.subheader("What is stored on this computer")
    counts = local_counts(client)
    m = reason.harness_metrics()
    mcol1, mcol2, mcol3, mcol4 = st.columns(4)
    with mcol1:
        st.metric("Notes saved", f"{counts['notes']:,}")
    with mcol2:
        st.metric("Contributions saved", f"{counts['contributions']:,}")
        if counts["waiting_to_send"]:
            st.caption(f"{counts['waiting_to_send']:,} waiting to be sent")
    with mcol3:
        st.metric("Lookups made", f"{int(m.get('total_recalls', 0)):,}")
    with mcol4:
        reported = int(m.get("estimated_tokens_saved", 0) or 0)
        st.metric("Tokens saved (reported)", f"{reported:,}" if reported else "None reported")
        st.caption("Only counts tokens_saved values your agents report.")

    if m.get("suggestions"):
        st.info("**Suggestions**")
        for suggestion in m["suggestions"]:
            st.write(f"• {suggestion}")

    st.divider()

    # ============================================================
    #  DEPOSIT FORM
    # ============================================================

    with st.container(border=True):
        st.markdown("### Save a note")
        st.caption("Notes are saved in the local database on this computer.")

        col_a, col_b = st.columns([3, 1])
        with col_a:
            content = st.text_area("Note", height=100, placeholder="Fixed the deployment race by pausing writes first...")
            project = st.text_input("Project", value="astrognosy")
            tags = st.text_input("Tags (comma separated)", value="handoff,infra")
            col_prefix, col_rest = st.columns([1.1, 4])
            with col_prefix:
                st.markdown("**reason://**")
            with col_rest:
                uri_rest = st.text_input("Fixed address (optional)",
                                         value="", label_visibility="collapsed",
                                         placeholder="team/topic/task")
            uri = f"reason://{uri_rest}" if uri_rest else None
            tokens_used = st.number_input("Tokens used to produce this (optional)", min_value=0, value=0, step=100, help="The real token cost of the work behind this note, if you know it.")
        with col_b:
            target_network = st.checkbox("Prepare for sharing", value=use_network)
            st.caption("The note is saved locally either way. Sharing it is a separate, explicit step.")

            if st.button("Save note", use_container_width=True, type="primary"):
                tags_list = [t.strip() for t in tags.split(",") if t.strip()]
                meta = {"reason_uri": uri} if uri else {}
                tu = int(tokens_used) if tokens_used and tokens_used > 0 else None
                res = reason.remember(
                    content,
                    uri=uri,
                    tags=tags_list,
                    project=project,
                    meta=meta,
                    tokens_used=tu,
                    network_share=target_network,
                )
                st.success(f"Saved at {res.get('address') or res.get('local', {}).get('address', '')}")

                if res.get("artifact_hash"):
                    st.markdown("**Artifact Hash**")
                    render_fingerprint_viz(res["artifact_hash"])

    # ============================================================
    #  REASON:// EXPLORER
    # ============================================================

    with st.container(border=True):
        st.markdown("### Look up an address")

        # Session state for prefix input persistence
        if "explorer_prefix" not in st.session_state:
            st.session_state.explorer_prefix = ""

        # Use module-level recent URIs for cross-session persistence
        try:
            recent_uris = reason.get_recent_uris()
        except Exception:
            recent_uris = []

        col_prefix2, col_rest2 = st.columns([1.1, 4])
        with col_prefix2:
            st.markdown("**reason://**")
        with col_rest2:
            uri_rest2 = st.text_input(
                "Partial or full URI (supports prefix browsing)",
                value=st.session_state.explorer_prefix,
                key="explorer_input",
                label_visibility="collapsed",
                placeholder="team   or   team/topic   or   team/topic/task"
            )
            if uri_rest2 != st.session_state.explorer_prefix:
                st.session_state.explorer_prefix = uri_rest2

        uri = f"reason://{uri_rest2}" if uri_rest2 else None

        if st.button("Look up", use_container_width=True):
            if uri:
                # Try exact first
                art = None
                try:
                    art = RDNClient().resolve_from_registry(uri) if use_network else client.resolve(uri)
                except reason.ArtifactValidationError:
                    art = None  # a partial address: browse it as a prefix below
                except reason.RDNRequestError as exc:
                    st.error(f"The online registry could not be reached: {exc}")
                if art:
                    st.json(art.resolution_dict() if hasattr(art, "resolution_dict") else dict(art))
                    if art.get("artifact_hash"):
                        render_fingerprint_viz(art["artifact_hash"], "RESOLVED HASH")
                else:
                    # Prefix / incomplete URI browse with recursive tree view
                    matches = reason.list_prefix(uri, limit=30)
                    if matches:
                        st.success(f"**{len(matches)}** artifact(s) under or matching **{uri}**")

                        # Build nested tree structure
                        def build_tree(items, base):
                            tree = {}
                            for m in items:
                                addr = m.get("address", "")
                                if not addr.startswith(base):
                                    continue
                                rel = addr[len(base):].lstrip("/")
                                parts = [p for p in rel.split("/") if p]
                                current = tree
                                for i, part in enumerate(parts):
                                    if i == len(parts) - 1:
                                        current.setdefault(part, []).append(m)
                                    else:
                                        current = current.setdefault(part, {})
                            return tree

                        def render_tree(node, current_path=""):
                            for key in sorted(node.keys()):
                                full_path = f"{current_path}/{key}".lstrip("/")
                                full_uri = f"{uri.rstrip('/')}/{full_path}".rstrip("/")
                                if isinstance(node[key], dict):
                                    with st.expander(f"📁 {key}"):
                                        render_tree(node[key], f"{current_path}/{key}".lstrip("/"))
                                else:
                                    items = node[key]
                                    with st.expander(f"📄 {key} ({len(items)})", expanded=True):
                                        for m in items:
                                            addr = m.get("address", full_uri)
                                            col1, col2 = st.columns([6, 1])
                                            with col1:
                                                st.markdown(f"**{addr}**")
                                                st.code(addr, language=None)  # easy to copy
                                                content = m.get("content", "")
                                                st.write(content[:350] + ("..." if len(content) > 350 else ""))
                                                if m.get("artifact_hash"):
                                                    render_fingerprint_viz(m["artifact_hash"])
                                                st.caption(f"Project: {m.get('project', '?')} • {m.get('deposited_at', '')[:16]}")
                                            with col2:
                                                if st.button("📋", key=f"use_{addr}", help="Load this URI"):
                                                    st.session_state.explorer_prefix = addr.replace("reason://", "")
                                                    try:
                                                        reason.add_recent_uri(addr)
                                                    except Exception:
                                                        pass
                                                    st.toast(f"Loaded {addr}")
                                                    st.rerun()

                        tree = build_tree(matches, uri)
                        render_tree(tree)
                    else:
                        st.warning(f"No exact match and no artifacts found under the prefix **{uri}**.")

        # Recent URIs (persisted via reason module for cross-session)
        try:
            recent_uris = reason.get_recent_uris()
        except Exception:
            recent_uris = []
        if recent_uris:
            st.caption("Recent URIs (click to load)")
            rec_cols = st.columns(min(4, len(recent_uris)))
            for i, rec in enumerate(recent_uris[:4]):
                with rec_cols[i]:
                    if st.button(rec.replace("reason://", ""), key=f"recent_{i}"):
                        st.session_state.explorer_prefix = rec.replace("reason://", "")
                        try:
                            reason.add_recent_uri(rec)
                        except Exception:
                            pass
                        st.rerun()

    # ============================================================
    #  RECENT NOTES
    # ============================================================

    st.subheader("Recent notes")
    recent = client.recall("", limit=8)
    if recent:
        for item in recent:
            with st.expander(f"{item.get('project', 'unknown')} • {item.get('deposited_at', '')[:16]}"):
                st.code(item.get("address", ""), language=None)
                st.write(item.get("content", ""))
                if item.get("artifact_hash"):
                    render_fingerprint_viz(item["artifact_hash"])
    else:
        st.info("No notes yet. Save one above or run: rdn remember \"your first note\"")

    if st.button("🔄 Refresh"):
        st.rerun()


# ============================================================
#  THIN LAUNCHER — ONLY FOR CONSOLE SCRIPT / DIRECT EXECUTION
# ============================================================

def build_launch_args(port: int = DEFAULT_PORT, network: bool = False) -> List[str]:
    """Return the Streamlit command line used by ``rdn start``.

    Binds to 127.0.0.1 unless ``network`` is true, runs headless (which skips the
    first-run email prompt), and turns off Streamlit usage statistics.
    """
    return [
        "streamlit",
        "run",
        __file__,
        "--server.address",
        NETWORK_HOST if network else LOCAL_HOST,
        "--server.port",
        str(int(port)),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
        "--theme.base",
        "dark",
    ]


def launch(argv: Optional[List[str]] = None, *, port: Optional[int] = None,
           network: Optional[bool] = None, check_services: Optional[bool] = None) -> None:
    """Console script entry point for ``rdn-dash`` and ``rdn start``."""
    import argparse

    parser = argparse.ArgumentParser(prog="rdn-dash", description="Start the ReasonRDN dashboard.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to use (default 8501)")
    parser.add_argument(
        "--network",
        action="store_true",
        help="Also listen on your local network (0.0.0.0). Off by default: only this computer can open it.",
    )
    parser.add_argument(
        "--check-services",
        action="store_true",
        help="Contact online services on load to show whether they answer.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    port = args.port if port is None else port
    network = args.network if network is None else network
    check_services = args.check_services if check_services is None else check_services

    try:
        from streamlit.web import cli as stcli
    except ImportError:
        print('The dashboard needs an extra package. Install it with: pip install "reason-rdn[dash]"')
        print('(from a source checkout: pip install -e ".[dash]")')
        raise SystemExit(1)

    if check_services:
        os.environ[CHECK_SERVICES_ENV] = "1"
    os.environ.setdefault("STREAMLIT_BROWSER_GATHER_USAGE_STATS", "false")

    shown = "http://127.0.0.1" if not network else "http://<this computer's address>"
    print(f"ReasonRDN dashboard: {shown}:{port}")
    if network:
        print("Warning: --network is on, so other computers on your network can open this dashboard.")
    else:
        print("Only this computer can open it. Use --network to allow other computers.")
    print("Press Ctrl+C to stop.")

    sys.argv = build_launch_args(port=port, network=network)
    sys.exit(stcli.main())


if __name__ == "__main__":
    # Streamlit runs this file as __main__ (via launch() or `streamlit run`).
    _app()
