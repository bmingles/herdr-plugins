/**
 * The one decision this plugin makes, kept pure and separate so it is testable with
 * no socket and no server: does the current snapshot mean "ping the bridge"?
 *
 * Unlike agent-caffeinate there is no state machine here -- no grace period, no
 * held/idle transition. The host (`devc-bridge`'s idle timeout) owns release; this
 * plugin only ever answers the instantaneous question "is anyone working right now".
 */

import type { PaneStatuses } from "./sock.ts";

/**
 * True if any pane's status is in `activeStatuses`.
 *
 * `blocked` is deliberately excluded from the default set: it means the agent is
 * waiting on a human, so no work is in flight to protect. A socket-only reader sees
 * `done`, essentially never `idle` (herdr-daemon-facts.md § C4) -- fixtures used to
 * test this should reflect that rather than testing a state that does not occur.
 */
export function isAnyPaneActive(
  statuses: PaneStatuses,
  activeStatuses: readonly string[],
): boolean {
  const active = new Set(activeStatuses);
  return Object.values(statuses).some((status) => active.has(status));
}

/** The subset of pane ids currently counted as active, for logging/diagnostics. */
export function activePaneIds(
  statuses: PaneStatuses,
  activeStatuses: readonly string[],
): string[] {
  const active = new Set(activeStatuses);
  return Object.entries(statuses)
    .filter(([, status]) => active.has(status))
    .map(([paneId]) => paneId)
    .sort();
}
