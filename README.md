# ReasonRDN

**Resolve before reasoning. Contribute what is worth reusing.**

ReasonRDN lets agents keep useful decisions across sessions, assign stable
`reason://` addresses, resolve through configured local, organization, and
shared layers, and durably contribute reusable artifacts in the background.

WARF remains independently callable when competing submissions need
deterministic arbitration.

## The problem it solves

Agents can call tools through MCP and follow repository instructions through
AGENTS.md, but useful work still disappears between runs. The next agent often
has to rediscover what was decided, where the evidence came from, and which
artifact is current.

ReasonRDN provides the handoff layer:

- Remember and recall private context locally.
- Give reusable work a stable `reason://authority/category/task` address.
- Resolve from one exact source or an explicit layered resolver chain.
- Queue reusable improvements locally before any organization or shared write.
- Call WARF separately when an application needs arbitration.

## Quick start

```bash
rdn remember "Fixed the deployment race" --tags infra,deploy
rdn recall "deployment race"
```

Python:

```python
import rdn as reason

address = "reason://ops/deployment/rollback-plan"
artifact = reason.resolve(address, source="chain", scope="organization")

if artifact is None:
    answer = "Pause writes, restore, verify, then switch traffic."
    reason.contribute(
        answer,
        reason_address=address,
        scope="organization",
    )
else:
    answer = artifact.content
```

The default configured scope is `local`. Local contributions stay in the local
ledger and never POST. Organization and shared contributions enter the SQLite
queue before bounded delivery and retry.

## Windows setup

This PowerShell path does not require `py`, `pip`, or Python Scripts on `PATH`:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
& $python -m pip install -e '.[mcp]'
& $python -m pytest -q
& $python -m rdn.cli status
```

Register the local MCP server with Codex:

```powershell
$server = Join-Path (Split-Path $python -Parent) 'Scripts\rdn-mcp.exe'
codex mcp add reason-rdn -- $server
```

Select an organization resolver for the current PowerShell session:

```powershell
$env:REASON_RESOLUTION_SCOPE = 'organization'
$env:REASON_ORGANIZATION_RESOLVER = 'https://reason.example.org'
$env:REASON_ORGANIZATION_API_KEY = '<organization secret>'
```

No organization endpoint is assumed. Shared scope uses the configured shared
resolver or the Astrognosy reference Registry only when shared is selected.
Use `REASON_SHARED_API_KEY` for a credentialed shared resolver. The older
global Registry variables remain compatibility fallbacks; scope-specific keys
take precedence and are not sent to the other layer.

## CLI loop

```powershell
rdn resolve reason://ops/deployment/rollback-plan --source chain --scope organization
rdn contribute "Pause writes, restore, verify, then switch traffic." `
  --uri reason://ops/deployment/rollback-plan `
  --scope organization
rdn queue
rdn queue --flush
```

The short-lived CLI exits after its durable queue write unless `--flush` is
selected. Long-running SDK and MCP clients schedule a bounded background flush.

Contributions preserve exact bytes and explicit media types. XML is not parsed
or reserialized:

```powershell
rdn contribute --file .\artifact.xml `
  --media-type application/xml `
  --uri reason://agents/knowledge/artifact `
  --scope organization
```

Optional structured `context` and `adapter` objects provide a format-neutral
integration seam. The optional DocLang bridge preserves existing DocLang bytes
and records conversion and validation metadata without adding DocLang or
Docling as core dependencies. See
[`docs/DOCLANG-INTEROPERABILITY.md`](docs/DOCLANG-INTEROPERABILITY.md).

```powershell
& $python -m pip install -e '.[doclang]'
rdn contribute --file .\runbook.dclg --doclang `
  --doclang-validation auto `
  --uri reason://ops/deployment/rollback-plan `
  --scope organization
```

Install `.[documents]` when non-DocLang files should be converted through the
optional Docling adapter. Neither extra is installed with the core package.
The runnable
[`examples/resolve_doclang_contribute.py`](examples/resolve_doclang_contribute.py)
shows the full resolve-once, prepare-on-miss, queue-for-reuse loop without
fetching or publishing anything.

## Resolution behavior

`source="chain"` searches local, then organization, then shared, stopping at the
selected scope and the first verified hit. Exact `version="sha256:..."` requests
remain pinned throughout the chain.

Existing direct behavior is unchanged:

- `source="local"` reads only local memory.
- `source="registry"` reads only the Reason Registry.
- Neither direct mode silently falls through.

## One SDK and MCP lock

`rdn/protocol-lock.json` owns the public artifact, contribution, resolver, and
MCP contract. ReasonRDN 0.6 advertises exactly:

- `remember`
- `recall`
- `resolve`
- `contribute`
- `arbitrate`
- `status`

The 0.5 `admit` SDK and CLI operation remains as a low-level compatibility path
for callers that already hold exact WARF event custody. It is not an advertised
MCP tool. Current and pinned Registry resolution still uses the locked
nine-field `ReasonArtifact`.

## Components

```text
rdn/artifact.py          verified artifact and immutable version identity
rdn/contribution.py      exact-byte contribution envelope
rdn/client.py            local memory, resolver chain, and durable queue
rdn/reason.py            high-level Python API
rdn/cli.py               command line interface
rdn/mcp/server.py        six-tool MCP surface
rdn/doclang_adapter.py   optional DocLang and Docling bridge
rdn/node/server.py       embedded local memory node
rdn/handoff/             fingerprints and repository handoffs
```

## Ecosystem boundary

Reason is layered operating memory. WARF is independently usable deterministic
arbitration and event custody. Managed resolver services own convergence and
current-pointer transitions; protected scoring remains behind their interfaces.

WARF Edge, Edge Flow, and Xact are a separate Rust on-device advertising
matching domain. Xfer is a separate P2P/PSV structural exchange domain.

See [`docs/CANONICAL.md`](docs/CANONICAL.md) for the maintained product boundary
and [`docs/AAIF-INTEROPERABILITY.md`](docs/AAIF-INTEROPERABILITY.md) for the
portable agent-handoff concept.

## IETF work

The submitted versions remain on IETF Datatracker:

- [reason:// draft](https://datatracker.ietf.org/doc/draft-westerbeck-reason-protocol/)
- [WARF draft](https://datatracker.ietf.org/doc/draft-westerbeck-warf-protocol/)

This implementation update does not submit or replace the drafts.

## Development

```powershell
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
& $python -m pytest -q
```

Apache-2.0. See [LICENSE](LICENSE).
