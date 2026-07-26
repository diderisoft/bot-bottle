# CI

The test workflow lives at [`.gitea/workflows/test.yml`](../.gitea/workflows/test.yml).
It runs the unit suite plus one integration job per backend
(`integration-docker`, `integration-firecracker`, `integration-macos`) on:

- every push to a branch with an open pull request, and
- every push to `main`.

`integration-macos` is the exception: it is **advisory**, running only on
`workflow_dispatch` (manual dispatch), never on push or pull requests. It targets the
Apple Container backend on a self-hosted macOS runner (label `macos`,
registered in host mode — Apple Container can't run in a Linux container, so it
can't reuse the `kvm` runner). A single non-redundant laptop must not be able
to block a PR merge, so the job stays out of the `coverage` job's `needs` and
its coverage never feeds the diff-coverage gate. Because the infra container is
a singleton (`bot-bottle-mac-infra`), the job declares a `concurrency` group
and tears the container down on exit; keep runner concurrency at 1. See the
README "macOS Apple Container" CI note for runner provisioning.

Each integration job selects its backend via `BOT_BOTTLE_BACKEND` and
runs a **preflight** (`./cli.py backend status --backend=<name>`) that
prints a clear per-check readiness summary and fails the job when the
backend is missing — so absent infrastructure is visible at the job level
rather than hidden among per-test `unittest.skip` lines. The skip guards in
[`tests/_backend.py`](../tests/_backend.py) gate on the same readiness
check (`bot_bottle.backend.has_backend`): backend-agnostic tests use
`skip_unless_selected_backend_available()` and run through whichever
backend is selected (checking, e.g., Linux + `/dev/kvm` for Firecracker
rather than unrelated Docker availability); Docker-implementation tests use
`skip_unless_backend("docker")` and no-op under a non-Docker run.

A small subset of integration tests skip when running specifically
under Gitea Actions (`GITEA_ACTIONS=true`), because `act_runner` runs
the job inside a container with the host's `/var/run/docker.sock`
mounted in. That topology breaks two assumptions those tests make:

- networks created via the host daemon aren't always visible to a
  same-process `docker network ls` call from inside the job container,
  and
- ports published by sibling containers land on the host's loopback,
  not on the job container's `127.0.0.1` — so HTTP probes against
  `http://127.0.0.1:<host_port>` from inside the job time out.

The affected tests (`test_orphan_cleanup.test_create_and_remove`,
`test_gateway_image.TestGatewayImage`) still run
locally where the test process and Docker daemon share a host.
Making them work in CI is a follow-up: either re-write them to
discover container IPs via `docker inspect`, or reconfigure the
runner with host networking.
