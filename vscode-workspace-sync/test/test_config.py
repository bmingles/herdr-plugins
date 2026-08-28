import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _support  # noqa: E402

import config  # noqa: E402


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="vws-cfg-")
        self.target = os.path.join(self.tmp, "w.code-workspace")
        with open(self.target, "w") as fh:
            fh.write("{}\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_config(self, text):
        cfg_dir = os.path.join(self.tmp, "cfgdir")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "config.json"), "w") as fh:
            fh.write(text)
        return cfg_dir

    def load(self, raw=None, text=None, env_extra=None):
        if text is None:
            body = dict(raw or {})
            body.setdefault("workspaceFile", self.target)
            text = json.dumps(body)
        cfg_dir = self.write_config(text)
        env = {"HERDR_PLUGIN_CONFIG_DIR": cfg_dir}
        env.update(env_extra or {})
        saved = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = cfg_dir
        try:
            return config.load(env)
        finally:
            if saved is None:
                del os.environ["HERDR_PLUGIN_CONFIG_DIR"]
            else:
                os.environ["HERDR_PLUGIN_CONFIG_DIR"] = saved


class TestLoad(ConfigCase):
    def test_defaults(self):
        cfg = self.load({})
        self.assertEqual(cfg.mode, "mirror")
        self.assertEqual(cfg.pinned_folders, [])

    def test_comments_and_trailing_commas_allowed(self):
        text = '{\n  // a comment\n  "workspaceFile": "%s",\n}\n' % self.target
        cfg = self.load(text=text)
        self.assertEqual(cfg.workspace_file, self.target)

    def test_tilde_expansion(self):
        cfg = self.load({"workspaceFile": "~/w.code-workspace",
                         "pinnedFolders": ["~/pinned"]})
        home = os.path.expanduser("~")
        self.assertEqual(cfg.workspace_file, os.path.join(home, "w.code-workspace"))
        self.assertEqual(cfg.pinned_folders, [os.path.join(home, "pinned")])

    def test_env_override_wins(self):
        other = os.path.join(self.tmp, "other.code-workspace")
        cfg = self.load({"workspaceFile": self.target},
                        env_extra={config.ENV_WORKSPACE_FILE: other})
        self.assertEqual(cfg.workspace_file, other)

    def test_unknown_key_is_a_warning_not_an_error(self):
        cfg = self.load({"nope": 1, "alsoNope": 2})
        self.assertEqual(len(cfg.warnings), 2)
        self.assertTrue(any("nope" in w for w in cfg.warnings))

    def test_missing_workspace_file_is_fatal_and_names_the_config_path(self):
        with self.assertRaises(config.ConfigError) as ctx:
            self.load({"workspaceFile": None})
        message = str(ctx.exception)
        self.assertIn("config.json", message)
        self.assertIn("herdr plugin config-dir vscode-workspace-sync", message)

    def test_missing_config_file_is_fatal_and_names_the_config_path(self):
        cfg_dir = os.path.join(self.tmp, "empty-cfgdir")
        os.makedirs(cfg_dir)
        saved = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = cfg_dir
        try:
            with self.assertRaises(config.ConfigError) as ctx:
                config.load({"HERDR_PLUGIN_CONFIG_DIR": cfg_dir})
        finally:
            if saved is None:
                del os.environ["HERDR_PLUGIN_CONFIG_DIR"]
            else:
                os.environ["HERDR_PLUGIN_CONFIG_DIR"] = saved
        self.assertIn(os.path.join(cfg_dir, "config.json"), str(ctx.exception))

    def test_env_override_makes_a_missing_config_file_acceptable(self):
        cfg_dir = os.path.join(self.tmp, "empty2")
        os.makedirs(cfg_dir)
        saved = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = cfg_dir
        try:
            cfg = config.load({"HERDR_PLUGIN_CONFIG_DIR": cfg_dir,
                               config.ENV_WORKSPACE_FILE: self.target})
        finally:
            if saved is None:
                del os.environ["HERDR_PLUGIN_CONFIG_DIR"]
            else:
                os.environ["HERDR_PLUGIN_CONFIG_DIR"] = saved
        self.assertEqual(cfg.workspace_file, self.target)

    def test_bad_json_is_fatal(self):
        with self.assertRaises(config.ConfigError):
            self.load(text="{ not json")

    def test_bad_mode_is_fatal(self):
        with self.assertRaises(config.ConfigError):
            self.load({"mode": "sideways"})

    def test_active_mode_accepted_with_no_pinned_folders(self):
        # Never a validation error: unpinned `active` is supported, just costlier.
        cfg = self.load({"mode": "active"})
        self.assertEqual(cfg.mode, "active")
        self.assertEqual(cfg.pinned_folders, [])



