"""Plugin configuration: `$HERDR_PLUGIN_CONFIG_DIR/config.json`.

Parsed with `jsonc.loads` so the user may comment their own config. JSON rather than
TOML deliberately: `tomllib` is Python 3.11+ and this plugin targets 3.9.

Four keys. `workspaceFile`, `mode` and `pinnedFolders` configure one session; `sessions`
maps a Herdr session name to its own such config, because plugin registration is global
across sessions -- see `resolve_session_name` and README's "One workspace file per Herdr
session". Earlier drafts also had `excludePaths`, `excludeLabels`, `useSpaceLabels`,
`debounceMs` and `sessionSocket`; they were cut as unearned surface. Label-naming
(previously `useSpaceLabels`) is now always on -- see `folders.folder_name`.
"""

import os

import jsonc

PLUGIN_ID = "vscode-workspace-sync"

ENV_WORKSPACE_FILE = "HERDR_VSCODE_SYNC_WORKSPACE_FILE"
ENV_FAKE_SNAPSHOT = "HERDR_VSCODE_SYNC_FAKE_SNAPSHOT"
ENV_SOCKET_PATH = "HERDR_SOCKET_PATH"

DEFAULT_SESSION = "default"

MODE_MIRROR = "mirror"
MODE_ACTIVE = "active"
MODES = (MODE_MIRROR, MODE_ACTIVE)

KNOWN_KEYS = ("workspaceFile", "mode", "pinnedFolders", "sessions")
KNOWN_SESSION_KEYS = ("workspaceFile", "mode", "pinnedFolders")

SELECTION_TOP_LEVEL = "top-level workspaceFile"
SELECTION_UNMAPPED = "unmapped"


class ConfigError(Exception):
    """Fatal configuration problem. The message is user-facing."""


# Herdr resolves both of these through the XDG base directories -- verified against
# `herdr plugin config-dir` and against the state directory it creates at install, with
# and without XDG_CONFIG_HOME / XDG_STATE_HOME set. The env vars above are what a hook
# gets; these fallbacks are for a run the user starts, which must land in the same place.
# A relative XDG value is invalid per the spec and ignored.


