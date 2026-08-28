"""Plugin configuration: `$HERDR_PLUGIN_CONFIG_DIR/config.json`, entirely optional.

Unlike `vscode-workspace-sync`, this plugin works with **no config file at all** --
there is nothing a user must tell it. The file exists only to change defaults.

Parsed with `jsonc.loads` so the user may comment their own config. JSON rather than
TOML deliberately: `tomllib` is Python 3.11+ and this targets 3.9.

Env overrides exist for the test suite, which drives the daemon against a fake socket
server and a fake inhibitor; they are not a documented user interface.
"""

import json
import os
import sys

import jsonc

PLUGIN_ID = "agent-caffeinate"

ENV_INHIBITOR = "HERDR_CAFFEINATE_INHIBITOR_COMMAND"
ENV_IDLE_GRACE = "HERDR_CAFFEINATE_IDLE_GRACE_SEC"
ENV_POLL = "HERDR_CAFFEINATE_POLL_INTERVAL_SEC"
ENV_ACTIVE = "HERDR_CAFFEINATE_ACTIVE_STATUSES"
ENV_LOG_LEVEL = "HERDR_CAFFEINATE_LOG_LEVEL"

KNOWN_KEYS = ("idleGraceSec", "pollIntervalSec", "activeStatuses",
              "inhibitorCommand", "logLevel")

LOG_LEVELS = ("error", "warn", "info", "debug")

# `-i` prevents system idle sleep and `-s` prevents system sleep on AC; those are the
# two that keep an agent's work running. `-d` is display-only -- a sleeping display never
# suspends a process, so it buys the agent nothing and leaves an unlocked screen
# unattended. `-m` is a spinning-disk assertion, a no-op on SSD. `-u` without `-t`
# asserts user-active for a default of 5 seconds and wakes the display, so it is useless
# as a long-lived assertion. Users who *want* the display awake add "-d" via config.
MACOS_INHIBITOR = ["caffeinate", "-i", "-s"]
LINUX_INHIBITOR = ["systemd-inhibit", "--what=idle:sleep",
                   "--why=herdr agent working", "--mode=block",
                   "sleep", "infinity"]


class ConfigError(Exception):
    """Fatal configuration problem. The message is user-facing."""


# Herdr resolves both of these through the XDG base directories -- verified against
# `herdr plugin config-dir` and against the state directory it creates at install, with
# and without XDG_CONFIG_HOME / XDG_STATE_HOME set. The env vars above are what a hook
# gets; these fallbacks are for a run the user starts, which must land in the same place.
# A relative XDG value is invalid per the spec and ignored.


