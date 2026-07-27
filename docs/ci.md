# CI

## Required pull-request gate

[`.gitea/workflows/test.yml`](../.gitea/workflows/test.yml) runs the unit
suite, Docker integration suite, combined coverage report, and diff-coverage
gate when tested package/build inputs change on a pull request or on `main`.

The Docker job preflights the backend before discovery. Gitea's `act_runner`
runs the job in a container with the host Docker socket, so the test process
reaches control-plane siblings through the job's Docker network and uses named
Docker volumes for orchestrator/CA state the host daemon must mount. The
orchestrator runs the package baked into the image built from the checkout; it
does not bind the job container's invisible workspace into a sibling container.
Docker integration jobs share fixed singleton names, so required and manual
runs use one non-cancelling concurrency group. The shared agent/gateway network
has an explicit subnet, which Docker requires for the pinned source IPs used as
the isolation/attribution key.

`scripts.unittest_gate` enforces the Docker job's contract: all 22 integration
tests must execute and none may skip. This includes the real gateway-image,
control-plane authentication, multitenant policy/token isolation,
sandbox-escape, and orphan-network tests. Backend skip decorators remain useful
for local runs, but the CI preflight plus execution-count gate prevents a
missing backend or runner-topology regression from becoming a green job.

Combined unit + Docker coverage is informational globally. Two focused gates
are enforced:

- changed executable Python lines must be at least 90% covered; and
- the validated critical security/logic core must remain at least 90% covered.

## Privileged pre-release matrix

[`.gitea/workflows/pre-release-test.yml`](../.gitea/workflows/pre-release-test.yml)
is manually dispatched before a release. It repeats unit and Docker integration
coverage, then runs:

- Firecracker integration on the self-hosted `kvm` runner; and
- advisory Apple Container integration on the self-hosted `macos` runner.

These privileged host-mode runners never execute unreviewed pull-request code
automatically. Firecracker coverage is combined in the manual pre-release
report; macOS reports advisory coverage in its own job. The macOS infra
container is a singleton, so its job uses a concurrency group and always tears
the service down.

## Artifact publication and promotion

[`publish-artifacts.yml`](../.gitea/workflows/publish-artifacts.yml) is the
manual deployment-test boundary. Dispatch it with any branch, tag, or commit;
it resolves that input once, publishes the orchestrator, gateway, Claude,
Codex, Pi, and Firecracker artifacts, then publishes only the wheel verified
against those identities. Install the reported commit with
`BOT_BOTTLE_REF=<sha> sh install.sh`; the installer warns that snapshots may
not have completed release qualification.

[`release.yml`](../.gitea/workflows/release.yml) handles promotion tags.
`vX.Y.Z-rc.N` must be reachable from `staging`; `vX.Y.Z` must be reachable
from `production`. It runs the complete pre-release workflow, ensures that the
same commit bundle is published, verifies a clean install, creates the
qualified release pointer, and advances the matching channel. Promotion
reuses immutable bundle bytes and never rebuilds them.

## Scheduled canary

[`.gitea/workflows/canaries.yml`](../.gitea/workflows/canaries.yml) runs weekly
and on manual dispatch. It verifies the pinned gitleaks release URL, checksum,
archive shape, and executable. The same unittest execution gate requires at
least one executed canary and rejects skips.
