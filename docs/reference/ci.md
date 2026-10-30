# CI

## Required pull-request gate

[`.gitea/workflows/test.yml`](../../.gitea/workflows/test.yml) runs the unit
suite, Docker integration suite, combined coverage report, and diff-coverage
gate when tested package/build inputs change on a pull request or on `main`.

The Docker job preflights the backend before discovery. Gitea's `act_runner`
runs the job in a container with the host Docker socket, so the test process
reaches control-plane siblings through the job's Docker network and uses named
Docker volumes for orchestrator/CA state the host daemon must mount. The
orchestrator runs the package baked into the image built from the checkout; it
does not bind the job container's invisible workspace into a sibling container.
Docker integration jobs share fixed singleton names, so required and manual
runs use one non-cancelling concurrency group. The shared gateway network and
each agent's own network have explicit subnets, which Docker requires for the
pinned source IPs used as the isolation/attribution key.

`scripts.unittest_gate` enforces the Docker job's contract: all 25 integration
tests must execute and none may skip. This includes the real gateway-image,
control-plane authentication, multitenant policy/token isolation, commit-trailer
(git-gate push, refusal and audit record)
and sandbox-escape tests. Backend skip decorators remain useful
for local runs, but the CI preflight plus execution-count gate prevents a
missing backend or runner-topology regression from becoming a green job.

Combined unit + Docker coverage is informational globally. Two focused gates
are enforced:

- changed executable Python lines must be at least 80% covered; and
- the validated critical security/logic core must remain at least 85% covered
  (thresholds per [ADR 0004](../decisions/0004-coverage-policy.md)).

## GitHub Actions (hosted runners only)

[`.github/workflows/`](../../.github/workflows/) mirrors the required gate on
GitHub-hosted `ubuntu-latest` runners while Gitea stays the primary CI:

- [`test.yml`](../../.github/workflows/test.yml): unit, Docker integration
  (same 25-executed/no-skip gate), image-input builds, and combined coverage
  with the critical-modules and diff-coverage gates; and
- [`lint.yml`](../../.github/workflows/lint.yml): image-input and
  published-artifacts checks, pylint (score >= 8.0), and strict pyright.

Both run on `push` (any branch) and `pull_request`, filtered by the same paths
as their Gitea counterparts, on Python 3.13 via `actions/setup-python`. The
Docker job runs directly on the runner VM that owns the Docker daemon, so it
needs none of the `act_runner` client-network or named-volume overrides, and
each job's fresh VM removes the need for a shared concurrency group. The
diff-coverage base is the PR's base branch, or `main` on a push; the coverage
job checks out full history so the base is present without an authenticated
fetch.

The workflows are hardened: third-party actions are pinned to full commit
SHAs (with a version comment), the token is `contents: read`, no workflow uses
`pull_request_target` or secrets, checkout does not persist credentials, and a
per-ref concurrency group cancels superseded runs (except on `main`).

Only GitHub-hosted runners are used. The KVM and macOS self-hosted runners are
never registered with GitHub; the pre-release matrix, canaries, and the
tracker-policy, PRD-number, and image publish/refresh/prune workflows run only
on Gitea.

## Privileged pre-release matrix

[`.gitea/workflows/pre-release-test.yml`](../../.gitea/workflows/pre-release-test.yml)
is manually dispatched before a release. It repeats unit and Docker integration
coverage, then runs:

- Firecracker integration on the self-hosted `kvm` runner; and
- advisory Apple Container integration on the self-hosted `macos` runner.

These privileged host-mode runners never execute unreviewed pull-request code
automatically. Firecracker coverage is combined in the manual pre-release
report; macOS reports advisory coverage in its own job.

Every integration job (Gitea and GitHub) runs in its own environment
([install.md](install.md#environments)). Docker and firecracker jobs use
`BOT_BOTTLE_ENV=ci-<run id>`, so the state root, container and VM names,
docker subnets and firecracker pool slice are the run's own, and an always-run
step removes it (`bot-bottle envs rm "$BOT_BOTTLE_ENV" --force`). Jobs never
touch a host's live install or each other, so there are no
`integration-*-infra` concurrency groups. macOS uses the fixed `ci-macos`,
because each environment needs its own pf anchor, installed once as root; the
single host-mode runner runs one job at a time. Two docker runs on one daemon
collide only if their names hash to the same gateway /24 (1 in 256).

### Provisioning the self-hosted runners

- **`kvm` (Firecracker):** provision it like any Firecracker host
  ([install.md](install.md#linux-firecracker)): `firecracker` on `PATH`,
  `/dev/kvm`, the cached guest kernel and static dropbear
  (`/var/cache/bot-bottle-fc/dropbear`), and the TAP/nft pool as a persistent
  systemd unit. The pool must reserve environment slices (the default 4):
  each concurrent firecracker run claims one, and a run with none free fails
  saying so. Slots are recorded per user, so CI runs and a developer's `dev`
  environment share them safely only when they run as the same user. The
  job's preflight fails when any of these is missing.
- **`macos` (Apple Container, advisory; it never gates `promote`, since the runner is a laptop that may be asleep):** an Apple Silicon host-mode runner;
  Apple Container can't run inside the Linux pull-request runner. It needs the
  `container` CLI with `container system status` running, and Python ≥ 3.11
  with `coverage` importable on the launchd service's explicit `PATH`. Install
  the `ci-macos` environment's host firewall once (`sudo env
  BOT_BOTTLE_ENV=ci-macos bot-bottle install --backend=macos-container`, or
  the printed `sudo` block); the runner itself never gets root, and the job's
  preflight and isolation probe fail without it. Keep runner concurrency at 1,
  since every run shares `ci-macos`. Its coverage never feeds the required
  pull-request gate.

When every job passes, it releases: it promotes the tested images to a release
version and commits their pins (`published-artifacts.json`) to `main`, with
`pyproject.toml` bumped to that version in the same commit, and tags that
commit `vX.Y.Z` (annotated; a rerun leaves an existing tag alone). `install.sh`
installs the newest such tag. The release commit goes only directly on the
tested commit: if `main` moved during the run, the job fails ("main moved past
the tested commit …; re-run the release") before promoting, or before pushing
if it moves later. Before committing, `scripts/check_published_artifacts.py
--match-sources` checks that every pinned `inputs_hash` equals the release
tree's, so a release whose pins its own installs can't use fails instead of
shipping. `inputs_hash` ignores `pyproject.toml`'s `[project] version`, which
is the one thing the release commit changes after the images are built. Leave the `version`
input blank to take the first unpublished version at or above `pyproject.toml`'s.
[`publish-images.yml`](../../.gitea/workflows/publish-images.yml) only rehearses a
publish: it defaults to a `0.0.0-dev-<run>` version and pins nothing.

## Scheduled canary

[`.gitea/workflows/canaries.yml`](../../.gitea/workflows/canaries.yml) runs weekly
and on manual dispatch. It verifies the pinned gitleaks release URL, checksum,
archive shape, and executable. The same unittest execution gate requires at
least one executed canary and rejects skips.
