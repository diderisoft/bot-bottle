"""Credential provider protocol: mint, renew, retire.

A `CredentialProvider` knows how to make a `MintedCredential` from a
source declaration and base secrets. The host controller calls `mint` at
launch, `renew` before the credential expires, and `retire` at teardown.
The registry discovers built-in and user-contributed providers.

Kind constants name what a minted credential is:

- `token`   — one `token` field: a bearer/token/raw header value.
- `aws`     — `access_key_id`, `secret_access_key`, optional `session_token`.
- `ssh-key` — `private_key`, `public_key`: only git-gate uses it.

Built-in sources: `secret`, `prompt`, `host_login`, `file` and
`gitea_deploy_key`. `assume_role` and `profile` come in a later chunk.
"""

from __future__ import annotations

import importlib.util
import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from .secret_source import SecretSource

log = logging.getLogger(__name__)

# Credential kinds.
KIND_TOKEN = "token"
KIND_AWS = "aws"
KIND_SSH_KEY = "ssh-key"

KNOWN_KINDS = frozenset({KIND_TOKEN, KIND_AWS, KIND_SSH_KEY})

# Fields each kind carries in `MintedCredential.values`.
KIND_FIELDS: dict[str, tuple[str, ...]] = {
    KIND_TOKEN: ("token",),
    KIND_AWS: ("access_key_id", "secret_access_key"),
    KIND_SSH_KEY: ("private_key", "public_key"),
}

# Kinds accepted in each manifest position.
ENV_KINDS = frozenset({KIND_TOKEN})
AUTH_BEARER_KINDS = frozenset({KIND_TOKEN})
AUTH_AWS_KINDS = frozenset({KIND_AWS})
GIT_KEY_KINDS = frozenset({KIND_SSH_KEY})


@dataclass(frozen=True)
class Grant:
    """What a credential grants, for the trust prompt."""

    acts_as: str
    authority: str
    scope: str
    lifetime: str
    starts_from: str


@dataclass(frozen=True)
class MintedCredential:
    """A live credential the host controller holds."""

    kind: str
    # The kind's fields; values are never logged or repr'd.
    values: Mapping[str, str]
    expires_at: datetime | None
    # Who it acts as (for audit); never a secret value.
    principal: str
    # Opaque provider-specific data for `retire()` (e.g. a key_id to revoke).
    # Never forwarded to the agent or logged.
    retire_data: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (f"MintedCredential(kind={self.kind!r}, "
                f"expires_at={self.expires_at!r}, principal={self.principal!r})")


class CredentialProvider(ABC):
    """Base class for credential sources."""

    source: ClassVar[str]
    # What kinds this source can mint. One entry means `kind` is implied.
    kinds: ClassVar[tuple[str, ...]]
    # Whether a project (repo) bottle may declare a credential with this source.
    project_definable: ClassVar[bool] = False
    # Settings that name files; their content is included in the trust hash.
    file_fields: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def mint(self, label: str, bottle_id: str,
             settings: Mapping[str, object], secrets: SecretSource) -> MintedCredential:
        """Make a fresh credential. Raise on failure."""

    def renew(self, label: str, bottle_id: str, current: MintedCredential,
              settings: Mapping[str, object], secrets: SecretSource) -> MintedCredential:
        """Refresh `current`. Default: mint again."""
        return self.mint(label, bottle_id, settings, secrets)

    def retire(self, label: str, bottle_id: str, current: MintedCredential) -> None:
        """Revoke or clean up `current`. Default: nothing."""

    @abstractmethod
    def describe(self, settings: Mapping[str, object]) -> Grant:
        """What this credential grants, for the trust prompt. Reads no secrets."""


