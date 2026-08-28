"""Plugin configuration: `$HERDR_PLUGIN_CONFIG_DIR/config.json`, entirely optional.

Same shape as `agent-caffeinate`'s: every key has a working default, so the plugin needs
no config file. Env overrides exist for the test suite, not as a user interface.
"""

import os

import jsonc

PLUGIN_ID = "workspace-time-tracker"

ENV_IDLE_TIMEOUT = "HERDR_TRACK_IDLE_TIMEOUT_SEC"
ENV_POLL = "HERDR_TRACK_POLL_INTERVAL_SEC"
ENV_SNAPSHOT = "HERDR_TRACK_SNAPSHOT_INTERVAL_SEC"
ENV_MIN_ENTRY = "HERDR_TRACK_MIN_ENTRY_SEC"
ENV_LOG_LEVEL = "HERDR_TRACK_LOG_LEVEL"
ENV_ACTIVE = "HERDR_TRACK_ACTIVE_STATUSES"

KNOWN_KEYS = ("idleTimeoutSec", "pollIntervalSec", "snapshotIntervalSec",
              "activeStatuses", "minEntrySec", "logLevel")

LOG_LEVELS = ("error", "warn", "info", "debug")


class ConfigError(Exception):
    """Fatal configuration problem. The message is user-facing."""


# Herdr resolves both of these through the XDG base directories -- verified against
# `herdr plugin config-dir` and against the state directory it creates at install, with
# and without XDG_CONFIG_HOME / XDG_STATE_HOME set. The env vars above are what a hook
# gets; these fallbacks are for a run the user starts, which must land in the same place.
# A relative XDG value is invalid per the spec and ignored.


def config_dir(env=None):
    env = env or os.environ
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
    env = env or os.environ
    from_env = env.get("HERDR_PLUGIN_STATE_DIR")
    if from_env:
        return from_env
    base = env.get("XDG_STATE_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.expanduser(os.path.join("~", ".local", "state"))
    return os.path.join(base, "herdr", "plugins", PLUGIN_ID)


class Config(object):
    __slots__ = ("idle_timeout_sec", "poll_interval_sec", "snapshot_interval_sec",
                 "active_statuses", "min_entry_sec", "log_level", "source_path",
                 "source", "warnings")

    def __init__(self):
        self.idle_timeout_sec = 60.0
        self.poll_interval_sec = 10.0      # how often the screen hash is sampled
        self.snapshot_interval_sec = 2.0   # how often focus/status are polled
        self.active_statuses = ["working"]
        self.min_entry_sec = 30.0
        self.log_level = "info"
        self.source_path = None
        self.source = "defaults"
        self.warnings = []


def _number(raw, key, where, minimum=0.0, allow_zero=False):
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError("%r must be a number, got %r\n  %s" % (key, raw, where))
    if value < minimum or (value == 0 and not allow_zero):
        raise ConfigError("%r must be greater than %g, got %r\n  %s"
                          % (key, minimum, raw, where))
    return value


def load(env=None):
    env = env or os.environ
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

    if "idleTimeoutSec" in raw:
        cfg.idle_timeout_sec = _number(raw["idleTimeoutSec"], "idleTimeoutSec", where)
    if "pollIntervalSec" in raw:
        cfg.poll_interval_sec = _number(raw["pollIntervalSec"], "pollIntervalSec", where)
    if "snapshotIntervalSec" in raw:
        cfg.snapshot_interval_sec = _number(raw["snapshotIntervalSec"],
                                            "snapshotIntervalSec", where)
    if "minEntrySec" in raw:
        # Zero is meaningful here: keep every entry, however short.
        cfg.min_entry_sec = _number(raw["minEntrySec"], "minEntrySec", where,
                                    allow_zero=True)

    if "activeStatuses" in raw:
        statuses = raw["activeStatuses"]
        if not isinstance(statuses, list) or not statuses:
            raise ConfigError("\"activeStatuses\" must be a non-empty array of "
                              "strings\n  %s" % where)
        cleaned = [s for s in statuses if isinstance(s, str)]
        if not cleaned:
            raise ConfigError("\"activeStatuses\" held no strings\n  %s" % where)
        cfg.active_statuses = cleaned

    if "logLevel" in raw:
        if raw["logLevel"] not in LOG_LEVELS:
            raise ConfigError("\"logLevel\" must be one of %s, got %r\n  %s"
                              % (" | ".join(LOG_LEVELS), raw["logLevel"], where))
        cfg.log_level = raw["logLevel"]

    _apply_env(cfg, env)

    if cfg.idle_timeout_sec <= cfg.poll_interval_sec:
        cfg.warnings.append(
            "idleTimeoutSec (%g) is not larger than pollIntervalSec (%g): activity may "
            "time out before it is ever sampled" % (cfg.idle_timeout_sec,
                                                    cfg.poll_interval_sec))
    return cfg


def _apply_env(cfg, env):
    if env.get(ENV_IDLE_TIMEOUT):
        cfg.idle_timeout_sec = _number(env[ENV_IDLE_TIMEOUT], ENV_IDLE_TIMEOUT, "(env)")
    if env.get(ENV_POLL):
        cfg.poll_interval_sec = _number(env[ENV_POLL], ENV_POLL, "(env)")
    if env.get(ENV_SNAPSHOT):
        cfg.snapshot_interval_sec = _number(env[ENV_SNAPSHOT], ENV_SNAPSHOT, "(env)")
    if env.get(ENV_MIN_ENTRY) is not None and env.get(ENV_MIN_ENTRY) != "":
        cfg.min_entry_sec = _number(env[ENV_MIN_ENTRY], ENV_MIN_ENTRY, "(env)",
                                    allow_zero=True)
    if env.get(ENV_ACTIVE):
        cfg.active_statuses = [s for s in env[ENV_ACTIVE].split(",") if s]
    if env.get(ENV_LOG_LEVEL):
        if env[ENV_LOG_LEVEL] not in LOG_LEVELS:
            raise ConfigError("%s must be one of %s" % (ENV_LOG_LEVEL,
                                                        " | ".join(LOG_LEVELS)))
        cfg.log_level = env[ENV_LOG_LEVEL]
