# Contributing

Most of bot-bottle is written by coding agents under the maintainer's
direction; [AI_POLICY.md](AI_POLICY.md) says how that works and what keeps it
safe. Start with [AGENTS.md](AGENTS.md) for orientation and
[`docs/`](docs/README.md) for how the system works and why.

## Commits

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):
`<type>[(scope)][!]: <description>`, where `<type>` is one of `feat`, `fix`,
`docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore` or
`revert`. A `commit-msg` hook in `.githooks/` enforces this; activate it once
per clone:

```sh
git config core.hooksPath .githooks
```

## Running from main

Develop in the `dev` environment, never your live install: with
`BOT_BOTTLE_ENV=dev` a checkout gets its own state root
(`~/.bot-bottle-envs/dev`), container and VM names, network and host
controller, so a migration, teardown or crash can't reach `~/.bot-bottle` or
the bottles running there
([docs/reference/install.md](docs/reference/install.md#environments)).

```sh
export BOT_BOTTLE_ENV=dev
./cli.py status                # says "environment: dev ..."
./cli.py envs rm dev           # start over: tears dev down, deletes its state
```

Installs default to the latest release tag. A checkout, or an install of
`main` (`install.sh --main`), usually has no published orchestrator and gateway
images to match it, and bot-bottle won't build them unasked. Build them from
your source:

```sh
./cli.py up --build    # or: BOT_BOTTLE_INFRA_BUILD=local ./cli.py <command>
```

Launches after it keep building locally until an `up` without `--build`. It
needs Docker (Apple's `container` on macOS). See
[docs/reference/backends.md](docs/reference/backends.md#infra-images).

## Pull requests and the tracker

Issues are the canonical work items and own their labels; every issue has at
least one. Each pull request is tracked in exactly one of two ways
([ADR 0005](docs/decisions/0005-issues-own-tracker-metadata.md)):

- it references an issue in its title or description, with `Closes #…`,
  `Fixes #…`, `Part of #…`, `Refs #…` or another form the ADR lists, and
  stays unlabelled; or
- it carries at least one label itself, when a separate issue would add
  nothing.

Gitea Actions enforces this for new work from 2026-07-18 onward. Earlier
closed PRs are grandfathered rather than given artificial retrospective
issues.

`#NNN` references in commit messages and PR descriptions point to the Gitea
issue tracker at <https://gitea.dideric.is/didericis/bot-bottle/issues>. If
the project ever moves to another tracker, that URL remains the authoritative
destination for all historical references.

A change that alters behaviour a page in `docs/reference/` describes updates
that page in the same pull request.

## Security issues

Report vulnerabilities as [SECURITY.md](SECURITY.md) describes, not in a
public issue.
