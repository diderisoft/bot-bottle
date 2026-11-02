"""The agent guest's PID-1 init keeps dropbear as its job, so its reap loop
blocks instead of spinning a vCPU (every idle agent VM burned a host core:
dropbear daemonized, leaving `wait` with no job to wait for)."""

import re
import resource
import shutil
import subprocess
import unittest

from bot_bottle.backend.firecracker import util


def _reap_loop() -> str:
    m = re.search(r"^while : ; do wait.*$", util._GUEST_INIT, re.M)  # pylint: disable=protected-access
    assert m, "no reap loop in the guest init"
    return m.group(0)


def _child_cpu_after(script: str, seconds: float) -> float:
    """CPU seconds a shell running `script` uses in `seconds` (it's killed then)."""
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    subprocess.run(["timeout", str(seconds), "sh", "-c", script], check=False)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (after.ru_utime - before.ru_utime) + (after.ru_stime - before.ru_stime)


class TestGuestInit(unittest.TestCase):
    def test_dropbear_stays_in_the_foreground(self) -> None:
        line = next(l for l in util._GUEST_INIT.splitlines()  # pylint: disable=protected-access
                    if l.startswith("/bb-dropbear"))
        self.assertIn(" -F ", f" {line} ")
        self.assertTrue(line.rstrip().endswith("&"))

    @unittest.skipUnless(shutil.which("sh") and shutil.which("timeout"), "needs sh and timeout")
    def test_the_reap_loop_idles_when_the_shell_has_no_job(self) -> None:
        # A daemonizing server: the shell's job exits at once, its child lives on.
        script = "( sleep 5 & ) & " + _reap_loop()
        self.assertLess(_child_cpu_after(script, 1.5), 0.3)

    @unittest.skipUnless(shutil.which("sh") and shutil.which("timeout"), "needs sh and timeout")
    def test_the_reap_loop_blocks_on_a_foreground_job(self) -> None:
        script = "sleep 5 & " + _reap_loop()
        self.assertLess(_child_cpu_after(script, 1.5), 0.3)


if __name__ == "__main__":
    unittest.main()
