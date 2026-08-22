# ReasonRDN canonical boundary

ReasonRDN is the public agent client for layered `reason://` operating memory.
Its ordinary loop is resolve before expensive reasoning, then contribute useful
work within the configured local, organization, or shared scope.

## Owns

- Local memory, handoffs, and artifact integrity metadata.
- Public Python, CLI, MCP, dashboard, and embedded-node UX.
- Canonical `reason://` parsing and the verified nine-field `ReasonArtifact`.
- Exact local and Registry resolution, including immutable version pinning.
- The explicit local to organization to shared resolver chain.
- The format-neutral contribution envelope and deterministic contribution ID.
- Durable local contribution custody, bounded background delivery, retries, and
  queue inspection.
- Optional public adapter seams, including DocLang interoperability, without a
  hard format dependency in the Reason core.
- `rdn/protocol-lock.json`, the single public SDK and MCP contract.

## Implemented client loop

1. `resolve(..., source="chain")` checks local memory and then configured
   resolver layers up to the selected scope.
2. A verified hit returns the current or exact pinned artifact unchanged.
3. `contribute(...)` preserves exact source bytes, media type, digest, bounded
   context, and adapter metadata in a deterministic envelope.
4. Local scope retains the envelope locally and performs no HTTP write.
5. Organization and shared scopes write to SQLite before bounded background or
   explicit delivery to the selected resolver.
6. Delivery uses the contribution ID as its idempotency key and retains the
   result or schedules bounded retry.

The client submits contributions. The selected resolver owns convergence,
immutable event creation, and any compare-and-set current-pointer transition.

## Public operations

The advertised MCP surface is exactly:

- `remember`
- `recall`
- `resolve`
- `contribute`
- `arbitrate`
- `status`

The `admit` SDK and CLI path remains callable for 0.5 compatibility when a
caller already holds exact arbitration custody. It is not the ordinary agent
contribution action and is not an advertised MCP tool.

## Related owners

- `monowarfo` owns managed Reason Registry convergence, current-pointer
  transitions, WARF Gateway service behavior, deployment configuration, and
  working next-revision RFCXML.
- `astragnostic-api` owns protected scoring and advancement policies.
- WARF Arbitration is an associated but independently callable deterministic
  selection and event-custody product.
- WARF Edge, Edge Flow, and Xact are a detached Rust on-device cosine-pairing
  and advertising-matching product domain.
- Xfer, P2P, and PSV are a detached structural exchange product domain.
- IETF Datatracker owns the submitted Internet-Draft record.

## Separation rules

- Local is the initial configured scope. Selecting organization or shared scope
  is what authorizes the client to queue delivery to that layer.
- Local contributions never POST, including when flush is requested.
- Direct `source="local"` and `source="registry"` behavior remains exact and
  never falls through. Only `source="chain"` uses layered lookup.
- A URI is a stable task address. It is not proof of universal truth and does
  not require one mandatory global Registry.
- Best-current means the reproducible result for one exact resolver context,
  contribution set, evidence snapshot, profile, and policy.
- ReasonRDN transports exact bytes and explicit media types. It does not parse
  or reserialize XML or require DocLang or Docling in the core package.
- Managed Registry artifacts may carry either retained WARF event validation or
  a retained convergence event. Both methods remain part of immutable artifact
  version identity.
- Protected scoring, convergence thresholds, service credentials, and managed
  deployment internals do not belong in this public repo.
- Edge/Flow/Xact and Xfer/P2P/PSV may connect through adapters but remain
  separately owned product domains.

## Check

```powershell
$python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
& $python -m pytest -q
```