class TestResolveSessionName(unittest.TestCase):
    """Derivation from `$HERDR_SOCKET_PATH`, per probe 11's recorded layouts."""

    def name(self, socket_path):
        env = {} if socket_path is None else {config.ENV_SOCKET_PATH: socket_path}
        return config.resolve_session_name(env)

    def test_unset_socket_is_the_default_session(self):
        self.assertEqual(self.name(None), "default")

    def test_default_session_socket(self):
        self.assertEqual(self.name("/Users/x/.config/herdr/herdr.sock"), "default")

    def test_named_session_socket(self):
        self.assertEqual(
            self.name("/Users/x/.config/herdr/sessions/probe/herdr.sock"), "probe"
        )

    def test_session_literally_named_sessions(self):
        self.assertEqual(
            self.name("/Users/x/.config/herdr/sessions/sessions/herdr.sock"), "sessions"
        )

    def test_socket_somewhere_else_entirely_is_default(self):
        self.assertEqual(self.name("/tmp/herdr.sock"), "default")


class TestSessionMap(ConfigCase):
    """The four resolution rules in `config.load`."""

    def setUp(self):
        ConfigCase.setUp(self)
        self.other = os.path.join(self.tmp, "other.code-workspace")
        with open(self.other, "w") as fh:
            fh.write("{}\n")

    def as_session(self, name):
        return {config.ENV_SOCKET_PATH: "/x/.config/herdr/sessions/%s/herdr.sock" % name}

    def load_raw(self, body, env_extra=None):
        return self.load(text=json.dumps(body), env_extra=env_extra)

    # -- rule 2 ------------------------------------------------------------

    def test_named_session_uses_its_own_entry(self):
        cfg = self.load_raw(
            {"sessions": {"work": {"workspaceFile": self.other}}},
            env_extra=self.as_session("work"),
        )
        self.assertEqual(cfg.session_name, "work")
        self.assertEqual(cfg.workspace_file, self.other)
        self.assertIsNone(cfg.skip_reason)
        self.assertIn("work", cfg.selection)

    def test_entry_inherits_mode_and_pinned_from_top_level(self):
        cfg = self.load_raw(
            {
                "mode": "active",
                "pinnedFolders": [self.tmp],
                "sessions": {"work": {"workspaceFile": self.other}},
            },
            env_extra=self.as_session("work"),
        )
        self.assertEqual(cfg.mode, "active")
        self.assertEqual(cfg.pinned_folders, [self.tmp])

    def test_entry_overrides_mode_and_pinned(self):
        cfg = self.load_raw(
            {
                "mode": "active",
                "pinnedFolders": [self.tmp],
                "sessions": {
                    "work": {
                        "workspaceFile": self.other,
                        "mode": "mirror",
                        "pinnedFolders": [],
                    }
                },
            },
            env_extra=self.as_session("work"),
        )
        self.assertEqual(cfg.mode, "mirror")
        self.assertEqual(cfg.pinned_folders, [])

    # -- rule 3 ------------------------------------------------------------

    def test_default_session_falls_back_to_top_level(self):
        cfg = self.load_raw(
            {
                "workspaceFile": self.target,
                "sessions": {"work": {"workspaceFile": self.other}},
            }
        )
        self.assertEqual(cfg.session_name, "default")
        self.assertEqual(cfg.workspace_file, self.target)
        self.assertEqual(cfg.selection, config.SELECTION_TOP_LEVEL)

    def test_explicit_default_entry_beats_top_level(self):
        cfg = self.load_raw(
            {
                "workspaceFile": self.target,
                "sessions": {"default": {"workspaceFile": self.other}},
            }
        )
        self.assertEqual(cfg.workspace_file, self.other)

    # -- rule 4 ------------------------------------------------------------

    def test_unmapped_named_session_skips(self):
        cfg = self.load_raw(
            {"sessions": {"work": {"workspaceFile": self.other}}},
            env_extra=self.as_session("play"),
        )
        self.assertIsNone(cfg.workspace_file)
        self.assertIsNotNone(cfg.skip_reason)
        self.assertIn("play", cfg.skip_reason)
        self.assertIn("work", cfg.skip_reason)
        self.assertEqual(cfg.selection, config.SELECTION_UNMAPPED)

    def test_named_session_with_no_sessions_key_skips_as_before(self):
        # The shipped behaviour, reached by rule 4 rather than the deleted
        # `named_session()`: a flat config never syncs a named session.
        cfg = self.load_raw(
            {"workspaceFile": self.target}, env_extra=self.as_session("other")
        )
        self.assertIsNone(cfg.workspace_file)
        self.assertIn("only the default session syncs", cfg.skip_reason)

    def test_default_session_skips_when_only_named_sessions_are_mapped(self):
        cfg = self.load_raw({"sessions": {"work": {"workspaceFile": self.other}}})
        self.assertEqual(cfg.session_name, "default")
        self.assertIsNone(cfg.workspace_file)
        self.assertIsNotNone(cfg.skip_reason)

    def test_env_override_beats_an_unmapped_session(self):
        cfg = self.load_raw(
            {"sessions": {"work": {"workspaceFile": self.other}}},
            env_extra=dict(
                self.as_session("play"), **{config.ENV_WORKSPACE_FILE: self.target}
            ),
        )
        self.assertEqual(cfg.workspace_file, self.target)
        self.assertIsNone(cfg.skip_reason)

    # -- top-level requirement --------------------------------------------

    def test_top_level_workspace_file_optional_when_sessions_present(self):
        cfg = self.load_raw(
            {"sessions": {"work": {"workspaceFile": self.other}}},
            env_extra=self.as_session("work"),
        )
        self.assertEqual(cfg.workspace_file, self.other)

    def test_still_fatal_with_neither_top_level_nor_sessions(self):
        with self.assertRaises(config.ConfigError) as ctx:
            self.load_raw({"sessions": {}})
        self.assertIn("workspaceFile", str(ctx.exception))

    # -- uniqueness --------------------------------------------------------

    def test_two_entries_sharing_a_file_is_fatal_and_names_both(self):
        with self.assertRaises(config.ConfigError) as ctx:
            self.load_raw(
                {
                    "sessions": {
                        "work": {"workspaceFile": self.other},
                        "oss": {"workspaceFile": self.other},
                    }
                }
            )
        message = str(ctx.exception)
        self.assertIn("work", message)
        self.assertIn("oss", message)
        self.assertIn(os.path.realpath(self.other), message)

    def test_entry_colliding_with_reachable_top_level_is_fatal(self):
        with self.assertRaises(config.ConfigError):
            self.load_raw(
                {
                    "workspaceFile": self.target,
                    "sessions": {"work": {"workspaceFile": self.target}},
                }
            )

    def test_default_entry_may_shadow_the_top_level_file(self):
        # The top level is unreachable once `sessions.default` exists (rule 3 is only
        # tried when there is no entry), so this is not a collision.
        cfg = self.load_raw(
            {
                "workspaceFile": self.target,
                "sessions": {"default": {"workspaceFile": self.target}},
            }
        )
        self.assertEqual(cfg.workspace_file, self.target)

    # -- entry validation --------------------------------------------------

    def test_entry_without_workspace_file_is_fatal_and_names_the_session(self):
        with self.assertRaises(config.ConfigError) as ctx:
            self.load_raw({"sessions": {"work": {"mode": "active"}}})
        self.assertIn("work", str(ctx.exception))

    def test_entry_that_is_not_an_object_is_fatal(self):
        with self.assertRaises(config.ConfigError):
            self.load_raw({"sessions": {"work": "~/w.code-workspace"}})

    def test_sessions_that_is_not_an_object_is_fatal(self):
        with self.assertRaises(config.ConfigError):
            self.load_raw({"workspaceFile": self.target, "sessions": ["work"]})

    def test_bad_mode_inside_an_entry_is_fatal(self):
        with self.assertRaises(config.ConfigError):
            self.load_raw(
                {"sessions": {"work": {"workspaceFile": self.other, "mode": "sideways"}}}
            )

    def test_unknown_key_inside_an_entry_is_a_warning(self):
        cfg = self.load_raw(
            {"sessions": {"work": {"workspaceFile": self.other, "nope": 1}}},
            env_extra=self.as_session("work"),
        )
        self.assertTrue(any("nope" in w and "work" in w for w in cfg.warnings))

    def test_tilde_expansion_inside_an_entry(self):
        cfg = self.load_raw(
            {"sessions": {"work": {"workspaceFile": "~/w.code-workspace"}}},
            env_extra=self.as_session("work"),
        )
        self.assertEqual(
            cfg.workspace_file, os.path.join(os.path.expanduser("~"), "w.code-workspace")
        )


