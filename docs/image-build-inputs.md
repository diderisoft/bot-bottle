# Container image build inputs

Bot-bottle's supported images are intended to rebuild from the same declared
inputs on Linux amd64 and arm64. The repository enforces four layers of
immutability:

- Python and Node base images use version-qualified tags plus multi-platform
  OCI index digests.
- Debian packages resolve from the same dated `snapshot.debian.org` archive in
  every image that runs `apt-get install`.
- Gateway Python dependencies install from `requirements.gateway.lock` with
  `pip --require-hashes`; provider npm packages install with `npm ci` from
  committed lockfiles.
- The Codex standalone installer is fetched from the selected Codex release
  tag, checked against `install.sh.sha256`, invoked with an exact `--release`,
  and then verifies the selected release archive against upstream metadata.
  The standalone layout is retained because Codex remote control requires it.

`Dockerfile.orchestrator.fc` is the one intentionally dynamic `FROM`. It has no
default value: the Firecracker build coordinator builds the orchestrator first,
resolves its local `sha256:` image ID, and supplies that exact ID as
`ORCHESTRATOR_BASE_IMAGE`.

## Refreshing inputs

Make refreshes on a feature branch and review them like an application
dependency update:

1. For a Python or Node base update, select a version-qualified tag and record
   its multi-platform index digest in every Dockerfile that shares the base.
   Confirm the index still contains `linux/amd64` and `linux/arm64`.
2. For Debian packages, advance `DEBIAN_SNAPSHOT` to one fixed UTC timestamp in
   every Dockerfile that uses apt. Do not use the moving Debian mirrors.
3. Change direct Python versions in `requirements.gateway.in`, direct npm
   versions in the provider's `package.json`, or `CODEX_VERSION` in the Codex
   Dockerfile. Direct versions must be exact—no ranges, dist-tags, or
   unversioned package names.
4. Push the feature branch. The `refresh-image-locks` workflow regenerates the
   pip/npm locks and Codex installer checksum, then adds a
   `build: refresh image input locks` commit to that branch. It can also be run
   manually; on `main` it uploads the candidates without committing them.
5. Review the lock diff and let both normal CI and `image-input-builds` pass.
   The latter checks both base architectures, builds every supported image,
   exercises the exact local Firecracker base-ID handoff, and runs each
   provider CLI's version smoke test.

Run the fast policy check locally at any point:

```sh
python3 scripts/check_image_inputs.py
```

The check rejects mutable or `latest` bases, moving apt repositories,
network-to-shell installers, unlocked npm/Pi installs, non-exact direct
dependencies, missing npm integrity values, and unhashed gateway Python
requirements.

Image signing and attestation are tracked separately in issue #339; this policy
covers deterministic and integrity-checked build inputs.
