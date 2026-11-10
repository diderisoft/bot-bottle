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
from unittest.mock import patch

from bot_bottle.manifest import ManifestError, ManifestIndex, trust


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text).lstrip("\n"))


_BOTTLE_DEV = """
    ---
    egress:
      routes:
        - host: api.anthropic.com
          methods: ["*"]
          auth:
            type: bearer
            value: secret:CLAUDE_CODE_OAUTH_TOKEN
        - host: example.com
    ---

    The dev bottle. Anthropic OAuth via egress.
"""

_AGENT_IMPL = """
    ---
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
        m = self.resolve().load_for_agent("implementer", ("dev",))
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
        m = self.resolve().load_for_agent("implementer", ("dev",))
        a = m.agent
        self.assertEqual(("init-prd",), a.skills)
        # Body became the prompt; whitespace stripped.
        self.assertIn("feature implementation agent", a.prompt)
        self.assertFalse(a.prompt.startswith("\n"))
        self.assertFalse(a.prompt.endswith("\n"))


class TestCwdAgentOverridesHome(_ResolveCase):
    """SC #3: a cwd agent file with the same name as a home agent
    wins once trusted, since it runs on the home bottle. The home bottle
    stays intact."""

    def test_cwd_wins(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)
        # Cwd overrides with a different prompt
        _write(
            self.cwd_cb / "agents" / "implementer.md",
            """
            ---
            ---

            CWD-OVERRIDE-PROMPT
            """,
        )
        with patch.dict(os.environ, {"BOT_BOTTLE_ROOT": str(self.home_cb)}):
            with self.assertRaises(trust.UntrustedBottleError) as caught:
                self.resolve().load_for_agent("implementer", ("dev",))
            trust.trust(caught.exception)
            m = self.resolve().load_for_agent("implementer", ("dev",))
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
                  auth:
                    type: bearer
                    value: secret:CLAUDE_CODE_OAUTH_TOKEN
            ---
            """,
        )
        m = self.resolve().load_for_agent("implementer", ("dev",))
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
        src = Path("bot_bottle/contract/yaml_subset.py").read_text()
        self.assertNotIn("import yaml", src)
        self.assertNotIn("from yaml", src)


class TestExistingFromJsonObjStillWorks(unittest.TestCase):
    """SC #6: `ManifestIndex.from_json_obj` continues to work as a
    programmatic entry point even though disk loading moved to the
    MD layout."""

    def test_from_json_obj(self):
        m = ManifestIndex.from_json_obj({
            "bottles": {"dev": {}},
            "agents": {"demo": {"skills": [], "prompt": "hi"}},
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
            skills:
              - init-prd
            ---

            Agent prompt body.
            """,
        )
        m = self.resolve().load_for_agent("implementer", ("dev",))
        self.assertEqual(("init-prd",), m.agent.skills)


class TestManifestEntryPointParity(_ResolveCase):
    """The MD and JSON entry points share validation and composition
    behavior for the same raw manifest shape."""

    def test_agent_prompt_and_skills_match_json_entry(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)

        md_manifest = self.resolve().load_for_agent("implementer", ("dev",))
        json_index = ManifestIndex.from_json_obj({
            "bottles": {
                "dev": {
                    "egress": {
                        "routes": [
                            {
                                "host": "api.anthropic.com",
                                "methods": ["*"],
                                "auth": {
                                    "type": "bearer",
                                    "value": "secret:CLAUDE_CODE_OAUTH_TOKEN",
                                },
                            },
                            {"host": "example.com"},
                        ],
                    },
                },
            },
            "agents": {
                "implementer": {
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
                },
            },
        })

        self.assertIn("implementer", index.agents)


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
            skillz: [init-prd]
            ---
            """,
        )
        m = self.resolve()
        with self.assertRaises(ManifestError):
            m.load_for_agent("bad")

    def test_broken_bottle_only_fails_at_preflight(self):
        """A broken bottle does not crash resolve; only a launch that picks
        it raises. Launches in other bottles still work."""
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
        m = self.resolve()
        self.assertIn("implementer", m.all_agent_names)
        # A launch in a good bottle loads fine; one in the broken bottle raises.
        m.load_for_agent("implementer", ("dev",))
        with self.assertRaises(ManifestError):
            m.load_for_agent("implementer", ("bad",))


class TestStaleJsonDies(_ResolveCase):
    """If `bot-bottle.json` exists in $HOME alongside no
    `.bot-bottle/` dir, die with a clear pointer at the manifest
    reference (docs/reference/manifest.md). Don't silently ignore the JSON content."""

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


class TestAgentNamingABottleFailsAtPreflight(_ResolveCase):
    """Agents don't name a bottle (ADR 0011). One that still does appears in
    all_agent_names at resolve time; loading it says what to do instead."""

    def test_a_bottle_line_fails_at_preflight_with_what_to_do(self):
        _write(self.home_cb / "bottles" / "dev.md", _BOTTLE_DEV)
        _write(
            self.home_cb / "agents" / "stray.md",
            """
            ---
            bottle: anything
            ---
            """,
        )
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)
        m = self.resolve()
        self.assertIn("stray", m.all_agent_names)
        m.load_for_agent("implementer", ("dev",))
        with self.assertRaises(ManifestError) as cm:
            m.load_for_agent("stray", ("dev",))
        self.assertIn("agents no longer name a bottle", str(cm.exception))
        self.assertIn("--bottle", str(cm.exception))

    def test_no_bottles_picked_runs_in_basic(self):
        _write(self.home_cb / "agents" / "implementer.md", _AGENT_IMPL)
        m = self.resolve().load_for_agent("implementer")
        hosts = {r.Host for r in m.bottle.egress.routes}
        self.assertIn("pypi.org", hosts)  # from the built-in `basic`


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
