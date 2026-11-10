# Secrets

Secrets live on the host. Bottles refer to them by name. The gateway adds most of them to requests on the way out, and only env secrets reach the agent.

## Summary

| Secret | Stored in | Held at runtime by | The agent sees |
|---|---|---|---|
| `secret:NAME` env entry | secrets file, or typed at launch | the agent's env, and the orchestrator for scanning | the real value |
| `secret:?<message>` env entry | typed at launch | the agent's env, and the orchestrator for scanning | the real value |
| `secret:${VAR}` env entry | your shell | the agent's env, and the orchestrator for scanning | the real value |
| `?<message>` or `${VAR}` env entry | typed at launch, or your shell | the agent's env | the value (it's an input, not a secret) |
| Literal env entry | the bottle file | the agent's env | the value (it isn't secret) |
| Route token (`auth.value`) | secrets file (`secret:NAME`) or your shell (`secret:${VAR}`) | the gateway | nothing |
| Claude or Codex login (`login: host`) | the harness's own login on the host | the gateway | a placeholder |
| Model key (`login: {oauth_token}`, `login: {api_key}`) | secrets file | the gateway | a placeholder |
| Gitea token, or GitHub with `--transport deploy-key` | secrets file | the host, which mints a deploy key per repo for git-gate | nothing |
| GitHub token, HTTPS transport | secrets file | the gateway's memory, for git-gate | nothing |
| Canary | made per launch | the agent's env and the gateway | a decoy value |

- The secrets file is plain text, readable only by your user.
- Bottles and agents name secrets. They never hold a value.
- A value typed at a `?` or `secret:?` prompt is never written to the secrets file. A missing `secret:NAME` is saved only if you agree.

## Where secrets are stored

### The secrets file

```
# ~/.bot-bottle/secrets
EXAMPLE_API_TOKEN=tok_live_abc123
GITEA_EXAMPLE_COM_TOKEN=6f1c...
```

The file is `~/.bot-bottle/secrets`. A named environment uses `~/.bot-bottle-envs/<name>/secrets`, and `BOT_BOTTLE_ROOT` moves it.

- One `NAME=value` per line. Blank lines and `#` comments are skipped.
- The value is everything after the first `=`, taken literally. Quotes are part of the value.
- A malformed line fails with its line number. The line itself is never printed.

bot-bottle refuses to read the file unless you own it and it has no group or other access. The error says to run `chmod 600`. bot-bottle's own writes replace the file atomically at mode 0600.

| Who writes it | What |
|---|---|
| `bot-bottle setup` | pi's key: `OPENROUTER_API_KEY` from the OpenRouter sign-in, or, if you agree, the literal key in `~/.pi/agent/models.json` as `PI_<PROVIDER>_API_KEY` |
| `bot-bottle forge connect` | a forge's token (see [Forges](#forges)) |
| The paired console | any name, through its Credentials tab |
| You | any line, with an editor |

The file stays on the host. The orchestrator gets only `~/.bot-bottle/db/`, which doesn't contain it.

### Your shell

`${VAR}` and `secret:${VAR}` read your shell's environment. A `${VAR}` found only in the secrets file is used, with a warning. `secret:NAME` reads only the secrets file. A launch from the console, or a child bottle, has no shell and reads only the file.

### Harness logins

`login: host` reads the login your host's harness already keeps. It never copies it into the secrets file.

| Harness | Read from | What's taken |
|---|---|---|
| Claude Code | `~/.claude/.credentials.json`, else the macOS Keychain item `Claude Code-credentials` | the OAuth access token, never the refresh token |
| Codex | `$CODEX_HOME/auth.json`, else `~/.codex/auth.json` | the access token of a ChatGPT or device login. An API-key login isn't forwarded. |

## How a bottle names a secret

### Env entries

```yaml
env:
  OPENAI_API_KEY: secret:OPENAI_API_KEY
  DB_PASSWORD: "secret:?Paste the staging DB password"
  NPM_TOKEN: "secret:${MY_NPM_TOKEN}"
  BRANCH: "?Which branch?"
  AWS_REGION: "${AWS_REGION}"
  NODE_ENV: development
```

