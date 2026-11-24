"""Unit: the `@label` credential reference and the `@@` escape in
`sigil_ref`, and where a literal `@` reaches the agent."""

from __future__ import annotations

import unittest

from bot_bottle import secret_ref, sigil_ref
from bot_bottle.env import resolve_env
from bot_bottle.manifest import ManifestError, ManifestIndex
from bot_bottle.secret_source import MappingSecrets


class TestCredRef(unittest.TestCase):
    def test_at_label(self) -> None:
        ref = sigil_ref.parse("@my-token")
        self.assertEqual((ref.kind, ref.name), (sigil_ref.CRED_REF, "my-token"))
        self.assertTrue(sigil_ref.is_cred_ref("@my-token"))
        self.assertEqual(sigil_ref.cred_label("@my-token"), "my-token")
        self.assertEqual(sigil_ref.source_label("@my-token"), "credential my-token")

    def test_not_a_cred_ref(self) -> None:
        for raw in ("@@x", "@", "@Bad", "plain", "secret:X", "${X}"):
            self.assertFalse(sigil_ref.is_cred_ref(raw), raw)
            self.assertEqual(sigil_ref.cred_label(raw), "", raw)

    def test_malformed_at_is_a_problem(self) -> None:
        for raw in ("@", "@Bad", "@1x", "@a_b"):
            self.assertIn("'@' must be followed by a credential label",
                          sigil_ref.problem(raw), raw)
        for raw in ("@a", "@@", "@@Bad", "plain"):
            self.assertEqual(sigil_ref.problem(raw), "", raw)

    def test_secret_ref_is_the_same_module(self) -> None:
        self.assertIs(secret_ref.parse, sigil_ref.parse)
        self.assertEqual(secret_ref.kind("@a"), sigil_ref.CRED_REF)


class TestLiteral(unittest.TestCase):
    def test_double_at_is_a_literal_at(self) -> None:
        self.assertEqual(sigil_ref.parse("@@scope/pkg").kind, sigil_ref.LITERAL)
        self.assertEqual(sigil_ref.literal("@@scope/pkg"), "@scope/pkg")
        self.assertEqual(sigil_ref.literal("@@"), "@")

    def test_anything_else_is_unchanged(self) -> None:
        for raw in ("plain", "a@@b", "@label", ""):
            self.assertEqual(sigil_ref.literal(raw), raw)

    def test_env_literal_reaches_the_agent_unescaped(self) -> None:
        manifest = ManifestIndex.from_json_obj({
            "bottles": {"dev": {"env": {"NPM_SCOPE": "@@myorg", "PLAIN": "x"}}},
            "agents": {"demo": {}},
        }).load_for_agent("demo", ("dev",))
        resolved = resolve_env(manifest, MappingSecrets({}), prompt=False)
        self.assertEqual(resolved.literals, {"NPM_SCOPE": "@myorg", "PLAIN": "x"})

    def test_env_malformed_at_is_refused(self) -> None:
        with self.assertRaisesRegex(ManifestError, "'@' must be followed"):
            ManifestIndex.from_json_obj({
                "bottles": {"dev": {"env": {"NPM_SCOPE": "@MyOrg/pkg"}}},
                "agents": {"demo": {}},
            }).load_for_agent("demo", ("dev",))


if __name__ == "__main__":
    unittest.main()
