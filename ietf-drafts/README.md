# IETF Datatracker links

Submitted Internet-Draft versions are authoritative on IETF Datatracker:

- [draft-westerbeck-reason-protocol](https://datatracker.ietf.org/doc/draft-westerbeck-reason-protocol/)
- [draft-westerbeck-warf-protocol](https://datatracker.ietf.org/doc/draft-westerbeck-warf-protocol/)
- [Jacob Westerbeck's Datatracker profile](https://datatracker.ietf.org/person/jacob%40pcfic.com)

ReasonRDN does not carry unpublished draft source. Private working RFCXML for
the next revisions lives with the canonical reference-network implementation
until a separate review and submission action.

The intended next-revision boundary is simple:

- reason:// specifies stable task addressing, local/organization/shared
  resolver layers, current and pinned artifacts, durable contribution, and
  deterministic URI-scoped convergence.
- WARF specifies independently usable arbitration, exact scoring-profile
  binding, deterministic tie-breaking, and event-record verification.
- A Reason resolver may compose a retained WARF event with a separately
  attested advancement profile, then own the convergence event and current
  pointer transition. WARF does not require or mutate a Reason Registry.
- Protected profile formulas remain behind versioned attestations. WARF Edge's
  Edge/Flow/Xact matching domain and Xfer's P2P/PSV exchange domain remain
  separately owned systems.

ReasonRDN's public protocol lock implements the next Reason boundary through
one artifact type, the local/organization/shared resolver chain, an idempotent
contribution envelope and receipt, and the six agent actions `remember`,
`recall`, `resolve`, `contribute`, `arbitrate`, and `status`.

These documents are work in progress, not approved RFCs. The next RFCXML and
renders remain private, local, and unsubmitted until a separate review and
submission decision.
