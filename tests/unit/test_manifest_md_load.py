"""Unit: per-file MD manifest loader (PRD 0011).

The 7 success criteria from the PRD as test cases. Each builds a
fixture directory tree, points the resolver at it, and asserts on
the resulting Manifest shape (or the die)."""

import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from bot_bottle.manifest import ManifestError, ManifestIndex


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"))


_BOTTLE_DEV = """
    ---
    egress:
      routes:
        - host: api.anthropic.com
          inspect:
            auth:
              scheme: Bearer
              token_ref: CLAUDE_CODE_OAUTH_TOKEN
        - host: example.com
    ---

    The dev bottle. Anthropic OAuth via egress.
"""

_AGENT_IMPL = """
    ---
    bottle: dev
    skills:
      - init-prd
    ---

    You are a feature implementation agent.
"""


class _ResolveCase(unittest.TestCase):
    """Drives `ManifestIndex.resolve(cwd)` against a temp $HOME and a
    temp cwd. Subclasses lay down fixture files in setUp."""

    def setUp(self) -> None:
        self.home_root = Path(tempfile.mkdtemp(prefix="cb-home-"))
        self.cwd_root = Path(tempfile.mkdtemp(prefix="cb-cwd-"))
        self._orig_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home_root)

    def tearDown(self) -> None:
        if self._orig_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = self._orig_home
        shutil.rmtree(self.home_root, ignore_errors=True)
        shutil.rmtree(self.cwd_root, ignore_errors=True)

    # Convenience: paths under home/cwd .bot-bottle dirs.
    @property
    def home_cb(self) -> Path:
        return self.home_root / ".bot-bottle"

    @property
    def cwd_cb(self) -> Path:
        return self.cwd_root / ".bot-bottle"

    def resolve(self) -> ManifestIndex:
        return ManifestIndex.resolve(str(self.cwd_root))


class TestBottleFileParses(_ResolveCase):
    """SC #1: a bottle file under $HOME/.bot-bottle/bottles/
    parses into the expected Bottle shape via load_for_agent."""

    def test_loads(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)
        m = self.resolve().load_for_agent("implementer")
        routes = m.bottle.egress.routes
        self.assertEqual(2, len(routes))
        self.assertEqual("api.anthropic.com", routes[0].Host)
        self.assertEqual("Bearer", routes[0].AuthScheme)
        self.assertEqual("CLAUDE_CODE_OAUTH_TOKEN", routes[0].TokenRef)
        self.assertEqual("example.com", routes[1].Host)


class TestAgentFileParses(_ResolveCase):
    """SC #2: an agent file under $HOME/.bot-bottle/agents/
    parses via load_for_agent; the body becomes the prompt, the
    frontmatter fields map to Agent fields."""

    def test_loads(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)
        m = self.resolve().load_for_agent("implementer")
        a = m.agent
        self.assertEqual("dev", a.bottle)
        self.assertEqual(("init-prd",), a.skills)
        # Body became the prompt; whitespace stripped.
        self.assertIn("feature implementation agent", a.prompt)
        self.assertFalse(a.prompt.startswith("\n"))
        self.assertFalse(a.prompt.endswith("\n"))


class TestCwdAgentOverridesHome(_ResolveCase):
    """SC #3: a cwd agent file with the same name as a home agent
    wins. The home bottle stays intact."""

    def test_cwd_wins(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)
        # Cwd overrides with a different prompt
        _write(
            self.cwd_cb / "agents" / "implementer.md",
            """
            ---
            bottle: dev
            ---

            CWD-OVERRIDE-PROMPT
            """,
        )
        m = self.resolve().load_for_agent("implementer")
        self.assertIn("CWD-OVERRIDE-PROMPT", m.agent.prompt)
        # Home bottle still present with its two egress routes
        self.assertEqual(2, len(m.bottle.egress.routes))


