# ReasonRDN Examples

This directory shows how to retain work locally, arbitrate competing candidates,
admit one selected result, and resolve it by immutable `reason://` version.

## Architecture Note

- Local memory works without network access.
- The WARF Gateway at `https://warf.astrognosy.com` arbitrates at least two
  candidates and retains the exact event record.
- The Reason Registry at `https://reason.astrognosy.com` resolves `reason://`
  addresses and accepts a separate authenticated admission request.
- Arbitration never writes the Registry. Remember and default resolve never use
  the network.

## Agent Prompts

```text
Install ReasonRDN and use the rdn CLI or MCP tools to remember durable project context.
```

```text
Before solving this, resolve reason://<domain>/<category>/<task> to see whether useful prior context exists.
```

```text
Remember this solution under reason://ops/deployment/ecs-task-failures with tags ops,ecs,networking.
```

## Local Deposit

```python
from rdn.handoff import ReasonRDN

rdn = ReasonRDN()
rdn.deposit_handoff(
    project="my-team",
    summary="ECS tasks need assignPublicIp: ENABLED for ECR pulls in private subnets",
    state_tokens=["ecs", "ecr", "private-subnet", "assignPublicIp"],
    tags=["infra", "ecs", "networking"],
)
```

## Arbitrate, admit, resolve

```python
from rdn import RDNClient, ReasonArtifact

client = RDNClient()
address = "reason://ops/ecr/private-pull-failures"
answers = {
    "candidate-a": "Enable the reviewed network setting, then verify an ECR pull.",
    "candidate-b": "Restart every task without changing network configuration.",
}

decision = client.network_arbitrate(
    "Choose the supported ECR recovery.",
    [
        {
            "agent_id": candidate_id,
            "answer_text": answer,
            "corpus": [{"doc_id": "runbook", "text": "Verify the network setting before restart."}],
        }
        for candidate_id, answer in answers.items()
    ],
    reason_address=address,
)

if decision["status"] == "selected":
    selected = ReasonArtifact.from_dict(
        {
            "address": address,
            "media_type": "text/plain; charset=utf-8",
            "content": answers[decision["winner_submission_id"]],
        },
        source="local",
    )
    admitted = client.admit(
        selected,
        {
            "query_id": decision["query_id"],
            "winner_submission_id": decision["winner_submission_id"],
            "event_record": decision["event_record"],
            "audit_hash": decision["audit_hash"],
        },
        expected_current_version=None,
    )
    resolved = client.resolve(
        address,
        source="registry",
        version=admitted.version,
    )
    assert resolved.to_dict() == admitted.to_dict()
```

## CLI

```bash
rdn remember "Useful deployment note" --project ops --tags deploy,ecs
rdn recall "deployment note" --project ops
rdn --network arbitrate "Choose the supported recovery" \
  --uri reason://ops/ecr/private-pull-failures \
  --package candidate-a:"Enable the reviewed setting, then verify" \
  --package candidate-b:"Restart without changing the setting"
rdn admit @artifact.json @arbitration.json
rdn resolve reason://ops/ecr/private-pull-failures \
  --source registry --version sha256:<64-lowercase-hex>
rdn-sync --once --install-hooks
```

## Security Notes

- The local node is localhost-only by default.
- Use the configured Gateway credential for arbitration and
  `REASON_REGISTRY_API_KEY` for admission.
- This public package contains client and local-memory code only.
