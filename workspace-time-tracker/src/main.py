#!/usr/bin/env python3
"""workspace-time-tracker -- how long you actually spend in each Herdr Space.

Poll-driven for the same reasons as `agent-caffeinate` (see
`docs/herdr-daemon-facts.md`): there is no session-wide agent-status stream to subscribe
to, the server closes a connection after every non-subscribe request, and a whole
snapshot costs 0.35 ms. Two cadences share one loop -- the snapshot (focus and agent
status) every `snapshotIntervalSec`, and the focused pane's screen hash every
`pollIntervalSec`, since that read is the more expensive of the two and needs less
resolution.
"""

import argparse
import json
import os
import signal
import socket as socket_mod
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import activity                       # noqa: E402
import config as config_mod           # noqa: E402
import daemonize                      # noqa: E402
import report as report_mod           # noqa: E402
import segments as seg_mod            # noqa: E402
import sock                           # noqa: E402
import store                          # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_SOCKET = 2
EXIT_USAGE = 64

# This file is `<plugin root>/src/main.py`, so the shim two levels up is what a generated
# launcher must exec. Derived from `__file__` rather than from `$HERDR_PLUGIN_ROOT`,
# which is unset when the user runs the command by hand.
ENTRYPOINT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "track")


class Paths(object):
    __slots__ = ("state_dir", "session_dir", "launcher", "lock", "log", "current",
                 "entries", "status")

    def __init__(self, state_dir, socket_path, env=None):
        self.state_dir = state_dir
        # Shared by every session, like `entries` and unlike everything keyed on the
        # socket: the PATH symlink that reaches it is one line in the user's shell setup.
        self.launcher = os.path.join(state_dir, "track")
        self.session_dir = daemonize.session_dir(state_dir, socket_path)
        self.lock = os.path.join(self.session_dir, "daemon.lock")
        self.log = os.path.join(self.session_dir, "daemon.log")
        self.current = store.current_path(self.session_dir)
        self.status = os.path.join(self.session_dir, "daemon.json")
        self.entries = store.entries_path(state_dir, env)


def _resolve(env=None):
    env = env or os.environ
    cfg = config_mod.load(env)
    socket_path = sock.default_socket_path(env)
    paths = Paths(config_mod.state_dir(env), socket_path, env)
    return cfg, socket_path, paths


def session_name(socket_path):
    """`default`, or the name from a `.../sessions/<name>/herdr.sock` path."""
    if not socket_path:
        return "unknown"
    parts = socket_path.replace(os.sep, "/").split("/")
    if "sessions" in parts:
        index = parts.index("sessions")
        if index + 1 < len(parts):
            return parts[index + 1]
    return "default"


def focused_workspace(snapshot):
    """`{workspace_id, label, cwd}` for the focused Space, or None.

    A workspace record carries no `cwd` (verified), so the directory is taken from the
    focused pane, joined on workspace id.
    """
    workspace_id = snapshot.get("focused_workspace_id")
    if not workspace_id:
        return None
    label = None
    for workspace in snapshot.get("workspaces") or []:
        if workspace.get("workspace_id") == workspace_id:
            label = workspace.get("label")
            break
    cwd = None
    focused_pane = snapshot.get("focused_pane_id")
    for pane in snapshot.get("panes") or []:
        if pane.get("pane_id") == focused_pane:
            cwd = pane.get("cwd")
            break
    return {"workspace_id": workspace_id, "label": label, "cwd": cwd}


def _write_status(paths, tracker, extra):
    payload = {"pid": os.getpid(), "updated_at": time.time()}
    payload.update(extra)
    if tracker.current is not None:
        payload["open"] = {
            "workspace_id": tracker.current.workspace_id,
            "label": tracker.current.label,
            "start": tracker.current.start,
            "last_activity": tracker.current.last_activity,
            "idle_for": tracker.idle_for(),
            "seconds_until_close": tracker.seconds_until_idle_close(),
        }
    else:
        payload["open"] = None
    store.write_current(paths.status, payload)  # atomic write, any shape


