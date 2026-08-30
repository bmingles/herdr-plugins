/**
 * One-shot requests to the Herdr socket. The Deno/TypeScript counterpart of
 * agent-caffeinate's `src/sock.py` -- same protocol, same shape, reimplemented
 * rather than shared because the two plugins do not depend on each other (see
 * README -> "Not depend on agent-caffeinate").
 *
 * Deliberately *not* a subscription client. Two facts from
 * `docs/herdr-daemon-facts.md` shaped this:
 *
 * - The server closes the connection after answering any non-subscribe request, so a
 *   connection cannot be held and reused for polling -- a fresh connection per poll is
 *   required.
 * - `pane.agent_status_changed` subscriptions require a concrete, existing `pane_id` --
 *   `*` and `""` are both rejected -- so there is no session-wide agent-status stream to
 *   subscribe to in the first place.
 *
 * A `session.snapshot` over a fresh connection measured 0.35ms, ~3x cheaper than
 * spawning the `herdr` CLI. Polling is both simpler and more robust than maintaining
 * per-pane subscriptions.
 *
 * `ServerGone` is the important signal: a plugin-spawned daemon *outlives its server*
 * (measured twice, herdr-daemon-facts.md § A1b), so a failed connect is how the daemon
 * learns to release and exit.
 */

export class ServerGone extends Error {
  override readonly name = "ServerGone";
}

export class ProtocolError extends Error {
  override readonly name = "ProtocolError";
}

export function defaultSocketPath(
  env: Record<string, string | undefined> = Deno.env.toObject(),
): string | undefined {
  return env.HERDR_SOCKET_PATH;
}

/**
 * Send one newline-delimited JSON request, return the parsed reply.
 *
 * Requires `--allow-net=unix:<socketPath>` **and** `--allow-read=<socketPath>` **and**
 * `--allow-write=<socketPath>` -- Deno's `Deno.connect()` over a unix-domain socket
 * checks all three, not just net. See README -> "Permissions, corrected" for why this
 * differs from the plan this plugin was built from.
 */
export async function request(
  socketPath: string | undefined,
  method: string,
  params: Record<string, unknown> = {},
  timeoutMs = 5000,
): Promise<unknown> {
  if (!socketPath) {
    throw new ServerGone("no socket path (HERDR_SOCKET_PATH unset)");
  }

  let conn: Deno.UnixConn;
  try {
    conn = await Deno.connect({ path: socketPath, transport: "unix" });
  } catch (err) {
    if (
      err instanceof Deno.errors.NotFound ||
      err instanceof Deno.errors.ConnectionRefused
    ) {
      throw new ServerGone(`${socketPath}: ${(err as Error).message}`);
    }
    throw err;
  }

  try {
    const payload = JSON.stringify({ id: "bridge-keepawake", method, params }) + "\n";
    const timeout = AbortSignal.timeout(timeoutMs);

    try {
      await conn.write(new TextEncoder().encode(payload));
    } catch (err) {
      throw new ServerGone(`send failed: ${(err as Error).message}`);
    }

    let buf = "";
    const reader = conn.readable.getReader();
    try {
      while (!buf.includes("\n")) {
        if (timeout.aborted) {
          throw new ServerGone(`timed out awaiting a reply to ${method}`);
        }
        const { value, done } = await Promise.race([
          reader.read(),
          new Promise<never>((_, reject) => {
            timeout.addEventListener("abort", () =>
              reject(new ServerGone(`timed out awaiting a reply to ${method}`)));
          }),
        ]);
        if (done) {
          throw new ServerGone(`server closed before replying to ${method}`);
        }
        buf += new TextDecoder().decode(value);
      }
    } finally {
      reader.releaseLock();
    }

    const line = buf.split("\n", 1)[0];
    try {
      return JSON.parse(line);
    } catch (err) {
      throw new ProtocolError(`unparseable reply to ${method}: ${(err as Error).message}`);
    }
  } finally {
    try {
      conn.close();
    } catch {
      // already closed -- fine.
    }
  }
}

export interface PaneStatuses {
  [paneId: string]: string;
}

/**
 * `{pane_id: agent_status}` for every pane in the session.
 *
 * Panes with no agent report `"unknown"`, which is simply never in `activeStatuses`.
 * A socket-only reader sees `done`, essentially never `idle`
 * (herdr-daemon-facts.md § C4) -- callers and fixtures should reflect that.
 */
export async function paneStatuses(
  socketPath: string | undefined,
  timeoutMs = 5000,
): Promise<PaneStatuses> {
  const reply = await request(socketPath, "session.snapshot", {}, timeoutMs) as Record<
    string,
    unknown
  >;
  if (reply.error) {
    throw new ProtocolError(`session.snapshot failed: ${JSON.stringify(reply.error)}`);
  }
  const result = reply.result as Record<string, unknown> | undefined;
  const snapshot = result?.snapshot as Record<string, unknown> | undefined;
  const panes = snapshot?.panes;
  if (!Array.isArray(panes)) {
    throw new ProtocolError("session.snapshot reply has no result.snapshot.panes");
  }
  const out: PaneStatuses = {};
  for (const pane of panes) {
    const paneId = (pane as Record<string, unknown>)?.pane_id;
    if (typeof paneId === "string") {
      out[paneId] = ((pane as Record<string, unknown>).agent_status as string) ??
        "unknown";
    }
  }
  return out;
}
