"""Per-provisioner modules for the Docker backend.

Each module exports one top-level function:
    provision_<thing>(plan: DockerBottlePlan, target: str) -> ...

`DockerBottleBackend.provision_*` methods delegate to these. The
abstract `BottleBackend.provision_*` surface is unchanged; this
subpackage exists only to keep `backend.py` from being a god-file."""