def run_loop(cfg, socket_path, paths, log, stop_flag):
    session = session_name(socket_path)
    host = socket_mod.gethostname()
    tracker = seg_mod.SegmentTracker(cfg.idle_timeout_sec, cfg.min_entry_sec,
                                     session=session, host=host)

    def emit(entries):
        for entry in entries:
            store.append_entry(paths.entries, entry)
            log.info("entry %s %s %s (%ds, %s)"
                     % (entry.get("label") or entry["workspace_id"],
                        entry["start"], entry["end"], entry["seconds"],
                        entry["end_reason"]))

    # A crashed daemon leaves its open segment on disk; close it out rather than lose it.
    stranded = store.read_current(paths.current)
    if stranded:
        log.warn("recovering a segment left by a previous daemon")
        emit(tracker.recover(stranded))
        store.clear_current(paths.current)

    probe = activity.ActivityProbe(
        lambda pane_id: sock.pane_read(socket_path, pane_id))

    log.info("daemon start pid=%d socket=%s idle=%.0fs poll=%.1fs snapshot=%.1fs "
             "min_entry=%.0fs" % (os.getpid(), socket_path, cfg.idle_timeout_sec,
                                  cfg.poll_interval_sec, cfg.snapshot_interval_sec,
                                  cfg.min_entry_sec))

    last_screen_sample = 0.0
    last_workspace_id = None
    last_focused_pane = None
    # The daemon's first observation is startup, not a user action. Seeding from it
    # stops "the plugin just started" from being recorded as a focus change, which
    # would open an entry nobody earned and close it seconds later as idle.
    seeded = False

    try:
        while not stop_flag["stop"]:
            try:
                snapshot = sock.snapshot(socket_path)
            except sock.ServerGone as exc:
                log.info("server gone (%s); closing the open entry and exiting" % exc)
                emit(tracker.close(seg_mod.SHUTDOWN))
                store.clear_current(paths.current)
                break
            except sock.ProtocolError as exc:
                log.warn("snapshot unusable: %s" % exc)
                time.sleep(cfg.snapshot_interval_sec)
                continue

            workspace = focused_workspace(snapshot)
            panes = snapshot.get("panes") or []
            focused_pane_id = snapshot.get("focused_pane_id")
            now = time.time()
            reasons = []

            # 1. Navigation is activity: you cannot switch Spaces without being here.
            if not seeded:
                last_workspace_id = workspace["workspace_id"] if workspace else None
                last_focused_pane = focused_pane_id
                seeded = True
            else:
                if workspace and workspace["workspace_id"] != last_workspace_id:
                    reasons.append("focus")
                    last_workspace_id = workspace["workspace_id"]
                if focused_pane_id != last_focused_pane:
                    reasons.append("pane")
                    last_focused_pane = focused_pane_id

            # 2. An agent working in this Space is activity.
            if workspace:
                working = activity.agent_activity(panes, workspace["workspace_id"],
                                                  cfg.active_statuses)
                if working:
                    reasons.append("agent:" + ",".join(working))

            # 3. The focused pane's screen, but only for plain shells -- an animating
            #    agent UI would hash differently every sample and never look idle.
            if focused_pane_id and (now - last_screen_sample) >= cfg.poll_interval_sec:
                last_screen_sample = now
                pane = next((p for p in panes
                             if p.get("pane_id") == focused_pane_id), None)
                if pane is not None and not activity.pane_has_agent(pane):
                    if probe.changed(focused_pane_id):
                        reasons.append("screen")
                probe.forget({p.get("pane_id") for p in panes})

            if reasons:
                log.debug("activity %s in %s" % (",".join(reasons),
                                                 workspace["workspace_id"]
                                                 if workspace else "-"))
            emit(tracker.update(workspace, bool(reasons)))

            if tracker.current is not None:
                store.write_current(paths.current, tracker.current.to_state())
            else:
                store.clear_current(paths.current)
            _write_status(paths, tracker, {"socket_path": socket_path,
                                           "session": session,
                                           "entries_path": paths.entries,
                                           "discarded": tracker.discarded})

            deadline = now + cfg.snapshot_interval_sec
            while not stop_flag["stop"] and time.time() < deadline:
                time.sleep(min(0.1, max(0.0, deadline - time.time())))
    finally:
        if tracker.current is not None:
            emit(tracker.close(seg_mod.SHUTDOWN))
            store.clear_current(paths.current)
        log.info("daemon exit pid=%d" % os.getpid())
    return EXIT_OK


