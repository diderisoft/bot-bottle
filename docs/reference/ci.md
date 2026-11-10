# CI

| Workflow | Runs on | When | For |
|---|---|---|---|
| [`.gitea/workflows/test.yml`](../../.gitea/workflows/test.yml) | Gitea | pull requests and `main`, when tested package or build inputs change | the required gate |
| [`.github/workflows/`](../../.github/workflows/) | GitHub-hosted runners | `push` (any branch) and `pull_request`, filtered by the same paths | a mirror of the required gate |
| [`.gitea/workflows/pre-release-test.yml`](../../.gitea/workflows/pre-release-test.yml) | Gitea, plus self-hosted `kvm` and `macos` runners | manual dispatch before a release | the privileged matrix, then the release |
| [`.gitea/workflows/canaries.yml`](../../.gitea/workflows/canaries.yml) | Gitea | weekly and on manual dispatch | checking the pinned gitleaks release |

## Required pull-request gate

[`.gitea/workflows/test.yml`](../../.gitea/workflows/test.yml) runs these
when tested package or build inputs change, on a pull request or on `main`:

- the unit suite
- the Docker integration suite
- a combined coverage report
- the diff-coverage gate

### The Docker job

`scripts.unittest_gate` enforces the Docker job's contract. All 25
integration tests must execute and none may skip.

The 25 include the real gateway-image, control-plane authentication,
multitenant policy and token isolation, commit-trailer and sandbox-escape
tests. The commit-trailer tests cover the git-gate push, its refusal and the
audit record.

Backend skip decorators remain useful for local runs. In CI, the preflight
plus the execution-count gate stop a missing backend or a runner-topology
regression from becoming a green job.

### Coverage

Combined unit and Docker coverage is informational globally. Two focused
gates are enforced, with thresholds per
[ADR 0004](../decisions/0004-coverage-policy.md):

- changed executable Python lines must be at least 80% covered
- the validated critical security and logic core must remain at least 85%
  covered

### Details: the Docker job on Gitea

The job preflights the backend before test discovery.

Gitea's `act_runner` runs the job in a container with the host Docker socket.
So the test process:

- reaches control-plane siblings through the job's Docker network
- uses named Docker volumes for the orchestrator and CA state the host daemon
  must mount

The orchestrator runs the package baked into the image built from the
checkout. It does not bind the job container's invisible workspace into a
sibling container.

Docker integration jobs share fixed singleton names. Required and manual runs
therefore use one non-cancelling concurrency group.

The shared gateway network and each agent's own network have explicit
subnets. Docker requires them for the pinned source IPs used as the isolation
and attribution key.

## GitHub Actions (hosted runners only)

[`.github/workflows/`](../../.github/workflows/) mirrors the required gate on
GitHub-hosted `ubuntu-latest` runners. Gitea stays the primary CI.

| Workflow | Runs |
|---|---|
| [`test.yml`](../../.github/workflows/test.yml) | unit, Docker integration (the same 25-executed, no-skip gate), image-input builds, and combined coverage with the critical-modules and diff-coverage gates |
| [`lint.yml`](../../.github/workflows/lint.yml) | image-input and published-artifacts checks, pylint (score >= 8.0), and strict pyright |

Both run on `push` (any branch) and `pull_request`. They filter by the same
paths as their Gitea counterparts and use Python 3.13 via
`actions/setup-python`.

How the Docker job differs from Gitea's:

- It runs directly on the runner VM that owns the Docker daemon. It needs
  none of the `act_runner` client-network or named-volume overrides.
- Each job's fresh VM removes the need for a shared concurrency group.
- The diff-coverage base is the PR's base branch, or `main` on a push. The
  coverage job checks out full history, so the base is present without an
  authenticated fetch.

The workflows are hardened:

- Third-party actions are pinned to full commit SHAs, with a version comment.
- The token is `contents: read`.
- No workflow uses `pull_request_target` or secrets.
- Checkout does not persist credentials.
- A per-ref concurrency group cancels superseded runs, except on `main`.

Only GitHub-hosted runners are used. The KVM and macOS self-hosted runners
are never registered with GitHub.

