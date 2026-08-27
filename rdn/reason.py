"""
rdn.reason - coherent high-level API for ReasonRDN.

This is the "one thing" you (and agents) should use.

It unifies:
- Local ReasonRDN handoffs (repo state, decisions, insights)
- Explicit arbitration through the WARF Gateway (warf.astrognosy.com)
- Durable scoped contribution to a Reason resolver
- Low-level selected-result admission compatibility
- Resolution from the Reason Registry (reason.astrognosy.com)

The public package talks to the Gateway and Registry boundaries. Protected
scoring internals are not implemented or described here.

Usage (the simple coherent way):
    import rdn.reason as reason

    reason.remember("Fixed the race using prior handoff context...", tags=["infra"])
    artifact = reason.resolve("reason://ops/deployment/ecs-failures")
    result = reason.network_arbitrate("best fix for X?", packages=[...])

Or the full client:
    from rdn.reason import Reason
    r = Reason(network=True)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .artifact import ReasonArtifact
from .client import REASON_REGISTRY_URL, WARF_GATEWAY_URL, RDNClient
from .config import env_flag

# Simple persistent metrics for the agnostic harness
METRICS_FILE = Path.home() / ".reason-rdn" / "harness_metrics.json"

class HarnessMetrics:
    """Lightweight, local-first metrics tracker for the reason harness.
    Tracks token savings estimates, velocity, ship rate, and positive signals.
    Agnostic to specific agents/models - works across the stack.
    """
    def __init__(self):
        self.path = METRICS_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> Dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except Exception:
                pass
        return {
            "total_handoffs": 0,
            "total_recalls": 0,
            "estimated_tokens_saved": 0,  # rough: each recall vs full re-reason ~1500-4000 tokens
            "sessions": 0,
            "vibe_stars": 0,
            "last_activity": None,
            "workflow_suggestions": [],
        }

    def _save(self):
        self.path.write_text(json.dumps(self.data, indent=2))

    def record_handoff(self, content: str, tags: List[str] = None, tokens_used: int = None):
        self.data["total_handoffs"] += 1
        self.data["last_activity"] = datetime.now(timezone.utc).isoformat()
        if tags and "positive" in [t.lower() for t in tags]:
            self.data["vibe_stars"] += 1
        if tokens_used:
            # Record the cost of creating this high-quality artifact
            self.data.setdefault("tokens_invested", 0)
            self.data["tokens_invested"] += tokens_used
        self._save()

    def record_recall(self, query: str, tokens_saved: int = None):
        self.data["total_recalls"] += 1
        if tokens_saved:
            self.data["estimated_tokens_saved"] += tokens_saved
        else:
            # Fallback rough estimate only when agents don't report real numbers
            self.data["estimated_tokens_saved"] += 2200
        self.data["last_activity"] = datetime.now(timezone.utc).isoformat()
        self._save()

    def record_xchange_share(self):
        self.data["vibe_stars"] += 2
        self._save()

    def get_velocity(self) -> float:
        """Handoffs per active day (very approximate)."""
        if not self.data["last_activity"]:
            return 0.0
        # simplistic
        return round(self.data["total_handoffs"] / max(1, (self.data["total_recalls"] or 1) / 5), 1)

    def get_ship_rate(self) -> float:
        """% of handoffs that led to recalls (proxy for usefulness)."""
        if self.data["total_handoffs"] == 0:
            return 0.0
        return round((self.data["total_recalls"] / self.data["total_handoffs"]) * 100, 1)

    def get_suggestions(self) -> List[str]:
        suggestions = []
        if self.data["total_recalls"] > 5 and self.data["estimated_tokens_saved"] > 10000:
            suggestions.append("High recall rate - you are benefiting from prior artifacts. Give reusable work a precise reason:// address and contribute it within the configured scope.")
        if self.data["vibe_stars"] > 3:
            suggestions.append("Strong positive signal. Contribute the reusable result, or call WARF separately when competing candidates need arbitration.")
        if self.get_ship_rate() < 20:
            suggestions.append("Ship rate low - add clearer project, tags, and state_tokens in handoffs to improve local recall precision.")
        self.data["workflow_suggestions"] = suggestions[:3]
        self._save()
        return self.data["workflow_suggestions"]

    def summary(self) -> Dict:
        return {
            "estimated_tokens_saved": self.data["estimated_tokens_saved"],
            "velocity": self.get_velocity(),
            "ship_rate": self.get_ship_rate(),
            "vibe_stars": self.data["vibe_stars"],
            "total_handoffs": self.data["total_handoffs"],
            "suggestions": self.get_suggestions(),
        }

_harness_metrics = HarnessMetrics()


# The single coherent high-level namespace (what agents and humans should reach for)
class Reason:
    """
    One coherent object for layered Reason memory plus optional WARF arbitration.

    Automatically handles the split:
    - local, organization, and shared contribution and resolver scopes
    - WARF Gateway (warf.astrognosy.com) for independent arbitration
    - Reason Registry (reason.astrognosy.com) for managed resolution
    """

    def __init__(self, xchange: bool = False, network: Optional[bool] = None):
        self._client = RDNClient()
        env_network = env_flag("REASON_USE_NETWORK", "REASON_USE_XCHANGE")
        self._xchange_mode = (
            xchange or env_network
            if network is None
            else network
        )
        if self._xchange_mode:
            self._client.broker_url = self._client.broker_url or WARF_GATEWAY_URL
            self._client.xport_url = self._client.xport_url or REASON_REGISTRY_URL

    def remember(self, content: str, **kwargs) -> Dict[str, Any]:
        """Remember locally; publication requires later arbitration and admission."""
        options = dict(kwargs)
        network_share = options.pop("network_share", None)
        compatibility_share = options.pop("xchange_share", None)
        if network_share is not None:
            should_share = bool(network_share)
        elif compatibility_share is not None:
            should_share = bool(compatibility_share)
        else:
            should_share = False
        uri = options.pop("uri", None) or options.pop("reason_address", None)

        local_options = dict(options)
        if uri:
            local_options["reason_address"] = uri
        local_result = self._client.remember(content, **local_options)
        if local_result.get("status") != "remembered":
            return local_result

        if not should_share:
            return local_result

        return {
            "status": "admission-required",
            "reason": "requires-arbitration",
            "address": uri or local_result.get("address"),
            "local_copy_retained": True,
            "local": local_result,
            "next": ["arbitrate", "admit"],
        }

    def resolve(
        self,
        uri_or_address: str,
        *,
        source: str = "local",
        scope: Optional[str] = None,
        version: Optional[str] = None,
        bypass_cache: bool = False,
    ) -> Optional[ReasonArtifact]:
        """Resolve from exactly ``local`` or ``registry``; never fall through."""
        return self._client.resolve(
            uri_or_address,
            source=source,
            scope=scope,
            version=version,
            bypass_cache=bypass_cache,
        )

    def contribute(self, content, *, reason_address: str, **kwargs):
        """Queue a reusable artifact in the configured Reason scope."""
        return self._client.contribute(
            content,
            reason_address=reason_address,
            **kwargs,
        )

    def network_arbitrate(self, query_text: str, packages: List[Dict[str, Any]], **kwargs):
        self._client.broker_url = self._client.broker_url or WARF_GATEWAY_URL
        return self._client.network_arbitrate(query_text, packages, **kwargs)

    def admit(self, artifact, arbitration, *, expected_current_version=None):
        """Explicitly request create-only or compare-and-set Registry admission."""
        self._client.xport_url = self._client.xport_url or REASON_REGISTRY_URL
        return self._client.admit(
            artifact,
            arbitration,
            expected_current_version=expected_current_version,
        )

    def xchange_arbitrate(self, query_text: str, packages: List[Dict[str, Any]], **kwargs):
        return self.network_arbitrate(query_text, packages, **kwargs)

    def list_prefix(self, prefix: str, limit: int = 20):
        """List artifacts under a reason:// prefix (great for browsing with partial URIs)."""
        return self._client.list_prefix(prefix, limit=limit)

    @property
    def status(self):
        current = {
            "network_mode": self._xchange_mode,
            "gateway": getattr(self._client, "broker_url", None),
            "registry": getattr(self._client, "xport_url", None),
            "network_available": bool(
                getattr(self._client, "broker_url", None)
                and getattr(self._client, "xport_url", None)
            ),
            "xchange_mode": self._xchange_mode,
            "broker": getattr(self._client, "broker_url", None),
            "xport": getattr(self._client, "xport_url", None),
            "local_available": self._client.available,
        }
        runtime_status = getattr(self._client, "runtime_status", None)
        if callable(runtime_status):
            current.update(runtime_status())
        return current