class XdgBaseDirTest(unittest.TestCase):
    """The fallback used when the *user* starts `sync --doctor`, not Herdr.

    Herdr injects HERDR_PLUGIN_CONFIG_DIR into hooks but not into a terminal, and resolves
    it through XDG_CONFIG_HOME. A disagreement here would have a hand-run `--doctor`
    reporting "no config file" while the hooks read one perfectly well.
    """

    def test_herdrs_own_env_wins(self):
        self.assertEqual(
            config.config_dir({"HERDR_PLUGIN_CONFIG_DIR": "/from/herdr",
                               "XDG_CONFIG_HOME": "/xdg"}),
            "/from/herdr")

    def test_xdg_config_home_is_honoured(self):
        self.assertEqual(config.config_dir({"XDG_CONFIG_HOME": "/xdg"}),
                         "/xdg/herdr/plugins/config/" + config.PLUGIN_ID)

    def test_the_default_is_the_path_the_readme_hands_out(self):
        self.assertEqual(
            config.config_dir({}),
            os.path.join(os.path.expanduser("~"), ".config", "herdr", "plugins",
                         "config", config.PLUGIN_ID))

    def test_a_relative_xdg_value_is_ignored(self):
        self.assertTrue(
            config.config_dir({"XDG_CONFIG_HOME": "relative"}).startswith(
                os.path.expanduser("~")))


if __name__ == "__main__":
    unittest.main()
