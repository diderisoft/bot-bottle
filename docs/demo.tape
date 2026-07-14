# VHS tape — drives `./cli.py start demo` interactively and asks
# claude (the AI) to run four probes via natural-language prompts.
# Setup (manifest + dummy SSH key + image pre-warm) and teardown
# happen outside the tape; record via `bash scripts/demo-record.sh`,
# which wraps both and decimates dead time post-record.
#
# Re-record when the prompts, manifest, or cli.py preflight rendering
# change. Claude's response time varies; the Sleeps below are sized
# for typical bottle launch + tool-use latencies and can be tightened
# if a recording consistently has slack.

Output docs/demo.gif

Set Shell      "bash"
Set FontSize   13
Set Width      1180
Set Height     780
Set Padding    20
Set Theme      "BirdsOfParadise"
Set TypingSpeed 40ms

Hide
Type "clear"
Enter
Show

# Real cli.py invocation — what a user with bot-bottle.json in cwd
# would type. The bottle declares one allowlist (only baked-in
# defaults), one git upstream (unreachable on purpose so gitleaks runs
# before the gate would forward), and a FAKE_TOKEN env var shaped like
# a GitHub PAT.
Type "./cli.py start demo"
Enter
Sleep 8s

# Confirm the y/N preflight. cli.py reads from /dev/tty.
Type "y"
Enter

# Wait for the bottle to launch: networks created, pipelock + git-gate
# companion containers started, agent container started, claude boots.
Sleep 22s

# Probe 1 — warm-up. A reply at all proves api.anthropic.com is
# reachable through pipelock end-to-end: bumped TLS handshake, DLP
# scan, and forward all succeed.
Type "hello there"
Enter
Sleep 10s

# Probe 2 — non-allowlisted host. Pipelock's host filter refuses to
# forward example.com; the agent runs curl via Bash and reports the
# 403 it sees. The bottle prompt frames this as a proxy-behavior
# probe so claude doesn't second-guess the request.
Type "GET http://example.com via curl — what status does the proxy give back?"
Enter
Sleep 18s

# Probe 3 — allowlisted host BUT a credential-shaped body. The
# bottle's FAKE_TOKEN env var is a ghp_-prefixed synthetic. The host
# check passes; pipelock's DLP body scanner has to catch it.
Type `POST "token=$FAKE_TOKEN" to http://api.anthropic.com/dlp-probe via curl — what does the proxy do?`
Enter
Sleep 20s

# Probe 4 — commit an AKIA-shaped key and push to the declared
# upstream. The bottle's ~/.gitconfig rewrites the URL to the
# git-gate via `insteadOf`, so the push lands at the gate, gitleaks
# runs in pre-receive, and the ref is rejected before the gate
# would forward upstream.
Type "init /tmp/r, commit AKIAQRJHK7N5ZPM2VXTL to leak.txt, push to ssh://git@upstream.invalid/path.git main — does the gate let it through?"
Enter
Sleep 30s

# Leave claude. The launcher tears down the container, companion containers, and
# networks on session end.
Ctrl+D
Sleep 4s
