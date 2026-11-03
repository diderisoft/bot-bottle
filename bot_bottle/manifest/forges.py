"""Forges and one-line repo grants (PRD 0103), and the `credentials:` block
they take their token from (PRD 0097, the `secret` source so far).

A forge is declared in a bottle, under `git-gate.forges`, and shared through
`extends`. A bottle that has one in its chain grants repos on it in a line:

  credentials:
    gitea-api: { source: secret, secret: GITEA_TOKEN }
  git-gate:
    forges:
      gitea.example.com:
        kind: gitea
        url: https://gitea.example.com
        ssh: ssh://git@gitea.example.com:2222
        token: $gitea-api
        host_key: ssh-ed25519 AAAA...
    repos:
      - gitea.example.com/acme/app:write
      - gitea.example.com/acme/lib

Each grant expands, while `extends` is resolved, into today's long-form repo
entry: a deploy key minted per launch through the forge, read-only unless
`:write`. Everything downstream sees only the long form. A bottle file can't
write that long form itself for a forge-minted key (ADR 0008): its forge is
declared once, so a repo on it is a grant. The long form stays for a `static`
key, an operator's own key file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from ..contract.forges import FORGE_KINDS
from .legacy_git_gate import OldGitGateFormatError, old_repos
from .util import ManifestError, as_json_object

CREDENTIAL_SOURCES = ("secret",)
_CREDENTIAL_KEYS = {"source", "secret", "kind"}
_FORGE_KEYS = {"kind", "url", "ssh", "token", "account", "host_key", "app", "transport"}
# A forge's key: its host, or `<account>@<host>` for another account on it.
_FORGE_NAME_RE = re.compile(r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9.-]+(?::[0-9]+)?$")
_REPO_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_LABEL_RE = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class ManifestForge:
    """One account on one forge, from `git-gate.forges.<name>`. `token_secret`
    is the host secret its token comes from, resolved through `credentials:`."""

    name: str
    kind: str
    url: str
    ssh: str
    token_secret: str
    account: str = ""
    host_key: str = ""
    # A GitHub forge reached through the bot-bottle GitHub App's user token
    # (ADR 0012): the app's slug, for its install link.
    app: str = ""
    # GitHub only: "deploy-key" opts its repos into a key minted per launch
    # instead of HTTPS with the token (ADR 0014). Empty is the default.
    transport: str = ""

    def key(self, writable: bool) -> dict[str, object]:
        """The long-form `key` block a grant on this forge expands to."""
        key: dict[str, object] = {"provider": self.kind, "forge_token_env": self.token_secret,
                                  "api_url": self.url, "read_only": not writable}
        if self.transport:
            key["transport"] = self.transport
        return key


def _str(d: dict[str, object], key: str, label: str, *, required: bool = False) -> str:
    value = d.get(key, "")
    if not isinstance(value, str):
        raise ManifestError(f"{label}.{key} must be a string")
    if required and not value:
        raise ManifestError(f"{label} is missing {key!r}")
    return value


def credentials_raw(bottle_name: str, raw: dict[str, object]) -> dict[str, object]:
    """The bottle's own `credentials:`, checked; {} when it has none."""
    if "credentials" not in raw:
        return {}
    creds = as_json_object(raw["credentials"], f"bottle '{bottle_name}' credentials")
    for label, entry in creds.items():
        where = f"bottle '{bottle_name}' credentials.{label}"
        if not _LABEL_RE.match(label):
            raise ManifestError(f"{where}: a label is lowercase letters, digits and -")
        d = as_json_object(entry, where)
        unknown = set(d) - _CREDENTIAL_KEYS
        if unknown:
            raise ManifestError(f"{where} has unknown key {sorted(unknown)[0]!r}; "
                                f"allowed: {', '.join(sorted(_CREDENTIAL_KEYS))}")
        source = _str(d, "source", where, required=True)
        if source not in CREDENTIAL_SOURCES:
            raise ManifestError(f"{where}: source {source!r} isn't supported yet; "
                                f"supported: {', '.join(CREDENTIAL_SOURCES)}")
        _str(d, "secret", where, required=True)
        kind = _str(d, "kind", where)
        if kind not in ("", "token"):
            raise ManifestError(f"{where}: kind {kind!r} isn't supported yet; supported: token")
    return creds


