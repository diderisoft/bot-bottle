"""Backend capability preflight for the integration CI jobs.

Run as ``python3 -m tests.backend_preflight`` (optionally with a backend name
argument; default: ``BOT_BOTTLE_BACKEND`` or ``docker``). Prints a single
clear PASS/FAIL line for the selected backend's capability and exits non-zero
on failure, so a job surfaces missing infrastructure at the job level instead
of hiding it among ``unittest.skip`` lines further down the output.
"""

from __future__ import annotations

import sys

from tests._backend import backend_capability, selected_backend


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    backend = argv[0] if argv else selected_backend()
    cap = backend_capability(backend)
    status = "PASS" if cap.ok else "FAIL"
    print(f"[preflight] backend={backend} {status}: {cap.detail}")
    return 0 if cap.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
