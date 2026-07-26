"""tui.py — minimal curses filter-select picker for CLI prompts.

Exposed surface:

  filter_select(items, *, title="", tty_path="/dev/tty") -> str | None
  name_color_modal(default_label, *, tty_path="/dev/tty") -> (str, str)

Opens /dev/tty directly so the picker works even when stdout/stdin are
redirected.  Returns the selected item or None on cancel.
"""

from __future__ import annotations

import curses
import os
import sys
from typing import Any, Optional

from ..log import debug


def filter_multiselect(
    items: list[str],
    *,
    title: str = "",
    initial: Optional[list[str]] = None,
    tty_path: str = "/dev/tty",
) -> Optional[list[str]]:
    """Render a multi-select picker over *items*.

    Returns the ordered list of selected items, or ``None`` if the user
    cancelled (Esc / ``q`` / Ctrl-C / Ctrl-D with no items).

    Press Space to toggle the item under the cursor.
    Press Enter to confirm the current selection.
    Press Ctrl-D to confirm the current selection (returns even if empty).
    Press Esc/q to cancel (returns None).

    *initial* pre-populates the selection in insertion order. Items
    added are appended; removed items leave the remaining order unchanged.
    """
    if not items:
        return []

    try:
        tty_fd = open(tty_path, "r+b", buffering=0)
    except OSError as exc:
        debug(
            "multi-select unavailable; treating it as cancellation",
            context={"error_type": type(exc).__name__, "tty": tty_path},
        )
        return None

    try:
        fd_dup = os.dup(tty_fd.fileno())
        return _run_multiselect(
            items, title=title, initial=list(initial or []), tty_fd=fd_dup
        )
    finally:
        tty_fd.close()


def filter_select(
    items: list[str],
    *,
    title: str = "",
    tty_path: str = "/dev/tty",
) -> Optional[str]:
    """Render a filter-select picker over *items*.

    Returns the selected item string, or ``None`` if the user cancelled
    (Esc / ``q`` / Ctrl-C / Ctrl-D) or if the terminal is too small.

    The picker opens *tty_path* directly so it works even when
    stdout/stdin are redirected.
    """
    if not items:
        return None

    try:
        tty_fd = open(tty_path, "r+b", buffering=0)
    except OSError as exc:
        debug(
            "filter-select unavailable; treating it as cancellation",
            context={"error_type": type(exc).__name__, "tty": tty_path},
        )
        return None

    try:
        # Use os.dup() to duplicate the fd so the original file object
        # and FileIO in _run_picker each manage independent copies,
        # preventing double-close errors.
        fd_dup = os.dup(tty_fd.fileno())
        return _run_picker(items, title=title, tty_fd=fd_dup)
    finally:
        tty_fd.close()


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------

_KEY_ESC = 27
_KEY_CTRL_C = 3
_KEY_CTRL_D = 4
_KEY_BACKSPACE_WIN = 8
_KEY_ENTER_ALT = 10

_CANCEL_KEYS = frozenset([_KEY_ESC, _KEY_CTRL_C, _KEY_CTRL_D, ord("q")])


def _run_picker(items: list[str], *, title: str, tty_fd: int) -> Optional[str]:
    """Drive a curses session on *tty_fd* and return the picked item."""
    # newterm lets us run curses on an arbitrary fd rather than the
    # process's controlling tty / stdout — crucial when stdout is piped.
    os.environ.setdefault("TERM", "xterm-256color")

    # Save / restore the real stdin/stdout so curses newterm can use tty_fd.
    orig_stdin = sys.__stdin__
    orig_stdout = sys.__stdout__

    try:
        import io
        tty_text = io.TextIOWrapper(io.FileIO(tty_fd, mode='r+'), write_through=True)
        sys.__stdin__ = tty_text   # type: ignore[assignment]
        sys.__stdout__ = tty_text  # type: ignore[assignment]

        # curses.wrapper calls initscr which honours sys.__stdin__ / __stdout__
        # on some builds; use newterm where available.
        screen = curses.initscr()
        curses.noecho()
        curses.cbreak()
        screen.keypad(True)

        try:
            result = _picker_loop(screen, items, title=title)
        finally:
            screen.keypad(False)
            curses.nocbreak()
            curses.echo()
            curses.endwin()
    except Exception as exc:  # noqa: W0718 — curses can raise many error types
        debug(
            "filter-select display failed; treating it as cancellation",
            context={"error_type": type(exc).__name__},
        )
        return None
    finally:
        sys.__stdin__ = orig_stdin    # type: ignore[assignment]
        sys.__stdout__ = orig_stdout  # type: ignore[assignment]

    return result


