"""Config loading: defaults, file, validation, env overrides."""

import _support  # noqa: F401
import json
import os
import sys
import unittest

import config as config_mod
from _support import TempDirCase


class ConfigTest(TempDirCase):
    def env(self, **extra):
        env = {"HERDR_PLUGIN_CONFIG_DIR": self.path("cfg"),
               "HERDR_PLUGIN_STATE_DIR": self.path("state")}
        env.update(extra)
        return env

    def write(self, text):
        os.makedirs(self.path("cfg"), exist_ok=True)
        with open(self.path("cfg", "config.json"), "w") as fh:
            fh.write(text)

    def test_works_with_no_config_file_at_all(self):
        cfg = config_mod.load(self.env())
        self.assertEqual(cfg.source, "defaults")
        self.assertEqual(cfg.idle_grace_sec, 60.0)
        self.assertEqual(cfg.poll_interval_sec, 2.0)
        self.assertEqual(cfg.active_statuses, ["working"])
        self.assertEqual(cfg.warnings, [])

    def test_platform_default_inhibitor(self):
        cfg = config_mod.load(self.env())
        if sys.platform == "darwin":
            self.assertEqual(cfg.inhibitor_command, ["caffeinate", "-i", "-s"])
        else:
            self.assertEqual(cfg.inhibitor_command[0], "systemd-inhibit")

    def test_macos_default_excludes_d_m_and_u(self):
        """-d is display-only, -m is a spinning-disk no-op, -u expires after 5s."""
        self.assertEqual(config_mod.MACOS_INHIBITOR, ["caffeinate", "-i", "-s"])
        for flag in ("-d", "-m", "-u"):
            self.assertNotIn(flag, config_mod.MACOS_INHIBITOR)

    def test_file_overrides_defaults(self):
        self.write(json.dumps({"idleGraceSec": 5, "pollIntervalSec": 0.5,
                               "activeStatuses": ["working", "blocked"]}))
        cfg = config_mod.load(self.env())
        self.assertEqual(cfg.source, "file")
        self.assertEqual(cfg.idle_grace_sec, 5.0)
        self.assertEqual(cfg.poll_interval_sec, 0.5)
        self.assertEqual(cfg.active_statuses, ["working", "blocked"])

    def test_comments_and_trailing_commas_are_tolerated(self):
        self.write('{\n  // how long to wait\n  "idleGraceSec": 30,\n}\n')
        self.assertEqual(config_mod.load(self.env()).idle_grace_sec, 30.0)

    def test_unknown_key_warns_but_does_not_fail(self):
        self.write(json.dumps({"nonsense": 1}))
        cfg = config_mod.load(self.env())
        self.assertTrue(any("nonsense" in w for w in cfg.warnings))

    def test_bad_json_is_fatal(self):
        self.write("{not json")
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.env())

    def test_non_object_config_is_fatal(self):
        self.write("[1,2,3]")
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.env())

    def test_non_positive_grace_is_fatal(self):
        for bad in (0, -1):
            self.write(json.dumps({"idleGraceSec": bad}))
            with self.assertRaises(config_mod.ConfigError):
                config_mod.load(self.env())

    def test_non_numeric_grace_is_fatal(self):
        self.write(json.dumps({"idleGraceSec": "soon"}))
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.env())

    def test_empty_active_statuses_is_fatal(self):
        self.write(json.dumps({"activeStatuses": []}))
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.env())

    def test_bad_inhibitor_command_is_fatal(self):
        self.write(json.dumps({"inhibitorCommand": "caffeinate -i"}))
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.env())

    def test_null_inhibitor_command_falls_back_to_platform_default(self):
        self.write(json.dumps({"inhibitorCommand": None}))
        cfg = config_mod.load(self.env())
        self.assertEqual(cfg.inhibitor_command, config_mod.platform_inhibitor())

    def test_bad_log_level_is_fatal(self):
        self.write(json.dumps({"logLevel": "shout"}))
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.env())

    def test_env_overrides_win_over_the_file(self):
        self.write(json.dumps({"idleGraceSec": 60}))
        cfg = config_mod.load(self.env(HERDR_CAFFEINATE_IDLE_GRACE_SEC="2"))
        self.assertEqual(cfg.idle_grace_sec, 2.0)

    def test_env_inhibitor_must_be_a_json_array(self):
        with self.assertRaises(config_mod.ConfigError):
            config_mod.load(self.env(HERDR_CAFFEINATE_INHIBITOR_COMMAND="caffeinate"))

    def test_env_inhibitor_array_is_accepted(self):
        cfg = config_mod.load(
            self.env(HERDR_CAFFEINATE_INHIBITOR_COMMAND='["/bin/true","x"]'))
        self.assertEqual(cfg.inhibitor_command, ["/bin/true", "x"])

    def test_state_dir_honours_the_plugin_env(self):
        self.assertEqual(config_mod.state_dir(self.env()), self.path("state"))


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
