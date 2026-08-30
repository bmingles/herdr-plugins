// `node:assert/strict` rather than `jsr:@std/assert`: no network fetch needed to run
// `deno test` (this plugin makes no runtime dependency on the network beyond the one
// scoped Herdr socket, and its test suite doesn't either).
import assert from "node:assert/strict";
import { activePaneIds, isAnyPaneActive } from "../src/is-active.ts";

Deno.test("isAnyPaneActive: no panes at all", () => {
  assert.equal(isAnyPaneActive({}, ["working"]), false);
});

Deno.test("isAnyPaneActive: no panes with an agent", () => {
  // A socket-only reader sees "done", essentially never "idle" -- herdr-daemon-facts.md
  // § C4. Fixtures reflect that rather than a state that does not occur.
  assert.equal(isAnyPaneActive({ "w1:p1": "done" }, ["working"]), false);
});

Deno.test("isAnyPaneActive: one working pane counts", () => {
  assert.equal(
    isAnyPaneActive({ "w1:p1": "done", "w2:p1": "working" }, ["working"]),
    true,
  );
});

Deno.test("isAnyPaneActive: blocked is excluded by default", () => {
  assert.equal(isAnyPaneActive({ "w1:p1": "blocked" }, ["working"]), false);
});

Deno.test("isAnyPaneActive: blocked counts when explicitly configured", () => {
  assert.equal(isAnyPaneActive({ "w1:p1": "blocked" }, ["working", "blocked"]), true);
});

Deno.test("isAnyPaneActive: unknown status never matches", () => {
  assert.equal(isAnyPaneActive({ "w1:p1": "unknown" }, ["working"]), false);
});

Deno.test("activePaneIds: returns only the matching panes, sorted", () => {
  assert.deepEqual(
    activePaneIds(
      { "w2:p1": "working", "w1:p1": "done", "w3:p1": "working" },
      ["working"],
    ),
    ["w2:p1", "w3:p1"],
  );
});

Deno.test("activePaneIds: empty when nothing matches", () => {
  assert.deepEqual(activePaneIds({ "w1:p1": "done" }, ["working"]), []);
});