| Form | Value comes from | Secret? | When it's missing |
|---|---|---|---|
| `secret:NAME` | `NAME` in the secrets file | yes | a terminal launch asks once (hidden), then offers to save it. Any other launch fails, naming the file and `NAME=<value>`. |
| `secret:?<message>` | asked at every launch, input hidden | yes | with nobody to ask, a saved value of the entry's name (secrets file, then environment) is used, with a warning |
| `secret:${VAR}` | `VAR` in your shell | yes | the launch fails, naming both variables |
| `?<message>` | asked at every launch, input shown | no | as `secret:?` |
| `${VAR}` | `VAR` in your shell | no | as `secret:${VAR}` |
| anything else | the bottle file, as written | no | not applicable |

`NAME` is letters, digits and `_`, not starting with a digit. A literal can't start with `secret:`. `${VAR}` must be the whole value. `prefix-${VAR}` is a literal.

"Secret" means the value is push-scanned (8 or more characters), masked in transcripts and the console, and listed as `secret` in the trust prompt. A `?` or `${VAR}` entry is listed as `input`. One whose name looks secret (ends in `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `PASSWD`, `PAT` or `CREDENTIAL(S)`) warns that it "isn't marked secret".

An interactive launch asks for every `?` and `secret:?` entry, even when a saved value exists, and says that `secret:NAME` would use it.

### Route tokens

```yaml
egress:
  routes:
    - host: api.example.com
      auth: {type: bearer, value: secret:EXAMPLE_API_TOKEN}
```

The gateway sends `Authorization: Bearer <value>` to that host. `type: token` sends `Authorization: token <value>`. `type: header` with a `name` puts the value, raw, in that header instead.

`value` is `secret:NAME` or `secret:${VAR}`. A plain `?`, `${VAR}` or `secret:?` is refused here. A missing or empty value fails the launch, naming the secret. A `passthrough` route can't carry a token. The route fields are in [manifest.md](manifest.md#egress-routes).

### Forges

```sh
bot-bottle forge connect https://gitea.example.com
```

The token goes into the secrets file as `<HOST>_TOKEN`, here `GITEA_EXAMPLE_COM_TOKEN`. A forge's second user gets `<USER>_<HOST>_TOKEN`. `~/.bot-bottle/forges.md` records the name (`token: secret:NAME`), never the value.

A bottle grants repos (`gitea.example.com/acme/app:write`) and never names a token. A bottle with a `credentials:` block, or a `token` in a forge description, fails to load. Details are in [git-gate.md](git-gate.md#forges-and-one-line-grants).

A github.com token from the bot-bottle GitHub App expires. The file keeps `_REFRESH`, `_EXPIRES` and `_CLIENT_ID` lines beside it, and the host renews it.

### Host logins and model keys

```yaml
agent_provider:
  harness: claude
  login: host
```

| Agent setting | The gateway injects | The agent holds |
|---|---|---|
| `login: host` (Claude) | your Claude login | `CLAUDE_CODE_OAUTH_TOKEN=egress-placeholder` |
| `login: {oauth_token: secret:NAME}` (Claude) | a `claude setup-token` token from secret `NAME` | the same placeholder |
| `login: {api_key: secret:NAME}` (Claude) | an Anthropic API key, raw in `x-api-key` | `ANTHROPIC_API_KEY=egress-placeholder` |
| `login: host` (Codex) | your Codex login, to `api.openai.com` and `chatgpt.com` | an `auth.json` with placeholder tokens and the real expiry |
| `login: {api_key: secret:NAME}` (pi) | the model provider's key, as a Bearer token | `apiKey: egress-placeholder` |

A missing or expired host login fails the launch and says what to run on the host. See [install.md](install.md#signing-in-an-agent).

## Where each value goes

The CLI resolves each name at launch and hands the values to the host controller, which hands them to the backend.

- Env values go into the agent's environment.
- Injected tokens go to the orchestrator, which serves them to the gateway per request.
- Secret env values (`secret:...`) of 8 or more characters and the canary go to the orchestrator too, so git-gate can scan pushes for them.

The orchestrator stores what it's given in `bot-bottle.db`, encrypted under a random per-bottle key. That key (`ENV_VAR_SECRET`) is kept in the agent's guest, not with the database.

The host reads the key back to restore a bottle's tokens when the infra restarts. The agent has the key but can't reach the database.

Env values never go on a host command line, where other host users could read them. On docker and macos-container they reach the agent through the launching process's environment. On firecracker they are sent over the `ssh` connection's input and written to a file inside the VM ([backends.md](backends.md#firecracker-the-guest-environment)).

## What protects them

| Protection | Covers | Doesn't cover |
|---|---|---|
| Gateway injection | route tokens, host logins, model keys and forge tokens never enter the agent | env entries, which the agent holds |
| [Credential stripping](gateway.md#agent-credentials) | a key the agent adds itself is removed on inspected routes | `passthrough` routes |
| [Outbound DLP](gateway.md#outbound-dlp) `known_secrets` | injected tokens in any request on an inspected route, including encoded and split forms | env values, which may go to their own host |
| Canary | any request carrying it on an inspected route is a 403, recorded for `bot-bottle resolve-canary` | `passthrough` routes |
| [git-gate push scan](git-gate.md#push) | injected tokens, the canary and secret env values of 8 or more characters, anywhere in a push | `?` and `${VAR}` inputs, and shorter secret values |
| gitleaks on pushes | anything with a known token shape | shapeless values |
| [Trust prompt](manifest.md#trust) | each route `auth.value`, forge token, `secret:` entry (listed as `secret`), `?` and `${VAR}` entry (listed as `input`), and provider login or key | |
| Bottle file guard | a saved version or a console edit holding a secrets-file value is refused | values shorter than 8 characters |
| Console | sees names only. Transcripts mask secret values and token shapes. | `?` and `${VAR}` inputs. Typed and shell secrets after the host controller restarts and re-adopts a running bottle. |

Launch summaries, prompts and errors name variables, never values (`env.py`, `secret_source.py`).

### Limits

- The agent can send its own env values, secret or not, to any allowlisted host. Egress doesn't block them.
- A `passthrough` route is an unscanned tunnel. Anything the agent holds can go there.
- `?` and `${VAR}` inputs aren't scanned anywhere. Mark anything sensitive `secret:`.
- The secrets file is plain text. File permissions are its only protection, so anyone with your user account can read it.
- The console can set a secret but not read one. A compromised console could still point a route at a host of its choosing and send a secret there. See [What pairing lets the console do](host-controller.md#what-pairing-lets-the-console-do).
- A compromised gateway or orchestrator holds every running bottle's tokens. See the [threat model](../security/threat-model.md#what-we-explicitly-do-not-defend-against).

## Changing and removing a secret

To change a value, edit the file, set it from the console, or run `forge connect` again. Most kinds take effect at the next launch.

| Secret | How to change it | Running bottles |
|---|---|---|
| Env entries, route tokens, `login` keys | edit the file or your shell | keep the old value. Stop and resume to pick up the new one. |
| Claude or Codex login | log in again on the host | updated within seconds, or five minutes for a Keychain login. See [Forwarded host logins](host-controller.md#forwarded-host-logins). |
| GitHub token, HTTPS transport | `bot-bottle forge connect github`, or automatic renewal | the gateway gets the new token within four minutes |
| Gitea or deploy-key token | `bot-bottle forge connect <url>` again | keep the deploy keys minted at launch |

A resume resolves every secret again. A bottle with `preserve_container` on docker or macos-container keeps the env it was created with. `bot-bottle resume --fresh <bottle>` starts a new guest that reads the new values ([backends.md](backends.md#keeping-the-guest)).

To remove a secret, delete its line from the file. No command removes one, and the console can only set values. `bot-bottle uninstall --purge` deletes the whole `~/.bot-bottle`, secrets file included.

## Planned

An encrypted vault is planned and not yet available. It will keep secrets in the OS keychain and sync them end-to-end encrypted between your hosts. Until then, the secrets file is the store.
