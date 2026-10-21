"""Terminal escape-sequence helpers shared across all bottle backends."""

from __future__ import annotations

import shlex


# color name → (normal_idx, normal_hex, bright_idx, bright_hex, dark_bg_hex)
# OSC 4 sets indexed palette entries (affects syntax-highlighted code and any
# TUI content that uses indexed colors).  dark_bg_hex is used for OSC 11
# (default background) — a very dark tint that's visible even when the TUI
# uses true/24-bit colors for its own chrome, which would otherwise bypass
# the palette entirely.
_COLORS: dict[str, tuple[int, str, int, str, str]] = {
    "red":     (9,  "#e74c3c", 1,  "#c0392b", "#200808"),
    "green":   (10, "#2ecc71", 2,  "#27ae60", "#082008"),
    "yellow":  (11, "#f1c40f", 3,  "#d4ac0d", "#201808"),
    "blue":    (12, "#3498db", 4,  "#2471a3", "#080820"),
    "magenta": (13, "#9b59b6", 5,  "#7d3c98", "#160820"),
}

# OSC 104 resets all indexed palette entries; OSC 111 resets default background.
_RESET_PRINTF = "printf '\\033]104\\007\\033]111\\007'"


def palette_printf(color: str) -> str:
    """Shell `printf` command that emits OSC 4 + OSC 11 to tint the terminal
    for *color*: sets the normal/bright palette entries AND the default
    background to a dark shade of that color.  Returns '' if unknown."""
    entry = _COLORS.get(color)
    if not entry:
        return ""
    n_idx, n_hex, b_idx, b_hex, bg_hex = entry
    seq = (
        f"\\033]4;{n_idx};{n_hex}\\007"
        f"\\033]4;{b_idx};{b_hex}\\007"
        f"\\033]11;{bg_hex}\\007"
    )
    return f"printf '{seq}'"


def exec_shell_script(
    agent_argv: list[str],
    terminal_title: str = "",
    terminal_color: str = "",
) -> str | None:
    """Build a shell script string that optionally sets the terminal
    title and/or palette before running *agent_argv*, and resets the
    palette + background on exit. Returns None when no decoration is
    needed — callers should run *agent_argv* directly in that case."""
    title_cmd = (
        f"printf '\\033]0;%s\\007' {shlex.quote(terminal_title)}"
        if terminal_title else ""
    )
    pal_cmd = palette_printf(terminal_color)

    if not title_cmd and not pal_cmd:
        return None

    parts: list[str] = []
    if title_cmd:
        parts.append(title_cmd)
    if pal_cmd:
        parts.append(pal_cmd)
        parts.append(shlex.join(agent_argv))
        parts.append(_RESET_PRINTF)
    else:
        # No palette change — exec so the agent replaces the shell.
        parts.append(f"exec {shlex.join(agent_argv)}")

    return "; ".join(parts)


def agent_shell_command(
    agent_argv: list[str],
    terminal_title: str = "",
    terminal_color: str = "",
) -> list[str]:
    """*agent_argv*, wrapped in `sh -lc` when the terminal needs a title
    or palette set first (see `exec_shell_script`)."""
    script = exec_shell_script(agent_argv, terminal_title, terminal_color)
    return list(agent_argv) if script is None else ["sh", "-lc", script]
