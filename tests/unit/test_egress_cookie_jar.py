"""Unit: the egress gateway's per-bottle, per-host cookie jar (#807)."""

from __future__ import annotations

import unittest

from bot_bottle.gateway.egress.cookie_jar import MAX_COOKIES_PER_HOST, CookieJar


class TestCookieJar(unittest.TestCase):
    def test_nothing_set_nothing_sent(self) -> None:
        self.assertEqual("", CookieJar().filter("b", "h.example", ["sid=attacker"]))

    def test_a_cookie_the_host_set_is_sent(self) -> None:
        jar = CookieJar()
        jar.record("b", "h.example", ["sid=abc; Path=/; HttpOnly; Secure"])
        self.assertEqual("sid=abc", jar.filter("b", "h.example", ["sid=abc"]))

    def test_only_set_pairs_survive_in_sent_order(self) -> None:
        jar = CookieJar()
        jar.record("b", "h.example", ["a=1", "c=3; Max-Age=60"])
        self.assertEqual(
            "c=3; a=1",
            jar.filter("b", "h.example", ["c=3; evil=x", "a=1; b=2"]),
        )

    def test_same_name_other_value_is_dropped(self) -> None:
        jar = CookieJar()
        jar.record("b", "h.example", ["sid=abc"])
        self.assertEqual("", jar.filter("b", "h.example", ["sid=attacker"]))

    def test_a_later_set_replaces_the_value(self) -> None:
        jar = CookieJar()
        jar.record("b", "h.example", ["sid=old"])
        jar.record("b", "h.example", ["sid=new"])
        self.assertEqual("", jar.filter("b", "h.example", ["sid=old"]))
        self.assertEqual("sid=new", jar.filter("b", "h.example", ["sid=new"]))

    def test_scoped_to_bottle_and_host(self) -> None:
        jar = CookieJar()
        jar.record("b", "h.example", ["sid=abc"])
        self.assertEqual("", jar.filter("other", "h.example", ["sid=abc"]))
        self.assertEqual("", jar.filter("b", "sub.h.example", ["sid=abc"]))
        self.assertEqual("sid=abc", jar.filter("b", "H.Example.", ["sid=abc"]))

    def test_unattributed_records_nothing(self) -> None:
        jar = CookieJar()
        jar.record("", "h.example", ["sid=abc"])
        self.assertEqual("", jar.filter("", "h.example", ["sid=abc"]))

    def test_max_age_zero_deletes(self) -> None:
        jar = CookieJar()
        jar.record("b", "h.example", ["sid=abc"])
        jar.record("b", "h.example", ["sid=; Max-Age=0"])
        self.assertEqual("", jar.filter("b", "h.example", ["sid=abc"]))

    def test_past_expires_deletes(self) -> None:
        jar = CookieJar()
        jar.record("b", "h.example", ["sid=abc"])
        jar.record("b", "h.example", ["sid=gone; Expires=Thu, 01 Jan 1970 00:00:00 GMT"])
        self.assertEqual("", jar.filter("b", "h.example", ["sid=gone"]))
        self.assertEqual("", jar.filter("b", "h.example", ["sid=abc"]))

    def test_future_expires_and_bad_attributes_keep(self) -> None:
        jar = CookieJar()
        jar.record("b", "h.example", [
            "a=1; Expires=Fri, 01 Jan 2100 00:00:00 GMT",
            "b=2; Expires=not a date",
            "c=3; Max-Age=soon",
        ])
        self.assertEqual("a=1; b=2; c=3", jar.filter("b", "h.example", ["a=1; b=2; c=3"]))

    def test_malformed_set_cookie_is_ignored(self) -> None:
        jar = CookieJar()
        jar.record("b", "h.example", ["novalue", "=x", ""])
        self.assertEqual("", jar.filter("b", "h.example", ["novalue"]))

    def test_jar_is_bounded(self) -> None:
        jar = CookieJar()
        jar.record("b", "h.example", [f"c{i}=v" for i in range(MAX_COOKIES_PER_HOST + 1)])
        self.assertEqual("", jar.filter("b", "h.example", ["c0=v"]))
        self.assertEqual("c1=v", jar.filter("b", "h.example", ["c1=v"]))


if __name__ == "__main__":
    unittest.main()
