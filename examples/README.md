# ReasonRDN examples

Resolve before repeating expensive work, then contribute the reusable result.
WARF arbitration remains a separate action for competing candidates.

## Agent prompts

```text
Before solving this, resolve reason://<domain>/<category>/<task> through the configured scope.
```

```text
Contribute this reusable solution under reason://ops/deployment/ecs-task-failures.
```

## Resolve and contribute

```python
from rdn import RDNClient

client = RDNClient()
address = "reason://ops/ecr/private-pull-failures"

artifact = client.resolve(address, source="chain", scope="organization")
if artifact is None:
    result = client.contribute(
        "Enable the reviewed network setting, then verify an ECR pull.",
        reason_address=address,
        scope="organization",
        tags=["infra", "ecs", "networking"],
    )
```

The SDK writes the contribution to SQLite before its bounded background
delivery attempt. Local scope retains the contribution and never POSTs.

## Exact documents

```python
from pathlib import Path
from rdn import RDNClient

RDNClient().contribute(
    Path("runbook.xml").read_bytes(),
    reason_address="reason://ops/runbook/rollback",
    scope="organization",
    media_type="application/xml",
)
```

The exact XML bytes are base64-encoded for transport and bound to their SHA-256
digest. ReasonRDN does not reserialize the document.

Run the same resolve-before-convert loop with a local DocLang or
Docling-supported file:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
& $python .\examples\resolve_doclang_contribute.py .\runbook.dclg `
  --uri reason://ops/runbook/rollback `
  --scope local
```

Local is the default scope. The example performs no fetch or publication.

## Independent WARF arbitration

```python
decision = client.network_arbitrate(
    "Choose the supported ECR recovery.",
    [
        {"agent_id": "candidate-a", "answer_text": "Verify, then switch."},
        {"agent_id": "candidate-b", "answer_text": "Switch immediately."},
    ],
    reason_address=address,
)
```

Arbitration returns retained event custody. Managed convergence may use WARF
when comparing eligible candidates, but an ordinary agent contributes without
manually performing low-level admission. The 0.5 `admit` SDK and CLI call
remains available for compatibility.

## CLI

```powershell
rdn remember "Useful deployment note" --project ops --tags deploy,ecs
rdn recall "deployment note" --project ops
rdn resolve reason://ops/ecr/private-pull-failures --source chain --scope organization
rdn contribute "Enable the reviewed setting, then verify" `
  --uri reason://ops/ecr/private-pull-failures `
  --scope organization
rdn queue
rdn queue --flush
```

## Security notes

- Local memory and local contributions remain on the local machine by default.
- Organization and shared contribution writes require selecting that scope.
- Resolver credentials come from environment or local configuration.
- This public package does not contain protected scoring or convergence policy.
