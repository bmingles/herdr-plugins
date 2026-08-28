"""Singleton locking, detaching, and the daemon's log file.

Two measured facts drive this module (`docs/herdr-daemon-facts.md`):

- `[[startup]]` fires once per **server boot**, and plugin registration is global across
  sessions, so every running session's server starts its own daemon. The singleton key
  is therefore **per socket**, not global.
- A detached child survives the hook (which returned in 31 ms) *and* survives the server
  itself. `setsid` keeps the daemon out of the server's process group so a group-wide
  signal cannot kill it mid-release; noticing the server has gone is `sock.ServerGone`'s
  job, not the kernel's.
"""

import fcntl
import hashlib
import os
import shlex
import sys
import time

LOG_MAX_BYTES = 1 << 20  # 1 MB, then one rollover to .log.1


def session_key(socket_path):
    """A short stable id for one Herdr server, used to namespace lock/log/state."""
    raw = socket_path or "nosocket"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def session_dir(state_dir, socket_path):
    path = os.path.join(state_dir, session_key(socket_path))
    os.makedirs(path, exist_ok=True)
    return path


class Lock(object):
    """A flock held for the daemon's lifetime.

    The lock lives on the open file description, which is shared across `fork()`, so it
    survives the double-fork in :func:`detach` -- the intermediate parents exit while the
    final child keeps the descriptor, and therefore the lock, open.
    """

    __slots__ = ("path", "fd")

    def __init__(self, path):
        self.path = path
        self.fd = None

    def acquire(self):
        """True if we now hold it; False if another daemon does."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            os.close(fd)
            return False
        self.fd = fd
        return True

    def write_pid(self):
        if self.fd is None:
            return
        os.ftruncate(self.fd, 0)
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, ("%d\n" % os.getpid()).encode("utf-8"))
        os.fsync(self.fd)

    def release(self):
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        except (IOError, OSError):
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None


def read_holder_pid(path):
    """The pid recorded in a lock file, or None. Advisory only -- may be stale."""
    try:
        with open(path, "r") as fh:
            return int(fh.read().strip())
    except (IOError, OSError, ValueError):
        return None


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def detach(log_path):
    """Double-fork, `setsid`, and redirect stdio to `log_path`.

    Returns only in the final daemon process; the intermediate processes `_exit` so the
    plugin hook that spawned us returns immediately.
    """
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    os.chdir("/")
    os.umask(0o022)

    with open(os.devnull, "rb") as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, sys.stdout.fileno())
    os.dup2(fd, sys.stderr.fileno())
    if fd > 2:
        os.close(fd)


LEVELS = {"error": 0, "warn": 1, "info": 2, "debug": 3}


class Log(object):
    """Line-oriented log with a single size-capped rollover.

    Writes go to an explicitly opened file rather than to stdout, so `status` and
    `doctor` can log to the same place without their own output being swallowed.
    """

    __slots__ = ("path", "level", "echo")

    def __init__(self, path, level="info", echo=False):
        self.path = path
        self.level = LEVELS.get(level, 2)
        self.echo = echo

    def _rotate_if_needed(self):
        try:
            if os.path.getsize(self.path) < LOG_MAX_BYTES:
                return
        except OSError:
            return
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def log(self, level, message):
        if LEVELS.get(level, 2) > self.level:
            return
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        line = "%s %-5s %s\n" % (stamp, level, message)
        self._rotate_if_needed()
        try:
            with open(self.path, "a") as fh:
                fh.write(line)
        except (IOError, OSError):
            pass
        if self.echo:
            sys.stderr.write(line)
            sys.stderr.flush()

    def error(self, message):
        self.log("error", message)

    def warn(self, message):
        self.log("warn", message)

    def info(self, message):
        self.log("info", message)

    def debug(self, message):
        self.log("debug", message)


# -- the fixed-path launcher ----------------------------------------------------------
#
# A GitHub-installed plugin root is `~/.config/herdr/plugins/github/<id>-<hash>/<subdir>`
# and a linked one is wherever the user's checkout happens to live, so nothing outside the
# plugin can name `bin/agent-caffeinate` without first digging the path out of
# `herdr plugin list --json` -- neither a `ui.tab_bar_right` entry in the user's
# `config.toml` nor a symlink on their `PATH`. The state directory *is* fixed at
# `~/.local/state/herdr/plugins/agent-caffeinate`, so the daemon keeps an executable shim
# there. That gives the README one literal path to hand out, and it keeps pointing at the
# right place after a reinstall moves the plugin root.

LAUNCHER_HEADER = (
    "# Generated by agent-caffeinate. Do not edit -- rewritten on every daemon start,\n"
    "# so it follows the plugin when a reinstall moves its directory.\n"
)


def write_launcher(path, entrypoint):
    """Refresh the executable shim at *path* so it execs `entrypoint "$@"`.

    Rewrites only when the content or the executable bit is wrong, because `--ensure`
    runs this on every `workspace.focused`. Never raises: a failure here costs an
    optional tab bar entry, and must not take the daemon down with it.
    """
    body = "#!/bin/sh\n%sexec %s \"$@\"\n" % (LAUNCHER_HEADER, shlex.quote(entrypoint))
    try:
        with open(path) as fh:
            if fh.read() == body and os.access(path, os.X_OK):
                return path
    except (IOError, OSError):
        pass

    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w") as fh:
            fh.write(body)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    except (IOError, OSError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return path
