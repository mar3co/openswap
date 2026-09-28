# Decision memo: control-service operator, hosting, and repository

**Status: DECIDED 2026-09-28 (Yohan Marshall).** See [Decision](#decision)
and [Owner record](#owner-record). The options and gate analysis below are
kept as the record of what was considered.

## Decision

OpenSwap supports a pluggable control service behind one configured URL. The
same versioned worker protocol serves three backends:

1. **OpenTag-hosted.** OpenTag operates its own implementation of the protocol
   on its existing hosting and identity, in the OpenTag repository. Pairing
   codes are issued from the OpenTag portal and bound to a workspace user.
2. **Self-hosted reference server.** OpenSwap ships a minimal MIT reference
   server in this repository: single owner, plain HTTP API, no dependencies
   beyond Python and its standard library, deployable on any host the owner
   controls. It issues pairing codes to whoever runs it.
3. **Custom server.** Anyone may implement the published protocol
   specification; OpenSwap treats it like any other backend.

OpenSwap itself gains no accounts or login. Identity on the Mac is always a
one-use pairing code approved locally and exchanged for a device key stored
under the existing `openswap` Keychain service. Who may issue codes, and how
that maps to people and workspaces, is the backend's concern.

Consequences for the plan:

- The protocol specification, worker client and reference server live in
  `mar3co/openswap`. OpenTag's implementation lives in `mar3co/opentag` and
  is tracked in mar3co/opentag#135. Nothing OpenTag-specific enters this
  repository.
- Phase 3's "durable job service" is the reference server, a product
  deliverable rather than a throwaway; the pilot runs against a self-hosted
  instance and needs no OpenTag account.
- Phase 4 begins with OpenTag implementing the protocol server-side, then the
  connector work already listed.
- The operator of each backend owns its data, retention, availability and
  support. For the reference server that is the person who runs it.
- The worker must poll on short intervals rather than hold long-lived
  connections, so the protocol stays deployable on serverless hosting such as
  OpenTag's.

## Decision needed

Record all three together:

1. Who operates and supports the control service, including incident response,
   access control, retention/deletion, and service availability.
2. Where it is hosted and who owns its cloud account, production credentials,
   data processing, and costs.
3. Which repository owns the service implementation and release process.

The service will process submitted prompts, job metadata, and selected result
artifacts; the worker retains provider credentials locally. Hosting and
retention therefore determine a real data boundary, not only deployment
convenience.

The current phase-one evidence is tracked in [017 progress](../../017-progress.md).
The synthetic seatbelt test there proves only a low-level local filesystem
boundary; it does not choose a service operator, approve a hosting provider, or
authorize service code.

## Options

| Option | Benefits | Costs and boundaries |
| --- | --- | --- |
| OpenTag operator; OpenTag-hosted service in the OpenTag repository | Aligns with OpenTag's authenticated user/workspace and MCP client; keeps control-plane ownership with the first client. | Adds a security-sensitive service, data retention, on-call/support, and cost obligations to OpenTag. OpenSwap must depend on a versioned public contract, not private implementation details. This was the earlier research recommendation, not an owner decision. |
| OpenSwap operator; service hosted with OpenSwap infrastructure and in the OpenSwap repository | One organization owns local worker and service releases. | Creates control-plane, tenancy, privacy, operations, and support obligations for OpenSwap; duplicates some client identity concerns. |
| Third-party hosted operator or managed service | May reduce custom infrastructure work. | Introduces a separate data processor and owner, contractual/security review, ongoing cost, and a dependency that neither repository controls. |
| User/self-hosted service | Gives an operator control over data location and availability. | Increases deployment, upgrades, support burden, and configuration complexity; a self-hosted relay still does not replace job authorization on the worker. |

## Recommendation and gate

_Superseded by the Decision above; kept as the pre-decision analysis._

Favor OpenTag operating the service and owning its repository, consistent with
plan 017's recommendation: OpenTag is the first client and already owns the
tenant identity and MCP request surface. This is a recommendation, not the
owner's decision. The owner still needs to choose the hosting provider and
cloud account after selecting an operator, including its data, retention,
support, and cost terms. If the owner selects a different operator, the
repository should follow that operator. Keep any phase-3 reference package
clearly separated and movable behind an interface until the decision is
recorded; it must not quietly establish production hosting or ownership.

Plan 017's supporting research recommended OpenTag as the control-service
operator and its repository for the implementation, while hosting must follow
the owner's recorded choice. This is not the owner's answer and does not
authorize production deployment. Phase 1 remains blocked until the owner
records operator, hosting, and repository. Phase 3 remains unstarted until
phase 2 exits and is merged as well.

## Owner record

- Operator: the operator of whichever backend the owner points the worker at.
  OpenTag (mar3co) operates the OpenTag-hosted backend; a self-hosting owner
  operates their own reference-server instance.
- Hosting: OpenTag backend on OpenTag's existing hosting and identity
  provider; reference server on any host the owner controls.
- Repository: protocol specification, worker client and reference server in
  `mar3co/openswap`; OpenTag's implementation in `mar3co/opentag`.
- Decision date / owner: 2026-09-28 / Yohan Marshall
