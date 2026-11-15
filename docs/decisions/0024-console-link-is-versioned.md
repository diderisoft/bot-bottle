# ADR 0024: The console link has a protocol version, and the console gates on it

- **Status:** Accepted
- **Date:** 2026-11-14
- **Deciders:** didericis

## Context

A host and the console talk over one link of JSON messages
([ADR 0007](0007-remote-access-terminates-in-host-controller.md)). They ship
separately: a host runs the release it installed, and the console is deployed
when it's deployed. Nothing told either side what the other spoke. A message
one side didn't know was dropped (logged at debug on the host), and a request
waited out its timeout. Either way the operator saw only "nothing happened".

The case that showed it: a child-bottle (`launch-bottle`) card is answered
with `launch_bottle_answer`, but the console answered it with `approval`,
which a host refuses for that card. Both sides were current, so a version
alone wouldn't have caught it. The answer for each card wasn't written down
anywhere both sides check.

`compat.PROTOCOL_VERSION` already exists, but it covers agent images and
infra, which change for other reasons.

## Decision

- **One contract, in bot-bottle.** `bot_bottle/contract/console_link.py`
  holds the link's protocol version (`LINK_PROTOCOL`, an integer), every
  message type a host accepts (`CONSOLE_MESSAGES`), the message that answers
  each special card (`CARD_ANSWERS`), and the messages no version may change
  (`FROZEN`). It is stdlib-only, and the console vendors it verbatim.
- **Both ends say their protocol.** Each side opens the stream with `hello`.
  The host's carries `protocol` and its `version`, and is the first thing it
  sends. The console's carries `protocols`, the `[min, max]` it speaks.
- **The console gates.** A host whose protocol is outside the console's
  range is **outdated** (or the console is, when the host is newer). A host
  that sends no `hello` speaks protocol 0. The console sends an outdated host
  only `FROZEN` messages (hello, heartbeat and upgrade), and shows the upgrade
  prompt in place of the host's agents and actions until it's upgraded
  ([ADR 0022](0022-console-upgrades-hosts-with-a-passkey.md)).
- **The host says what it doesn't speak.** A message type outside
  `CONSOLE_MESSAGES` gets an `unsupported` reply, carrying the request's
  `request_id` and an `error`, and a warning in the log. A console of
  another protocol is logged too, but the host doesn't refuse it: the console
  gates, so an older console keeps working with a newer host.
- **When to bump.** `LINK_PROTOCOL` goes up when the two would misunderstand
  each other: a message type removed or renamed, a field one side requires
  added or changed in meaning, or an answer moved to another message. Adding
  a message the other side may ignore doesn't need a bump. `FROZEN` messages
  never change shape.
- **Checked in both repos.** bot-bottle's tests check that the host handles
  exactly `CONSOLE_MESSAGES`, and answers cards with exactly the
  `CARD_ANSWERS` types. The console's tests check that every message it sends
  is in its vendored copy and that each card is answered as `CARD_ANSWERS`
  says.

## Consequences

- A host that doesn't speak the console's protocol is shown as outdated with
  an upgrade prompt, not as a host whose buttons do nothing.
- A release must ship before a console that needs its protocol is deployed.
  Otherwise every host is outdated and has no release to upgrade to. Hosts
  from before this record send no `hello`, so they are outdated to a console
  that requires protocol 1. They're upgraded once, and from then on only a
  bump marks hosts outdated.
- The upgrade flow is frozen. Changing it needs a new message beside the old
  one, not a change to the old one.
- The vendored contract can fall behind bot-bottle's. That is a version
  mismatch, which the console reports, rather than a silent one.

## Links

- [ADR 0007](0007-remote-access-terminates-in-host-controller.md): the link.
- [ADR 0022](0022-console-upgrades-hosts-with-a-passkey.md): upgrades.
- [host-controller.md](../reference/host-controller.md#remote-access).