def forges_raw(bottle_name: str, raw: dict[str, object]) -> dict[str, object]:
    """The bottle's own `git-gate.forges`, checked for shape; {} when none."""
    git = raw.get("git-gate")
    if git is None:
        return {}
    git_d = as_json_object(git, f"bottle '{bottle_name}' git-gate")
    if "forges" not in git_d:
        return {}
    forges = as_json_object(git_d["forges"], f"bottle '{bottle_name}' git-gate.forges")
    for name, entry in forges.items():
        where = f"bottle '{bottle_name}' git-gate.forges[{name!r}]"
        if not _FORGE_NAME_RE.match(name):
            raise ManifestError(f"{where}: a forge is named by its host, or "
                                f"<account>@<host> for another account on it")
        d = as_json_object(entry, where)
        unknown = set(d) - _FORGE_KEYS
        if unknown:
            raise ManifestError(f"{where} has unknown key {sorted(unknown)[0]!r}; "
                                f"allowed: {', '.join(sorted(_FORGE_KEYS))}")
        kind = _str(d, "kind", where) or "gitea"
        if kind not in FORGE_KINDS:
            raise ManifestError(f"{where}: kind {kind!r} isn't supported yet; "
                                f"supported: {', '.join(FORGE_KINDS)}")
        url = _str(d, "url", where, required=True)
        if urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
            raise ManifestError(f"{where}.url must be an http(s) URL (was {url!r})")
        token = _str(d, "token", where, required=True)
        if not token.startswith("$"):
            raise ManifestError(f"{where}.token must name a credential, as $label")
        for key in ("ssh", "account", "host_key", "app"):
            _str(d, key, where)
        if d.get("app") and kind != "github":
            raise ManifestError(f"{where}: app is for kind github (the bot-bottle GitHub App)")
    return forges


def merge_named(earlier: dict[str, object], later: dict[str, object],
                what: str) -> dict[str, object]:
    """Two parents' entries by name. The same name declared differently is an
    error, not a silent pick."""
    for name, entry in later.items():
        if name in earlier and earlier[name] != entry:
            raise ManifestError(f"{what} {name!r} is declared differently by two "
                                f"bottles this one extends; declare it in this bottle "
                                f"to choose, or in only one of them")
    return {**earlier, **later}


def resolve_forge(name: str, raw: object, credentials: dict[str, object],
                  bottle_name: str) -> ManifestForge:
    """`git-gate.forges.<name>` with its token credential resolved."""
    d = as_json_object(raw, f"git-gate.forges[{name!r}]")
    where = f"bottle '{bottle_name}' git-gate.forges[{name!r}]"
    label = str(d["token"])[1:]
    if label not in credentials:
        raise ManifestError(f"{where}.token: no credential {label!r} in this bottle "
                            f"or the bottles it extends")
    cred = as_json_object(credentials[label], f"credentials.{label}")
    url = str(d["url"]).rstrip("/")
    host = urlparse(url).hostname or ""
    return ManifestForge(
        name=name, kind=str(d.get("kind") or "gitea"), url=url,
        ssh=str(d.get("ssh") or f"ssh://git@{host}").rstrip("/"),
        token_secret=str(cred["secret"]), account=str(d.get("account", "")),
        host_key=str(d.get("host_key", "")), app=str(d.get("app", "")),
        transport=_transport(d, where),
    )


def _transport(d: dict[str, object], where: str) -> str:
    transport = str(d.get("transport") or "")
    allowed = ("https", "deploy-key") if d.get("kind") == "github" else ("deploy-key",)
    if transport and transport not in allowed:
        raise ManifestError(f"{where}.transport must be {' or '.join(allowed)} "
                            f"(was {transport!r})")
    return transport


def split_grant_alias(grant: str) -> tuple[str | None, str]:
    """If `grant` starts with `<alias>=`, return `(alias, bare_grant)`; else
    `(None, grant)`. The alias becomes the repo's local name in git-gate,
    letting two repos with the same upstream name coexist in one bottle."""
    eq = grant.find("=")
    if eq > 0 and _REPO_PART_RE.match(grant[:eq]) and "/" in grant[eq + 1:]:
        return grant[:eq], grant[eq + 1:]
    return None, grant


