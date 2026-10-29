# ADR 0006: Stay independent of OpenShell and keep the core lean

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** didericis

## Context

NVIDIA's OpenShell is the closest project to bot-bottle: a sandbox runtime with
an L7 egress proxy, credentials swapped in at the boundary, declarative policy,
and (since mid-2026) workspaces, OIDC and roles in its open-source core. A
source review on 2026-10-17
(research note *nvidia-nemoclaw-openshell*, addendum; design
repo) found it at ~634k lines of Rust across 37 crates, with Docker,
Podman, microVM and Kubernetes drivers, and an extension point for external
traffic-inspection services.

That raised two options: build on top of OpenShell (ship bot-bottle's
protections as OpenShell middleware and services), or stay a separate runtime.
It also raised the pull to match OpenShell's breadth — a Kubernetes backend,
more compute drivers, enterprise identity in the core — as customers ask for
those things.

What bot-bottle has that OpenShell lacks is narrow but real: outbound secret
scanning with supervise / redact / block, gitleaks-scanned pushes with the
upstream credential held outside the bottle, inbound prompt-injection
detection, bottles that a cloned repo can't override, and native macOS. Those
depend on being small enough to audit and quick to change, not on breadth.

## Decision

1. **bot-bottle stays a standalone runtime.** It takes no dependency on
   OpenShell and an OpenShell backend is not on the roadmap. The secret
   scanner keeps a clean internal interface so that wrapping it as an
   OpenShell middleware service stays cheap if we ever want to; that is an
   option, not a commitment.
2. **The core stays lean.** The built-in backends remain Apple Container,
   Firecracker and Docker. The core does **not** take on a Kubernetes backend,
   operator or Helm chart, additional compute drivers, or an enterprise
   identity / role system.
3. **Identity belongs in the console.** Single sign-on (OIDC), roles and team
   features are console concerns, not CLI or gateway concerns. OIDC sign-in is
   the first of these (console PRD *OIDC sign-in*).
4. **Kubernetes is bring-your-own orchestration.** Teams that run agents on a
   cluster own the orchestration: scheduling, namespaces, quotas, runtime class
   (gVisor / Kata), NetworkPolicy and secrets plumbing. bot-bottle supplies the
   pieces that are genuinely ours — published images, a documented runtime
   contract between agent, gateway and orchestrator, a way to register a bottle
   and get its agent configuration, and a verifier that checks from inside an
   agent that the egress chokepoint actually holds. Reference manifests may
   ship as examples; they are not a supported install path. The details go in
   a PRD.
5. **We compete on exfiltration control and developer experience:** the
   leak-prevention features above, fail-closed defaults, and a zero-config,
   native local workflow.

## Consequences

- **Good:** a codebase a security reviewer can read end to end; fast changes to
  the manifest format and defaults; effort goes to the features that
  differentiate rather than to parity.
- **Good:** Kubernetes users keep full control of their own orchestration
  instead of fighting an opinionated operator, and we carry no cluster-specific
  code.
- **Cost:** no ride on NVIDIA's distribution (Red Hat, Canonical, Dell). The
  zero-config first run and a clear "secrets can't leak" story have to earn
  adoption instead.
- **Cost:** requests for Kubernetes, more drivers or core RBAC get "no" or "use
  the console / bring your own". This record is the thing to point at.
- **Cost:** a bring-your-own cluster deployment is only as safe as the
  operator's NetworkPolicy and CNI. The verifier in (4) exists so that is
  checked rather than assumed.
- **Follow-up:** two OpenShell ideas are worth adopting in a lean form, each
  as its own PRD — egress rules scoped to the calling binary, and an approval
  flow that reports how much a new route would open before it is granted.

## Links

- Research note *nvidia-nemoclaw-openshell* (design repo) — prior-art note
  and the 2026-10-17 source-review addendum
- Research note *agent-sandbox-landscape* (design repo)
- PRD 0070 (design repo) — the shared gateway and the
  source-IP attribution invariant a cluster deployment has to honour