def cmd_daemon(args):
    try:
        cfg, socket_path, paths = _resolve()
    except config_mod.ConfigError as exc:
        sys.stderr.write("track: %s\n" % exc)
        return EXIT_CONFIG

    daemonize.write_launcher(paths.launcher, ENTRYPOINT)

    if args.restart:
        _stop_running(paths, quiet=True)

    lock = daemonize.Lock(paths.lock)
    if not lock.acquire():
        if not args.ensure:
            holder = daemonize.read_holder_pid(paths.lock)
            sys.stderr.write("track: already running%s\n"
                             % (" (pid %d)" % holder if holder else ""))
        return EXIT_OK

    if not socket_path:
        lock.release()
        sys.stderr.write("track: HERDR_SOCKET_PATH is unset; not a Herdr plugin "
                         "environment\n")
        return EXIT_SOCKET

    if not args.foreground:
        daemonize.detach(paths.log)
    lock.write_pid()

    log = daemonize.Log(paths.log, cfg.log_level, echo=args.foreground)
    for warning in cfg.warnings:
        log.warn(warning)

    stop_flag = {"stop": False}

    def _handle(signum, _frame):
        stop_flag["stop"] = True
        log.info("signal %d received" % signum)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    try:
        return run_loop(cfg, socket_path, paths, log, stop_flag)
    finally:
        lock.release()


def _stop_running(paths, quiet=False, timeout=10.0):
    """Signal the daemon and wait for it to hand back its lock.

    Waiting on the lock rather than the pid: a daemon that is another process's child
    becomes a zombie on exit and `os.kill(pid, 0)` succeeds on zombies. The lock is
    released only after the open entry has been written.
    """
    pid = daemonize.read_holder_pid(paths.lock)
    if not pid or not daemonize.pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        if not quiet:
            sys.stderr.write("track: could not signal pid %d: %s\n" % (pid, exc))
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        probe = daemonize.Lock(paths.lock)
        if probe.acquire():
            probe.release()
            return True
        time.sleep(0.05)
    return False


def cmd_stop(_args):
    try:
        _cfg, _socket_path, paths = _resolve()
    except config_mod.ConfigError as exc:
        sys.stderr.write("track: %s\n" % exc)
        return EXIT_CONFIG
    sys.stdout.write("stopped\n" if _stop_running(paths) else "daemon: not running\n")
    return EXIT_OK


def cmd_flush(_args):
    """Close the open entry now, without stopping the daemon.

    Implemented as stop-then-restart because the daemon owns the entry: it writes the
    record on shutdown, and the `workspace.focused` hook (or the next `daemon` run)
    brings it back.
    """
    try:
        _cfg, socket_path, paths = _resolve()
    except config_mod.ConfigError as exc:
        sys.stderr.write("track: %s\n" % exc)
        return EXIT_CONFIG
    if not _stop_running(paths):
        sys.stdout.write("daemon: not running\n")
        return EXIT_OK
    sys.stdout.write("flushed\n")
    if socket_path:
        os.spawnve(os.P_NOWAIT, sys.executable,
                   [sys.executable, os.path.abspath(__file__), "daemon"], os.environ)
    return EXIT_OK


def cmd_status(args):
    try:
        _cfg, socket_path, paths = _resolve()
    except config_mod.ConfigError as exc:
        sys.stderr.write("track: %s\n" % exc)
        return EXIT_CONFIG

    pid = daemonize.read_holder_pid(paths.lock)
    alive = daemonize.pid_alive(pid)
    state = store.read_json(paths.status) if alive else None

    if args.json:
        json.dump({"running": bool(alive), "pid": pid if alive else None,
                   "session": session_name(socket_path), "state": state}, sys.stdout)
        sys.stdout.write("\n")
        return EXIT_OK

    if not alive:
        sys.stdout.write("daemon: not running\n")
        sys.stdout.write("  entries: %s\n" % paths.entries)
        return EXIT_OK

    sys.stdout.write("daemon: running (pid %d)\n" % pid)
    open_segment = (state or {}).get("open")
    if open_segment:
        elapsed = time.time() - open_segment["start"]
        sys.stdout.write("  tracking: %s (%s)\n"
                         % (open_segment.get("label") or "?",
                            open_segment["workspace_id"]))
        sys.stdout.write("  elapsed:  %s\n" % report_mod.format_duration(elapsed))
        remaining = open_segment.get("seconds_until_close")
        if remaining is not None:
            sys.stdout.write("  closes in %s of quiet\n"
                             % report_mod.format_duration(remaining))
    else:
        sys.stdout.write("  tracking: nothing (no activity yet)\n")
    sys.stdout.write("  entries:  %s\n" % paths.entries)
    return EXIT_OK


