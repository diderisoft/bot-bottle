"""Terminal title and palette escapes for agent sessions."""

from __future__ import annotations

import shlex


# color -> (normal_idx, normal_hex, bright_idx, bright_hex, dark_bg_hex). OSC 4
# sets palette entries; dark_bg_hex is for OSC 11 (default background), which
# also tints TUIs that draw in 24-bit color.
_COLORS: dict[str, tuple[int, str, int, str, str]] = {
    "red":     (9,  "#e74c3c", 1,  "#c0392b", "#200808"),
    "green":   (10, "#2ecc71", 2,  "#27ae60", "#082008"),
    "yellow":  (11, "#f1c40f", 3,  "#d4ac0d", "#201808"),
    "blue":    (12, "#3498db", 4,  "#2471a3", "#080820"),
    "magenta": (13, "#9b59b6", 5,  "#7d3c98", "#160820"),
}

# OSC 104 resets the palette; OSC 111 resets the background.
_RESET_PRINTF = "printf '\\033]104\\007\\033]111\\007'"


def palette_printf(color: str) -> str:
    """`printf` command tinting the terminal for `color` (palette entries and a
    dark background). "" if unknown."""
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
    """A shell script that sets the title and/or palette, runs `agent_argv` and
    resets on exit; None when no decoration is needed."""
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
        # No palette to reset, so exec.
        parts.append(f"exec {shlex.join(agent_argv)}")

    return "; ".join(parts)


def agent_shell_command(
    agent_argv: list[str],
    terminal_title: str = "",
    terminal_color: str = "",
) -> list[str]:
    """`agent_argv`, wrapped in `sh -lc` when a title or palette is set first."""
    script = exec_shell_script(agent_argv, terminal_title, terminal_color)
    return list(agent_argv) if script is None else ["sh", "-lc", script]