class _SecretProvider(CredentialProvider):
    """Wraps a value already in the host secrets store.

    For `kind: token` (the default): `secret: NAME`.
    For `kind: aws`: `access_key_id: NAME`, `secret_access_key: NAME`
    (and optionally `session_token: NAME`).

    Equivalent to the existing `secret:NAME` / `token_ref` mechanism; chunk 1
    produces the same values as before.
    """

    source = "secret"
    kinds = (KIND_TOKEN, KIND_AWS, KIND_SSH_KEY)
    project_definable = True

    def mint(self, label: str, bottle_id: str,
             settings: Mapping[str, object], secrets: SecretSource) -> MintedCredential:
        kind = str(settings.get("kind", KIND_TOKEN))
        if kind == KIND_TOKEN:
            name = settings.get("secret")
            if not isinstance(name, str) or not name:
                raise ValueError(f"credential {label!r}: source 'secret' with kind 'token' "
                                 f"requires 'secret: NAME'")
            value = secrets.get(name) or ""
            return MintedCredential(KIND_TOKEN, {"token": value}, None, f"secret:{name}")
        if kind == KIND_AWS:
            key_id_name = settings.get("access_key_id")
            secret_name = settings.get("secret_access_key")
            if not isinstance(key_id_name, str) or not isinstance(secret_name, str):
                raise ValueError(f"credential {label!r}: source 'secret' with kind 'aws' "
                                 f"requires 'access_key_id: NAME' and 'secret_access_key: NAME'")
            session_name = settings.get("session_token")
            values: dict[str, str] = {
                "access_key_id": secrets.get(key_id_name) or "",
                "secret_access_key": secrets.get(secret_name) or "",
            }
            if isinstance(session_name, str) and session_name:
                values["session_token"] = secrets.get(session_name) or ""
            return MintedCredential(KIND_AWS, values, None,
                                    f"secret:{key_id_name}+{secret_name}")
        if kind == KIND_SSH_KEY:
            priv_name = settings.get("private_key")
            pub_name = settings.get("public_key")
            if not isinstance(priv_name, str) or not isinstance(pub_name, str):
                raise ValueError(f"credential {label!r}: source 'secret' with kind 'ssh-key' "
                                 f"requires 'private_key: NAME' and 'public_key: NAME'")
            return MintedCredential(KIND_SSH_KEY, {
                "private_key": secrets.get(priv_name) or "",
                "public_key": secrets.get(pub_name) or "",
            }, None, f"secret:{priv_name}")
        raise ValueError(f"credential {label!r}: source 'secret' does not support kind {kind!r}")

    def describe(self, settings: Mapping[str, object]) -> Grant:
        kind = str(settings.get("kind", KIND_TOKEN))
        if kind == KIND_TOKEN:
            name = settings.get("secret", "?")
            return Grant(acts_as="", authority="", scope="",
                         lifetime="doesn't expire", starts_from=f"secrets file {name}")
        if kind == KIND_AWS:
            key_name = settings.get("access_key_id", "?")
            return Grant(acts_as="", authority="", scope="",
                         lifetime="doesn't expire",
                         starts_from=f"secrets file {key_name} (AWS keys)")
        return Grant(acts_as="", authority="", scope="",
                     lifetime="doesn't expire", starts_from="secrets file")


class _PromptProvider(CredentialProvider):
    """Asks the user for a value at launch (equivalent to `"?message"` in env:).

    The value is typed once and held for the bottle's life; it is not stored.
    """

    source = "prompt"
    kinds = (KIND_TOKEN,)
    project_definable = True

    def mint(self, label: str, bottle_id: str,
             settings: Mapping[str, object], secrets: SecretSource) -> MintedCredential:
        message = str(settings.get("message", label))
        # The actual value is collected at launch from the user; the host
        # controller looks it up from the launch answers by label+message.
        # An absent answer is treated as an empty string (the same as today
        # for `"?message"` env entries).
        value = secrets.get(f"__prompt__{label}") or ""
        return MintedCredential(KIND_TOKEN, {"token": value}, None, f"prompted:{message}")

    def describe(self, settings: Mapping[str, object]) -> Grant:
        message = str(settings.get("message", ""))
        return Grant(acts_as="", authority="", scope="",
                     lifetime="doesn't expire",
                     starts_from=f"asks: {message}" if message else "asks at launch")


class _HostLoginProvider(CredentialProvider):
    """The host's own login for a named agent harness (e.g. Codex, Claude).

    `harness: codex` reads `~/.codex/auth.json` via the Codex agent provider,
    returning a short-lived JWT the renewal loop keeps current. Requires the
    host CLI to be logged in; raises `HostLoginError` if not.

    Not `project_definable`: it spends your personal login.
    """

    source = "host_login"
    kinds = (KIND_TOKEN,)
    project_definable = False

    def mint(self, label: str, bottle_id: str,
             settings: Mapping[str, object], secrets: SecretSource) -> MintedCredential:
        harness = str(settings.get("harness", ""))
        if not harness:
            raise ValueError(f"credential {label!r}: source 'host_login' requires 'harness: NAME'")
        # Deferred to avoid import-time circularity with agent_provider, which
        # imports from secret_source.
        from .agent_provider import get_provider  # pylint: disable=import-outside-toplevel
        from .errors import HostLoginError  # pylint: disable=import-outside-toplevel
        try:
            provider = get_provider(harness)
        except Exception as e:  # pylint: disable=broad-exception-caught
            raise ValueError(f"credential {label!r}: unknown harness {harness!r}: {e}") from e
        try:
            token, expires_at = provider.read_host_login()
        except (NotImplementedError, HostLoginError) as e:
            raise ValueError(
                f"credential {label!r}: harness {harness!r} host login: {e}"
            ) from e
        return MintedCredential(KIND_TOKEN, {"token": token}, expires_at,
                                f"host_login:{harness}")

    def describe(self, settings: Mapping[str, object]) -> Grant:
        harness = str(settings.get("harness", ""))
        return Grant(
            acts_as="", authority="", scope="",
            lifetime="until the host login expires",
            starts_from=f"host login ({harness})" if harness else "host login",
        )


