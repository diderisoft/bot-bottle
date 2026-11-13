# ADR 0022: The console upgrades a host to a newer release, with a passkey

- **Status:** Accepted
- **Date:** 2026-11-13
- **Deciders:** didericis

## Context

Upgrading bot-bottle meant running `install.sh` on each host by hand. A
fleet of hosts paired to one console drifts, and the console, which sees each
host's version, couldn't help.

Until now the console could run nothing on a host outside a bottle
([host-controller.md](../reference/host-controller.md#what-pairing-lets-the-console-do)).
Installing software is exactly that, so whatever we allow has to stay safe
against a compromised console or a stolen console session.

## Decision

The console can ask a host to upgrade bot-bottle, within limits the host
enforces:

- **A newer release only.** The target is a version (`X.Y.Z`) newer than the
  host's (never when the host's own version is unknown), and the host checks
  that its `v<version>` tag exists on the
  `diderisoft/bot-bottle` repo. The console can't name a branch, a commit,
  an older release, or a source.
- **Nothing of the console's runs.** The host fetches that release's own
  `install.sh` from the tag and runs it with its own environment, minus any
  `BOT_BOTTLE_INSTALL_*` settings. The console sends no code, arguments or
  environment.
- **A passkey per upgrade.** As for audit reads (ADR 0013): the host issues a
  single-use challenge bound to the console session and the version; the
  user signs it with a passkey the host trusted at pairing, with user
  verification (biometric or PIN); the host checks the signature itself. A
  compromised console can't upgrade a host on its own.
- **Journalled.** Each upgrade, and each refused signature, is a
  `hostctl.upgrade` event naming the passkey.
- **Running bottles keep running.** After the installer, the host runs
  `bot-bottle up --restart --yes`, which restarts the host controller and
  adopts them, as `up --restart` does by hand. The upgrade fails if either
  step fails.
- **Not from a source checkout.** A host running bot-bottle from a checkout
  refuses: it's upgraded there.

## Consequences

- One tap (and a passkey) upgrades a host from anywhere.
- The console can now cause software to be installed on a host. The worst a
  compromised console can do with this alone is nothing: without the passkey
  it can't start an upgrade, and with one it can only install a published
  release the user could have installed by hand.
- The trust in a release is still the git tag over HTTPS, as for a manual
  install: releases aren't signed. Signing them would strengthen both.
- No automatic upgrades yet: a host setting can add them later.

## Links

- ADR 0013 (passkey-signed audit reads), whose challenge and check this
  reuses.
- [host-controller.md: Upgrading from the console](../reference/host-controller.md#upgrading-from-the-console)
