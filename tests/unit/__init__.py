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
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

# Resource names are fixed at import from `BOT_BOTTLE_ENV`, and the suite
# asserts live's; a developer's `BOT_BOTTLE_ENV=dev` must not leak in.
os.environ.pop("BOT_BOTTLE_ENV", None)

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

# The suite runs as the host controller does: code shared with the CLI (forge
# connections, trust records) uses the host DB's stores, under each test's
# root. Tests of the CLI's side reach them over HTTP (`serve_host_controller`)
# or switch this off themselves (`as_cli`).
from bot_bottle import host_access as _host_access  # pylint: disable=wrong-import-position

_host_access.use_stores()


def as_cli(test: unittest.TestCase) -> None:
    """For `test`, shared code asks the host controller (start one with
    `serve_host_controller`) instead of using the stores, as in the CLI."""
    test.enterContext(mock.patch.object(_host_access, "_stores", False))


def use_bottle_root(root: Path) -> Callable[[], None]:
    """Redirect ``paths.bot_bottle_root()`` to ``root`` by setting
    ``BOT_BOTTLE_ROOT`` — the supported override. Returns a callable that
    undoes it (store it as the test's restore hook, or pass to addCleanup).

    Replaces monkey-patching ``paths.bot_bottle_root``; one env var covers
    every module and the flat/package copies alike (they all read it)."""
    patcher = mock.patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(root)})
    patcher.start()
    return patcher.stop


def serve_host_controller(test: unittest.TestCase) -> None:
    """Serve the host controller's routes in this process, for `test`, under
    the bottle root it set, and point the CLI's `runner.ensure_running` at
    them. The CLI then reaches host state as it does for real: over HTTP."""
    # pylint: disable=import-outside-toplevel
    from bot_bottle.contract.hostctl import HOST_CONTROL
    from bot_bottle.control.hostctl import runner
    from bot_bottle.control.hostctl.server import HostControlServer
    from bot_bottle.trust_domain import signing_key
    server = HostControlServer(("127.0.0.1", 0), "test", signing_key(HOST_CONTROL))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    state = runner.HostControlState(pid=os.getpid(), port=server.server_address[1],
                                    build_id="test")
    test.enterContext(mock.patch.object(runner, "ensure_running", return_value=state))


# The unit suite never sees the repo's committed `published-artifacts.json`:
# with it present, launch paths resolve real pins and pull from the registry.
# Tests that want a manifest pass a `repo_root` or patch the loader. Assigned,
# not `mock.patch`ed, so a test's `mock.patch.stopall()` can't undo it.
from bot_bottle import published_artifacts as _published_artifacts  # pylint: disable=wrong-import-position

_real_artifacts_manifest_path = _published_artifacts.artifacts_manifest_path


def _no_committed_manifest(repo_root: Path | None = None) -> Path:
    if repo_root is None:
        missing = Path(_tmp_home) / "no-committed-manifest"
        return missing / _published_artifacts.ARTIFACTS_MANIFEST_NAME
    return _real_artifacts_manifest_path(repo_root)


_published_artifacts.artifacts_manifest_path = _no_committed_manifest
