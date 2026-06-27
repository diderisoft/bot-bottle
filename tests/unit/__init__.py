"""Unit-test package init.

Isolates ``HOME`` to a throwaway directory for the entire unit suite so
no test ever reads or writes the real ``~/.bot-bottle`` (state, queue,
and audit dirs all derive from ``supervise.bot_bottle_root()`` →
``Path.home()``). Without this, a test that takes a ``flock`` on the
real audit log can **block indefinitely** when a live bottle's supervise
sidecar holds that lock — observed as a hung ``coverage run`` at 0% CPU —
and unisolated tests otherwise pollute the developer's home dir.

Individual tests that need their own ``HOME`` still override
``os.environ['HOME']`` and restore it; they now restore to this isolated
dir rather than the real one, so isolation holds either way. Tests that
patch ``supervise.bot_bottle_root`` directly are unaffected.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

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
