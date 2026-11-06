"""A resolved bottle as one bottle document reads back as the same bottle."""

from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from typing import Any

from bot_bottle.contract.yaml_subset import parse_frontmatter, serialize_yaml_subset
from bot_bottle.forge_connections import ForgeConnection, ForgeUser
from bot_bottle.manifest.document import bottle_document
from bot_bottle.manifest.extends import resolve_bottles
from bot_bottle.manifest.loader import BUILTIN_BOTTLES_DIR
from tests.unit._forges import connected

ROOT = Path(__file__).resolve().parents[2]

# A chain that exercises what the examples don't: a forge with a grant, a
# credential route, detectors, headers, regex paths, audit and a git identity.
PARENT = """---
git-gate:
  user:
    name: bot
    email: bot@example.com
  forges:
    gitea.example.com:
      kind: gitea
      url: https://gitea.example.com
      ssh: ssh://git@gitea.example.com:2222
      host_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
egress:
  routes:
    - host: api.example.com
      inspect:
        auth:
          scheme: Bearer
          token_ref: API_TOKEN
        matches:
          - methods: [GET, POST]
            paths:
              - value: /v1/
              - type: regex
                value: ^/v2/[a-z]+$
            headers:
              - name: x-kind
                value: json
        outbound_detectors: false
        outbound_on_match: redact
    - host: raw.example.com
      inspect: false
  log: 1
audit:
  delivery: required
---
"""
CHILD = """---
extends: parent
env:
  LANG: C.UTF-8
git-gate:
  repos:
    - gitea.example.com/acme/app:write
nested_containers: true
---
"""


def _same(a: object, b: object) -> dict[str, object]:
    """Fields of two bottles, minus what only records how they were declared."""
    strip = {"declared_fields"}
    return ({f.name: getattr(a, f.name) for f in dataclasses.fields(a)  # type: ignore[arg-type]
             if f.name not in strip},
            {f.name: getattr(b, f.name) for f in dataclasses.fields(b)  # type: ignore[arg-type]
             if f.name not in strip})  # type: ignore[return-value]


CONN = ForgeConnection(
    host="gitea.example.com", kind="gitea", url="https://gitea.example.com",
    ssh="ssh://git@gitea.example.com:2222",
    host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl",
    users=(ForgeUser("bot", "GITEA_TOKEN", default=True),))


@connected(CONN)
class TestRoundTrip(unittest.TestCase):
    def round_trip(self, raws: dict[str, dict[str, object]], name: str) -> None:
        original = resolve_bottles(raws)[name]
        text = serialize_yaml_subset(bottle_document(original))
        again = resolve_bottles({"doc": parse_frontmatter(f"---\n{text}---\n")[0]})["doc"]
        mine, theirs = _same(original, again)
        self.assertEqual(mine, theirs)

    def test_a_chain_with_everything(self) -> None:
        raws = {"parent": parse_frontmatter(PARENT)[0], "child": parse_frontmatter(CHILD)[0]}
        self.round_trip(raws, "child")
        doc = bottle_document(resolve_bottles(raws)["child"])
        self.assertNotIn("extends", doc)
        # A repo granted on the chain's forge stays a grant, the forge with it.
        git: Any = doc["git-gate"]
        self.assertEqual("gitea.example.com/acme/app:write", git["repos"]["app"])
        # Described: never the token or user, which are the host's.
        self.assertEqual({"kind": "gitea", "ssh": "ssh://git@gitea.example.com:2222"},
                         git["forges"]["gitea.example.com"])
        self.assertNotIn("credentials", doc)

    def test_a_read_only_grant_and_a_long_form_repo(self) -> None:
        raws = {"parent": parse_frontmatter(PARENT)[0], "child": parse_frontmatter(CHILD.replace(
            "    - gitea.example.com/acme/app:write\n",
            "    app: gitea.example.com/acme/app\n"
            "    other:\n      url: ssh://git@elsewhere.example/o/r.git\n"
            "      key: {provider: static, path: /keys/r}\n"))[0]}
        self.round_trip(raws, "child")
        git: Any = bottle_document(resolve_bottles(raws)["child"])["git-gate"]
        self.assertEqual("gitea.example.com/acme/app", git["repos"]["app"])
        self.assertEqual("ssh://git@elsewhere.example/o/r.git", git["repos"]["other"]["url"])

    def test_examples_and_builtins(self) -> None:
        files = [*BUILTIN_BOTTLES_DIR.glob("*.md"), *(ROOT / "examples" / "bottles").glob("*.md")]
        self.assertTrue(files)
        raws = {p.stem: parse_frontmatter(p.read_text())[0] for p in files}
        for name in raws:
            with self.subTest(bottle=name):
                self.round_trip(raws, name)


if __name__ == "__main__":
    unittest.main()