def _picker_loop(screen: Any, items: list[str], *, title: str) -> Optional[str]:
    query = ""
    cursor = 0

    while True:
        filtered = _filter_items(items, query)

        # Clamp cursor into the visible list.
        if not filtered:
            cursor = 0
        elif cursor >= len(filtered):
            cursor = len(filtered) - 1

        try:
            _render(screen, filtered, cursor, query=query, title=title)
        except curses.error:
            # Terminal too small or write error — bail out.
            return None

        try:
            key = screen.getch()
        except KeyboardInterrupt:
            return None

        if key in _CANCEL_KEYS:
            return None

        if key in (curses.KEY_ENTER, _KEY_ENTER_ALT, ord("\r")):
            return filtered[cursor] if filtered else None

        if key in (curses.KEY_UP, ord("k")):
            if cursor > 0:
                cursor -= 1

        elif key in (curses.KEY_DOWN, ord("j")):
            if cursor < len(filtered) - 1:
                cursor += 1

        elif key in (curses.KEY_BACKSPACE, _KEY_BACKSPACE_WIN, 127):
            query = query[:-1]
            # After narrowing the filter, keep cursor in range.
            new_filtered = _filter_items(items, query)
            if cursor >= len(new_filtered):
                cursor = max(0, len(new_filtered) - 1)

        elif 32 <= key <= 126:
            # Printable ASCII — append to query and reset cursor so the
            # top of the newly-filtered list is selected.
            query += chr(key)
            cursor = 0


def _filter_items(items: list[str], query: str) -> list[str]:
    if not query:
        return list(items)
    q = query.lower()
    return [i for i in items if q in i.lower()]


def _render(screen: Any, filtered: list[str], cursor: int, *, query: str, title: str) -> None:
    screen.erase()
    rows, cols = screen.getmaxyx()
    min_rows = 5

    if rows < min_rows:
        raise curses.error("terminal too small")

    row = 0

    if title and row < rows - 1:
        _addstr_safe(screen, row, 0, title[:cols - 1], curses.A_BOLD)
        row += 1

    filter_label = f"Filter: {query}"
    if row < rows - 1:
        _addstr_safe(screen, row, 0, filter_label[:cols - 1])
        row += 1

    sep = "─" * min(cols - 1, 40)
    if row < rows - 1:
        _addstr_safe(screen, row, 0, sep)
        row += 1

    list_start = row
    # Reserve two rows for separator + help line at bottom.
    list_rows = rows - list_start - 2
    if list_rows < 1:
        return

    # Scroll window: keep cursor visible.
    scroll = max(0, cursor - list_rows + 1)
    visible = filtered[scroll: scroll + list_rows]

    for idx, item in enumerate(visible):
        abs_idx = scroll + idx
        attr = curses.A_REVERSE if abs_idx == cursor else curses.A_NORMAL
        prefix = "> " if abs_idx == cursor else "  "
        line = (prefix + item)[:cols - 1]
        if row < rows - 1:
            _addstr_safe(screen, row, 0, line, attr)
        row += 1

    if row < rows - 1:
        _addstr_safe(screen, row, 0, sep)
        row += 1

    help_line = "[↑↓/jk] move  [Enter] select  [Esc/q] cancel"
    if row < rows:
        _addstr_safe(screen, min(rows - 1, row), 0, help_line[:cols - 1])

    screen.refresh()


def _addstr_safe(screen: Any, row: int, col: int, text: str, attr: int = curses.A_NORMAL) -> None:
    try:
        screen.addstr(row, col, text, attr)
    except curses.error:
        pass


