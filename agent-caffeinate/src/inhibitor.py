"""Spawning and reaping the sleep-inhibiting process.

The plugin never implements sleep prevention itself -- it runs a long-lived command
(`caffeinate -i -s` on macOS) for exactly as long as the assertion should hold, and
kills it to release. Everything platform-specific lives in the argv.

Two behaviours worth knowing:

- **Dry mode.** If the command's argv[0] is not on PATH -- a Linux container, say -- the
  inhibitor logs what it *would* have done and spawns nothing. That keeps the plugin
  harmless where it cannot work, and makes the test suite's fake a plain argv swap
  rather than a code path of its own.
- **Stale adoption.** A `kill -9`'d daemon leaves its inhibitor running, and a
  plugin-spawned process outlives the Herdr server (measured). So the pid is recorded to
  disk before anything else, and a new daemon reaps a recorded pid that is still alive
  and still looks like our command.
"""

import json
import os
import shutil
import signal
import subprocess
import time

TERM_GRACE_SEC = 2.0


class Inhibitor(object):
    __slots__ = ("argv", "state_path", "log", "proc", "dry", "_warned")

    def __init__(self, argv, state_path, log):
        self.argv = list(argv)
        self.state_path = state_path
        self.log = log
        self.proc = None
        self.dry = shutil.which(self.argv[0]) is None
        self._warned = False

    # -- persistence -------------------------------------------------------------

    def _write_state(self, pid):
        try:
            with open(self.state_path, "w") as fh:
                json.dump({"pid": pid, "argv": self.argv, "at": time.time()}, fh)
        except (IOError, OSError) as exc:
            self.log.warn("could not record inhibitor state: %s" % exc)

    def _read_state(self):
        try:
            with open(self.state_path, "r") as fh:
                return json.load(fh)
        except (IOError, OSError, ValueError):
            return None

    def _clear_state(self):
        try:
            os.unlink(self.state_path)
        except OSError:
            pass

    # -- lifecycle ---------------------------------------------------------------

    def adopt_stale(self):
        """Kill an inhibitor left behind by a previous daemon. Returns its pid or None."""
        state = self._read_state()
        if not state:
            return None
        pid = state.get("pid")
        if not pid or state.get("argv") != self.argv:
            self._clear_state()
            return None
        if not _alive(pid):
            self._clear_state()
            return None
        self.log.warn("reaping stale inhibitor pid=%d from a previous daemon" % pid)
        _terminate(pid)
        self._clear_state()
        return pid

    def is_running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, trigger=""):
        if self.is_running():
            return True
        if self.dry:
            if not self._warned:
                self.log.warn("%s not found on PATH: running in dry mode, no sleep "
                              "assertion will be taken" % self.argv[0])
                self._warned = True
            self.log.info("dry-run: would start inhibitor argv=%s trigger=%s"
                          % (" ".join(self.argv), trigger))
            return True
        try:
            self.proc = subprocess.Popen(
                self.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True)
        except OSError as exc:
            self.log.error("could not start inhibitor %s: %s"
                           % (" ".join(self.argv), exc))
            self.proc = None
            return False
        self._write_state(self.proc.pid)
        self.log.info("inhibitor start pid=%d argv=%s trigger=%s"
                      % (self.proc.pid, " ".join(self.argv), trigger))
        return True

    def stop(self, reason=""):
        if self.dry:
            self.log.info("dry-run: would stop inhibitor reason=%s" % reason)
            return True
        if not self.is_running():
            self._clear_state()
            return True
        pid = self.proc.pid
        _terminate(pid, proc=self.proc)
        self.log.info("inhibitor stop pid=%d reason=%s" % (pid, reason))
        self.proc = None
        self._clear_state()
        return True

    def pid(self):
        return self.proc.pid if self.is_running() else None


def _reap_if_child(pid):
    """Reap `pid` if it happens to be ours.

    A signalled process that is still our child stays in the table as a **zombie** until
    someone waits on it, and `os.kill(pid, 0)` succeeds on a zombie -- so without this a
    termination loop would never see the process die. An inhibitor orphaned by a crashed
    daemon is normally reparented to init and reaped there, but the daemon's *own*
    inhibitor is its child, so both cases have to work.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _terminate(pid, proc=None):
    """SIGTERM, then SIGKILL after a grace period."""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.time() + TERM_GRACE_SEC
    while time.time() < deadline:
        if proc is not None:
            if proc.poll() is not None:
                return
        else:
            _reap_if_child(pid)
            if not _alive(pid):
                return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    if proc is not None:
        try:
            proc.wait(timeout=1)
        except Exception:
            pass
    else:
        _reap_if_child(pid)
