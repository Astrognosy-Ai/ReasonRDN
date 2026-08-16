# ReasonRDN

**Local-first memory and verifiable handoffs for agents.**

ReasonRDN lets agents keep useful decisions across sessions, assign stable
`reason://` addresses, and explicitly use WARF when competing submissions need
arbitration.

```powershell
pip install 'reason-rdn[full]'
rdn start
```

## The problem it solves

Agents can call tools through MCP and follow repository instructions through
AGENTS.md, but useful work still disappears between runs. The next agent often
has to rediscover what was decided, where the evidence came from, and which
artifact is current.

ReasonRDN provides the handoff layer:

- Remember context locally by default.
- Give reusable work a stable `reason://authority/category/task` address.
- Resolve it later from an explicit local or network registry.
- Submit competing candidates to WARF and retain the decision record.
- Keep network participation opt-in.

## Quick start

```bash
rdn remember "Fixed the deployment race" --tags infra,deploy
rdn recall "deployment race"
```

Python:

```python
import rdn as reason

reason.remember(
    "Pause writes, restore, verify, then switch traffic.",
    tags=["ops", "rollback"],
    reason_address="reason://ops/deployment/rollback-plan",
)

artifact = reason.resolve("reason://ops/deployment/rollback-plan")
assert artifact.source == "local"
```

Local data lives under `~/.reason-rdn/`.

## Managed round trip

The complete network path is explicit and reversible:

```python
from rdn import RDNClient, ReasonArtifact

address = "reason://ops/deployment/rollback-plan"
client = RDNClient()

# 1. Local retention never publishes.
client.remember(
    "We need a rollback plan that preserves data integrity.",
    project="ops",
    reason_address=address,
)

answers = {
    "candidate-a": "Pause writes, restore, verify, then switch traffic.",
    "candidate-b": "Switch traffic immediately.",
}
evidence = "The runbook requires verification before traffic changes."

# 2. Arbitration compares candidates and retains an event. It does not admit.
decision = client.network_arbitrate(
    "Choose the safest rollback plan.",
    [
        {
            "agent_id": candidate_id,
            "answer_text": answer,
            "corpus": [{"doc_id": "runbook", "text": evidence}],
        }
        for candidate_id, answer in answers.items()
    ],
    reason_address=address,
)

if decision["status"] == "selected":
    selected = answers[decision["winner_submission_id"]]
    artifact = ReasonArtifact.from_dict(
        {
            "address": address,
            "media_type": "text/plain; charset=utf-8",
            "content": selected,
        },
        source="local",
    )
    custody = {
        "query_id": decision["query_id"],
        "winner_submission_id": decision["winner_submission_id"],
        "event_record": decision["event_record"],
        "audit_hash": decision["audit_hash"],
    }

    # 3. Null is create-only. Updates name the exact prior sha256: version.
    admitted = client.admit(
        artifact,
        custody,
        expected_current_version=None,
    )

    # 4. Pinning the returned version resolves the same immutable bytes.
    pinned = client.resolve(
        address,
        source="registry",
        version=admitted.version,
    )
    assert pinned.to_dict() == admitted.to_dict()
```

Only three operations use the managed network: `arbitrate`, `admit`, and
`resolve(..., source="registry")`. Remember, recall, and default resolution stay
local. There is no automatic fallback, publication, or admission.

Managed endpoints:

- `https://warf.astrognosy.com` is the WARF Gateway.
- `https://reason.astrognosy.com` is the Reason Registry.
- `https://xport.astrognosy.com` remains a compatibility hostname.

The former Xchange and Xport names remain hidden compatibility aliases. New
applications use arbitration, admission, Gateway, and Registry language.

## One SDK and MCP lock

`reason-rdn` 0.5 and `rdn/protocol-lock.json` own the public artifact shape,
address grammar, parser, version identity, and MCP contract. The advertised MCP
tools are exactly `remember`, `recall`, `resolve`, `arbitrate`, `admit`, and
`status`. The private `reason-py` package is a compatibility facade and delegates
canonical parsing to this lock.

## Components

```text
rdn/client.py          local and network client
rdn/reason.py          high-level Python API
rdn/cli.py             command line interface
rdn/mcp/server.py      MCP tools
rdn/node/server.py     embedded local memory node
rdn/dash.py            optional dashboard
rdn/handoff/           fingerprints and repository handoffs
```

## How the ecosystem fits together

ReasonRDN is the local memory and client layer. The WARF Gateway and Reason
Registry provide managed arbitration and resolution; the WARF Scoring Service
supplies selected scoring profiles; WARF Edge adds matching and action
selection; and Xfer powers domain-specific verticals.

Applications can start with ReasonRDN alone and add WARF arbitration when a
handoff has competing submissions. A `reason://` URI supplies the stable
address. The Registry current pointer is convenient; a returned `sha256:`
version pins immutable content and validation bytes.

## IETF work

The submitted versions remain authoritative on IETF Datatracker:

- [reason:// draft](https://datatracker.ietf.org/doc/draft-westerbeck-reason-protocol/)
- [WARF draft](https://datatracker.ietf.org/doc/draft-westerbeck-warf-protocol/)

Working RFCXML and rendered artifacts for the next revisions are maintained
with the reference network implementation and reviewed before submission. The
current working revision is local and unsubmitted.

## AAIF interoperability concept

The focused open-source contribution is portable agent handoffs after a tool or
workflow finishes. See
[`docs/AAIF-INTEROPERABILITY.md`](docs/AAIF-INTEROPERABILITY.md) for the small
cross-runtime demo and its boundary.

## Development

```powershell
pip install -e '.[full]'
python -m pytest -q
```

## License

Apache-2.0. See [LICENSE](LICENSE).
