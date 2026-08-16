"""Canonical local reason:// address construction."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from .artifact import validate_reason_address


def project_label(project: str) -> str:
    """Turn a project name into one lowercase reason:// authority label."""
    ascii_name = (
        unicodedata.normalize("NFKD", str(project))
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    label = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    label = re.sub(r"^[^a-z]+", "", label)
    label = (label or "local")[:63].rstrip("-")
    return label or "local"


def project_address(project: str, content: str) -> str:
    """Build a stable local handoff address for a project and content value."""
    task_slug = f"h-{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}"
    return validate_reason_address(
        f"reason://{project_label(project)}/handoff/{task_slug}"
    )
