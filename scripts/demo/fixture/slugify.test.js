import { test } from "node:test";
import assert from "node:assert/strict";
import { slugify } from "./slugify.js";

test("lowercases words and joins them with dashes", () => {
  assert.equal(slugify("Hello World"), "hello-world");
});

test("drops punctuation and collapses repeated separators", () => {
  assert.equal(slugify("  Hello,  World!  "), "hello-world");
});
