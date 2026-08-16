# ReasonRDN canonical boundary

ReasonRDN is the public local-first memory product and reason:// on-ramp for
agents, developers, and teams.

## Owns

- Local memory, handoffs, and artifact integrity metadata.
- Public Python, CLI, MCP, dashboard, and embedded-node UX.
- reason:// parsing, local resolution, and explicit resolver selection.
- The public client adapter for WARF arbitration, selected-result Registry
  admission, and current or pinned Registry lookup.
- `rdn/protocol-lock.json`, the single public SDK and MCP contract.

## Related owners

- `monowarfo` owns the managed WARF Gateway, Reason Registry, deployment
  configuration, and working next-revision RFCXML.
- `astragnostic-api` owns protected scoring.
- `warf-edge` owns private matching and action selection.
- `xfer` owns private transfer technology for vertical products.
- IETF Datatracker owns the submitted Internet-Draft record.

## Separation rules

- Local is the default. Arbitration, admission, and Registry resolution are
  separate explicit network actions.
- reason:// addressing and local resolution do not require WARF.
- Astrognosy's managed Registry admits only content bound to an exact retained,
  selected WARF v2 event. Arbitration alone writes no Registry artifact.
- `reason-py` is private compatibility only and does not define a second
  artifact parser, version rule, or MCP contract.
- A URI is an address, not proof of quality or one mandatory global registry.
- Xchange and Xport remain compatibility identifiers, not separate products.
- Do not copy protected scoring or deployment internals into this public repo.

## Check

```powershell
python -m pytest -q
```
