import assert from "node:assert/strict";
import * as config from "../src/config.ts";

const TMP_PREFIX = "bridge-keepawake-config-test-";

async function withTmpDir(fn: (dir: string) => Promise<void>): Promise<void> {
  const dir = await Deno.makeTempDir({ prefix: TMP_PREFIX });
  try {
    await fn(dir);
  } finally {
    await Deno.remove(dir, { recursive: true });
  }
}

Deno.test("config: defaults with no file at all", async () => {
  await withTmpDir(async (dir) => {
    const cfg = await config.load({ HERDR_PLUGIN_CONFIG_DIR: dir });
    assert.equal(cfg.pollIntervalSec, 2);
    assert.deepEqual(cfg.activeStatuses, ["working"]);
    assert.equal(cfg.bridgeCommand, "devc-bridge");
    assert.equal(cfg.pingLabel, "bridge-keepawake");
    assert.equal(cfg.logLevel, "info");
    assert.equal(cfg.source, "defaults");
    assert.deepEqual(cfg.warnings, []);
  });
});

Deno.test("config: every key overridable from a config.json file", async () => {
  await withTmpDir(async (dir) => {
    await Deno.writeTextFile(
      `${dir}/config.json`,
      JSON.stringify({
        pollIntervalSec: 5,
        activeStatuses: ["working", "blocked"],
        bridgeCommand: "/opt/devc-bridge/devc-bridge",
        pingLabel: "custom",
        logLevel: "debug",
      }),
    );
    const cfg = await config.load({ HERDR_PLUGIN_CONFIG_DIR: dir });
    assert.equal(cfg.pollIntervalSec, 5);
    assert.deepEqual(cfg.activeStatuses, ["working", "blocked"]);
    assert.equal(cfg.bridgeCommand, "/opt/devc-bridge/devc-bridge");
    assert.equal(cfg.pingLabel, "custom");
    assert.equal(cfg.logLevel, "debug");
    assert.equal(cfg.source, "file");
  });
});

Deno.test("config: JSONC comments and trailing commas are tolerated", async () => {
  await withTmpDir(async (dir) => {
    await Deno.writeTextFile(
      `${dir}/config.json`,
      `{
        // a line comment
        "pollIntervalSec": 3, /* a block comment */
        "activeStatuses": ["working",],
      }`,
    );
    const cfg = await config.load({ HERDR_PLUGIN_CONFIG_DIR: dir });
    assert.equal(cfg.pollIntervalSec, 3);
    assert.deepEqual(cfg.activeStatuses, ["working"]);
  });
});

Deno.test("config: unknown keys warn, not fail", async () => {
  await withTmpDir(async (dir) => {
    await Deno.writeTextFile(`${dir}/config.json`, JSON.stringify({ idleGraceSec: 60 }));
    const cfg = await config.load({ HERDR_PLUGIN_CONFIG_DIR: dir });
    assert.equal(cfg.warnings.length, 1);
    assert.match(cfg.warnings[0], /idleGraceSec/);
  });
});

Deno.test("config: pollIntervalSec must be a positive number", async () => {
  await withTmpDir(async (dir) => {
    await Deno.writeTextFile(`${dir}/config.json`, JSON.stringify({ pollIntervalSec: -1 }));
    await assert.rejects(() => config.load({ HERDR_PLUGIN_CONFIG_DIR: dir }), config.ConfigError);
  });
});

Deno.test("config: activeStatuses must be a non-empty array", async () => {
  await withTmpDir(async (dir) => {
    await Deno.writeTextFile(`${dir}/config.json`, JSON.stringify({ activeStatuses: [] }));
    await assert.rejects(() => config.load({ HERDR_PLUGIN_CONFIG_DIR: dir }), config.ConfigError);
  });
});

Deno.test("config: logLevel must be one of the known values", async () => {
  await withTmpDir(async (dir) => {
    await Deno.writeTextFile(`${dir}/config.json`, JSON.stringify({ logLevel: "verbose" }));
    await assert.rejects(() => config.load({ HERDR_PLUGIN_CONFIG_DIR: dir }), config.ConfigError);
  });
});

Deno.test("config: malformed JSON is a ConfigError naming the file", async () => {
  await withTmpDir(async (dir) => {
    await Deno.writeTextFile(`${dir}/config.json`, "{not json");
    await assert.rejects(() => config.load({ HERDR_PLUGIN_CONFIG_DIR: dir }), config.ConfigError);
  });
});

Deno.test("config: env overrides win over the file, and are test-only", async () => {
  await withTmpDir(async (dir) => {
    await Deno.writeTextFile(`${dir}/config.json`, JSON.stringify({ pollIntervalSec: 5 }));
    const cfg = await config.load({
      HERDR_PLUGIN_CONFIG_DIR: dir,
      HERDR_KEEPAWAKE_POLL_INTERVAL_SEC: "9",
    });
    assert.equal(cfg.pollIntervalSec, 9);
  });
});

Deno.test("config: no idleGraceSec / inhibitorCommand keys exist -- by design", async () => {
  await withTmpDir(async (dir) => {
    await Deno.writeTextFile(
      `${dir}/config.json`,
      JSON.stringify({ idleGraceSec: 60, inhibitorCommand: ["caffeinate"] }),
    );
    const cfg = await config.load({ HERDR_PLUGIN_CONFIG_DIR: dir });
    assert.equal(cfg.warnings.length, 2);
    assert.ok(!("idleGraceSec" in cfg));
    assert.ok(!("inhibitorCommand" in cfg));
  });
});
