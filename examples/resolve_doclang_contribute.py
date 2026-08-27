"""Resolve a task first, then queue one local document as DocLang on a miss."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional, Sequence

from rdn import RDNClient, prepare_doclang_contribution


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Local DocLang or Docling-supported file")
    parser.add_argument("--uri", required=True, help="Canonical reason:// task address")
    parser.add_argument(
        "--scope",
        choices=("local", "organization", "shared"),
        default="local",
        help="Maximum resolver and contribution scope (default: local)",
    )
    parser.add_argument(
        "--resolution-source",
        choices=("local", "registry", "chain"),
        default="chain",
        help="Resolution mode before conversion (default: chain)",
    )
    parser.add_argument(
        "--validation",
        choices=("auto", "structural", "reference"),
        default="auto",
        help="DocLang validation mode (default: auto)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    client = RDNClient()
    artifact = client.resolve(
        args.uri,
        source=args.resolution_source,
        scope=args.scope,
    )
    if artifact is not None:
        print(
            json.dumps(
                {
                    "outcome": "hit",
                    "reason_address": artifact.address,
                    "version": artifact.version,
                    "scope": args.scope,
                },
                indent=2,
            )
        )
        return 0

    prepared = prepare_doclang_contribution(args.source, validation=args.validation)
    queued = prepared.contribute(
        client,
        args.uri,
        scope=args.scope,
        background=False,
    )
    print(
        json.dumps(
            {
                "outcome": "queued",
                "reason_address": args.uri,
                "scope": args.scope,
                "contribution_id": queued["contribution_id"],
                "queue_status": queued["status"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
