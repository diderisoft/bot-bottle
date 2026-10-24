"""Base class for host-side egress apply; backends override
`_signal_bundle_reload`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..bottle_state import egress_state_dir
from ..egress import EGRESS_ROUTES_FILENAME
from ..gateway.egress.schema import load_config
from ..gateway.egress.types import LOG_OFF


class EgressApplyError(RuntimeError):
    pass


class EgressApplicator(ABC):
    def apply_routes_change(self, slug: str, content: str) -> tuple[str, str]:
        """Persist `content` to the live routes file and reload egress."""
        self.validate_routes_content(content)
        routes_path = self._routes_path(slug)
        routes_path.parent.mkdir(parents=True, exist_ok=True)
        before = routes_path.read_text(encoding="utf-8") if routes_path.exists() else ""
        routes_path.write_text(content, encoding="utf-8")
        routes_path.chmod(0o600)
        self._signal_bundle_reload(slug)
        return before, content

    @staticmethod
    def validate_routes_content(content: str) -> None:
        try:
            config = load_config(content)
        except ValueError as e:
            raise EgressApplyError(
                f"proposed routes.yaml is not valid: {e}"
            ) from e
        if config.log != LOG_OFF:
            raise EgressApplyError(
                "proposed routes.yaml must not change egress logging"
            )

    @staticmethod
    def _routes_path(slug: str) -> Path:
        return egress_state_dir(slug) / EGRESS_ROUTES_FILENAME

    @abstractmethod
    def _signal_bundle_reload(self, slug: str) -> None: ...


__all__ = ["EgressApplicator", "EgressApplyError"]
