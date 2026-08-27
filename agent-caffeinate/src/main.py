#!/usr/bin/env python3
"""agent-caffeinate -- hold a sleep assertion while coding agents are working.

Entrypoint and daemon loop. The loop is a **poll**, not a subscription, for reasons
measured in `docs/herdr-daemon-facts.md`: there is no session-wide agent-status stream
to subscribe to (`pane.agent_status_changed` requires a concrete `pane_id`), the server
closes a connection after every non-subscribe request, and a whole-session snapshot
costs 0.35 ms. Polling the full state also makes a missed event impossible -- every
poll is a re-seed, so a pane that dies while `working` cannot strand the assertion.
"""

import argparse
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config as config_mod          # noqa: E402
import daemonize                     # noqa: E402
import sock                          # noqa: E402
from inhibitor import Inhibitor      # noqa: E402
from tracker import START, STOP, Tracker  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_SOCKET = 2


class Paths(object):
    """Everything the daemon writes, namespaced per Herdr server."""

    __slots__ = ("session_dir", "lock", "log", "inhibitor_state", "status")

    def __init__(self, state_dir, socket_path):
        self.session_dir = daemonize.session_dir(state_dir, socket_path)
        self.lock = os.path.join(self.session_dir, "daemon.lock")
        self.log = os.path.join(self.session_dir, "daemon.log")
        self.inhibitor_state = os.path.join(self.session_dir, "inhibitor.json")
        self.status = os.path.join(self.session_dir, "daemon.json")


def _resolve(env=None):
    env = env or os.environ
    cfg = config_mod.load(env)
    socket_path = sock.default_socket_path(env)
    paths = Paths(config_mod.state_dir(env), socket_path)
    return cfg, socket_path, paths


def _write_status(paths, tracker, inhibitor, socket_path, started_at):
    payload = {
        "pid": os.getpid(),
        "started_at": started_at,
        "socket_path": socket_path,
        "session_key": daemonize.session_key(socket_path),
        "holding": tracker.holding,
        "dry": inhibitor.dry,
        "inhibitor_pid": inhibitor.pid(),
        "active_panes": tracker.active_panes(),
        "statuses": tracker.statuses,
        "seconds_until_stop": tracker.seconds_until_stop(),
        "updated_at": time.time(),
    }
    tmp = paths.status + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(payload, fh)
        os.replace(tmp, paths.status)
    except (IOError, OSError):
        pass


def run_loop(cfg, socket_path, paths, log, stop_flag):
    """The daemon proper. Returns an exit code."""
    tracker = Tracker(cfg.active_statuses, cfg.idle_grace_sec)
    inhibitor = Inhibitor(cfg.inhibitor_command, paths.inhibitor_state, log)
    inhibitor.adopt_stale()
    started_at = time.time()

    log.info("daemon start pid=%d socket=%s poll=%.2fs grace=%.1fs active=%s%s"
             % (os.getpid(), socket_path, cfg.poll_interval_sec, cfg.idle_grace_sec,
                ",".join(cfg.active_statuses), " DRY" if inhibitor.dry else ""))

    code = EXIT_OK
    try:
        while not stop_flag["stop"]:
            try:
                statuses = sock.pane_statuses(socket_path)
            except sock.ServerGone as exc:
                log.info("server gone (%s); releasing and exiting" % exc)
                break
            except sock.ProtocolError as exc:
                # A malformed reply is not a reason to drop the assertion; log and retry.
                log.warn("snapshot unusable: %s" % exc)
                time.sleep(cfg.poll_interval_sec)
                continue

            action = tracker.update(statuses)
            if action == START:
                inhibitor.start(trigger=",".join(tracker.active_panes()))
            elif action == STOP:
                idle = tracker.idle_for()
                inhibitor.stop(reason="idle-grace idle_for=%.1fs"
                                      % (idle if idle is not None else 0.0))

            _write_status(paths, tracker, inhibitor, socket_path, started_at)

            nap = tracker.next_wakeup(cfg.poll_interval_sec)
            deadline = time.time() + nap
            while not stop_flag["stop"] and time.time() < deadline:
                time.sleep(min(0.1, max(0.0, deadline - time.time())))
    finally:
        inhibitor.stop(reason="shutdown")
        _write_status(paths, tracker, inhibitor, socket_path, started_at)
        log.info("daemon exit pid=%d" % os.getpid())
    return code


def cmd_daemon(args):
    try:
        cfg, socket_path, paths = _resolve()
    except config_mod.ConfigError as exc:
        sys.stderr.write("agent-caffeinate: %s\n" % exc)
        return EXIT_CONFIG

    if args.restart:
        _stop_running(paths, quiet=True)

    lock = daemonize.Lock(paths.lock)
    if not lock.acquire():
        if not args.ensure:
            holder = daemonize.read_holder_pid(paths.lock)
            sys.stderr.write("agent-caffeinate: already running%s\n"
                             % (" (pid %d)" % holder if holder else ""))
        return EXIT_OK

    if not socket_path:
        lock.release()
        sys.stderr.write("agent-caffeinate: HERDR_SOCKET_PATH is unset; not a Herdr "
                         "plugin environment\n")
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
    """Signal a running daemon and wait for it to hand back its lock.

    Waiting on the **lock**, not on the pid: a daemon that is some other process's child
    becomes a zombie when it exits, and `os.kill(pid, 0)` succeeds on a zombie, so a
    pid-based wait would report failure for a daemon that had shut down perfectly. The
    lock is released only after the inhibitor has been stopped, which is precisely the
    thing the caller wants to have happened.
    """
    pid = daemonize.read_holder_pid(paths.lock)
    if not pid or not daemonize.pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        if not quiet:
            sys.stderr.write("agent-caffeinate: could not signal pid %d: %s\n"
                             % (pid, exc))
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        probe = daemonize.Lock(paths.lock)
        if probe.acquire():
            probe.release()
            return True
        time.sleep(0.05)
    if not quiet:
        sys.stderr.write("agent-caffeinate: pid %d did not release its lock within "
                         "%.0fs\n" % (pid, timeout))
    return False