def config_dir(env=None):
    """The plugin's config directory, from Herdr's env or the documented default."""
    env = env or os.environ
    from_env = env.get("HERDR_PLUGIN_CONFIG_DIR")
    if from_env:
        return from_env
    base = env.get("XDG_CONFIG_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.expanduser(os.path.join("~", ".config"))
    return os.path.join(base, "herdr", "plugins", "config", PLUGIN_ID)


def config_path():
    return os.path.join(config_dir(), "config.json")


def _where():
    return "%s\n  (print it with: herdr plugin config-dir %s)" % (config_path(), PLUGIN_ID)


def _norm(path):
    return os.path.abspath(os.path.expanduser(path))


def resolve_session_name(env=None):
    """The name of the Herdr session this run belongs to, from `$HERDR_SOCKET_PATH`.

    Verified against probe 11 in `docs/herdr-vscode-sync-facts.md`, which recorded both
    layouts from a live `herdr session list`::

        /Users/x/.config/herdr/herdr.sock                  -> "default"
        /Users/x/.config/herdr/sessions/probe/herdr.sock   -> "probe"

    The socket path is authoritative and already in the hook environment, so this never
    shells out to `herdr session list`. The **first** `sessions` component is used, which
    is also correct for a session literally named `sessions`
    (`.../sessions/sessions/herdr.sock` -> `"sessions"`).
    """
    if env is None:
        env = os.environ
    socket_path = env.get(ENV_SOCKET_PATH)
    if not socket_path:
        return DEFAULT_SESSION
    parts = socket_path.replace(os.sep, "/").split("/")
    try:
        index = parts.index("sessions")
    except ValueError:
        return DEFAULT_SESSION
    if index + 1 < len(parts) and parts[index + 1]:
        return parts[index + 1]
    return DEFAULT_SESSION


class SessionEntry(object):
    """One validated `sessions` entry.

    `mode` and `pinned_folders` are ``None`` when the entry did not set them, which is
    distinct from setting them empty: ``None`` inherits the top-level value.
    """

    __slots__ = ("workspace_file", "mode", "pinned_folders")

    def __init__(self, workspace_file, mode=None, pinned_folders=None):
        self.workspace_file = workspace_file
        self.mode = mode
        self.pinned_folders = pinned_folders

    def __repr__(self):  # pragma: no cover - debugging aid
        return "SessionEntry(workspace_file=%r, mode=%r, pinned_folders=%r)" % (
            self.workspace_file,
            self.mode,
            self.pinned_folders,
        )


class Config(object):
    """Resolved configuration for *this* session.

    `workspace_file` is ``None`` exactly when `skip_reason` is set: the session resolved
    to no configuration and the run must write nothing.
    """

    __slots__ = (
        "workspace_file",
        "mode",
        "pinned_folders",
        "source_path",
        "warnings",
        "session_name",
        "sessions",
        "selection",
        "skip_reason",
    )

    def __init__(self):
        self.workspace_file = None
        self.mode = MODE_MIRROR
        self.pinned_folders = []
        self.source_path = None
        self.warnings = []
        self.session_name = DEFAULT_SESSION
        self.sessions = {}
        self.selection = None
        self.skip_reason = None


def _validate_mode(value, context=""):
    if value not in MODES:
        raise ConfigError(
            "\"mode\" must be one of %s, got %r%s\n  %s"
            % (" | ".join(repr(m) for m in MODES), value, context, _where())
        )
    return value


def _parse_pinned(value, cfg, context=""):
    """Resolve a `pinnedFolders` array, or ``None`` when the key was absent."""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ConfigError(
            "pinnedFolders must be an array of strings%s in %s" % (context, _where())
        )
    pinned = []
    for item in value:
        if not isinstance(item, str):
            cfg.warnings.append(
                "pinnedFolders%s: ignoring non-string entry %r" % (context, item)
            )
            continue
        pinned.append(_norm(item))
    return pinned


def _parse_sessions(raw, cfg):
    """Validate and resolve the `sessions` map into ``{name: SessionEntry}``."""
    value = raw.get("sessions")
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            "\"sessions\" must be an object mapping a Herdr session name to its own "
            "config\n  %s" % _where()
        )

    sessions = {}
    for name in value:
        entry = value[name]
        context = " in sessions[%r]" % name
        if not isinstance(entry, dict):
            raise ConfigError(
                "sessions[%r] must be an object with at least a \"workspaceFile\"\n  %s"
                % (name, _where())
            )
        for key in entry:
            if key not in KNOWN_SESSION_KEYS:
                cfg.warnings.append("unknown config key %r%s (ignored)" % (key, context))

        workspace_file = entry.get("workspaceFile")
        if not workspace_file or not isinstance(workspace_file, str):
            raise ConfigError(
                "sessions[%r] needs a \"workspaceFile\": an absolute path to a "
                "*.code-workspace file. It is never inherited from the top level, so "
                "that two sessions cannot silently end up sharing one file.\n  %s"
                % (name, _where())
            )

        mode = entry.get("mode")
        if mode is not None:
            _validate_mode(mode, context)
        sessions[name] = SessionEntry(
            _norm(workspace_file), mode, _parse_pinned(entry.get("pinnedFolders"), cfg, context)
        )
    return sessions