class _FileProvider(CredentialProvider):
    """A credential read from files on the host.

    - `kind: ssh-key` (implied): `private_key_path:` names the private key;
      `public_key_path:` names the public half, defaulting to the private
      key's path plus `.pub` when that file exists (git-gate only needs the
      private key, so a missing public key is empty).
    - `kind: token`: `path:` names the file; its trimmed content is the token.

    `~` is expanded. The named files are part of the trust hash (`file_fields`).
    `renew` (the default, mint again) re-reads them.

    Not `project_definable`: a long-lived personal key on the host shouldn't
    reach a project bottle.
    """

    source = "file"
    kinds = (KIND_SSH_KEY, KIND_TOKEN)
    project_definable = False
    file_fields: ClassVar[tuple[str, ...]] = ("path", "private_key_path", "public_key_path")

    def mint(self, label: str, bottle_id: str,
             settings: Mapping[str, object], secrets: SecretSource) -> MintedCredential:
        kind = str(settings.get("kind", KIND_SSH_KEY))
        if kind == KIND_TOKEN:
            path = _required_file(label, settings, "path", "kind 'token'")
            value = path.read_text(encoding="utf-8").strip()
            return MintedCredential(KIND_TOKEN, {"token": value}, None, f"file:{path}")
        if kind == KIND_SSH_KEY:
            priv_path = _required_file(label, settings, "private_key_path", "kind 'ssh-key'")
            pub_setting = settings.get("public_key_path")
            if isinstance(pub_setting, str) and pub_setting:
                pub_path = _required_file(label, settings, "public_key_path", "kind 'ssh-key'")
            else:
                pub_path = Path(str(priv_path) + ".pub")
            pub = pub_path.read_text(encoding="utf-8").strip() if pub_path.is_file() else ""
            return MintedCredential(KIND_SSH_KEY, {
                "private_key": priv_path.read_text(encoding="utf-8"),
                "public_key": pub,
            }, None, f"file:{priv_path}")
        raise ValueError(f"credential {label!r}: source 'file' does not support kind {kind!r}")

    def describe(self, settings: Mapping[str, object]) -> Grant:
        kind = str(settings.get("kind", KIND_SSH_KEY))
        where = settings.get("path" if kind == KIND_TOKEN else "private_key_path", "?")
        return Grant(acts_as="", authority="", scope="",
                     lifetime="doesn't expire", starts_from=f"file: {where}")


def _required_file(label: str, settings: Mapping[str, object], key: str, what: str) -> Path:
    """`settings[key]` as an existing file path, `~` expanded."""
    raw = settings.get(key)
    if not isinstance(raw, str) or not raw:
        raise ValueError(
            f"credential {label!r}: source 'file' with {what} requires "
            f"'{key}: /path/to/file'")
    path = Path(raw).expanduser()
    if not path.is_file():
        raise ValueError(f"credential {label!r}: source 'file': {key} not found: {path}")
    return path


