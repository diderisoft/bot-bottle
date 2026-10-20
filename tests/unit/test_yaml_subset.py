"""Unit: YAML-subset parser used by the per-file MD manifest
(PRD 0011). Covers happy paths, the constructs the manifest files
actually use, and every rejection case the PRD enumerates."""

import textwrap
import unittest

from bot_bottle.contract.yaml_subset import YamlSubsetError
from bot_bottle.contract.yaml_subset import parse_frontmatter, parse_yaml_subset, serialize_yaml_subset


def _y(s: str):
    """Parse a dedented YAML string."""
    return parse_yaml_subset(textwrap.dedent(s).lstrip("\n"))


class TestScalars(unittest.TestCase):
    def test_string(self):
        self.assertEqual({"k": "hello"}, _y("k: hello\n"))

    def test_string_with_url_chars(self):
        self.assertEqual(
            {"k": "https://example.com/path?x=1"},
            _y("k: https://example.com/path?x=1\n"),
        )

    def test_int(self):
        self.assertEqual({"port": 9099}, _y("port: 9099\n"))

    def test_negative_int(self):
        self.assertEqual({"n": -3}, _y("n: -3\n"))

    def test_bool_true(self):
        self.assertEqual({"x": True}, _y("x: true\n"))

    def test_bool_false(self):
        self.assertEqual({"x": False}, _y("x: false\n"))

    def test_null(self):
        self.assertEqual({"x": None}, _y("x: null\n"))

    def test_tilde_null(self):
        self.assertEqual({"x": None}, _y("x: ~\n"))

    def test_double_quoted_string(self):
        self.assertEqual({"k": "a b"}, _y('k: "a b"\n'))

    def test_double_quoted_escape(self):
        self.assertEqual({"k": "a\nb"}, _y(r'k: "a\nb"' + "\n"))

    def test_single_quoted_string(self):
        self.assertEqual({"k": "a b"}, _y("k: 'a b'\n"))

    def test_single_quoted_apos_double(self):
        # Single-quoted YAML uses `''` to embed a literal `'`.
        self.assertEqual({"k": "it's"}, _y("k: 'it''s'\n"))


class TestForbiddenBoolLikes(unittest.TestCase):
    """Ambiguous bool-ish tokens have to be quoted explicitly."""

    def _expect_die(self, src: str):
        with self.assertRaises(YamlSubsetError):
            _y(src)

    def test_yes_dies(self):
        self._expect_die("k: yes\n")

    def test_no_dies(self):
        self._expect_die("k: no\n")

    def test_on_dies(self):
        self._expect_die("k: on\n")

    def test_capital_TRUE_dies(self):
        self._expect_die("k: TRUE\n")

    def test_norway_quoted_is_fine(self):
        self.assertEqual({"country": "NO"}, _y('country: "NO"\n'))


class TestForbiddenScalarShapes(unittest.TestCase):
    def _expect_die(self, src: str):
        with self.assertRaises(YamlSubsetError):
            _y(src)

    def test_bare_date_dies(self):
        self._expect_die("k: 2026-05-24\n")

    def test_bare_octal_dies(self):
        self._expect_die("k: 0o755\n")

    def test_bare_hex_dies(self):
        self._expect_die("k: 0xFF\n")

    def test_bare_float_dies(self):
        self._expect_die("k: 1.5\n")

    def test_quoted_date_is_fine(self):
        self.assertEqual({"k": "2026-05-24"}, _y('k: "2026-05-24"\n'))


class TestMapping(unittest.TestCase):
    def test_flat_mapping(self):
        self.assertEqual(
            {"a": 1, "b": "two", "c": True},
            _y("""
                a: 1
                b: two
                c: true
            """),
        )

    def test_nested_mapping(self):
        out = _y("""
            outer:
              inner: hello
              other: 5
        """)
        self.assertEqual({"outer": {"inner": "hello", "other": 5}}, out)

    def test_duplicate_key_dies(self):
        with self.assertRaises(YamlSubsetError):
            _y("""
                a: 1
                a: 2
            """)

    def test_key_must_be_bare_identifier(self):
        with self.assertRaises(YamlSubsetError):
            _y('"weird key": 1\n')