def cmd_report(args):
    try:
        _cfg, _socket_path, paths = _resolve()
    except config_mod.ConfigError as exc:
        sys.stderr.write("track: %s\n" % exc)
        return EXIT_CONFIG

    bad = []
    entries = store.read_entries(paths.entries,
                                 on_bad_line=lambda n, _r: bad.append(n))

    try:
        if args.since:
            since = report_mod.parse_day(args.since)
            until = report_mod.parse_day(args.until) if args.until else \
                report_mod.parse_day("today")
        elif args.day:
            since = until = report_mod.parse_day(args.day)
        else:
            since = until = report_mod.parse_day("today")
    except ValueError as exc:
        sys.stderr.write("track: bad date (%s). Use YYYY-MM-DD, today or yesterday\n"
                         % exc)
        return EXIT_USAGE

    summary = report_mod.summarise(entries, by=args.by, since=since, until=until)

    if args.json:
        json.dump(summary, sys.stdout)
        sys.stdout.write("\n")
        return EXIT_OK

    if since == until:
        today = report_mod.parse_day("today")
        suffix = "  (today)" if since == today else ""
        title = "%s%s" % (since.isoformat(), suffix)
    else:
        title = "%s .. %s" % (since.isoformat(), until.isoformat())
    sys.stdout.write(report_mod.render(summary, title))
    if bad:
        sys.stderr.write("track: skipped %d malformed line(s) in %s\n"
                         % (len(bad), paths.entries))
    return EXIT_OK


def cmd_doctor(_args):
    try:
        cfg, socket_path, paths = _resolve()
    except config_mod.ConfigError as exc:
        sys.stderr.write("track: %s\n" % exc)
        return EXIT_CONFIG

    out = sys.stdout
    out.write("workspace-time-tracker doctor\n")
    out.write("  config source       : %s\n" % cfg.source)
    out.write("  config path         : %s\n" % cfg.source_path)
    out.write("  idleTimeoutSec      : %g\n" % cfg.idle_timeout_sec)
    out.write("  pollIntervalSec     : %g  (screen hash)\n" % cfg.poll_interval_sec)
    out.write("  snapshotIntervalSec : %g  (focus + agent status)\n"
              % cfg.snapshot_interval_sec)
    out.write("  minEntrySec         : %g\n" % cfg.min_entry_sec)
    out.write("  activeStatuses      : %s\n" % ", ".join(cfg.active_statuses))
    out.write("  activity token      : sha256 of pane.read(source=visible); agent panes\n")
    out.write("                        use agent_status instead and are never hashed\n")
    out.write("  session             : %s\n" % session_name(socket_path))
    out.write("  socket path         : %s\n" % (socket_path or "<unset>"))
    out.write("  entries             : %s\n" % paths.entries)
    entries = store.read_entries(paths.entries)
    out.write("  entries recorded    : %d\n" % len(entries))
    out.write("  log                 : %s\n" % paths.log)
    out.write("  launcher            : %s%s\n"
              % (paths.launcher,
                 "" if os.access(paths.launcher, os.X_OK)
                 else "  (not written yet -- start the daemon)"))

    if socket_path:
        try:
            snapshot = sock.snapshot(socket_path)
            workspace = focused_workspace(snapshot)
            out.write("  server              : reachable\n")
            out.write("  focused Space       : %s\n"
                      % (("%s (%s)" % (workspace.get("label"),
                                       workspace["workspace_id"]))
                         if workspace else "none"))
        except (sock.ServerGone, sock.ProtocolError) as exc:
            out.write("  server              : UNREACHABLE (%s)\n" % exc)
    for warning in cfg.warnings:
        out.write("  warning             : %s\n" % warning)
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(prog="track", add_help=True)
    sub = parser.add_subparsers(dest="command")

    d = sub.add_parser("daemon", help="run the tracking loop (detaches by default)")
    d.add_argument("--ensure", action="store_true")
    d.add_argument("--foreground", action="store_true")
    d.add_argument("--restart", action="store_true")
    d.set_defaults(func=cmd_daemon)

    sub.add_parser("stop", help="stop the daemon, closing the open entry").set_defaults(
        func=cmd_stop)
    sub.add_parser("flush", help="close the open entry now").set_defaults(func=cmd_flush)

    st = sub.add_parser("status", help="what is being tracked right now")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    r = sub.add_parser("report", help="time per Space")
    r.add_argument("--day", help="today | yesterday | YYYY-MM-DD")
    r.add_argument("--since", help="YYYY-MM-DD (inclusive)")
    r.add_argument("--until", help="YYYY-MM-DD (inclusive; defaults to today)")
    r.add_argument("--by", choices=report_mod.GROUPINGS, default=report_mod.BY_LABEL)
    r.add_argument("--json", action="store_true")
    r.set_defaults(func=cmd_report)

    sub.add_parser("doctor", help="resolved config and reachability").set_defaults(
        func=cmd_doctor)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_OK
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
