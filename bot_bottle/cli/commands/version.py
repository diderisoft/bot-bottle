"""version: show bot-bottle component versions."""

from __future__ import annotations

from ... import image_inputs
from ...compat import PROTOCOL_VERSION
from ...engine import install_version
from ...published_artifacts import FORMAT_OCI, current_platform, load_artifacts_manifest_optional
from ...control.client import OrchestratorClient, recorded_orchestrator
from ..constants import PROG


def cmd_version(argv: list[str]) -> int:
    del argv
    ver, commit = install_version()
    commit_suffix = f"  ({commit})" if commit else ""
    print(f"{PROG}  {ver}{commit_suffix}")
    print(f"protocol  {PROTOCOL_VERSION}")

    # Running orchestrator (if any) — shows live infra image state.
    endpoint = recorded_orchestrator()
    orch_ver: dict[str, object] | None = None
    if endpoint is not None:
        client = OrchestratorClient(endpoint.url, timeout=3.0)
        orch_ver = client.version()

    print()
    if orch_ver is not None:
        ov = orch_ver.get("version", "")
        oc = orch_ver.get("commit", "")
        commit_part = f"  ({oc})" if oc else ""
        print(f"orchestrator  {endpoint.backend} · {endpoint.url}  {ov}{commit_part}")  # type: ignore[union-attr]
        images = orch_ver.get("images")
        if isinstance(images, dict) and images:
            print()
            print("infra images (running):")
            for name, info in sorted(images.items()):
                if not isinstance(info, dict):
                    continue
                source = info.get("source", "?")
                short = name.removeprefix("bot-bottle-")
                if source == "published":
                    digest = str(info.get("digest", ""))
                    ih = str(info.get("inputs_hash", ""))
                    digest_short = digest[:19] + "…" if len(digest) > 19 else digest
                    print(f"  {short:<14}  published  {digest_short}  inputs:{ih}")
                elif source == "local":
                    ih = str(info.get("inputs_hash", ""))
                    print(f"  {short:<14}  local      inputs:{ih}")
                else:
                    print(f"  {short:<14}  {source}")
    else:
        hint = "" if endpoint is None else f"  ({endpoint.url} not answering)"
        print(f"orchestrator  not running{hint}  (run `{PROG} up`)")

    # Agent images from published-artifacts.json (the CLI always has access).
    manifest = load_artifacts_manifest_optional()
    if manifest:
        print()
        print("agent images:")
        plat = current_platform()
        agent_names = sorted(n for n in manifest if n not in image_inputs.INFRA_ROLES)
        for name in agent_names:
            entry = manifest[name]
            short = name.removeprefix("bot-bottle-")
            artifact = entry.for_format(FORMAT_OCI, plat)
            platforms = sorted({a.platform for a in entry.artifacts if a.format == FORMAT_OCI})
            plat_list = "  ".join(p.split("/")[1] for p in platforms)
            if artifact:
                d = artifact.digest[:19] + "…"
                print(f"  {short:<8}  {entry.version}  {d}  {plat_list}")
            else:
                print(f"  {short:<8}  {entry.version}  (no {plat} artifact)  {plat_list}")
    elif manifest is None:
        print()
        print("agent images  (no published-artifacts.json)")

    return 0