def _check_unique_targets(sessions, top_workspace_file):
    """Fail if two resolvable configs claim the same file.

    Two Herdr servers writing one workspace file, each from its own Space list, is the
    exact failure the per-session mapping exists to prevent -- so a config that sets it
    up is fatal rather than a warning. The top-level file is only reachable when
    `sessions` has no `default` entry (resolution rule 3), so it is only a claimant then.
    """
    claims = [
        (name, os.path.realpath(sessions[name].workspace_file)) for name in sorted(sessions)
    ]
    if top_workspace_file and DEFAULT_SESSION not in sessions:
        claims.append((DEFAULT_SESSION, os.path.realpath(top_workspace_file)))

    owner = {}
    for name, real in claims:
        if real in owner:
            raise ConfigError(
                "sessions %r and %r both resolve to the same workspaceFile:\n    %s\n"
                "  Two Herdr sessions writing one file is what the per-session mapping "
                "exists to prevent; give each session its own file.\n  %s"
                % (owner[real], name, real, _where())
            )
        owner[real] = name


def load(env=None):
    """Read, validate and resolve the config for this session. Raises `ConfigError`.

    Resolution, in order:

    1. Resolve the session name from `$HERDR_SOCKET_PATH`.
    2. `sessions[name]` if present -- its `workspaceFile` is required and never
       inherited; `mode` and `pinnedFolders` fall back to the top-level values.
    3. Else the top-level config, but only for the `default` session.
    4. Else no configuration applies: `skip_reason` is set and `workspace_file` stays
       ``None``.

    With no `sessions` key, rules 3 and 4 are exactly the behaviour that shipped: only
    the default session syncs.

    The `HERDR_VSCODE_SYNC_WORKSPACE_FILE` env override wins over all of it -- including
    over a rule-4 skip, which is what makes it usable to test a named session by hand --
    and makes an otherwise-absent config file acceptable.
    """
    if env is None:
        env = os.environ
    cfg = Config()
    cfg.source_path = config_path()
    cfg.session_name = resolve_session_name(env)

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

    top_mode = _validate_mode(raw.get("mode", MODE_MIRROR))
    top_pinned = _parse_pinned(raw.get("pinnedFolders"), cfg)
    if top_pinned is None:
        top_pinned = []

    top_file = raw.get("workspaceFile")
    if not isinstance(top_file, str) or not top_file:
        top_file = None

    cfg.sessions = _parse_sessions(raw, cfg)
    _check_unique_targets(cfg.sessions, top_file)

    if not top_file and not cfg.sessions and not override:
        raise ConfigError(
            "\"workspaceFile\" is required and must be an absolute path to a "
            "*.code-workspace file\n  %s" % _where()
        )

    entry = cfg.sessions.get(cfg.session_name)
    if entry is not None:
        cfg.workspace_file = entry.workspace_file
        cfg.mode = top_mode if entry.mode is None else entry.mode
        cfg.pinned_folders = (
            list(top_pinned) if entry.pinned_folders is None else entry.pinned_folders
        )
        cfg.selection = "sessions[%r]" % cfg.session_name
    else:
        cfg.mode = top_mode
        cfg.pinned_folders = list(top_pinned)
        if cfg.session_name == DEFAULT_SESSION and top_file:
            cfg.workspace_file = _norm(top_file)
            cfg.selection = SELECTION_TOP_LEVEL
        else:
            cfg.selection = SELECTION_UNMAPPED
            cfg.skip_reason = _skip_reason(cfg, env)

    if override:
        cfg.workspace_file = _norm(override)
        cfg.skip_reason = None
        cfg.selection = "%s env override" % ENV_WORKSPACE_FILE

    return cfg


def _skip_reason(cfg, env):
    """Why this session syncs nothing -- the line the run logs before exiting 0."""
    socket_path = env.get(ENV_SOCKET_PATH) or "<unset>"
    if cfg.sessions:
        return (
            "session %r has no entry in \"sessions\" (configured: %s); nothing to sync"
            % (cfg.session_name, ", ".join(repr(n) for n in sorted(cfg.sessions)))
        )
    return (
        "socket %s belongs to Herdr session %r; only the default session syncs. Add a "
        "\"sessions\" map to give this session its own workspace file."
        % (socket_path, cfg.session_name)
    )