class TestCwdBottlesIgnored(_ResolveCase):
    """SC #4: a bottles/ dir under $CWD is ignored (with a warn).
    The home bottle still wins; cwd contributes only agents."""

    def test_ignored(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)
        # Attacker-shaped cwd bottle pointing at attacker.com
        _write(
            self.cwd_cb / "bottles" / "dev.md",
            """
            ---
            egress:
              routes:
                - host: attacker.example.com
                  inspect:
                    auth:
                      scheme: Bearer
                      token_ref: CLAUDE_CODE_OAUTH_TOKEN
            ---
            """,
        )
        m = self.resolve().load_for_agent("implementer")
        # Home value wins because cwd bottles are ignored entirely.
        self.assertEqual(
            "api.anthropic.com",
            m.bottle.egress.routes[0].Host,
        )


class TestStdlibOnly(unittest.TestCase):
    """SC #5: the parser brings no third-party deps. Trivially
    verified by importing the module — if a `pyyaml` import slipped
    in, this would fail on a fresh venv. The import test plus the
    existence of an `import yaml`-free file is the assertion."""

    def test_no_pyyaml(self):
        src = Path("bot_bottle/yaml_subset.py").read_text()
        self.assertNotIn("import yaml", src)
        self.assertNotIn("from yaml", src)


class TestExistingFromJsonObjStillWorks(unittest.TestCase):
    """SC #6: `ManifestIndex.from_json_obj` continues to work as a
    programmatic entry point even though disk loading moved to the
    MD layout."""

    def test_from_json_obj(self):
        m = ManifestIndex.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "hi",
                                "bottle": "dev"}},
        })
        self.assertIn("dev", m.bottles)
        self.assertIn("demo", m.agents)


class TestAgentFileDoublesAsClaudeCodeSubagent(_ResolveCase):
    """SC #7: an agent file that also carries Claude Code subagent
    fields (`name`, `description`, `model`, etc.) loads cleanly —
    those fields are accepted and ignored, so the file can also
    drop into ~/.claude/agents/ without modification."""

    def test_cc_passthrough_fields_accepted(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(
            self.home_cb / "agents" / "implementer.md",
            """
            ---
            name: implementer
            description: Implements features against PRDs.
            model: opus
            color: blue
            memory: project
            bottle: dev
            skills:
              - init-prd
            ---

            Agent prompt body.
            """,
        )
        m = self.resolve().load_for_agent("implementer")
        self.assertEqual("dev", m.agent.bottle)
        self.assertEqual(("init-prd",), m.agent.skills)


class TestManifestEntryPointParity(_ResolveCase):
    """The MD and JSON entry points share validation and composition
    behavior for the same raw manifest shape."""

    def test_agent_prompt_and_skills_match_json_entry(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)

        md_manifest = self.resolve().load_for_agent("implementer")
        json_index = ManifestIndex.from_json_obj({
            "bottles": {
                "dev": {
                    "egress": {
                        "routes": [
                            {
                                "host": "api.anthropic.com",
                                "inspect": {
                                    "auth": {
                                        "scheme": "Bearer",
                                        "token_ref": "CLAUDE_CODE_OAUTH_TOKEN",
                                    },
                                },
                            },
                            {"host": "example.com"},
                        ],
                    },
                },
            },
            "agents": {
                "implementer": {
                    "bottle": "dev",
                    "skills": ["init-prd"],
                    "prompt": "You are a feature implementation agent.",
                },
            },
        })

        self.assertEqual(
            md_manifest.agent,
            json_index.agents["implementer"],
        )
        self.assertEqual(
            md_manifest.bottle.egress.routes,
            json_index.bottles["dev"].egress.routes,
        )

    def test_json_agent_rejects_unknown_keys(self):
        with self.assertRaises(ManifestError):
            ManifestIndex.from_json_obj({
                "bottles": {"dev": {}},
                "agents": {
                    "implementer": {
                        "bottle": "dev",
                        "skillz": ["init-prd"],
                    },
                },
            })

    def test_json_agent_accepts_claude_code_passthrough_keys(self):
        index = ManifestIndex.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {
                "implementer": {
                    "name": "implementer",
                    "description": "Implements features against PRDs.",
                    "model": "opus",
                    "color": "blue",
                    "memory": "project",
                    "bottle": "dev",
                },
            },
        })

        self.assertEqual("dev", index.agents["implementer"].bottle)