# ---------------------------------------------------------------------------
# filter_multiselect internals
# ---------------------------------------------------------------------------

_KEY_SPACE = 32


def _run_multiselect(
    items: list[str], *, title: str, initial: list[str], tty_fd: int
) -> Optional[list[str]]:
    """Drive a curses multi-select session on *tty_fd*."""
    os.environ.setdefault("TERM", "xterm-256color")

    orig_stdin = sys.__stdin__
    orig_stdout = sys.__stdout__

    try:
        import io
        tty_text = io.TextIOWrapper(io.FileIO(tty_fd, mode='r+'), write_through=True)
        sys.__stdin__ = tty_text   # type: ignore[assignment]
        sys.__stdout__ = tty_text  # type: ignore[assignment]

        screen = curses.initscr()
        curses.noecho()
        curses.cbreak()
        screen.keypad(True)

        try:
            result = _multiselect_loop(screen, items, title=title, initial=initial)
        finally:
            screen.keypad(False)
            curses.nocbreak()
            curses.echo()
            curses.endwin()
    except Exception as exc:  # noqa: W0718
        debug(
            "multi-select display failed; treating it as cancellation",
            context={"error_type": type(exc).__name__},
        )
        return None
    finally:
        sys.__stdin__ = orig_stdin    # type: ignore[assignment]
        sys.__stdout__ = orig_stdout  # type: ignore[assignment]

    return result


def _toggle_membership(items: list[str], item: str) -> None:
    """Add `item` if absent, remove it if present (in place)."""
    if item in items:
        items.remove(item)
    else:
        items.append(item)


def _handle_order_key(key: int, selected: list[str], order_cursor: int) -> int:
    """Apply a keypress in 'order' focus: navigate, reorder, or remove the
    item at `order_cursor`. Mutates `selected` in place and returns the new
    order cursor."""
    if key in (curses.KEY_UP, ord("k")):
        if order_cursor > 0:
            order_cursor -= 1
    elif key in (curses.KEY_DOWN, ord("j")):
        if order_cursor < len(selected) - 1:
            order_cursor += 1
    elif key == ord("K"):
        # Move selected item up (earlier in order).
        if order_cursor > 0:
            i = order_cursor
            selected[i - 1], selected[i] = selected[i], selected[i - 1]
            order_cursor -= 1
    elif key == ord("J"):
        # Move selected item down (later in order).
        if order_cursor < len(selected) - 1:
            i = order_cursor
            selected[i], selected[i + 1] = selected[i + 1], selected[i]
            order_cursor += 1
    elif key in (curses.KEY_ENTER, _KEY_ENTER_ALT, ord("\r"), _KEY_SPACE):
        # Remove item from selection while in order mode.
        del selected[order_cursor]
        if order_cursor >= len(selected) and order_cursor > 0:
            order_cursor -= 1
    return order_cursor


def _multiselect_loop(
    screen: Any, items: list[str], *, title: str, initial: list[str]
) -> Optional[list[str]]:
    query = ""
    cursor = 0
    selected: list[str] = [s for s in initial if s in items]
    # focus = "filter": navigate + toggle items in the filterable list
    # focus = "order":  navigate + reorder items in the selected list
    focus = "filter"
    order_cursor = 0

    while True:
        filtered = _filter_items(items, query)

        if not filtered:
            cursor = 0
        elif cursor >= len(filtered):
            cursor = len(filtered) - 1

        if not selected:
            order_cursor = 0
            if focus == "order":
                focus = "filter"
        elif order_cursor >= len(selected):
            order_cursor = len(selected) - 1

        try:
            _render_multiselect(
                screen, filtered, cursor,
                query=query, title=title, selected=selected,
                focus=focus, order_cursor=order_cursor,
            )
        except curses.error:
            return None

        try:
            key = screen.getch()
        except KeyboardInterrupt:
            return None

        if key in (_KEY_ESC, _KEY_CTRL_C, ord("q")):
            return None

        if key == _KEY_CTRL_D:
            return list(selected)

        # Tab toggles between filter and order focus.
        if key == ord("\t"):
            if focus == "filter" and selected:
                focus = "order"
                order_cursor = 0
            else:
                focus = "filter"
            continue

        if focus == "filter":
            if key in (curses.KEY_ENTER, _KEY_ENTER_ALT, ord("\r")):
                return list(selected)

            elif key == _KEY_SPACE:
                if filtered:
                    _toggle_membership(selected, filtered[cursor])

            elif key in (curses.KEY_UP, ord("k")):
                if cursor > 0:
                    cursor -= 1

            elif key in (curses.KEY_DOWN, ord("j")):
                if cursor < len(filtered) - 1:
                    cursor += 1

            elif key in (curses.KEY_BACKSPACE, _KEY_BACKSPACE_WIN, 127):
                query = query[:-1]
                new_filtered = _filter_items(items, query)
                if cursor >= len(new_filtered):
                    cursor = max(0, len(new_filtered) - 1)

            elif 32 <= key <= 126 and key != _KEY_SPACE:
                query += chr(key)
                cursor = 0

        else:  # focus == "order"
            order_cursor = _handle_order_key(key, selected, order_cursor)


