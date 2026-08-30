/**
 * Plugin configuration: `$HERDR_PLUGIN_CONFIG_DIR/config.json`, entirely optional --
 * same convention as agent-caffeinate. There is deliberately no `idleGraceSec` and no
 * `inhibitorCommand`: this plugin holds no state and starts no inhibitor of its own.
 * Their absence is the design (see the plan this plugin was built from).
 */

import { parseJsonc } from "./jsonc.ts";

export const PLUGIN_ID = "bridge-keepawake";

export const KNOWN_KEYS = [
  "pollIntervalSec",
  "activeStatuses",
  "bridgeCommand",
  "pingLabel",
  "logLevel",
] as const;

export const LOG_LEVELS = ["error", "warn", "info", "debug"] as const;
export type LogLevel = typeof LOG_LEVELS[number];

export class ConfigError extends Error {
  override readonly name = "ConfigError";
}

export interface Config {
  pollIntervalSec: number;
  activeStatuses: string[];
  bridgeCommand: string;
  pingLabel: string;
  logLevel: LogLevel;
  sourcePath: string;
  source: "defaults" | "file" | "defaults+env";
  warnings: string[];
}

// Env overrides exist for the test suite, which drives the daemon against a fake
// socket server and a fake `devc-bridge` stub; they are not a documented user
// interface.
const ENV_POLL = "HERDR_KEEPAWAKE_POLL_INTERVAL_SEC";
const ENV_ACTIVE = "HERDR_KEEPAWAKE_ACTIVE_STATUSES";
const ENV_BRIDGE_COMMAND = "HERDR_KEEPAWAKE_BRIDGE_COMMAND";
const ENV_LOG_LEVEL = "HERDR_KEEPAWAKE_LOG_LEVEL";

// Herdr resolves both of these through the XDG base directories -- same fallback rule
// agent-caffeinate's `src/config.py` documents, verified there against
// `herdr plugin config-dir` and against the state directory Herdr creates at install.
export function configDir(env: Record<string, string | undefined> = Deno.env.toObject()): string {
  if (env.HERDR_PLUGIN_CONFIG_DIR) return env.HERDR_PLUGIN_CONFIG_DIR;
  const base = isAbsolute(env.XDG_CONFIG_HOME) ? env.XDG_CONFIG_HOME! : `${homeDir(env)}/.config`;
  return `${base}/herdr/plugins/config/${PLUGIN_ID}`;
}

export function configPath(env: Record<string, string | undefined> = Deno.env.toObject()): string {
  return `${configDir(env)}/config.json`;
}

export function stateDir(env: Record<string, string | undefined> = Deno.env.toObject()): string {
  if (env.HERDR_PLUGIN_STATE_DIR) return env.HERDR_PLUGIN_STATE_DIR;
  const base = isAbsolute(env.XDG_STATE_HOME) ? env.XDG_STATE_HOME! : `${homeDir(env)}/.local/state`;
  return `${base}/herdr/plugins/${PLUGIN_ID}`;
}

function isAbsolute(p: string | undefined): p is string {
  return !!p && p.startsWith("/");
}

function homeDir(env: Record<string, string | undefined>): string {
  return env.HOME ?? "/root";
}

function defaults(): Config {
  return {
    pollIntervalSec: 2,
    activeStatuses: ["working"],
    bridgeCommand: "devc-bridge",
    pingLabel: "bridge-keepawake",
    logLevel: "info",
    sourcePath: "",
    source: "defaults",
    warnings: [],
  };
}

function positive(raw: unknown, key: string, where: string): number {
  const value = typeof raw === "number" ? raw : Number(raw);
  if (!Number.isFinite(value)) {
    throw new ConfigError(`'${key}' must be a number, got ${JSON.stringify(raw)}\n  ${where}`);
  }
  if (value <= 0) {
    throw new ConfigError(`'${key}' must be greater than 0, got ${JSON.stringify(raw)}\n  ${where}`);
  }
  return value;
}

