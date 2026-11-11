"""`FirecrackerBottleBackend`: agents in KVM microVMs, controlled over SSH on a
point-to-point TAP, behind a fail-closed nftables boundary.
"""

from __future__ import annotations

import io
from contextlib import AbstractContextManager, contextmanager, redirect_stderr
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Generator, Sequence

from ...agent_provider import AgentProvisionPlan
from ...egress import EgressPlan
from ...env import ResolvedEnv
from ...git_gate import GitGatePlan
from ...lib.log import warn
from ...manifest import Manifest
from ...secret_source import SecretSource
from ...supervisor.plan import SupervisePlan
from .. import ActiveAgent, BottleBackend, BottleImages, BottleSpec
from . import cleanup as _cleanup
from . import enumerate as _enumerate
from . import launch as _launch
from . import resolve_plan as _resolve_plan
from . import util as _util
from .bottle import FirecrackerBottle
from .bottle_cleanup_plan import FirecrackerBottleCleanupPlan
from .bottle_plan import FirecrackerBottlePlan

if TYPE_CHECKING:
    from ..cache_entries import CacheEntry
    from ...workspace import DiskInfo
    from ..adoption import AdoptionRecord
    from .infra import FirecrackerInfraService


class FirecrackerBottleBackend(
    BottleBackend["FirecrackerBottlePlan", "FirecrackerBottleCleanupPlan"]
):
    name = "firecracker"

    @classmethod
    def is_available(cls) -> bool:
        return _util.is_available()

    @classmethod
    def is_host_capable(cls) -> bool:
        """Linux + KVM, whether or not the `firecracker` binary is installed,
        so a capable host selects firecracker and gets an install pointer."""
        return _util.is_host_capable()

    def setup(self) -> int:
        from . import setup as _setup
        return _setup.setup(self.settings)

    def user_setup(self) -> int:
        """Download what's missing into the user's own directories: the
        `firecracker` binary, then the guest kernel and dropbear."""
        from . import binary
        from .guest_files import GuestFilesError, ensure
        try:
            binary.ensure(say=print)
            ensure(self.settings.infra, say=print)
        except (binary.BinaryError, GuestFilesError) as e:
            warn(str(e))
            return 1
        return 0

    def root_setup(self) -> str | None:
        from . import setup as _setup
        return _setup.root_setup(self.settings)

    def status(self, *, quiet: bool = False) -> int:
        from . import setup as _setup
        if quiet:
            with redirect_stderr(io.StringIO()):
                return _setup.status(self.settings)
        return _setup.status(self.settings)

    def cache_entries(self) -> "list[CacheEntry]":
        from . import cache_entries as _cache
        return _cache.list_entries(self.settings.infra)

    def prune_build_store(self, *, dry_run: bool) -> tuple[int, str] | None:
        from . import cache_entries as _cache
        return _cache.prune_build_store(dry_run=dry_run)

    def workspace_disks(self) -> "list[DiskInfo]":
        from . import workspace_disk
        return workspace_disk.listing()

    def remove_workspace_disk(self, path: Path) -> bool:
        from . import workspace_disk
        return workspace_disk.remove(path)

    def teardown(self) -> int:
        from . import setup as _setup
        return _setup.teardown(self.settings)

    def _preflight(self) -> None:
        _resolve_plan.preflight(self.settings)

    def _build_guest_env(self, resolved_env: ResolvedEnv) -> dict[str, str]:
        return _resolve_plan.build_guest_env(resolved_env)

    def _resolve_plan(
        self,
        spec: BottleSpec,
        *,
        manifest: Manifest,
        slug: str,
        resolved_env: ResolvedEnv,
        agent_provision_plan: AgentProvisionPlan,
        egress_plan: EgressPlan,
        git_gate_plan: GitGatePlan,
        supervise_plan: SupervisePlan | None,
        stage_dir: Path,
        secrets: SecretSource,
    ) -> FirecrackerBottlePlan:
        return _resolve_plan.resolve_plan(
            spec,
            manifest=manifest,
            slug=slug,
            resolved_env=resolved_env,
            agent_provision_plan=agent_provision_plan,
            egress_plan=egress_plan,
            supervise_plan=supervise_plan,
            git_gate_plan=git_gate_plan,
            stage_dir=stage_dir,
            settings=self.settings,
            secrets=secrets,
        )

    def _build_or_load_images(self, plan: FirecrackerBottlePlan) -> BottleImages:
        return BottleImages(agent=_launch.build_or_load_agent_base(plan))

    def prelaunch_checks(self, plan: FirecrackerBottlePlan) -> None:
        _launch.stale_checks(plan)

    @contextmanager
    def _launch_impl(
        self, plan: FirecrackerBottlePlan, images: BottleImages,
    ) -> Generator[FirecrackerBottle, None, None]:
        assert isinstance(images.agent, Path)
        with _launch.launch(plan, images.agent, provision=self.provision) as bottle:
            yield bottle

    def hold_adopted(self, bottle_id: str) -> Callable[[], None] | None:
        """Re-lock the TAP slot a running bottle's VM is on: the lock lived in
        the previous host controller's process."""
        from . import firecracker_vm, netpool  # pylint: disable=import-outside-toplevel
        tap = firecracker_vm.tap_of(_util.env_dir() / "run" / bottle_id)
        if not tap:
            return None
        lock = netpool.from_settings(self.settings.infra).reclaim(tap)
        if lock is None:
            warn(f"bottle {bottle_id}: its slot {tap} is already locked; leaving it")
            return None
        return lock.close

    def adopted_bottle(
        self, bottle_id: str, record: "AdoptionRecord",
    ) -> AbstractContextManager[FirecrackerBottle]:
        return _launch.adopted(bottle_id, record, self.settings)

    def attach_adopted(self, bottle_id: str, record: "AdoptionRecord") -> FirecrackerBottle:
        return _launch.attach(bottle_id, record)

    def kept_guest(self, slug: str) -> bool:
        # pylint: disable-next=import-outside-toplevel
        from .util import guest_image_path
        return guest_image_path(slug).exists()

    def remove_home(self, slug: str) -> None:
        # Its state dir's; gone with it, unless the state outlived it.
        # pylint: disable-next=import-outside-toplevel
        from .util import home_image_path
        home_image_path(slug).unlink(missing_ok=True)

    def prepare_cleanup(self) -> FirecrackerBottleCleanupPlan:
        return _cleanup.prepare_cleanup()

    def cleanup(self, plan: FirecrackerBottleCleanupPlan) -> None:
        _cleanup.cleanup(plan, self.settings)

    def enumerate_active(self) -> Sequence[ActiveAgent]:
        return _enumerate.enumerate_active()

    def infra_service(self) -> "FirecrackerInfraService":
        from .infra import FirecrackerInfraService
        return FirecrackerInfraService(self.settings)
