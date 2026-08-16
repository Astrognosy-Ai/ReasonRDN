"""
rdn.dash - web dashboard for ReasonRDN memory and optional WARF networking.

Launch with:
  rdn-dash
  or
  python -m rdn.dash
  or
  streamlit run -m rdn.dash   (after `pip install 'reason-rdn[dash]'`)

This file is structured to avoid the "Runtime instance already exists!" error.

- ALL Streamlit UI code lives inside _app().
- launch() is a *thin* launcher used ONLY by the console script / direct execution.
- When Streamlit itself runs the module, it safely executes _app().
- No st.* calls outside _app().
- No self-launching stcli logic inside the app execution path.
"""

from __future__ import annotations

import hashlib

try:
    import streamlit as st
except ImportError:
    print("Install with: pip install 'reason-rdn[dash]' or 'reason-rdn[full]'")
    raise

import rdn as reason  # THE coherent API
from rdn.client import RDNClient

# ============================================================
#  UI HELPERS (safe to call from _app only)
# ============================================================

def render_fingerprint_viz(artifact_hash: str, label: str = "ARTIFACT HASH") -> None:
    """Simple visualization of an artifact integrity hash."""
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
    """The actual Streamlit application. 
    Streamlit executes THIS when it runs the script.
    """

    # --- Page config ---
    st.set_page_config(
        page_title="ReasonRDN • WARF",
        page_icon="🧠",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # --- Theme ---
    st.markdown(
        """
        <style>
        .stApp { background: #0a0a0f; color: #e0e0ff; }
        .stButton>button { background: #7c6af5; color: white; border: none; border-radius: 8px; }
        .stButton>button:hover { background: #5a4ed1; }
        .metric-card { background: #13131a; border: 1px solid #2a2a3a; border-radius: 12px; padding: 16px; }
        .reason-uri { font-family: 'SF Mono', monospace; color: #7c6af5; }
        .fingerprint-viz { font-family: monospace; letter-spacing: 2px; }
        h1, h2, h3 { color: #c0c0ff; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # --- Header ---
    st.title("reason - Local Memory Harness")
    st.markdown("""
    **One command. Persistent agent memory.**

    Persistent memory across every agent and session.
    Explicitly share selected handoffs through the WARF Gateway at warf.astrognosy.com.
    Local memory and artifact hashes stay simple and auditable.
    Reason Registry at reason.astrognosy.com, with xport.astrognosy.com retained for compatibility.

    Live metrics: token savings, velocity, ship rate, positive signals, and suggestions.

    This is your model-agnostic on-ramp to the reason:// network. Local-first with an optional broker bridge.
    """)

    st.success("ReasonRDN is running. Your agents can now keep local memory across sessions, optionally share high-signal artifacts through the broker, and track estimated token savings from reused reason:// artifacts.")

    status = reason.status()

    # --- Sidebar toggle ---
    use_network = st.sidebar.checkbox(
        "WARF NETWORK - share selected handoffs",
        value=bool(status.get("network_mode")),
        help="When enabled, explicitly selected deposits use the WARF Gateway. Local retention remains the default."
    )

    # --- Status ---
    st.subheader("Harness Status & Impact")
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Local Node", "🟢 Online" if status.get("local_available") else "🔴 Offline", "private by default")
    with col2:
        st.metric("WARF Gateway", "🟢 warf.astrognosy.com", "Optional network arbitration")
    with col3:
        st.metric("Reason Registry", "🟢 reason.astrognosy.com", "xport hostname compatible")
    with col4:
        st.metric("Mode", "NETWORK" if use_network else "LOCAL", "Local memory first")

    # --- Metrics ---
    st.subheader("Your Impact (Agnostic Harness Metrics)")
    m = reason.harness_metrics() if hasattr(reason, "harness_metrics") else status

    mcol1, mcol2, mcol3, mcol4 = st.columns(4)
    with mcol1:
        st.metric("💰 Tokens Saved (est.)", f"{m.get('estimated_tokens_saved', 0):,}", "vs re-reasoning from scratch")
    with mcol2:
        st.metric("⚡ Velocity", f"{m.get('velocity', 0)}", "handoffs per active cycle")
    with mcol3:
        st.metric("🚢 Ship Rate", f"{m.get('ship_rate', 0)}%", "handoffs that became useful recalls")
    with mcol4:
        st.metric("Positive Signals", m.get('vibe_stars', 0), "shares and useful handoffs")

    if m.get("suggestions"):
        st.info("**Workflow Suggestions** (harness intelligence)")
        for s in m["suggestions"]:
            st.write(f"• {s}")

    st.divider()

    # ============================================================
    #  DEPOSIT FORM
    # ============================================================

    with st.container(border=True):
        st.markdown("### Deposit")
        st.caption("Every deposit creates durable local memory and updates harness metrics.")

        col_a, col_b = st.columns([3, 1])
        with col_a:
            content = st.text_area("Content / Insight / Handoff", height=100, placeholder="Fixed the critical race in the handoff protocol...")
            project = st.text_input("Project / Domain", value="astrognosy")
            tags = st.text_input("Tags (comma separated)", value="handoff,infra")
            col_prefix, col_rest = st.columns([1.1, 4])
            with col_prefix:
                st.markdown("**reason://**")
            with col_rest:
                uri_rest = st.text_input("reason:// address suffix (optional)",
                                         value="", label_visibility="collapsed",
                                         placeholder="grok/build/test   or   warf/decision-theory/...")
            uri = f"reason://{uri_rest}" if uri_rest else None
            tokens_used = st.number_input("Tokens used to produce this (optional, for accurate harness metrics)", min_value=0, value=0, step=100, help="Report the real token cost of the work that produced this artifact. Used for precise savings tracking when others resolve it later.")
        with col_b:
            target_network = st.checkbox("Prepare for WARF arbitration", value=use_network)
            st.caption("This deposit stays local and returns the explicit arbitrate → admit next steps.")

            if st.button("🚀 DEPOSIT TO HARNESS", use_container_width=True, type="primary"):
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
                st.success(f"Deposited. Your harness metrics just updated. artifact_hash: {res.get('artifact_hash', 'N/A')[:16]}...")

                if res.get("artifact_hash"):
                    st.markdown("**Artifact Hash**")
                    render_fingerprint_viz(res["artifact_hash"])

    # ============================================================
    #  REASON:// EXPLORER
    # ============================================================

    with st.container(border=True):
        st.markdown("### reason:// Explorer")

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
                placeholder="warf   or   warf/build   or   grok/build/test"
            )
            if uri_rest2 != st.session_state.explorer_prefix:
                st.session_state.explorer_prefix = uri_rest2

        uri = f"reason://{uri_rest2}" if uri_rest2 else None

        if st.button("🔍 RESOLVE / BROWSE PREFIX", use_container_width=True):
            if uri:
                # Try exact first
                art = RDNClient().resolve_from_registry(uri) if use_network else reason.resolve(uri)
                if art:
                    st.json(art)
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
    #  ACTIVITY
    # ============================================================

    st.subheader("Activity & Visuals")
    tab1, tab2 = st.tabs(["Recent Handoffs", "Fingerprint Gallery"])

    with tab1:
        # Simple recall (coherent API)
        c = RDNClient()
        recent = c.recall("handoff", limit=8)
        if recent:
            for item in recent:
                with st.expander(f"{item.get('project', 'unknown')} • {item.get('deposited_at', '')[:16]}"):
                    st.write(item.get("content", ""))
                    if item.get("artifact_hash"):
                        render_fingerprint_viz(item["artifact_hash"])
        else:
            st.info("No recent handoffs yet. Deposit something above.")

    with tab2:
        st.caption("Visualizations of local artifact hashes")
        # Demo gallery
        demo_hashes = [
            hashlib.sha256(b"demo1").hexdigest(),
            hashlib.sha256(b"demo2").hexdigest(),
            hashlib.sha256(b"demo3").hexdigest(),
        ]
        cols = st.columns(3)
        for i, h in enumerate(demo_hashes):
            with cols[i]:
                st.markdown(f"**Demo {i+1}**")
                render_fingerprint_viz(h)

    # Footer coherence note
    st.caption(
        "Local harness (this) + warf.astrognosy.com (WARF Gateway) + reason.astrognosy.com (Reason Registry). "
        "This public package does not expose private scoring internals."
    )

    if st.button("🔄 Refresh Stack"):
        st.rerun()


# ============================================================
#  THIN LAUNCHER — ONLY FOR CONSOLE SCRIPT / DIRECT EXECUTION
# ============================================================

def launch():
    """Console script entry point for `rdn-dash`.

    This is a *thin* launcher. It must never contain any Streamlit UI
    commands itself (no st.set_page_config, st.title, etc.).

    When you run `rdn-dash`, this tells Streamlit to run this file.
    Streamlit then re-imports the module in a properly initialized runtime
    and executes the real app code inside _app().
    """
    import sys

    from streamlit.web import cli as stcli

    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print("ReasonRDN dashboard")
        print("Run: rdn-dash")
        print("      streamlit run -m rdn.dash")
        return

    sys.argv = ["streamlit", "run", __file__]
    sys.exit(stcli.main())


if __name__ == "__main__":
    # When running directly with `python -m rdn.dash` or
    # `streamlit run rdn/dash.py`, call the actual app.
    # The console script (rdn-dash) calls launch() directly via the entry point.
    _app()