def config_dir(env=None):
    if env is None:
        env = os.environ
    from_env = env.get("HERDR_PLUGIN_CONFIG_DIR")
    if from_env:
        return from_env
    base = env.get("XDG_CONFIG_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.expanduser(os.path.join("~", ".config"))
    return os.path.join(base, "herdr", "plugins", "config", PLUGIN_ID)


def config_path(env=None):
    return os.path.join(config_dir(env), "config.json")


def state_dir(env=None):
    if env is None:
        env = os.environ
    from_env = env.get("HERDR_PLUGIN_STATE_DIR")
    if from_env:
        return from_env
    base = env.get("XDG_STATE_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.expanduser(os.path.join("~", ".local", "state"))
    return os.path.join(base, "herdr", "plugins", PLUGIN_ID)


def platform_inhibitor():
    return list(MACOS_INHIBITOR if sys.platform == "darwin" else LINUX_INHIBITOR)


class Config(object):
    __slots__ = ("idle_grace_sec", "poll_interval_sec", "active_statuses",
                 "inhibitor_command", "log_level", "source_path", "source",
                 "warnings")

    def __init__(self):
        self.idle_grace_sec = 60.0
        self.poll_interval_sec = 2.0
        self.active_statuses = ["working"]
        self.inhibitor_command = platform_inhibitor()
        self.log_level = "info"
        self.source_path = None
        self.source = "defaults"
        self.warnings = []


def _positive(raw, key, where):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError("%r must be a number, got %r\n  %s" % (key, raw, where))
    if value <= 0:
        raise ConfigError("%r must be greater than 0, got %r\n  %s" % (key, raw, where))
    return value


def load(env=None):
    """Read, validate and resolve the config. Raises :class:`ConfigError`."""
    if env is None:
        env = os.environ
    cfg = Config()
    cfg.source_path = config_path(env)
    where = "%s\n  (print it with: herdr plugin config-dir %s)" % (cfg.source_path,
                                                                  PLUGIN_ID)

    raw = {}
    if os.path.exists(cfg.source_path):
        cfg.source = "file"
        try:
            with open(cfg.source_path, "r") as fh:
                text = fh.read()
        except (IOError, OSError) as exc:
            raise ConfigError("cannot read config file: %s\n  %s" % (exc, where))
        try:
            raw = jsonc.loads(text)
        except ValueError as exc:
            raise ConfigError("config file is not valid JSON: %s\n  %s" % (exc, where))
        if not isinstance(raw, dict):
            raise ConfigError("config file must contain a JSON object\n  %s" % where)

    for key in raw:
        if key not in KNOWN_KEYS:
            cfg.warnings.append("unknown config key %r (ignored)" % key)

    if "idleGraceSec" in raw:
        cfg.idle_grace_sec = _positive(raw["idleGraceSec"], "idleGraceSec", where)
    if "pollIntervalSec" in raw:
        cfg.poll_interval_sec = _positive(raw["pollIntervalSec"], "pollIntervalSec",
                                          where)

    if "activeStatuses" in raw:
        statuses = raw["activeStatuses"]
        if not isinstance(statuses, list) or not statuses:
            raise ConfigError("\"activeStatuses\" must be a non-empty array of "
                              "strings\n  %s" % where)
        cleaned = []
        for item in statuses:
            if isinstance(item, str):
                cleaned.append(item)
            else:
                cfg.warnings.append("activeStatuses: ignoring non-string %r" % item)
        if not cleaned:
            raise ConfigError("\"activeStatuses\" held no strings\n  %s" % where)
        cfg.active_statuses = cleaned

    if raw.get("inhibitorCommand") is not None:
        argv = raw["inhibitorCommand"]
        if not isinstance(argv, list) or not argv or \
                not all(isinstance(a, str) for a in argv):
            raise ConfigError("\"inhibitorCommand\" must be a non-empty array of "
                              "strings\n  %s" % where)
        cfg.inhibitor_command = list(argv)

    if "logLevel" in raw:
        if raw["logLevel"] not in LOG_LEVELS:
            raise ConfigError("\"logLevel\" must be one of %s, got %r\n  %s"
                              % (" | ".join(LOG_LEVELS), raw["logLevel"], where))
        cfg.log_level = raw["logLevel"]

    _apply_env(cfg, env)
    return cfg


def _apply_env(cfg, env):
    """Test-only overrides. They win over the file."""
    if env.get(ENV_IDLE_GRACE):
        cfg.idle_grace_sec = _positive(env[ENV_IDLE_GRACE], ENV_IDLE_GRACE, "(env)")
    if env.get(ENV_POLL):
        cfg.poll_interval_sec = _positive(env[ENV_POLL], ENV_POLL, "(env)")
    if env.get(ENV_LOG_LEVEL):
        if env[ENV_LOG_LEVEL] not in LOG_LEVELS:
            raise ConfigError("%s must be one of %s" % (ENV_LOG_LEVEL,
                                                        " | ".join(LOG_LEVELS)))
        cfg.log_level = env[ENV_LOG_LEVEL]
    if env.get(ENV_ACTIVE):
        cfg.active_statuses = [s for s in env[ENV_ACTIVE].split(",") if s]
    if env.get(ENV_INHIBITOR):
        try:
            argv = json.loads(env[ENV_INHIBITOR])
        except ValueError as exc:
            raise ConfigError("%s is not valid JSON: %s" % (ENV_INHIBITOR, exc))
        if not isinstance(argv, list) or not argv or \
                not all(isinstance(a, str) for a in argv):
            raise ConfigError("%s must be a JSON array of strings" % ENV_INHIBITOR)
        cfg.inhibitor_command = argv
    if cfg.source == "defaults" and any(
            env.get(k) for k in (ENV_IDLE_GRACE, ENV_POLL, ENV_ACTIVE, ENV_INHIBITOR)):
        cfg.source = "defaults+env"