class TestBlockList(unittest.TestCase):
    def test_list_of_strings(self):
        out = _y("""
            allowlist:
              - example.com
              - github.com
        """)
        self.assertEqual({"allowlist": ["example.com", "github.com"]}, out)

    def test_list_of_mappings(self):
        out = _y("""
            routes:
              - path: /a/
                upstream: https://a.example
              - path: /b/
                upstream: https://b.example
        """)
        self.assertEqual(
            {"routes": [
                {"path": "/a/", "upstream": "https://a.example"},
                {"path": "/b/", "upstream": "https://b.example"},
            ]},
            out,
        )

    def test_list_item_with_nested_mapping(self):
        out = _y("""
            entries:
              - name: foo
                metadata:
                  host.example: 10.0.0.1
              - name: bar
        """)
        self.assertEqual(
            {"entries": [
                {"name": "foo", "metadata": {"host.example": "10.0.0.1"}},
                {"name": "bar"},
            ]},
            out,
        )

    def test_list_item_with_inline_list_value(self):
        # role: [git-insteadof, tea-login] — the exact shape in the
        # bot-bottle manifest.
        out = _y("""
            routes:
              - path: /x/
                role: [git-insteadof, tea-login]
        """)
        self.assertEqual(
            {"routes": [
                {"path": "/x/", "role": ["git-insteadof", "tea-login"]},
            ]},
            out,
        )


class TestInline(unittest.TestCase):
    def test_inline_list(self):
        self.assertEqual({"l": [1, 2, 3]}, _y("l: [1, 2, 3]\n"))

    def test_inline_list_of_strings(self):
        self.assertEqual({"l": ["a", "b", "c"]}, _y("l: [a, b, c]\n"))

    def test_inline_dict(self):
        self.assertEqual(
            {"d": {"a": "1", "b": "2"}},
            _y('d: {a: "1", b: "2"}\n'),
        )

    def test_nested_flow_dies(self):
        with self.assertRaises(YamlSubsetError):
            _y("l: [[1, 2], [3, 4]]\n")


class TestForbiddenConstructs(unittest.TestCase):
    def test_anchor_dies(self):
        with self.assertRaises(YamlSubsetError):
            _y("""
                a: &anchor 1
                b: *anchor
            """)

    def test_multiline_block_scalar_dies(self):
        with self.assertRaises(YamlSubsetError):
            _y("""
                k: |
                  line 1
                  line 2
            """)

    def test_tag_dies(self):
        with self.assertRaises(YamlSubsetError):
            _y("k: !!str hello\n")

    def test_tab_in_indent_dies(self):
        with self.assertRaises(YamlSubsetError):
            _y("a:\n\tb: 1\n")


class TestComments(unittest.TestCase):
    def test_full_line_comment(self):
        out = _y("""
            # comment
            k: v
        """)
        self.assertEqual({"k": "v"}, out)

    def test_trailing_comment(self):
        self.assertEqual({"k": "v"}, _y("k: v  # trailing\n"))

    def test_hash_in_quoted_string_kept(self):
        self.assertEqual({"k": "a#b"}, _y('k: "a#b"\n'))