/** Read, validate and resolve the config. Throws {@link ConfigError}. */
export async function load(env: Record<string, string | undefined> = Deno.env.toObject()): Promise<Config> {
  const cfg = defaults();
  cfg.sourcePath = configPath(env);
  const where = `${cfg.sourcePath}\n  (print it with: herdr plugin config-dir ${PLUGIN_ID})`;

  let raw: Record<string, unknown> = {};
  try {
    const text = await Deno.readTextFile(cfg.sourcePath);
    cfg.source = "file";
    let parsed: unknown;
    try {
      parsed = parseJsonc(text);
    } catch (err) {
      throw new ConfigError(`config file is not valid JSON: ${(err as Error).message}\n  ${where}`);
    }
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new ConfigError(`config file must contain a JSON object\n  ${where}`);
    }
    raw = parsed as Record<string, unknown>;
  } catch (err) {
    if (err instanceof ConfigError) throw err;
    if (!(err instanceof Deno.errors.NotFound)) {
      throw new ConfigError(`cannot read config file: ${(err as Error).message}\n  ${where}`);
    }
    // No config file -- defaults stand.
  }

  for (const key of Object.keys(raw)) {
    if (!(KNOWN_KEYS as readonly string[]).includes(key)) {
      cfg.warnings.push(`unknown config key '${key}' (ignored)`);
    }
  }

  if ("pollIntervalSec" in raw) {
    cfg.pollIntervalSec = positive(raw.pollIntervalSec, "pollIntervalSec", where);
  }

  if ("activeStatuses" in raw) {
    const statuses = raw.activeStatuses;
    if (!Array.isArray(statuses) || statuses.length === 0) {
      throw new ConfigError(`"activeStatuses" must be a non-empty array of strings\n  ${where}`);
    }
    const cleaned = statuses.filter((s): s is string => typeof s === "string");
    if (cleaned.length === 0) {
      throw new ConfigError(`"activeStatuses" held no strings\n  ${where}`);
    }
    if (cleaned.length !== statuses.length) {
      cfg.warnings.push("activeStatuses: ignoring non-string entries");
    }
    cfg.activeStatuses = cleaned;
  }

  if (raw.bridgeCommand !== undefined && raw.bridgeCommand !== null) {
    if (typeof raw.bridgeCommand !== "string" || raw.bridgeCommand === "") {
      throw new ConfigError(`"bridgeCommand" must be a non-empty string\n  ${where}`);
    }
    cfg.bridgeCommand = raw.bridgeCommand;
  }

  if (raw.pingLabel !== undefined && raw.pingLabel !== null) {
    if (typeof raw.pingLabel !== "string" || raw.pingLabel === "") {
      throw new ConfigError(`"pingLabel" must be a non-empty string\n  ${where}`);
    }
    cfg.pingLabel = raw.pingLabel;
  }

  if ("logLevel" in raw) {
    if (!(LOG_LEVELS as readonly string[]).includes(raw.logLevel as string)) {
      throw new ConfigError(
        `"logLevel" must be one of ${LOG_LEVELS.join(" | ")}, got ${JSON.stringify(raw.logLevel)}\n  ${where}`,
      );
    }
    cfg.logLevel = raw.logLevel as LogLevel;
  }

  applyEnv(cfg, env);
  return cfg;
}

function applyEnv(cfg: Config, env: Record<string, string | undefined>): void {
  let touched = false;
  if (env[ENV_POLL]) {
    cfg.pollIntervalSec = positive(env[ENV_POLL], ENV_POLL, "(env)");
    touched = true;
  }
  if (env[ENV_LOG_LEVEL]) {
    if (!(LOG_LEVELS as readonly string[]).includes(env[ENV_LOG_LEVEL]!)) {
      throw new ConfigError(`${ENV_LOG_LEVEL} must be one of ${LOG_LEVELS.join(" | ")}`);
    }
    cfg.logLevel = env[ENV_LOG_LEVEL] as LogLevel;
    touched = true;
  }
  if (env[ENV_ACTIVE]) {
    cfg.activeStatuses = env[ENV_ACTIVE]!.split(",").filter(Boolean);
    touched = true;
  }
  if (env[ENV_BRIDGE_COMMAND]) {
    cfg.bridgeCommand = env[ENV_BRIDGE_COMMAND]!;
    touched = true;
  }
  if (cfg.source === "defaults" && touched) {
    cfg.source = "defaults+env";
  }
}