# Back-compat + advanced SDK bridge (thin, safe)
class ReasonClient(Reason):
    """Reason Registry-focused client."""
    def __init__(self, endpoint: str = None, **kwargs):
        super().__init__(xchange=False)
        self._client.xport_url = endpoint or REASON_REGISTRY_URL

    def resolve(
        self,
        uri_or_address: str,
        *,
        source: str = "registry",
        scope: Optional[str] = None,
        version: Optional[str] = None,
        bypass_cache: bool = False,
    ) -> Optional[ReasonArtifact]:
        return self._client.resolve(
            uri_or_address,
            source=source,
            scope=scope,
            version=version,
            bypass_cache=bypass_cache,
        )


class WARFClient(Reason):
    """Compatibility name for explicit Gateway arbitration and Registry admission."""
    def __init__(self, broker_endpoint: str = None, **kwargs):
        super().__init__(xchange=True)
        self._client.broker_url = broker_endpoint or WARF_GATEWAY_URL


# Module-level convenience (the "simplest coherent API")
_default_reason = None


def _network_result_succeeded(result: Dict[str, Any]) -> bool:
    status_value = str(result.get("status") or "").lower()
    return status_value in {"shared", "accepted", "ok", "success", "promoted"} or bool(
        result.get("audit_hash") and result.get("winner")
    )


