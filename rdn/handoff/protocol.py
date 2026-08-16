"""
rdn/handoff/protocol.py

High-level handoff orchestrator.

This layer captures handoff context from operators and agents, produces a
stable local integrity hash, and deposits the result via the unified memory
client.

Public consumers interact with:
- ReasonRDN.deposit_handoff(...)
- ReasonRDN.resolve_handoff(...)
- The "artifact_hash" field present in returned artifacts
"""

from __future__ import annotations

from typing import Dict, List, Optional

from rdn.client import RDNClient

from .fingerprint import ArtifactFingerprint


class ReasonRDN:
    """
    Orchestrates high-utility, privacy-preserving handoffs between agents,
    sessions, and the desktop operator.

    Uses a local artifact hash for integrity metadata. Arbitration and optional
    Registry admission are separate explicit operations.
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        node_url: Optional[str] = None,
        mirror_local: Optional[bool] = None,
    ):
        self._fingerprint = ArtifactFingerprint()
        self.client = RDNClient(
            db_path=db_path,
            node_url=node_url,
            mirror_local=True if mirror_local is None else mirror_local,
        )

    def deposit_handoff(
        self,
        project: str,
        summary: str,
        state_tokens: List[str],
        tags: Optional[List[str]] = None,
    ) -> Dict:
        """
        Deposit a handoff artifact.

        A stable artifact hash is computed from the provided state_tokens and
        stored under the `artifact_hash` key in the artifact metadata. This is
        a local integrity aid only.
        """
        fingerprint = self._fingerprint.compute(state_tokens)

        meta = {
            "protocol": "ReasonRDN/v1",
            "artifact_hash": fingerprint,
            "integrity": "verified",
            "fingerprint_strength": self._fingerprint.strength(state_tokens),
        }

        final_tags = tags or ["handoff", "ReasonRDN"]
        result = self.client.remember(
            content=summary,
            tags=final_tags,
            project=project,
            meta=meta,
        )
        if isinstance(result, dict):
            result.setdefault("artifact_hash", fingerprint)
        return result

    def resolve_handoff(self, project: str) -> Optional[Dict]:
        """
        Return the most recent handoff artifact for the project.

        Callers can inspect the returned artifact metadata to decide whether
        the handoff is still relevant.
        """
        results = self.client.recall(
            query="handoff",
            tags=["ReasonRDN", "handoff"],
            project=project,
            limit=5,
        )

        if not results:
            return None

        return results[0]
