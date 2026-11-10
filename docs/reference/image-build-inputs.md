# Container image build inputs

Bot-bottle's supported images are intended to rebuild from the same declared
inputs on Linux amd64 and arm64. The repository enforces four layers of
immutability.

| Input | Pinned by |
|---|---|
| Python, Node and nested-container Docker CLI base images | Required, defaultless Docker build arguments. Their version-qualified tags and multi-platform OCI index digests live together in `image-build-args.json`. |
| Debian packages | The same dated `snapshot.debian.org` archive, in every image that runs `apt-get install`. |
| Gateway Python dependencies and provider npm packages | `pip --require-hashes` from `requirements.gateway.lock`. `npm ci` from committed lockfiles. |
| The Codex standalone archive | Fetched from the exact `CODEX_VERSION` release and checked against the committed upstream `codex-package_SHA256SUMS` before extraction. |

### Details

- Every supported builder reads `image-build-args.json` and passes only the
  arguments its Dockerfile declares.
- The snapshot endpoint uses HTTP, so a fresh image does not need a
  host-specific TLS interception CA. APT still authenticates the repository
  metadata and package hashes with Debian's signed Release files.
- Codex keeps the standalone layout because Codex remote control requires it.

### The one dynamic `FROM`

`Dockerfile.orchestrator.fc` is the one intentionally dynamic `FROM`. It has
no default value. Instead the Firecracker build coordinator:

1. resolves the freshly built orchestrator's local `sha256:` image ID
2. creates a local tag containing the complete ID
3. verifies that tag resolves to the same ID
4. supplies the content-derived reference as `ORCHESTRATOR_BASE_IMAGE`

This accommodates BuildKit, which treats a bare image ID in `FROM` as a
registry repository.

## Refreshing inputs

Make refreshes on a feature branch. Review them like an application
dependency update.

1. **Python or Node base:** select a version-qualified tag and update its one
   digest-pinned value in `image-build-args.json`. Confirm the index still
   contains `linux/amd64` and `linux/arm64`.
2. **Debian packages:** advance `DEBIAN_SNAPSHOT` to one fixed UTC timestamp
   in every Dockerfile that uses apt. Do not use the moving Debian mirrors.
3. **Direct versions:** change Python ones in `requirements.gateway.in`, npm
   ones in the provider's `package.json`, or `CODEX_VERSION` in the Codex
   Dockerfile. They must be exact: no ranges, dist-tags, or unversioned
   package names.
4. **Regenerate:** push the feature branch, or manually run the
   `refresh-image-locks` workflow. It regenerates the four derived files with
   the image's exact Python and Node versions, and uploads them as the
   `image-input-locks` artifact.
5. **Commit:** download the artifact, replace the four committed files, and
   review the lock diff. Let both normal CI and `image-input-builds` pass.

The workflow never writes to the branch, so a refresh cannot race with
developer pushes.

The four derived files are `requirements.gateway.lock`,
`bot_bottle/contrib/claude/package-lock.json`,
`bot_bottle/contrib/pi/package-lock.json` and
`bot_bottle/contrib/codex/codex-package_SHA256SUMS`.

`image-input-builds` checks both base architectures and builds every
supported image. It exercises the exact local Firecracker base-ID handoff and
runs each provider CLI's version smoke test.

## Local check

```sh
python3 scripts/check_image_inputs.py
```

Run this fast policy check at any point. It rejects:

- mutable or `latest` bases
- moving apt repositories
- network-to-shell installers
- unlocked npm or Pi installs
- non-exact direct dependencies
- missing npm integrity values
- unhashed gateway Python requirements

Image signing and attestation are tracked separately in issue #339. This
policy covers deterministic and integrity-checked build inputs.
