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

artifact = client.resolve(address)
if artifact is None:
    client.contribute(
        "Enable the reviewed network setting, then verify an ECR pull.",
        reason_address=address,
        tags=["infra", "ecs", "networking"],
    )
    artifact = client.resolve(address)
print(artifact.content)
```

With the default local scope the answer is saved on this computer and can be
looked up right away. Pass `scope="organization"` (with an organization server
set up) to also queue it for your team; it is saved locally first and sent in
the background.

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

```bash
python examples/resolve_doclang_contribute.py examples/runbook.dclg \
  --uri reason://ops/runbook/rollback \
  --scope local
```

`examples/runbook.dclg` is a tiny sample file included so this runs as is.
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

```bash
rdn remember "Useful deployment note" --project ops --tags deploy,ecs
rdn recall "deployment note" --project ops
rdn contribute "Enable the reviewed setting, then verify" \
  --uri reason://ops/ecr/private-pull-failures
rdn resolve reason://ops/ecr/private-pull-failures

# Team sharing, once an organization server is set up
# (export REASON_ORGANIZATION_RESOLVER=https://reason.example.org):
rdn resolve reason://ops/ecr/private-pull-failures --source chain --scope organization
rdn contribute "Enable the reviewed setting, then verify" \
  --uri reason://ops/ecr/private-pull-failures \
  --scope organization
rdn queue
rdn queue --flush
```

On Windows PowerShell, put each command on one line or end lines with a
backtick instead of `\`.
## Security notes

- Local memory and local contributions remain on the local machine by default.
- Organization and shared contribution writes require selecting that scope.
- Resolver credentials come from environment or local configuration.
- This public package does not contain protected scoring or convergence policy.