class TestRealisticBottleFile(unittest.TestCase):
    """The exact shape a real bottle frontmatter takes — the parser
    has to round-trip this without surprise."""

    def test_dev_bottle(self):
        out = _y("""
            egress:
              routes:
                - host: api.anthropic.com
                  auth:
                    scheme: Bearer
                    token_ref: CLAUDE_CODE_OAUTH_TOKEN
                - host: gitea.dideric.is
                  auth:
                    scheme: token
                    token_ref: GITEA_TOKEN
                  matches:
                    - paths:
                        - value: /didericis/
            git:
              remotes:
                gitea.dideric.is:
                  Name: bot-bottle
                  Upstream: ssh://git@gitea.dideric.is:30009/x/y.git
                  IdentityFile: ~/.ssh/gitea.pem
                  KnownHostKey: ssh-ed25519 AAAA...
        """)
        # Spot-check the deep parts; the structure is large.
        self.assertEqual(2, len(out["egress"]["routes"]))  # type: ignore
        self.assertEqual(
            "/didericis/",
            out["egress"]["routes"][1]["matches"][0]["paths"][0]["value"],  # type: ignore
        )
        self.assertEqual(
            "Bearer",
            out["egress"]["routes"][0]["auth"]["scheme"],  # type: ignore
        )
        self.assertEqual(
            "ssh-ed25519 AAAA...",
            out["git"]["remotes"]["gitea.dideric.is"]["KnownHostKey"],  # type: ignore
        )


class TestFrontmatter(unittest.TestCase):
    def test_basic(self):
        text = textwrap.dedent("""
            ---
            bottle: dev
            ---
            This is the body.
        """).lstrip("\n")
        fm, body = parse_frontmatter(text)
        self.assertEqual({"bottle": "dev"}, fm)
        self.assertIn("This is the body", body)

    def test_no_frontmatter_passes_through(self):
        text = "no frontmatter here\njust body\n"
        fm, body = parse_frontmatter(text)
        self.assertEqual({}, fm)
        self.assertEqual(text, body)

    def test_unclosed_frontmatter_dies(self):
        with self.assertRaises(YamlSubsetError):
            parse_frontmatter("---\nbottle: dev\nno closing")

    def test_body_preserves_blank_lines(self):
        text = (
            "---\n"
            "k: v\n"
            "---\n"
            "\n"
            "line one\n"
            "\n"
            "line three\n"
        )
        _, body = parse_frontmatter(text)
        self.assertEqual("\nline one\n\nline three\n", body)


