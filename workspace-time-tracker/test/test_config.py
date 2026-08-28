"""Config loading for the tracker."""

import _support  # noqa: F401
import json
import os
import unittest

import config as config_mod
from _support import TempDirCase


class ConfigTest(TempDirCase):
    def env(self, **extra):
        env = {"HERDR_PLUGIN_CONFIG_DIR": self.path("cfg"),
               "HERDR_PLUGIN_STATE_DIR": self.path("state")}
        env.update(extra)
        return env

    def write(self, payload):
        os.makedirs(self.path("cfg"), exist_ok=True)
        with open(self.path("cfg", "config.json"), "w") as fh:
            fh.write(payload if isinstance(payload, str) else json.dumps(payload))

    def test_defaults_without_a_file(self):
        cfg = config_mod.load(self.env())
        self.assertEqual(cfg.source, "defaults")
        self.assertEqual(cfg.idle_timeout_sec, 60.0)
        self.assertEqual(cfg.poll_interval_sec, 10.0)
        self.assertEqual(cfg.snapshot_interval_sec, 2.0)
        self.assertEqual(cfg.min_entry_sec, 30.0)
        self.assertEqual(cfg.active_statuses, ["working"])
        self.assertEqual(cfg.warnings, [])

    def test_file_overrides(self):
        self.write({"idleTimeoutSec": 120, "minEntrySec": 5})
        cfg = config_mod.load(self.env())
        self.assertEqual(cfg.idle_timeout_sec, 120.0)
        self.assertEqual(cfg.min_entry_sec, 5.0)

    def test_min_entry_zero_is_allowed(self):
        """Zero is meaningful: keep every entry however short."""
        self.write({"minEntrySec": 0})
        self.assertEqual(config_mod.load(self.env()).min_entry_sec, 0.0)

    def test_zero_idle_timeout_is_rejected(self):
        self.write({"idleTimeoutSec": 0})
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.env())

    def test_comments_are_tolerated(self):
        self.write('{\n  // how long\n  "idleTimeoutSec": 90,\n}\n')
        self.assertEqual(config_mod.load(self.env()).idle_timeout_sec, 90.0)

    def test_unknown_keys_warn(self):
        self.write({"nope": 1})
        self.assertTrue(any("nope" in w for w in config_mod.load(self.env()).warnings))

    def test_idle_timeout_below_poll_interval_warns(self):
        """Otherwise activity could time out before it is ever sampled."""
        self.write({"idleTimeoutSec": 5, "pollIntervalSec": 10})
        cfg = config_mod.load(self.env())
        self.assertTrue(any("pollIntervalSec" in w for w in cfg.warnings))

    def test_bad_json_is_fatal(self):
        self.write("{nope")
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.env())

    def test_bad_log_level_is_fatal(self):
        self.write({"logLevel": "loud"})
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.env())

    def test_empty_active_statuses_is_fatal(self):
        self.write({"activeStatuses": []})
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.env())

    def test_env_overrides_win(self):
        self.write({"idleTimeoutSec": 60})
        cfg = config_mod.load(self.env(HERDR_TRACK_IDLE_TIMEOUT_SEC="3"))
        self.assertEqual(cfg.idle_timeout_sec, 3.0)

    def test_env_min_entry_zero_is_honoured(self):
        cfg = config_mod.load(self.env(HERDR_TRACK_MIN_ENTRY_SEC="0"))
        self.assertEqual(cfg.min_entry_sec, 0.0)


class SessionNameTest(unittest.TestCase):
    def test_named_and_default_sessions(self):
        import main
        self.assertEqual(
            main.session_name("/home/u/.config/herdr/sessions/probe/herdr.sock"),
            "probe")
        self.assertEqual(main.session_name("/home/u/.config/herdr/herdr.sock"),
                         "default")
        self.assertEqual(main.session_name(None), "unknown")


class FocusedWorkspaceTest(unittest.TestCase):
    def test_label_from_workspaces_and_cwd_from_the_focused_pane(self):
        import main
        snapshot = {
            "focused_workspace_id": "w1", "focused_pane_id": "w1:p2",
            "workspaces": [{"workspace_id": "w1", "label": "alpha"},
                           {"workspace_id": "w2", "label": "beta"}],
            "panes": [{"pane_id": "w1:p1", "cwd": "/wrong"},
                      {"pane_id": "w1:p2", "cwd": "/right"}],
        }
        self.assertEqual(main.focused_workspace(snapshot),
                         {"workspace_id": "w1", "label": "alpha", "cwd": "/right"})

    def test_no_focus_is_none(self):
        import main
        self.assertIsNone(main.focused_workspace({"workspaces": [], "panes": []}))


class XdgBaseDirTest(unittest.TestCase):
    """The fallbacks used when the *user* starts a command, not Herdr.

    Herdr injects HERDR_PLUGIN_{CONFIG,STATE}_DIR into hooks but not into a terminal, and
    it resolves both through the XDG base directories. If these fallbacks disagreed, a
    hand-run `doctor` would read a different directory than the daemon writes -- silently
    empty, for anyone who sets XDG_STATE_HOME.
    """

    def test_herdrs_own_env_wins_over_everything(self):
        env = {"HERDR_PLUGIN_CONFIG_DIR": "/from/herdr/cfg",
               "HERDR_PLUGIN_STATE_DIR": "/from/herdr/state",
               "XDG_CONFIG_HOME": "/xdg/cfg", "XDG_STATE_HOME": "/xdg/state"}
        self.assertEqual(config_mod.config_dir(env), "/from/herdr/cfg")
        self.assertEqual(config_mod.state_dir(env), "/from/herdr/state")

    def test_xdg_home_is_honoured(self):
        env = {"XDG_CONFIG_HOME": "/xdg/cfg", "XDG_STATE_HOME": "/xdg/state"}
        self.assertEqual(config_mod.config_dir(env),
                         "/xdg/cfg/herdr/plugins/config/" + config_mod.PLUGIN_ID)
        self.assertEqual(config_mod.state_dir(env),
                         "/xdg/state/herdr/plugins/" + config_mod.PLUGIN_ID)

    def test_the_default_is_the_path_the_readme_hands_out(self):
        home = os.path.expanduser("~")
        self.assertEqual(
            config_mod.config_dir({}),
            os.path.join(home, ".config", "herdr", "plugins", "config",
                         config_mod.PLUGIN_ID))
        self.assertEqual(
            config_mod.state_dir({}),
            os.path.join(home, ".local", "state", "herdr", "plugins",
                         config_mod.PLUGIN_ID))

    def test_a_relative_xdg_value_is_ignored(self):
        # Invalid per the spec; falling back beats resolving it against whatever cwd the
        # command happened to be run from.
        env = {"XDG_CONFIG_HOME": "relative/cfg", "XDG_STATE_HOME": "relative/state"}
        home = os.path.expanduser("~")
        self.assertTrue(config_mod.config_dir(env).startswith(home))
        self.assertTrue(config_mod.state_dir(env).startswith(home))


if __name__ == "__main__":
    unittest.main()
