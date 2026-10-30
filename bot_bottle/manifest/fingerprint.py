"""A bottle's fingerprint (PRD 0111): its full resolution (its whole `extends`
chain merged, as `document.bottle_document` writes it) in a canonical order,
hashed. However a file orders its keys, routes or repos, the same bottle hashes
the same; a real change hashes differently. A bottled agent's launch records
its chain's hashes, and the console compares them with the files as they are
now to tell whether each bottle is still the one it launched with.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from .document import bottle_document

if TYPE_CHECKING:
    from .bottle import ManifestBottle
    from .index import ManifestIndex


def _canonical(value: object) -> object:
    """`value` with every list sorted, so order in a file doesn't count."""
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_canonical(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True))
    return value


def bottle_hash(bottle: "ManifestBottle") -> str:
    """The SHA-256 of `bottle`'s canonical document."""
    text = json.dumps(_canonical(bottle_document(bottle)), sort_keys=True,
                      separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def chain_hashes(index: "ManifestIndex", names: "tuple[str, ...] | list[str]") -> dict[str, str]:
    """Each of `names`' fingerprint, from `index`'s files as they are now. A
    bottle that no longer loads is left out. Raises nothing."""
    # pylint: disable=import-outside-toplevel
    from .loader import load_bottle_chain
    from .util import ManifestError
    out: dict[str, str] = {}
    for name in names:
        try:
            bottle = (index.bottles[name] if index.home_md is None
                      else load_bottle_chain(name, index.bottle_dirs())[0])
        except (KeyError, ManifestError, OSError):
            continue
        out[name] = bottle_hash(bottle)
    return out


__all__ = ["bottle_hash", "chain_hashes"]