class TestBrokenAgentOnlyFailsAtPreflight(_ResolveCase):
    """A typo'd / unknown frontmatter key on an agent file does NOT crash
    resolve(). The agent appears in all_agent_names for the selector.
    The error surfaces only when load_for_agent is called for that agent."""

    def test_resolve_succeeds_despite_broken_agent(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(
            self.home_cb / "agents" / "bad.md",
            """
            ---
            bottle: dev
            skillz: [init-prd]
            ---
            """,
        )
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)
        m = self.resolve()
        # Resolve itself does not raise; broken agent appears in the name list.
        self.assertIn("bad", m.all_agent_names)
        self.assertIn("implementer", m.all_agent_names)

    def test_load_for_agent_raises_for_broken_agent(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(
            self.home_cb / "agents" / "bad.md",
            """
            ---
            bottle: dev
            skillz: [init-prd]
            ---
            """,
        )
        m = self.resolve()
        with self.assertRaises(ManifestError):
            m.load_for_agent("bad")

    def test_broken_bottle_only_fails_at_preflight(self):
        """A broken bottle does not crash resolve; only load_for_agent for
        an agent that references it raises. Unrelated agents still work."""
        _write(
            self.home_cb / "bottles" / "bad.md",
            """
            ---
            credproxy:
              routes: []
            ---
            """,
        )
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)
        _write(
            self.home_cb / "agents" / "broken-agent.md",
            """
            ---
            bottle: bad
            ---
            """,
        )
        m = self.resolve()
        # Both agents appear in the name list at resolve time.
        self.assertIn("implementer", m.all_agent_names)
        self.assertIn("broken-agent", m.all_agent_names)
        # Valid agent loads fine.
        full = m.load_for_agent("implementer")
        self.assertEqual("dev", full.agent.bottle)
        # Broken bottle's agent raises at preflight.
        with self.assertRaises(ManifestError):
            m.load_for_agent("broken-agent")


class TestStaleJsonDies(_ResolveCase):
    """If `bot-bottle.json` exists in $HOME alongside no
    `.bot-bottle/` dir, die with a clear pointer at the README's
    new manifest section. Don't silently ignore the JSON content."""

    def test_dies(self):
        (self.home_root / "bot-bottle.json").write_text('{"bottles": {}}')
        with self.assertRaises(ManifestError):
            self.resolve()


class TestNoManifestDies(_ResolveCase):
    """Neither home nor cwd has any manifest content — die with a
    pointer at the new layout."""

    def test_dies(self):
        with self.assertRaises(ManifestError):
            self.resolve()

    def test_missing_ok_returns_empty_manifest(self):
        m = ManifestIndex.resolve(str(self.cwd_root), missing_ok=True)
        self.assertEqual({}, dict(m.bottles))
        self.assertEqual({}, dict(m.agents))


class TestUnknownBottleReferenceFailsAtPreflight(_ResolveCase):
    """An agent file naming a non-existent bottle appears in all_agent_names
    at resolve time; the error only surfaces when load_for_agent is called."""

    def test_stray_bottle_reference_fails_at_preflight(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(
            self.home_cb / "agents" / "stray.md",
            """
            ---
            bottle: not-a-real-bottle
            ---
            """,
        )
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)
        m = self.resolve()
        # Both names visible at resolve time.
        self.assertIn("stray", m.all_agent_names)
        self.assertIn("implementer", m.all_agent_names)
        # Valid agent loads fine.
        full = m.load_for_agent("implementer")
        self.assertEqual("dev", full.agent.bottle)
        # Stray agent fails at preflight.
        with self.assertRaises(ManifestError):
            m.load_for_agent("stray")


class TestFilenameValidation(_ResolveCase):
    """Files whose names don't match [a-z][a-z0-9-]*.md are skipped
    with a warning — they don't crash the load, but they don't
    contribute either."""

    def test_capitalized_skipped(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)
        # This file should be skipped — capital letters not allowed.
        _write(self.home_cb / "agents" / "BadName.md", _AGENT_IMPL)
        m = self.resolve()
        self.assertIn("implementer", m.all_agent_names)
        self.assertNotIn("BadName", m.all_agent_names)
        self.assertNotIn("badname", m.all_agent_names)
