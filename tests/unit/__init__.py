"""Unit-test package init.

Isolates ``HOME`` to a throwaway directory for the entire unit suite so
no test ever reads or writes the real ``~/.bot-bottle`` (state, queue,
and audit dirs all derive from ``paths.bot_bottle_root()`` →
``Path.home()``). Without this, a test that takes a ``flock`` on the
real audit log can **block indefinitely** when a live bottle's supervise
gateway holds that lock — observed as a hung ``coverage run`` at 0% CPU —
and unisolated tests otherwise pollute the developer's home dir.

Individual tests that need their own ``HOME`` still override
``os.environ['HOME']`` and restore it; they now restore to this isolated
dir rather than the real one, so isolation holds either way.

Tests that need their own app-data root call ``use_bottle_root`` (below),
which sets ``BOT_BOTTLE_ROOT`` — the supported override read by
``paths.bot_bottle_root`` — instead of monkey-patching. One env setting
covers every module and every flat/package copy of the helper.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from unittest import mock

_real_home = os.environ.get("HOME")
_tmp_home = tempfile.mkdtemp(prefix="bot-bottle-unit-home.")
os.environ["HOME"] = _tmp_home


def _restore_home() -> None:
    if _real_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = _real_home
    shutil.rmtree(_tmp_home, ignore_errors=True)


atexit.register(_restore_home)


def use_bottle_root(root: Path) -> Callable[[], None]:
    """Redirect ``paths.bot_bottle_root()`` to ``root`` by setting
    ``BOT_BOTTLE_ROOT`` — the supported override. Returns a callable that
    undoes it (store it as the test's restore hook, or pass to addCleanup).

    Replaces monkey-patching ``paths.bot_bottle_root``; one env var covers
    every module and the flat/package copies alike (they all read it)."""
    patcher = mock.patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(root)})
    patcher.start()
    return patcher.stop
