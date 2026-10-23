"""The request firehose's wire shapes (PRD 0093).

The gateway posts batches of records to the orchestrator's `FIREHOSE_PATH`,
which forwards them to the host controller's; the host controller fans them
out to `audit tail` subscribers (`FIREHOSE_STREAM_PATH`) and configured sinks.
Each reply is a `Capture`: whether anything is consuming the stream, and if
so what to capture, so the gateway builds nothing when nothing listens.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

FIREHOSE_PATH = "/firehose"
FIREHOSE_STREAM_PATH = "/firehose/stream"
DEFAULT_MAX_BODY_BYTES = 65536


@dataclass(frozen=True)
class Capture:
    """What the gateway should capture, from the host controller's reply."""

    wanted: bool = False
    request_bodies: bool = True
    response_bodies: bool = False
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, body: Mapping[str, Any]) -> "Capture":
        """Lenient: a missing or malformed field keeps its default, and a reply
        that isn't a capture at all means nothing is wanted."""
        def flag(name: str, default: bool) -> bool:
            value = body.get(name, default)
            return value if isinstance(value, bool) else default

        size = body.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES)
        return cls(
            wanted=flag("wanted", False),
            request_bodies=flag("request_bodies", True),
            response_bodies=flag("response_bodies", False),
            max_body_bytes=size if isinstance(size, int) and size >= 0 else DEFAULT_MAX_BODY_BYTES,
        )


__all__ = ["Capture", "DEFAULT_MAX_BODY_BYTES", "FIREHOSE_PATH", "FIREHOSE_STREAM_PATH"]
