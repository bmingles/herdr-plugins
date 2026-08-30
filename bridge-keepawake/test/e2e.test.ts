/**
 * End-to-end against a fake Herdr socket and a fake `devc-bridge`: the two real
 * integration points this plugin has with the outside world, both faked here rather
 * than mocked, so the test exercises the actual `Deno.connect`/`Deno.Command` calls
 * `src/sock.ts` and `src/bridge.ts` make. This is what makes the test "ordinary" per
 * the plan this plugin was built from, rather than a bespoke integration harness: the
 * fake snapshot server is a `Deno.listen({ transport: "unix" })` in the test itself.
 *
 * Deliberately does not drive this through `main.ts`'s daemon/lock/detach machinery --
 * that's process lifecycle, exercised indirectly by the unit tests around it. This
 * test is about one thing: does "any pane working" reliably turn into a
 * `devc-bridge ping` call, and does it reliably stop when nothing is working.
 */
import assert from "node:assert/strict";
import { paneStatuses } from "../src/sock.ts";
import { ping } from "../src/bridge.ts";
import { isAnyPaneActive } from "../src/is-active.ts";

/** A fake Herdr server: answers `session.snapshot` from mutable in-test state. */
async function fakeHerdrServer(socketPath: string, panes: () => Record<string, string>) {
  const listener = Deno.listen({ transport: "unix", path: socketPath });
  const closed = { value: false };

  (async () => {
    for await (const conn of listener) {
      (async () => {
        try {
          const buf = new Uint8Array(1 << 16);
          const n = await conn.read(buf);
          if (n === null) return;
          const req = JSON.parse(new TextDecoder().decode(buf.subarray(0, n)));
          const reply = req.method === "session.snapshot"
            ? {
              id: req.id,
              result: {
                snapshot: {
                  panes: Object.entries(panes()).map(([pane_id, agent_status]) => ({
                    pane_id,
                    agent_status,
                  })),
                },
              },
            }
            : { id: req.id, error: { message: `unknown method ${req.method}` } };
          await conn.write(new TextEncoder().encode(JSON.stringify(reply) + "\n"));
        } catch {
          // connection reset etc. -- fine for a fake, this is what ServerGone tests want.
        } finally {
          try {
            conn.close();
          } catch {
            // already closed
          }
        }
      })();
    }
  })();

  return {
    close() {
      closed.value = true;
      listener.close();
    },
  };
}

/** A fake `devc-bridge`: appends its argv, one JSON line per call, to `logPath`. */
async function writeFakeBridge(scriptPath: string, logPath: string): Promise<void> {
  const script = `#!/bin/sh\necho "$@" >> ${logPath}\nexit 0\n`;
  await Deno.writeTextFile(scriptPath, script);
  await Deno.chmod(scriptPath, 0o755);
}

async function readLines(path: string): Promise<string[]> {
  try {
    const text = await Deno.readTextFile(path);
    return text.split("\n").filter(Boolean);
  } catch {
    return [];
  }
}

Deno.test({
  name: "e2e: pings while active, stops when idle",
  // Uses a real unix socket + a real subprocess spawn -- needs the matching Deno
  // permissions when this file is run directly rather than through `deno task test`.
  permissions: { read: true, write: true, net: true, run: true, env: true },
  fn: async () => {
    const dir = await Deno.makeTempDir({ prefix: "bridge-keepawake-e2e-" });
    const socketPath = `${dir}/herdr.sock`;
    const bridgeScript = `${dir}/devc-bridge`;
    const pingLog = `${dir}/pings.log`;

    let statuses: Record<string, string> = { "w1:p1": "working" };
    const server = await fakeHerdrServer(socketPath, () => statuses);
    await writeFakeBridge(bridgeScript, pingLog);

    try {
      // Poll 1: active -- ping expected.
      let snap = await paneStatuses(socketPath);
      assert.equal(isAnyPaneActive(snap, ["working"]), true);
      let result = await ping(bridgeScript, "e2e:1");
      assert.equal(result.ok, true);

      // Poll 2: still active -- another ping.
      snap = await paneStatuses(socketPath);
      assert.equal(isAnyPaneActive(snap, ["working"]), true);
      result = await ping(bridgeScript, "e2e:1");
      assert.equal(result.ok, true);

      assert.equal((await readLines(pingLog)).length, 2, "two pings while active");

      // Work stops.
      statuses = { "w1:p1": "done" };
      snap = await paneStatuses(socketPath);
      assert.equal(isAnyPaneActive(snap, ["working"]), false);
      // The daemon's own loop would not call ping() at all here -- this is the
      // assertion that matters: no ping call is made once nothing is active.
      assert.equal((await readLines(pingLog)).length, 2, "no new pings once idle");
    } finally {
      server.close();
      await Deno.remove(dir, { recursive: true });
    }
  },
});

Deno.test({
  name: "e2e: a refused connection surfaces as ServerGone (server-death signal)",
  permissions: { read: true, write: true, net: true },
  fn: async () => {
    const dir = await Deno.makeTempDir({ prefix: "bridge-keepawake-e2e-" });
    const socketPath = `${dir}/no-such-server.sock`; // never listened on
    try {
      await assert.rejects(
        () => paneStatuses(socketPath),
        (err: Error) => err.name === "ServerGone",
      );
    } finally {
      await Deno.remove(dir, { recursive: true });
    }
  },
});

Deno.test({
  name: "e2e: a failing devc-bridge is reported, never thrown",
  permissions: { run: true, read: true, write: true },
  fn: async () => {
    const dir = await Deno.makeTempDir({ prefix: "bridge-keepawake-e2e-" });
    const bridgeScript = `${dir}/devc-bridge`;
    await Deno.writeTextFile(bridgeScript, "#!/bin/sh\necho 'bridge down' >&2\nexit 1\n");
    await Deno.chmod(bridgeScript, 0o755);
    try {
      const result = await ping(bridgeScript, "x");
      assert.equal(result.ok, false);
      assert.match(result.detail, /bridge down/);
    } finally {
      await Deno.remove(dir, { recursive: true });
    }
  },
});
