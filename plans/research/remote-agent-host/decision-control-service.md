# Decision memo: control-service operator, hosting, and repository

**Status: PENDING OWNER DECISION.** Plan 017 requires this decision to be
recorded before phase 1 exits and before any phase-3 service or transport work
starts. This memo presents options; it does not choose an operator, provider,
hosting environment, or repository for the owner.

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

- Operator: **Pending**
- Hosting: **Pending**
- Repository: **Pending**
- Decision date / owner: **Pending**
