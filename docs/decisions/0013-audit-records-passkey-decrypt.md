# ADR 0013: Audit records are read through the host, unlocked by a passkey

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** didericis

## Context

Each console agent's conversation is kept on the console as audit records, so
its history outlives the bottle and the host's copy. The console is the
system's hub: whoever controls it (a breach, a malicious deploy, later a
hosted operator) holds every paired host's link. Records it could read would
be every operator's conversations in one place.

A first version encrypted records on the host but answered any console
request to decrypt one. That makes the host a decryption oracle for whoever
controls the console: it trusts the console's node id, not the person.

PRD 0104 (passkey-signed actions) and PRD 0114 (the vault), both in the
design repo, describe the passkey and key-custody direction this follows.

## Decision

- **Only the host can decrypt.** Records are encrypted with AES-256-GCM under
  a key in the host's root (`audit.key`, 0600) that is never sent anywhere.
  The console stores ciphertext and hashes, and relays plaintext to the
  browser without storing or logging it.
- **A read needs a passkey signature the host checks itself.** The host
  learns the account's passkey public keys at pairing, and trusts them only
  once the user confirms their fingerprints match the console's. Changing
  them is a local CLI action. To read, the browser signs a challenge the
  host issued; the host verifies the WebAuthn assertion (origin, rp id,
  challenge, counter, signature) with `cryptography`, a dependency of the
  `remote` extra.
- **Once per console session.** A verified signature unlocks reads for that
  console session: 12 hours, or an hour unused. Archive views aren't
  sensitive enough to prompt on each one. Reads are rate-limited and each
  one is journalled on the host.
- **A lost passkey means reading on the host.** The console can't read the
  records without one; the host's CLI, which holds the key, still can.

## Consequences

- A compromised console can't read records: it can't forge a passkey
  signature for a host that pinned the keys. A console compromised *before*
  pairing could offer its own key; the fingerprint check is the defence, so
  `pair` shows them and asks.
- While a session is unlocked, that session's console could read that host's
  records until the token lapses. The window is the session, capped.
- Losing `audit.key` loses the records' readability: `pair` says to back it up.
- Later: changing credentials (secrets, forge tokens) should need a passkey
  signature per change, not per session.
