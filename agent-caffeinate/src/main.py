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
from tracker import START, STOP, Tracker, TransitionJournal  # noqa: E402

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_SOCKET = 2

# This file is `<plugin root>/src/main.py`, so the shim two levels up is the entrypoint
# a generated launcher must exec. Derived from `__file__` rather than from
# `$HERDR_PLUGIN_ROOT`, which is unset when the user runs the command by hand.
ENTRYPOINT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "bin", "agent-caffeinate")


class Paths(object):
    """Everything the daemon writes, namespaced per Herdr server."""

    __slots__ = ("root", "session_dir", "launcher", "lock", "log", "inhibitor_state",
                 "status")

    def __init__(self, state_dir, socket_path):
        self.session_dir = daemonize.session_dir(state_dir, socket_path)
        # Shared by every session, unlike everything below it: the tab bar entry and
        # the PATH symlink that reach it are one line each in the user's own files, not
        # one per Herdr server.
        self.root = state_dir
        self.launcher = os.path.join(state_dir, "agent-caffeinate")
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
    journal = TransitionJournal()
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

            for line in journal.observe(statuses):
                # info, not debug: these lines are the only record of how long a
                # working agent falsely reads as idle, and so the only way to ever
                # tune idleGraceSec from data. Gating them behind a non-default level
                # meant a normal run collected nothing. Volume is one line per pane
                # status change -- a few hundred a day.
                log.info(line)

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


def _staleness_bounds(cfg):
    """How old a status file must be before its owner is presumed wedged.

    Generous on purpose. The daemon rewrites `daemon.json` every poll, so a healthy one
    is never more than one interval behind; anything beyond several intervals is either
    wedged or stopped. The floor keeps a very short `pollIntervalSec` from making the
    check trigger-happy.
    """
    stale_after = max(15.0, cfg.poll_interval_sec * 5)
    confirm_delay = max(3.0, cfg.poll_interval_sec * 2)
    return stale_after, confirm_delay


def _holder_age(paths):
    """Seconds since the lock holder last reported progress, or None if unknowable."""
    state = _read_status(paths)
    if not state or not isinstance(state.get("updated_at"), (int, float)):
        return None
    return time.time() - state["updated_at"]


def _take_over(paths, cfg, log, lock):
    """Displace a wedged daemon and claim its lock. True if we now hold it.

    Confirms before acting: a laptop that slept leaves a status file hours old, and the
    holder will refresh it within a poll of waking. Killing a daemon that is merely
    catching up would be worse than the problem being solved, so we look twice.
    """
    stale_after, confirm_delay = _staleness_bounds(cfg)
    holder = daemonize.read_holder_pid(paths.lock)
    log.warn("lock held by pid %s whose status is %.0fs stale; confirming before "
             "taking over" % (holder, _holder_age(paths) or -1))

    deadline = time.time() + confirm_delay
    while time.time() < deadline:
        time.sleep(0.2)
        age = _holder_age(paths)
        if age is not None and age < stale_after:
            log.info("pid %s recovered on its own; leaving it alone" % holder)
            return False

    if not holder or not daemonize.pid_alive(holder):
        # It died while we were looking; the lock is free for the taking.
        log.info("previous holder is gone; claiming the lock")
        return lock.acquire()

    log.warn("taking over from wedged daemon pid %d" % holder)
    for signum in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(holder, signum)
        except OSError as exc:
            log.warn("could not signal pid %d: %s" % (holder, exc))
        grace = time.time() + (5.0 if signum == signal.SIGTERM else 2.0)
        while time.time() < grace:
            if lock.acquire():
                log.warn("took over from pid %d (killed with %s); its inhibitor will "
                         "be reaped" % (holder, "SIGTERM" if signum == signal.SIGTERM
                                        else "SIGKILL"))
                return True
            time.sleep(0.1)

    log.error("could not take the lock from pid %d; giving up" % holder)
    return False


def cmd_daemon(args):
    try:
        cfg, socket_path, paths = _resolve()
    except config_mod.ConfigError as exc:
        sys.stderr.write("agent-caffeinate: %s\n" % exc)
        return EXIT_CONFIG

    daemonize.write_launcher(paths.launcher, ENTRYPOINT)

    if args.restart:
        _stop_running(paths, quiet=True)

    lock = daemonize.Lock(paths.lock)
    stale_after, _confirm = _staleness_bounds(cfg)
    taking_over = False
    if not lock.acquire():
        age = _holder_age(paths)
        if age is None or age < stale_after:
            # Someone healthy owns this session. Nothing to do.
            if not args.ensure:
                holder = daemonize.read_holder_pid(paths.lock)
                sys.stderr.write("agent-caffeinate: already running%s\n"
                                 % (" (pid %d)" % holder if holder else ""))
            return EXIT_OK
        # The holder has stopped reporting progress. Detach first so the plugin hook
        # returns immediately, then confirm and displace it from the background.
        taking_over = True

    if not socket_path:
        lock.release()
        sys.stderr.write("agent-caffeinate: HERDR_SOCKET_PATH is unset; not a Herdr "
                         "plugin environment\n")
        return EXIT_SOCKET

    if not args.foreground:
        daemonize.detach(paths.log)

    log = daemonize.Log(paths.log, cfg.log_level, echo=args.foreground)

    if taking_over and not _take_over(paths, cfg, log, lock):
        return EXIT_OK

    lock.write_pid()
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