class _GiteaDeployKeyProvider(CredentialProvider):
    """A per-bottle ed25519 deploy key on a Gitea repository.

    Mints a fresh keypair, registers the public half with the Gitea API, and
    revokes it at teardown. `token` must resolve to a Gitea personal access
    token (use `$label` for a credential defined earlier in the same block).

    `project_definable`: a scoped key, revoked at teardown, is safer than a
    raw secret.
    """

    source = "gitea_deploy_key"
    kinds = (KIND_SSH_KEY,)
    project_definable = True

    def mint(self, label: str, bottle_id: str,
             settings: Mapping[str, object], secrets: SecretSource) -> MintedCredential:
        api_url, token, repo, read_only = self._required(label, settings)
        from .deploy_key_provisioner import get_provisioner  # pylint: disable=import-outside-toplevel
        title = f"bot-bottle:{bottle_id}:{label}"
        prov = get_provisioner("gitea", token, api_url)
        # create() generates the keypair, registers the public half with Gitea,
        # and returns (key_id, private_key_bytes).
        key_id, priv_bytes = prov.create(repo, title, read_only=read_only)
        return MintedCredential(
            KIND_SSH_KEY,
            {"private_key": priv_bytes.decode(), "public_key": ""},
            None,
            f"gitea_deploy_key:{repo}",
            retire_data={"key_id": key_id, "api_url": api_url, "token": token, "repo": repo},
        )

    def retire(self, label: str, bottle_id: str, current: MintedCredential) -> None:
        rd = current.retire_data
        key_id = rd.get("key_id", "")
        api_url = rd.get("api_url", "")
        token = rd.get("token", "")
        repo = rd.get("repo", "")
        if not all([key_id, api_url, token, repo]):
            return
        from .deploy_key_provisioner import get_provisioner  # pylint: disable=import-outside-toplevel
        get_provisioner("gitea", token, api_url).delete(repo, key_id)
        log.info("credential %r: revoked Gitea deploy key %s on %s", label, key_id, repo)

    def describe(self, settings: Mapping[str, object]) -> Grant:
        repo = settings.get("repo", "?")
        api_url = settings.get("api_url", "")
        read_only = bool(settings.get("read_only", True))
        return Grant(
            acts_as=f"deploy key on {repo}",
            authority=str(api_url),
            scope="read-only" if read_only else "read-write",
            lifetime="until teardown (key revoked)",
            starts_from="Gitea API",
        )

    @staticmethod
    def _required(label: str, settings: Mapping[str, object]) -> tuple[str, str, str, bool]:
        api_url = str(settings.get("api_url", "")).strip()
        token = str(settings.get("token", "")).strip()
        repo = str(settings.get("repo", "")).strip()
        read_only = bool(settings.get("read_only", True))
        missing = [k for k, v in [("api_url", api_url), ("token", token), ("repo", repo)] if not v]
        if missing:
            raise ValueError(
                f"credential {label!r}: source 'gitea_deploy_key' requires: "
                + ", ".join(missing)
            )
        return api_url, token, repo, read_only


_BUILTIN_PROVIDERS: tuple[CredentialProvider, ...] = (
    _SecretProvider(),
    _PromptProvider(),
    _HostLoginProvider(),
    _FileProvider(),
    _GiteaDeployKeyProvider(),
)


class CredentialRegistry:
    """Maps `source:` strings to their providers. Built-ins first; a user
    plugin under `~/.bot-bottle/contrib/<name>/credential_provider.py` shadows
    a built-in with the same `source` name."""

    def __init__(self, contrib_root: Path | None = None) -> None:
        self._providers: dict[str, CredentialProvider] = {
            p.source: p for p in _BUILTIN_PROVIDERS
        }
        self._load_user_contrib(contrib_root)

    def get(self, source: str) -> CredentialProvider | None:
        """The provider for `source`, or None."""
        return self._providers.get(source)

    def known_sources(self) -> frozenset[str]:
        return frozenset(self._providers)

    def _load_user_contrib(self, contrib_root: Path | None) -> None:
        if contrib_root is None:
            try:
                from .paths import bot_bottle_root  # pylint: disable=import-outside-toplevel
                contrib_root = bot_bottle_root() / "contrib"
            except Exception:  # noqa: BLE001
                return
        if not contrib_root.is_dir():
            return
        for entry in sorted(contrib_root.iterdir()):
            plugin = entry / "credential_provider.py"
            if not plugin.is_file():
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"bb_user_cred_{entry.name}", plugin)
                if spec is None or spec.loader is None:
                    continue
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
                for attr in vars(mod).values():
                    if (isinstance(attr, type) and issubclass(attr, CredentialProvider)
                            and attr is not CredentialProvider
                            and hasattr(attr, "source")):
                        provider = attr()
                        self._providers[provider.source] = provider
            except Exception as exc:  # noqa: BLE001
                log.warning("credential_provider %s: %s", plugin, exc)


# A module-level default registry; the host controller may build its own with
# a custom contrib_root.
_default_registry: CredentialRegistry | None = None


def default_registry() -> CredentialRegistry:
    global _default_registry  # noqa: PLW0603
    if _default_registry is None:
        _default_registry = CredentialRegistry()
    return _default_registry


__all__ = [
    "AUTH_AWS_KINDS", "AUTH_BEARER_KINDS", "ENV_KINDS", "GIT_KEY_KINDS",
    "CredentialProvider", "CredentialRegistry",
    "Grant", "KIND_AWS", "KIND_SSH_KEY", "KIND_TOKEN", "KIND_FIELDS",
    "KNOWN_KINDS", "MintedCredential", "default_registry",
]
