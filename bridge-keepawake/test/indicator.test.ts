/**
 * `indicator` -- a pure read of `daemon.json` and the pid file, never a socket call
 * and never a `devc-bridge` subprocess (see src/main.ts's "status indicator" module
 * comment and the plan this was built from). Most cases here drive `indicatorState`/
 * `indicatorText` directly against fixture files; the last test drives the real CLI as
 * a subprocess specifically to prove the "no subprocess" claim end to end.
 */
import assert from "node:assert/strict";
import * as config from "../src/config.ts";
import * as main from "../src/main.ts";

const TMP_PREFIX = "bridge-keepawake-indicator-test-";

async function withTmpDir(fn: (dir: string) => Promise<void>): Promise<void> {
  const dir = await Deno.makeTempDir({ prefix: TMP_PREFIX });
  try {
    await fn(dir);
  } finally {
    await Deno.remove(dir, { recursive: true });
  }
}

/** A pid guaranteed to be dead: spawned, awaited to completion, never reused this fast. */
async function deadPid(): Promise<number> {
  const child = new Deno.Command("true", { stdin: "null", stdout: "null", stderr: "null" }).spawn();
  const pid = child.pid;
  await child.status;
  return pid;
}

function baseStatus(overrides: Partial<main.StatusPayload> = {}): main.StatusPayload {
  const now = Date.now() / 1000;
  return {
    pid: Deno.pid,
    startedAt: now - 100,
    socketPath: undefined,
    sessionKey: "nosocket",
    activePanes: [],
    statuses: {},
    lastPingOk: null,
    updatedAt: now,
    lastActiveAt: undefined,
    ...overrides,
  };
}

async function fixture(
  dir: string,
  opts: { pid?: number; status?: main.StatusPayload | null },
): Promise<main.Paths> {
  const paths = await main.resolvePaths(dir, undefined);
  if (opts.pid !== undefined) {
    await Deno.writeTextFile(paths.pid, `${opts.pid}\n`);
  }
  if (opts.status) {
    await main.writeStatus(paths, opts.status);
  }
  return paths;
}

const DEFAULT_CFG = config.defaults();

Deno.test("indicator: no status file at all -> absent, renders nothing", async () => {
  await withTmpDir(async (dir) => {
    const paths = await fixture(dir, { pid: Deno.pid, status: null });
    const info = await main.indicatorState(paths, DEFAULT_CFG);
    assert.equal(info.state, "absent");
    assert.equal(main.indicatorText(info, "keepawake", main.ICON_HOLDING, false), "");
  });
});

Deno.test("indicator: pid on record is dead -> absent, renders nothing", async () => {
  await withTmpDir(async (dir) => {
    const pid = await deadPid();
    const paths = await fixture(dir, { pid, status: baseStatus({ activePanes: ["w1:p1"] }) });
    const info = await main.indicatorState(paths, DEFAULT_CFG);
    assert.equal(info.state, "absent");
    assert.equal(main.indicatorText(info, "keepawake", main.ICON_HOLDING, false), "");
  });
});

Deno.test("indicator: activePanes non-empty -> holding icon", async () => {
  await withTmpDir(async (dir) => {
    const paths = await fixture(dir, {
      pid: Deno.pid,
      status: baseStatus({ activePanes: ["w1:p1"], lastPingOk: true }),
    });
    const info = await main.indicatorState(paths, DEFAULT_CFG);
    assert.equal(info.state, "holding");
    assert.equal(main.indicatorText(info, "keepawake", main.ICON_HOLDING, false), "☕ keepawake");
  });
});

Deno.test("indicator: activePanes empty but lastActiveAt 5s ago -> held", async () => {
  await withTmpDir(async (dir) => {
    const now = Date.now() / 1000;
    const paths = await fixture(dir, {
      pid: Deno.pid,
      status: baseStatus({ activePanes: [], lastPingOk: null, lastActiveAt: now - 5 }),
    });
    const info = await main.indicatorState(paths, DEFAULT_CFG);
    assert.equal(info.state, "holding");
    assert.equal(main.indicatorText(info, "keepawake", main.ICON_HOLDING, false), "☕ keepawake");
  });
});

Deno.test("indicator: idle beyond the hold -> nothing, or hollow circle with --show-idle", async () => {
  await withTmpDir(async (dir) => {
    const now = Date.now() / 1000;
    const paths = await fixture(dir, {
      pid: Deno.pid,
      status: baseStatus({ activePanes: [], lastPingOk: null, lastActiveAt: now - 45 }),
    });
    const info = await main.indicatorState(paths, DEFAULT_CFG);
    assert.equal(info.state, "idle");
    assert.equal(main.indicatorText(info, "keepawake", main.ICON_HOLDING, false), "");
    assert.equal(main.indicatorText(info, "keepawake", main.ICON_HOLDING, true), "○ keepawake");
  });
});