# -- status indicator ---------------------------------------------------------------
#
# For a `ui.tab_bar_right` entry in the user's `config.toml`. Herdr re-runs that command
# on the server every `interval_seconds` and renders its last line, so this path must be
# cheap and must never block: it reads the two files the daemon already maintains and
# makes no socket call at all. Herdr also clears the entry on empty output, which is
# exactly the rendering wanted for "nothing to say" -- so silence is the default and
# every branch below that has nothing to report returns "".

ICON_HOLDING = u"\u2615"   # coffee cup
ICON_IDLE = u"\u25cb"      # hollow circle
ICON_FAULT = u"\u26a0"     # warning sign


def indicator_state(cfg, paths):
    """What the indicator should report, read from the daemon's own state files.

    ``state`` is one of ``absent`` (no live daemon), ``starting`` (holds the lock but
    has not written a status yet), ``wedged`` (alive but no longer making progress),
    ``idle``, or ``holding``. ``dry`` is orthogonal: it says the inhibitor command does
    not resolve, so the daemon is tracking agents but spawning nothing.
    """
    info = {"state": "absent", "dry": False, "pid": None, "age": None,
            "active_panes": [], "seconds_until_stop": None}

    pid = daemonize.read_holder_pid(paths.lock)
    if not pid or not daemonize.pid_alive(pid):
        return info
    info["pid"] = pid

    status = _read_status(paths)
    updated = (status or {}).get("updated_at")
    if not isinstance(updated, (int, float)):
        # The lock is taken but nothing has been reported. A daemon one poll into its
        # life looks exactly like this, so it is not a fault worth drawing.
        info["state"] = "starting"
        return info

    info["age"] = max(0.0, time.time() - updated)
    info["dry"] = bool(status.get("dry"))
    info["active_panes"] = list(status.get("active_panes") or [])
    pending = status.get("seconds_until_stop")
    if isinstance(pending, (int, float)):
        info["seconds_until_stop"] = pending

    stale_after, _confirm = _staleness_bounds(cfg)
    if info["age"] >= stale_after:
        # Same rule `doctor` uses. A wedged daemon's `holding` flag is whatever it was
        # when it stopped making progress, so it is not worth rendering as fact.
        info["state"] = "wedged"
    elif status.get("holding"):
        info["state"] = "holding"
    else:
        info["state"] = "idle"
    return info


def indicator_text(info, label, icon, show_idle):
    """One line for the tab bar, or "" meaning render nothing."""
    state = info["state"]
    if state == "wedged":
        return (u"%s %s" % (ICON_FAULT, label)).strip()
    if state == "holding":
        text = u"%s %s" % (icon, label)
    elif state == "idle" and show_idle:
        text = u"%s %s" % (ICON_IDLE, label)
    else:
        return u""
    if info["dry"]:
        text += u" (dry)"
    return text.strip()


def cmd_indicator(args):
    env = os.environ
    try:
        cfg = config_mod.load(env)
    except config_mod.ConfigError:
        # A broken config file is `doctor`'s to report, not the tab bar's. Only the
        # staleness bound depends on it, so fall back to defaults rather than go blind
        # about a daemon that may well be running fine on an older, valid config.
        cfg = config_mod.Config()

    paths = Paths(config_mod.state_dir(env), sock.default_socket_path(env))
    info = indicator_state(cfg, paths)
    text = indicator_text(info, args.label, args.icon, args.show_idle)

    if args.json:
        payload = dict(info)
        payload["text"] = text
        json.dump(payload, sys.stdout)
        sys.stdout.write("\n")
    elif text:
        sys.stdout.write(text + "\n")
    return EXIT_OK


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
    out.write("  launcher          : %s%s\n"
              % (paths.launcher,
                 "" if os.access(paths.launcher, os.X_OK)
                 else "  (not written yet -- start the daemon)"))

    holder = daemonize.read_holder_pid(paths.lock)
    if holder and daemonize.pid_alive(holder):
        age = _holder_age(paths)
        stale_after, _confirm = _staleness_bounds(cfg)
        if age is None:
            out.write("  daemon            : pid %d holds the lock but reports no "
                      "status\n" % holder)
        elif age >= stale_after:
            out.write("  daemon            : pid %d is WEDGED (last progress %.0fs "
                      "ago); the next start will take over\n" % (holder, age))
        else:
            out.write("  daemon            : running (pid %d, last progress %.0fs "
                      "ago)\n" % (holder, age))
    else:
        out.write("  daemon            : not running\n")

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

    ind = sub.add_parser("indicator",
                         help="one status line for a ui.tab_bar_right entry")
    ind.add_argument("--label", default="caffeinate",
                     help="text after the icon (default: caffeinate)")
    ind.add_argument("--icon", default=ICON_HOLDING,
                     help="glyph shown while holding (default: %s)" % ICON_HOLDING)
    ind.add_argument("--show-idle", action="store_true",
                     help="also render when the daemon is running but not holding")
    ind.add_argument("--json", action="store_true")
    ind.set_defaults(func=cmd_indicator)

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
