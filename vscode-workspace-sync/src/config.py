"""Plugin configuration: `$HERDR_PLUGIN_CONFIG_DIR/config.json`.

Parsed with `jsonc.loads` so the user may comment their own config. JSON rather than
TOML deliberately: `tomllib` is Python 3.11+ and this plugin targets 3.9.

Three keys only. Earlier drafts also had `excludePaths`, `excludeLabels`,
`useSpaceLabels`, `debounceMs` and `sessionSocket`; they were cut as unearned surface.
Label-naming (previously `useSpaceLabels`) is now always on -- see `folders.folder_name`.
"""

import os

import jsonc

PLUGIN_ID = "vscode-workspace-sync"

ENV_WORKSPACE_FILE = "HERDR_VSCODE_SYNC_WORKSPACE_FILE"
ENV_FAKE_SNAPSHOT = "HERDR_VSCODE_SYNC_FAKE_SNAPSHOT"

MODE_MIRROR = "mirror"
MODE_ACTIVE = "active"
MODES = (MODE_MIRROR, MODE_ACTIVE)

KNOWN_KEYS = ("workspaceFile", "mode", "pinnedFolders")


class ConfigError(Exception):
    """Fatal configuration problem. The message is user-facing."""


def config_dir():
    """The plugin's config directory, from Herdr's env or the documented default."""
    from_env = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if from_env:
        return from_env
    return os.path.expanduser(
        os.path.join("~", ".config", "herdr", "plugins", "config", PLUGIN_ID)
    )


def config_path():
    return os.path.join(config_dir(), "config.json")


def _where():
    return "%s\n  (print it with: herdr plugin config-dir %s)" % (config_path(), PLUGIN_ID)


class Config(object):
    """Resolved configuration."""

    __slots__ = ("workspace_file", "mode", "pinned_folders", "source_path", "warnings")

    def __init__(self):
        self.workspace_file = None
        self.mode = MODE_MIRROR
        self.pinned_folders = []
        self.source_path = None
        self.warnings = []


def load(env=None):
    """Read, validate and resolve the config. Raises :class:`ConfigError`.

    The `HERDR_VSCODE_SYNC_WORKSPACE_FILE` env override wins over the file, and makes
    an otherwise-absent config file acceptable.
    """
    if env is None:
        env = os.environ
    cfg = Config()
    cfg.source_path = config_path()

    override = env.get(ENV_WORKSPACE_FILE)
    raw = {}
    if os.path.exists(cfg.source_path):
        try:
            with open(cfg.source_path, "r") as fh:
                text = fh.read()
        except (IOError, OSError) as exc:
            raise ConfigError("cannot read config file: %s\n  %s" % (exc, _where()))
        try:
            raw = jsonc.loads(text)
        except ValueError as exc:
            raise ConfigError("config file is not valid JSON: %s\n  %s" % (exc, _where()))
        if not isinstance(raw, dict):
            raise ConfigError("config file must contain a JSON object\n  %s" % _where())
    elif not override:
        raise ConfigError(
            "no config file. Create it with at least a \"workspaceFile\" key:\n  %s"
            % _where()
        )

    for key in raw:
        if key not in KNOWN_KEYS:
            cfg.warnings.append("unknown config key %r (ignored)" % key)

    workspace_file = override or raw.get("workspaceFile")
    if not workspace_file or not isinstance(workspace_file, str):
        raise ConfigError(
            "\"workspaceFile\" is required and must be an absolute path to a "
            "*.code-workspace file\n  %s" % _where()
        )
    cfg.workspace_file = os.path.abspath(os.path.expanduser(workspace_file))

    cfg.mode = raw.get("mode", MODE_MIRROR)
    if cfg.mode not in MODES:
        raise ConfigError(
            "\"mode\" must be one of %s, got %r\n  %s"
            % (" | ".join(repr(m) for m in MODES), cfg.mode, _where())
        )

    pinned = raw.get("pinnedFolders")
    if pinned is None:
        pinned = []
    if not isinstance(pinned, list):
        raise ConfigError("pinnedFolders must be an array of strings in %s" % _where())
    for item in pinned:
        if not isinstance(item, str):
            cfg.warnings.append("pinnedFolders: ignoring non-string entry %r" % item)
            continue
        cfg.pinned_folders.append(os.path.abspath(os.path.expanduser(item)))

    return cfg


def named_session(env=None):
    """A reason to skip, when this is a *named* Herdr session -- else None.

    Plugin registration is global (`~/.config/herdr/plugins.json` is not session-scoped),
    so every running session's server runs this plugin. Without a guard, two sessions
    would each rewrite the one configured workspace file from its own Space list and the
    folders would flap. Only the default session syncs; a named session's socket lives
    under `.../sessions/<name>/`.
    """
    if env is None:
        env = os.environ
    socket_path = env.get("HERDR_SOCKET_PATH")
    if not socket_path:
        return None
    if "sessions" in socket_path.replace(os.sep, "/").split("/"):
        return "socket %s belongs to a named Herdr session; only the default session syncs" % (
            socket_path,
        )
    return None
