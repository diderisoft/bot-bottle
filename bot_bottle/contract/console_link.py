"""The console link's protocol: what a host and the console say to each other
(ADR 0024). The console vendors this file verbatim; keep it stdlib-only and
free of imports from the rest of bot-bottle.

`LINK_PROTOCOL` is not the release version, and not `compat.PROTOCOL_VERSION`
(agent images and infra). Bump it when a host of the old protocol and a console
of the new one (or the reverse) would misunderstand each other: a message type
removed or renamed, a field one side requires added or changed in meaning, or
an answer moved to another message. Adding a message type the other side may
ignore doesn't need a bump.

Each side says its protocol in its `hello`. The console speaks a range and
marks a host outside it outdated (or itself, when the host is newer); a host
that sends no `hello` speaks protocol 0. An outdated host is sent only
`FROZEN` messages, which every protocol keeps as they are, so it can always be
upgraded.
"""

from __future__ import annotations

# 2: `approval` with a `request_id` is answered with `approval_answered`
# (`message`, or `error` when it wasn't applied), so the console can say why.
LINK_PROTOCOL = 2

# Messages no protocol may change: the hello, the heartbeat and the upgrade,
# so a console can still reach and upgrade a host of any protocol.
FROZEN: frozenset[str] = frozenset({
    "hello", "ping", "list_local",
    "upgrade_challenge", "upgrade", "upgrade_status",
})

# Every message type a host accepts from the console. A host answers any other
# with `unsupported` (`type`, `request_id`, `message_type`, `error`).
CONSOLE_MESSAGES: frozenset[str] = frozenset({
    # the link
    "hello", "ping", "list_local",
    # agents
    "launch", "resume", "stop", "rename", "snapshot", "refresh_credentials",
    "metrics_watch",
    # terminals
    "input", "resize", "history",
    # supervise cards: allow/deny, and the answers in CARD_ANSWERS
    "approval", "resolve_canary", "operator_egress_allow",
    "forge_connect_answer", "add_repo_answer", "create_repo_answer",
    "launch_bottle_answer", "launch_trust_answer",
    # manifests and the library
    "manifest", "bottle_chain", "bottle_write", "manifest_save", "manifest_transfer",
    "library", "resolve", "library_sync", "library_moved", "library_conflict",
    "project_bottles",
    "bottle_suggest", "bottle_suggest_status", "bottle_suggestion_accept",
    "bottle_suggestion_edit", "bottle_suggestion_reply",
    # forges and secrets
    "forge_connect", "forge_status", "forge_repos", "forge_remove",
    "github_authorize_start", "github_status", "list_secrets", "set_secret",
    # workspace files
    "workspace_file", "put_workspace_file",
    "upload_begin", "upload_chunk", "upload_end", "upload_abort",
    # transcripts and audit
    "transcript", "transcript_search",
    "audit_query", "audit_head", "audit_unlock_challenge", "audit_unlock",
    "decrypt_audit_records",
    # upgrades
    "upgrade_challenge", "upgrade", "upgrade_status",
    # the account's bottle architect host and connection
    "architect",
})

# A card's option (in `attention.options`) -> the message that answers it.
# A card with none of these takes `approval` (allow, allow_project, deny);
# each of these takes `approval` only to deny.
CARD_ANSWERS: dict[str, str] = {
    "forge_connect": "forge_connect_answer",
    "add_repo": "add_repo_answer",
    "create_repo": "create_repo_answer",
    "launch_bottle": "launch_bottle_answer",
}

# The reply to a message type the host doesn't accept.
UNSUPPORTED = "unsupported"

__all__ = ["CARD_ANSWERS", "CONSOLE_MESSAGES", "FROZEN", "LINK_PROTOCOL", "UNSUPPORTED"]