class TestEdgeAndErrorBranches(unittest.TestCase):
    """Reachable error / edge branches of the parser (coverage ratchet)."""

    # --- scalars / comments -------------------------------------------------
    def test_hash_not_preceded_by_space_is_literal(self) -> None:
        self.assertEqual({"k": "a#b"}, parse_yaml_subset("k: a#b\n"))

    def test_blank_line_between_entries_skipped(self) -> None:
        self.assertEqual({"a": 1, "b": 2}, parse_yaml_subset("a: 1\n\nb: 2\n"))

    def test_unterminated_quote_single_char(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset('k: "\n')

    def test_bad_double_quote_escape(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset('k: "\\x"\n')

    # --- inline list / dict -------------------------------------------------
    def test_inline_dict_empty_value_is_empty_string(self) -> None:
        self.assertEqual({"k": {"a": ""}}, parse_yaml_subset("k: {a: }\n"))

    def test_unterminated_inline_list(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("k: [a, b\n")

    def test_empty_inline_list(self) -> None:
        self.assertEqual({"k": []}, parse_yaml_subset("k: []\n"))

    def test_unterminated_inline_dict(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("k: {a: 1\n")

    def test_empty_inline_dict(self) -> None:
        self.assertEqual({"k": {}}, parse_yaml_subset("k: {}\n"))

    def test_inline_dict_entry_missing_colon(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("k: {a}\n")

    def test_inline_dict_non_bare_key(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("k: {$x: 1}\n")

    def test_quoted_comma_in_flow_is_one_item(self) -> None:
        self.assertEqual({"k": ["a", "b, c"]}, parse_yaml_subset("k: [a, 'b, c']\n"))

    # --- block mapping / list ----------------------------------------------
    def test_line_missing_colon_separator(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("justtext\n")

    def test_single_quoted_key_rejected_as_non_bare(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("'ab': v\n")

    def test_list_item_at_mapping_indent_rejected(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("a: 1\n- b\n")

    def test_empty_block_value_is_none(self) -> None:
        self.assertEqual({"k": None}, parse_yaml_subset("k:\n"))

    def test_list_item_first_key_non_bare(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("k:\n  - $x: 1\n")

    def test_bare_dash_nested_block_list(self) -> None:
        self.assertEqual(
            {"k": [["nested"]]},
            parse_yaml_subset("k:\n  -\n    - nested\n"),
        )

    def test_list_item_quoted_colon_is_scalar(self) -> None:
        self.assertEqual({"k": ["a:b"]}, parse_yaml_subset('k:\n  - "a:b"\n'))

    def test_list_item_mapping_with_nested_block(self) -> None:
        self.assertEqual(
            {"k": [{"a": {"b": 2}}]},
            parse_yaml_subset("k:\n  - a:\n        b: 2\n"),
        )

    def test_list_item_sibling_key_empty_is_none(self) -> None:
        self.assertEqual(
            {"k": [{"a": 1, "b": None}]},
            parse_yaml_subset("k:\n  - a: 1\n    b:\n"),
        )

    def test_list_item_duplicate_key(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("k:\n  - a: 1\n    a: 2\n")

    def test_list_item_sibling_key_non_bare(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("k:\n  - a: 1\n    $b: 2\n")

    # --- document-level rejections -----------------------------------------
    def test_block_scalar_folded_rejected(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset(">folded\n")

    def test_block_scalar_literal_rejected(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("|literal\n")

    def test_anchor_rejected(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("k: &a x\n")

    def test_ampersand_in_quoted_value_allowed(self) -> None:
        self.assertEqual({"k": "a & b"}, parse_yaml_subset('k: "a & b"\n'))

    def test_yaml_tag_rejected(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("k: !!str x\n")

    def test_only_comments_is_empty_mapping(self) -> None:
        self.assertEqual({}, parse_yaml_subset("# just a comment\n"))

    def test_top_level_not_column_zero(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("  k: 1\n")

    def test_top_level_list_rejected(self) -> None:
        with self.assertRaises(YamlSubsetError):
            parse_yaml_subset("- a\n- b\n")

    # --- frontmatter --------------------------------------------------------
    def test_frontmatter_empty_text(self) -> None:
        self.assertEqual(({}, ""), parse_frontmatter(""))


# ---------------------------------------------------------------------------
# serialize_yaml_subset
# ---------------------------------------------------------------------------


class TestSerializeYamlSubset(unittest.TestCase):
    def _roundtrip(self, text: str) -> None:
        """Parse then serialize then re-parse; result must equal original parse."""
        parsed = parse_yaml_subset(text)
        serialized = serialize_yaml_subset(parsed)
        self.assertEqual(parsed, parse_yaml_subset(serialized))

    # --- scalar values ---------------------------------------------------------

    def test_string_bare(self) -> None:
        self.assertEqual("key: value\n", serialize_yaml_subset({"key": "value"}))

    def test_string_empty_is_quoted(self) -> None:
        out = serialize_yaml_subset({"key": ""})
        self.assertIn("key: ''", out)
        self._roundtrip("key: ''\n")

    def test_string_true_like_is_quoted(self) -> None:
        out = serialize_yaml_subset({"key": "true"})
        self.assertIn("'true'", out)
        self.assertEqual({"key": "true"}, parse_yaml_subset(out))

    def test_string_int_like_is_quoted(self) -> None:
        out = serialize_yaml_subset({"key": "42"})
        self.assertIn("'42'", out)
        self.assertEqual({"key": "42"}, parse_yaml_subset(out))

    def test_string_special_start_char_is_quoted(self) -> None:
        for ch in ('"', "'", "[", "{", "!", "&", "*", "#", "|", ">"):
            out = serialize_yaml_subset({"key": ch + "rest"})
            self.assertIn("'", out, msg=f"expected quoting for {ch!r}")

    def test_none_emits_null(self) -> None:
        self.assertEqual("key: null\n", serialize_yaml_subset({"key": None}))

    def test_bool_true(self) -> None:
        self.assertEqual("key: true\n", serialize_yaml_subset({"key": True}))

    def test_bool_false(self) -> None:
        self.assertEqual("key: false\n", serialize_yaml_subset({"key": False}))

    def test_int(self) -> None:
        self.assertEqual("key: 42\n", serialize_yaml_subset({"key": 42}))

    # --- nested dict -----------------------------------------------------------

    def test_nested_dict(self) -> None:
        data: dict[str, object] = {"outer": {"inner": "val"}}
        out = serialize_yaml_subset(data)
        self.assertIn("outer:", out)
        self.assertIn("  inner: val", out)
        self.assertEqual(data, parse_yaml_subset(out))

    def test_empty_dict_value_inline(self) -> None:
        out = serialize_yaml_subset({"key": {}})
        self.assertIn("key: {}", out)

    # --- lists -----------------------------------------------------------------

    def test_list_of_scalars(self) -> None:
        data: dict[str, object] = {"items": ["a", "b", "c"]}
        out = serialize_yaml_subset(data)
        self.assertEqual(data, parse_yaml_subset(out))

    def test_empty_list_value_inline(self) -> None:
        out = serialize_yaml_subset({"key": []})
        self.assertIn("key: []", out)

    def test_list_of_mappings(self) -> None:
        data: dict[str, object] = {"items": [{"name": "alpha", "val": 1}, {"name": "beta", "val": 2}]}
        out = serialize_yaml_subset(data)
        self.assertEqual(data, parse_yaml_subset(out))

    def test_list_item_mapping_with_nested_dict(self) -> None:
        data: dict[str, object] = {"items": [{"name": "x", "sub": {"k": "v"}}]}
        out = serialize_yaml_subset(data)
        self.assertEqual(data, parse_yaml_subset(out))

    def test_list_item_first_key_is_nested_dict(self) -> None:
        # Covers the branch where the FIRST key of a mapping list item
        # has a nested dict/list value (not a scalar).
        data: dict[str, object] = {"items": [{"cfg": {"a": 1, "b": 2}, "name": "x"}]}
        out = serialize_yaml_subset(data)
        self.assertEqual(data, parse_yaml_subset(out))

    def test_list_item_mapping_continuation_keys(self) -> None:
        data: dict[str, object] = {"items": [{"a": 1, "b": 2, "c": 3}]}
        out = serialize_yaml_subset(data)
        self.assertEqual(data, parse_yaml_subset(out))

    # --- empty / trivial -------------------------------------------------------

    def test_empty_dict(self) -> None:
        self.assertEqual("", serialize_yaml_subset({}))

    # --- realistic bottle frontmatter round-trip --------------------------------

    def test_bottle_frontmatter_round_trip(self) -> None:
        text = textwrap.dedent("""\
            git-gate:
              repos:
                myrepo:
                  url: ssh://git@host/org/repo.git
                  key:
                    provider: static
                    path: /dev/null
        """)
        self._roundtrip(text)

    def test_host_key_inserted_at_repo_level(self) -> None:
        """The canonical use-case: host_key lands as a sibling of url/key."""
        from typing import cast
        text = textwrap.dedent("""\
            git-gate:
              repos:
                myrepo:
                  url: ssh://git@host/org/repo.git
                  key:
                    provider: static
                    path: /dev/null
        """)
        data = parse_yaml_subset(text)
        repos = cast(dict[str, object], cast(dict[str, object], data["git-gate"])["repos"])
        cast(dict[str, object], repos["myrepo"])["host_key"] = "ssh-ed25519 AAAA"
        out = serialize_yaml_subset(data)
        parsed_back = parse_yaml_subset(out)
        repo = cast(dict[str, object], cast(dict[str, object], cast(dict[str, object], parsed_back["git-gate"])["repos"])["myrepo"])
        self.assertEqual("ssh-ed25519 AAAA", repo["host_key"])
        self.assertIn("provider", cast(dict[str, object], repo["key"]))


if __name__ == "__main__":
    unittest.main()
