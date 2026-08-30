/**
 * The one thing this plugin does to the outside world: `devc-bridge ping <label>`.
 *
 * A ping failure is never fatal -- a down or unreachable bridge must not kill the
 * daemon or spam the log, the same `|| true` discipline the `devc-bridge` README
 * prescribes for hooks. `ping`'s argument is a free-form diagnostic label and never
 * affects host behaviour.
 */

export interface PingResult {
  ok: boolean;
  detail: string;
}

export async function ping(bridgeCommand: string, label: string): Promise<PingResult> {
  try {
    const { success, stderr } = await new Deno.Command(bridgeCommand, {
      args: ["ping", label],
      stdin: "null",
      stdout: "null",
      stderr: "piped",
    }).output();
    if (success) return { ok: true, detail: "" };
    return { ok: false, detail: new TextDecoder().decode(stderr).trim() || `exit != 0` };
  } catch (err) {
    return { ok: false, detail: (err as Error).message };
  }
}

/** `devc-bridge --version` (or `version`) -- used only by `doctor`, never by the loop. */
export async function bridgeVersion(bridgeCommand: string): Promise<string | undefined> {
  try {
    const { success, stdout } = await new Deno.Command(bridgeCommand, {
      args: ["--version"],
      stdin: "null",
      stdout: "piped",
      stderr: "null",
    }).output();
    if (!success) return undefined;
    return new TextDecoder().decode(stdout).trim();
  } catch {
    return undefined;
  }
}