def _render_multiselect(
    screen: Any,
    filtered: list[str],
    cursor: int,
    *,
    query: str,
    title: str,
    selected: list[str],
    focus: str = "filter",
    order_cursor: int = 0,
) -> None:
    screen.erase()
    rows, cols = screen.getmaxyx()
    min_rows = 7

    if rows < min_rows:
        raise curses.error("terminal too small")

    sep = "─" * min(cols - 1, 40)
    row = 0

    if title and row < rows - 1:
        _addstr_safe(screen, row, 0, title[:cols - 1], curses.A_BOLD)
        row += 1

    # Filter line — dim when focus is on the order panel.
    filter_label = f"Filter: {query}"
    filter_hint = "  [Tab: reorder]" if focus == "filter" and selected else ""
    filter_attr = curses.A_DIM if focus == "order" else curses.A_NORMAL
    if row < rows - 1:
        _addstr_safe(screen, row, 0, (filter_label + filter_hint)[:cols - 1], filter_attr)
        row += 1

    if row < rows - 1:
        _addstr_safe(screen, row, 0, sep)
        row += 1

    # Compute how many rows the bottom order panel needs.
    # Cap the visible selected list to keep the filter list legible.
    order_rows = min(len(selected), max(1, (rows - row) // 3)) if selected else 0
    # Bottom reserved: sep + order_rows + sep + help = order_rows + 3
    bottom_reserved = order_rows + 3

    list_start = row
    list_rows = rows - list_start - bottom_reserved
    if list_rows < 1:
        list_rows = 1

    selected_set = set(selected)
    filter_dim = focus == "order"
    scroll = max(0, cursor - list_rows + 1)
    visible = filtered[scroll: scroll + list_rows]

    for idx, item in enumerate(visible):
        abs_idx = scroll + idx
        mark = "[*]" if item in selected_set else "[ ]"
        prefix = "> " if (abs_idx == cursor and focus == "filter") else "  "
        line = (prefix + mark + " " + item)[:cols - 1]
        item_attr = curses.A_DIM if filter_dim else (
            curses.A_REVERSE if abs_idx == cursor else curses.A_NORMAL
        )
        if row < rows - bottom_reserved:
            _addstr_safe(screen, row, 0, line, item_attr)
        row += 1

    # Separator before the order panel.
    if row < rows - (order_rows + 2):
        _addstr_safe(screen, row, 0, sep)
        row += 1

    # Order panel.
    order_scroll = max(0, order_cursor - order_rows + 1)
    order_visible = selected[order_scroll: order_scroll + order_rows]
    for idx, item in enumerate(order_visible):
        abs_idx = order_scroll + idx
        is_active = focus == "order" and abs_idx == order_cursor
        prefix = "> " if is_active else "  "
        line = (prefix + item)[:cols - 1]
        attr = curses.A_REVERSE if is_active else curses.A_NORMAL
        if row < rows - 2:
            _addstr_safe(screen, row, 0, line, attr)
        row += 1

    if row < rows - 1:
        _addstr_safe(screen, row, 0, sep)
        row += 1

    if focus == "filter":
        help_line = "[↑↓/jk] move  [Space] toggle  [Enter] confirm  [Tab] reorder  [Esc/q] cancel"
    else:
        help_line = "[↑↓/jk] cursor  [K/J] reorder  [Space/Enter] remove  [Tab] back  [Ctrl-D] done"
    if row < rows:
        _addstr_safe(screen, min(rows - 1, row), 0, help_line[:cols - 1])

    screen.refresh()


# ---------------------------------------------------------------------------
# name_color_modal — two-step label + color picker
# ---------------------------------------------------------------------------

_ANSI_COLORS = [
    "red", "green", "yellow", "blue", "magenta",
]

_CURSES_COLOR_MAP: dict[str, int] = {
    "red": curses.COLOR_RED,
    "green": curses.COLOR_GREEN,
    "yellow": curses.COLOR_YELLOW,
    "blue": curses.COLOR_BLUE,
    "magenta": curses.COLOR_MAGENTA,
}

_COLOR_NONE = "(none)"


def name_color_modal(
    default_label: str,
    *,
    disclaimer: str = "",
    tty_path: str = "/dev/tty",
) -> tuple[str, str]:
    """Present a two-step curses modal: first edit the agent label,
    then optionally pick a color.

    ``disclaimer`` is shown below the input field — use it to surface
    an error from a previous attempt (e.g. name already in use).

    Returns ``(label, color)`` where ``color`` is one of the 16 ANSI
    color name strings or ``""`` for no color. Falls back to
    ``(default_label, "")`` on any error (terminal too small, not a tty).
    """
    try:
        tty_fd = open(tty_path, "r+b", buffering=0)  # pylint: disable=consider-using-with
    except OSError as exc:
        debug(
            "name/color picker unavailable; using defaults",
            context={"error_type": type(exc).__name__, "tty": tty_path},
        )
        return default_label, ""

    try:
        fd_dup = os.dup(tty_fd.fileno())
        return _run_name_color(default_label, tty_fd=fd_dup, disclaimer=disclaimer)
    except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
        debug(
            "name/color picker failed; using defaults",
            context={"error_type": type(exc).__name__},
        )
        return default_label, ""
    finally:
        tty_fd.close()


def _run_name_color(default_label: str, *, tty_fd: int, disclaimer: str = "") -> tuple[str, str]:
    import io
    orig_stdin = sys.__stdin__
    orig_stdout = sys.__stdout__
    try:
        tty_text = io.TextIOWrapper(io.FileIO(tty_fd, mode="r+"), write_through=True)
        sys.__stdin__ = tty_text   # type: ignore[assignment]
        sys.__stdout__ = tty_text  # type: ignore[assignment]
        os.environ.setdefault("TERM", "xterm-256color")

        screen = curses.initscr()
        curses.noecho()
        curses.cbreak()
        screen.keypad(True)
        try:
            label = _label_step(screen, default_label, disclaimer=disclaimer)
            color = _color_step(screen, label)
        finally:
            screen.keypad(False)
            curses.nocbreak()
            curses.echo()
            curses.endwin()
    finally:
        sys.__stdin__ = orig_stdin    # type: ignore[assignment]
        sys.__stdout__ = orig_stdout  # type: ignore[assignment]
    return label, color


def _label_step(screen: Any, default_label: str, *, disclaimer: str = "") -> str:
    """Step 1: edit the label. First printable key replaces the
    pre-fill; subsequent keys append. Enter confirms."""
    text = default_label
    replaced = False  # True once the user has typed their first char

    while True:
        _render_label(screen, text, disclaimer=disclaimer)
        try:
            key = screen.getch()
        except KeyboardInterrupt:
            return default_label

        if key in (curses.KEY_ENTER, _KEY_ENTER_ALT, ord("\r")):
            return text.strip() or default_label

        if key in (curses.KEY_BACKSPACE, _KEY_BACKSPACE_WIN, 127):
            if replaced:
                text = text[:-1]
            else:
                text = ""
                replaced = True

        elif 32 <= key <= 126:
            if not replaced:
                text = chr(key)
                replaced = True
            else:
                text += chr(key)


def _render_label(screen: Any, text: str, *, disclaimer: str = "") -> None:
    screen.erase()
    rows, cols = screen.getmaxyx()
    sep = "─" * min(cols - 1, 40)
    _addstr_safe(screen, 0, 0, "Name agent", curses.A_BOLD)
    _addstr_safe(screen, 1, 0, sep)
    _addstr_safe(screen, 2, 0, text[:cols - 1], curses.A_REVERSE)
    _addstr_safe(screen, 3, 0, sep)
    row = 4
    if disclaimer and rows > row + 1:
        _addstr_safe(screen, row, 0, disclaimer[:cols - 1], curses.A_BOLD)
        row += 1
    if rows > row + 1:
        _addstr_safe(screen, row, 0, "[any key] edit  [Enter] confirm", curses.A_DIM)
    screen.refresh()


def _color_step(screen: Any, confirmed_label: str) -> str:
    """Step 2: pick a color from the list, or skip."""
    items = [_COLOR_NONE] + _ANSI_COLORS
    cursor = 0

    # Initialise color pairs once; index 0 = none, 1..16 = palette.
    color_attrs = _init_color_pairs()

    while True:
        _render_color(screen, items, cursor, confirmed_label, color_attrs)
        try:
            key = screen.getch()
        except KeyboardInterrupt:
            return ""

        if key in (ord("q"), _KEY_ESC):
            return ""

        if key in (curses.KEY_ENTER, _KEY_ENTER_ALT, ord("\r")):
            chosen = items[cursor]
            return "" if chosen == _COLOR_NONE else chosen

        if key in (curses.KEY_UP, ord("k")) and cursor > 0:
            cursor -= 1
        elif key in (curses.KEY_DOWN, ord("j")) and cursor < len(items) - 1:
            cursor += 1


def _init_color_pairs() -> dict[str, int]:
    """Return {color_name: curses_attr} for the palette items."""
    attrs: dict[str, int] = {_COLOR_NONE: curses.A_NORMAL}
    try:
        curses.start_color()
        curses.use_default_colors()
        pair_idx = 2  # pair 1 reserved for other uses
        for name in _ANSI_COLORS:
            fg = _CURSES_COLOR_MAP.get(name, curses.COLOR_WHITE)
            try:
                curses.init_pair(pair_idx, fg, -1)
                attr = curses.color_pair(pair_idx) | curses.A_BOLD
                attrs[name] = attr
                pair_idx += 1
            except curses.error:
                attrs[name] = curses.A_NORMAL
    except curses.error:
        for name in _ANSI_COLORS:
            attrs[name] = curses.A_NORMAL
    return attrs


def _render_color(
    screen: Any,
    items: list[str],
    cursor: int,
    confirmed_label: str,
    color_attrs: dict[str, int],
) -> None:
    screen.erase()
    rows, cols = screen.getmaxyx()
    sep = "─" * min(cols - 1, 40)
    _addstr_safe(screen, 0, 0, "Name agent", curses.A_BOLD)
    _addstr_safe(screen, 1, 0, sep)
    _addstr_safe(screen, 2, 0, confirmed_label[:cols - 1])
    _addstr_safe(screen, 3, 0, sep)
    _addstr_safe(screen, 4, 0, "Color (optional)", curses.A_BOLD)

    list_start = 5
    list_rows = rows - list_start - 2
    scroll = max(0, cursor - list_rows + 1)
    visible = items[scroll: scroll + list_rows]

    for idx, name in enumerate(visible):
        abs_idx = scroll + idx
        row = list_start + idx
        if row >= rows - 2:
            break
        prefix = "> " if abs_idx == cursor else "  "
        attr = color_attrs.get(name, curses.A_NORMAL)
        if abs_idx == cursor:
            attr |= curses.A_REVERSE
        _addstr_safe(screen, row, 0, (prefix + name)[:cols - 1], attr)

    _addstr_safe(screen, rows - 2, 0, sep)
    _addstr_safe(
        screen, rows - 1, 0,
        "[↑↓/jk] move  [Enter] select  [Esc/q] skip",
        curses.A_DIM,
    )
    screen.refresh()