def parse_grant(grant: str, where: str) -> tuple[str, str, str, bool]:
    """`<forge>/<owner>/<repo>[:write]` as (forge, owner, repo, writable)."""
    body, writable = grant, False
    if grant.endswith(":write"):
        body, writable = grant[:-len(":write")], True
    parts = body.split("/")
    if len(parts) != 3 or not all(parts):
        raise ManifestError(f"{where}: a repo grant is <forge>/<owner>/<repo>[:write] "
                            f"(was {grant!r})")
    forge, owner, repo = parts
    repo = repo.removesuffix(".git")
    if not (_REPO_PART_RE.match(owner) and _REPO_PART_RE.match(repo)):
        raise ManifestError(f"{where}: {grant!r} has an invalid owner or repo name")
    return forge, owner, repo, writable


def normalize_repos(bottle_name: str, raw: dict[str, object]) -> dict[str, object]:
    """`raw` with `git-gate.repos` as a mapping: a list of grants becomes
    `{<repo>: <grant>}`. Other forms are left for the long-form parser."""
    git = raw.get("git-gate")
    if git is None:
        return raw
    git_d = as_json_object(git, f"bottle '{bottle_name}' git-gate")
    repos = git_d.get("repos")
    if not isinstance(repos, list):
        return raw
    mapped: dict[str, object] = {}
    for i, item in enumerate(repos):
        where = f"bottle '{bottle_name}' git-gate.repos[{i}]"
        if not isinstance(item, str):
            raise ManifestError(f"{where}: a list of repos holds grants, "
                                f"<forge>/<owner>/<repo>[:write]")
        repo = parse_grant(item, where)[2]
        if repo in mapped:
            raise ManifestError(f"{where}: two grants for repos named {repo!r}; use the "
                                f"mapping form to name them apart")
        mapped[repo] = item
    return {**raw, "git-gate": {**git_d, "repos": mapped}}


def refuse_minted_long_form(bottle_name: str, repos: dict[str, object],
                            chain_forges: dict[str, object] | None = None) -> None:
    """A bottle file's own repos, as written: a key a forge mints is declared
    as a grant on that forge, never in long form (ADR 0008). The error names
    every such repo and how to convert them (`manifest/legacy_git_gate.py`)."""
    if old_repos(repos, FORGE_KINDS):
        raise OldGitGateFormatError(bottle_name, repos, FORGE_KINDS,
                                    chain_forges=chain_forges)


def expand_grants(bottle_name: str, raw: dict[str, object], forges: dict[str, object],
                  credentials: dict[str, object]) -> dict[str, object]:
    """`raw` with each grant in `git-gate.repos` replaced by its long form, using
    the forges and credentials of the bottle's whole chain."""
    raw = normalize_repos(bottle_name, raw)
    git = raw.get("git-gate")
    if git is None:
        return raw
    git_d = as_json_object(git, f"bottle '{bottle_name}' git-gate")
    repos = git_d.get("repos")
    if isinstance(repos, dict):
        refuse_minted_long_form(bottle_name, repos, forges)
    if not isinstance(repos, dict) or not any(isinstance(v, str) for v in repos.values()):
        return raw
    expanded: dict[str, object] = {}
    for name, entry in repos.items():
        if not isinstance(entry, str):
            expanded[name] = entry
            continue
        where = f"bottle '{bottle_name}' git-gate.repos[{name!r}]"
        forge_name, owner, repo, writable = parse_grant(entry, where)
        if forge_name not in forges:
            raise ManifestError(
                f"{where}: no bottle in this chain declares the forge {forge_name!r}; "
                f"extend one that does (`bot-bottle forge connect` writes one)")
        forge = resolve_forge(forge_name, forges[forge_name], credentials, bottle_name)
        long_form: dict[str, object] = {
            "url": f"{forge.ssh}/{owner}/{repo}.git", "key": forge.key(writable)}
        if forge.host_key:
            long_form["host_key"] = forge.host_key
        expanded[name] = long_form
    return {**raw, "git-gate": {**git_d, "repos": expanded}}


def strip_chain_keys(raw: dict[str, object]) -> dict[str, object]:
    """`raw` without `credentials` and `git-gate.forges`, which `extends`
    resolves and the bottle parser doesn't read."""
    out = {k: v for k, v in raw.items() if k != "credentials"}
    git = out.get("git-gate")
    if isinstance(git, dict) and "forges" in git:
        out["git-gate"] = {k: v for k, v in git.items() if k != "forges"}
    return out


__all__ = [
    "FORGE_KINDS", "ManifestForge", "credentials_raw", "expand_grants", "forges_raw",
    "merge_named", "normalize_repos", "parse_grant", "refuse_minted_long_form", "resolve_forge",
    "split_grant_alias",
    "strip_chain_keys",
]
