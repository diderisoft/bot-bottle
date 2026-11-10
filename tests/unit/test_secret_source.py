"""Unit: `SecretSource` providers, the CLI's builder, and env resolution
through a source (PRD 0092)."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_bottle.cli.secrets_env import secrets_from_env
from bot_bottle.env import resolve_env
from bot_bottle.errors import MissingEnvVarError, SecretsFileError
from bot_bottle.manifest import Manifest, ManifestIndex
from bot_bottle.secret_source import ChainSecrets, FileSecrets, MappingSecrets


class _TmpDir(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="secret-source."))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, text: str, mode: int = 0o600) -> Path:
        path = self.dir / "secrets"
        path.write_text(text)
        path.chmod(mode)
        return path


class TestMappingSecrets(unittest.TestCase):
    def test_get(self) -> None:
        source = MappingSecrets({"A": "1", "EMPTY": ""})
        self.assertEqual("1", source.get("A"))
        self.assertEqual("", source.get("EMPTY"))
        self.assertIsNone(source.get("B"))

    def test_repr_hides_values(self) -> None:
        self.assertNotIn("hunter2", repr(MappingSecrets({"A": "hunter2"})))


class TestFileSecrets(_TmpDir):
    def test_parses_name_value_lines(self) -> None:
        path = self.write(
            "# a comment\n"
            "\n"
            "TOKEN=abc=def\n"
            "  SPACED=kept $HOME ${X} 'q'  \n"
            "EMPTY=\n"
        )
        source = FileSecrets(path)
        self.assertEqual("abc=def", source.get("TOKEN"))
        self.assertEqual("kept $HOME ${X} 'q'", source.get("SPACED"))
        self.assertEqual("", source.get("EMPTY"))
        self.assertIsNone(source.get("MISSING"))

    def test_later_line_wins(self) -> None:
        self.assertEqual("2", FileSecrets(self.write("A=1\nA=2\n")).get("A"))

    def test_missing_file_has_no_secrets(self) -> None:
        self.assertIsNone(FileSecrets(self.dir / "absent").get("A"))

    def test_refuses_group_or_world_access(self) -> None:
        for mode in (0o640, 0o604, 0o620):
            with self.subTest(mode=oct(mode)):
                source = FileSecrets(self.write("A=1\n", mode))
                with self.assertRaises(SecretsFileError) as cm:
                    source.get("A")
                self.assertIn("chmod 600", str(cm.exception))

    def test_refuses_a_file_owned_by_someone_else(self) -> None:
        source = FileSecrets(self.write("A=1\n"))
        with mock.patch("bot_bottle.secret_source.os.getuid", return_value=os.getuid() + 1), \
                self.assertRaises(SecretsFileError) as cm:
            source.get("A")
        self.assertIn("not owned", str(cm.exception))

    def test_malformed_line_names_the_line_not_the_value(self) -> None:
        source = FileSecrets(self.write("A=1\nnot-a-pair hunter2\n"))
        with self.assertRaises(SecretsFileError) as cm:
            source.get("A")
        self.assertIn("line 2", str(cm.exception))
        self.assertNotIn("hunter2", str(cm.exception))

    def test_reads_lazily_and_once(self) -> None:
        path = self.write("A=1\n", 0o644)
        source = FileSecrets(path)  # insecure, but nothing has asked yet
        path.chmod(0o600)
        self.assertEqual("1", source.get("A"))
        path.write_text("A=2\n")
        self.assertEqual("1", source.get("A"))


class TestChainSecrets(unittest.TestCase):
    def test_first_hit_wins(self) -> None:
        chain = ChainSecrets(
            MappingSecrets({"A": "first", "E": ""}),
            MappingSecrets({"A": "second", "B": "b", "E": "later"}),
        )
        self.assertEqual("first", chain.get("A"))
        self.assertEqual("b", chain.get("B"))
        self.assertEqual("", chain.get("E"))
        self.assertIsNone(chain.get("C"))

    def test_later_sources_are_not_consulted_on_a_hit(self) -> None:
        later = mock.Mock()
        self.assertEqual("1", ChainSecrets(MappingSecrets({"A": "1"}), later).get("A"))
        later.get.assert_not_called()


class TestSecretsFromEnv(_TmpDir):
    def test_env_then_file(self) -> None:
        path = self.write("A=file\nB=file\n")
        source = secrets_from_env({"A": "env"}, secrets_file=path)
        self.assertEqual("env", source.get("A"))
        self.assertEqual("file", source.get("B"))
        self.assertIsNone(source.get("C"))

    def test_defaults_to_the_root_secrets_file(self) -> None:
        source = secrets_from_env({})
        file_source = source.sources[1]
        assert isinstance(file_source, FileSecrets)
        self.assertEqual("secrets", file_source.path.name)


def _manifest(env: dict[str, str]) -> Manifest:
    return ManifestIndex.from_json_obj({
        "bottles": {"dev": {"env": env}},
        "agents": {"demo": {}},
    }).load_for_agent("demo", ("dev",))


class TestResolveEnvThroughASource(unittest.TestCase):
    def test_interpolated_value_comes_from_the_source(self) -> None:
        manifest = _manifest({"API_KEY": "${HOST_KEY}", "PLAIN": "lit"})
        with mock.patch.dict(os.environ, {"HOST_KEY": "from-env"}):
            resolved = resolve_env(manifest, MappingSecrets({"HOST_KEY": "from-source"}))
        self.assertEqual({"API_KEY": "from-source"}, resolved.forwarded)
        self.assertEqual({"PLAIN": "lit"}, resolved.literals)

    def test_empty_interpolated_value_raises(self) -> None:
        manifest = _manifest({"API_KEY": "${HOST_KEY}"})
        with self.assertRaises(MissingEnvVarError) as cm:
            resolve_env(manifest, MappingSecrets({"HOST_KEY": ""}))
        self.assertEqual("HOST_KEY", cm.exception.var_name)

    def test_secret_entry_asks_even_with_a_saved_value(self) -> None:
        # "?" asks at every launch; a saved value only earns a hint.
        manifest = _manifest({"TOKEN": "secret:?Enter it"})
        with mock.patch("bot_bottle.env._read_secret_silent", return_value="typed") as prompt, \
                mock.patch("bot_bottle.env.info") as info:
            resolved = resolve_env(manifest, MappingSecrets({"TOKEN": "from-source"}))
        prompt.assert_called_once_with("TOKEN", "Enter it")
        self.assertEqual({"TOKEN": "typed"}, resolved.forwarded)
        info.assert_called_once_with(
            "(a saved TOKEN exists; write TOKEN: secret:TOKEN to use it without asking)")

    def test_secret_entry_uses_the_saved_value_when_no_one_can_be_asked(self) -> None:
        manifest = _manifest({"TOKEN": "secret:?Enter it"})
        with mock.patch("bot_bottle.env._read_secret_silent") as prompt, \
                mock.patch("bot_bottle.env.warn") as warn:
            resolved = resolve_env(manifest, MappingSecrets({"TOKEN": "from-source"}),
                                   prompt=False)
        prompt.assert_not_called()
        self.assertEqual({"TOKEN": "from-source"}, resolved.forwarded)
        warn.assert_called_once_with(
            "TOKEN: secret:? asks at every launch; there's no one to ask here, so the saved "
            "value is used. Write TOKEN: secret:TOKEN to say so.")

    def test_secret_entry_prompts_when_absent(self) -> None:
        manifest = _manifest({"TOKEN": "secret:?Enter it"})
        with mock.patch("bot_bottle.env._read_secret_silent",
                        return_value="typed") as prompt:
            resolved = resolve_env(manifest, MappingSecrets({}))
        prompt.assert_called_once_with("TOKEN", "Enter it")
        self.assertEqual({"TOKEN": "typed"}, resolved.forwarded)

    def test_secret_entry_raises_when_absent_and_prompting_is_off(self) -> None:
        manifest = _manifest({"TOKEN": "secret:?"})
        with mock.patch("bot_bottle.env._read_secret_silent") as prompt, \
                self.assertRaises(MissingEnvVarError):
            resolve_env(manifest, MappingSecrets({}), prompt=False)
        prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
