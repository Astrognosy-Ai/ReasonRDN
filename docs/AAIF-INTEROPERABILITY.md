# Portable agent handoffs for the AAIF ecosystem

## Proposal

ReasonRDN and WARF can fill a narrow gap in the open agent stack: carrying a
useful, verifiable result from one run, agent, or framework into the next one.

The motivating question is simple:

> When an agent finishes work, how does the next agent know what was decided,
> which artifact is current, what validated it, and whether it can be reused
> without replaying the entire prior context?

## How it complements existing projects

- [MCP](https://github.com/modelcontextprotocol/modelcontextprotocol) connects
  models and agents to tools and data.
- [AGENTS.md](https://github.com/agentsmd/agents.md) gives coding agents
  predictable repository guidance.
- [goose](https://github.com/block/goose) executes local agent workflows.
- [agentgateway](https://github.com/agentgateway/agentgateway) governs service,
  LLM, and MCP traffic.
- **ReasonRDN** retains selected work locally and gives it a stable reason://
  address.
- **WARF** compares competing submissions under an exact scoring profile and
  returns a verifiable event record.

This is complementary infrastructure. It does not replace tool protocols,
workflow engines, gateways, agent runtimes, or project instructions.

## Small interoperability demonstration

1. Agent A completes a task and stores a handoff locally in ReasonRDN.
2. The handoff receives `reason://project/decision/task` plus a content digest
   and resolves locally without a network dependency.
3. Two agents propose competing updates. A WARF Gateway evaluates both under a
   named, versioned profile.
4. Agent C explicitly admits the selected content using the exact retained WARF
   event. Arbitration by itself writes no Registry artifact.
5. A second runtime resolves the returned immutable `sha256:` version from the
   Reason Registry and verifies the same content, digest, version, and
   validation record.

The demo passes when two independent clients agree on the canonical address and
WARF event, and pinned Registry resolution returns the identical verified
artifact. A public test profile can exercise interoperability; protected
scoring is not required for that demonstration.

## Contribution surfaces

- reason:// syntax and resolver-context tests.
- WARF request/result and event-record conformance fixtures.
- The locked ReasonRDN MCP surface: `remember`, `recall`, `resolve`, `arbitrate`,
  `admit`, and `status`.
- A cross-runtime demo using two AAIF ecosystem projects.
- Security notes for untrusted artifacts, resolver equivocation, stale results,
  and explicit network sharing.

The work most naturally intersects AAIF discussions around workflows and
process integration, accuracy and reliability, security and privacy, and
observability and traceability. See the
[AAIF organization](https://github.com/aaif) for the current project and
working-group structure.

## Boundary

Astrognosy is not claiming AAIF endorsement or project status. This is a
candidate contribution and interoperability experiment. WARF Edge matching,
Xfer vertical technology, and protected scoring are not required for the public
demo.
