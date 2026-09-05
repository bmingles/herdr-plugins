#!/usr/bin/env -S deno run
/**
 * bridge-keepawake -- ping devc-bridge while coding agents are working, so the host
 * (which owns the inhibitor and the idle timeout) keeps the Mac awake.
 *
 * Entrypoint and daemon loop. The loop is a poll, not a subscription, for the same
 * reasons agent-caffeinate's -- see src/sock.ts. Unlike agent-caffeinate there is no
 * grace period and no held state: the host releases on its own idle timeout, this
 * daemon only ever answers "is anyone working right now".
 *
 * **Singleton locking is not this module's job.** `bin/bridge-keepawake` (the shell
 * launcher) wraps the real process in `flock -n` before it ever reaches here -- see
 * `src/daemonize.ts`'s module comment for why. By the time `daemon` runs in this file,
 * it can assume it is already the sole holder for this Herdr session.
 */

import * as configMod from "./config.ts";
import * as daemonize from "./daemonize.ts";
import * as sock from "./sock.ts";
import * as bridgeMod from "./bridge.ts";
import { activePaneIds, isAnyPaneActive } from "./is-active.ts";

const EXIT_OK = 0;
const EXIT_CONFIG = 1;
const EXIT_SOCKET = 2;

export interface Paths {
  root: string;
  sessionDir: string;
  launcher: string;
  pid: string;
  log: string;
  status: string;
}

export async function resolvePaths(stateDir: string, socketPath: string | undefined): Promise<Paths> {
  const sessionDir = await daemonize.sessionDir(stateDir, socketPath);
  return {
    root: stateDir,
    sessionDir,
    launcher: `${stateDir}/bridge-keepawake`,
    pid: `${sessionDir}/daemon.pid`,
    log: `${sessionDir}/daemon.log`,
    status: `${sessionDir}/daemon.json`,
  };
}

async function resolve(env: Record<string, string | undefined> = Deno.env.toObject()) {
  const cfg = await configMod.load(env);
  const socketPath = sock.defaultSocketPath(env);
  const paths = await resolvePaths(configMod.stateDir(env), socketPath);
  return { cfg, socketPath, paths };
}

export interface StatusPayload {
  pid: number;
  startedAt: number;
  socketPath: string | undefined;
  sessionKey: string;
  activePanes: string[];
  statuses: Record<string, string>;
  lastPingOk: boolean | null;
  updatedAt: number;
  /** Epoch seconds of the last poll where `activePanes` was non-empty, carried
   * forward unchanged on every other poll. Absent/non-numeric means "never active".
   * Exists only for `indicator`'s flicker-absorbing hold -- see its module comment. */
  lastActiveAt: number | undefined;
}

export async function writeStatus(paths: Paths, payload: StatusPayload): Promise<void> {
  const tmp = `${paths.status}.tmp`;
  try {
    await Deno.writeTextFile(tmp, JSON.stringify(payload));
    await Deno.rename(tmp, paths.status);
  } catch {
    // best effort
  }
}