These run only on Gitea:

- the pre-release matrix
- canaries
- the tracker-policy and PRD-number workflows
- the image publish, refresh and prune workflows

## Privileged pre-release matrix

[`.gitea/workflows/pre-release-test.yml`](../../.gitea/workflows/pre-release-test.yml)
is manually dispatched before a release. It repeats unit and Docker
integration coverage, then runs:

| Job | Runner | Coverage |
|---|---|---|
| Firecracker integration | self-hosted `kvm` | combined in the manual pre-release report |
| Apple Container integration, advisory | self-hosted `macos` | advisory, in its own job |

These privileged host-mode runners never execute unreviewed pull-request code
automatically.

### Environments per run

Every integration job, on Gitea and GitHub, runs in its own environment
([install.md](install.md#environments)).

| Jobs | Environment | Why |
|---|---|---|
| Docker and firecracker | `BOT_BOTTLE_ENV=ci-<run id>` | The state root, container and VM names, docker subnets and firecracker pool slice are the run's own. |
| macOS | the fixed `ci-macos` | Each environment needs its own pf anchor, installed once as root. The single host-mode runner runs one job at a time. |

- An always-run step removes the run's environment
  (`bot-bottle envs rm "$BOT_BOTTLE_ENV" --force`).
- Jobs never touch a host's live install or each other. There are no
  `integration-*-infra` concurrency groups.
- Docker runs on one daemon allocate their gateway /24s, moving on from any
  that another run holds.

### Provisioning the self-hosted runners

**`kvm` (Firecracker):** provision it like any Firecracker host
([install.md](install.md#linux-firecracker)). The job's preflight fails when
any of these is missing.

- `firecracker` on `PATH`, and `/dev/kvm`
- the cached guest kernel and static dropbear
  (`/var/cache/bot-bottle-fc/dropbear`)
- the TAP and nft pool, as a persistent systemd unit
- environment slices reserved in the pool, by default 4. Each concurrent
  firecracker run claims one. A run with none free fails saying so.
- the runner's user and a developer's both in the pool's group. Slice claims
  are host-wide (`/var/lib/bot-bottle-fc/slices`). On NixOS the group is
  `services.bot-bottle-firecracker.group`.

**`macos` (Apple Container, advisory):** it never gates `promote`, since the
runner is a laptop that may be asleep. Its coverage never feeds the required
pull-request gate.

- An Apple Silicon host-mode runner. Apple Container can't run inside the
  Linux pull-request runner.
- The `container` CLI, with `container system status` running.
- Python ≥ 3.11, with `coverage` importable on the launchd service's explicit
  `PATH`.
- The `ci-macos` environment's host firewall, installed once. Use `sudo env
  BOT_BOTTLE_ENV=ci-macos bot-bottle install --backend=macos-container`, or
  the printed `sudo` block. The runner itself never gets root. The job's
  preflight and isolation probe fail without the firewall.
- Runner concurrency kept at 1, since every run shares `ci-macos`.

### Releasing

The matrix tests a published candidate. When every job passes, it releases
that candidate.

1. The candidate is published to Gitea as `0.0.0-dev-<run>`, with the rootfs
   as Gitea generic packages. Images unchanged since the last release are
   [reused](#reusing-unchanged-images), not rebuilt.
2. The tested images are promoted to a release version on GHCR. They are
   copied byte for byte through the registry API, the rootfs as OCI artifacts.
   Reused entries are already on GHCR and are kept as they are.
3. `scripts/check_published_artifacts.py --match-sources` checks that every
   pinned `inputs_hash` equals the release tree's.
4. Their pins (`published-artifacts.json`) are committed to `main`, with
   `pyproject.toml` bumped to that version in the same commit.
5. That commit is tagged `vX.Y.Z`, annotated. A rerun leaves an existing tag
   alone.

`install.sh` installs the newest such tag. Leave the `version` input blank to
take the first unpublished version at or above `pyproject.toml`'s.

#### Reusing unchanged images

An image is rebuilt only when its `inputs_hash` changed. The committed
`published-artifacts.json`, the last release's pins, is the reuse source.

```json
"bot-bottle-claude": {
  "artifacts": {
    "oci_linux-amd64": {
      "digest": "sha256:1248a312…",
      "platform": "linux-amd64",
      "type": "oci",
      "url": "ghcr.io/diderisoft/bot-bottle-claude:0.1.8-amd64"
    },
    …
  },
  "inputs_hash": "300cb3deabf3744d",
  "protocol": 1,
  "version": "0.1.8"
}
```

That entry can ship unchanged in a later release, such as 0.1.9. Its
`version` and URLs keep naming the release that built it. An image is reused
only when all of these hold:

- its `inputs_hash` and `protocol` equal this tree's
- it has both formats on both arches, `oci` and `ext4` on amd64 and arm64
- its registry still serves every artifact by its pinned digest

| Stage | A reused image |
|---|---|
| publish | Not built or uploaded. Its entry is copied byte for byte into the candidate manifest, logged as `reusing <image> from <version> (inputs <hash>)`. |
| integration | Pulled from GHCR anonymously, as installs pull it. |
| promote | Confirmed on GHCR by digest, then kept. Only entries outside GHCR, the newly built ones, are copied. |
| `--match-sources` | Checked like any entry: its `inputs_hash` must equal the release tree's. |

The release manifest therefore mixes versions. Promotion writes the release
version to `released/release-version`, since no single entry carries it.

##### Details: reuse flags and versions

- `publish_images --reuse-from MANIFEST` names another reuse source.
  `--no-reuse` builds every image. `--force` only replaces what a rerun
  already published at its own dev version.
- [`publish-images.yml`](../../.gitea/workflows/publish-images.yml) and
  [`publish-dev-images.yml`](../../.github/workflows/publish-dev-images.yml)
  pass `--no-reuse`. A rehearsal builds what it names, and the dev merge
  stitches every image's per-arch tags.
- A release that reuses every image publishes nothing at its version. Only
  its git tag marks the version taken, so promote fetches the release tags
  before picking the next free version.
- The prune workflow cleans only Gitea, so reused GHCR artifacts stay. A
  reused artifact deleted by hand is caught at publish and rebuilt.

#### Details: release safety

The release commit goes only directly on the tested commit. If `main` moved
during the run, the job fails before promoting: "main moved past the tested
commit …; re-run the release". It also fails before pushing if `main` moves
later.

The `inputs_hash` check runs before committing. A release whose pins its own
installs can't use fails instead of shipping.

`inputs_hash` ignores `pyproject.toml`'s `[project] version`. That is the one
thing the release commit changes after the images are built.

#### Other publish workflows

| Workflow | Does |
|---|---|
| [`publish-images.yml`](../../.gitea/workflows/publish-images.yml) | Only rehearses a publish, to Gitea. It defaults to a `0.0.0-dev-<run>` version and pins nothing. |
| [`.github/workflows/publish-dev-images.yml`](../../.github/workflows/publish-dev-images.yml) | Pushes OCI-only dev images to GHCR from GitHub's runners on merges to `main`. Nothing pins them. |

#### Publishing to GHCR

Publishing to GHCR needs, once:

1. A classic personal access token with `write:packages`, for an account that
   can create packages in the `diderisoft` org. Fine-grained tokens can't push
   to GHCR.
2. That token as the Gitea Actions secret `GHCR_TOKEN`, and the account name
   as the variable `GHCR_USER`.
3. After the first release, each package (`bot-bottle-*` and
   `bot-bottle-*-rootfs`) set to Public under the org's Packages settings. The
   org must allow public packages.

The packages link to the repository through
`org.opencontainers.image.source`. If the repository is ever made private,
turn off "Inherit access from source repository" on each package first, or
the packages become private with it.

## Scheduled canary

[`.gitea/workflows/canaries.yml`](../../.gitea/workflows/canaries.yml) runs
weekly and on manual dispatch. It verifies the pinned gitleaks release:

- the URL
- the checksum
- the archive shape
- the executable

The same unittest execution gate requires at least one executed canary and
rejects skips.
