# CI

The test workflow lives at [`.gitea/workflows/test.yml`](../.gitea/workflows/test.yml).
It runs `tests/run_tests.py` (full suite — unit + integration) on:

- every push to a branch with an open pull request, and
- every push to `main`.

Integration tests need Docker on the runner; they skip cleanly via
`tests/_docker.skip_unless_docker` when no daemon is reachable.

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
`test_sidecar_bundle_image.TestSidecarBundleImage`,
`test_sidecar_bundle_compose.TestSidecarBundleCompose`) still run
locally where the test process and Docker daemon share a host.
Making them work in CI is a follow-up: either re-write them to
discover container IPs via `docker inspect`, or reconfigure the
runner with host networking.
