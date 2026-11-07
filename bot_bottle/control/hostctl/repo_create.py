"""Creating a repo for an agent's `create-repo` (PRD 0120): check the forge is
connected here and the repo doesn't exist yet, create it with the API as the
user the agent pushes as there, then grant it to the running bottle with
write (`repo_grants`). The token never leaves the host.

A bottle's `git-gate.actions.create` lets the agent create under an owner
without a card; anything else asks the operator.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ...lib import log

if TYPE_CHECKING:
    from ...manifest.forges import ManifestForge

PUBLIC_SUFFIX = ":public"


class CreateRepoError(Exception):
    """The repo can't be created; the message says why, for the agent."""


class AppNotReady(CreateRepoError):
    """The GitHub App can't create repos under the owner yet: the operator
    fixes that on GitHub at `url` (`line` says what, for the card), and the
    card waits for it."""

    def __init__(self, message: str, url: str, line: str) -> None:
        super().__init__(message)
        self.url = url
        self.line = line


@dataclass(frozen=True)
class NewRepo:
    """The repo an agent asked for: `<forge>/<owner>/<name>`, private unless
    it asked for a public one."""

    forge: str
    owner: str
    name: str
    private: bool = True

    @property
    def repo(self) -> str:
        return f"{self.forge}/{self.owner}/{self.name}"

    @property
    def grant(self) -> str:
        return f"{self.repo}:write"


def parse(proposed: str) -> NewRepo:
    """`<forge>/<owner>/<name>[:public]` (the request's `proposed_file`)."""
    text = proposed.strip()
    public = text.endswith(PUBLIC_SUFFIX)
    parts = text.removesuffix(PUBLIC_SUFFIX).split("/")
    if len(parts) != 3 or not all(parts):
        raise CreateRepoError(f"{text!r} isn't <forge>/<owner>/<name>")
    return NewRepo(parts[0], parts[1], parts[2], private=not public)


def _token(forge: "ManifestForge") -> str:
    """The connected user's token: GitHub's app token renewed when due."""
    # pylint: disable=import-outside-toplevel
    from ...contrib.github.user_token import current_token
    from ...forge_kinds import ForgeApiError
    from ...paths import host_secrets_path
    from ...secret_source import FileSecrets
    secrets = FileSecrets(host_secrets_path())
    try:
        token = (current_token(forge.token_secret, secrets) if forge.kind == "github"
                 else secrets.get(forge.token_secret))
    except ForgeApiError as e:
        raise CreateRepoError(str(e)) from e
    if not token:
        raise CreateRepoError(f"this host has no token for {forge.name} "
                              f"(${forge.token_secret}); connect it again")
    return token


def _check_app(new: NewRepo, forge: "ManifestForge", token: str) -> None:
    """Through the GitHub App, a repo can only be created under an account
    the app is installed on, with Administration (write) accepted there.
    CreateRepoError saying what to do on GitHub when it isn't."""
    # pylint: disable-next=import-outside-toplevel
    from ...contrib.github.user_token import install_url, installations
    found = [i for i in installations(token) if i["account"].lower() == new.owner.lower()]
    if not found:
        raise AppNotReady(
            f"the {forge.app} GitHub App isn't installed on {new.owner}, so it can't create "
            f"repos there; the operator installs it at {install_url(forge.app)} (all "
            f"repositories), then ask again", install_url(forge.app),
            f"not installed: {forge.app} isn't on {new.owner}; install it there (all "
            f"repositories), then allow")
    if found[0].get("administration") != "write":
        settings = ("https://github.com/settings/installations"
                    if found[0].get("type") == "User" else
                    f"https://github.com/organizations/{new.owner}/settings/installations")
        raise AppNotReady(
            f"the {forge.app} GitHub App's installation on {new.owner} hasn't accepted the "
            f"Administration (write) permission creating repos needs; the operator accepts "
            f"it at {settings}, then ask again", settings,
            f"not accepted: {forge.app} on {new.owner} needs Administration (write); "
            f"accept its new permissions there, then allow")