async function readStatus(paths: Paths): Promise<StatusPayload | undefined> {
  try {
    return JSON.parse(await Deno.readTextFile(paths.status));
  } catch {
    return undefined;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function sleepInterruptible(seconds: number, stopFlag: { stop: boolean }): Promise<void> {
  const deadline = Date.now() + seconds * 1000;
  while (!stopFlag.stop && Date.now() < deadline) {
    await sleep(Math.min(100, Math.max(0, deadline - Date.now())));
  }
}

async function runLoop(
  cfg: configMod.Config,
  socketPath: string | undefined,
  paths: Paths,
  log: daemonize.Log,
  stopFlag: { stop: boolean },
): Promise<number> {
  const startedAt = Date.now() / 1000;
  await log.info(
    `daemon start pid=${Deno.pid} socket=${socketPath} poll=${cfg.pollIntervalSec}s ` +
      `active=${cfg.activeStatuses.join(",")}`,
  );

  let lastPingFailed = false;
  // Carried forward across polls (and, by seeding from any status file already on
  // disk, across a restart shortly after one) -- see `StatusPayload.lastActiveAt`.
  let lastActiveAt: number | undefined = (await readStatus(paths))?.lastActiveAt;
  try {
    while (!stopFlag.stop) {
      let statuses: sock.PaneStatuses;
      try {
        statuses = await sock.paneStatuses(socketPath);
      } catch (err) {
        if (err instanceof sock.ServerGone) {
          await log.info(`server gone (${(err as Error).message}); releasing and exiting`);
          break;
        }
        if (err instanceof sock.ProtocolError) {
          await log.warn(`snapshot unusable: ${(err as Error).message}`);
          await sleepInterruptible(cfg.pollIntervalSec, stopFlag);
          continue;
        }
        throw err;
      }

      const active = isAnyPaneActive(statuses, cfg.activeStatuses);
      let lastPingOk: boolean | null = null;
      if (active) {
        lastActiveAt = Date.now() / 1000;
        const panes = activePaneIds(statuses, cfg.activeStatuses);
        const result = await bridgeMod.ping(cfg.bridgeCommand, `${cfg.pingLabel}:${panes.length}`);
        lastPingOk = result.ok;
        if (!result.ok) {
          // Log at most once per transition into a failing state -- a down bridge
          // must not spam the log every poll interval.
          if (!lastPingFailed) await log.warn(`ping failed: ${result.detail}`);
          lastPingFailed = true;
        } else {
          if (lastPingFailed) await log.info("ping recovered");
          lastPingFailed = false;
        }
      }

      await writeStatus(paths, {
        pid: Deno.pid,
        startedAt,
        socketPath,
        sessionKey: daemonize.sessionKey(socketPath),
        activePanes: active ? activePaneIds(statuses, cfg.activeStatuses) : [],
        statuses,
        lastPingOk,
        updatedAt: Date.now() / 1000,
        lastActiveAt,
      });

      await sleepInterruptible(cfg.pollIntervalSec, stopFlag);
    }
  } finally {
    await writeStatus(paths, {
      pid: Deno.pid,
      startedAt,
      socketPath,
      sessionKey: daemonize.sessionKey(socketPath),
      activePanes: [],
      statuses: {},
      lastPingOk: null,
      updatedAt: Date.now() / 1000,
      lastActiveAt,
    });
    await log.info(`daemon exit pid=${Deno.pid}`);
  }
  return EXIT_OK;
}

/**
 * Run the daemon body. By construction, the only caller is `bin/bridge-keepawake`,
 * already running under `flock -n` -- see the module comment and
 * `src/daemonize.ts`'s. This function does not acquire or check any lock itself.
 */
async function cmdDaemon(args: { foreground: boolean }): Promise<number> {
  let cfg: configMod.Config, socketPath: string | undefined, paths: Paths;
  try {
    ({ cfg, socketPath, paths } = await resolve());
  } catch (err) {
    if (err instanceof configMod.ConfigError) {
      console.error(`bridge-keepawake: ${err.message}`);
      return EXIT_CONFIG;
    }
    throw err;
  }

  if (!socketPath) {
    console.error("bridge-keepawake: HERDR_SOCKET_PATH is unset; not a Herdr plugin environment");
    return EXIT_SOCKET;
  }

  const entrypoint = `${new URL("..", import.meta.url).pathname}bin/bridge-keepawake`;
  await daemonize.writeLauncher(paths.launcher, entrypoint);
  await Deno.writeTextFile(paths.pid, `${Deno.pid}\n`);

  const log = new daemonize.Log(paths.log, cfg.logLevel, args.foreground);
  for (const warning of cfg.warnings) await log.warn(warning);

  const stopFlag = { stop: false };
  for (const sig of ["SIGTERM", "SIGINT"] as const) {
    Deno.addSignalListener(sig, () => {
      stopFlag.stop = true;
    });
  }

  try {
    return await runLoop(cfg, socketPath, paths, log, stopFlag);
  } finally {
    try {
      await Deno.remove(paths.pid);
    } catch {
      // best effort
    }
  }
}

async function cmdStop(): Promise<number> {
  const { paths } = await resolve();
  const pid = await daemonize.readHolderPid(paths.pid);
  if (!pid || !(await daemonize.pidAlive(pid))) {
    console.log("daemon: not running");
    return EXIT_OK;
  }
  await daemonize.signalPid(pid, "TERM");
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    if (!(await daemonize.pidAlive(pid))) {
      console.log("stopped");
      return EXIT_OK;
    }
    await sleep(50);
  }
  console.error(`bridge-keepawake: pid ${pid} did not exit within 10s`);
  return EXIT_OK;
}

async function cmdStatus(json: boolean): Promise<number> {
  const { socketPath, paths } = await resolve();
  const pid = await daemonize.readHolderPid(paths.pid);
  const alive = await daemonize.pidAlive(pid);
  const status = alive ? await readStatus(paths) : undefined;

  if (json) {
    console.log(JSON.stringify({
      running: alive,
      pid: alive ? pid : null,
      socketPath,
      sessionKey: daemonize.sessionKey(socketPath),
      state: status ?? null,
    }));
    return EXIT_OK;
  }

  if (!alive) {
    console.log("daemon: not running");
    console.log(`  session: ${daemonize.sessionKey(socketPath)}`);
    console.log(`  log:     ${paths.log}`);
    return EXIT_OK;
  }

  console.log(`daemon: running (pid ${pid})`);
  if (status) {
    const up = Date.now() / 1000 - status.startedAt;
    console.log(`  uptime:      ${Math.floor(up / 60)}m ${Math.floor(up % 60)}s`);
    console.log(`  last ping:   ${status.lastPingOk === null ? "n/a (idle)" : status.lastPingOk ? "ok" : "FAILING"}`);
    console.log(`  active:      ${status.activePanes.length ? status.activePanes.join(", ") : "none"}`);
    for (const pane of Object.keys(status.statuses).sort()) {
      console.log(`    ${pane.padEnd(10)} ${status.statuses[pane]}`);
    }
  }
  console.log(`  log:         ${paths.log}`);
  return EXIT_OK;
}

async function cmdDoctor(): Promise<number> {
  let cfg: configMod.Config, socketPath: string | undefined, paths: Paths;
  try {
    ({ cfg, socketPath, paths } = await resolve());
  } catch (err) {
    if (err instanceof configMod.ConfigError) {
      console.error(`bridge-keepawake: ${err.message}`);
      return EXIT_CONFIG;
    }
    throw err;
  }

  console.log("bridge-keepawake doctor");
  console.log(`  config source     : ${cfg.source}`);
  console.log(`  config path       : ${cfg.sourcePath}`);
  console.log(`  pollIntervalSec   : ${cfg.pollIntervalSec}`);
  console.log(`  activeStatuses    : ${cfg.activeStatuses.join(", ")}`);
  console.log(`  bridgeCommand     : ${cfg.bridgeCommand}`);
  console.log(`  pingLabel         : ${cfg.pingLabel}`);
  console.log(`  logLevel          : ${cfg.logLevel}`);
  console.log(`  socket path       : ${socketPath ?? "<unset>"}`);
  console.log(`  session key       : ${daemonize.sessionKey(socketPath)}`);
  console.log(`  log               : ${paths.log}`);
  console.log(`  deno resolves as  : ${Deno.execPath()}`);

  const pid = await daemonize.readHolderPid(paths.pid);
  if (pid && (await daemonize.pidAlive(pid))) {
    console.log(`  daemon            : running (pid ${pid})`);
  } else {
    console.log("  daemon            : not running");
  }

  if (socketPath) {
    try {
      const statuses = await sock.paneStatuses(socketPath);
      console.log(`  server            : reachable, ${Object.keys(statuses).length} pane(s)`);
      for (const pane of Object.keys(statuses).sort()) {
        console.log(`      ${pane.padEnd(10)} ${statuses[pane]}`);
      }
    } catch (err) {
      console.log(`  server            : UNREACHABLE (${(err as Error).message})`);
    }
  }

  const version = await bridgeMod.bridgeVersion(cfg.bridgeCommand);
  console.log(`  devc-bridge       : ${version ?? "NOT FOUND on --allow-run allowlist / PATH"}`);
  if (version) {
    const ping = await bridgeMod.ping(cfg.bridgeCommand, "doctor");
    console.log(`  devc-bridge ping  : ${ping.ok ? "ok" : `FAILED (${ping.detail})`}`);
  }

  for (const warning of cfg.warnings) console.log(`  warning           : ${warning}`);
  return EXIT_OK;
}

// -- status indicator ---------------------------------------------------------------
//
// For a `ui.tab_bar_right` entry in the user's `config.toml`. Herdr re-runs that command
// on the server every `interval_seconds` and renders its last line, so this path must be
// cheap and must never block: it reads `daemon.json` and the pid file only. **No socket
// call, no `devc-bridge` subprocess, no `Deno.Command` of any kind.** Herdr also clears
// the entry on empty output, which is exactly the rendering wanted for "nothing to say"
// -- so silence is the default and every branch below that has nothing to report
// returns "".
//
// This answers "is the plugin pinging right now", not "is the Mac awake" -- see the
// plan's "Why, and what it is honest about". The host's own idle timeout can lag this
// by up to `DEVC_BRIDGE_KEEPAWAKE_IDLE_MS`; that gap is real and this indicator does
// not paper over it.

export const ICON_HOLDING = "☕"; // coffee cup
export const ICON_IDLE = "○"; // hollow circle
export const ICON_FAULT = "⚠"; // warning sign

export type IndicatorState = "absent" | "wedged" | "fault" | "holding" | "idle";

export interface IndicatorInfo {
  state: IndicatorState;
  activePanes: string[];
  lastPingOk: boolean | null;
}

/**
 * What the indicator should report, read from the daemon's own state files.
 *
 * `holding` covers both "pinging right now" and "last active within
 * `indicatorHoldSec`" -- the anti-flicker hold from the plan's "Anti-flicker" section.
 * It adds no state to the daemon loop: `lastActiveAt` is written every poll regardless
 * of whether anything reads it, and this function is the only reader.
 */
export async function indicatorState(paths: Paths, cfg: configMod.Config): Promise<IndicatorInfo> {
  const absent: IndicatorInfo = { state: "absent", activePanes: [], lastPingOk: null };

  const pid = await daemonize.readHolderPid(paths.pid);
  if (!pid || !(await daemonize.pidAlive(pid))) return absent;

  const status = await readStatus(paths);
  if (!status || typeof status.updatedAt !== "number") {
    // The pid is alive but nothing has been written yet -- a daemon one poll into its
    // life looks exactly like this, so it is not a fault worth drawing.
    return absent;
  }

  const now = Date.now() / 1000;
  const age = Math.max(0, now - status.updatedAt);
  const activePanes = status.activePanes ?? [];
  const lastPingOk = status.lastPingOk ?? null;

  if (age >= daemonize.staleAfterSec(cfg.pollIntervalSec)) {
    // Same rule `doctor` could apply -- see src/daemonize.ts's staleAfterSec. A wedged
    // daemon's last-written fields are whatever they were when it stopped making
    // progress, so they are not worth rendering as current fact.
    return { state: "wedged", activePanes, lastPingOk };
  }

  if (lastPingOk === false) {
    // The one place this indicator says something agent-caffeinate's cannot: a
    // reachable daemon whose pings are being refused means the bridge or the host is
    // down, exactly when a user asks why their Mac slept.
    return { state: "fault", activePanes, lastPingOk };
  }

  const lastActiveAt = typeof status.lastActiveAt === "number" ? status.lastActiveAt : undefined;
  const heldFor = lastActiveAt !== undefined ? now - lastActiveAt : undefined;
  const activeNow = activePanes.length > 0;
  if (activeNow || (heldFor !== undefined && heldFor <= cfg.indicatorHoldSec)) {
    return { state: "holding", activePanes, lastPingOk };
  }

  return { state: "idle", activePanes, lastPingOk };
}

/** One line for the tab bar, or "" meaning render nothing. */
export function indicatorText(info: IndicatorInfo, label: string, icon: string, showIdle: boolean): string {
  if (info.state === "wedged" || info.state === "fault") {
    return `${ICON_FAULT} ${label}`.trim();
  }
  if (info.state === "holding") {
    return `${icon} ${label}`.trim();
  }
  if (info.state === "idle" && showIdle) {
    return `${ICON_IDLE} ${label}`.trim();
  }
  return "";
}

async function cmdIndicator(args: { label: string; icon: string; showIdle: boolean }): Promise<number> {
  const env = Deno.env.toObject();
  let cfg: configMod.Config;
  try {
    cfg = await configMod.load(env);
  } catch {
    // A broken config file is `doctor`'s to report, not the tab bar's. Only the
    // staleness and hold bounds depend on it, so fall back to defaults rather than go
    // blind about a daemon that may well be running fine on an older, valid config.
    cfg = configMod.defaults();
  }

  const socketPath = sock.defaultSocketPath(env);
  const paths = await resolvePaths(configMod.stateDir(env), socketPath);
  const info = await indicatorState(paths, cfg);
  const text = indicatorText(info, args.label, args.icon, args.showIdle);
  if (text) console.log(text);
  return EXIT_OK;
}

function parseArgs(argv: string[]) {
  const [command, ...rest] = argv;
  const flags = new Set(rest);
  return { command, rest, flags };
}

function parseIndicatorArgs(rest: string[]): { label: string; icon: string; showIdle: boolean } {
  let label = "keepawake";
  let icon = ICON_HOLDING;
  let showIdle = false;
  for (let i = 0; i < rest.length; i++) {
    const arg = rest[i];
    if (arg === "--label") {
      label = rest[++i] ?? label;
    } else if (arg === "--icon") {
      icon = rest[++i] ?? icon;
    } else if (arg === "--show-idle") {
      showIdle = true;
    }
  }
  return { label, icon, showIdle };
}

async function main(): Promise<number> {
  const { command, rest, flags } = parseArgs(Deno.args);
  switch (command) {
    case "daemon":
      return await cmdDaemon({ foreground: flags.has("--foreground") });
    case "stop":
      return await cmdStop();
    case "status":
      return await cmdStatus(flags.has("--json"));
    case "doctor":
      return await cmdDoctor();
    case "indicator":
      return await cmdIndicator(parseIndicatorArgs(rest));
    default:
      console.log(
        "usage: bridge-keepawake <daemon [--foreground]|stop|status [--json]|doctor|" +
          "indicator [--label TEXT] [--icon GLYPH] [--show-idle]>",
      );
      return EXIT_OK;
  }
}

if (import.meta.main) {
  Deno.exit(await main());
}