def _get_default():
    global _default_reason
    if _default_reason is None:
        _default_reason = Reason()
    return _default_reason

def remember(content: str, tokens_used: int = None, **kwargs):
    """Coherent remember. Pass tokens_used=1234 for accurate harness accounting."""
    res = _get_default().remember(content, **kwargs)
    _harness_metrics.record_handoff(content, kwargs.get("tags"), tokens_used=tokens_used)
    network_share = kwargs.get("network_share")
    compatibility_share = kwargs.get("xchange_share")
    if network_share is not None:
        share_requested = bool(network_share)
    elif compatibility_share is not None:
        share_requested = bool(compatibility_share)
    else:
        share_requested = False
    if share_requested and _network_result_succeeded(res):
        _harness_metrics.record_xchange_share()
    return res

def resolve(
    uri_or_address: str,
    tokens_saved: int = None,
    *,
    source: str = "local",
    scope: Optional[str] = None,
    version: Optional[str] = None,
    bypass_cache: bool = False,
):
    """Coherent resolve. Pass tokens_saved=... (from the agent) for real metrics."""
    res = _get_default().resolve(
        uri_or_address,
        source=source,
        scope=scope,
        version=version,
        bypass_cache=bypass_cache,
    )
    _harness_metrics.record_recall(str(uri_or_address), tokens_saved=tokens_saved)
    return res


def contribute(content, *, reason_address: str, **kwargs):
    """Durably queue a reusable artifact for local, organization, or shared scope."""
    return _get_default().contribute(
        content,
        reason_address=reason_address,
        **kwargs,
    )

def network_arbitrate(query_text: str, packages: List[Dict[str, Any]], **kwargs):
    res = _get_default().network_arbitrate(query_text, packages, **kwargs)
    if _network_result_succeeded(res):
        _harness_metrics.record_xchange_share()
    return res


def admit(artifact, arbitration, *, expected_current_version=None):
    """Explicit arbitration-backed Reason Registry admission."""
    return _get_default().admit(
        artifact,
        arbitration,
        expected_current_version=expected_current_version,
    )

def xchange_arbitrate(query_text: str, packages: List[Dict[str, Any]], **kwargs):
    return network_arbitrate(query_text, packages, **kwargs)

def status():
    base = _get_default().status
    base.update(_harness_metrics.summary())
    base["recent_uris"] = get_recent_uris()
    # Top level prefixes from recent for quick visibility
    prefixes = set()
    for u in get_recent_uris()[:5]:
        if u.startswith("reason://"):
            parts = u[len("reason://"):].split("/")
            if parts:
                prefixes.add("reason://" + parts[0])
    base["top_prefixes"] = sorted(list(prefixes))[:5]
    return base

def harness_metrics():
    """Direct access to the agnostic harness metrics (tokens, velocity, suggestions, etc)."""
    return _harness_metrics.summary()


def list_prefix(prefix: str, limit: int = 20):
    """List artifacts currently registered under a reason:// prefix.
    Example: list_prefix("reason://warf") or list_prefix("warf") returns
    all known URIs starting with that prefix. Useful for autocomplete / browsing.
    """
    return _get_default().list_prefix(prefix, limit=limit)


# Recent URIs for quick access / persistence across sessions
RECENT_URIS_FILE = Path.home() / ".reason-rdn" / "recent_uris.json"

def _load_recent_uris():
    if RECENT_URIS_FILE.exists():
        try:
            data = json.loads(RECENT_URIS_FILE.read_text())
            return [u for u in data if isinstance(u, str) and u.startswith("reason://")][:10]
        except Exception:
            pass
    return []

def _save_recent_uris(uris):
    RECENT_URIS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RECENT_URIS_FILE.write_text(json.dumps(uris, indent=2))

_recent_uris = _load_recent_uris()

def add_recent_uri(uri: str):
    """Add a reason:// URI to the recent list (for quick access in dashboard/CLI)."""
    global _recent_uris
    if uri and uri.startswith("reason://"):
        if uri in _recent_uris:
            _recent_uris.remove(uri)
        _recent_uris.insert(0, uri)
        _recent_uris = _recent_uris[:10]
        _save_recent_uris(_recent_uris)

def get_recent_uris():
    """Return the list of recently used reason:// URIs."""
    return list(_recent_uris)


# Public record helpers for direct paths (CLI direct deposits, etc) so token accounting
# works without double-submitting artifacts. The high-level remember/resolve already use these.
def record_handoff(content: str, tags: List[str] = None, tokens_used: int = None):
    """Record a handoff for harness metrics.
    Use this from direct deposit paths (e.g. CLI using low-level handoff) to ensure
    accurate token accounting without re-performing the remember.
    Also used internally by the high-level remember/resolve wrappers.
    """
    _harness_metrics.record_handoff(content, tags, tokens_used)


def record_recall(query: str, tokens_saved: int = None):
    """Record a recall for harness metrics (tokens saved).
    Use this from direct recall/resolve paths to ensure accurate savings numbers.
    """
    _harness_metrics.record_recall(query, tokens_saved)