def plan(session: str, proposed: str) -> tuple[NewRepo, "ManifestForge"]:
    """The repo and the forge it goes on, checked: connected here, and not
    there yet. CreateRepoError saying why not."""
    # pylint: disable=import-outside-toplevel
    from ...forge_kinds import ForgeApiError, forge_api
    from .repo_grants import find_forge
    new = parse(proposed)
    forge = find_forge(session, new.forge)
    if forge is None:
        raise CreateRepoError(f"this host hasn't connected {new.forge}; ask for it with "
                              f"forge-connect (https://{new.forge}) first")
    token = _token(forge)
    try:
        if forge.kind == "github" and forge.app:
            _check_app(new, forge, token)
        exists = forge_api(forge.kind).repo_exists(forge.url, token, new.owner, new.name)
    except ForgeApiError as e:
        raise CreateRepoError(str(e)) from e
    if exists:
        raise CreateRepoError(f"{new.repo} exists already; ask for it with add-repo")
    return new, forge


def allowed_by_bottle(session: str, new: NewRepo) -> bool:
    """Whether the running bottle's `git-gate.actions.create` allows `new`
    (False when its manifest doesn't load as it is now)."""
    # pylint: disable=import-outside-toplevel
    from ...manifest.git import create_allowed
    from .manifest_view import ManifestViewError, _load
    try:
        manifest = _load(session)[1]  # pylint: disable=protected-access
    except ManifestViewError:
        return False
    return create_allowed(manifest.bottle.git_create, new.repo)


def not_ready_lines(session: str, proposed: str) -> list[str]:
    """The card's lines for fixing the app on GitHub (`install:` the link
    the console opens), [] when nothing there stops creating the repo."""
    try:
        plan(session, proposed)
    except AppNotReady as e:
        return [f"install: {e.url}", e.line]
    except CreateRepoError:
        pass
    return []


def create(new: NewRepo, forge: "ManifestForge") -> str:
    """Create `new` on `forge` as its connected user; its web URL."""
    # pylint: disable-next=import-outside-toplevel
    from ...forge_kinds import ForgeApiError, forge_api
    try:
        url = forge_api(forge.kind).create_repo(
            forge.url, _token(forge), owner=new.owner, name=new.name, private=new.private,
            as_user=new.owner.lower() == forge.account.lower())
    except ForgeApiError as e:
        raise CreateRepoError(str(e)) from e
    log.info(f"created {new.repo} ({'private' if new.private else 'public'}) "
             f"as {forge.account}")
    return url


def _alias(session: str, new: NewRepo) -> str | None:
    """The gate's name for `new` when its own is another repo's in the bottle,
    else None."""
    # pylint: disable-next=import-outside-toplevel
    from .repo_grants import proposed_alias
    return proposed_alias(session, new.grant) or None


def card_lines(request: Any, new: NewRepo, forge: "ManifestForge") -> list[str]:
    """What the phone shows for a `create-repo` it asks about."""
    alias = _alias(request.session, new)
    then = "then: granted to this bottle with write"
    if alias is not None:
        then += f", as {alias} (its name is another repo's here)"
    lines = [f"create: {new.repo} ({'private' if new.private else 'public'})",
             f"as: {forge.account} on {forge.name}", then]
    if request.justification.strip():
        lines.append(f"why: {request.justification.strip()}")
    return lines


def create_and_grant(session: str, new: NewRepo, forge: "ManifestForge",
                     grant_repo: Any) -> str:
    """Create `new`, grant it to `session` with write, and return the note
    the agent reads. CreateRepoError when either step fails (a repo created
    but not granted says so: the agent can add-repo it)."""
    alias = _alias(session, new)
    url = create(new, forge)
    grant, named = new.grant, ""
    if alias is not None:
        grant, named = f"{alias}={new.grant}", f" (as {alias} in the gate)"
    result = grant_repo(session, grant) if grant_repo is not None else {
        "error": "this host controller can't grant repos"}
    if result.get("error"):
        raise CreateRepoError(f"created {new.repo} ({url}), but granting it failed: "
                              f"{result['error']}; ask for it with add-repo")
    return (f"created {new.repo} ({'private' if new.private else 'public'}, {url}) and "
            f"granted it with write{named}; "
            f"push to its normal URL: {result.get('url', url)}")


__all__ = ["AppNotReady", "CreateRepoError", "NewRepo", "PUBLIC_SUFFIX", "allowed_by_bottle",
           "card_lines", "create", "create_and_grant", "not_ready_lines", "parse", "plan"]