def cmd_stop(_args):
    try:
        _, _socket_path, paths = _resolve()
    except config_mod.ConfigError as exc:
        sys.stderr.write("agent-caffeinate: %s\n" % exc)
        return EXIT_CONFIG
    if _stop_running(paths):
        sys.stdout.write("stopped\n")
    else:
        sys.stdout.write("daemon: not running\n")
    return EXIT_OK


def _read_status(paths):
    try:
        with open(paths.status, "r") as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return None


def cmd_status(args):
    try:
        _cfg, socket_path, paths = _resolve()
    except config_mod.ConfigError as exc:
        sys.stderr.write("agent-caffeinate: %s\n" % exc)
        return EXIT_CONFIG

    pid = daemonize.read_holder_pid(paths.lock)
    alive = daemonize.pid_alive(pid)
    status = _read_status(paths) if alive else None

    if args.json:
        json.dump({"running": bool(alive), "pid": pid if alive else None,
                   "socket_path": socket_path,
                   "session_key": daemonize.session_key(socket_path),
                   "state": status}, sys.stdout)
        sys.stdout.write("\n")
        return EXIT_OK

    if not alive:
        sys.stdout.write("daemon: not running\n")
        sys.stdout.write("  session: %s\n" % daemonize.session_key(socket_path))
        sys.stdout.write("  log:     %s\n" % paths.log)
        return EXIT_OK

    sys.stdout.write("daemon: running (pid %d)\n" % pid)
    if status:
        up = time.time() - status.get("started_at", time.time())
        sys.stdout.write("  uptime:    %dm %ds\n" % (up // 60, up % 60))
        if status.get("dry"):
            sys.stdout.write("  inhibitor: dry mode (command not on PATH)\n")
        elif status.get("inhibitor_pid"):
            sys.stdout.write("  inhibitor: holding (pid %d)\n"
                             % status["inhibitor_pid"])
        else:
            sys.stdout.write("  inhibitor: idle\n")
        active = status.get("active_panes") or []
        sys.stdout.write("  active:    %s\n" % (", ".join(active) if active else "none"))
        pending = status.get("seconds_until_stop")
        if pending is not None:
            sys.stdout.write("  stopping in %.0fs\n" % pending)
        statuses = status.get("statuses") or {}
        for pane in sorted(statuses):
            sys.stdout.write("    %-10s %s\n" % (pane, statuses[pane]))
    sys.stdout.write("  log:       %s\n" % paths.log)
    return EXIT_OK


def cmd_doctor(_args):
    try:
        cfg, socket_path, paths = _resolve()
    except config_mod.ConfigError as exc:
        sys.stderr.write("agent-caffeinate: %s\n" % exc)
        return EXIT_CONFIG

    import shutil
    out = sys.stdout
    out.write("agent-caffeinate doctor\n")
    out.write("  config source     : %s\n" % cfg.source)
    out.write("  config path       : %s\n" % cfg.source_path)
    out.write("  idleGraceSec      : %g\n" % cfg.idle_grace_sec)
    out.write("  pollIntervalSec   : %g\n" % cfg.poll_interval_sec)
    out.write("  activeStatuses    : %s\n" % ", ".join(cfg.active_statuses))
    out.write("  inhibitorCommand  : %s\n" % " ".join(cfg.inhibitor_command))
    resolved = shutil.which(cfg.inhibitor_command[0])
    out.write("  argv[0] resolves  : %s\n" % (resolved or "NO -- dry mode, no assertion"))
    out.write("  logLevel          : %s\n" % cfg.log_level)
    out.write("  socket path       : %s\n" % (socket_path or "<unset>"))
    out.write("  session key       : %s\n" % daemonize.session_key(socket_path))
    out.write("  session dir       : %s\n" % paths.session_dir)
    out.write("  log               : %s\n" % paths.log)

    if socket_path:
        try:
            statuses = sock.pane_statuses(socket_path)
            out.write("  server            : reachable, %d pane(s)\n" % len(statuses))
            for pane in sorted(statuses):
                out.write("      %-10s %s\n" % (pane, statuses[pane]))
        except (sock.ServerGone, sock.ProtocolError) as exc:
            out.write("  server            : UNREACHABLE (%s)\n" % exc)
    for warning in cfg.warnings:
        out.write("  warning           : %s\n" % warning)
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(prog="agent-caffeinate", add_help=True)
    sub = parser.add_subparsers(dest="command")

    d = sub.add_parser("daemon", help="run the poll loop (detaches by default)")
    d.add_argument("--ensure", action="store_true",
                   help="silent no-op when a daemon already owns this session")
    d.add_argument("--foreground", action="store_true",
                   help="stay attached and echo the log to stderr")
    d.add_argument("--restart", action="store_true",
                   help="stop a running daemon first")
    d.set_defaults(func=cmd_daemon)

    s = sub.add_parser("stop", help="stop this session's daemon and its inhibitor")
    s.set_defaults(func=cmd_stop)

    st = sub.add_parser("status", help="what the daemon is doing right now")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)

    doc = sub.add_parser("doctor", help="resolved config and reachability")
    doc.set_defaults(func=cmd_doctor)
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