Deno.test("indicator: a longer indicatorHoldSec honours the wider hold", async () => {
  await withTmpDir(async (dir) => {
    const now = Date.now() / 1000;
    const paths = await fixture(dir, {
      pid: Deno.pid,
      status: baseStatus({ activePanes: [], lastPingOk: null, lastActiveAt: now - 45 }),
    });
    const cfg: config.Config = { ...DEFAULT_CFG, indicatorHoldSec: 60 };
    const info = await main.indicatorState(paths, cfg);
    assert.equal(info.state, "holding");
    assert.equal(main.indicatorText(info, "keepawake", main.ICON_HOLDING, false), "☕ keepawake");
  });
});

Deno.test("indicator: lastPingOk false while active -> fault icon", async () => {
  await withTmpDir(async (dir) => {
    const paths = await fixture(dir, {
      pid: Deno.pid,
      status: baseStatus({ activePanes: ["w1:p1"], lastPingOk: false }),
    });
    const info = await main.indicatorState(paths, DEFAULT_CFG);
    assert.equal(info.state, "fault");
    assert.equal(main.indicatorText(info, "keepawake", main.ICON_HOLDING, false), "⚠ keepawake");
  });
});

Deno.test("indicator: updatedAt beyond the staleness bound -> wedged, fault icon", async () => {
  await withTmpDir(async (dir) => {
    const now = Date.now() / 1000;
    // pollIntervalSec=2 -> staleAfterSec = max(15, 2*5) = 15; 20s old is beyond it.
    const paths = await fixture(dir, {
      pid: Deno.pid,
      status: baseStatus({ activePanes: ["w1:p1"], lastPingOk: true, updatedAt: now - 20 }),
    });
    const info = await main.indicatorState(paths, DEFAULT_CFG);
    assert.equal(info.state, "wedged");
    assert.equal(main.indicatorText(info, "keepawake", main.ICON_HOLDING, false), "⚠ keepawake");
  });
});

Deno.test("indicator: --label/--icon override both halves of the output", async () => {
  await withTmpDir(async (dir) => {
    const paths = await fixture(dir, {
      pid: Deno.pid,
      status: baseStatus({ activePanes: ["w1:p1"], lastPingOk: true }),
    });
    const info = await main.indicatorState(paths, DEFAULT_CFG);
    assert.equal(main.indicatorText(info, "AWAKE", "🔴", false), "🔴 AWAKE");
  });
});

Deno.test({
  name: "indicator: the CLI path spawns no subprocess, even when a stub devc-bridge could be reached",
  // Drives the real CLI end to end -- a `main.ts indicator` invocation with `--allow-run`
  // deliberately granted for the stub, so if `indicator` ever regresses into calling
  // `bridge.ping()`, the stub's marker file would prove it rather than a permission
  // error silently getting swallowed (ping() catches every error it can raise).
  permissions: { read: true, write: true, run: true, env: true },
  fn: async () => {
    await withTmpDir(async (dir) => {
      const stateDir = `${dir}/state`;
      const configDir = `${dir}/config`;
      const pathDir = `${dir}/bin`;
      await Deno.mkdir(stateDir, { recursive: true });
      await Deno.mkdir(configDir, { recursive: true });
      await Deno.mkdir(pathDir, { recursive: true });

      const marker = `${dir}/bridge-was-called.marker`;
      const stub = `${pathDir}/devc-bridge`;
      await Deno.writeTextFile(stub, `#!/bin/sh\ntouch '${marker}'\nexit 0\n`);
      await Deno.chmod(stub, 0o755);

      const paths = await main.resolvePaths(stateDir, undefined);
      await Deno.writeTextFile(paths.pid, `${Deno.pid}\n`);
      await main.writeStatus(
        paths,
        baseStatus({ activePanes: ["w1:p1"], lastPingOk: true }),
      );

      const root = new URL("..", import.meta.url).pathname;
      const cmd = new Deno.Command(Deno.execPath(), {
        args: [
          "run",
          `--allow-read=${stateDir},${configDir}`,
          `--allow-write=${stateDir}`,
          "--allow-env",
          `--allow-run=devc-bridge,kill`,
          `${root}src/main.ts`,
          "indicator",
        ],
        env: {
          // `kill` must still resolve for the pidAlive check -- only devc-bridge is
          // stubbed, by putting pathDir first so it shadows any real one on PATH.
          PATH: `${pathDir}:/usr/bin:/bin`,
          HOME: dir,
          HERDR_PLUGIN_STATE_DIR: stateDir,
          HERDR_PLUGIN_CONFIG_DIR: configDir,
        },
        clearEnv: true,
        stdout: "piped",
        stderr: "piped",
      });
      const { code, stdout } = await cmd.output();
      assert.equal(code, 0);
      assert.equal(new TextDecoder().decode(stdout).trim(), "☕ keepawake");

      let markerExists = true;
      try {
        await Deno.stat(marker);
      } catch {
        markerExists = false;
      }
      assert.equal(markerExists, false, "indicator must never invoke devc-bridge");
    });
  },
});
